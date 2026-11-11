# 开发约定

本文件定义仓库的修改规则与验证要求，提交前须完整阅读「不可改动项」。

## 项目

ESP32 + MicroPython 的无头终端服务器：单线程 HTTP 服务，向局域网提供 7 个页面与一组 API，
数据来自路由器的 CGI 接口。固件源码有两份板级副本——`esp32/`（ESP32-D0WD-V3）与
`esp32-s3/`（ESP32-S3 N16R8），逻辑一致，差异只在 LED 驱动、WiFi 省电、内存布局与频率管理。
文档在 `docs/`，路由器端组件在 `mirouter/`。

运行环境约束：单线程、可用 TCP PCB 约 10 个；ESP32 版堆约 70KB，
S3 版 Python 堆在 8MB PSRAM、内部 SRAM 留给 WiFi 与 lwIP。

**两个目录要同步改**：页面、路由、API 的改动必须同时落到 `esp32/` 与 `esp32-s3/`，
除非改动本身是板级差异（LED / 频率 / 内存）。唯一允许的例外是 `news.html` 与
`quake.html` 两页及其 `/api/news`、`/api/quake`——它们只有 S3 版有（CI 里列了白名单）。

## 路由器端 CGI

新闻与地震的数据由路由器上的 shell CGI 转发（`/data/iptv/www/cgi-bin/`），源码在 `mirouter/cgi/`。
设备经 uhttpd 的 8088 端口直连；nginx 仅代理白名单内的 `/cgi-bin/xxx`，无需修改其配置。
组件的完整清单、端口与部署方式见 `mirouter/README.md`。

- 路由器**没有 `sha1sum`**（busybox 只编了 `md5sum`），哈希要退回 `openssl dgst`；
  直接写 `sha1sum` 会得到空签名，上游回 `errno 10012`
- CGI 文件必须是 LF 换行。CRLF 会让 `#!/bin/sh^M` 报 not found
- 缓存写在 `/tmp`（tmpfs），不要写 `/data`，免得反复擦写 flash
- 并发只用一个 `mkdir` 原子锁做单飞，不要用 `flock`（busybox 不一定有）
- 新闻 CGI 的 `?a=page&last_time=<秒>` 用于往前翻页，不进入首页缓存；
  参数按字典序参与签名，且只接受纯数字时间戳

---

## 不可改动项

以下实现为稳定运行的必要条件，不得简化或替换。

| 位置 | 实现 | 约束依据 |
|---|---|---|
| `httpsrv.py` `start()` | `listen(3)` | lwIP 在三次握手时即分配 PCB。改大后未发请求的预连接占满池子，真实请求被重置，整站无响应 |
| `httpsrv.py` `_serve()` | 结束时 `gc.collect()` | 临时对象累积使空闲堆降至 20KB 以下，`accept()` 与内存分配失败 |
| `httpsrv.py` `_draining` | 响应完成后不主动 `close()`，等客户端关闭 | MicroPython 无 `SO_LINGER`，主动关闭使 PCB 停留 TIME_WAIT 60 秒，池子被占满 |
| `httpsrv.py` `poll()` | `select` 超时 30ms | 设为 0 会导致 lwIP 任务得不到调度，出现丢包 |
| `httpsrv.py` `_in_poll` | 重入计数上限 2 | 移除后嵌套 `poll()` 无限递归 |
| `rap.py` 下载路径 | 边接收边写文件，单次分配 ≤ 8KB | GC 不压缩内存，大块连续分配失败后堆难以恢复 |
| `rap.py` 落盘 | 写 `.tmp` 后 rename，优先覆盖 | 先删旧再改名会留下空窗，并发读拿到 404 |
| `main.py` 主循环 | 除新闻定时补采外无周期性取数 | 路由器 CGI 需 1~1.6 秒；周期性并发取数会阻塞单线程主循环 |
| `main.py` `h_weatherhist()` | 只吐落盘文件，取数交给 `refresh_weather_history()` | 历史天气要串行汇总 7 天，冷启动 5~15 秒，放进请求路径会堵死主循环 |
| `main.py` `idle_power()`（S3） | `idle_mhz` 不低于 80，调用点放在 `web.poll()` 之后 | 80MHz 是 WiFi 的 APB 与 Octal PSRAM 时序的下限，更低会断网或崩堆；放在 poll 之前会漏掉本轮 accept |
| `main.py` 频率基准 | 满频取配置 `power.max_mhz`，不读 `machine.freq()` | `esp_pm` 时钟配置不随软复位恢复 |
| `main.py` `_clock_ok()` | 写日文件与 `_csv_prune()` 前校验系统年份 | 断电重启后 NTP 同步前时间停在 2000 年，按日期落盘会写错文件，清理会删掉真实历史 |
| `main.py` `h_weather(force=1)` | 先清路由器 `a=flush` 与板端缓存，取数交主循环 | 路由器 CGI 有独立缓存；在请求路径等待上游取数会阻塞单线程主循环 |

页面约束：

- 页面须为单文件（CSS/JS 内联）；外链资源会增加 TCP 连接数。
- 页面源码在 `web/`，`www/` 为构建产物。

---

## 禁止

1. **不要直接修改 `esp32/www/` 或 `esp32-s3/www/`**。它们由各自的 `web/` 经 `scripts/build.py` 生成，下次构建会被覆盖。
2. **不要提交** `esp32/config.json`、`esp32-s3/config.json`、`*.pem`、`*.bin`、`firmware/`、`legacy/`、
   `esp32/tests/`、`esp32-s3/factory/`、`*/build_mpy/`。前两项含真实 WiFi 凭据，`firmware/` 与 `legacy/` 含设备数据。
   `mirouter/cgi/` 中的 `Q_ID` / `Q_KEY` 与 `W_HOST` 保持空值或占位符，凭据在部署时注入。
   `mirouter/bin/` 的 udpxy、rtp2httpd 为 GPL-2.0 二进制，是唯一允许入库的可执行文件。
3. **不要引入** `asyncio`、`thread`、`_thread`、第三方库。运行环境不支持且有额外内存开销。
4. **不要在请求处理中做超过 1 秒的阻塞操作**（除非配合 TTL 缓存）。
5. **不要在页面中引用 CDN**（外部 CSS/JS/字体/图片）。
6. **不要在 JS 注释里写 `*/`**（例如 `ngcdn*/cnr.cn`），会提前闭合块注释导致整段脚本不执行。
7. **除新闻的每小时定时补采外，不要把联网接口做成后台轮询**，
   只在请求到达且缓存过期时拉取。新闻补采周期由 `poll.news_tick_ms` 控制，
   每次最多 `poll.news_pages` 页，取数仍在主循环内完成。

---

## 修改流程

### 改页面

```bash
# 改 esp32/web/*.html 与 esp32-s3/web/*.html、theme.css、ui.js
python esp32/scripts/build.py          # 生成 esp32/www/（内联 + gzip）
python esp32-s3/scripts/build.py       # 生成 esp32-s3/www/
# 提交时 web/ 与 www/ 一起提交
```

新增页面还需：`main.py` 的 `setup_routes()` 注册 `serve_file()`、`web/ui.js` 的 `PAGES` 加页签、
`web/index.html` 的 `TILES` 加入口。

### 改固件模块

直接改 `esp32/*.py`（或 `esp32-s3/*.py`）。部署时若板上有 `.mpy`，需重新执行对应的
`scripts/mpy.py`，否则 `.py` 不生效（MicroPython 优先加载 `.py`，但板上若只剩 `.mpy` 则不会更新）。

### 改路由器端组件

直接改 `mirouter/cgi/*` 与 `mirouter/iptv/*`，用 `mirouter/tools/deploy.py` 上传，
不手工编辑路由器上的文件（`start.sh` 会在启动时重写 nginx 规则与播放列表）。

```bash
for f in mirouter/cgi/* mirouter/iptv/*.sh; do sh -n "$f"; done   # 语法检查
python mirouter/tools/deploy.py all
```

CGI 与脚本必须是 LF 换行；凭据不写入仓库。

### 改配置项

四处同步：各板 `main.py` 的 `DEFAULT_CFG`（必须有默认值）、`config.example.json`、`docs/CONFIG.md`。

### 改 API

`main.py` 新增处理函数 + `setup_routes()` 注册，并更新 `README.md` 的 API 表。

### 改文档

`README.md`（功能与用法）、`docs/ARCHITECTURE.md`（机制）、`docs/CONFIG.md`（配置）、
`docs/DEPLOY.md`（部署）、`CHANGELOG.md`（变更条目）。

文档使用陈述式写法：描述机制与规则，不写调试过程、故障复盘、环境细节。

---

## 验证

```bash
# Python 语法（两个板级目录）
python -m compileall -q esp32/*.py esp32/scripts esp32-s3/*.py esp32-s3/scripts

# 配置模板有效
python -c "import json; json.load(open('esp32/config.example.json', encoding='utf-8'))"
python -c "import json; json.load(open('esp32-s3/config.example.json', encoding='utf-8'))"

# 页面构建可重现（www/ 应与 web/ 同步）
python esp32/scripts/build.py    && git diff --exit-code -- esp32/www
python esp32-s3/scripts/build.py && git diff --exit-code -- esp32-s3/www
```

仓库不含测试代码。涉及运行时行为的改动无法在 CI 中验证，提交说明中须标注验证方式。

---

## 提交

- 一个提交只做一类改动：页面、固件、文档分开。
- 改页面时 `web/` 与 `www/` 放在同一提交。
- 提交信息格式：`类型: 简述`，类型取 `feat` / `fix` / `docs` / `chore`。
- 发布新版本时同步更新 `CHANGELOG.md`，并按 `vX.Y.Z` 打标签。
