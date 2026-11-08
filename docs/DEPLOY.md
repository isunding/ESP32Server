# 烧写与部署

适用于 Windows + esptool + mpremote。两个板级目录（`esp32/`、`esp32-s3/`）的部署流程一致，
差异只在固件变体、烧写偏移与 USB 串口形态。

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

固件从 [micropython.org/download](https://micropython.org/download/) 下载：
ESP32 用 `ESP32_GENERIC` 变体（不用 spiram / d2wd / unicore / ota）；
S3 必须用 `ESP32_GENERIC_S3-SPIRAM_OCT` 变体——板载 8MB Octal PSRAM 只有这个变体带驱动。

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

ESP32 波特率 230400 较稳定（1.7MB 固件约 51 秒）；921600 在部分 CH9102 上会中断。
S3 用 460800 即可（2.4MB 固件约 20 秒）。

S3 注意两点：**偏移是 `0x0`**（写入 bootloader 起点，用 `0x1000` 会起不来）；
`--after hard-reset` 对原生 USB 无效，刷完可能要手动按 RST 或执行 `esptool.py run`。

---

## 三、两项注意

### 1. 射频校准数据（ESP32 版）

`0xF000` 处的 4KB 为出厂射频校准数据。`--erase-all` 会将其一并擦除，导致：

- `scan()` 返回空列表
- 连接 WiFi 报 `201 NO_AP_FOUND`
- 热点模式仍可正常工作

处理方法：从烧写前的整片备份中恢复该 4KB。

```bash
mpremote connect COM3 cp phy_init.bin :
mpremote connect COM3 exec "import esp; esp.flash_write(0xf000, open('phy_init.bin','rb').read())"
```

校验：`esp.flash_read(0xf000, 4)` 返回 `b'PHYI'`。

S3 版没有这个位置：射频参数在独立分区，整片擦除后首次启动自行做完整校准（首启略慢），
无需手工恢复。

另：热点开启状态下 `scan()` 同样返回空，排查 WiFi 时先关闭 AP 模式：
`network.WLAN(network.AP_IF).active(False)`。

### 2. mpremote 中断运行

`mpremote` 每次连接会发送 Ctrl-C 中断当前程序，传输完成后不会自动恢复。
表现为设备可 ping 通但 HTTP 无响应，串口停在 REPL。

传输完成后需复位：

```bash
mpremote connect COM3 exec "import machine; machine.reset()"
# 或
python esp32/scripts/flash.py --no-upload
```

---

## 四、部署代码

以下命令以 ESP32 版为例。S3 版把 `esp32/` 全部换成 `esp32-s3/`，串口换成 S3 的口
（运行模式是原生 USB，下载模式是另一个 PID/端口号，换口时注意区分）。

### 方式 A：逐个上传

```bash
mpremote connect COM3 fs mkdir :www

mpremote connect COM3 cp esp32/boot.py esp32/main.py esp32/netmgr.py \
  esp32/httpsrv.py esp32/rap.py esp32/cncal.py esp32/ntpsync.py esp32/ntpsrv.py :

mpremote connect COM3 cp esp32/config.json :

python esp32/scripts/build.py
mpremote connect COM3 cp esp32/www/index.html esp32/www/weather.html :www
```

`mpremote` 单次传输超过约 8 个文件会超时，使用分批脚本：

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

MicroPython 优先加载 `.py`，`.mpy` 仅在无同名 `.py` 时生效。部署 `.mpy` 需同时删除板上对应 `.py`。

6 个模块预编译后，导入耗时约 1.7s 降至 1.0s，服务就绪时间减少约 1.6 秒。
`.mpy` 与解释器版本绑定（当前 `.mpy v6.3`），升级固件后需重新编译。

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

判定标准：全部 200，单页 0.35~0.55s（受客户端网络影响较大，局域网内波动属正常）；
`/api/health` 的 `mem_free`：ESP32 版保持 60KB 以上，S3 版在 7MB 以上（Python 堆在 PSRAM）。

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

代码：使用本地保留的 `backup/ok-YYYYMMDD-HHMMSS/` 快照，按相同流程上传覆盖。

---

## 八、故障排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| ping 通但 HTTP 无响应 | 程序被 mpremote 中断，停在 REPL | 复位；检查串口是否有 `>>>` |
| HTTP 无响应但 UDP NTP 正常 | lwIP socket 池耗尽 | 完全断电 10 秒后重新上电 |
| `scan()` 返回空列表 | 射频校准丢失 / AP 模式开启 | 恢复 `phy_init`；关闭 AP |
| 连接 WiFi 报 `201 NO_AP_FOUND` | 射频校准丢失，或 SSID 大小写不符 | 同上；核对 SSID |
| 页面可访问但无数据 | 路由器 CGI 未部署或不可达 | 检查 `router.host`；直接访问 CGI 验证 |
| 天气页返回 `missing weather.json` | 落盘文件丢失 | 接口会自动重试；仍异常则重启 |
| 打开多个页面后无响应 | 空闲堆低于 20KB | 串口查看 `gc.mem_free()` |
| `incompatible .mpy file` | `.mpy` 与固件版本不匹配 | 重新执行 `scripts/mpy.py` 或 `--revert` |
| 上传报 `could not enter raw repl` | 设备仍在启动 | 等待 3 秒重试 |
| S3 刷完无任何输出 | 烧写偏移写成 `0x1000` | 用 `0x0` 重刷 |
| S3 刷完不自动运行 | 原生 USB 的 `--after hard-reset` 无效 | 手动按 RST，或 `esptool.py run` |
| 空闲后首个请求偏慢 | 空闲降频停在低频没回升 | 看 `/api/info` 的 `fw.freq` / `fw.pm_downs`；可先 `power.on=false` 关掉降频对比 |
| S3 能连上但收发失败 | 内部 RAM 最大连续块过小（Python 堆在 PSRAM，不占内部 RAM） | `/api/info` 看内部 RAM 分块，最大连续块应 ≥64KB；低于 16KB 需断电重启 |

### 串口诊断

用任意串口工具以 115200 打开设备串口（不拉 DTR/RTS，避免中断运行中的程序），
先读取输出；需要 traceback 时发送 Ctrl-C 中断程序。

Windows 上可用 `mpremote connect COM3`，或 PuTTY / 串口助手。

---

## 九、路由器端 CGI（新闻 / 地震，仅 S3）

数据经路由器转发，板子不直接访问公网。两个脚本在 `router/cgi/`：

| 文件 | 部署到 | 说明 |
|---|---|---|
| `news` | `/data/iptv/www/cgi-bin/news` | 财联社电报，本地算签名，缓存 5 分钟 |
| `quake` | `/data/iptv/www/cgi-bin/quake` | 中国地震台网（apihz），缓存 30 分钟 |

```bash
# 必须是 LF 换行，否则 #!/bin/sh^M 报 not found
scp router/cgi/news router/cgi/quake root@<路由器>:/data/iptv/www/cgi-bin/
ssh root@<路由器> 'chmod 755 /data/iptv/www/cgi-bin/news /data/iptv/www/cgi-bin/quake'

# 地震接口凭据（600 权限）
ssh root@<路由器> "printf 'Q_ID=你的ID\nQ_KEY=你的KEY\n' > /data/iptv/etc/quake.conf && \
  chmod 600 /data/iptv/etc/quake.conf"
```

板子通过 `router.aux_port`（默认 8088）直连 uhttpd，**不需要改 nginx**——
nginx 只对白名单里的 `/cgi-bin/xxx` 做了反代，新加 CGI 不必动它。

自检：

```bash
curl --noproxy "*" "http://<路由器>:8088/cgi-bin/news?a=cache"
curl --noproxy "*" "http://<路由器>:8088/cgi-bin/quake?a=list"
```

两个脚本都有 `?a=flush` 清缓存。路由器上没有 `sha1sum`，脚本会退回 `openssl dgst`。
