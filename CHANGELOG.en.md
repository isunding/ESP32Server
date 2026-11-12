# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/); entries are grouped as
`Added`, `Changed`, `Fixed`, `Removed` and `Security`.

**English** · [简体中文](CHANGELOG.md)

## Versions

| Version | Date | Summary |
|---|---|---|
| v1.1.0 | 2026-11-11 | ESP32-S3 build, news / quake / fuel price / almanac pages; router components consolidated into `mirouter/` |
| v1.0.0 | 2026-11-08 | First release: ESP32 build with 7 pages, on-board NTP server, IPTV |

---

## 2026-11-12

### Added

- Wireless update (OTA, S3 only): `/ota.html` page, `GET|PUT|POST /api/ota` endpoints and
  `scripts/ota.py` (per-file push plus `--pack` to build a package). Writes go to `.new` and
  are swapped in only after a CRC32 check; a failed run never reboots
- Upgrade tooling: `scripts/ota_menu.py` provides a menu-driven console, packaged as
  `ota/OTA升级.exe`; `scripts/pack_ota.py` writes packages into `ota/` and runs automatically
  from `.githooks/post-commit` after every commit
- Upgrade record: `ota.json` keeps the version string, time, file count, a pending-reboot flag
  and the last 5 entries; the version travels inside the package and the flag clears on boot.
  Writing `x.html` also deletes the sibling `x.html.gz` so browsers cannot keep getting the old page
- Streaming request body in `httpsrv` (`route_stream()`), so uploads no longer load a whole
  file into memory
- Light/dark toggle in the top bar: follows the system until tapped, then locks the choice in
  the browser. An inline bootstrap script in `<head>` sets `data-theme` on `<html>` first, so
  the page never paints dark before switching to light
- Back-to-top button in the bottom-right corner of the news and quake pages; it appears once
  you scroll past one screen and smooth-scrolls back to the top

---

## 2026-11-11

### Changed

- Added `README.en.md` plus English versions of `ARCHITECTURE`, `CONFIG`, `DEPLOY` and
  `CHANGELOG` (`*.en.md`), each pair linked at the top
- `docs/ARCHITECTURE.md`: updated main loop and router-side component description, added a
  "new router CGI" procedure
- Router-side components consolidated into `mirouter/`: data and status CGI, IPTV start/stop
  and playlist scripts, console pages, stock admin page injection, udpxy and rtp2httpd
  binaries, deploy script `tools/deploy.py`; the former `router/cgi/` moved to `mirouter/cgi/`
- Added `mirouter/README.md` covering the component inventory, ports, deploy locations,
  operating rules and credential locations
- `docs/DEPLOY.md`: router chapter now points at `mirouter/`

### Security

- Committed router components are sanitized: API ID/KEY left empty, QWeather host and region
  replaced with placeholders; no SSH or admin passwords, host keys, broadband account,
  device MACs or channel library

---

## 2026-11-09

### Added

- Anhui fuel prices on the market page: 92 / 95 / 98 petrol and 0 diesel with adjustment date
  and change; a router CGI relays upstream and caches for 24 hours, the device caches daily too
- Almanac on the first NTP card: dos and don'ts shown per day under the lunar row (colored
  tags on a tinted background), cached daily; card layout, clock size and status pill unchanged
- 7-day bar chart on the quake page, bucketed by USGS magnitude: M3 / M4 / M5 / M6+ layers per
  bar, colors from the USGS scale (`#83d0da` / `#7bc87f` / `#f9f518` / `#fa8a11`), events below
  M3 excluded from bar height and counts, with a color legend. Stats travel with the
  `/api/quake` response (`stat`: daily counts plus M3 / M4 / M5 / M6 totals)
- Scheduled news catch-up: `poll.news_tick_ms` defaults to hourly, the main loop sets a refresh
  flag; pages back via upstream `last_time` until the previous collection point is covered,
  `poll.news_pages` caps the page count
- News collection progress recovered from the last few days of CSV after a restart
- Router news CGI gained `?a=page&last_time=<seconds>`; paging requests bypass the front-page cache
- USGS source on the quake page: `/api/quake?a=usgs` (M2.5+, last 24 hours); a new `usgs` router
  CGI relays the USGS CSV, cached 15 minutes
- LED heartbeat toggle to the right of the IP in the top bar: `POST /api/led` writes `led.on`
  and takes effect without a restart; `/api/health` gained `led_on` / `led_hw`

### Changed

- Quake cards use a dedicated style: solid magnitude block on the left, location, depth and
  coordinates on the right; the news feed style is no longer reused, `.fmag` / `.fsum` removed
  from the theme
- IPTV page title changed to "IPTV 电视直播" with a "直播" nav button; NTP page title changed to
  "NTP 时间校准服务" with a "校时" nav button
- News page loads in segments: 30 items on first paint, scrolling to the bottom pages backwards
  via the `before` cursor; the API gained `?limit=` and `?before=`, default still the latest 120
- Pull-to-refresh and the 5-minute timer on the news page only read the on-device cache; only a
  manual refresh triggers an upstream fetch
- News cards show the full body directly; the three-line excerpt and tap-to-expand are gone
- Persisted news body length relaxed
- On-board WS2812 on S3 switched to a low-brightness heartbeat; RX / TX LEDs stay hardware-driven
- Config template gained `poll.news_ms`, `poll.news_tick_ms`, `poll.news_pages`

### Fixed

- Bar count labels on the quake chart were not centered: numbers sat at the left edge of the
  column, offset from both bar and date
- Quake stats were grouped by file date, so cross-day records from one fetch piled onto the
  fetch date; now grouped by event time, with anything outside the 7-day window excluded
- The market batch API stopped reading based on an estimated byte count, truncating the last
  symbol when the watchlist was large, so that symbol never got a quote
- News and quake day files were written under the wrong date before the clock synced, and the
  following prune deleted real history; fetching and pruning now validate the system year first
- Stock names on news tags carried the exchange prefix, so watchlist entries showed `sz` + name
- News and quake APIs returned the oldest slice of the day files; they now return the newest
  (120 items for news)

---

## 2026-11-08

### Added

- ESP32-S3 N16R8 board build `esp32-s3/`: Python heap in 8MB PSRAM, native USB serial, WS2812
  indicator, modem sleep disabled for WiFi
- Idle frequency scaling (`power`): drops to `idle_mhz` after `idle_after_s` seconds without
  requests, restores `max_mhz` when a new connection arrives
- Sleep history and timeline: the `pm` field of `/api/info` plus a device page card
- News page `/news.html` (S3 only): importance and topic filters, bold `【】` titles, stock tags
  to add/remove watchlist entries
- Quake page `/quake.html` (S3 only): domestic / global switching, magnitude filter, hemisphere
  labels on coordinates
- `/api/news`, `/api/quake`, `/api/quake?a=global`: incremental per-day CSV, 7-day retention;
  cold start returns `pending` and the main loop fetches in the background
- `/api/watch?a=codes`: returns watchlist codes without triggering a quote request
- Router `router/cgi/news`, `router/cgi/quake`: signing and caching proxy, supporting
  `?a=global`, `?a=cache`, `?a=flush`
- `/api/info`: chip temperature, reset reason, internal RAM blocks, WiFi details
- Weather page: 24-hour temperature / precipitation curves, 7-day high-low range, 24-hour AQI,
  past 7 days curve, 7-day list with range bars and daily detail
- `/api/weatherhist`: past 7 days, persisted with a 12-hour cache
- Top-bar heartbeat: refreshes `/api/health` every 15 seconds, shows the status dot and device IP
- News and quake pages converted to cards: one module per record, header carries time and
  importance or magnitude, body carries text or epicenter; the module style is shared by both
  pages from `theme.css`
- `/api/weather?force=1`: clears both the router CGI and on-device caches, the fetch runs in the
  main loop background
- `/api/watch`: watchlist entries without a quote return a placeholder row marked "quote
  unavailable" and can still be removed
- `rap.py`: download read timeout is configurable
- Project documentation and MIT license

### Changed

- Config template gained `led`, `tz`, `poll.weatherhist_ms`, `router.aux_port`, `poll.news_ms`,
  `poll.quake_ms`; removed `tz_min`, `first_source`
- In-page toolbar styles moved into `theme.css`
- Weather alert source switched to `warning.alerts`
- Home and top-bar order: weather, quake / news, market / IPTV, radio / device, NTP
- Page titles and copy unified on "新闻"
- News and quake lists became single-column, card width matching the content area; the home
  directory keeps two columns on wide screens and one on phones
- News cards: time and importance on the title row above the card, body inside, topic and stock
  tags below

### Fixed

- Weather page top bar and navigation rendering
- Top-bar status and device IP refresh across pages
- Market page refresh button alignment
- Weather alert parsing: multiple response shapes, level casing and UTC conversion
- Empty-result handling in the CI directory check
- Watchlist entries invisible when the quote source returned nothing
- Weather refresh and router data source synchronization

### Security

- `config.json`, private keys, firmware images, router-side implementation and local
  maintenance scripts are not committed
- Third-party API credentials live only in `/data/iptv/etc/quake.conf` on the router (mode 600);
  the repository carries placeholders

---

## 2026-11-07

### Removed

- Calendar page `/calendar.html` and `/api/calmonth`; the lunar calendar stays on the home and
  NTP pages
- Legacy `traffic.html`

### Changed

- Radio categories adjusted to "East China · Jiangsu / Zhejiang / Shanghai / Anhui" and
  "Anhui · Hefei", 67 stations in 9 categories
- Home card and top-bar entry order

### Fixed

- HTTP unresponsive caused by lwIP socket pool exhaustion

---

## 2026-11-06

### Removed

- `/status.html`, `/traffic.html` and their APIs

### Added

- `/info.html` device page, radio page
- Build, precompile and batch upload scripts

### Changed

- `listen(3)`, wait for the client to close after a response, inline static assets, home data
  merged into `/api/home`

### Fixed

- HTTP unresponsive after long uptime
- Connection release when `select` raises

---

## 2026-11-05

### Changed

- Project name unified as ESP32 Terminal Server
- Pre-connection timeout reduced to 1 second

### Fixed

- Pre-connections filling the listen backlog

---

## 2026-11-04

### Added

- Lunar date, 24 solar terms, festivals and annual table cache
- NTP time zone selection
- Alert events recorded to CSV

### Removed

- Ticking clock on the home page

### Fixed

- Event history display

---

## 2026-11-03

### Added

- Inline player on the IPTV page, weather card on the home page, on-board NTP server, LED boot
  indication

---

## 2026-11-01

### Added

- ESP32 + MicroPython terminal server: home, weather, status, NTP and IPTV pages plus the theme
  system

### Security

- RF calibration data preserved during a full flash
