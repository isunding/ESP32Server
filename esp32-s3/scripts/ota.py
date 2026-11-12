"""通过 HTTP 升级板子上的应用文件（.py 与 www/），不需要串口。

与 mpremote 的区别：mpremote 一连接就复位板子，传输期间程序停着；这里走
板子自己在跑的 `PUT /api/ota`，传完才重启一次。`config.json`、`ose.json`
之类的板端数据不在升级范围内，不会被覆盖。

用法：
    python scripts/ota.py                      # 推送本板全套并重启
    python scripts/ota.py --dry-run            # 只列清单，不联网
    python scripts/ota.py --board esp32        # 推送旧板目录
    python scripts/ota.py --only www/index.html main.py
    python scripts/ota.py --no-reboot          # 传完不重启
    python scripts/ota.py --pack dist/ota.mrp  # 只打包（给网页上传用）

环境变量 ESP32_HOST 可代替 --host，默认 10.0.0.100（S3 板）。
"""
import argparse
import binascii
import http.client
import json
import os
import struct
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 板级目录
REPO = os.path.dirname(ROOT)

# 与 main.py 的 OTA_MODULES 保持一致：板子只接受这些文件名
MODULES = ("boot.py", "main.py", "httpsrv.py", "netmgr.py",
           "ntpsrv.py", "ntpsync.py", "rap.py", "cncal.py")

# 先传页面、再传模块，boot.py / main.py 最后：中途失败时启动链仍是完整的
TAIL = ("boot.py", "main.py")

CHUNK = 16384
MAGIC = b"MRP2"


def log(msg):
    print(msg, flush=True)


def collect(board_dir, only=None):
    """返回 [(相对路径, 本地绝对路径)]，顺序即上传顺序。"""
    www = os.path.join(board_dir, "www")
    mods = []
    for name in sorted(os.listdir(board_dir)):
        if name.endswith(".py") and os.path.isfile(os.path.join(board_dir, name)):
            if name not in MODULES:
                log("跳过未登记的模块 %s（板子不接受）" % name)
                continue
            mods.append(name)
    mods.sort(key=lambda n: (n in TAIL, n))      # TAIL 里的排到最后

    items = []
    if os.path.isdir(www):
        for name in sorted(os.listdir(www)):
            if os.path.isfile(os.path.join(www, name)):
                items.append(("www/" + name, os.path.join(www, name)))
    for name in mods:
        items.append((name, os.path.join(board_dir, name)))

    if only:
        keep = set(x.strip("/") for x in only)
        missing = keep - set(rel for rel, _ in items)
        if missing:
            raise SystemExit("--only 里有不存在的文件: %s" % ", ".join(sorted(missing)))
        items = [it for it in items if it[0] in keep]
    return items


def crc_of(path):
    c = 0
    n = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(65536)
            if not b:
                break
            c = binascii.crc32(b, c)
            n += len(b)
    return c & 0xFFFFFFFF, n


def version(board_dir):
    """版本串：git 短哈希（工作区脏就加 -dirty），没有 git 就用时间。"""
    try:
        h = subprocess.run(["git", "-C", REPO, "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=5)
        if h.returncode == 0:
            v = h.stdout.strip()
            d = subprocess.run(["git", "-C", board_dir, "status", "--porcelain",
                                "--", "."], capture_output=True, text=True, timeout=5)
            if d.returncode == 0 and d.stdout.strip():
                v += "-dirty"
            return v
    except Exception:
        pass
    return time.strftime("%Y%m%d-%H%M")


def put_one(host, port, rel, path, timeout=60):
    """上传单个文件，返回 (ok, 消息)。失败由调用方重试。"""
    crc, size = crc_of(path)
    # 板子按 & 和 = 切参数后直接取值，不做 URL 解码，所以路径里的 / 原样发
    url = "/api/ota?path=%s&crc=%d" % (rel, crc)
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.putrequest("PUT", url)
        conn.putheader("Content-Length", str(size))
        conn.putheader("Content-Type", "application/octet-stream")
        conn.endheaders()
        sent = 0
        with open(path, "rb") as f:
            while sent < size:
                b = f.read(min(CHUNK, size - sent))
                if not b:
                    raise OSError("本地文件读短了")
                conn.send(b)
                sent += len(b)
        r = conn.getresponse()
        text = r.read().decode("utf-8", "replace")
        if r.status != 200:
            return False, "HTTP %d %s" % (r.status, text[:120])
        d = json.loads(text)
        if not d.get("ok"):
            return False, d.get("msg") or "设备未确认"
        if d.get("size") != size:
            return False, "设备报告 %s 字节，本地 %d" % (d.get("size"), size)
        return True, text
    finally:
        try:
            conn.close()
        except Exception:
            pass


def api(host, port, method, url, body=None, timeout=20):
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(data))
        conn.request(method, url, body=data, headers=headers)
        r = conn.getresponse()
        text = r.read().decode("utf-8", "replace")
        try:
            return r.status, json.loads(text)
        except ValueError:
            return r.status, {"raw": text}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def wait_back(host, port, timeout=60):
    """等板子重启完：/api/health 能回就算起来了。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(2)
        try:
            st, d = api(host, port, "GET", "/api/health", timeout=4)
            if st == 200 and d.get("ok"):
                return True
        except Exception:
            pass
    return False


def push(args, items):
    ver = version(args.board_dir)
    total = sum(crc_of(p)[1] for _, p in items)
    log("目标 %s:%d · %d 个文件 · %.1f KB · 版本 %s"
        % (args.host, args.port, len(items), total / 1024.0, ver))

    t_all = time.time()
    bad = []
    for i, (rel, path) in enumerate(items, 1):
        size = os.path.getsize(path)
        t0 = time.time()
        ok = False
        msg = ""
        for attempt in range(1, args.retry + 1):
            try:
                ok, msg = put_one(args.host, args.port, rel, path, args.timeout)
            except Exception as e:
                ok, msg = False, "%s: %s" % (type(e).__name__, e)
            if ok:
                break
            if attempt < args.retry:
                log("   重试 %d/%d：%s" % (attempt, args.retry, msg))
                time.sleep(1.5)
        mark = "ok  " if ok else "FAIL"
        log("[%2d/%2d] %-22s %7.1f KB  %s %5.2fs"
            % (i, len(items), rel, size / 1024.0, mark, time.time() - t0))
        if not ok:
            bad.append((rel, msg))

    if bad:
        log("\n失败 %d 个，未重启（板子仍在跑旧版本）：" % len(bad))
        for rel, msg in bad:
            log("  %-22s %s" % (rel, msg))
        return 1

    if args.no_reboot:
        log("\n全部写入成功，%.1fs（按 --no-reboot 未重启，新页面重启后生效）"
            % (time.time() - t_all))
        return 0

    st, d = api(args.host, args.port, "POST",
                "/api/ota?a=done&ver=%s&files=%d" % (ver, len(items)))
    if st != 200 or not d.get("ok"):
        log("收尾失败：HTTP %s %s" % (st, d))
        return 1
    log("\n全部写入成功，%.1fs，设备重启中…" % (time.time() - t_all))
    if wait_back(args.host, args.port):
        try:
            _, h = api(args.host, args.port, "GET", "/api/ota")
            log("已恢复：版本 %s · 上次写入 %s"
                % (h.get("ver") or "-", (h.get("last") or {}).get("path") or "-"))
        except Exception:
            log("已恢复")
        return 0
    log("等待超时：板子没有在 60 秒内回到可访问状态，请检查串口")
    return 2


def build_pack(items, out, ver=""):
    """生成升级包：MRP2 + 版本长度 u8 + 版本串 + 重复 [名长 u8][名字][大小 u32][crc u32][内容]。

    不做压缩：MicroPython 没有 zlib（只有 uzlib 的 raw inflate，还要自己
    拼窗口），而这个包在同一局域网内传，1.1MB 也就十几秒。
    """
    d = os.path.dirname(os.path.abspath(out))
    if d and not os.path.isdir(d):
        os.makedirs(d)
    n = 0
    vb = ver.encode("utf-8")
    if len(vb) > 63:
        vb = vb[:63]
    with open(out, "wb") as w:
        w.write(MAGIC)
        w.write(struct.pack("<B", len(vb)))
        w.write(vb)
        for rel, path in items:
            crc, size = crc_of(path)
            name = rel.encode("utf-8")
            if len(name) > 255:
                raise SystemExit("文件名过长: " + rel)
            w.write(struct.pack("<B", len(name)))
            w.write(name)
            w.write(struct.pack("<II", size, crc))
            with open(path, "rb") as f:
                while True:
                    b = f.read(65536)
                    if not b:
                        break
                    w.write(b)
            n += 1
    log("已生成 %s：%d 个文件，%.1f KB，版本 %s（网页上传用）"
        % (out, n, os.path.getsize(out) / 1024.0, ver or "-"))


def main(argv=None):
    ap = argparse.ArgumentParser(description="通过 HTTP 升级板端应用文件")
    ap.add_argument("--host", default=os.environ.get("ESP32_HOST", "10.0.0.100"))
    ap.add_argument("--port", type=int, default=80)
    ap.add_argument("--board", default=os.path.basename(ROOT),
                    help="esp32 或 esp32-s3（默认按脚本所在目录）")
    ap.add_argument("--only", nargs="+", metavar="REL",
                    help="只传指定文件，如 www/index.html main.py")
    ap.add_argument("--pack", metavar="OUT", help="只生成升级包，不联网")
    ap.add_argument("--dry-run", action="store_true", help="只列清单")
    ap.add_argument("--no-reboot", action="store_true")
    ap.add_argument("--retry", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args(argv)

    board_dir = ROOT
    if args.board != os.path.basename(ROOT):
        board_dir = os.path.join(REPO, args.board)
        if not os.path.isdir(board_dir):
            raise SystemExit("没有这个板级目录: " + board_dir)

    args.board_dir = board_dir
    items = collect(board_dir, args.only)
    if not items:
        raise SystemExit("没有可上传的文件")

    if args.pack:
        build_pack(items, args.pack, version(board_dir))
        return 0

    if args.dry_run:
        total = 0
        for rel, path in items:
            size = os.path.getsize(path)
            total += size
            log("%-24s %8.1f KB" % (rel, size / 1024.0))
        log("共 %d 个文件，%.1f KB（目标 %s:%d）"
            % (len(items), total / 1024.0, args.host, args.port))
        return 0

    return push(args, items)


if __name__ == "__main__":
    sys.exit(main())
