# -*- coding: utf-8 -*-
"""ESP32 本地 NTP 服务器（UDP 123）。

两个坑都踩过，所以这里的写法刻意「整数化」：

1) 时间基准不用 RTC：RTC 只有整秒精度，直接读会有最高 1 秒误差。
   改用「同步时刻 + ticks_ms 增量」的锚点，可到毫秒级。
2) ESP32 的 MicroPython 是**单精度浮点**：1.79e9 + 5.0 会直接舍回原值
   （这个量级下 float32 的精度约 128 秒）。所以纪元一律拆成
   「整数秒 + 整数毫秒」存放，绝不把 epoch 塞进 float。
"""

import socket
import struct
import time

NTP_DELTA = 2208988800   # UNIX(1970) -> NTP(1900)
MP_DELTA = 946684800     # UNIX(1970) -> MicroPython(2000)

_sock = None
# [utc_sec, utc_ms, ticks_ms, tz_off]
_anchor = [None, 0, 0, 0]
_tz = 0
stat = {"served": 0, "last_client": None, "last_at": None, "err": None}


# ---------------------------------------------------------------- 时间基准
def set_tz(tz_off):
    """时区与校准状态解耦：RTC 里存的是本地时间，没校准时也要靠它换算 UTC。

    不设的话，未同步期间 now_parts() 会把「本地时间」当成 UTC 返回，
    事件时间戳直接偏 8 小时（踩过一次）。
    """
    global _tz
    _tz = int(tz_off or 0)
    _anchor[3] = _tz


def get_tz():
    return _tz


def set_anchor(utc_sec, utc_ms, ticks, tz_off):
    global _tz
    _anchor[0] = int(utc_sec)
    _anchor[1] = int(utc_ms)
    _anchor[2] = ticks
    _anchor[3] = int(tz_off)
    _tz = _anchor[3]


def has_anchor():
    return _anchor[0] is not None


def now_parts():
    """当前 UTC，返回 (整数秒, 毫秒)。"""
    if _anchor[0] is None:
        # 未校准：RTC 存的是本地时间（_set_rtc 写入的是本地钟），减去时区才是 UTC
        return time.time() + MP_DELTA - _tz, 0
    d = time.ticks_diff(time.ticks_ms(), _anchor[2])
    total = _anchor[1] + d
    return _anchor[0] + total // 1000, total % 1000


def now_local_str():
    try:
        sec, _ms = now_parts()
        if _anchor[0] is None:
            t = time.localtime()
        else:
            t = time.localtime(sec + _anchor[3] - MP_DELTA)
        return "%04d-%02d-%02d %02d:%02d:%02d" % (t[0], t[1], t[2], t[3], t[4], t[5])
    except Exception:
        return None


# ---------------------------------------------------------------- 服务
def is_running():
    return _sock is not None


def start(port=123):
    global _sock
    stop()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port))
        s.setblocking(False)
        _sock = s
        stat["err"] = None
        print("[ntpsrv] listen 0.0.0.0:%d/udp" % port)
        return True
    except Exception as e:
        # 失败也要把刚创建的 socket 关掉：主循环会每 2 秒重试一次，
        # 不关的话 fd 会一路泄漏。
        try:
            s.close()
        except Exception:
            pass
        _sock = None
        stat["err"] = repr(e)[:48]
        print("[ntpsrv] start failed:", e)
        return False


def stop():
    global _sock
    if _sock is not None:
        try:
            _sock.close()
        except Exception:
            pass
        _sock = None


def _stamp(sec, ms):
    """整数秒 + 毫秒 -> 8 字节 NTP 时间戳（全程整数，避免 float32 精度损失）"""
    frac = ms * 4294967296 // 1000
    return struct.pack("!II", (sec + NTP_DELTA) & 0xFFFFFFFF, frac & 0xFFFFFFFF)


def serve():
    """处理一个待处理的请求（非阻塞）。有应答返回 True。"""
    if _sock is None:
        return False
    try:
        data, addr = _sock.recvfrom(64)
    except OSError:
        return False  # EAGAIN：暂时没有数据

    if not data or len(data) < 48:
        return False

    try:
        sec, ms = now_parts()
        synced = has_anchor()

        pkt = bytearray(48)
        # 未校准时按协议置 LI=3、stratum=16，客户端会主动忽略，
        # 别把错误时间散播到局域网。
        pkt[0] = 0x24 if synced else 0xE4             # LI/VN=4/Mode=4(server)
        pkt[1] = 2 if synced else 16                  # stratum
        pkt[2] = data[2] if data[2] else 6            # poll，沿用客户端
        pkt[3] = 0xEC                                 # precision = -20
        pkt[4:8] = struct.pack("!I", 0)               # root delay
        pkt[8:12] = struct.pack("!I", 0x00000010)     # root dispersion
        pkt[12:16] = b"LOCL"                          # reference id
        pkt[16:24] = _stamp(sec, ms)                  # reference timestamp
        pkt[24:32] = data[40:48]                      # origin = 客户端发送戳
        pkt[32:40] = _stamp(sec, ms)                  # receive
        pkt[40:48] = _stamp(*now_parts())             # transmit（再取一次更准）

        _sock.sendto(bytes(pkt), addr)
        stat["served"] += 1
        stat["last_client"] = addr[0]
        t = time.localtime()
        stat["last_at"] = "%02d:%02d:%02d" % (t[3], t[4], t[5])
        return True
    except Exception as e:
        stat["err"] = repr(e)[:48]
        return False
