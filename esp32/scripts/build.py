"""把 web/ 下的页面模板构建成 www/ 下的可部署页面（内联 css / js）。

为什么内联：ESP32 的 lwIP TCP PCB 池只有 10 个左右，而手机浏览器打开一个
页面会先预连 6~8 条 TCP（拿到 HTML 之前不发数据，但每条都占一个 PCB）。
原来每页要 4~5 条真实连接（html / css / js / 2 个接口），加上预连接正好
把池子占满，真实请求抢不到 PCB 就被 RST——外部表现就是「手机上打开某页
卡死、整站一分钟无响应」。

内联后每页只剩 1 条 HTML + 1 条接口 = 2 条连接，加 6 条预连接也才 8 条，
池子还有余量。代价是每页多传 16KB 左右，但单线程服务器上「少一条连接」
比「少 16KB」值钱得多。

用法：
    python tests/build.py          # 全部页面
    python tests/build.py traffic  # 只构建 traffic.html

改页面请改 web/*.html、web/theme.css、web/ui.js，然后跑本脚本再上传 www/
里的产物。www/ 下的大库（mpegts.min.js / hls.min.js）不参与构建。
"""
import gzip
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "web")
OUT = os.path.join(ROOT, "www")


def read(name):
    with open(os.path.join(SRC, name), encoding="utf-8") as f:
        return f.read()


def write(name, text):
    # newline="" 保留原始换行，避免 Windows 下被改成 \r\n 白占板上空间
    with open(os.path.join(OUT, name), "w", encoding="utf-8", newline="") as f:
        f.write(text)


def write_gz(name, text):
    """同时写一份 .gz（预压缩）。

    板上内存只有 320KB DRAM，WiFi 驱动 + lwIP 会吃掉大半，IDF 堆一旦碎片化
    lwIP 就分不出 pbuf，TCP 收发全废（实测 idf_heap_info 只剩 1.5KB，
    largest_free_block=1024）。页面从 44KB 压到 8KB，发送时的 TCP 段数、
    pbuf 占用都降一个量级，是最省内存的一条路。
    """
    data = text.encode("utf-8")
    # mtime=0 让同样的输入产出同样的字节，便于比对
    blob = gzip.compress(data, compresslevel=9, mtime=0)
    with open(os.path.join(OUT, name + ".gz"), "wb") as f:
        f.write(blob)
    return len(blob)


def main():
    want = [a for a in sys.argv[1:] if not a.startswith("-")]
    all_pages = sorted(f for f in os.listdir(SRC) if f.endswith(".html"))
    if want:
        names = []
        for w in want:
            n = w if w.endswith(".html") else w + ".html"
            if n not in all_pages:
                raise SystemExit("[build] 没有这个模板: " + n)
            names.append(n)
    else:
        names = all_pages

    css = read("theme.css")
    js = read("ui.js")
    if "</style>" in css or "</script>" in js:
        raise SystemExit("[build] css/js 里出现了闭合标签，不能内联")
    print("[build] 内联 theme.css(%d B) + ui.js(%d B)" % (len(css), len(js)))

    for name in names:
        html = read(name)
        before = len(html)
        html2 = re.sub(r'<link rel="stylesheet" href="/theme\.css[^"]*">',
                       "<style>\n" + css + "\n</style>", html, count=1)
        html2 = re.sub(r'<script src="/ui\.js[^"]*"></script>',
                       "<script>\n" + js + "\n</script>", html2, count=1)
        write(name, html2)
        gz_len = write_gz(name, html2)
        print("   %-14s %6d -> %6d B  (gz %5d B)" % (name, before, len(html2), gz_len))
    print("[build] 完成，%d 个页面" % len(names))


if __name__ == "__main__":
    main()
