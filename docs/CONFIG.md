# 配置说明

## 一、配置文件

板端配置文件为 `/config.json`（文件系统根目录）。仓库内仅提供脱敏模板：

```bash
cp esp32/config.example.json esp32/config.json
```

`config.json` 已在 `.gitignore` 中，不会被提交。

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
    "udpxy_port": 12123             // udpxy 端口
  },

  "ntp": {
    "upstream": ["ntp.aliyun.com", "ntp.tencent.com", "cn.pool.ntp.org"],
    "sync_ms": 1800000,             // 同步间隔，30 分钟
    "tz": 8                         // 时区小时数
  },

  "poll": {
    "weather_ms": 600000,           // 天气缓存 TTL
    "weatherhist_ms": 43200000,     // 过去 7 天历史缓存 TTL（12 小时）
    "channels_ms": 300000           // 频道库缓存 TTL
  },

  "led": {
    "on": 1,                        // 0 表示就绪后熄灭
    "period_ms": 2000,
    "pulse_ms": 70
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
若 `.lan` / `.local` 后缀无法解析，使用裸名。

### `router`

数据源地址。设备向以下 CGI 请求数据：

| 路径 | 用途 |
|---|---|
| `/cgi-bin/sysstat?a=stat` `?a=dhcp` `?a=arp` | 设备状态、客户端列表 |
| `/cgi-bin/weather?a=data` | 天气聚合数据 |
| `/cgi-bin/iptv?a=list` | 频道库 |

这些 CGI 需自行部署，不在本仓库内。缺少时天气、频道、服务状态显示为空，
本机信息、NTP、电台、农历、行情功能不受影响。

### `ntp`

设备同时作为 NTP 服务端监听 UDP 123，`upstream` 为其公网时间源。

`tz` 单位为**小时**，支持小数：`8` 为北京时间，`5.5` 为印度时区，`-5` 为美东时区。

### `poll`

缓存有效期，无后台轮询。数据过期后由下一次请求触发拉取。

### `led`

板载 LED（GPIO2）指示：上电常亮，服务就绪后按 `period_ms` 闪烁 `pulse_ms` 毫秒。
`on: 0` 时就绪后熄灭。部分开发板 LED 未连接 GPIO2，此时无显示。

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

因此：

- 无需在 `config.json` 中填写任何 API Key
- 更换天气服务商只需修改路由器端 CGI
- 设备侧不存储天气服务凭据

### 无路由器 CGI 时直连天气 API

需自行实现处理函数（参考 `h_weather`）。密钥不应写入源码或 `config.json`，
建议单独存放于 `esp32/secrets.json` 并加入 `.gitignore`。

MCU 无安全密钥存储，写入 flash 的凭据在物理接触设备时可被读取。

---

## 五、安全

| 项 | 建议 |
|---|---|
| WiFi 密码 | 明文存于 flash，设备不应部署在不可信网段 |
| 配网热点密码 | 默认 `esp32setup123`，首次配置后建议修改 |
| 传输加密 | 不支持 HTTPS，不应暴露至公网 |
| 配置接口 | 无鉴权，仅限局域网使用 |
| 第三方密钥 | 存放于路由器端，不存放于 MCU |

本服务定位为局域网展示终端。对外提供访问时需自行增加反向代理与认证。

---

## 六、最小配置

仅需本机信息、农历、NTP 功能时：

```json
{
  "wifi": {
    "ssid": "你的WiFi",
    "password": "你的密码"
  }
}
```

其余字段使用 `DEFAULT_CFG` 默认值。天气与 IPTV 页面将无数据。
