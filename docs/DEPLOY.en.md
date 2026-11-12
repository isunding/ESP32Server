# Flashing and deployment

Written for Windows, using esptool and mpremote. Both board directories follow the same
procedure; the differences are the firmware variant, the flash offset and the USB serial form.

**English** · [简体中文](DEPLOY.md)

---

## 1. Prerequisites

| Item | ESP32 build (`esp32/`) | S3 build (`esp32-s3/`) |
|---|---|---|
| Board | ESP32-D0WD-V3, 4MB flash | ESP32-S3 N16R8, 16MB flash + 8MB Octal PSRAM |
| Firmware | `ESP32_GENERIC-20250911-v1.26.1.bin` | `ESP32_GENERIC_S3-SPIRAM_OCT-20260824-v1.29.0.bin` |
| Flash offset | `0x1000` | `0x0` |
| Serial | CH9102 / CP2102 | Native USB-Serial/JTAG |
| Download mode | Hold BOOT, tap EN, release BOOT | Hold BOOT, tap RST, release BOOT |

Tools: `pip install esptool mpremote`. The device and the router must be on the same LAN.

Firmware comes from [micropython.org/download](https://micropython.org/download/). The ESP32
build uses the `ESP32_GENERIC` variant; the S3 build uses `ESP32_GENERIC_S3-SPIRAM_OCT` (with
the on-board 8MB Octal PSRAM driver).

---

## 2. Flashing

```bash
# Enter download mode: hold BOOT, tap EN (RST on S3), release BOOT

# ---------- ESP32 ----------
# 1. Back up the whole chip
esptool.py --port COM3 --before no-reset read-flash 0 0x400000 backup-4MB.bin

# 2. Write the firmware
esptool.py --port COM3 --before no-reset --after hard-reset \
  --baud 230400 write-flash --erase-all --compress --flash-size 4MB \
  0x1000 ESP32_GENERIC-20250911-v1.26.1.bin

# ---------- ESP32-S3 ----------
esptool.py --chip esp32s3 --port COM3 erase-flash
esptool.py --chip esp32s3 --port COM3 --baud 460800 --after hard-reset \
  write-flash -z 0x0 ESP32_GENERIC_S3-SPIRAM_OCT-20260824-v1.29.0.bin
```

Recommended baud rates: 230400 for ESP32, 460800 for S3.

The S3 flash offset is `0x0`. On the native USB port `--after hard-reset` has no effect, so
press RST manually when flashing finishes, or run `esptool.py run`.

---

## 3. Flashing notes

### 1. RF calibration data (ESP32 build)

The 4KB at `0xF000` holds factory RF calibration data. `--erase-all` wipes it too; afterwards
`scan()` returns an empty list and joining WiFi reports `201 NO_AP_FOUND`. Restore it by
writing that 4KB back from the full-chip backup taken before flashing.

```bash
mpremote connect COM3 cp phy_init.bin :
mpremote connect COM3 exec "import esp; esp.flash_write(0xf000, open('phy_init.bin','rb').read())"
```

Verification: `esp.flash_read(0xf000, 4)` returns `b'PHYI'`.

The S3 build keeps RF parameters in a separate partition and re-calibrates automatically on
first boot after a full erase.

`scan()` also returns an empty list while AP mode is active. Turn the AP off before
diagnosing WiFi: `network.WLAN(network.AP_IF).active(False)`.

### 2. mpremote connections and resets

`mpremote` sends Ctrl-C when it connects, interrupting the running program, and leaves the
device at the REPL. Reset after the transfer:

```bash
mpremote connect COM3 exec "import machine; machine.reset()"
# or
python esp32/scripts/flash.py --no-upload
```

---

## 4. Deploying code

The commands below use the ESP32 build. For S3, replace the path prefix with `esp32-s3/` and
use the matching serial port; the native USB port exposes different PIDs and port numbers in
run mode and download mode.

### Option A: upload file by file

```bash
mpremote connect COM3 fs mkdir :www

mpremote connect COM3 cp esp32/boot.py esp32/main.py esp32/netmgr.py \
  esp32/httpsrv.py esp32/rap.py esp32/cncal.py esp32/ntpsync.py esp32/ntpsrv.py :

mpremote connect COM3 cp esp32/config.json :

python esp32/scripts/build.py
mpremote connect COM3 cp esp32/www/index.html esp32/www/weather.html :www
```

A single `mpremote` transfer times out beyond about 8 files; use the batching script instead:

```bash
python esp32/scripts/up_batch.py esp32/main.py esp32/www/index.html esp32/www/index.html.gz ...
```

`up_batch.py` uploads 4 files per batch and retries each failure 3 times.

### Option B: precompile to .mpy

```bash
pip install mpy-cross-v6.3
python esp32/scripts/mpy.py            # compile, upload, delete on-device .py, restart
python esp32/scripts/mpy.py --revert   # revert to .py
```

MicroPython prefers `.py`; deploying `.mpy` requires deleting the same-named `.py` on the
device. `.mpy` files are bound to the interpreter version (currently v6.3) and must be
recompiled after a firmware upgrade.

---

## 5. First-time configuration

### Option 1: edit the file

```bash
# edit wifi.ssid / wifi.password / router.host in esp32/config.json
mpremote connect COM3 cp esp32/config.json :
mpremote connect COM3 exec "import machine; machine.reset()"
```

### Option 2: setup AP

When `wifi.ssid` is empty in `config.json`, or the WiFi join fails, the device starts an AP:

| Item | Value |
|---|---|
| SSID | `ESP32-Setup` |
| Password | `esp32setup123` |
| Config page | `http://192.168.4.1` |

Join the AP, open the config page, enter WiFi and router addresses, and the device restarts
on save.

Field reference: [CONFIG.md](CONFIG.md).

---

## 6. Update workflow

On the S3 build the same update can be done wirelessly, see
[section 10](#10-wireless-update-ota-s3-only). The wired path below is still required for the
first deployment, firmware changes and partition changes.

```bash
# 1. page changes
python esp32/scripts/build.py

# 2. core module changes (httpsrv / cncal / rap / netmgr / ntpsrv / ntpsync)
python esp32/scripts/mpy.py

# 3. upload
python esp32/scripts/up_batch.py esp32/www/index.html esp32/www/index.html.gz ...

# 4. reset and self-check
python esp32/scripts/flash.py --no-upload
```

`flash.py` enters the REPL, restarts with Ctrl-D, captures the boot log and calls
`/api/health` to confirm the service state.

### Verification

```bash
curl --noproxy "*" -s -o /dev/null -w "%{http_code} %{time_total}s\n" http://<device-ip>/api/health

for i in $(seq 15); do curl -s -o /dev/null -w "%{http_code} " http://<device-ip>/; done
```

Acceptance: every response code is 200, and `mem_free` from `/api/health` is at least 60KB on
the ESP32 build and at least 7MB on S3.

---

## 7. Rollback

Firmware:

```bash
# ESP32
esptool.py --port COM3 --before no-reset write-flash --erase-all --compress \
  0x1000 backup-4MB.bin

# ESP32-S3
esptool.py --chip esp32s3 --port COM3 write-flash -z 0x0 backup-8MB.bin
```

Code: use a local snapshot directory `backup/ok-YYYYMMDD-HHMMSS/` and upload it over the
current files with the same procedure.

---

## 8. Troubleshooting

| Symptom | Possible cause | Action |
|---|---|---|
| Ping works but HTTP does not | Program interrupted by mpremote, sitting at the REPL | Reset; check the serial console for `>>>` |
| No HTTP but UDP NTP works | lwIP socket pool exhausted | Power off completely for 10 seconds, then power on |
| `scan()` returns an empty list | RF calibration lost / AP mode active | Restore `phy_init`; turn off AP mode |
| Joining WiFi reports `201 NO_AP_FOUND` | RF calibration lost, or SSID case mismatch | Same as above; check the SSID |
| Pages load but show no data | Router CGI not deployed or unreachable | Check `router.host`; call the CGI directly |
| Weather page returns `missing weather.json` | Persisted file is gone | The API retries automatically; restart if it persists |
| No response after opening several pages | Free heap below 20KB | Read `gc.mem_free()` over serial |
| `incompatible .mpy file` | `.mpy` does not match the firmware version | Re-run `scripts/mpy.py` or use `--revert` |
| Upload reports `could not enter raw repl` | Device still booting | Wait 3 seconds and retry |
| S3 produces no output after flashing | Flash offset written as `0x1000` | Reflash at `0x0` |
| S3 does not start after flashing | `--after hard-reset` does not work on native USB | Press RST, or run `esptool.py run` |
| First request after idle is slow | Frequency did not return to full speed | Check `fw.freq` and `fw.pm_downs` in `/api/info`; set `power.on=false` |
| S3 connects but transfers fail | Largest contiguous internal RAM block too small | Check `/api/info`; it should be at least 64KB, power-cycle if below 16KB |

### Serial diagnostics

Open the device serial port at 115200 without asserting DTR/RTS; send Ctrl-C when a traceback
is needed. On Windows use `mpremote connect COM3` or a serial terminal.

---

## 9. Router-side CGI (news / quakes, S3 only)

Data is relayed by the router and the device never reaches the internet directly. Scripts live
in `mirouter/cgi/`; the full inventory and operating rules are in
[mirouter/README.md](../mirouter/README.md). The entries the device uses directly:

| File | Deployed to | Description |
|---|---|---|
| `news` | `/data/iptv/www/cgi-bin/news` | `?a=list` newest page (upstream signature computed locally, cached 5 minutes); `?a=page&last_time=<seconds>` pages backwards without caching |
| `quake` | `/data/iptv/www/cgi-bin/quake` | `?a=list` domestic bulletin (`dizhen.php`), `?a=global` global bulletin (`dizhensu.php`, includes overseas events); separate 30-minute cache, lock and file per source |
| `usgs` | `/data/iptv/www/cgi-bin/usgs` | `?a=list` USGS M2.5+ for the last 24 hours (CSV passed through unchanged), cached 15 minutes; public source, no credentials |
| `oil` | `/data/iptv/www/cgi-bin/oil` | `?a=list` national fuel prices (Anhui by default, `?sheng=` selects a province), JSON passed through, cached 24 hours; requires `Q_ID` / `Q_KEY` in the file header |
| `almanac` | `/data/iptv/www/cgi-bin/almanac` | `?a=list` today's almanac, cached 24 hours; also requires `Q_ID` / `Q_KEY` |

```bash
# must be LF, otherwise #!/bin/sh^M reports not found
scp mirouter/cgi/news mirouter/cgi/quake mirouter/cgi/usgs mirouter/cgi/oil mirouter/cgi/almanac \
  root@<router>:/data/iptv/www/cgi-bin/
ssh root@<router> 'chmod 755 /data/iptv/www/cgi-bin/news /data/iptv/www/cgi-bin/quake /data/iptv/www/cgi-bin/usgs /data/iptv/www/cgi-bin/oil /data/iptv/www/cgi-bin/almanac'

# quake API credentials (mode 600)
ssh root@<router> "printf 'Q_ID=your-id\nQ_KEY=your-key\n' > /data/iptv/etc/quake.conf && \
  chmod 600 /data/iptv/etc/quake.conf"
```

The device connects to uhttpd directly via `router.aux_port` (8088 by default). nginx only
proxies whitelisted `/cgi-bin/xxx` paths, so no nginx change is needed.

Self-check:

```bash
curl --noproxy "*" "http://<router>:8088/cgi-bin/news?a=cache"
curl --noproxy "*" "http://<router>:8088/cgi-bin/quake?a=cache"   # domestic + global cache state
curl --noproxy "*" "http://<router>:8088/cgi-bin/quake?a=list"
curl --noproxy "*" "http://<router>:8088/cgi-bin/quake?a=global"
curl --noproxy "*" "http://<router>:8088/cgi-bin/usgs?a=list"
curl --noproxy "*" "http://<router>:8088/cgi-bin/usgs?a=cache"
curl --noproxy "*" "http://<router>:8088/cgi-bin/oil?a=list"
curl --noproxy "*" "http://<router>:8088/cgi-bin/almanac?a=list"
```

Both scripts accept `?a=flush`; `quake` clears the domestic and global caches together. Both
upstream APIs share the ID/KEY in `/data/iptv/etc/quake.conf`. When the router has no
`sha1sum`, the news script falls back to `openssl dgst`.

---

## 10. Wireless update (OTA, S3 only)

The device exposes upgrade endpoints on `:80`. Files arrive while the firmware keeps running
and the device reboots once at the end, so no serial port is needed and a phone or tablet can
do the update. Only application files are in scope: the runtime modules in the root directory
and the page assets under `www/`. Board data such as `config.json`, `watch.json`,
`weather.json` and the daily news / quake CSVs is not on the allowlist and is never replaced.

| Device path | Source | Notes |
|---|---|---|
| 8 modules such as `main.py`, `httpsrv.py` | `*.py` in the board directory | fixed allowlist; any other file name is rejected |
| `www/*.html` `*.css` `*.js` `*.json` `*.ico` `*.svg` `*.gz` | `www/` in the board directory | includes the `.gz` pre-compressed variants produced by the build |

### Option 1: push from a PC

```bash
python esp32-s3/scripts/ota.py                 # push everything, then reboot
python esp32-s3/scripts/ota.py --dry-run       # list the plan only
python esp32-s3/scripts/ota.py --only www/index.html www/index.html.gz
python esp32-s3/scripts/ota.py --no-reboot     # upload without rebooting
```

The default target is `10.0.0.100`; override it with `--host` or `ESP32_HOST`. The script sends
one file at a time with `PUT /api/ota?path=<path>&crc=<crc32>` and retries each file three
times. The order is `www/` → modules → `boot.py` → `main.py`, so an interrupted run still
leaves a complete boot chain. When every file is in place it calls
`POST /api/ota?a=done`, which records the version and reboots, then polls `/api/health` until
the device is back (60 seconds at most).

### Option 2: upload from the browser

Open `/ota.html` (the "系统升级" link at the bottom of the home page), pick a package and
upload it; press "重启生效" when the progress bar completes. The package is built from the
same file list:

```bash
python esp32-s3/scripts/ota.py --pack dist/ota.mrp
```

The format is `MRP2` and is not compressed:

```
"MRP2" + version length u8 | version | repeated[ name length u8 | name | size u32 | CRC32 u32 | content ]
```

The version string is the current short commit hash (with `-dirty` appended when the working
tree has changes). It travels inside the package, so a browser upload records which build the
device now holds.

The device unpacks while receiving; it never stores the whole package (about 1.2MB) first,
which would double the flash needed.

### Write rules

| Rule | Behaviour |
|---|---|
| Write then swap | Data goes to `<target>.new` and is renamed into place only when the byte count and CRC32 both match; an interrupted transfer leaves a `.new` file while the old file keeps serving |
| Space check | Rejected with HTTP 507 before the transfer starts when free space is below "file size + 64KB" |
| Stale `.gz` removed | Writing `www/x.html` deletes `www/x.html.gz`, because static files prefer the `.gz` copy: updating only the `.html` would keep serving the old page. A full sync re-uploads the matching `.gz` right after |
| Path allowlist | Only the 8 root modules and one level of files under `www/`; `..`, backslashes, absolute paths and other extensions get 400 |
| No authentication | Same as `/api/config`; intended for a local network only |
| No reboot on failure | Any failed file aborts the run without recording a version or rebooting; the device keeps running the previous build |

### Version tracking

| Field | Meaning |
|---|---|
| `ver` | version of the most recent upgrade; empty when nothing was recorded |
| `at` | write time of that upgrade (`time.time()`; 0 while the clock is unset) |
| `files` | number of files written |
| `pending` | `1` means files are replaced but the process still runs the old code; the boot path clears it |
| `hist` | the last 5 upgrades (`ver` / `at` / `files` / `src`, where `src` is `push` or `pack`) |

The version string is an identifier only, never a comparison: uploads do not skip files with a
matching version, and both `--only` and a full package overwrite by content. `config.json` is
outside the upgrade scope, and a missing or corrupt record is treated as "no record".

### Self-check

```bash
curl --noproxy "*" http://<device-ip>/api/ota
# {"ok":1,"ver":"6b0496e","at":1794482315,"files":32,"free":11915264,...}
```
