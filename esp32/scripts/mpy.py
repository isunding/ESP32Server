"""把运行模块预编译成 .mpy 并部署到板子（或回退回 .py）。

为什么需要它：MicroPython 实测**优先加载 .py**，.mpy 只有在同名 .py 不
存在时才会被用上。所以部署 .mpy 必须同时删掉板上对应的 .py，只传文件
是没用的（__file__ 会仍然指向 .py）。这套「编译 → 上传 → 删 .py → 重启」
的流程容易漏步骤，固化成一条命令：

    python tests/mpy.py            # 切到 .mpy（推荐）
    python tests/mpy.py --revert   # 回退到 .py（出问题时用）

boot.py / main.py 不能预编译（固件直接 exec 源码），所以始终保留 .py。

实测收益（ESP32-D0WD-V3，6 个模块）：
    模块导入 1.74s -> 0.97s，服务就绪提前约 1.6s。
"""
import glob
import os
import site
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import flash  # 复用它的 wait_port / upload / boot_and_check / check_http

MODULES = ["netmgr", "httpsrv", "rap", "ntpsync", "ntpsrv", "cncal"]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "build_mpy")


def find_mpy_cross():
    """找 mpy-cross 可执行文件。

    注意：pip 可能装在和当前解释器不同的 Python 版本下（这台机器上 pip
    是 3.15、WorkBuddy 跑的是 3.13），所以不能只看 site.getusersitepackages()，
    要按 APPDATA 下的 Python3* 目录逐个扫。
    """
    import shutil
    cands = []
    for name in ("mpy-cross-v6.3", "mpy-cross"):
        w = shutil.which(name)
        if w:
            cands.append(w)
    appdata = os.environ.get("APPDATA") or ""
    if appdata:
        for pat in (r"Python\Python3*\Scripts\mpy-cross*.exe",
                    r"Python\Python3*\site-packages\mpy_cross_v6_3\mpy-cross.exe"):
            cands += glob.glob(os.path.join(appdata, pat))
    try:
        base = os.path.dirname(site.getusersitepackages())
        cands += glob.glob(os.path.join(base, "Scripts", "mpy-cross*.exe"))
        cands += glob.glob(os.path.join(site.getusersitepackages(),
                                        "mpy_cross_v6_3", "mpy-cross.exe"))
    except Exception:
        pass
    for sp in (getattr(site, "getsitepackages", lambda: [])() or []):
        cands += glob.glob(os.path.join(sp, "mpy_cross_v6_3", "mpy-cross.exe"))
    for f in cands:
        if os.path.isfile(f):
            return f
    raise SystemExit("找不到 mpy-cross，先执行: pip install mpy-cross-v6.3")


def build():
    cc = find_mpy_cross()
    ver = subprocess.run([cc, "--version"], capture_output=True, text=True).stdout.strip()
    print("[mpy] ", ver)
    os.makedirs(BUILD, exist_ok=True)
    total_src = total_mpy = 0
    for m in MODULES:
        src = os.path.join(ROOT, m + ".py")
        out = os.path.join(BUILD, m + ".mpy")
        r = subprocess.run([cc, "-O2", "-o", out, src], capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit("[mpy] 编译失败 %s: %s" % (m, r.stderr.strip()[:200]))
        a, b = os.path.getsize(src), os.path.getsize(out)
        total_src += a
        total_mpy += b
        print("   %-9s %6d -> %5d B" % (m, a, b))
    print("[mpy] 合计 %d -> %d B（省 %d B / %.0f%%）" % (
        total_src, total_mpy, total_src - total_mpy,
        100.0 * (total_src - total_mpy) / total_src))


def repl_cmds(lines):
    """进 REPL 逐行执行（每行必须是单行合法语句，不用缩进块）。"""
    import serial
    s = serial.Serial()
    s.port = os.environ.get("ESP32_PORT", "COM3")
    s.baudrate = 115200
    s.timeout = 0.3
    s.open()
    s.dtr = False
    s.rts = False
    time.sleep(0.8)
    # 两次 Ctrl-C：一次打断主循环，一次清掉可能残留的续行（`...`）状态
    s.write(b"\x03")
    time.sleep(0.5)
    s.write(b"\x03")
    time.sleep(0.6)
    s.read(32768)
    out = ""
    for ln in lines:
        s.write(ln.encode() + b"\r\n")
        time.sleep(0.45)
        out += s.read(8192).decode("utf-8", "replace")
    s.close()
    return out


def remove_on_board(names):
    """删板上的文件。

    必须写成「列表推导」这种单行表达式：REPL 收到 `if cond: stmt` 这种复合
    语句会进续行模式（提示 `...`），后面所有命令都被当成块内容吞掉。
    """
    lst = "(" + ",".join("'%s'" % n for n in names) + ")"
    lines = [
        "import os",
        "[os.remove(f) for f in %s if f in os.listdir()]" % lst,
        "print('LEFT', sorted(x for x in os.listdir() "
        "if x.endswith(('.py','.mpy'))))",
    ]
    out = repl_cmds(lines)
    for l in out.splitlines():
        # 命令本身含有 "LEFT" 字样，必须匹配输出行的开头，否则会认成回显
        if l.strip().startswith("LEFT"):
            print("[mpy] 板上:", l.strip())
            return True
    print("[mpy] 删除确认失败，输出尾部:", out.strip()[-200:])
    return False


def main():
    revert = "--revert" in sys.argv
    if not flash.wait_port():
        raise SystemExit("[mpy] 找不到串口")

    if revert:
        files = [m + ".py" for m in MODULES]
        print("[mpy] 回退：上传 .py 并删除 .mpy")
    else:
        build()
        # flash.upload 只认「无路径的文件名 → :同名」这种映射（带 "/" 会被
        # 当成子目录），所以先把 .mpy 复制到项目根再按文件名上传。
        files = []
        for m in MODULES:
            dst = os.path.join(ROOT, m + ".mpy")
            with open(os.path.join(BUILD, m + ".mpy"), "rb") as f:
                data = f.read()
            with open(dst, "wb") as f:
                f.write(data)
            files.append(m + ".mpy")
        print("[mpy] 切换：上传 .mpy 并删除 .py")

    if not flash.upload(files):
        raise SystemExit("[mpy] 上传失败")

    # 只有在另一套文件确认到位后才删旧的，避免两边都缺导致起不来
    if revert:
        remove_on_board([m + ".mpy" for m in MODULES])
    else:
        remove_on_board([m + ".py" for m in MODULES])

    flash.boot_and_check()
    raise SystemExit(0 if flash.check_http() else 1)


if __name__ == "__main__":
    main()
