# ota/ · 升级包投放目录

每次提交后由 `.githooks/post-commit` 自动生成，内容是最新一次提交的板端文件
（`.py` 与 `www/`，共 32 个）。**包与 exe 不入库**，只在本机存在。

| 文件 | 说明 |
|---|---|
| `ota-<版本>-<YYYYMMDD-HHMM>.mrp` | 升级包，格式 `MRP2`，约 1.26 MB；默认保留最近 20 个 |
| `latest.mrp` | 最近一次生成的包，网页上传时选它 |
| `latest.json` | 版本、文件数、大小、生成时间；`stale` 表示 `web/` 比 `www/` 新 |
| `OTA升级.exe` | 双击运行的升级工具（本机构建，换机器需重新生成） |

操作说明见 [../memory.md](../memory.md)。

手动重新生成：

```bash
python esp32-s3/scripts/pack_ota.py
```
