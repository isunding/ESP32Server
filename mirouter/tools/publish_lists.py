#!/usr/bin/env python3
"""把路由器上的播放列表整理成对外发布的三套格式（六份文件），并可上传到站点。

用法：
    python mirouter/tools/publish_lists.py                        # 从 10.0.0.1 拉取，生成到 dist/publish/
    python mirouter/tools/publish_lists.py --out D:/tmp/lists      # 指定输出目录
    python mirouter/tools/publish_lists.py --diff D:/tmp/old       # 与旧目录逐行比对，只列差异
    python mirouter/tools/publish_lists.py --upload --host H --user U --port 255 --key K
                                                                  # 生成后上传，覆盖前留 .bak.<日期>

六份文件与线上格式一一对应：
    iptv.m3u   rtp2httpd :5140，带回看            ← iptv-r2h.m3u 原样
    IPTV.txt   名称 + :5140                       ← iptv.txt 换端口
    new.m3u    udpxy :12123，有 tvg-id、无回看      ← iptv-r2h.m3u 去掉 catchup*
    new.txt    名称 + :12123                      ← iptv.txt 原样
    itv.m3u    原始组播 rtp://，无 tvg-id、无回看   ← iptv-r2h.m3u 去掉 tvg-id / catchup*
    itv.txt    名称 + rtp://                      ← iptv.txt 换协议

约定：
    内网地址统一用路由器的 10.0.0.1；凭据（主机、用户、私钥）一律由命令行传入，不写死、不落盘。
"""

import argparse
import hashlib
import pathlib
import re
import subprocess
import sys
import urllib.request

ROUTER = "10.0.0.1"
SITE_ROOT = "/www/wwwroot/sc.isunding.com"
SITE_USER = "www"
EXT_TVG = "https://epg.webnet.cloudns.pro/epg/epg.xml.gz"
CATVOD_TVG = "https://epg.catvod.com/epg.xml"
PORT_R2H = 5140
PORT_UDPXY = 12123

# 属性名 -> 是否保留
DROP_NEW = ("catchup",)          # udpxy 那套没有回看
DROP_ITV = ("catchup", "tvg-id", "display-name")

ATTR = re.compile(r'\s([A-Za-z0-9_-]+)="([^"]*)"')


def fetch(url, timeout=20):
    """直连局域网，不走 http_proxy。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as r:
        return r.read().decode("utf-8")


def split_extinf(line):
    head, name = line.rsplit(",", 1)
    return ATTR.findall(head), name


def keep_prefix(attrs, drops):
    return [k for k, _ in attrs if not any(k == d or k.startswith(d + "-") for d in drops)]


def render(attrs, keep, name):
    d = dict(attrs)
    return "#EXTINF:-1" + "".join(' %s="%s"' % (k, d[k]) for k in keep) + "," + name


def build(m3u_text, txt_text, host=ROUTER):
    """返回 {文件名: 内容}。"""
    src = [l for l in m3u_text.splitlines() if l.strip()]
    r2h_prefix = "http://%s:%d/rtp/" % (host, PORT_R2H)
    udpxy_prefix = "http://%s:%d/rtp/" % (host, PORT_UDPXY)

    iptv, new, itv = ['#EXTM3U x-tvg-url="%s"' % EXT_TVG], ['#EXTM3U x-tvg-url=""'], \
                     ['#EXTM3U x-tvg-url="%s"' % CATVOD_TVG]
    for i in range(len(src) - 1):
        line = src[i]
        if not line.startswith("#EXTINF"):
            continue
        url = src[i + 1]
        if "/rtp/" not in url:
            continue
        ip_port = url.rsplit("/rtp/", 1)[1]
        attrs, name = split_extinf(line)

        iptv += [line, url]
        new += [render(attrs, keep_prefix(attrs, DROP_NEW), name), udpxy_prefix + ip_port]
        disp = dict(attrs).get("display-name", name)
        itv += [render(attrs, keep_prefix(attrs, DROP_ITV), disp), "rtp://" + ip_port]

    def txt_with(prefix, port=None):
        out = []
        for l in txt_text.splitlines():
            if not l.strip():
                continue
            if l.endswith(",#genre#"):
                out.append(l)
                continue
            n, u = l.rsplit(",", 1)
            out.append(n + "," + (prefix + u.rsplit("/rtp/", 1)[-1] if prefix else u))
        return out

    def txt_with_port(port):
        return txt_with("http://%s:%d/rtp/" % (host, port))

    return {
        "iptv.m3u": "\n".join(iptv) + "\n",
        "IPTV.txt": "\n".join(txt_with_port(PORT_R2H)) + "\n",
        "new.m3u": "\n".join(new) + "\n",
        "new.txt": "\n".join(txt_with_port(PORT_UDPXY)) + "\n",
        "itv.m3u": "\n".join(itv) + "\n",
        "itv.txt": "\n".join(txt_with("rtp://")) + "\n",
    }


def md5(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def do_diff(files, old_dir):
    old_dir = pathlib.Path(old_dir)
    dirty = 0
    for name, text in files.items():
        p = old_dir / name
        if not p.is_file():
            print("  %-10s 旧文件不存在" % name)
            continue
        old = p.read_text(encoding="utf-8")
        if old == text:
            print("  %-10s 无变化" % name)
            continue
        a, b = old.splitlines(), text.splitlines()
        diffs = [(x, y) for x, y in zip(a, b) if x != y]
        dirty += len(diffs)
        print("  %-10s 行数 %d→%d，差异 %d 行" % (name, len(a), len(b), len(diffs)))
        for x, y in diffs[:5]:
            print("      - %s\n      + %s" % (x[:100], y[:100]))
        if len(diffs) > 5:
            print("      …共 %d 行" % len(diffs))
    return dirty


def ssh_base(key, user, host, port):
    return ["ssh", "-i", key, "-p", str(port),
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "BatchMode=yes", "%s@%s" % (user, host)]


def scp_base(key, port):
    return ["scp", "-i", key, "-P", str(port), "-o", "StrictHostKeyChecking=accept-new",
            "-o", "BatchMode=yes"]


def run(argv, stdin=None):
    done = subprocess.run(argv, capture_output=True, text=True, input=stdin)
    if done.returncode != 0:
        sys.exit("命令失败：%s\n%s%s" % (" ".join(argv), done.stdout, done.stderr))
    return done.stdout


def do_upload(files, out_dir, key, user, host, port):
    import datetime
    stamp = datetime.date.today().strftime("%Y%m%d")
    ssh = ssh_base(key, user, host, port)
    scp = scp_base(key, port)
    stage = "/tmp/publish-%d" % int(datetime.datetime.now().timestamp())
    run(ssh + ["mkdir -p %s" % stage])
    for name in files:
        run(scp + [str(out_dir / name), "%s@%s:%s/%s" % (user, host, stage, name)])

    cmds = []
    for name in files:
        dst = "%s/%s" % (SITE_ROOT, name)
        cmds.append("if [ -f {d} ]; then cp -p {d} {d}.bak.{s}; fi".format(d=dst, s=stamp))
        cmds.append("chown %s:%s %s/%s" % (SITE_USER, SITE_USER, stage, name))
        cmds.append("chmod 755 %s/%s" % (stage, name))
        cmds.append("mv -f %s/%s %s" % (stage, name, dst))
    cmds.append("rmdir %s" % stage)
    run(ssh + ["sh -c '%s'" % "; ".join(cmds)])

    remote = run(ssh + ["cd %s && md5sum %s" % (SITE_ROOT, " ".join(files))])
    bad = 0
    for line in remote.splitlines():
        h, name = line.split()[0], line.split()[-1]
        ok = h == md5(files[name])
        bad += 0 if ok else 1
        print("  %-10s %s %s" % (name, h, "一致" if ok else "不一致"))
    if bad:
        sys.exit("有 %d 个文件校验不一致" % bad)


def main():
    ap = argparse.ArgumentParser(description="生成并发布 IPTV 播放列表")
    ap.add_argument("--router", default=ROUTER, help="路由器地址，默认 10.0.0.1")
    ap.add_argument("--out", default="dist/publish", help="输出目录")
    ap.add_argument("--diff", metavar="OLD_DIR", help="与旧目录逐行比对")
    ap.add_argument("--upload", action="store_true", help="上传到站点")
    ap.add_argument("--host", help="站点主机")
    ap.add_argument("--user", default="root", help="站点用户")
    ap.add_argument("--port", type=int, default=22, help="站点 SSH 端口")
    ap.add_argument("--key", help="站点私钥路径")
    args = ap.parse_args()

    m3u = fetch("http://%s/iptv/iptv-r2h.m3u" % args.router)
    txt = fetch("http://%s/iptv/iptv.txt" % args.router)
    files = build(m3u, txt, args.router)

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8", newline="\n")
        print("  %-10s %6d 字节  %s" % (name, len(text.encode("utf-8")), md5(text)))

    if args.diff:
        print("与 %s 比对：" % args.diff)
        do_diff(files, args.diff)

    if args.upload:
        if not (args.host and args.key):
            sys.exit("上传需要 --host 与 --key")
        print("上传到 %s:%d …" % (args.host, args.port))
        do_upload(files, out, args.key, args.user, args.host, args.port)


if __name__ == "__main__":
    main()
