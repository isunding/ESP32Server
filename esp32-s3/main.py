# -*- coding: utf-8 -*-
"""ESP32 终端服务器 — 主程序

定位：路由器 CGI 的消费者 + 展示终端。不做组播转发，视频流由
浏览器直连路由器 5140 端口，MCU 只出页面和 API。

功能页：天气 / 行情 / 状态 / 流量 / NTP / IPTV（无告警）
"""

import gc
import json
import time
import machine

try:
    from machine import Pin
except ImportError:      # PC 上的单测桩没有 Pin
    Pin = None

import netmgr
import httpsrv
import rap
import ntpsrv
import ntpsync
import cncal

# 导入完成后的剩余堆：这是判断「模块占了多少 RAM」最直接的数字。
# 用 .mpy 部署时这一行会比 .py 明显高（省掉 lexer/parser 的临时分配）。
# PC 上的单测桩没有 gc.mem_free()，所以要包一层。
try:
    gc.collect()
    print("[mem] after imports free=", gc.mem_free())
except Exception:
    pass

CONFIG_PATH = "config.json"
BOOT_MS = time.ticks_ms()

# 板载指示灯。S3 DevKit 是单颗 WS2812（GPIO48），旧板是普通 GPIO 接的单色灯。
# kind="np" 用 neopixel 驱动，kind="gpio" 用引脚电平。
# on=0 时上电立刻写黑、全程不驱动 —— 这就是彻底关灯的开关。
# 心跳非阻塞（主循环用 ticks 判断，不 sleep），否则会拖慢 HTTP 响应。
LED_PIN = 48
LED_KIND = "np"
LED_ON = 1                   # gpio 模式的点亮电平
HB_PERIOD_MS = 2000          # 心跳周期
HB_PULSE_MS = 70             # 每次点亮时长

_led = None
_np = None
_led_rgb = (0, 127, 0)       # np 模式的心跳颜色
_led_ts = 0
_led_beat = False
_hb_enable = False           # 默认不闪，由 config 的 led.on 打开
_hb_period = HB_PERIOD_MS
_hb_pulse = HB_PULSE_MS

try:
    if LED_KIND == "np":
        import neopixel
        _np = neopixel.NeoPixel(Pin(LED_PIN), 1)
        _np[0] = (0, 0, 0)
        _np.write()          # 上电即熄灭
    else:
        _led = Pin(LED_PIN, Pin.OUT, value=1 - LED_ON)
except Exception:
    _np = None
    _led = None


def _led_set(on):
    """点亮/熄灭指示灯：on 为真用心跳颜色，为假全黑。"""
    if _np is not None:
        try:
            _np[0] = _led_rgb if on else (0, 0, 0)
            _np.write()
        except Exception:
            pass
    elif _led is not None:
        try:
            _led.value(LED_ON if on else 1 - LED_ON)
        except Exception:
            pass


def led_off():
    _led_set(0)


def led_config(cfg):
    """led 段：on / kind / pin / color / period_ms / pulse_ms。"""
    global _hb_enable, _hb_period, _hb_pulse, _led_rgb
    c = cfg.get("led") or {}
    _hb_enable = bool(c.get("on", 0))
    _hb_period = int(c.get("period_ms", HB_PERIOD_MS))
    _hb_pulse = int(c.get("pulse_ms", HB_PULSE_MS))
    col = c.get("color")
    if isinstance(col, (list, tuple)) and len(col) == 3:
        _led_rgb = (int(col[0]), int(col[1]), int(col[2]))
    if _hb_period < 200:
        _hb_period = 200
    if _hb_pulse >= _hb_period:
        _hb_pulse = _hb_period // 2
    if not _hb_enable:
        led_off()


def led_heartbeat():
    """非阻塞心跳，主循环每轮调一次即可。"""
    global _led_ts, _led_beat
    if (_np is None and _led is None) or not _hb_enable:
        return
    now = time.ticks_ms()
    if _led_beat:
        if time.ticks_diff(now, _led_ts) >= _hb_pulse:
            led_off()
            _led_beat = False
            _led_ts = now
    elif time.ticks_diff(now, _led_ts) >= _hb_period:
        _led_set(1)
        _led_beat = True
        _led_ts = now

DEFAULT_CFG = {
    "device_name": "",
    "wifi": {
        "ssid": "",
        "password": "",
        "hostname": "server",
        "ap_ssid": "ESP32-Setup",
        "ap_pass": "esp32setup123",
    },
    "router": {"host": "10.0.0.1", "port": 80, "r2h_port": 5140, "udpxy_port": 12123},
    "ntp": {
        "upstream": ["ntp.aliyun.com", "ntp.tencent.com", "cn.pool.ntp.org"],
        "sync_ms": 1800000,   # 30 分钟同步一次
        "tz": 8,              # 时区小时数，+8 = 北京时间
    },
    # 只留「缓存 TTL」：数据多久算过期。没有任何周期性后台采集了。
    "poll": {"weather_ms": 600000, "weatherhist_ms": 43200000,
             "channels_ms": 300000},
    # 板载蓝灯：启动时常亮，就绪后按此节奏心跳（0 = 关闭，就绪后熄灭）
    "led": {"on": 0, "period_ms": 2000, "pulse_ms": 70},
    # 空闲降频：连续 idle_after_s 秒没有任何 HTTP 请求就把 CPU 降到 idle_mhz，
    # 下一个请求一到立刻回满频。80MHz 是硬下限——WiFi 和 Octal PSRAM 都要求
    # 时钟不低于它，再低就不是慢而是断网/崩堆了。
    "power": {"on": True, "idle_mhz": 80, "idle_after_s": 20},
    # 联网小功能：行情页（指数 + 汇率）。默认「打开行情页且缓存过期」才联网一次，
    # 后台不轮询，所以对路由器和板子都几乎没有负担。
    # 一次 /list=a,b,c 批量取，6 个标的只花一次 TCP 往返（实测 72ms / 约 1.2KB）。
    # 想加减标的就往 items 里塞：kind 只影响页面分组（index / fx）。
    "quote": {
        "on": True,
        "ttl_ms": 600000,
        "items": [
            {"code": "sh000001", "name": "上证指数", "kind": "index"},
            {"code": "sz399001", "name": "深证成指", "kind": "index"},
            {"code": "sz399006", "name": "创业板指", "kind": "index"},
            {"code": "fx_susdcny", "name": "美元 / 人民币", "kind": "fx"},
            {"code": "fx_seurcny", "name": "欧元 / 人民币", "kind": "fx"},
            {"code": "fx_shkdcny", "name": "港元 / 人民币", "kind": "fx"},
        ],
    },
}

P_STATUS = "/cgi-bin/sysstat?a=stat"
P_DHCP = "/cgi-bin/sysstat?a=dhcp"
P_ARP = "/cgi-bin/sysstat?a=arp"
P_CLIENTS = "/cgi-bin/netperip"
P_WEATHER = "/cgi-bin/weather?a=data"
P_WHIST = "/cgi-bin/weatherhist?a=history"
P_CHANNELS = "/cgi-bin/iptv?a=list"
P_SVC = "/cgi-bin/svcstat?port=%d"
P_NTP = "/cgi-bin/ntpctl?a=status"

WEATHER_FILE = "weather.json"   # 天气太大，落盘后由 HTTP 层流式吐出
WHIST_FILE = "whist.json"        # 过去 7 天历史（约 34KB），同样落盘
CHANNELS_FILE = "channels.json"  # 频道库 18KB，同样落盘，避免 24KB 连续堆
WATCH_FILE = "watch.json"        # 自选股列表（几十字节）
WATCH_TTL = 600000               # 自选行情缓存 10 分钟，与行情页一致
WATCH_MAX = 20
DEFAULT_WATCH = [
    {"code": "sh600519", "name": "贵州茅台"},
    {"code": "sz000858", "name": "五粮液"},
    {"code": "sh601318", "name": "中国平安"},
    {"code": "sz300750", "name": "宁德时代"},
    {"code": "sh600036", "name": "招商银行"},
]


def load_watch():
    """自选股列表。文件坏了就当没配过，用默认几支，绝不让页面 500。"""
    try:
        with open(WATCH_FILE) as f:
            v = json.load(f)
        if isinstance(v, list) and v:
            return [{"code": str(x["code"]), "name": str(x.get("name", ""))}
                    for x in v if x.get("code")]
    except Exception:
        pass
    return [dict(x) for x in DEFAULT_WATCH]


def save_watch(items):
    tmp = WATCH_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(items, f)
        import os
        try:
            os.rename(tmp, WATCH_FILE)
        except OSError:
            try:
                os.remove(WATCH_FILE)
            except Exception:
                pass
            os.rename(tmp, WATCH_FILE)
        return True
    except Exception as e:
        print("[watch] save fail", e)
        return False


def q(s):
    """最小 URL 编码（MicroPython 的 urllib 在不同固件上可用性不一致）。"""
    out = ""
    for ch in s:
        if ch.isalnum() or ch in "-._~":
            out += ch
        else:
            out += "%%%02X" % ord(ch)
    return out


# ---------------------------------------------------------------- 配置
def _merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def load_cfg():
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    try:
        with open(CONFIG_PATH) as f:
            _merge(cfg, json.load(f))
    except Exception as e:
        print("[cfg] 未找到或损坏，使用默认值:", e)
    return cfg


def save_cfg(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f)
    import os
    try:
        os.rename(tmp, CONFIG_PATH)
    except Exception:
        os.remove(CONFIG_PATH)
        os.rename(tmp, CONFIG_PATH)


# ---------------------------------------------------------------- 工具
def find_num(raw, key):
    """在原始 JSON 文本里抓第一个 "key":<数值> 的整数部分。

    刻意不用 json.loads：天气聚合体 30~40KB，整包解析在 ESP32 上会
    把堆打散甚至 MemoryError。这里只做定长扫描，稳定且几乎零分配。

    兼容两种写法：`"aqi":"62"`（v7 天气风格）与 `"aqi": 62`
    （v1 空气质量接口是数值型）。
    """
    pat = ('"%s":' % key).encode()
    i = raw.find(pat)
    if i < 0:
        return None
    n = len(raw)
    k = i + len(pat)
    while k < n and raw[k] in b" \t":
        k += 1
    if k < n and raw[k] == 34:      # 双引号
        k += 1
    j = k
    while j < n and raw[j] in b"-0123456789.":
        j += 1
    if j == k:
        return None
    try:
        return int(float(raw[k:j]))
    except Exception:
        return None



def find_num_file(path, key, block=1024, overlap=48):
    """在文件里流式查找 "key":<数值>，只占用 block+overlap 字节内存。

    天气 JSON 落在 flash 上，不整个读进内存也能取到 temp / aqi 等数值。
    """
    pat = ('"%s":' % key).encode()
    carry = b""
    try:
        f = open(path, "rb")
    except Exception:
        return None
    try:
        while True:
            buf = f.read(block)
            if not buf:
                return None
            data = carry + buf
            i = data.find(pat)
            if i >= 0:
                k = i + len(pat)
                while k < len(data) and data[k] in b" \t":
                    k += 1
                if k < len(data) and data[k] == 34:
                    k += 1
                j = k
                while j < len(data) and data[j] in b"-0123456789.":
                    j += 1
                if j > k:
                    try:
                        return int(float(data[k:j]))
                    except Exception:
                        pass
                elif j >= len(data):
                    # 数字被切在块边界，扩大重叠后再试一次
                    carry = data[i:]
                    continue
            carry = data[-overlap:]
    finally:
        try:
            f.close()
        except Exception:
            pass

def rate_of(prev, cur, dt_ms):
    """字节计数器差分 → bytes/s"""
    if not prev or dt_ms <= 0:
        return 0
    d = cur - prev
    if d < 0:
        return 0
    return int(d * 1000 / dt_ms)


def local_ymd():
    """RTC 里的本地日期（NTP 同步时已把时区偏移算进去）。

    还没同步过时间时 RTC 停在 2000 年附近，这种年份直接当无效，
    否则农历会按 2000 年的表去查，显示出的干支/节气全是错的。
    """
    try:
        t = time.localtime()
    except Exception:
        return (0, 0, 0)
    y, m, d = t[0], t[1], t[2]
    if y < 2024:
        return (0, 0, 0)
    return (y, m, d)


# ---------------------------------------------------------------- 全局状态
class App:
    def __init__(self):
        self.cfg = load_cfg()
        led_config(self.cfg)
        w = self.cfg["wifi"]
        self.net = netmgr.Net(w["ssid"], w["password"], w["hostname"],
                              w["ap_ssid"], w["ap_pass"])
        r = self.cfg["router"]
        # yield_cb：等路由器 CGI 的每一小段都回头服务一次 HTTP。
        # 用 lambda 延迟取 self.web —— web 在下一行才创建，构造时还不能用。
        # 没有这个回调时，一次 1.2 秒的 CGI 会把别的请求整段卡住（实测 1.63s）。
        self.api = rap.RouterAPI(r["host"], r["port"],
                                 yield_cb=lambda: self.web.poll(1))
        self.web = httpsrv.HttpSrv(80)

        self.status = None
        self.net_prev = None
        self.net_prev_ts = 0
        self.clients = {"devices": [], "leases": [], "arp": []}
        self.clients_ts = 0         # 上次成功取到终端列表的时刻（ticks_ms）
        self.clients_due = False    # 需要后台补刷（h_clients 先吐旧数据，不等 CGI）
        self.svc_cache = None       # 转发引擎状态（两个 CGI，缓存住别每次都打）
        self.svc_ts = 0
        self.svc_due = False
        self.weather_size = 0
        self.hist_size = 0
        self.hist_ts = 0            # 上次成功取到历史的时刻（ticks_ms）
        self.hist_due = False       # 需要后台补刷（h_weatherhist 只吐旧文件）
        self.hist_fail_ts = 0       # 上次失败时刻，失败后 60 秒内不再催
        self.channels_size = 0
        self.channels_ts = 0        # 上次成功刷新的时刻（ticks_ms）
        self.channels_due = False   # 需要后台补刷（h_channels 只吐旧文件，不阻塞）
        self.wx = {}
        self.reachable = None
        self.quote = None           # 行情缓存（几百字节）
        self.quote_ts = 0
        self.watch = load_watch()   # 自选股（列表 + 行情各一份缓存）
        self.watch_quotes = None
        self.watch_ts = 0

        self.t_wifi = 0
        self.reboot_at = 0
        self.ready_ms = 0           # boot 到 Web 就绪的耗时，本机信息页展示

        # NTP：ESP32 本机做服务端，上游走公网 NTP
        self.tz_off = int(self.cfg["ntp"].get("tz", 8)) * 3600
        self.t_ntp = 0
        self.ntp_sync_now = 0
        self.ntp_srv_on = True
        self.ntp_up = None
        self.cal_warm = False       # 农历年度表是否已预热

        # ---- 空闲降频 ----
        pw = self.cfg.get("power") or {}
        self.pm_on = bool(pw.get("on", True))
        # 满频不能拿 machine.freq() 当基准：esp_pm 的时钟配置不随 Ctrl-D
        # 软复位恢复，上次空闲时是什么频率，复位后还是什么频率。拿它当基准
        # 会把 80MHz 误认成满频，从此再也不升频（这一版就踩到了）。
        want = int(pw.get("max_mhz", 240)) * 1000000
        try:
            if machine.freq() != want:
                machine.freq(want)          # 软复位带着低频起来时拉回满频
        except Exception as e:
            print("[pm] set max freq fail:", e)
        self.freq_max = machine.freq()       # 以实际值为准（有的板子只到 160）
        idle_hz = int(pw.get("idle_mhz", 80)) * 1000000
        # 低于 80MHz 一律不接受：WiFi 要 80MHz 的 APB，PSRAM 时序也是按它配的。
        self.pm_hz = idle_hz if 80000000 <= idle_hz < self.freq_max else 0
        if not self.pm_hz:
            self.pm_on = False                      # 配错就当没开，别降频后失联
        self.pm_after = max(int(pw.get("idle_after_s", 20)), 3) * 1000
        self.pm_cur = self.freq_max
        self.pm_n = 0                # 上次看到的 web.accepted
        self.pm_ts = BOOT_MS         # 最近一次有请求的时刻
        self.pm_downs = 0            # 累计降频次数（info 页看有没有生效）

    # ---------------- 采集（只在有人请求时触发） ----------------
    def poll_status(self):
        """取一次路由器状态。只在 /api/status 被请求且缓存过期时调用。

        以前是主循环每 15 秒无脑拉一次，路由器 CGI 本身要 1.1~1.2 秒才回，
        没人看页面时也照拉，既占路由器 CPU，又把单线程主循环整段堵住
        （实测这是「跑一段时间后卡死」的直接原因）。现在改成按需 + TTL。
        """
        try:
            st = self.api.cached_json(P_STATUS, 20000, 4096)
            self.reachable = True
        except OSError:
            self.reachable = False
            st = None

        if st:
            now = time.ticks_ms()
            cur = st.get("net") or {}
            out = {}
            for iface, v in cur.items():
                p = (self.net_prev or {}).get(iface, {})
                dt = time.ticks_diff(now, self.net_prev_ts) if self.net_prev_ts else 0
                out[iface] = {
                    "rx": v.get("rx", 0),
                    "tx": v.get("tx", 0),
                    "rxs": rate_of(p.get("rx"), v.get("rx", 0), dt),
                    "txs": rate_of(p.get("tx"), v.get("tx", 0), dt),
                }
            self.net_prev = cur
            self.net_prev_ts = now
            st["net"] = out
            st["ts"] = time.time()
            self.status = st

    def poll_clients(self):
        """取终端流量 + DHCP 租约。

        两个 CGI 的 TTL 刻意不一样：流量 45 秒、租约 180 秒。租约几乎不变，
        让它多缓存一会儿，绝大多数刷新就只用问路由器一个 CGI（1.1~1.6 秒），
        而不是两个串起来等 3 秒——那 3 秒在单线程主循环里就是全站停摆。
        """
        ok = False
        try:
            d = self.api.cached_json(P_CLIENTS, 45000, 8192)
            self.clients["devices"] = (d or {}).get("devices", [])
            ok = True
        except OSError as e:
            print("[clients] fail", e, self.api.last_err)
        try:
            leases = self.api.cached_json(P_DHCP, 180000, 8192)
            self.clients["leases"] = (leases or {}).get("leases", [])
            ok = True
        except OSError as e:
            print("[dhcp] fail", e, self.api.last_err)
        if ok:
            self.clients_ts = time.ticks_ms()

    def poll_weather(self):
        try:
            n = self.api.download_cached(P_WEATHER, WEATHER_FILE,
                                         self.cfg["poll"]["weather_ms"])
            self.weather_size = n
            self.wx = {"temp": find_num_file(WEATHER_FILE, "temp"),
                       "aqi": find_num_file(WEATHER_FILE, "aqi")}
        except OSError as e:
            # 不能只 pass：不清零的话 weather_size 会留着上一次的长度，
            # h_weather 就拿一个「其实不存在的文件」去响应，浏览器只看到 404。
            self.weather_size = 0
            self.wx = None
            print("[weather] fail", e, self.api.last_err)

    # ---------------- NTP ----------------
    def sync_time(self):
        """按配置的顺序向公网 NTP 服务器取时，并记录结果。"""
        up = self.cfg["ntp"].get("upstream") or ["ntp.aliyun.com"]
        try:
            res = ntpsync.sync(up, tz_off=self.tz_off, timeout=4)
            print("[ntp] sync", "ok:" + str(res.get("host")) if res.get("ok")
                  else ("fail:" + str(res.get("err"))),
                  "delay=", res.get("delay"), "offset=", res.get("offset"))
            return res
        except Exception as e:
            print("[ntp] sync err", e)
            return None

    # ---------------- 路由 ----------------
    def setup_routes(self):
        w = self.web
        # 页面
        w.serve_file("/", "www/index.html")
        w.serve_file("/index.html", "www/index.html")
        w.serve_file("/weather.html", "www/weather.html")
        w.serve_file("/info.html", "www/info.html")
        w.serve_file("/radio.html", "www/radio.html")
        w.serve_file("/market.html", "www/market.html")
        w.serve_file("/ntp.html", "www/ntp.html")
        w.serve_file("/iptv.html", "www/iptv.html")
        # 资源
        w.serve_file("/theme.css", "www/theme.css")
        w.serve_file("/ui.js", "www/ui.js")
        w.serve_file("/mpegts.js", "www/mpegts.min.js")
        w.serve_file("/hls.js", "www/hls.min.js")

        w.route("GET", "/api/health", self.h_health)
        w.route("GET", "/api/home", self.h_home)
        w.route("GET", "/api/info", self.h_info)
        w.route("GET", "/api/weather", self.h_weather)
        w.route("GET", "/api/weatherhist", self.h_weatherhist)
        w.route("GET", "/api/channels", self.h_channels)
        w.route("GET", "/api/services", self.h_services)
        w.route("GET", "/api/ntp", self.h_ntp)
        w.route("GET", "/api/calendar", self.h_calendar)
        w.route("GET", "/api/quote", self.h_quote)
        w.route("GET", "/api/watch", self.h_watch)
        w.route("POST", "/api/watch", self.h_watch_action)
        w.route("POST", "/api/ntp", self.h_ntp_action)
        w.route("GET", "/api/config", self.h_get_cfg)
        w.route("POST", "/api/config", self.h_set_cfg)

    def poll_services(self):
        """探两个转发引擎：借路由器上的 svcstat 反代探测，绕开跨域。

        两次 CGI（各 1.1~1.6 秒）很贵，所以结果缓存 60 秒，且一律先吐旧值、
        后台再补刷——首页每打开一次就要问两遍路由器的话，单线程主循环会被
        按住两三秒，这正是「开几个页面就卡死」的帮凶。
        """
        r = self.cfg["router"]
        out = {"ok": 1, "svc": {}}
        for name, port in (("r2h", r.get("r2h_port", 5140)),
                           ("udpxy", r.get("udpxy_port", 12123))):
            try:
                d = self.api.cached_json(P_SVC % port, 60000, 2048)
                out["svc"][name] = {
                    "ok": bool(d.get("ok")),
                    "service": d.get("service", "unknown"),
                    "port": port,
                }
            except OSError:
                out["svc"][name] = {"ok": False, "service": "none", "port": port}
        self.svc_cache = out
        self.svc_ts = time.ticks_ms()

    def _svc_state(self):
        if self.svc_cache is None:
            self.poll_services()
        elif time.ticks_diff(time.ticks_ms(), self.svc_ts) >= 60000:
            self.svc_due = True
        return self.svc_cache or {"ok": 1, "svc": {}}

    def _health_state(self):
        r = self.cfg["router"]
        # 冷启动后第一次被访问时 reachable 还是 None，直接返回会让所有页面
        # 顶部显示「路由器不可达」。这里顺手取一次（TTL 20 秒）。
        if self.reachable is None:
            self.poll_status()
        return {
            "ok": 1,
            "name": self.cfg["device_name"],
            "ip": self.net.ip(),
            "rssi": self.net.rssi(),
            "up": time.ticks_diff(time.ticks_ms(), BOOT_MS) // 1000,
            "mem_free": gc.mem_free(),
            "router": {"host": r["host"], "r2h_port": r["r2h_port"],
                       "udpxy_port": r.get("udpxy_port", 12123)},
            "router_ok": bool(self.reachable),
            "err": self.api.last_err,
        }

    def h_home(self, req):
        """首页一次取全：健康 + 服务状态 + 今日历法。

        为什么合并：首页原来要发 4 条请求（health / weather / services /
        calendar），ESP32 的 lwIP 只有约 10 个 TCP PCB，手机还要预连 6~8 条，
        每多一条真实连接都在逼近死线。合成一条后首页只剩 HTML + 本接口
        （天气实况另外一条），从 5 条降到 3 条。
        """
        out = self._health_state()
        out["svc"] = self._svc_state().get("svc", {})
        try:
            y, m, d = local_ymd()
            out["cal"] = cncal.today(y, m, d) if y else None
        except Exception as e:
            print("[cal]", e)
            out["cal"] = None
        return httpsrv.json_resp(out)

    def h_health(self, req):
        return httpsrv.json_resp(self._health_state())

    def h_watch(self, req):
        """自选股：列表存板上 watch.json，行情一次批量取，按需 + TTL。

        与行情页同一套限制：只有人打开页面才联网，`?force=1` 15 秒内最多一次，
        取失败先给旧值。名称由前端用 GBK 解码（固件里没有 gbk 编码表）。
        """
        now = time.ticks_ms()
        force = req.arg("force") == "1"
        age = None
        if self.watch_quotes and self.watch_ts:
            age = time.ticks_diff(now, self.watch_ts)
            if age < 0:
                age = 0
            if age < WATCH_TTL and not (force and age >= 15000):
                return httpsrv.json_resp({"ok": 1, "age": age // 1000,
                                          "items": self.watch_quotes})
        got = None
        if self.watch:
            try:
                got = rap.sina_quotes([it["code"] for it in self.watch])
            except Exception as e:
                print("[watch]", e)
        if got:
            by = {d["code"]: d for d in got}
            out = []
            for it in self.watch:
                d = by.get(it["code"])
                if not d:
                    continue
                d["name"] = it.get("name") or it["code"]
                out.append(d)
            self.watch_quotes = out
            self.watch_ts = now
            return httpsrv.json_resp({"ok": 1, "age": 0, "items": out})
        if self.watch_quotes:
            return httpsrv.json_resp({"ok": 1, "stale": 1,
                                      "items": self.watch_quotes})
        return httpsrv.json_resp({"ok": 0, "msg": "行情源不可用"})

    def h_watch_action(self, req):
        """增删自选。只动板上 watch.json，不触发重启、不联网。"""
        data = req.json() or {}
        op = data.get("op")
        code = str(data.get("code") or "").strip().lower()
        # sh600519 / sz000001 / bj430047 / fx_susdcny
        ok_code = (len(code) == 8 and code[2:].isdigit()
                   and code[:2] in ("sh", "sz", "bj")) or code.startswith("fx_")
        if not ok_code:
            return httpsrv.json_resp({"ok": 0, "msg": "代码格式不对，例：sh600519"})
        if op == "add":
            if len(self.watch) >= WATCH_MAX:
                return httpsrv.json_resp({"ok": 0, "msg": "最多 %d 支" % WATCH_MAX})
            if not any(x["code"] == code for x in self.watch):
                self.watch.append({"code": code, "name": str(data.get("name") or "")})
        elif op == "del":
            self.watch = [x for x in self.watch if x["code"] != code]
        else:
            return httpsrv.json_resp({"ok": 0, "msg": "未知操作"})
        if not save_watch(self.watch):
            return httpsrv.json_resp({"ok": 0, "msg": "写入失败"})
        self.watch_quotes = None
        self.watch_ts = 0
        return httpsrv.json_resp({"ok": 1,
                                  "codes": [x["code"] for x in self.watch]})

    def h_info(self, req):
        """本机信息：全部读本地（os / sys / gc / machine），零网络请求。

        这是「状态」页的替代品。原来那页每 8 秒打一次路由器 CGI，
        路由器本身要 1.1~1.6 秒才回，单线程主循环被持续占用，
        开久了就会卡死。这里不碰 socket，毫秒级返回，也不给路由器添负担。
        """
        import os
        import sys
        gc.collect()
        free = gc.mem_free()
        alloc = gc.mem_alloc()
        v = os.statvfs("/")
        bs = v[0]
        files = []
        try:
            for name in sorted(os.listdir("/")):
                if name != "www":
                    files.append(["/" + name, os.stat("/" + name)[6]])
        except OSError:
            pass
        try:
            for name in sorted(os.listdir("/www")):
                files.append(["/www/" + name, os.stat("/www/" + name)[6]])
        except OSError:
            pass
        try:
            import network
            _wlan = network.WLAN(network.STA_IF)
            mac = ":".join(["%02x" % b for b in _wlan.config("mac")])
        except Exception:
            _wlan = None
            mac = ""

        # 芯片原生状态。注意：Python 堆（在 PSRAM 上）大，并不代表网络层安全。
        # WiFi/lwIP 只用内部 RAM，它的「最大连续块」才是判断会不会出现
        # 「连得上但没响应」的关键指标 —— 旧板就是把这个块耗到只剩 1KB 才卡的。
        try:
            import esp32
        except Exception:
            esp32 = None
        temp = None
        heap_data = []
        heap_exec = []
        if esp32 is not None:
            try:
                temp = esp32.mcu_temperature()
            except Exception:
                temp = None
            try:
                for _b in esp32.idf_heap_info(esp32.HEAP_DATA):
                    heap_data.append([_b[0], _b[1], _b[2], _b[3]])
            except Exception:
                pass
            try:
                for _b in esp32.idf_heap_info(esp32.HEAP_EXEC):
                    heap_exec.append([_b[0], _b[1], _b[2], _b[3]])
            except Exception:
                pass
        try:
            _rc = machine.reset_cause()
        except Exception:
            _rc = 0
        RESET_CN = {1: "上电", 2: "硬复位", 3: "看门狗", 4: "深睡唤醒",
                    5: "软复位", 6: "掉电"}
        try:
            import micropython
            _opt = micropython.opt_level()
        except Exception:
            _opt = -1

        _ssid = ""
        _wstat = -1
        _chan = 0
        _txp = 0
        _pm = -1
        if _wlan is not None:
            try:
                _ssid = _wlan.config("ssid") or ""
            except Exception:
                pass
            try:
                _wstat = _wlan.status()
            except Exception:
                pass
            try:
                _chan = _wlan.config("channel")
            except Exception:
                pass
            try:
                _txp = _wlan.config("txpower")
            except Exception:
                pass
            try:
                _pm = _wlan.config("pm")
            except Exception:
                pass
        STA_CN = {1000: "空闲", 1001: "连接中", 1002: "密码错误",
                  1003: "未找到 AP", 1004: "连接失败", 1010: "已连接"}
        PM_CN = {0: "关闭省电", 1: "均衡", 2: "省电"}

        u = os.uname()
        ver = sys.implementation.version
        return httpsrv.json_resp({
            "ok": 1,
            "fw": {"ver": "%d.%d.%d" % (ver[0], ver[1], ver[2]),
                   "build": u.version, "board": u.machine,
                   "freq": machine.freq(), "freq_max": self.freq_max,
                   "pm_on": self.pm_on, "pm_downs": self.pm_downs,
                   "pm_mhz": (self.pm_hz // 1000000) if self.pm_on else 0,
                   "opt": _opt},
            "net": {"ip": self.net.ip(), "mac": mac,
                    "rssi": self.net.rssi(),
                    "hostname": self.cfg["wifi"].get("hostname", ""),
                    "ssid": _ssid, "status": _wstat,
                    "status_name": STA_CN.get(_wstat, str(_wstat)),
                    "channel": _chan, "txpower": _txp, "pm": _pm,
                    "pm_name": PM_CN.get(_pm, "—")},
            "chip": {"temp": temp, "reset": _rc,
                     "reset_name": RESET_CN.get(_rc, str(_rc))},
            "heap": {"data": heap_data, "exec": heap_exec},
            "mem": {"free": free, "used": alloc, "total": free + alloc},
            "flash": {"total": v[2] * bs, "avail": v[3] * bs,
                      "used": (v[2] - v[3]) * bs},
            "up": time.ticks_diff(time.ticks_ms(), BOOT_MS) // 1000,
            "ready_ms": self.ready_ms,
            "files": files,
        })

    def h_clients(self, req):
        """终端流量排行。

        **2026-11-07 已下线**（页面与路由都摘了）：它的数据源
        `/cgi-bin/netperip` 是路由器上最慢的 CGI，实测单次 25 秒都没返回，
        而板子是单线程的，等它的时候整站不响应。留着代码是为了以后路由器
        那边接口变快时能一键恢复；现在没有任何页面引用它。
        """
        ttl = 45000
        fresh = self.clients_ts and time.ticks_diff(
            time.ticks_ms(), self.clients_ts) < ttl
        if self.clients["devices"]:
            if not fresh:
                self.clients_due = True
            return httpsrv.json_resp({"ok": 1,
                                      "devices": self.clients.get("devices", []),
                                      "leases": self.clients.get("leases", []),
                                      "stale": 0 if fresh else 1})
        self.poll_clients()
        return httpsrv.json_resp({"ok": 1,
                                  "devices": self.clients.get("devices", []),
                                  "leases": self.clients.get("leases", []),
                                  "stale": 0})

    def h_calendar(self, req):
        """农历 / 节气 / 节日：纯本地查表，不联网、不读路由器 CGI。

        年度表由 cncal 生成一次落盘成 calendar.json，之后命中缓存直接返回，
        所以这个接口不会像 CGI 采集那样把单线程主循环卡住。
        """
        y, m, d = local_ymd()
        if not y:
            return httpsrv.json_resp({"ok": 0, "msg": "时间未同步"})
        try:
            return httpsrv.json_resp(cncal.today(y, m, d))
        except Exception as e:
            print("[cal]", e)
            return httpsrv.json_resp({"ok": 0, "msg": "历法不可用"})

    def h_quote(self, req):
        """行情页：指数 + 汇率，一次批量取（/list=a,b,c）。

        只有「行情页请求且缓存过期」才联网，后台完全不轮询。
        `?force=1` 可强制刷新，但 15 秒内最多真联网一次——单线程下
        连点刷新会把主循环整段堵住。取失败时 60 秒后再试，有旧值先给旧值。
        """
        q = self.cfg.get("quote") or {}
        if not q.get("on", True):
            return httpsrv.json_resp({"ok": 0, "msg": "已关闭"})
        # 兼容只写了 code/name 的旧配置
        items = q.get("items") or [{"code": q.get("code", "sh000001"),
                                    "name": q.get("name", "上证指数"),
                                    "kind": "index"}]
        ttl = int(q.get("ttl_ms", 600000))
        now = time.ticks_ms()
        force = req.arg("force") == "1"

        age = None
        if self.quote and self.quote_ts:
            age = time.ticks_diff(now, self.quote_ts)
            if age < 0:
                age = 0
            # 缓存还新鲜就直出；force 也只在满 15 秒后才真的再联网
            if age < ttl and not (force and age >= 15000):
                return httpsrv.json_resp({"ok": 1, "age": age // 1000,
                                          "items": self.quote})

        got = None
        try:
            got = rap.sina_quotes([it["code"] for it in items])
        except Exception as e:
            print("[quote]", e)

        if got:
            by = {}
            for d in got:
                by[d["code"]] = d
            out = []
            for it in items:
                d = by.get(it["code"])
                if not d:
                    continue
                # 名称与分组取自配置：返回体里的中文名是 GBK，不能直接 decode
                d["name"] = it.get("name") or it["code"]
                d["kind"] = it.get("kind") or "index"
                out.append(d)
            if out:
                self.quote = out
                self.quote_ts = now
                return httpsrv.json_resp({"ok": 1, "age": 0, "items": out})

        # 失败：提前 60 秒到期，下次再试；手里有旧值就先给旧的
        self.quote_ts = time.ticks_add(now, -(ttl - 60000)) if ttl > 65000 else now
        if self.quote:
            return httpsrv.json_resp({"ok": 1, "stale": 1,
                                      "age": (age or 0) // 1000,
                                      "items": self.quote})
        return httpsrv.json_resp({"ok": 0, "msg": "行情不可用"})

    def h_weather(self, req):
        # download_cached 自带 TTL（weather_ms，默认 10 分钟），命中就秒回，
        # 过期才去拉一次，所以这里可以放心每次调用。
        self.poll_weather()
        if not self.weather_size or not self._file_ok(WEATHER_FILE):
            # 内存里记着长度、flash 上却没文件（被删 / 断电丢 / rename 空窗），
            # 丢掉缓存强制重拉一次让页面自愈，而不是把 404 甩给浏览器。
            self.api.drop(P_WEATHER)
            self.poll_weather()
        if not self.weather_size or not self._file_ok(WEATHER_FILE):
            return httpsrv.json_resp({"ok": 0, "msg": "天气数据不可用（路由器 CGI 取数失败）"})
        return httpsrv.file_resp(WEATHER_FILE, httpsrv.CTYPES[".json"])

    def h_weatherhist(self, req):
        """过去 7 天历史天气：只吐已落盘的旧文件，从不在这里等 CGI。

        路由器那条 /cgi-bin/weatherhist 要**串行**向上游取 7 天，每次超时
        15 秒，冷启动常常 5 秒以上。放进请求路径里会把单线程主循环整段堵死，
        所以策略和频道库一样：
        - 有文件 → 立刻吐（几十毫秒），过期只打个「该刷了」的标记
        - 没文件 → 立刻回 pending，前端过几秒自己再来要
        真正的取数在主循环里做（refresh_weather_history）。
        """
        ttl = self.cfg["poll"].get("weatherhist_ms", 43200000)
        if self._file_ok(WHIST_FILE):
            if (not self.hist_size or not self.hist_ts
                    or time.ticks_diff(time.ticks_ms(), self.hist_ts) >= ttl):
                # 刚刚失败过就先别催，免得每个请求都触发一次 20 秒的空转
                if not self.hist_fail_ts or time.ticks_diff(
                        time.ticks_ms(), self.hist_fail_ts) >= 60000:
                    self.hist_due = True
            return httpsrv.file_resp(WHIST_FILE, httpsrv.CTYPES[".json"])
        if not self.hist_fail_ts or time.ticks_diff(
                time.ticks_ms(), self.hist_fail_ts) >= 60000:
            self.hist_due = True
        return httpsrv.json_resp({"ok": 0, "pending": 1,
                                  "msg": "历史数据准备中，稍后自动刷新"})

    def refresh_weather_history(self):
        """主循环里的补刷：7 天历史最长可能要十几秒，绝不放在请求路径上。

        等数据期间 _more() 每 0.12 秒 yield 一次，HTTP 照常服务；
        失败不删除旧文件，下次进页面时按 60 秒退避重试。
        """
        self.hist_due = False
        ttl = self.cfg["poll"].get("weatherhist_ms", 43200000)
        try:
            self.api.drop(P_WHIST)
            n = self.api.download_cached(P_WHIST, WHIST_FILE, ttl,
                                         limit=131072, dl=20000)
            if n and self._file_ok(WHIST_FILE):
                self.hist_size = n
                self.hist_ts = time.ticks_ms()
                self.hist_fail_ts = 0
                print("[whist] ok", n)
        except OSError as e:
            self.hist_fail_ts = time.ticks_ms()
            print("[whist] fail", e, self.api.last_err)

    @staticmethod
    def _file_ok(filename):
        import os
        try:
            return os.stat(filename)[6] > 0
        except OSError:
            return False

    def h_channels(self, req):
        # 18KB 的频道库不塞内存：落盘后由 HTTP 层流式吐，避免碎片化时申请失败。
        #
        # 这里刻意**不**在请求里等 CGI（stale-while-revalidate）：
        # 路由器 /cgi-bin/iptv 要 1.6~3.1 秒，而它是单线程主循环里跑的，
        # 这段时间所有别的连接都在排队——浏览器打开 IPTV 页时同时有 5~6 条
        # 连接，等到 3 秒的挂起超时就被丢掉，Chrome 再重试，形成雪崩。
        # 现在：有旧文件就立刻吐（0.3 秒），刷新标记推给主循环下一轮去做。
        ttl = self.cfg["poll"]["channels_ms"]
        # 只要 flash 上还有上次落盘的频道库就先用它——哪怕刚重启、
        # 内存里还没记长度，也比让用户等 3 秒 CGI 强。
        if self._file_ok(CHANNELS_FILE):
            if not self.channels_size or time.ticks_diff(
                    time.ticks_ms(), self.channels_ts) >= ttl:
                self.channels_due = True
            return httpsrv.file_resp(CHANNELS_FILE, httpsrv.CTYPES[".json"])
        # 第一次（或文件丢了）只能同步拉一次，否则页面没东西可显示
        try:
            self.channels_size = self.api.download_cached(
                P_CHANNELS, CHANNELS_FILE, ttl)
            self.channels_ts = time.ticks_ms()
        except OSError as e:
            print("[channels] fail", e, self.api.last_err)
            return httpsrv.json_resp({"ok": 0, "msg": "频道库不可用", "items": []})
        if not self._file_ok(CHANNELS_FILE):
            return httpsrv.json_resp({"ok": 0, "msg": "频道库不可用", "items": []})
        return httpsrv.file_resp(CHANNELS_FILE, httpsrv.CTYPES[".json"])

    def refresh_channels(self):
        """主循环里的补刷：不等请求，失败也不影响已展示的旧列表。"""
        self.channels_due = False
        try:
            self.api.drop(P_CHANNELS)
            n = self.api.download_cached(P_CHANNELS, CHANNELS_FILE,
                                         self.cfg["poll"]["channels_ms"])
            if n and self._file_ok(CHANNELS_FILE):
                self.channels_size = n
                self.channels_ts = time.ticks_ms()
        except OSError as e:
            print("[channels] refresh fail", e, self.api.last_err)

    def h_services(self, req):
        """转发引擎状态：有缓存先吐缓存，刷新交给主循环（见 poll_services）。"""
        return httpsrv.json_resp(self._svc_state())

    def h_ntp(self, req):
        """NTP 页：本机服务状态 + 上游配置 + 同步记录。"""
        try:
            ip = self.net.ip()
        except Exception:
            ip = "—"
        return httpsrv.json_resp({
            "ok": 1,
            "local": {
                "running": ntpsrv.is_running(),
                "port": 123,
                "ip": ip,
                "served": ntpsrv.stat["served"],
                "last_client": ntpsrv.stat["last_client"],
                "last_at": ntpsrv.stat["last_at"],
                "err": ntpsrv.stat["err"],
            },
            "upstream": self.cfg["ntp"].get("upstream") or [],
            "sync_ms": self.cfg["ntp"].get("sync_ms", 1800000),
            "tz": int(self.cfg["ntp"].get("tz", 8)),
            "sync": ntpsync.snapshot(),
        })

    def h_ntp_action(self, req):
        """NTP 写操作：立即同步 / 启停本机服务 / 改上游 / 改间隔 / 改时区。"""
        d = req.json() or {}
        act = str(d.get("action", ""))
        val = str(d.get("value", ""))

        if act == "sync":
            self.ntp_sync_now = 1
            return httpsrv.json_resp({"ok": 1, "msg": "已排队同步，稍候刷新"})

        if act == "srv":
            if val == "off":
                ntpsrv.stop()
                self.ntp_srv_on = False
                return httpsrv.json_resp({"ok": 1, "msg": "本机 NTP 服务已停止"})
            ok = ntpsrv.start(123)
            self.ntp_srv_on = ok
            return httpsrv.json_resp({"ok": 1 if ok else 0,
                                      "msg": "本机 NTP 服务已启动" if ok else "启动失败（123 端口被占？）"})

        if act == "upstream":
            hosts = [h.strip() for h in val.replace(" ", ",").split(",")]
            hosts = [h for h in hosts if h]
            if not hosts:
                return httpsrv.json_resp({"ok": 0, "msg": "上游列表不能为空"}, 400)
            for h in hosts:
                for ch in h:
                    if not (ch.isalnum() or ch in ".:-"):
                        return httpsrv.json_resp({"ok": 0, "msg": "地址格式不合法: " + h}, 400)
            self.cfg["ntp"]["upstream"] = hosts
            save_cfg(self.cfg)
            self.ntp_sync_now = 1
            return httpsrv.json_resp({"ok": 1, "msg": "上游已保存，正在重新同步"})

        if act == "interval":
            if not val.isdigit() or int(val) < 60:
                return httpsrv.json_resp({"ok": 0, "msg": "间隔至少 60 秒"}, 400)
            self.cfg["ntp"]["sync_ms"] = int(val) * 1000
            save_cfg(self.cfg)
            return httpsrv.json_resp({"ok": 1, "msg": "同步间隔已保存"})

        if act == "tz":
            try:
                tz = int(val)
            except ValueError:
                return httpsrv.json_resp({"ok": 0, "msg": "时区必须是整数小时"}, 400)
            if tz < -12 or tz > 14:
                return httpsrv.json_resp({"ok": 0, "msg": "时区超出范围"}, 400)
            self.cfg["ntp"]["tz"] = tz
            self.tz_off = tz * 3600
            save_cfg(self.cfg)
            self.ntp_sync_now = 1
            return httpsrv.json_resp({"ok": 1, "msg": "时区已保存，正在重新同步"})

        return httpsrv.json_resp({"ok": 0, "msg": "不支持的动作: " + act}, 400)

    def h_get_cfg(self, req):
        cfg = json.loads(json.dumps(self.cfg))
        if cfg["wifi"].get("password"):
            cfg["wifi"]["password"] = "********"
        return httpsrv.json_resp({"ok": 1, "cfg": cfg})

    def h_set_cfg(self, req):
        data = req.json()
        if not data:
            return httpsrv.json_resp({"ok": 0, "msg": "请求体不是合法 JSON"}, 400)
        cfg = self.cfg
        for k in ("device_name",):
            if k in data:
                cfg[k] = data[k]
        if "wifi" in data:
            for k, v in data["wifi"].items():
                if k == "password" and v == "********":
                    continue
                cfg["wifi"][k] = v
        if "router" in data:
            for k, v in data["router"].items():
                cfg["router"][k] = v
        try:
            save_cfg(cfg)
        except Exception as e:
            return httpsrv.json_resp({"ok": 0, "msg": "写入失败: %s" % e}, 500)
        self.reboot_at = time.ticks_ms()
        return httpsrv.json_resp({"ok": 1, "msg": "已保存，设备即将重启", "reboot": 1})

    # ---------------- 空闲降频 ----------------
    def _set_freq(self, hz):
        """切换 CPU 频率。切不动就永久关掉这个功能，不要每轮刷日志。"""
        if hz == self.pm_cur:
            return
        try:
            machine.freq(hz)
            # 换频当轮摸一下堆（Python 堆在 PSRAM 上）：PSRAM 时序要是没跟上，
            # 崩在这里比崩在别人的请求里好定位。2KB 只在切换的那一轮分配。
            bytearray(2048)
            self.pm_cur = machine.freq()
            if self.pm_cur != hz:
                raise OSError("freq not applied")
        except Exception as e:
            print("[pm] freq %d fail:" % hz, e)
            self.pm_on = False          # 这平台改不了频，别再试了
            try:
                machine.freq(self.freq_max)   # 尽量爬回满频
            except Exception:
                pass
            try:
                self.pm_cur = machine.freq()  # 记真实值，别让状态撒谎
            except Exception:
                self.pm_cur = self.freq_max
            return
        if hz < self.freq_max:
            self.pm_downs += 1
            print("[pm] idle ->", hz // 1000000, "MHz")
        else:
            print("[pm] busy ->", hz // 1000000, "MHz")

    def idle_power(self, now):
        """有请求就回满频，连续 idle_after_s 没人访问就降频。

        `accepted` 在**请求被服务完**时才加，光靠它等于让空闲后的第一发
        整段跑在低频：实测 TTFB 从 0.66s 变成 1.17s。所以再瞄一眼服务层的
        `_pending`（刚 accept、请求还没读上来的连接），有 2 秒内的新连接就
        先升频，下一次 poll 读请求时已经是满频。
        没请求的预连接不该一直把我们钉在满频，所以只认 2 秒内的。
        """
        if not self.pm_on:
            return
        n = self.web.accepted
        if n != self.pm_n:
            self.pm_n = n
            self.pm_ts = now
            self._set_freq(self.freq_max)
            return
        for item in getattr(self.web, "_pending", None) or ():
            if time.ticks_diff(now, item[1]) < 2000:
                self._set_freq(self.freq_max)
                return
        if self.pm_cur == self.freq_max and \
                time.ticks_diff(now, self.pm_ts) >= self.pm_after:
            self._set_freq(self.pm_hz)

    # ---------------- 主循环 ----------------
    def boot(self):
        print("\n=== ESP32 Terminal Server ===")
        print("[cfg]", self.cfg["router"]["host"], "->", self.cfg["wifi"]["ssid"])
        if not self.cfg["wifi"]["ssid"]:
            print("[wifi] 未配置，直接进配置模式")
            self.net.start_ap()
        elif not self.net.connect(20):
            print("[wifi] 连接失败，开热点等配置")
            self.net.start_ap()
        else:
            print("[wifi] ok:", self.net.ip())

        self.setup_routes()
        self.web.start()
        self.ready_ms = time.ticks_diff(time.ticks_ms(), BOOT_MS)
        print("[web] ready in %d ms" % self.ready_ms)
        # 首轮取数交给 run() 的循环：先让 web 能立刻响应，
        # 否则页面首屏请求会撞在阻塞的 CGI 拉取上而超时。
        ntpsrv.start(123)   # 本机 NTP 服务（时间由 run() 里的同步任务负责）
        led_off()  # 启动灯灭，主循环接手心跳

    def run(self):
        self.boot()
        while True:
            now = time.ticks_ms()

            try:
                self.web.poll()
            except Exception as e:
                print("[web]", e)

            self.idle_power(now)   # 空闲降频：有人访问就回满频，长时间没人降频

            led_heartbeat()   # 非阻塞：到点闪一下，不影响请求处理

            # 本机 NTP 服务：非阻塞，每次循环捡几个请求
            for _ in range(3):
                try:
                    if not ntpsrv.serve():
                        break
                except Exception as e:
                    print("[ntpsrv]", e)
                    break

            # 这里**不再有任何**周期性取数：状态 / 客户端 / 天气 / 频道库
            # 全部改成「有人请求且缓存过期」时才去问路由器一次。
            # 路由器 CGI 本身要 1.1~1.2 秒才回，后台轮询既白占它 CPU，
            # 又会把单线程主循环整段堵住——这正是长时间运行后卡死的根因。

            # 频道库 / 终端列表到期补刷：请求侧只吐旧数据，补刷放在这里慢慢做，
            # 不占用任何一次 HTTP 请求的等待时间。
            if self.channels_due:
                self.refresh_channels()
            if self.hist_due:
                self.refresh_weather_history()
            if self.clients_due:
                self.clients_due = False
                self.poll_clients()
            if self.svc_due:
                self.svc_due = False
                self.poll_services()

            if time.ticks_diff(now, self.t_wifi) >= 30000:
                self.t_wifi = now
                if not self.net.is_up() and self.cfg["wifi"]["ssid"]:
                    print("[wifi] 掉线，重连")
                    self.net.reconnect()

            # 时间同步：开机先对一次，之后按 sync_ms 周期；失败 60 秒后重试
            ntp_ms = self.cfg["ntp"].get("sync_ms", 1800000)
            if (self.ntp_sync_now or self.t_ntp == 0
                    or time.ticks_diff(now, self.t_ntp) >= ntp_ms):
                self.ntp_sync_now = 0
                self.t_ntp = now
                self.web.poll(1)
                r = None
                try:
                    r = self.sync_time()
                except Exception as e:
                    print("[ntp]", e)
                if not (r and r.get("ok")) and ntp_ms > 60000:
                    self.t_ntp = time.ticks_add(now, -(ntp_ms - 60000))

            # 农历年度表预热：等 NTP 把时间对上后做一次。命中 calendar.json
            # 只有几十毫秒；首次生成也不过一两百毫秒，且一年只发生一次。
            # 放在这里预热，首个 /api/calendar 请求就能直接读内存返回。
            if not self.cal_warm:
                y0 = local_ymd()[0]
                if y0:
                    self.cal_warm = True
                    try:
                        t_cal = time.ticks_ms()
                        c = cncal.ensure(y0)
                        print("[cncal] warm", y0,
                              "ms=", time.ticks_diff(time.ticks_ms(), t_cal),
                              "count=", (c or {}).get("count"))
                    except Exception as e:
                        print("[cncal] warm err", e)

            if self.reboot_at and time.ticks_diff(now, self.reboot_at) > 800:
                print("[sys] 配置已保存，重启")
                time.sleep_ms(200)
                machine.reset()

            time.sleep_ms(30)


app = App()
app.run()
