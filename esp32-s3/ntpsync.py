# -*- coding: utf-8 -*-
"""向公网 NTP 服务器取时，并把结果做成 ESP32 本地 NTP 服务的锚点。

自己发 SNTP 包而不是用 ntptime：这样能拿到毫秒级的往返延迟，
并把「服务端发送时刻 + 半个 RTT」作为锚点写入 ntpsrv 供其对局域网应答。

全程整数运算：ESP32 的 MicroPython 是 float32，epoch 塞进 float 会丢精度
（1.79e9 量级下精度只有约 128 秒）。

另外这里必须「协作式」等待：主循环是单线程，等 NTP 回复的 3 秒里若不
回头处理 HTTP，页面就会整个卡住。所以 recvfrom 用 20ms 短超时轮询，
每轮调一次 yield_cb（通常是 web.poll），不用 select（ESP32 上对 UDP
socket 表现不稳定）。
"""

import socket
import struct
import time
import machine

import ntpsrv

NTP_DELTA = 2208988800   # UNIX(1970) -> NTP(1900)
MP_DELTA = 946684800     # UNIX(1970) -> MicroPython(2000)

MAX_HIST = 5

# 直连 IP 时同步记录仍显示熟悉的域名，页面上看不出差别
IP_ALIAS = {"203.107.6.88": "ntp.aliyun.com",
            "120.25.115.20": "ntp1.aliyun.com",
            "10.0.0.1": "路由器 10.0.0.1"}

last = None
history = []


def _addr(host):
    """统一先解析成数值地址再 sendto。

    纯 IP 会命中 LWIP 的数字地址快速路径（不查 DNS，微秒级）；域名则
    可能卡在 DNS 重试里十几到几十秒，而且是 C 层阻塞，Ctrl-C 都打不断。
    反过来，若把字符串 IP 直接交给 sendto，ESP32 上它仍可能再走一次
    主机名解析——所以不能图省事跳过 getaddrinfo。

    解析失败标记成 dns:，让 sync() 立刻短路，别把几个域名挨个试一遍。
    """
    # 纯数字 IP 直接返回：启动初期 LWIP 的 PCB / netconn 吃紧时，
    # 连 getaddrinfo 的数字快速路径都会抛 -203。首次校准走的是
    # 路由器 10.0.0.1，不能因为这一步失败整个跳过。
    if host and host.replace(".", "").isdigit():
        return (host, 123)
    try:
        return socket.getaddrinfo(host, 123)[0][-1]
    except Exception as e:
        raise OSError("dns:" + repr(e)[:32])


def _yield(cb):
    if cb:
        try:
            cb()
        except Exception:
            pass


def _query(host, timeout, yield_cb=None):
    """发一个 SNTP 请求，返回 (utc_sec, utc_ms, rtt_ms)。失败抛异常。"""
    pkt = bytearray(48)
    pkt[0] = 0x1B  # LI=0 VN=3 Mode=3(client)

    _yield(yield_cb)
    addr = _addr(host)       # 唯一可能长时间阻塞的一步（域名解析）
    _yield(yield_cb)

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0.02)          # 短超时轮询，等待期间可以回头服务 HTTP
    try:
        t0 = time.ticks_ms()
        s.sendto(bytes(pkt), addr)
        data = None
        deadline = int(timeout * 1000)
        while True:
            _yield(yield_cb)
            try:
                data, _ = s.recvfrom(64)
                break
            except OSError:
                pass             # 20ms 超时 / EAGAIN，继续等
            if time.ticks_diff(time.ticks_ms(), t0) >= deadline:
                raise OSError("timeout")
        t3 = time.ticks_ms()
    finally:
        s.close()

    if not data or len(data) < 48:
        raise OSError("short reply")

    secs, frac = struct.unpack("!II", data[40:48])
    sec = secs - NTP_DELTA
    ms = frac * 1000 // 4294967296
    rtt = time.ticks_diff(t3, t0)

    # 服务端时间戳是它发送的时刻，我们收到时又过了约半个 RTT
    ms += rtt // 2
    if ms >= 1000:
        sec += ms // 1000
        ms %= 1000

    return sec, ms, rtt


def _set_rtc(sec, tz_off):
    """把本地时间写进 RTC，供页面上显示（NTP 应答不依赖它）。"""
    try:
        tm = time.gmtime(int(sec) + tz_off - MP_DELTA)
        machine.RTC().datetime((tm[0], tm[1], tm[2], tm[6],
                                tm[3], tm[4], tm[5], 0))
    except Exception:
        pass


def sync(hosts, tz_off=8 * 3600, timeout=4, yield_cb=None, fallback_ips=None):
    """按顺序尝试 hosts，第一个成功即返回。结果 dict，不抛异常。

    fallback_ips 是纯 IP 的兜底上游：DNS 挂掉时用它直连，绕开解析。
    等待期间每 20ms 回调 yield_cb，保证 HTTP 主循环不被锁死。
    """
    global last

    if isinstance(hosts, str):
        hosts = [hosts]
    hosts = [h.strip() for h in (hosts or []) if h and h.strip()]
    if not hosts:
        hosts = ["ntp.aliyun.com"]

    had = ntpsrv.has_anchor()
    old_sec, old_ms, old_ticks = (ntpsrv._anchor[0], ntpsrv._anchor[1],
                                  ntpsrv._anchor[2])

    res = {"ok": False, "host": None, "tried": [], "delay": None,
           "offset": None, "time": None, "first": not had}

    def commit(sec, ms, rtt, host):
        ticks = time.ticks_ms()
        ntpsrv.set_anchor(sec, ms, ticks, tz_off)
        _set_rtc(sec, tz_off)
        res["ok"] = True
        res["host"] = IP_ALIAS.get(host, host)
        res["delay"] = rtt
        res["time"] = ntpsrv.now_local_str()
        if had and old_sec is not None:
            # 两次锚点之间「本应经过」的毫秒 vs 实际差值 = 真实漂移
            expect = old_sec * 1000 + old_ms + time.ticks_diff(ticks, old_ticks)
            res["offset"] = (sec * 1000 + ms) - expect

    dns_broken = False
    t_start = time.ticks_ms()
    for h in hosts:
        # 别让「IP 全挂」时的多个域名 DNS 挨个超时叠加成几十秒
        if time.ticks_diff(time.ticks_ms(), t_start) > 8000:
            res["err"] = res.get("err") or "budget"
            break
        res["tried"].append(h)
        print("[ntp] try", h)
        try:
            sec, ms, rtt = _query(h, timeout, yield_cb)
        except Exception as e:
            res["err"] = repr(e)[:48]
            # DNS 不通时后面几个域名一样不通，别再各等一次，直接走 IP 兜底
            if "dns:" in repr(e) or "dns:" in str(e):
                dns_broken = True
                break
            continue
        commit(sec, ms, rtt, h)
        break

    if not res["ok"] and dns_broken:
        for h in (fallback_ips or []):
            res["tried"].append(h)
            try:
                sec, ms, rtt = _query(h, timeout, yield_cb)
            except Exception as e:
                res["err"] = repr(e)[:48]
                continue
            commit(sec, ms, rtt, h)
            break

    last = res
    history.append({"time": res["time"], "ok": res["ok"], "host": res["host"],
                    "delay": res["delay"], "offset": res["offset"],
                    "first": res["first"], "err": res.get("err")})
    while len(history) > MAX_HIST:
        history.pop(0)

    return res


def snapshot():
    return {
        "last": last,
        "history": history,
        "esp_time": ntpsrv.now_local_str(),
        "calibrated": ntpsrv.has_anchor(),
    }
