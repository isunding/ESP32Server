# 编码助手说明

本文件供 AI 编码助手（Claude Code / Cursor / Copilot / Codex 等）阅读。
修改本仓库前请先读完，尤其是「不可改动项」。

## 项目

ESP32 + MicroPython 的无头终端服务器：单线程 HTTP 服务，向局域网提供 7 个页面与一组 API，
数据来自路由器的 CGI 接口。固件源码在 `esp32/`，文档在 `docs/`。

运行环境约束：单线程、可用 TCP PCB 约 10 个、堆约 70KB。

---

## 不可改动项

以下代码看起来冗余或可疑，但都是维持稳定运行的必要条件。**不要"优化"它们。**

| 位置 | 内容 | 改动后果 |
|---|---|---|
| `httpsrv.py` `start()` | `listen(3)` | lwIP 在三次握手时即分配 PCB。改大后未发请求的预连接占满池子，真实请求被重置，整站无响应 |
| `httpsrv.py` `_serve()` | 结束时 `gc.collect()` | 临时对象累积使空闲堆降至 20KB 以下，`accept()` 与内存分配失败 |
| `httpsrv.py` `_draining` | 响应完成后不主动 `close()`，等客户端关闭 | MicroPython 无 `SO_LINGER`，主动关闭使 PCB 停留 TIME_WAIT 60 秒，池子被占满 |
| `httpsrv.py` `poll()` | `select` 超时 30ms | 设为 0 会导致 lwIP 任务得不到调度，出现丢包 |
| `httpsrv.py` `_in_poll` | 重入计数上限 2 | 移除后嵌套 `poll()` 无限递归 |
| `rap.py` 下载路径 | 边接收边写文件，单次分配 ≤ 8KB | GC 不压缩内存，大块连续分配失败后堆难以恢复 |
| `rap.py` 落盘 | 写 `.tmp` 后 rename，优先覆盖 | 先删旧再改名会留下空窗，并发读拿到 404 |
| `main.py` 主循环 | 无周期性取数 | 路由器 CGI 需 1~1.6 秒，后台轮询会阻塞单线程主循环 |

页面侧同样有约束：

- 页面必须单文件（CSS/JS 内联）。引入外链资源等于增加 TCP 连接，直接逼近 PCB 上限。
- 页面源码在 `web/`，`www/` 是构建产物。

---

## 禁止

1. **不要直接修改 `esp32/www/`**。它由 `esp32/web/` 经 `esp32/scripts/build.py` 生成，下次构建会被覆盖。
2. **不要提交** `esp32/config.json`、`*.pem`、`*.bin`、`firmware/`、`legacy/`、`esp32/tests/`。前四项含真实凭据或设备数据。
3. **不要引入** `asyncio`、`thread`、`_thread`、第三方库。运行环境不支持且有额外内存开销。
4. **不要在请求处理中做超过 1 秒的阻塞操作**（除非配合 TTL 缓存）。
5. **不要在页面中引用 CDN**（外部 CSS/JS/字体/图片）。
6. **不要在 JS 注释里写 `*/`**（例如 `ngcdn*/cnr.cn`），会提前闭合块注释导致整段脚本不执行。
7. **不要把新增联网接口做成后台轮询**，只在请求到达且缓存过期时拉取。

---

## 修改流程

### 改页面

```bash
# 改 esp32/web/*.html、theme.css、ui.js
python esp32/scripts/build.py          # 生成 esp32/www/（内联 + gzip）
# 提交时 web/ 与 www/ 一起提交
```

新增页面还需：`main.py` 的 `setup_routes()` 注册 `serve_file()`、`web/ui.js` 的 `PAGES` 加页签、
`web/index.html` 的 `TILES` 加入口。

### 改固件模块

直接改 `esp32/*.py`。部署时若板上有 `.mpy`，需重新执行 `esp32/scripts/mpy.py`，
否则 `.py` 不生效（MicroPython 优先加载 `.py`，但板上若只剩 `.mpy` 则不会更新）。

### 改配置项

三处同步：`main.py` 的 `DEFAULT_CFG`（必须有默认值）、`esp32/config.example.json`、`docs/CONFIG.md`。

### 改 API

`main.py` 新增处理函数 + `setup_routes()` 注册，并更新 `README.md` 的 API 表。

### 改文档

`README.md`（功能与用法）、`docs/ARCHITECTURE.md`（机制）、`docs/CONFIG.md`（配置）、
`docs/DEPLOY.md`（部署）、`CHANGELOG.md`（变更条目）。

文档使用陈述式写法：描述机制与规则，不写调试过程、故障复盘、环境细节。

---

## 验证

```bash
# Python 语法
python -m compileall -q esp32/*.py esp32/scripts

# 配置模板有效
python -c "import json; json.load(open('esp32/config.example.json', encoding='utf-8'))"

# 页面构建可重现（www/ 应与 web/ 同步）
python esp32/scripts/build.py && git diff --exit-code -- esp32/www
```

仓库内不含测试代码与硬件测试。涉及运行时行为的改动（HTTP 服务、内存、连接管理）
无法在 CI 中验证，提交说明中应标注「未在真机验证」或注明验证方式。

---

## 提交

- 一个提交只做一类改动：页面、固件、文档分开。
- 改页面时 `web/` 与 `www/` 放在同一提交。
- 提交信息格式：`类型: 简述`，类型取 `feat` / `fix` / `docs` / `chore`。
- 发布新版本时同步更新 `CHANGELOG.md`，并按 `vX.Y.Z` 打标签。
