"""升级工具（双击运行）：菜单式操作，等价于 scripts/ota.py 的各条命令。

打包成 ota/OTA升级.exe 后，双击即可用，不需要记命令；带参数运行时
直接转交给 ota.py，例如 `OTA升级.exe --only www/ui.js`。
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ota                    # noqa: E402
import pack_ota               # noqa: E402

DEFAULT_HOST = os.environ.get("ESP32_HOST", "10.0.0.100")


def locate():
    """找到仓库根目录。exe 可能放在 ota/ 或仓库根目录。"""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = os.path.dirname(os.path.dirname(HERE))
    for cand in (base, os.path.dirname(base)):
        if os.path.isdir(os.path.join(cand, "esp32-s3")):
            return cand
    return base


REPO = locate()
BOARD = os.path.join(REPO, "esp32-s3")
OTA_DIR = os.path.join(REPO, "ota")
ota.set_root(BOARD, REPO)


def args_ns(**kw):
    import argparse
    d = dict(host=DEFAULT_HOST, port=80, retry=3, timeout=60,
             no_reboot=False, board_dir=BOARD)
    d.update(kw)
    return argparse.Namespace(**d)


def clear():
    os.system("cls" if os.name == "nt" else "clear")


def hr(ch="-"):
    print(ch * 56)


def device_state():
    """返回 (health, ota)，都失败时返回 (None, None)。"""
    try:
        _, h = ota.api(DEFAULT_HOST, 80, "GET", "/api/health", timeout=4)
    except Exception:
        h = None
    try:
        _, o = ota.api(DEFAULT_HOST, 80, "GET", "/api/ota", timeout=4)
    except Exception:
        o = None
    return h, o


def show_header():
    clear()
    hr("=")
    print("  ESP32 终端服务器 · 升级工具")
    print("  设备 %s" % DEFAULT_HOST)
    hr("=")
    h, o = device_state()
    if not h or not h.get("ok"):
        print("  设备无响应：检查是否通电、与本机是否同一局域网")
    else:
        state = "待重启" if (o or {}).get("pending") else "运行中"
        print("  在线 · 版本 %s · %s · 已运行 %d 分钟"
              % ((o or {}).get("ver") or "-", state, (h.get("up") or 0) // 60))
    try:
        items = ota.collect(BOARD)
        total = sum(os.path.getsize(p) for _, p in items)
        print("  本地 %d 个文件 · %.1f KB · 版本 %s"
              % (len(items), total / 1024.0, ota.version(BOARD)))
    except Exception as e:
        print("  本地文件读取失败：%s" % e)
    hr()


def do_push_all():
    show_header()
    print("推送全部文件并重启。传输期间页面会短暂无响应。\n")
    if input("确认？(y/N) ").strip().lower() != "y":
        return
    items = ota.collect(BOARD)
    ota.push(args_ns(), items)


def do_push_one():
    show_header()
    items = ota.collect(BOARD)
    for i, (rel, path) in enumerate(items, 1):
        print("  %2d) %-24s %8.1f KB" % (i, rel, os.path.getsize(path) / 1024.0))
    print()
    s = input("序号或路径（直接回车取消）：").strip()
    if not s:
        return
    pick = None
    if s.isdigit() and 1 <= int(s) <= len(items):
        pick = [items[int(s) - 1]]
    else:
        want = s.replace("\\", "/").lstrip("/")
        for it in items:
            if it[0] == want or it[0].endswith("/" + want):
                pick = [it]
                break
    if not pick:
        print("没找到这一项")
        return
    ota.push(args_ns(no_reboot=True), pick)
    print("\n已写入，未重启；回到菜单执行 1) 或重启设备后才生效。")


def do_pack():
    show_header()
    m = pack_ota.build(BOARD, OTA_DIR, keep=20)
    print("\n已生成：ota\\%s" % m["file"])
    print("同时更新：ota\\latest.mrp（网页上传时选它即可）")
    if m["stale"]:
        print("\n提醒：web/ 有 %d 个页面比 www/ 新，先跑 build.py 再打包" % m["stale"])


def do_status():
    show_header()
    h, o = device_state()
    if not h or not h.get("ok"):
        print("设备无响应")
        return
    print("  版本      %s%s" % (o.get("ver") or "-",
                             "（待重启）" if o.get("pending") else ""))
    print("  运行时长  %d 分钟" % ((h.get("up") or 0) // 60))
    print("  空闲内存  %.2f MB" % ((h.get("mem_free") or 0) / 1048576.0))
    print("  可用空间  %.1f MB" % ((o.get("free") or 0) / 1048576.0))
    last = o.get("last") or {}
    if last:
        print("  上次写入  %s · %.1f KB · %s"
              % (last.get("path"), (last.get("size") or 0) / 1024.0,
                 time.strftime("%m-%d %H:%M", time.localtime(last.get("at") or 0))))
    hist = o.get("hist") or []
    if hist:
        print("\n  最近升级：")
        for e in reversed(hist[-5:]):
            print("    %s  %-12s %2d 个文件  %s"
                  % (time.strftime("%m-%d %H:%M", time.localtime(e.get("at") or 0)),
                     e.get("ver") or "-", e.get("files") or 0,
                     "网页上传" if e.get("src") == "pack" else "PC 推送"))


def do_push_pack():
    show_header()
    packs = []
    if os.path.isdir(OTA_DIR):
        for n in sorted(os.listdir(OTA_DIR), reverse=True):
            if n.endswith(".mrp"):
                p = os.path.join(OTA_DIR, n)
                packs.append((n, p, os.path.getsize(p)))
    if not packs:
        print("ota/ 里还没有升级包，先做 3) 生成一个")
        return
    print("  可用升级包：")
    for i, (n, p, sz) in enumerate(packs, 1):
        print("  %2d) %-34s %.1f KB" % (i, n, sz / 1024.0))
    print()
    s = input("序号或完整路径（直接回车取消）：").strip()
    if not s:
        return
    if s.isdigit() and 1 <= int(s) <= len(packs):
        path = packs[int(s) - 1][1]
    elif os.path.isfile(s):
        path = s
    else:
        print("没找到这个文件")
        return
    print("\n上传 %s（%.1f KB）…" % (os.path.basename(path), os.path.getsize(path) / 1024.0))
    t0 = time.time()
    try:
        st, d = ota.api_put_file(DEFAULT_HOST, 80, "/api/ota/pack", path,
                                 on_progress=lambda pct: print("  %3d%%" % pct, end="\r"))
    except Exception as e:
        print("\n失败：%s" % e)
        return
    print()
    if st != 200 or not d.get("ok"):
        print("失败：%s" % (d.get("msg") or d))
        return
    print("写入 %d 个文件 · %.1f 秒，版本 %s"
          % (d.get("files") or 0, time.time() - t0, d.get("ver") or "-"))
    if input("\n现在重启生效？(Y/n) ").strip().lower() in ("", "y"):
        try:
            ota.api(DEFAULT_HOST, 80, "POST", "/api/ota?a=reboot")
        except Exception:
            pass
        print("重启中，约 15 秒…")
        print("已恢复" if ota.wait_back(DEFAULT_HOST, 80) else "等待超时，请检查设备")


def do_open_dir():
    os.makedirs(OTA_DIR, exist_ok=True)
    if os.name == "nt":
        os.startfile(OTA_DIR)
    else:
        print(OTA_DIR)


MENU = [
    ("1", "推送全部并重启（约 70 秒）", do_push_all),
    ("2", "只推送指定文件", do_push_one),
    ("3", "生成升级包到 ota/", do_pack),
    ("4", "查看设备状态", do_status),
    ("5", "用升级包推送（整包）", do_push_pack),
    ("6", "打开 ota 文件夹", do_open_dir),
    ("0", "退出", None),
]


def menu_loop():
    while True:
        show_header()
        for k, title, _fn in MENU:
            print("  %s) %s" % (k, title))
        print()
        try:
            s = input("请选择：").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        fn = dict((k, f) for k, _t, f in MENU).get(s)
        if s == "0":
            return 0
        if not fn:
            continue
        try:
            fn()
        except Exception as e:
            print("\n出错：%s" % e)
        try:
            input("\n回车返回菜单…")
        except (EOFError, KeyboardInterrupt):
            return 0


def main(argv=None):
    # 重定向到文件或管道时统一用 UTF-8，控制台保持系统编码（Windows 控制台走 Unicode API）
    try:
        if not sys.stdout.isatty():
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv:
        return ota.main(argv)
    print("工作目录：%s" % REPO)
    return menu_loop()


if __name__ == "__main__":
    sys.exit(main())
