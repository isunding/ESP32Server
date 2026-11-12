"""把当前板级文件打成升级包，落在仓库根目录的 ota/ 下。

约定：每次改动提交后都生成一个包（.githooks/post-commit 调用本脚本），
文件名带版本与时间，latest.mrp 始终是最近一份。包不进 git，见 .gitignore。

用法：
    python esp32-s3/scripts/pack_ota.py              # 打一个包
    python esp32-s3/scripts/pack_ota.py --keep 50    # 保留最近 50 个
    python esp32-s3/scripts/pack_ota.py --quiet      # 给 git hook 用
    python esp32-s3/scripts/pack_ota.py --out D:/x   # 换输出目录
"""
import argparse
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ota


def now_stamp():
    return time.strftime("%Y%m%d-%H%M")


def safe(ver):
    """版本串进文件名：只留字母数字与 . _ -，-dirty 缩成 -d。"""
    v = (ver or "unknown").replace("-dirty", "-d")
    v = re.sub(r"[^0-9A-Za-z._-]", "_", v)
    return v[:40]


def build_lag(board_dir):
    """构建是否落后：web/ 里比 www/ 新的文件数。"""
    web = os.path.join(board_dir, "web")
    www = os.path.join(board_dir, "www")
    n = 0
    try:
        for name in os.listdir(web):
            src = os.path.join(web, name)
            if not os.path.isfile(src):
                continue
            dst = os.path.join(www, name) if name.endswith(".html") else None
            if dst and (not os.path.exists(dst)
                        or os.path.getmtime(src) > os.path.getmtime(dst) + 1):
                n += 1
    except OSError:
        pass
    return n


def build(board_dir, out_dir=None, keep=20, quiet=False, latest=True):
    """打一个包，返回 {file, ver, files, bytes, stale, latest}。"""
    repo = os.path.dirname(os.path.abspath(board_dir))
    out_dir = out_dir or os.path.join(repo, "ota")
    os.makedirs(out_dir, exist_ok=True)

    items = ota.collect(board_dir)
    if not items:
        raise SystemExit("没有可打包的文件，检查板级目录：" + board_dir)
    ver = ota.version(board_dir)
    name = "ota-%s-%s.mrp" % (safe(ver), now_stamp())
    path = os.path.join(out_dir, name)

    if not quiet:
        total = sum(os.path.getsize(p) for _, p in items)
        print("打包 %d 个文件 · %.1f KB · 版本 %s" % (len(items), total / 1024.0, ver))
    if quiet:
        _quiet_pack(items, path, ver)
    else:
        ota.build_pack(items, path, ver)

    size = os.path.getsize(path)
    newest = path
    if latest:
        newest = os.path.join(out_dir, "latest.mrp")
        shutil.copyfile(path, newest)

    stale = build_lag(board_dir)
    meta = {"ver": ver, "at": int(time.time()), "files": len(items),
            "bytes": size, "file": name, "latest": os.path.basename(newest),
            "stale": stale}
    with open(os.path.join(out_dir, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    prune(out_dir, keep)
    return meta


def _quiet_pack(items, path, ver):
    """build_pack 会打印进度，hook 里要安静，这里把 stdout 收起来。"""
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ota.build_pack(items, path, ver)


def prune(out_dir, keep):
    """只保留最近 keep 个 ota-*.mrp；latest.mrp 不算在内。"""
    files = []
    try:
        for n in os.listdir(out_dir):
            if n.startswith("ota-") and n.endswith(".mrp"):
                p = os.path.join(out_dir, n)
                files.append((os.path.getmtime(p), p))
    except OSError:
        return
    files.sort(reverse=True)
    for _t, p in files[keep:]:
        try:
            os.remove(p)
        except OSError:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成升级包到 ota/")
    ap.add_argument("--board", default="esp32-s3")
    ap.add_argument("--out", help="输出目录，默认 <仓库>/ota")
    ap.add_argument("--keep", type=int, default=20, help="保留最近几个包")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--brief", action="store_true", help="只输出文件名，给 git hook 用")
    ap.add_argument("--no-latest", action="store_true", help="不更新 latest.mrp")
    args = ap.parse_args(argv)

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 板级目录
    board = os.path.join(os.path.dirname(here), args.board)
    if not os.path.isdir(board):
        board = here
    m = build(board, args.out, args.keep, args.quiet or args.brief,
              not args.no_latest)
    if args.brief:
        print(m["file"])
        return 0
    if not args.quiet:
        print("→ %s  (%.1f KB)" % (m["file"], m["bytes"] / 1024.0))
        if m["stale"]:
            print("提醒：web/ 有 %d 个页面比 www/ 新，先跑 scripts/build.py 再打包"
                  % m["stale"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
