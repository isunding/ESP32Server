# mirouter — 路由器端组件

网关为 Redmi AX3000（RA81），固件 1.0.68（OpenWrt 18.06 底包），地址 `10.0.0.1`。
本目录收录运行在该网关上的自建组件：设备侧（`esp32/`、`esp32-s3/`）不直接访问公网，
数据经此处的 shell CGI 转发；直播与回看由 udpxy、rtp2httpd 提供单播出口；
原厂管理页通过 nginx 注入入口按钮。

## 目录

| 路径 | 内容 | 部署位置 |
|---|---|---|
| `cgi/` | 13 个 shell CGI（数据代理、状态、IPTV 控制台） | `/data/iptv/www/cgi-bin/` |
| `iptv/` | 部署与保活脚本、播放列表生成器、控制台页面 | `/data/iptv/`、`/data/iptv/www/iptv/pages/` |
| `inject/` | 原厂管理页的入口注入片段 | `/data/iptv/www/inject/` |
| `bin/` | udpxy、rtp2httpd 二进制 | `/data/iptv/bin/` |
| `tools/` | 部署脚本 `deploy.py` | 本机运行 |

## 服务与端口

| 端口 | 服务 | 说明 |
|---|---|---|
| 80 / 443 | 原厂 nginx | 管理页；把白名单内的 `/cgi-bin/xxx` 反代到 8088 |
| 8088 | uhttpd | 文档根 `/data/iptv/www`，带 CGI 执行，超时 60 秒 |
| 5140 | rtp2httpd | 网页播放器、`/status`、`/playlist.m3u`、EPG |
| 12123 | udpxy | 组播转单播 |
| 12130–12133 | udpxy 扫描实例 | 频道探测，实例数由 `SCANNERS` 决定 |
| 123/udp | sysntpd | 内网 NTP 时间源 |

设备侧默认经 `router.aux_port`（8088）直连，不经过 nginx。

## CGI 一览

数据接口原样透传上游 JSON 或 CSV，缓存写在 `/tmp`，并发取数由 `mkdir` 原子锁收敛为单飞。

| 文件 | 参数 | 说明 |
|---|---|---|
| `news` | `a=list`、`a=page&last_time=<秒>`、`a=cache`、`a=flush` | 财联社电报；首页缓存 5 分钟，翻页不入缓存 |
| `quake` | `a=list`、`a=global`、`a=cache`、`a=flush` | 国内速报与全球震情，两路缓存各 30 分钟 |
| `usgs` | `a=list`、`a=cache`、`a=flush` | USGS M2.5+ 近 24 小时，CSV 透传，缓存 15 分钟 |
| `oil` | `a=list[&sheng=省份]`、`a=cache`、`a=flush` | 全国油价，缓存 24 小时，需 `Q_ID` / `Q_KEY` |
| `almanac` | `a=list`、`a=cache`、`a=flush` | 当日万年历，缓存 24 小时，需 `Q_ID` / `Q_KEY` |
| `weather` | `a=data`、`a=flush`、`sethost=` 等 | 实时天气与空气质量，缓存 3 小时 |
| `weatherhist` | `a=history` | 近 7 天历史天气，缓存 12 小时 |
| `iptv` | 透传至 `/data/iptv/iptv cgi` | 频道库查询、扫描任务、配置读写 |
| `ntpctl` | `a=status` | 上游校时状态与偏差 |
| `svcstat` | `port=<端口>` | 探测本机服务端口，供页面判断在线状态 |
| `sysstat` | `a=stat`、`a=dhcp`、`a=arp` | CPU、内存、负载、温度、流量、租约、ARP |
| `memctl` | `a=analyze`、`a=clean` | 内存构成分析与释放可回收页 |
| `netperip` | `a=sample`、无参数 | 按设备汇总 conntrack 上下行流量 |

## 运行约束

| 项 | 规则 | 约束依据 |
|---|---|---|
| 换行 | 所有 shell 与 CGI 必须为 LF | CRLF 使 `#!/bin/sh^M` 无法解释，报 not found |
| 哈希 | 签名用 `openssl dgst`，不用 `sha1sum` | busybox 只编入 `md5sum`，`sha1sum` 返回空串，上游回 `errno 10012` |
| 缓存位置 | 一律写 `/tmp` | tmpfs，避免反复擦写 `/data` 的 flash |
| 单飞 | 只用 `mkdir` 原子锁 | busybox 不一定编入 `flock` |
| 请求耗时 | 单次请求路径不超过 1 秒，取数交给 TTL 缓存或主循环 | 上游取数需 1~1.6 秒，并发会拖垮单线程侧 |
| 配置 | `config.sh` 由 `/etc/iptv/config.sh` 承载运行时副本，`/etc` 重启清空 | `/etc/iptv` 缺失时由 `start.sh` 从 `/data/iptv/etc/` 恢复 |

## 部署

`tools/deploy.py` 按目录分批上传，自动设置权限。

```bash
export MIROUTER_PASS='<root 口令>'          # 或用 --password
python mirouter/tools/deploy.py all          # bin + cgi + iptv
python mirouter/tools/deploy.py cgi --q-id <ID> --q-key <KEY>   # 带上接口凭据
python mirouter/tools/deploy.py iptv --restart
```

传输优先使用 plink/pscp（支持口令），否则回退到 ssh/scp（需免密登录）。

手工部署：

```bash
scp mirouter/cgi/* root@10.0.0.1:/data/iptv/www/cgi-bin/
ssh root@10.0.0.1 'chmod 755 /data/iptv/www/cgi-bin/*'

scp mirouter/bin/* root@10.0.0.1:/data/iptv/bin/
ssh root@10.0.0.1 'chmod 755 /data/iptv/bin/udpxy /data/iptv/bin/rtp2httpd; sh /data/iptv/start.sh'
```

自检：

```bash
curl --noproxy "*" "http://10.0.0.1:8088/cgi-bin/news?a=cache"
curl --noproxy "*" "http://10.0.0.1:8088/cgi-bin/sysstat?a=stat"
curl --noproxy "*" "http://10.0.0.1:12123/status"
curl --noproxy "*" "http://10.0.0.1:5140/status"
```

## 凭据

仓库内所有凭据位均为空值或占位符，部署时按实际环境填写。

| 文件 | 内容 | 权限 |
|---|---|---|
| `/data/iptv/etc/quake.conf` | `Q_ID` / `Q_KEY` | 600 |
| `/data/iptv/www/cgi-bin/oil`、`almanac` | 文件头 `Q_ID` / `Q_KEY` | 755 |
| `/data/iptv/etc/weather.conf` | `W_HOST` / `W_KEY` / `W_LOC` | 600 |
| `/data/iptv/etc/weather.key`、`weather.token` | 和风天气 Key 与 JWT | 600 |
| `/data/iptv/etc/ntp.conf` | `NTP_INTERVAL` | 644 |

不含：SSH 与后台口令、主机密钥指纹、宽带账号、终端 MAC、频道库与扫描结果。

## 许可

`bin/udpxy`、`bin/rtp2httpd` 均为 GPL-2.0，许可证见 `bin/LICENSE-GPL-2.0`，
源码与版本信息见 `bin/README.md`。仓库内其余脚本与页面由本项目维护。
