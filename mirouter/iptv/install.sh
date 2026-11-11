#!/bin/sh
# 一次性安装 — 先把 mirouter/ 上传到路由器，再在路由器上以 root 执行本脚本。
#   sh /tmp/mirouter/iptv/install.sh [/tmp/mirouter]
set -e
SRC=${1:-/tmp/mirouter}
BASE=/data/iptv

[ -f "$SRC/iptv/iptv_engine.sh" ] || { echo "源码目录不正确: $SRC"; exit 1; }

mkdir -p "$BASE/bin" "$BASE/etc" "$BASE/www/cgi-bin" \
         "$BASE/www/inject" "$BASE/www/iptv/pages" /etc/iptv

# 二进制（udpxy: 组播转单播；rtp2httpd: 网页播放引擎）
cp -f "$SRC/bin/udpxy" "$SRC/bin/rtp2httpd" "$BASE/bin/"
chmod 755 "$BASE/bin/udpxy" "$BASE/bin/rtp2httpd"

# 启动脚本与播放列表生成器
cp -f "$SRC/iptv/start.sh" "$BASE/start.sh"
cp -f "$SRC/iptv/iptv_engine.sh" "$BASE/iptv"
chmod 755 "$BASE/start.sh" "$BASE/iptv"

# 配置：已存在则保留（含实际网络参数与频道库）
if [ ! -s "$BASE/etc/config.sh" ]; then
  cp -f "$SRC/iptv/config.example.sh" "$BASE/etc/config.sh"
  echo "已生成 $BASE/etc/config.sh，需按实际网络修改后重启 start.sh"
fi

# CGI
cp -f "$SRC"/cgi/* "$BASE/www/cgi-bin/"
chmod 755 "$BASE/www/cgi-bin/"*

# 控制台页面
cp -rf "$SRC/iptv/pages/." "$BASE/www/iptv/pages/"
chmod 644 "$BASE/www/iptv/pages/index.html"

# 首页跳转
printf '<!doctype html>\n<meta charset="utf-8">\n<meta http-equiv="refresh" content="0; url=/iptv/">\n' \
  > "$BASE/www/index.html"

# 原厂页面注入片段（start.sh 会在启动时把片段拼到 vendor 包，common.js 需人工生成）
cp -f "$SRC/inject/patch.js" "$BASE/www/inject/patch.js"

sh "$BASE/start.sh"

# 开机自启：cron 每分钟保活（crontabs 在 /data 持久化）
grep -q '/data/iptv/start.sh' /etc/crontabs/root 2>/dev/null || \
  echo '* * * * * /data/iptv/start.sh >/dev/null 2>&1' >> /etc/crontabs/root
/etc/init.d/cron restart 2>/dev/null || true

echo "=== install done ==="
pidof udpxy
pidof rtp2httpd
netstat -lnt 2>/dev/null | grep -E ':8088|:12123|:5140'
