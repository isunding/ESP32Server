# 变更记录

本项目遵循 [Keep a Changelog](https://keepachangelog.com/) 格式，条目分为
`新增`、`变更`、`修复`、`移除`、`安全` 五类。

---

## 2026-11-08

### 新增
- ESP32-S3 N16R8 板级版本 `esp32-s3/`：Python 堆位于 8MB PSRAM，原生 USB 串口，
  WS2812 指示灯，WiFi 关闭 modem sleep
- 空闲降频（`power`）：连续 `idle_after_s` 秒无请求后降至 `idle_mhz`，
  新连接到达时恢复 `max_mhz`
- 休眠记录与时间轴：`/api/info` 的 `pm` 字段及本机页卡片
- 新闻页 `/news.html`（仅 S3）：重要度与主题筛选、`【】` 标题加粗、
  股票标签加入 / 移出自选
- 地震页 `/quake.html`（仅 S3）：国内 / 全球切换、震级筛选、经纬度半球标注
- `/api/news`、`/api/quake`、`/api/quake?a=global`：按天增量写入 CSV，保留 7 天；
  冷启动返回 `pending`，取数在主循环后台完成
- `/api/watch?a=codes`：返回自选代码清单，不触发行情请求
- 路由器端 `router/cgi/news`、`router/cgi/quake`：签名与缓存代理，
  支持 `?a=global`、`?a=cache`、`?a=flush`
- `/api/info`：芯片温度、复位原因、内部 RAM 分块、WiFi 详情
- 天气页：24 小时温度 / 降水曲线、7 天高低温区间、24 小时 AQI、
  过去 7 天曲线、7 天列表温度区间条与日详情
- `/api/weatherhist`：过去 7 天历史，落盘缓存 12 小时
- 顶栏心跳：每 15 秒刷新 `/api/health`，显示状态点与本机 IP
- `rap.py`：下载读取超时可配置
- 项目文档与 MIT 许可证

### 变更
- 配置模板补齐 `led`、`tz`、`poll.weatherhist_ms`、`router.aux_port`、
  `poll.news_ms`、`poll.quake_ms`；移除 `tz_min`、`first_source`
- 页内工具条样式提取至 `theme.css`
- 天气页预警数据源改为 `warning.alerts`
- 首页与顶栏顺序：天气 · 地震 / 新闻 · 行情 / IPTV · 电台 / 本机 · NTP
- 页面标题与文案统一为「新闻」

### 修复
- 天气页顶栏与导航渲染
- 各页顶栏状态与本机 IP 刷新
- 行情页刷新按钮对齐
- 天气页预警解析：兼容多种返回形状、等级大小写与 UTC 时间换算
- CI 目录结构检查的空结果处理

### 安全
- `config.json`、私钥、固件镜像、路由器端实现与本机维护脚本不入库
- 第三方接口凭据仅存于路由器 `/data/iptv/etc/quake.conf`（权限 600），
  仓库内为占位符

---

## 2026-11-07

### 移除
- 日历页 `/calendar.html` 与 `/api/calmonth`；农历保留在首页与 NTP 页
- 遗留页面 `traffic.html`

### 变更
- 电台分类调整为「华东 · 江浙沪皖」与「安徽 · 合肥」，共 67 台 / 9 类
- 首页卡片与顶栏入口顺序

### 修复
- lwIP socket 池耗尽导致的 HTTP 无响应

---

## 2026-11-06

### 移除
- `/status.html`、`/traffic.html` 及相关接口

### 新增
- `/info.html` 本机信息页、电台页
- 构建、预编译、分批上传脚本

### 变更
- `listen(3)`、响应完成后等待客户端关闭、静态资源内联、
  首页数据合并为 `/api/home`

### 修复
- 长时间运行后 HTTP 无响应
- `select` 异常时的连接释放

---

## 2026-11-05

### 变更
- 项目名称统一为 ESP32 Terminal Server
- 预连接超时调整为 1 秒

### 修复
- 预连接占满 listen backlog

---

## 2026-11-04

### 新增
- 农历、24 节气、节日与年度表缓存
- NTP 时区选择
- 告警事件 CSV 记录

### 移除
- 首页走秒时钟

### 修复
- 事件历史显示异常

---

## 2026-11-03

### 新增
- IPTV 页内嵌播放器、首页天气卡片、板载 NTP 服务端、LED 启动指示

---

## 2026-11-01

### 新增
- ESP32 + MicroPython 终端服务器：首页、天气、状态、NTP、IPTV 页与主题样式系统

### 安全
- 完整烧写时保留射频校准数据
