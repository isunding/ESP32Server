# 配置说明

## 一、配置文件

板端配置文件为 `/config.json`（文件系统根目录）。仓库提供脱敏模板，
两个板级目录各一份，字段一致，`led` 默认值不同：

```bash
cp esp32/config.example.json    esp32/config.json       # ESP32
cp esp32-s3/config.example.json esp32-s3/config.json    # ESP32-S3
```

`config.json` 已列入 `.gitignore`。

### 修改方式

| 方式 | 操作 | 适用场景 |
|---|---|---|
| 文件 | 编辑 `config.json` 后软复位（Ctrl-D） | 首次部署 |
| Web | 页面修改后 `POST /api/config`，设备自动重启 | 日常调整 |
| 串口 | `mpremote` 修改文件后复位 | 无法联网时 |

`GET /api/config` 返回时密码被打码为 `********`。
`POST` 时提交 `********` 被视为未修改，保留原值。

---

## 二、字段

```jsonc
{
  "device_name": "客厅终端",        // 显示名

  "wifi": {
    "ssid": "YOUR_WIFI_SSID",       // 留空则启动配网热点
    "password": "YOUR_WIFI_PASSWORD",
    "hostname": "server",           // DHCP 主机名
    "ap_ssid": "ESP32-Setup",       // 配网热点名称
    "ap_pass": "esp32setup123"      // 配网热点密码，≥8 位
  },

  "router": {
    "host": "192.168.1.1",          // 数据源路由器地址
    "port": 80,
    "r2h_port": 5140,               // rtp2httpd 端口
    "udpxy_port": 12123,            // udpxy 端口
    "aux_port": 8088                // 路由器 uhttpd 端口（新闻/地震 CGI 走这里）
  },

  "ntp": {
    "upstream": ["ntp.aliyun.com", "ntp.tencent.com", "cn.pool.ntp.org"],
    "sync_ms": 1800000,             // 同步间隔，30 分钟
    "tz": 8                         // 时区小时数
  },

  "poll": {
    "weather_ms": 600000,           // 天气缓存 TTL
    "weatherhist_ms": 43200000,     // 过去 7 天历史缓存 TTL（12 小时）
    "channels_ms": 300000,          // 频道库缓存 TTL
    "news_ms": 300000,              // 新闻电报 TTL（仅 S3）
    "quake_ms": 10800000            // 地震 TTL（3 小时，仅 S3）
  },

  "led": {
    "on": 1,                        // 0 表示就绪后熄灭
    "period_ms": 2000,
    "pulse_ms": 70
  },

  "power": {
    "on": true,                     // 空闲降频开关
    "max_mhz": 240,                 // 开机拉回的满频
    "idle_mhz": 80,                 // 空闲时降到多少 MHz
    "idle_after_s": 20              // 连续多少秒无请求算空闲
  },

  "quote": {
    "on": true,
    "ttl_ms": 600000,
    "items": [
      { "code": "sh000001", "name": "上证指数", "kind": "index" }
    ]
  }
}
```

---

## 三、字段说明

### `wifi`

`ssid` 留空时，设备启动后开启配网热点（`ap_ssid` / `ap_pass`）。
连接热点后可访问配置页填写 WiFi 与路由器地址。

`hostname` 通过 DHCP 上报，在支持本地 DNS 的路由器上可用 `http://server/` 访问。
`.lan` / `.local` 后缀不可解析时使用裸主机名。

### `router`

数据源地址。设备向以下 CGI 请求数据：

| 路径 | 用途 |
|---|---|
| `/cgi-bin/sysstat?a=stat` `?a=dhcp` `?a=arp` | 设备状态、客户端列表 |
| `/cgi-bin/weather?a=data` | 天气聚合数据 |
| `/cgi-bin/iptv?a=list` | 频道库 |

上述 CGI 需单独部署，不在本仓库内。未提供时天气、频道与服务状态为空，
其余功能不受影响。

### `ntp`

设备同时作为 NTP 服务端监听 UDP 123，`upstream` 为其公网时间源。

`tz` 单位为小时，支持小数：`8` 为 UTC+8，`5.5` 为 UTC+5:30，`-5` 为 UTC-5。

### `poll`

缓存有效期。除新闻定时补采外，数据过期后由下一次请求触发拉取。

`weather_ms` 为板端缓存有效期。路由器 CGI 另有独立缓存；手动刷新
（`/api/weather?force=1`）同时清除两级缓存，取数在主循环后台完成，
页面在数据时间戳变化后重新渲染。

`news_ms` / `quake_ms` 只对 S3 版生效。取回的数据按天追加到板上的 CSV
（`news-YYYYMMDD.csv`、`quake-YYYYMMDD.csv`、`quakeg-YYYYMMDD.csv`），保留最近 7 天。

新闻按 `news_tick_ms`（默认 1 小时）定时补采，由主循环设置补刷标记，不占用
请求路径。每次从最新一页开始，以本页最早一条的时间作为 `last_time` 逐页
向前，直到追平上次采集时刻或达到 `news_pages`（默认 6 页）。翻页请求经
路由器 CGI 的 `?a=page&last_time=<秒>` 转发，不进入首页缓存。调大
`news_pages` 可覆盖更长的历史空档，取数耗时随之增加。

### `router.aux_port`

新闻与地震 CGI 经路由器 uhttpd 端口（默认 8088）直连。
nginx 仅代理白名单内的 `/cgi-bin/xxx`，无需修改其配置。

### `led`

板载指示灯。`on: 1` 时按 `period_ms` 周期点亮 `pulse_ms` 毫秒，
`on: 0` 时上电写入一次关闭值，之后不再驱动。

S3 版为单颗 WS2812（GPIO48），ESP32 版为普通引脚单色灯，用 `kind` 区分驱动方式：

```json
"led": { "on": 1, "kind": "np", "pin": 48, "color": [0, 10, 0],
         "period_ms": 2000, "pulse_ms": 60 }
```

`kind: "np"` 走 neopixel（RMT），`kind: "gpio"` 走普通引脚电平。
`color` 为 WS2812 的 RGB 值，低亮度心跳取较小数值；`pulse_ms` 相对
`period_ms` 越小，占空比越低。

网页顶栏 IP 右侧的波形图标用于即时切换心跳：点击写入 `led.on` 并立即生效，
不重启设备，状态保存在配置文件中。`/api/health` 的 `led_hw` 为 0 时
（无可控指示灯）不显示该图标。

板载 RX / TX 指示灯由 USB-UART 桥接芯片的 RXD / TXD 电平直接驱动，
与 ESP32 的 GPIO 无连接，不受本配置控制。

### `power`

空闲降频用于降低空闲时段的功耗与芯片温度。

- 连续 `idle_after_s` 秒没有任何 HTTP 请求 → `machine.freq(idle_mhz)`
- 新连接被 accept 时恢复 `max_mhz`
- `idle_mhz` 下限 80：WiFi 需要 80MHz 的 APB 时钟，Octal PSRAM 时序也按它配置，
  填更低的值会被忽略并按「不开启」处理
- `max_mhz` 取自配置，不读取 `machine.freq()`：`esp_pm` 时钟配置不随软复位恢复
- 板级不支持改频时，首次切换失败后自动关闭该功能并记录日志

访问活动以 HTTP 服务的 accept 计数为准；NTP 与 TCP 握手不计入。

### `quote`

行情页标的，使用新浪行情代码：

- 指数：`sh000001`（上证）、`sz399001`（深成）、`sz399006`（创业板）
- 汇率：`fx_susdcny`、`fx_seurcny`、`fx_shkdcny`
- 个股：`sh600519`、`sz000001`

`kind` 决定页面分组：`index` 为指数区，`fx` 为汇率区。
多个标的由一次 `/api/quote` 请求批量获取。

---

## 四、天气数据与认证

ESP32 侧不持有天气服务的凭据。天气数据由路由器 CGI 提供：

| 项目 | 位置 |
|---|---|
| 认证方式 | 路由器 CGI（JWT / API Key） |
| 密钥与凭据 | 路由器本地存储 |
| 令牌续签 | 路由器定时任务 |

设备仅请求 `/cgi-bin/weather?a=data`，获取已聚合的 JSON。

配置要点：

- 无需在 `config.json` 中填写任何 API Key
- 更换天气服务商只需修改路由器端 CGI
- 设备侧不存储天气服务凭据

### 无路由器 CGI 时直连天气 API

需自行实现处理函数。密钥应单独存放并列入 `.gitignore`，
不写入源码或 `config.json`。

MCU 无安全存储，写入 flash 的凭据在物理接触设备时可被读取。

---

## 五、安全

| 项 | 建议 |
|---|---|
| WiFi 密码 | 明文存于 flash，设备不应部署在不可信网段 |
| 配网热点密码 | 默认 `esp32setup123`，首次配置后建议修改 |
| 传输加密 | 不支持 HTTPS，不应暴露至公网 |
| 配置接口 | 无鉴权，仅限局域网使用 |
| 第三方密钥 | 存放于路由器端，不存放于 MCU |

本服务面向局域网使用；对外提供访问时需配置反向代理与认证。

---

## 六、最小配置

最小配置（本机信息、农历、NTP）：

```json
{
  "wifi": {
    "ssid": "你的WiFi",
    "password": "你的密码"
  }
}
```

其余字段使用 `DEFAULT_CFG` 默认值；天气与 IPTV 页面无数据。
