"""可靠的刷机小工具：等串口 → 上传 → Ctrl-D 启动 → 自检。

为什么需要它：mpremote 一打开串口就拉 DTR/RTS 让板子复位，紧接着
enter_raw_repl 常常抢不到（板子还在启动），于是报
`could not enter raw repl`。重试两次基本都能成。

而且 fs cp 结束后板子会停在 REPL（程序不跑），必须补一个 Ctrl-D。

用法：
    python tests/flash.py rap.py main.py www/index.html
    python tests/flash.py --no-upload      # 只重启 + 自检
"""
import os
import sys
import time

import serial
from serial.tools import list_ports


BOOT_WAIT = 13          # 板子从复位到服务就绪约 8~12 秒

# 串口与板子地址，可用环境变量覆盖：
#   set ESP32_PORT=COM3
#   set ESP32_HOST=192.168.1.50
PORT = os.environ.get("ESP32_PORT", "COM3")
BOARD = os.environ.get("ESP32_HOST", "192.168.1.50")


def wait_port(name=None, timeout=60):
    name = name or PORT
    t0 = time.time()
    while time.time() - t0 < timeout:
        if any(p.device == name for p in list_ports.comports()):
            return True
        time.sleep(1)
    return False


def upload(files):
    import subprocess
    # mpremote 的 "+" 是分隔「命令」的，所以每段都要带完整的 fs cp
    cmd = [sys.executable, "-m", "mpremote", "connect", PORT]
    for i, f in enumerate(files):
        if i:
            cmd.append("+")
        if "/" in f:
            head, _, tail = f.partition("/")
            dst = ":%s/%s" % (head, tail)
        else:
            dst = ":" + f
        cmd += ["fs", "cp", f, dst]
    for attempt in range(3):
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if r.returncode == 0:
            print("[flash] upload OK")
            return True
        print("[flash] upload retry %d: %s" % (
            attempt + 1, (r.stderr or r.stdout).strip().splitlines()[-1][:80]))
        time.sleep(BOOT_WAIT)
    return False


def boot_and_check():
    """Ctrl-D 软复位并确认服务起来。"""
    s = serial.Serial()
    s.port = PORT
    s.baudrate = 115200
    s.timeout = 0.2
    s.dtr = False
    s.rts = False
    s.open()
    time.sleep(0.3)
    s.read(32768)
    s.write(b"\x03")
    time.sleep(0.4)
    s.read(16384)
    s.write(b"\x04")                 # Ctrl-D → 重新执行 boot.py + main.py
    t0 = time.time()
    buf = b""
    while time.time() - t0 < 16:
        d = s.read(4096)
        if d:
            buf += d
    s.close()
    txt = buf.decode("utf-8", "replace")
    ok = "[web]" in txt
    print("[flash] boot:", "OK" if ok else "未确认")
    for line in txt.strip().splitlines()[-6:]:
        print("   ", line)


def check_http():
    import json
    import urllib.request
    for i in range(6):
        try:
            r = urllib.request.urlopen("http://%s/api/health" % BOARD, timeout=8)
            d = json.loads(r.read().decode())
            print("[flash] health OK mem=%d up=%ss" % (d["mem_free"], d["up"]))
            return True
        except Exception:
            time.sleep(3)
    print("[flash] health 失败")
    return False


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    do_upload = "--no-upload" not in sys.argv
    if not wait_port():
        print("[flash] 找不到串口", PORT)
        raise SystemExit(1)
    if do_upload and args:
        if not upload(args):
            raise SystemExit(1)
        time.sleep(2)
    boot_and_check()
    raise SystemExit(0 if check_http() else 1)
