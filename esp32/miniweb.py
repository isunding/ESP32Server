# -*- coding: utf-8 -*-
"""miniweb.py — ESP32 MicroPython 极简网页服务

设计要点（针对"连上了但无响应"）：
  1. listen socket 设为非阻塞 + select 等待，accept 永不干等
  2. 每个客户端 settimeout()，慢连接最多占用 0.6 秒，不会拖死主循环
  3. 主循环外层 try/except 兜底，任何异常都只打印、不退出
  4. 请求处理全程无阻塞调用（不查 NTP、不读大 JSON）
  5. 定期 gc.collect()，避免长期运行碎片化
  6. 静态页从 www/ 分块发送（优先同名 .gz），单次读写不超过 2KB

路由：
  /              首页，取 /www/index.html（缺失时退回内嵌极简页）
  /api/health    本机状态 + 路由器可达性 + 转发引擎状态（含 /api/home）
  /api/info      本机信息（内存 / Flash / 文件清单）
  /api/ntp       校时页只读状态
  /api/weather   天气聚合，带 TTL 从路由器拉取，失败退回板上旧文件
  /api/weatherhist、/api/channels  只发板上已有数据文件
  /api/quote、/api/watch、/api/calendar  精简版未接入，返回 ok:0
  /api/status    本机状态 JSON（调试用）
  /ping          健康探测，返回 pong
  /reboot        重启设备
  /<name>        从 /www/ 读取静态文件，优先 /www/<name>.gz
"""

import gc
import os
import select
import socket
import sys
import time

import network

PORT = 80
_T0 = time.ticks_ms()
_hits = 0
_CHUNK = 2048              # GC 不压缩内存，单次读写越小越不容易失败
_WX_STALE = 1800           # 天气文件超过 30 分钟就主动让路由器重取

try:
    import machine
    _HAS_MACHINE = True
except Exception:
    _HAS_MACHINE = False


# ---------------------------------------------------------------- 工具函数
def _ms():
    return time.ticks_diff(time.ticks_ms(), _T0)


def _wlan():
    return network.WLAN(network.STA_IF)


def _ensure_wifi():
    """boot.py 通常已连好；这里做兜底重连，用的是 config.json 里的凭据。"""
    sta = _wlan()

    # ★ 关键一行：关闭 WiFi 省电模式。
    # ESP32 默认 pm=1(WIFI_PS_MIN_MODEM)， modem 会周期性休眠，
    # 休眠窗口内收到的 TCP SYN 直接被丢弃 -> 三次握手要重传，
    # 表现为「ping 能通，但网页/接口 7 秒以上无响应」。
    # 设为 PM_NONE 后收包即时响应，代价是待机电流从 ~20mA 升到 ~120mA。
    try:
        sta.active(True)
        sta.config(pm=network.WLAN.PM_NONE)
    except Exception as e:
        print("[miniweb] pm:", e)

    if sta.isconnected():
        return True
    try:
        import json
        with open("config.json") as f:
            w = json.load(f).get("wifi", {})
        ssid = w.get("ssid")
        if ssid and not sta.isconnected():
            try:
                sta.connect(ssid, w.get("password", ""))
            except Exception:
                pass
        t = time.ticks_ms()
        while not sta.isconnected() and time.ticks_diff(
                time.ticks_ms(), t) < 15000:
            time.sleep_ms(200)
    except Exception as e:
        print("[miniweb] wifi:", e)
    return sta.isconnected()


def _mac(sta):
    try:
        return ":".join("%02X" % b for b in sta.config("mac"))
    except Exception:
        return "n/a"


def _rssi(sta):
    try:
        return sta.status("rssi")
    except Exception:
        return 0


def _info():
    sta = _wlan()
    try:
        ip, mask, gw, dns = sta.ifconfig()
    except Exception:
        ip, gw = "0.0.0.0", "0.0.0.0"
    return ip, gw, _rssi(sta), _mac(sta)


# ---------------------------------------------------------------- 页面模板
# 用 @@XXX@@ 占位再 replace，避开 CSS 里 { } 和 % 与格式化的冲突
_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
"Microsoft YaHei",sans-serif;background:#f4f6fa;color:#1a2233;
padding:18px;line-height:1.5}
.wrap{max-width:680px;margin:0 auto}
.head{background:linear-gradient(135deg,#2563eb,#7c3aed);color:#fff;
border-radius:16px;padding:22px 24px;box-shadow:0 8px 24px rgba(37,99,235,.25)}
.head h1{font-size:20px;font-weight:600;letter-spacing:.3px}
.head p{opacity:.85;font-size:13px;margin-top:6px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;
background:#4ade80;margin-right:7px;box-shadow:0 0 0 3px rgba(74,222,128,.3)}
.bar{display:flex;gap:10px;margin-top:16px;flex-wrap:wrap}
a.btn{flex:1;min-width:120px;text-align:center;padding:12px 18px;
border-radius:10px;text-decoration:none;font-size:14px;font-weight:600;
background:#fff;color:#2563eb;border:1px solid #d8e0ee;transition:.15s}
a.btn:hover{background:#eff4ff;border-color:#b9c9e8}
"""

# 入口页：只放标题与页面链接，不显示任何运行状态
_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ESP32 Server</title>
<style>@@CSS@@</style>
</head><body><div class="wrap">
  <div class="head">
    <h1><span class="dot"></span>ESP32 Server</h1>
    <p>精简模式 · 静态页面</p>
  </div>
  <div class="bar">
    <a class="btn" href="/weather.html">天气</a>
    <a class="btn" href="/market.html">行情</a>
    <a class="btn" href="/iptv.html">IPTV</a>
    <a class="btn" href="/radio.html">收音机</a>
    <a class="btn" href="/ntp.html">时间</a>
    <a class="btn" href="/info.html">本机信息</a>
  </div>
</div></body></html>"""

_CT = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def _ctype(name):
    i = name.rfind(".")
    return _CT.get(name[i:].lower() if i >= 0 else "", "application/octet-stream")


def _page():
    return _HTML.replace("@@CSS@@", _CSS)


def _json_status():
    ip, gw, rssi, mac = _info()
    gc.collect()
    return ('{"ok":true,"ip":"%s","gw":"%s","rssi":%d,"mac":"%s",'
            '"free":%d,"uptime_ms":%d,"hits":%d}'
            % (ip, gw, rssi, mac, gc.mem_free(), _ms(), _hits))


# ---------------------------------------------------------------- 配置与探测
_cfg_cache = None


def _cfg():
    """配置只读一次：每个请求都 open + json.load 会白占时间。"""
    global _cfg_cache
    if _cfg_cache is None:
        try:
            import json
            with open("config.json") as f:
                _cfg_cache = json.load(f)
        except Exception as e:
            print("[miniweb] cfg:", e)
            _cfg_cache = {}
    return _cfg_cache


def _router_cfg():
    r = _cfg().get("router") or {}
    return (r.get("host", "10.0.0.1"), int(r.get("port", 80)),
            int(r.get("r2h_port", 5140)), int(r.get("udpxy_port", 12123)))


_probe_cache = {}


def _probe(host, port, ttl_ms=20000):
    """短超时 TCP 探测，结果缓存。

    顶栏心跳 15 秒一次，每次都去连三个端口（各 0.4 秒超时）会把单线程
    主循环拖住；缓存后正常情况下请求是零网络开销的。
    """
    key = (host, port)
    now = time.ticks_ms()
    hit = _probe_cache.get(key)
    if hit and 0 <= time.ticks_diff(now, hit[0]) < ttl_ms:
        return hit[1]
    ok = False
    s = None
    try:
        s = socket.socket()
        s.settimeout(0.4)
        s.connect((host, port))
        ok = True
    except Exception:
        ok = False
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
    _probe_cache[key] = (now, ok)
    return ok


def _b(x):
    return "true" if x else "false"


# ---------------------------------------------------------------- 数据接口
def _json_health():
    """首页与顶栏共用：本机状态 + 路由器可达性 + 转发引擎状态。"""
    sta = _wlan()
    try:
        ip = sta.ifconfig()[0]
    except Exception:
        ip = "0.0.0.0"
    host, p80, p2, pu = _router_cfg()
    rk = _probe(host, p80)
    r2 = _probe(host, p2)
    ud = _probe(host, pu)
    name = _cfg().get("device_name") or "ESP32 终端服务器"
    return ('{"ok":1,"name":"%s","ip":"%s","rssi":%d,"up":%d,"mem_free":%d,'
            '"router":{"host":"%s","r2h_port":%d,"udpxy_port":%d},'
            '"router_ok":%s,'
            '"svc":{"r2h":{"ok":%s,"service":"%s","port":%d},'
            '"udpxy":{"ok":%s,"service":"%s","port":%d}},'
            '"led_on":false,"led_hw":false}'
            % (name, ip, _rssi(sta), _ms() // 1000, gc.mem_free(),
               host, p2, pu, _b(rk),
               _b(r2), "running" if r2 else "none", p2,
               _b(ud), "running" if ud else "none", pu))


def _json_info():
    """本机信息页：全部取自 os / gc / machine，不发网络请求。"""
    sta = _wlan()
    try:
        ip = sta.ifconfig()[0]
    except Exception:
        ip = "0.0.0.0"
    gc.collect()
    free = gc.mem_free()
    used = gc.mem_alloc()
    try:
        v = os.statvfs("/")
        ftotal = v[0] * v[2]
        favail = v[0] * v[3]
    except Exception:
        ftotal = favail = 0
    items = []
    try:
        for n in sorted(os.listdir()):
            if n == "www":
                continue
            try:
                items.append('["%s",%d]' % (n, os.stat(n)[6]))
            except OSError:
                pass
        for n in sorted(os.listdir("www")):
            try:
                items.append('["www/%s",%d]' % (n, os.stat("www/" + n)[6]))
            except OSError:
                pass
    except OSError:
        pass
    try:
        import esp
        flash_total = esp.flash_size()
    except Exception:
        flash_total = 0
    try:
        freq = machine.freq() if _HAS_MACHINE else 0
    except Exception:
        freq = 0
    try:
        impl = sys.implementation[1]
        ver = "%d.%d.%d" % (impl[0], impl[1], impl[2])
    except Exception:
        ver = "—"
    hostname = (_cfg().get("wifi") or {}).get("hostname", "")
    return ('{"ok":1,"net":{"hostname":"%s","ip":"%s","mac":"%s","rssi":%d},'
            '"up":%d,"ready_ms":0,'
            '"mem":{"total":%d,"used":%d,"free":%d},'
            '"flash":{"total":%d,"used":%d,"avail":%d},'
            '"fw":{"ver":"%s","build":"","board":"%s","freq":%d},'
            '"files":[%s]}'
            % (hostname, ip, _mac(sta), _rssi(sta), _ms() // 1000,
               free + used, used, free,
               flash_total, flash_total - favail, favail,
               ver, "ESP32", freq, ",".join(items)))


_ntp_mod = None
_ntp_due = True          # 需要同步一次（启动后第一次空闲就做）
_ntp_last = 0


def _ntp():
    """懒加载 NTP 模块：不打开校时页时不必把这两个模块编译进内存。"""
    global _ntp_mod
    if _ntp_mod is None:
        import ntpsrv
        import ntpsync
        _ntp_mod = (ntpsrv, ntpsync)
    return _ntp_mod


def _ntp_cfg():
    n = _cfg().get("ntp") or {}
    return (n.get("upstream") or ["ntp.aliyun.com"],
            int(n.get("tz", 8)), int(n.get("sync_ms", 1800000)))


def _save_cfg():
    try:
        import json
        with open("config.json", "w") as f:
            f.write(json.dumps(_cfg()))
        return True
    except Exception as e:
        print("[miniweb] cfg save:", e)
        return False


def _json_ntp():
    """校时页：本机服务状态 + 上游配置 + 同步记录。"""
    import json
    ntpsrv, ntpsync = _ntp()
    up, tz, sync_ms = _ntp_cfg()
    try:
        ip = _wlan().ifconfig()[0]
    except Exception:
        ip = "—"
    return json.dumps({
        "ok": 1,
        "local": {"running": ntpsrv.is_running(), "port": 123, "ip": ip,
                  "served": ntpsrv.stat["served"],
                  "last_client": ntpsrv.stat["last_client"],
                  "last_at": ntpsrv.stat["last_at"],
                  "err": ntpsrv.stat["err"]},
        "upstream": up,
        "sync_ms": sync_ms,
        "tz": tz,
        "sync": ntpsync.snapshot(),
    })


def _read_body(c, req):
    """取 POST 请求体：内容可能在首次 recv 里，也可能还要再读。"""
    i = req.find(b"\r\n\r\n")
    if i < 0:
        return b""
    n = 0
    for line in req[:i].decode("utf-8", "replace").split("\r\n"):
        if line.lower().startswith("content-length:"):
            try:
                n = int(line.split(":", 1)[1].strip())
            except Exception:
                n = 0
            break
    if n <= 0:
        return b""
    body = req[i + 4:]
    while len(body) < n:
        try:
            d = c.recv(min(512, n - len(body)))
        except Exception:
            break
        if not d:
            break
        body += d
    return body[:n]


def _api_ntp_post(c, req):
    """NTP 写操作：立即同步 / 启停本机服务 / 改上游 / 改间隔 / 改时区。

    同步本身要联网，最慢 4 秒，所以这里只排队，由主循环在空闲时执行，
    避免一个「立即同步」把页面里的其它请求全卡住。
    """
    global _ntp_due
    import json
    ntpsrv, _ntpsync = _ntp()
    try:
        d = json.loads(_read_body(c, req) or b"{}")
    except Exception:
        d = {}
    act = str(d.get("action", ""))
    val = str(d.get("value", ""))
    n = _cfg().setdefault("ntp", {})

    if act == "sync":
        _ntp_due = True
        _send(c, "200 OK", "application/json",
              '{"ok":1,"msg":"已排队同步，稍候刷新"}')
    elif act == "srv":
        if val == "off":
            ntpsrv.stop()
            _send(c, "200 OK", "application/json",
                  '{"ok":1,"msg":"本机 NTP 服务已停止"}')
        else:
            ok = ntpsrv.start(123)
            _send(c, "200 OK", "application/json",
                  '{"ok":%d,"msg":"%s"}' % (
                      1 if ok else 0,
                      "本机 NTP 服务已启动" if ok else "启动失败（123 端口被占？）"))
    elif act == "upstream":
        hosts = [h.strip() for h in val.replace(" ", ",").split(",") if h.strip()]
        if not hosts:
            _send(c, "200 OK", "application/json",
                  '{"ok":0,"msg":"上游列表不能为空"}')
            return
        for h in hosts:
            for ch in h:
                if not (ch.isalnum() or ch in ".:-"):
                    _send(c, "200 OK", "application/json",
                          '{"ok":0,"msg":"地址格式不合法"}')
                    return
        n["upstream"] = hosts
        if _save_cfg():
            _ntp_due = True
            _send(c, "200 OK", "application/json",
                  '{"ok":1,"msg":"上游已保存，正在重新同步"}')
        else:
            _send(c, "200 OK", "application/json",
                  '{"ok":0,"msg":"写入失败"}')
    elif act == "interval":
        if not val.isdigit() or int(val) < 60:
            _send(c, "200 OK", "application/json",
                  '{"ok":0,"msg":"间隔至少 60 秒"}')
            return
        n["sync_ms"] = int(val) * 1000
        _send(c, "200 OK", "application/json",
              '{"ok":1,"msg":"同步间隔已保存"}' if _save_cfg()
              else '{"ok":0,"msg":"写入失败"}')
    elif act == "tz":
        try:
            tz = int(val)
        except Exception:
            tz = 999
        if tz < -12 or tz > 14:
            _send(c, "200 OK", "application/json",
                  '{"ok":0,"msg":"时区必须是 -12 ~ 14 的整数"}')
            return
        n["tz"] = tz
        ntpsrv.set_tz(tz * 3600)
        if _save_cfg():
            _ntp_due = True
            _send(c, "200 OK", "application/json",
                  '{"ok":1,"msg":"时区已保存，正在重新同步"}')
        else:
            _send(c, "200 OK", "application/json",
                  '{"ok":0,"msg":"写入失败"}')
    else:
        _send(c, "200 OK", "application/json",
              '{"ok":0,"msg":"不支持的动作"}')


def _ntp_tick(idle):
    """主循环里的 NTP 维护：每轮收 UDP 包，空闲时才做联网同步。"""
    global _ntp_due, _ntp_last
    ntpsrv, ntpsync = _ntp()
    ntpsrv.serve()
    if not idle:
        return
    _up, tz, sync_ms = _ntp_cfg()
    now = time.ticks_ms()
    if not _ntp_due and 0 <= time.ticks_diff(now, _ntp_last) < sync_ms:
        return
    _ntp_due = False
    _ntp_last = now
    try:
        ntpsync.sync(_ntp_cfg()[0], tz_off=tz * 3600, timeout=4)
    except Exception as e:
        print("[miniweb] ntp:", e)


_rap_obj = None


def _rap():
    """懒加载路由器客户端：只有真的取天气时才把它编译进内存。"""
    global _rap_obj
    if _rap_obj is None:
        import rap
        host, p80, _p2, _pu = _router_cfg()
        _rap_obj = rap.RouterAPI(host, p80, timeout=1.5)
    return _rap_obj


def _api_weather(c, force=False):
    """天气聚合：force 或文件过旧时先清路由器 CGI 缓存，再重新拉取。

    路由器那条 /cgi-bin/weather 自己缓存约 3 小时：板子只重取本地文件的话，
    拿到的一直是同一份旧数据。所以超过 _WX_STALE 秒就发一次 ?a=flush，
    让它向上游重取；失败不报错，退回板上已有文件。
    """
    stale = True
    try:
        mtime = os.stat("/weather.json")[8]          # 2000 纪元秒
        stale = time.time() - mtime > _WX_STALE
    except OSError:
        stale = True

    try:
        api = _rap()
        if force or stale:
            try:
                api.get_json("/cgi-bin/weather?a=flush", 2048)
            except Exception as e:
                print("[miniweb] wx flush:", e)
            api.drop("/cgi-bin/weather?a=data")
        api.download_cached("/cgi-bin/weather?a=data", "/weather.json",
                            600000, limit=65536, dl=4000)
    except Exception as e:
        print("[miniweb] weather:", e)

    if not _send_root(c, "/weather.json", "application/json"):
        _send(c, "200 OK", "application/json",
              '{"ok":0,"msg":"天气数据不可用"}')


def _api_channels(c):
    """频道库只在文件缺失时才拉：CGI 要 1.6~3.1 秒，不能挂在每次请求上。"""
    try:
        os.stat("/channels.json")
    except OSError:
        try:
            _rap().download_cached("/cgi-bin/iptv?a=list", "/channels.json",
                                   43200000, limit=65536, dl=4000)
        except Exception as e:
            print("[miniweb] channels:", e)
    if not _send_root(c, "/channels.json", "application/json"):
        _send(c, "200 OK", "application/json", '{"ok":0,"msg":"频道库不可用"}')


# ---------------------------------------------------------------- 响应构造
def _send_head(c, code, ctype, length, extra=""):
    c.send(("HTTP/1.1 %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
            "Cache-Control: no-store\r\nConnection: close\r\n%s\r\n"
            % (code, ctype, length, extra)).encode())


def _send(c, code, ctype, body, extra=""):
    if isinstance(body, str):
        body = body.encode()
    _send_head(c, code, ctype, len(body), extra)
    if body:
        c.send(body)


def _send_file(c, urlpath):
    """把 URL 路径映射到 /www/ 下的文件，优先发同名 .gz。

    分块读取：旧板堆只有几十 KB，整文件读进内存会把 lwIP 的接收缓冲挤掉。
    """
    name = urlpath.lstrip("/")
    if not name or ".." in name:
        return False
    for cand, enc in ((name + ".gz", "gzip"), (name, "")):
        fp = "/www/" + cand
        try:
            size = os.stat(fp)[6]
        except OSError:
            continue
        _send_head(c, "200 OK", _ctype(name), size,
                   "Content-Encoding: gzip\r\n" if enc else "")
        f = open(fp, "rb")
        try:
            while True:
                b = f.read(_CHUNK)
                if not b:
                    break
                c.send(b)
        finally:
            f.close()
        return True
    return False


def _send_root(c, path, ctype):
    """发根目录下的文件（数据 JSON 都在根目录），同样分块读取。"""
    try:
        size = os.stat(path)[6]
    except OSError:
        return False
    _send_head(c, "200 OK", ctype, size)
    f = open(path, "rb")
    try:
        while True:
            b = f.read(_CHUNK)
            if not b:
                break
            c.send(b)
    finally:
        f.close()
    return True


# ---------------------------------------------------------------- 路由
def _handle(c, req):
    global _hits
    # 只取请求行和 body 长度，够用了
    try:
        line = req.split(b"\r\n", 1)[0].decode("utf-8", "replace")
        parts = line.split(" ")
        method = parts[0] if parts else "GET"
        path = parts[1] if len(parts) > 1 else "/"
    except Exception:
        method, path = "GET", "/"

    query = ""
    if "?" in path:
        path, query = path.split("?", 1)

    _hits += 1

    # 静态页优先：页面路径本身交给 /www/，入口页与接口单独处理
    if path == "/" or path == "/index.html":
        # 首页用构建好的整页（与子页面共用同一套样式与脚本）；
        # 文件缺失时退回内嵌的极简页，至少还能进各个子页面。
        if not _send_file(c, "/index.html"):
            _send(c, "200 OK", "text/html; charset=utf-8", _page())
    elif path == "/api/health" or path == "/api/home":
        _send(c, "200 OK", "application/json", _json_health())
    elif path == "/api/status":
        _send(c, "200 OK", "application/json", _json_status())
    elif path == "/api/info":
        _send(c, "200 OK", "application/json", _json_info())
    elif path == "/api/ntp":
        if method == "POST":
            _api_ntp_post(c, req)
        else:
            _send(c, "200 OK", "application/json", _json_ntp())
    elif path == "/api/weather":
        _api_weather(c, "force=1" in query)
    elif path == "/api/weatherhist":
        # 历史数据要串行向上游取 7 天，精简版只发板上已有文件
        if not _send_root(c, "/whist.json", "application/json"):
            _send(c, "200 OK", "application/json",
                  '{"ok":0,"pending":1,"msg":"历史数据未就绪"}')
    elif path == "/api/channels":
        _api_channels(c)
    elif path == "/api/calendar":
        _send(c, "200 OK", "application/json",
              '{"ok":0,"msg":"精简版不提供历法"}')
    elif path == "/api/quote" or path == "/api/watch":
        _send(c, "200 OK", "application/json",
              '{"ok":0,"msg":"精简版未接入行情"}')
    elif path == "/ping":
        _send(c, "200 OK", "text/plain", "pong")
    elif path == "/reboot":
        _send(c, "200 OK", "text/html; charset=utf-8",
              "<meta charset='utf-8'><body style='font-family:sans-serif;"
              "padding:40px;text-align:center'><h2>正在重启…</h2>"
              "<p>约 8 秒后重新访问</p>")
        time.sleep_ms(300)
        if _HAS_MACHINE:
            machine.reset()
    elif path == "/favicon.ico":
        _send(c, "204 No Content", "text/plain", b"")
    elif _send_file(c, path):
        pass
    else:
        _send(c, "404 Not Found", "text/html; charset=utf-8",
              "<meta charset='utf-8'><body style='font-family:sans-serif;"
              "padding:40px;text-align:center'><h2>404</h2>"
              "<p><a href='/'>返回首页</a></p>")


# ---------------------------------------------------------------- 主循环
def run(port=PORT):
    global _hits
    if not _ensure_wifi():
        print("[miniweb] WiFi 未连接，仍启动服务（仅本机可达）")

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("0.0.0.0", port))
    except OSError as e:
        print("[miniweb] bind 失败:", e)
        raise
    srv.listen(5)
    srv.setblocking(False)

    # NTP：本机 UDP 123 服务 + 首次同步交给主循环空闲时做
    try:
        _ntpsrv, _nsync = _ntp()
        _ntpsrv.set_tz(_ntp_cfg()[1] * 3600)
        _ntpsrv.start(123)
    except Exception as e:
        print("[miniweb] ntpsrv:", e)

    ip = _info()[0]
    print("[miniweb] http://%s:%d/  ready" % (ip, port))

    n = 0
    while True:
        try:
            # 关键：带超时的等待，绝不无限阻塞
            r, _, _ = select.select([srv], [], [], 0.3)
        except Exception as e:
            print("[miniweb] select:", e)
            time.sleep_ms(100)
            continue

        if not r:
            # 空闲：收 UDP 校时包，需要的话做一次联网同步
            try:
                _ntp_tick(True)
            except Exception as e:
                print("[miniweb] ntp tick:", e)
            continue

        # 有连接待处理：只收 UDP，不同步，避免把请求拖住
        try:
            _ntp_tick(False)
        except Exception:
            pass

        try:
            c, addr = srv.accept()
        except Exception:
            continue              # 非阻塞下 EAGAIN 属正常

        try:
            c.settimeout(0.6)
            try:
                req = c.recv(768)
            except Exception:
                req = b""
            if req:
                _handle(c, req)
        except Exception as e:
            print("[miniweb] handle:", e)
        finally:
            try:
                c.close()
            except Exception:
                pass

        n += 1
        if n % 20 == 0:           # 定期回收，防碎片
            gc.collect()


if __name__ == "__main__":
    run()
