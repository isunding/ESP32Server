# 使用备忘 · 升级与打包

面向日常操作：改完代码怎么发到板子、升级包放哪、出问题先看哪。
机制细节见 [docs/DEPLOY.md](docs/DEPLOY.md) 第十节，架构见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

默认设备 `10.0.0.100`（S3 板），换设备用 `--host` 或环境变量 `ESP32_HOST`。

---

## 一、日常改动：三步发到设备

```bash
# 1. 改了 web/ 下的页面才需要：重新构建 www/
python esp32-s3/scripts/build.py

# 2. 提交。提交后钩子自动把当前文件打成升级包，落在 ota/
git add -A && git commit -m "feat: ..."

# 3. 推送并重启（任选其一）
ota\OTA升级.exe                      # 双击，选 1
python esp32-s3/scripts/ota.py       # 命令行
```

钩子（`.githooks/post-commit`）每次提交后执行
`python esp32-s3/scripts/pack_ota.py --brief`，生成 `ota/ota-<版本>-<时间>.mrp`
并更新 `ota/latest.mrp`，同时打印一行 `[ota] 升级包已更新：…`。

钩子只在本机生效，新克隆的仓库需要启用一次：

```bash
git config core.hooksPath .githooks
```

---

## 二、升级工具 `ota\OTA升级.exe`

双击运行，菜单操作，不需要 Python。

| 菜单 | 作用 | 等价命令 |
|---|---|---|
| 1 | 推送全部文件并重启 | `python esp32-s3/scripts/ota.py` |
| 2 | 只推送指定文件（不重启） | `python esp32-s3/scripts/ota.py --only www/news.html` |
| 3 | 生成升级包到 `ota/` | `python esp32-s3/scripts/pack_ota.py` |
| 4 | 查看设备状态（版本、内存、最近升级） | `curl http://10.0.0.100/api/ota` |
| 5 | 用升级包推送（整包） | `python esp32-s3/scripts/ota.py --host … --port …`（走 `PUT /api/ota/pack`） |
| 6 | 打开 `ota` 文件夹 | — |

exe 也接受命令行参数，直接转交给 `ota.py`：

```bash
ota\OTA升级.exe --dry-run            # 只列清单
ota\OTA升级.exe --only www/ui.js     # 只推一个文件
ota\OTA升级.exe --host 10.0.0.51
```

`--only` 点名 `.html` 时自动带上同名 `.gz`，不用手动加。
exe 是本地构建产物，不入库；换机器重新生成：

```bash
python -m pip install pyinstaller
python -m PyInstaller --onefile --console --name ota_tool --distpath ota \
  --workpath build/pyi --specpath build/pyi esp32-s3/scripts/ota_menu.py
```

---

## 三、三种升级路径

| 方式 | 适用 | 耗时（32 个文件 1.26 MB） | 入口 |
|---|---|---|---|
| 逐个文件推送 | 日常改一两处，能看清每个文件 | 约 70 秒 | exe 菜单 1/2、`ota.py` |
| 整包推送 | 一次换很多文件，或手上只有 `.mrp` | 约 60 秒 | exe 菜单 5、`PUT /api/ota/pack` |
| 网页上传 | 没有电脑，用手机/平板 | 约 60 秒 + 手动重启 | 设备 `/ota.html`，首页页脚「系统升级」 |

三者共用同一套设备端接口，写入规则一致：先写 `<文件>.new`，字节数与 CRC32 都对上
才顶替正式文件；任一项失败即中止，不重启，设备继续跑旧版本。

**网页上传**：`ota/latest.mrp` 拷到手机（或直接选电脑上的文件），在 `/ota.html`
点「选择升级包」→「开始升级」→ 进度条走完点「重启生效」。版本在重启后才算生效，
重启前升级页会标「待重启」。

---

## 四、首次引导（只在新板子或刷固件后需要）

老固件没有 `/api/ota`，HTTP 推不进去，必须先用串口把 `main.py` 与 `httpsrv.py` 送上去：

```bash
python -m mpremote connect COM31 fs cp esp32-s3/httpsrv.py :httpsrv.py
python -m mpremote connect COM31 fs cp esp32-s3/main.py :main.py
set ESP32_PORT=COM31
set ESP32_HOST=10.0.0.100
python esp32-s3/scripts/flash.py --no-upload      # 复位 + 自检
```

`mpremote` 一连接就会打断板子上的程序，传完必须复位，否则设备不应答 HTTP。
之后所有更新都可以走无线。

---

## 五、版本、包与回滚

- 包名 `ota-<版本>-<YYYYMMDD-HHMM>.mrp`，版本取 git 短哈希，工作区有改动时带 `-d`
- `ota/latest.mrp` 始终是最近一次生成的那份，网页上传直接选它
- `ota/latest.json` 记录版本、文件数、大小、生成时间，以及「构建是否落后」（`stale`）
- 默认保留最近 20 个包，更早的自动删除（`pack_ota.py --keep N` 可调）

**回滚**：在 exe 菜单 5 里选一个旧包推送，重启即可。设备端只比内容不比版本号，
旧包里的文件会如实覆盖回去。

**查当前版本**：设备升级页，或 `curl http://10.0.0.100/api/ota`。
`pending: 1` 表示文件已换、还没重启。

---

## 六、常见问题

| 现象 | 原因与处理 |
|---|---|
| 推完页面没变化 | 浏览器里是内存中的旧页面，刷新一次；`.gz` 缓存已由设备在写入 `.html` 时一并清掉 |
| 推送立刻失败，提示 `Connection refused` | 设备在 REPL 里（刚用过 mpremote）或正在重启，等 15 秒或执行 `flash.py --no-upload` |
| 传到一半断了 | 旧文件完好，重新推一次即可；`.new` 残留会被下次覆盖 |
| 升级后版本号没变 | 看升级页是否标「待重启」，重启后 `pending` 归零、版本才算生效 |
| 提示「空间不足」 | 设备剩余空间小于「文件大小 + 64KB」，先清理或换小包 |
| exe 双击闪退 | 看是否被杀软拦截；也可改用 `python esp32-s3/scripts/ota.py` |

---

## 七、约束

- 涉及真机的动作用户确认后再做：上传文件、刷写、重启
- 升级接口无鉴权，仅限局域网使用；`config.json` 不在升级范围内，不会被覆盖
- 升级只覆盖根目录 8 个运行模块与 `www/` 下的页面资源；`.mpy`、CSV、`ota.json` 不动
- `ota/*.mrp`、`ota/*.exe`、`ota/latest.json` 与 `build/pyi/` 不入库
- 凭据（路由器口令、API KEY、WiFi 密码）不写入仓库
