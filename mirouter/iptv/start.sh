#!/bin/sh
# IPTV 服务启动/保活 — 可重复执行
#
# 本脚本由 cron 每分钟执行一次, 因此这里只做两件事:
#   1) 服务保活与接口自愈, 每条都带"已是目标状态就直接跳过"的判断;
#   2) 内容没变就不写盘, 不留定时任务式的重复下发。
# 一次性配置 (nginx 路由注入 / NTP 服务端) 用标记文件或配置内标记控制,
# 重启后标记随 /tmp 或配置一起清空, 会自动重做。
BASE=/data/iptv
CONF=/etc/nginx/miwifi-webinitrd.conf
NGMARK='# iptv-routes v1'

mkdir -p /etc/iptv "$BASE/www/iptv" /var/run/iptv /tmp/iptv 2>/dev/null

# ---------------------------------------------------------------- 配置与频道库
# 配置放在 /etc/iptv (ramfs)。内容没变就不再覆写。
if ! cmp -s "$BASE/etc/config.sh" /etc/iptv/config.sh 2>/dev/null; then
  cp -f "$BASE/etc/config.sh" /etc/iptv/config.sh 2>/dev/null
fi

# 只有 /etc/iptv 为空 (重启后) 才从种子恢复, 否则会覆盖掉扫描结果
if [ ! -s /etc/iptv/channels.tsv ]; then
  if [ -s "$BASE/etc/channels.tsv.persist" ]; then
    cp -f "$BASE/etc/channels.tsv.persist" /etc/iptv/channels.tsv 2>/dev/null
  else
    cp -f "$BASE/etc/channels.tsv" /etc/iptv/channels.tsv 2>/dev/null
  fi
fi
# 权限一次性校正 (两个文件一次 chmod), 防止被误改后 CGI 读不到
chmod 644 /etc/iptv/config.sh /etc/iptv/channels.tsv 2>/dev/null

# 把扫描结果持久化 (/etc 是 ramfs, 重启会丢)。
# 用 ramfs 里的 md5 与上次比对, 内容没变就不去读 flash 上的副本。
dbm=$(md5sum /etc/iptv/channels.tsv 2>/dev/null | cut -d' ' -f1)
if [ -n "$dbm" ]; then
  if [ "$dbm" != "$(cat /tmp/iptv/db.md5 2>/dev/null)" ] || [ ! -s "$BASE/etc/channels.tsv.persist" ]; then
    # 只持久化“有效结果”（含在线/失效），避免把全 -1 写入
    if grep -qv '	-1	' /etc/iptv/channels.tsv 2>/dev/null; then
      cp -f /etc/iptv/channels.tsv "$BASE/etc/channels.tsv.persist" 2>/dev/null
    fi
    printf '%s\n' "$dbm" > /tmp/iptv/db.md5 2>/dev/null
  fi
fi

# ---------------------------------------------------------------- 交换机 VLAN
# 安全约定: 本段每次都必须独立读取当前状态再判断, 不复用缓存。
# VLAN 43 只允许 "2t 6t" (LAN2 tagged + CPU tagged); 一旦把 LAN3 带进来,
# 就会与光猫/机顶盒形成环路, 导致整网被封。端口串是固定字面量, 不做任何拼接,
# 因此这里只做"与目标不一致才下发"的减负, 不改判断依据。

# LAN1/LAN2/LAN3 都保持 VLAN1 (正常上网)
if ! swconfig dev switch1 show 2>/dev/null | grep -A2 '^VLAN 1' | grep -q 'ports: 1 2 3 6'; then
  swconfig dev switch1 vlan 1 set ports "1 2 3 6"
  swconfig dev switch1 port 1 set pvid 1
  swconfig dev switch1 port 2 set pvid 1
  swconfig dev switch1 port 3 set pvid 1
  swconfig dev switch1 set apply
fi

# IPTV: 光猫 trunk 接 LAN2, 只有 LAN2(tagged 43) + LAN CPU(6t)
# 注意: 不要把 LAN3 加进 VLAN43 —— 实测会与光猫/机顶盒形成环路风暴!
if ! swconfig dev switch1 show 2>/dev/null | grep -A2 '^VLAN 43' | grep -q 'ports: 2t 6t'; then
  swconfig dev switch1 vlan 43 set ports "2t 6t"
  swconfig dev switch1 set apply
fi

# ---------------------------------------------------------------- IPTV 接口
# 清理旧接口: 只在真的存在时才下发, 否则每分钟三次无效 ip 调用
for l in eth1.46 eth0.45 eth1.45; do
  [ -e "/sys/class/net/$l" ] && ip link del "$l" 2>/dev/null
done

if [ ! -e /sys/class/net/eth1.43 ]; then
  ip link add link eth1 name eth1.43 type vlan id 43
fi
# 两条都是幂等操作: 已 up / 地址已存在时返回 EEXIST, 不做额外判断反而更省。
ip link set eth1.43 up 2>/dev/null
ip addr add 192.168.43.3/24 dev eth1.43 2>/dev/null

# ---------------------------------------------------------------- 服务保活
# udpxy: 组播转 HTTP, 监听 12123, 从 IPTV VLAN 收组播
if ! pidof udpxy >/dev/null 2>&1; then
  "$BASE/bin/udpxy" -p 12123 -m 192.168.43.3 -c 10 -B 65536 -T -S >/dev/null 2>&1 &
fi

# rtp2httpd: 网页播放器 / 状态面板 / EPG, 监听 5140
if ! pidof rtp2httpd >/dev/null 2>&1; then
  # -O '*' : CORS 允许任意来源（电视/手机 App 跨域拉流需要）
  # --external-m3u-update-interval: 重读 M3U 与重抓 EPG 的间隔（秒）。
  #   build 时仅在 M3U 内容变化才发 HUP，这里的间隔作为兜底。
  "$BASE/bin/rtp2httpd" -l 0.0.0.0:5140 -r eth1.43 -m 10 -B 524288 -O '*' \
    -M "file://$BASE/www/iptv/iptv-r2h.m3u" -I 0 --external-m3u-update-interval 43200 \
    --pid-file /var/run/rtp2httpd.pid >/dev/null 2>&1 &
fi

# uhttpd: 前端 + CGI, 监听 8088
if ! pidof uhttpd >/dev/null 2>&1; then
  /usr/sbin/uhttpd -h "$BASE/www" -p 8088 -x /cgi-bin -t 60 -T 30 >/dev/null 2>&1 &
fi

# ---------------------------------------------------------------- 一次性系统配置
# NTP 服务端: 让内网设备以 10.0.0.1 作为时间源 (UDP 123)。
# uci 读写与 sysntpd 重启只需做一次, 成功后在 /tmp 留标记, 重启后自动重做。
if [ ! -f /tmp/iptv/ntp.ok ]; then
  if uci get system.ntp >/dev/null 2>&1; then
    if [ "$(uci get system.ntp.enable_server 2>/dev/null)" != "1" ]; then
      uci set system.ntp.enable_server='1'; uci commit system
      /etc/init.d/sysntpd restart 2>/dev/null
    fi
    : > /tmp/iptv/ntp.ok 2>/dev/null
  fi
fi

# conntrack 上限: 默认 16384 对家用过大, 占内核 Slab。
# 用 read 内建直接读 /proc, 不起进程, 因此不必加标记。
read -r ctm < /proc/sys/net/netfilter/nf_conntrack_max 2>/dev/null
case "$ctm" in
  ''|*[!0-9]*) ;;
  *) [ "$ctm" -gt 8192 ] && echo 8192 > /proc/sys/net/netfilter/nf_conntrack_max 2>/dev/null ;;
esac
# conntrack 字节记账 (每设备流量统计需要)
read -r cta < /proc/sys/net/netfilter/nf_conntrack_acct 2>/dev/null
[ "$cta" = "1" ] || echo 1 > /proc/sys/net/netfilter/nf_conntrack_acct 2>/dev/null

# 天气历史数据: 一次性回填过去 7 天 (纯数据, 不含代码), 缓存 12 小时
if [ -f "$BASE/www/cgi-bin/weatherhist" ] && [ ! -s /tmp/weather_hist.json ]; then
  QUERY_STRING="a=history" sh "$BASE/www/cgi-bin/weatherhist" >/dev/null 2>&1
fi

# ---------------------------------------------------------------- nginx 路由
# /manage/ 前端 + /iptv/ 播放列表(地址不变) + 各 CGI + 原厂页面注入。
#
# 注入是幂等的, 但逐条检查要跑十几个 grep 和 8 次 add_route, 每分钟一次纯属浪费。
# 全部检查通过后在配置末尾留一行 NGMARK, 之后只比对这一行就整段跳过。
# 固件重新生成配置、换机、或新增路由时 (NEW: 请同步修改 NGMARK 的版本号),
# 标记不存在或版本不符, 会自动重新注入。
if [ -f "$CONF" ] && ! grep -qF "$NGMARK" "$CONF" 2>/dev/null; then
  CHANGED=0
  cp -f "$CONF" /tmp/miwifi-webinitrd.conf.bak 2>/dev/null

  # 逐条路由检查并补齐（每条独立判断，缺哪个补哪个）
  add_route() {
    ep="$1"
    grep -q "location = /cgi-bin/$ep" "$CONF" 2>/dev/null && return 0
    printf '\n    location = /cgi-bin/%s {\n        proxy_pass http://127.0.0.1:8088/cgi-bin/%s;\n    }\n' "$ep" "$ep" >> "$CONF"
    CHANGED=1
  }

  # /manage/ 前端
  if ! grep -q 'location \^~ /manage/' "$CONF" 2>/dev/null; then
    cat >> "$CONF" <<'NGEOF'

    location ^~ /manage/ {
        alias /data/iptv/www/iptv/pages/;
        index index.html;
    }
    location = /manage {
        return 302 /manage/;
    }
NGEOF
    CHANGED=1
  fi

  # IPTV 播放列表
  if ! grep -q 'location = /iptv/iptv-r2h.m3u' "$CONF" 2>/dev/null; then
    cat >> "$CONF" <<'NGEOF'

    location = /iptv/iptv.m3u      { alias /data/iptv/www/iptv/iptv.m3u; }
    location = /iptv/iptv-r2h.m3u  { alias /data/iptv/www/iptv/iptv-r2h.m3u; }
    location = /iptv/iptv-rtp.m3u  { alias /data/iptv/www/iptv/iptv-rtp.m3u; }
    location = /iptv/iptv.txt      { alias /data/iptv/www/iptv/iptv.txt; }
    location ^~ /iptv/ {
        return 302 /manage/;
    }
    location = /iptv {
        return 302 /manage/;
    }
NGEOF
    CHANGED=1
  fi

  # 播放器专用回看列表 (时间以 epoch 传入, 由 CGI 换算, 与客户端时区无关)
  if ! grep -q 'location = /iptv/iptv-kodi.m3u' "$CONF" 2>/dev/null; then
    cat >> "$CONF" <<'NGEOF'

    location = /iptv/iptv-kodi.m3u    { alias /data/iptv/www/iptv/iptv-kodi.m3u; }
    location = /iptv/iptv-tvbox.m3u   { alias /data/iptv/www/iptv/iptv-tvbox.m3u; }
    location = /iptv/iptv-iptvpro.m3u { alias /data/iptv/www/iptv/iptv-iptvpro.m3u; }
NGEOF
    CHANGED=1
  fi

  # 节目单切片 (按频道拆分的小 JSON, 网页从其它源跨域直取)
  if ! grep -q 'location \^~ /iptv/epg/' "$CONF" 2>/dev/null; then
    cat >> "$CONF" <<'NGEOF'

    location ^~ /iptv/epg/ {
        alias /data/iptv/www/iptv/epg/;
        default_type application/json;
        add_header Access-Control-Allow-Origin "*" always;
    }
NGEOF
    CHANGED=1
  fi

  # 回看 HLS 反代: 复用 rtp2httpd 的 /http/ 转发, nginx 只补 CORS 与 Host。
  # 上游回看 CDN 不返回 Access-Control-Allow-Origin, 网页里的 hls.js 取不到播放
  # 列表与分片; rtp2httpd 只给 /rtp/ 与 OPTIONS 补跨域头, 这里由 nginx 补齐。
  # Host 传 $host: rtp2httpd 按 Host 重写 m3u8 内的分片地址, 分片才会继续回到这里。
  if ! grep -q 'location \^~ /http/' "$CONF" 2>/dev/null; then
    cat >> "$CONF" <<'NGEOF'

    location ^~ /http/ {
        proxy_pass http://127.0.0.1:5140;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout 60s;
        add_header Access-Control-Allow-Origin "*" always;
        add_header Access-Control-Allow-Methods "GET, HEAD, OPTIONS" always;
    }
NGEOF
    CHANGED=1
  fi

  # 所有 CGI 路由逐条补齐
  for ep in iptv ntpctl weather weatherhist svcstat sysstat memctl netperip; do
    [ -f "$BASE/www/cgi-bin/$ep" ] || continue
    add_route "$ep"
  done

  # 原厂页面注入
  if ! grep -q 'location = /js/common.js' "$CONF" 2>/dev/null; then
    cat >> "$CONF" <<'NGEOF'

    location = /js/common.js {
        alias /data/iptv/www/inject/common.js;
        default_type application/javascript;
        expires epoch;
    }
NGEOF
    CHANGED=1
  fi

  # main.html 的 vendor 包注入
  VENDOR=$(ls /www/static/js/vendor.*.js 2>/dev/null | head -1)
  if [ -n "$VENDOR" ]; then
    VN=$(basename "$VENDOR")
    VF="$BASE/www/inject/$VN"
    if [ ! -f "$VF" ] || [ "$(wc -c < "$VF")" -le "$(wc -c < "$VENDOR")" ]; then
      cat "$VENDOR" > "$VF"
      [ -f "$BASE/www/inject/patch.js" ] && cat "$BASE/www/inject/patch.js" >> "$VF"
      chmod 644 "$VF"
    fi
    if ! grep -q "static/js/$VN" "$CONF" 2>/dev/null; then
      printf '\n    location = /static/js/%s {\n        alias %s;\n        default_type application/javascript;\n        expires epoch;\n    }\n' "$VN" "$VF" >> "$CONF"
      CHANGED=1
    fi
  fi

  NGOK=0
  if [ "$CHANGED" = "1" ]; then
    if nginx -t -c /etc/nginx/nginx.conf >/dev/null 2>&1; then
      nginx -s reload 2>/dev/null
      NGOK=1
    else
      cp -f /tmp/miwifi-webinitrd.conf.bak "$CONF" 2>/dev/null
    fi
  else
    NGOK=1
  fi
  # 只有本次全部通过才留标记; 校验失败则下一分钟重试
  [ "$NGOK" = "1" ] && printf '\n%s\n' "$NGMARK" >> "$CONF" 2>/dev/null
fi

# 生成播放列表: 仅在产物缺失时补一次 (首次部署或产物被清空)。
# 日常重建由 cron 每天执行一次 iptv build; 频道增删改由管理端 CGI 直接触发,
# 不跟随每分钟的保活节奏, 避免重复生成与写闪存。
[ -s "$BASE/www/iptv/iptv-r2h.m3u" ] || "$BASE/iptv" build >/dev/null 2>&1

exit 0
