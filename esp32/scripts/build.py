"""把 web/ 下的页面模板构建成 www/ 下的可部署页面（内联 CSS / JS，生成 gzip）。

内联的作用是减少连接数：ESP32 的 TCP PCB 池约 10 个，而浏览器打开页面时会
预连多条 TCP。内联后每页只需 1 条 HTML 请求 + 1 条接口请求，为预连接留出余量。

用法：
    python esp32/scripts/build.py          # 全部页面
    python esp32/scripts/build.py weather  # 只构建 weather.html

页面源在 web/*.html、web/theme.css、web/ui.js，构建产物写入 www/。
www/ 下的大库（mpegts.min.js / hls.min.js）不参与构建。
"""
import gzip
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "web")
OUT = os.path.join(ROOT, "www")

# 主题引导：在 <head> 里同步读一次 localStorage，先给 <html> 打上 data-theme。
# 没有它时 ui.js 要到 </body> 前才执行，慢设备上会先画一遍深色再跳成浅色。
THEME_BOOT = ('<script>try{var t=localStorage.getItem("mr.theme");'
              'if(t)document.documentElement.setAttribute("data-theme",t)}catch(e){}</script>')


def read(name):
    with open(os.path.join(SRC, name), encoding="utf-8") as f:
        return f.read()


def write(name, text):
    # newline="" 保留原始换行，避免 Windows 下被改成 \r\n 白占板上空间
    with open(os.path.join(OUT, name), "w", encoding="utf-8", newline="") as f:
        f.write(text)


def write_gz(name, text):
    """同时写一份 .gz 预压缩版本。

    压缩后页面体积降至约四分之一，发送时的 TCP 段数与 pbuf 占用相应减少。
    mtime 固定为 0，保证相同输入产出相同字节，便于校验构建是否同步。
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
        if "<head>" not in html:
            raise SystemExit("[build] %s 缺少 <head>，主题引导脚本无处插入" % name)
        html = html.replace("<head>", "<head>\n" + THEME_BOOT, 1)
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
