# Configuration

**English** · [简体中文](CONFIG.md)

## 1. The config file

The on-device config file is `/config.json` (at the root of the filesystem). The repository
ships a sanitized template per board directory; fields are identical, the `led` defaults
differ:

```bash
cp esp32/config.example.json    esp32/config.json       # ESP32
cp esp32-s3/config.example.json esp32-s3/config.json    # ESP32-S3
```

`config.json` is listed in `.gitignore`.

### Ways to change it

| Method | Action | When to use |
|---|---|---|
| File | Edit `config.json`, then soft reset (Ctrl-D) | First deployment |
| Web | Edit on the page, `POST /api/config`, the device restarts itself | Day-to-day changes |
| Serial | Edit the file with `mpremote`, then reset | No network available |

`GET /api/config` masks passwords as `********`. Submitting `********` in a `POST` is treated
as "unchanged" and keeps the current value.

---

## 2. Fields

```jsonc
{
  "device_name": "Living room terminal",   // display name

  "wifi": {
    "ssid": "YOUR_WIFI_SSID",       // empty = start the setup AP
    "password": "YOUR_WIFI_PASSWORD",
    "hostname": "server",           // DHCP hostname
    "ap_ssid": "ESP32-Setup",       // setup AP name
    "ap_pass": "esp32setup123"      // setup AP password, 8+ characters
  },

  "router": {
    "host": "192.168.1.1",          // data source router
    "port": 80,
    "r2h_port": 5140,               // rtp2httpd port
    "udpxy_port": 12123,            // udpxy port
    "aux_port": 8088                // router uhttpd port (news/quake CGI)
  },

  "ntp": {
    "upstream": ["ntp.aliyun.com", "ntp.tencent.com", "cn.pool.ntp.org"],
    "sync_ms": 1800000,             // sync interval, 30 minutes
    "tz": 8                         // time zone in hours
  },

  "poll": {
    "weather_ms": 600000,           // weather cache TTL
    "weatherhist_ms": 43200000,     // 7-day history cache TTL (12 hours)
    "channels_ms": 300000,          // channel library cache TTL
    "news_ms": 300000,              // news feed TTL (S3 only)
    "quake_ms": 10800000            // quake TTL (3 hours, S3 only)
  },

  "led": {
    "on": 1,                        // 0 turns the LED off once ready
    "period_ms": 2000,
    "pulse_ms": 70
  },

  "power": {
    "on": true,                     // idle frequency scaling
    "max_mhz": 240,                 // full speed restored on activity
    "idle_mhz": 80,                 // frequency when idle
    "idle_after_s": 20              // seconds without requests before counting as idle
  },

  "quote": {
    "on": true,
    "ttl_ms": 600000,
    "items": [
      { "code": "sh000001", "name": "SSE Composite", "kind": "index" }
    ]
  }
}
```

---

## 3. Field reference

### `wifi`

When `ssid` is empty the device starts a setup AP (`ap_ssid` / `ap_pass`) after boot. Connect
to that AP and open the config page to enter WiFi and router settings.

`hostname` is reported over DHCP; on routers with local DNS the device is reachable at
`http://server/`. Use the bare hostname when `.lan` / `.local` suffixes do not resolve.

### `router`

Data source address. The device requests these CGI:

| Path | Purpose |
|---|---|
| `/cgi-bin/sysstat?a=stat` `?a=dhcp` `?a=arp` | Device status, leases, ARP |
| `/cgi-bin/netperip` | Per-device traffic |
| `/cgi-bin/weather?a=data` | Aggregated weather |
| `/cgi-bin/weatherhist?a=history` | Last 7 days of weather |
| `/cgi-bin/iptv?a=list` | Channel library |
| `/cgi-bin/svcstat?port=<port>` | Service port status |
| `/cgi-bin/ntpctl?a=status` | Time sync status |
| `/cgi-bin/news` `quake` `usgs` `oil` `almanac` | News, quakes, fuel prices, almanac (S3 only) |

The scripts live in `mirouter/cgi/`; inventory and deployment are in
[mirouter/README.md](../mirouter/README.md). A missing endpoint only empties the matching
page; everything else keeps working.

### `ntp`

The device also runs an NTP server on UDP 123, with `upstream` as its public time sources.

`tz` is in hours and accepts fractions: `8` is UTC+8, `5.5` is UTC+5:30, `-5` is UTC-5.

### `poll`

Cache lifetimes. Except for the news catch-up fetch, data is refetched when the next request
finds it expired.

`weather_ms` is the on-device cache lifetime. The router CGI has its own cache; a manual
refresh (`/api/weather?force=1`) clears both layers, the fetch runs in the main loop and the
page re-renders once the data timestamp changes.

`news_ms` / `quake_ms` / `usgs_ms` apply to the S3 build only. Fetched data is appended to
per-day CSVs on the device (`news-YYYYMMDD.csv`, `quake-YYYYMMDD.csv`, `quakeg-YYYYMMDD.csv`,
`usgs-YYYYMMDD.csv`) and the last 7 days are kept. `usgs_ms` defaults to 30 minutes, matching
the router's 15-minute cache.

`oil_ms` / `almanac_ms` are the on-device caches for fuel prices and the almanac. Both change
once a day, default to 24 hours and match the router's 24-hour cache; the data lives in
memory only, so after a restart the router cache answers without another upstream call.

News is collected on a schedule set by `news_tick_ms` (1 hour by default). The main loop sets
a refresh flag, so the request path is never involved. Each run starts from the newest page
and uses the oldest item's timestamp as `last_time` for the next one, walking back until it
catches up with the previous collection point or reaches `news_pages` (6 by default). Paging
requests go through `?a=page&last_time=<seconds>` on the router CGI and bypass the front-page
cache. Raising `news_pages` covers a longer gap at the cost of a longer fetch.

### `router.aux_port`

News and quake CGI are reached directly on the router's uhttpd port (8088 by default). nginx
only proxies whitelisted `/cgi-bin/xxx` paths, so no nginx change is needed.

### `led`

The on-board indicator. With `on: 1` it lights for `pulse_ms` every `period_ms`; with `on: 0`
it is switched off once at boot and never driven again.

The S3 build uses a single WS2812 (GPIO48), the ESP32 build a plain single-color pin, told
apart by `kind`:

```json
"led": { "on": 1, "kind": "np", "pin": 48, "color": [0, 10, 0],
         "period_ms": 2000, "pulse_ms": 60 }
```

`kind: "np"` drives neopixel (RMT); `kind: "gpio"` drives a plain pin level. `color` is the
WS2812 RGB value, low values give a dim heartbeat; the smaller `pulse_ms` is relative to
`period_ms`, the lower the duty cycle.

The waveform icon next to the IP in the top bar toggles the heartbeat live: a click writes
`led.on` and takes effect without a restart, and the state is kept in the config file. When
`led_hw` from `/api/health` is 0 (no controllable LED) the icon is hidden.

The on-board RX / TX LEDs are driven directly by the RXD / TXD levels of the USB-UART bridge,
have no connection to the ESP32 GPIOs and are not affected by this setting.

### `power`

Idle frequency scaling reduces power draw and chip temperature while idle.

- After `idle_after_s` seconds without any HTTP request -> `machine.freq(idle_mhz)`
- Back to `max_mhz` when a new connection is accepted
- `idle_mhz` floor is 80: WiFi needs an 80MHz APB clock and Octal PSRAM timing is built
  around it; lower values are ignored and treated as "disabled"
- `max_mhz` comes from config rather than `machine.freq()`: `esp_pm` clock configuration does
  not survive a soft reset
- On boards that cannot change frequency, the first failed switch disables the feature and
  logs it

Activity is measured by the HTTP server's accept counter; NTP and TCP handshakes do not count.

### `quote`

Ticker symbols for the market page, using Sina quote codes:

- Indices: `sh000001` (SSE), `sz399001` (SZSE), `sz399006` (ChiNext)
- FX: `fx_susdcny`, `fx_seurcny`, `fx_shkdcny`
- Stocks: `sh600519`, `sz000001`

`kind` selects the page section: `index` for indices, `fx` for FX. All symbols are fetched in
one `/api/quote` request.

---

## 4. Weather data and authentication

The ESP32 side holds no weather credentials. Weather data comes from the router CGI:

| Item | Location |
|---|---|
| Authentication | Router CGI (JWT / API key) |
| Keys and credentials | Stored on the router |
| Token renewal | Router scheduled job |

The device only requests `/cgi-bin/weather?a=data` and receives aggregated JSON.

Key points:

- No API key goes into `config.json`
- Switching weather providers only requires changing the router CGI
- No weather credentials are stored on the device

### Calling the weather API directly without the router CGI

You have to implement the handler yourself. Keep the key in a separate file listed in
`.gitignore`; never put it in source or `config.json`.

The MCU has no secure storage: credentials written to flash can be read with physical access
to the device.

---

## 5. Security

| Item | Recommendation |
|---|---|
| WiFi password | Stored in plain text on flash; do not deploy on an untrusted network segment |
| Setup AP password | Defaults to `esp32setup123`; change it after initial setup |
| Transport | No HTTPS; never expose to the internet |
| Config API | No authentication, LAN use only |
| Third-party keys | Kept on the router, never on the MCU |

The service is meant for LAN use; publishing it requires a reverse proxy with authentication.

---

## 6. Minimal configuration

The smallest working config (device info, lunar calendar, NTP):

```json
{
  "wifi": {
    "ssid": "your-wifi",
    "password": "your-password"
  }
}
```

Every other field falls back to `DEFAULT_CFG`; the weather and IPTV pages show no data.
