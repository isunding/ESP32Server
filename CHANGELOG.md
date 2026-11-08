# Changelog

格式参考 [Keep a Changelog](https://keepachangelog.com/)，按日期组织。

标注：`新增` / `变更` / `修复` / `移除` / `安全`

---

## 2026-11-08

### 新增
- `README.md`：项目说明、页面清单、架构图、部署指引
- `docs/ARCHITECTURE.md`：启动流程、请求生命周期、构建流水线
- `docs/CONFIG.md`：配置项说明
- `docs/DEPLOY.md`：烧写与部署步骤
- `CHANGELOG.md`、`LICENSE`（MIT）、`.gitignore`
- 天气页：24 小时温度/降水折线图、7 天高低温区间图、未来 24 小时 AQI 柱状图、
  过去 7 天曲线（逐小时/逐日切换）、天气图标、生活指数建议文字
- `/api/weatherhist`：过去 7 天历史接口，落盘缓存 12 小时，取数在主线后台完成
- `rap.py`：下载超时改为可配置（`dl` 参数，默认仍 4 秒）

### 变更
- `config.example.json`：补齐 `led`、`tz` 字段，移除废弃字段 `tz_min`、`first_source`
- `config.example.json`：新增 `poll.weatherhist_ms`
- `esp32/README.md` 不再随仓库发布
- 天气页预警改为解析 `warning.alerts`（`headline` / `description` /
  `eventType.name` / `issuedTime`），此前读取的字段名不存在，导致有预警时仍显示「无生效预警」
- 天气页体积 8.7 KB → 15.6 KB（gzip）

### 修复
- 天气页预警恒显示为空

### 安全
- `config.json`（含 WiFi 密码）不入库，仅保留脱敏模板
- 排除私钥、固件镜像、路由器端历史实现与旧截图

---

## 2026-11-07

### 移除
- 日历页 `/calendar.html` 与 `/api/calmonth`；农历保留在首页与 NTP 页
- 遗留文件 `web/traffic.html`、`www/traffic.html`

### 变更
- 电台分类：移除「广东 · 华南」，新增「华东 · 江浙沪皖」（17 台）与「安徽 · 合肥」（9 台），
  合计 67 台 / 9 类
- 首页卡片顺序调整为：天气·行情 / IPTV·电台 / 本机·NTP
- 顶栏页签调整为 7 项

### 修复
- HTTP 请求无响应：lwIP socket 池耗尽，需完全断电后恢复
- 回退计算器页面至 `ok-20261107-110054` 基线

---

## 2026-11-06

### 移除
- 状态页 `/status.html` 与 `/api/status`，由 `/info.html` 替代
- 流量页 `/traffic.html` 与 `/api/clients`
- 菜单中的告警入口及相关样式

### 新增
- `/info.html`：本机信息（固件、板型、CPU、MAC、堆、Flash、文件清单、启动耗时）
- 电台页
- `scripts/build.py`：页面构建（内联 CSS/JS、生成 gzip）
- `scripts/mpy.py`：模块预编译为 `.mpy`
- `scripts/up_batch.py`：分批上传

### 修复
- 长时间运行后 HTTP 无响应：改为每个请求结束执行 `gc.collect()`
- `select` 抛 `ValueError` 时逐个关闭连接，避免 fd 泄漏

### 变更
- `listen(12)` 调整为 `listen(3)`
- 新增 `_draining` 队列，响应完成后等待客户端关闭
- 静态资源内联，单页连接数由 4~5 条降至 1~2 条
- 首页合并为 `/api/home`，首屏请求由 4 条降至 2 条

---

## 2026-11-05

### 变更
- 对外名称统一为 ESP32 Terminal Server
- 预连接超时由 3 秒调整为 1 秒

### 修复
- 预连接占满 listen backlog

---

## 2026-11-04

### 新增
- 农历、24 节气、节日；年度表缓存至 `calendar.json`
- NTP 时区下拉选择
- 告警事件写入 CSV

### 移除
- 首页走秒时钟

### 修复
- 事件历史显示异常（浏览器缓存）

---

## 2026-11-03

### 新增
- IPTV 页内嵌播放器（mpegts.js），左右布局
- 首页天气实况卡片
- 板载 NTP 服务端（UDP 123）
- 板载 LED 启动指示

---

## 2026-11-01

### 新增
- 项目由路由器端方案移植为 ESP32 + MicroPython 终端服务器
- 首页 / 天气 / 状态 / NTP / IPTV 页面
- 主题样式系统（`theme.css`）
- 烧写与串口探测脚本

### 安全
- 完整烧写时保留 `0xF000` 处的射频校准数据

---

## 前身

路由器端的 IPTV 组播转单播与控制台方案（未包含在本仓库）。ESP32 侧为其展示终端。
