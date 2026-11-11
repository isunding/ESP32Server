# 烧写与部署

[English](DEPLOY.en.md) · 简体中文

适用于 Windows 环境，工具为 esptool 与 mpremote。两个板级目录的部署流程一致，
差异为固件变体、烧写偏移与 USB 串口形态。

---

## 一、前置条件

| 项 | ESP32 版（`esp32/`） | S3 版（`esp32-s3/`） |
|---|---|---|
| 开发板 | ESP32-D0WD-V3，4MB flash | ESP32-S3 N16R8，16MB flash + 8MB Octal PSRAM |
| 固件 | `ESP32_GENERIC-20250911-v1.26.1.bin` | `ESP32_GENERIC_S3-SPIRAM_OCT-20260824-v1.29.0.bin` |
| 烧写偏移 | `0x1000` | `0x0` |
| 串口 | CH9102 / CP2102 | 原生 USB-Serial/JTAG |
| 下载模式 | 按住 BOOT → 点 EN → 松 BOOT | 按住 BOOT → 点 RST → 松 BOOT |

工具：`pip install esptool mpremote`。设备与路由器需在同一局域网。

固件取自 [micropython.org/download](https://micropython.org/download/)。
ESP32 使用 `ESP32_GENERIC` 变体；S3 使用 `ESP32_GENERIC_S3-SPIRAM_OCT` 变体（板载 8MB Octal PSRAM 驱动）。

---

## 二、烧写

```bash
# 进下载模式：按住 BOOT → 点一下 EN（S3 是 RST）→ 松开 BOOT

# ---------- ESP32 ----------
# 1. 备份整片
esptool.py --port COM3 --before no-reset read-flash 0 0x400000 backup-4MB.bin

# 2. 写入固件
esptool.py --port COM3 --before no-reset --after hard-reset \
  --baud 230400 write-flash --erase-all --compress --flash-size 4MB \
  0x1000 ESP32_GENERIC-20250911-v1.26.1.bin

# ---------- ESP32-S3 ----------
esptool.py --chip esp32s3 --port COM3 erase-flash
esptool.py --chip esp32s3 --port COM3 --baud 460800 --after hard-reset \
  write-flash -z 0x0 ESP32_GENERIC_S3-SPIRAM_OCT-20260824-v1.29.0.bin
```

推荐波特率：ESP32 为 230400，S3 为 460800。

S3 烧写偏移为 `0x0`。原生 USB 串口下 `--after hard-reset` 不生效，
烧写完成后需手动按 RST，或执行 `esptool.py run`。

---

## 三、烧写注意事项

### 1. 射频校准数据（ESP32 版）

`0xF000` 处的 4KB 为出厂射频校准数据。`--erase-all` 会一并擦除，
擦除后 `scan()` 返回空列表，连接 WiFi 报 `201 NO_AP_FOUND`。
恢复方法：从烧写前的整片备份中写回该 4KB。

```bash
mpremote connect COM3 cp phy_init.bin :
mpremote connect COM3 exec "import esp; esp.flash_write(0xf000, open('phy_init.bin','rb').read())"
```

校验：`esp.flash_read(0xf000, 4)` 返回 `b'PHYI'`。

S3 版射频参数存放于独立分区，整片擦除后首次启动自动完成校准。

AP 模式开启时 `scan()` 同样返回空列表。诊断 WiFi 前需关闭 AP：
`network.WLAN(network.AP_IF).active(False)`。

### 2. mpremote 连接与复位

`mpremote` 连接时会发送 Ctrl-C 中断运行中的程序，传输完成后设备停在 REPL。
传输结束后须复位：

```bash
mpremote connect COM3 exec "import machine; machine.reset()"
# 或
python esp32/scripts/flash.py --no-upload
```

---

## 四、部署代码

以下命令以 ESP32 版为例。S3 版将路径前缀替换为 `esp32-s3/`，并使用对应串口；
原生 USB 的运行模式与下载模式对应不同的 PID 与端口号。

### 方式 A：逐个上传

```bash
mpremote connect COM3 fs mkdir :www

mpremote connect COM3 cp esp32/boot.py esp32/main.py esp32/netmgr.py \
  esp32/httpsrv.py esp32/rap.py esp32/cncal.py esp32/ntpsync.py esp32/ntpsrv.py :

mpremote connect COM3 cp esp32/config.json :

python esp32/scripts/build.py
mpremote connect COM3 cp esp32/www/index.html esp32/www/weather.html :www
```

`mpremote` 单次传输文件数超过约 8 个会超时，改用分批脚本：

```bash
python esp32/scripts/up_batch.py esp32/main.py esp32/www/index.html esp32/www/index.html.gz ...
```

`up_batch.py` 每批上传 4 个文件，失败重试 3 次。

### 方式 B：预编译为 .mpy

```bash
pip install mpy-cross-v6.3
python esp32/scripts/mpy.py            # 编译、上传、删除板上 .py、重启
python esp32/scripts/mpy.py --revert   # 回退为 .py
```

MicroPython 优先加载 `.py`；部署 `.mpy` 时需删除板上同名 `.py`。
`.mpy` 与解释器版本绑定（当前 v6.3），升级固件后需重新编译。

---

## 五、首次配置

### 方式 1：修改文件

```bash
# 编辑 esp32/config.json 中的 wifi.ssid / wifi.password / router.host
mpremote connect COM3 cp esp32/config.json :
mpremote connect COM3 exec "import machine; machine.reset()"
```

### 方式 2：配网热点

`config.json` 中 `wifi.ssid` 留空，或 WiFi 连接失败时，设备自动开启热点：

| 项 | 值 |
|---|---|
| SSID | `ESP32-Setup` |
| 密码 | `esp32setup123` |
| 配置页 | `http://192.168.4.1` |

连接热点后打开配置页，填写 WiFi 与路由器地址，保存后自动重启。

配置项说明见 [CONFIG.md](CONFIG.md)。

---

## 六、更新流程

```bash
# 1. 页面改动
python esp32/scripts/build.py

# 2. 核心模块改动（httpsrv / cncal / rap / netmgr / ntpsrv / ntpsync）
python esp32/scripts/mpy.py

# 3. 上传
python esp32/scripts/up_batch.py esp32/www/index.html esp32/www/index.html.gz ...

# 4. 复位并自检
python esp32/scripts/flash.py --no-upload
```

`flash.py` 依次执行：进入 REPL、Ctrl-D 重启、捕获启动日志、请求 `/api/health` 确认服务状态。

### 验证

```bash
curl --noproxy "*" -s -o /dev/null -w "%{http_code} %{time_total}s\n" http://<设备IP>/api/health

for i in $(seq 15); do curl -s -o /dev/null -w "%{http_code} " http://<设备IP>/; done
```

验收标准：响应码全部为 200；`/api/health` 的 `mem_free`，ESP32 版不低于 60KB，
S3 版不低于 7MB。

---

## 七、回滚

固件：

```bash
# ESP32
esptool.py --port COM3 --before no-reset write-flash --erase-all --compress \
  0x1000 backup-4MB.bin

# ESP32-S3
esptool.py --chip esp32s3 --port COM3 write-flash -z 0x0 backup-8MB.bin
```

代码：使用本地快照目录 `backup/ok-YYYYMMDD-HHMMSS/`，按相同流程上传覆盖。

---

## 八、故障处理

| 现象 | 可能原因 | 处理 |
|---|---|---|
| ping 通但 HTTP 无响应 | 程序被 mpremote 中断，停在 REPL | 复位；检查串口是否有 `>>>` |
| HTTP 无响应但 UDP NTP 正常 | lwIP socket 池耗尽 | 完全断电 10 秒后重新上电 |
| `scan()` 返回空列表 | 射频校准丢失 / AP 模式开启 | 恢复 `phy_init`；关闭 AP |
| 连接 WiFi 报 `201 NO_AP_FOUND` | 射频校准丢失，或 SSID 大小写不符 | 同上；核对 SSID |
| 页面可访问但无数据 | 路由器 CGI 未部署或不可达 | 检查 `router.host`；直接访问 CGI 验证 |
| 天气页返回 `missing weather.json` | 落盘文件丢失 | 接口会自动重试；仍异常则重启 |
| 打开多个页面后无响应 | 空闲堆低于 20KB | 通过串口读取 `gc.mem_free()` |
| `incompatible .mpy file` | `.mpy` 与固件版本不匹配 | 重新执行 `scripts/mpy.py` 或 `--revert` |
| 上传报 `could not enter raw repl` | 设备仍在启动 | 等待 3 秒重试 |
| S3 刷完无任何输出 | 烧写偏移写成 `0x1000` | 用 `0x0` 重刷 |
| S3 刷完不自动运行 | 原生 USB 的 `--after hard-reset` 无效 | 手动按 RST，或 `esptool.py run` |
| 空闲后首个请求偏慢 | 降频后未恢复满频 | 检查 `/api/info` 的 `fw.freq` 与 `fw.pm_downs`；可设 `power.on=false` |
| S3 能连上但收发失败 | 内部 RAM 最大连续块过小 | 检查 `/api/info`；最大连续块应 ≥64KB，低于 16KB 时断电重启 |

### 串口诊断

以 115200 打开设备串口，不拉 DTR/RTS；需要 traceback 时发送 Ctrl-C。
Windows 下可使用 `mpremote connect COM3` 或串口工具。

---

## 九、路由器端 CGI（新闻 / 地震，仅 S3）

数据由路由器转发，设备不直接访问公网。脚本位于 `mirouter/cgi/`，
完整组件清单与运行约束见 [mirouter/README.md](../mirouter/README.md)。
设备侧直接使用的条目如下：

| 文件 | 部署到 | 说明 |
|---|---|---|
| `news` | `/data/iptv/www/cgi-bin/news` | `?a=list` 最新一页（本地算上游签名，缓存 5 分钟）；`?a=page&last_time=<秒>` 往前翻页，不缓存 |
| `quake` | `/data/iptv/www/cgi-bin/quake` | `?a=list` 国内速报（`dizhen.php`）、`?a=global` 全球震情速报（`dizhensu.php`，含海外），两路缓存各 30 分钟、锁与缓存文件各自独立 |
| `usgs` | `/data/iptv/www/cgi-bin/usgs` | `?a=list` USGS M2.5+ 近 24 小时（CSV 原样透传），缓存 15 分钟；公开源，无需凭据 |
| `oil` | `/data/iptv/www/cgi-bin/oil` | `?a=list` 全国油价（默认安徽，`?sheng=` 指定省份），JSON 原样透传，缓存 24 小时；需在文件头填写 `Q_ID` / `Q_KEY` |
| `almanac` | `/data/iptv/www/cgi-bin/almanac` | `?a=list` 当日万年历，缓存 24 小时；同样需要 `Q_ID` / `Q_KEY` |

```bash
# 必须是 LF 换行，否则 #!/bin/sh^M 报 not found
scp mirouter/cgi/news mirouter/cgi/quake mirouter/cgi/usgs mirouter/cgi/oil mirouter/cgi/almanac \
  root@<路由器>:/data/iptv/www/cgi-bin/
ssh root@<路由器> 'chmod 755 /data/iptv/www/cgi-bin/news /data/iptv/www/cgi-bin/quake /data/iptv/www/cgi-bin/usgs /data/iptv/www/cgi-bin/oil /data/iptv/www/cgi-bin/almanac'

# 地震接口凭据（600 权限）
ssh root@<路由器> "printf 'Q_ID=你的ID\nQ_KEY=你的KEY\n' > /data/iptv/etc/quake.conf && \
  chmod 600 /data/iptv/etc/quake.conf"
```

设备通过 `router.aux_port`（默认 8088）直连 uhttpd。
nginx 仅代理白名单内的 `/cgi-bin/xxx`，无需修改其配置。

自检：

```bash
curl --noproxy "*" "http://<路由器>:8088/cgi-bin/news?a=cache"
curl --noproxy "*" "http://<路由器>:8088/cgi-bin/quake?a=cache"   # 国内 + 全球两路缓存状态
curl --noproxy "*" "http://<路由器>:8088/cgi-bin/quake?a=list"
curl --noproxy "*" "http://<路由器>:8088/cgi-bin/quake?a=global"
curl --noproxy "*" "http://<路由器>:8088/cgi-bin/usgs?a=list"
curl --noproxy "*" "http://<路由器>:8088/cgi-bin/usgs?a=cache"
curl --noproxy "*" "http://<路由器>:8088/cgi-bin/oil?a=list"
curl --noproxy "*" "http://<路由器>:8088/cgi-bin/almanac?a=list"
```

两个脚本均支持 `?a=flush`；`quake` 同时清理国内与全球缓存。
两个上游接口共用 `/data/iptv/etc/quake.conf` 中的 ID/KEY。
路由器未提供 `sha1sum` 时，新闻脚本使用 `openssl dgst`。
