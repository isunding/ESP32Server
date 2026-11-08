"""分批上传：mpremote 一次传太多（>8 个）会 90 秒超时，这里每批 4 个。

用法：
    python tests/up_batch.py main.py rap.mpy www/index.html www/index.html.gz ...

每批失败重试 3 次；传完板子会停在 REPL，接着跑
    python tests/flash.py --no-upload
把它拉起来。
"""
import os
import subprocess
import sys
import time

BATCH = 4
PORT = os.environ.get("ESP32_PORT", "COM3")


def main(files):
    ok = True
    for i in range(0, len(files), BATCH):
        part = files[i:i + BATCH]
        cmd = [sys.executable, "-m", "mpremote", "connect", PORT]
        for j, f in enumerate(part):
            if j:
                cmd.append("+")
            cmd += ["fs", "cp", f, ":" + f]
        for attempt in range(3):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=70)
                if r.returncode == 0:
                    print("OK   ", " ".join(part))
                    break
                lines = (r.stderr or r.stdout).strip().splitlines()
                print("retry", part[0], lines[-1][:70] if lines else "?")
            except subprocess.TimeoutExpired:
                print("timeout", part[0])
            time.sleep(8)
        else:
            ok = False
        time.sleep(1.5)
    print("ALL OK" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
