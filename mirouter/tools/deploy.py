#!/usr/bin/env python3
"""把 mirouter/ 下的组件部署到网关（Redmi AX3000 / RA81）。

用法：
    python mirouter/tools/deploy.py all
    python mirouter/tools/deploy.py cgi --q-id 1234567 --q-key abcdef
    python mirouter/tools/deploy.py iptv --restart

传输优先级：plink/pscp（可带口令）→ ssh/scp（依赖免密登录）。
口令取自 --password 或环境变量 MIROUTER_PASS，命令行不落盘。
"""

import argparse
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent          # mirouter/
BASE = "/data/iptv"
CGI_DIR = BASE + "/www/cgi-bin"


class Transport:
    def __init__(self, host, user, password, port=22):
        self.host = host
        self.user = user
        self.password = password
        self.port = str(port)
        self.plink = shutil.which("plink")
        self.pscp = shutil.which("pscp")
        self.ssh = shutil.which("ssh")
        self.scp = shutil.which("scp")

    @property
    def target(self):
        return "%s@%s" % (self.user, self.host)

    def _auth(self):
        if self.plink and self.password:
            return ["-pw", self.password]
        return []

    def run(self, command):
        if self.plink:
            argv = [self.plink, "-batch", "-P", self.port] + self._auth() + [self.target, command]
        elif self.ssh:
            argv = [self.ssh, "-p", self.port, "-o", "BatchMode=yes", self.target, command]
        else:
            sys.exit("未找到 plink 或 ssh")
        done = subprocess.run(argv, capture_output=True, text=True)
        if done.returncode != 0:
            sys.exit("远程命令失败: %s\n%s%s" % (command, done.stdout, done.stderr))
        return done.stdout

    def put(self, local, remote):
        if self.pscp:
            argv = [self.pscp, "-scp", "-batch", "-q", "-P", self.port] + self._auth() + [str(local), "%s:%s" % (self.target, remote)]
        elif self.scp:
            argv = [self.scp, "-q", "-P", self.port, "-o", "BatchMode=yes", str(local), "%s:%s" % (self.target, remote)]
        else:
            sys.exit("未找到 pscp 或 scp")
        done = subprocess.run(argv, capture_output=True, text=True)
        if done.returncode != 0:
            sys.exit("上传失败: %s\n%s%s" % (local, done.stdout, done.stderr))


def inject_credentials(path, q_id, q_key, stamp):
    """把接口凭据写进临时副本，仓库内的文件保持为空。"""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r'(?m)^Q_ID=.*$', 'Q_ID="%s"' % q_id, text)
    text = re.sub(r'(?m)^Q_KEY=.*$', 'Q_KEY="%s"' % q_key, text)
    tmp = pathlib.Path(tempfile.gettempdir()) / (path.name + stamp)
    tmp.write_text(text, encoding="utf-8", newline="\n")
    return tmp


def deploy_bin(t):
    t.run("mkdir -p %s/bin" % BASE)
    for name in ("udpxy", "rtp2httpd"):
        t.put(ROOT / "bin" / name, "/tmp/" + name)
        t.run("mv -f /tmp/%s %s/bin/%s && chmod 755 %s/bin/%s" % (name, BASE, name, BASE, name))
    print("bin 已部署：udpxy、rtp2httpd")


def deploy_cgi(t, q_id, q_key):
    t.run("mkdir -p %s" % CGI_DIR)
    for src in sorted((ROOT / "cgi").iterdir()):
        if not src.is_file():
            continue
        local = src
        if src.name in ("oil", "almanac") and q_id and q_key:
            local = inject_credentials(src, q_id, q_key, ".deploy")
        t.put(local, "/tmp/" + src.name)
        t.run("mv -f /tmp/%s %s/%s && chmod 755 %s/%s" % (src.name, CGI_DIR, src.name, CGI_DIR, src.name))
        if local != src:
            local.unlink()
    print("cgi 已部署：%d 个脚本" % len([p for p in (ROOT / "cgi").iterdir() if p.is_file()]))


def deploy_iptv(t, restart):
    t.run("mkdir -p %s/etc %s/www/inject %s/www/iptv" % (BASE, BASE, BASE))
    t.put(ROOT / "iptv" / "start.sh", "/tmp/start.sh")
    t.put(ROOT / "iptv" / "iptv_engine.sh", "/tmp/iptv_engine.sh")
    t.put(ROOT / "iptv" / "config.example.sh", "/tmp/config.example.sh")
    t.run(
        "mv -f /tmp/start.sh %s/start.sh && chmod 755 %s/start.sh && "
        "mv -f /tmp/iptv_engine.sh %s/iptv && chmod 755 %s/iptv" % (BASE, BASE, BASE, BASE)
    )
    t.run(
        "if [ -s %s/etc/config.sh ]; then rm -f /tmp/config.example.sh; "
        "else mv -f /tmp/config.example.sh %s/etc/config.sh && chmod 644 %s/etc/config.sh; fi"
        % (BASE, BASE, BASE)
    )

    t.put(ROOT / "inject" / "patch.js", "/tmp/patch.js")
    t.run("mv -f /tmp/patch.js %s/www/inject/patch.js && chmod 644 %s/www/inject/patch.js" % (BASE, BASE))

    pages = ROOT / "iptv" / "pages"
    count = 0
    if (pages / "index.html").is_file():
        t.run("mkdir -p %s/www/iptv/pages" % BASE)
        t.put(pages / "index.html", "/tmp/page.html")
        t.run("mv -f /tmp/page.html %s/www/iptv/pages/index.html && chmod 644 %s/www/iptv/pages/index.html" % (BASE, BASE))
        count += 1
    for sub in sorted(pages.iterdir()):
        if not sub.is_dir() or not (sub / "index.html").is_file():
            continue
        t.run("mkdir -p %s/www/iptv/pages/%s" % (BASE, sub.name))
        t.put(sub / "index.html", "/tmp/page.html")
        t.run("mv -f /tmp/page.html %s/www/iptv/pages/%s/index.html && chmod 644 %s/www/iptv/pages/%s/index.html"
              % (BASE, sub.name, BASE, sub.name))
        count += 1
    print("iptv 已部署：start.sh、iptv、config、patch.js、%d 个页面" % count)

    if restart:
        print(t.run("sh %s/start.sh; pidof udpxy; pidof rtp2httpd" % BASE))


def main():
    ap = argparse.ArgumentParser(description="部署 mirouter 组件到网关")
    ap.add_argument("what", choices=["cgi", "iptv", "bin", "all"])
    ap.add_argument("--host", default="10.0.0.1")
    ap.add_argument("--user", default="root")
    ap.add_argument("--port", default=22, type=int)
    ap.add_argument("--password", default=os.environ.get("MIROUTER_PASS", ""))
    ap.add_argument("--q-id", default=os.environ.get("MIROUTER_Q_ID", ""))
    ap.add_argument("--q-key", default=os.environ.get("MIROUTER_Q_KEY", ""))
    ap.add_argument("--restart", action="store_true", help="部署后执行 start.sh")
    args = ap.parse_args()

    t = Transport(args.host, args.user, args.password, args.port)
    if args.what in ("bin", "all"):
        deploy_bin(t)
    if args.what in ("cgi", "all"):
        deploy_cgi(t, args.q_id, args.q_key)
    if args.what in ("iptv", "all"):
        deploy_iptv(t, args.restart)
    print("完成。设备侧通过 router.aux_port（默认 8088）访问这些 CGI。")


if __name__ == "__main__":
    main()
