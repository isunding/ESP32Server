# -*- coding: utf-8 -*-
# boot.py — 上电最先执行，在堆最空的时候把 WiFi 搞定
import gc

gc.collect()
print("[boot] ESP32 Terminal Server bootstrap")

# ESP-IDF 的 WiFi 驱动要一次性申请 10 个静态 rx buffer（约 12KB），
# 扫描/关联还要额外堆。等 main.py 把十几个模块编译进堆之后再做这些事，
# 只剩碎片：实测报 "Expected to init 10 rx buffer, actual is 4 /
# WiFi Out of Memory"，或者驱动起来了却一直关联不上（连 20 秒都没 IP）。
# 所以在这里完成「初始化 + 发起连接 + 等 DHCP」，main.py 只检查结果。
try:
    import network
    import time
    import json

    _sta = network.WLAN(network.STA_IF)
    _sta.active(True)
    # 关闭 WiFi 省电（modem sleep）。ESP32 默认会在空闲后让首个包等 beacon，
    # 表现为每次空闲再访问时的额外延迟；USB 供电不心疼这点功耗。
    try:
        _sta.config(pm=network.WLAN.PM_NONE)
    except Exception:
        pass
    with open("config.json") as _f:
        _wifi = json.load(_f).get("wifi", {})
    if _wifi.get("ssid"):
        try:
            network.hostname(_wifi.get("hostname") or "server")
        except Exception:
            pass
        if not _sta.isconnected():
            try:
                _sta.connect(_wifi["ssid"], _wifi["password"])
            except Exception as e:
                # 驱动已带着 NVS 凭据自动重连时，重复 connect() 会抛
                # "Wifi Internal Error"，关联其实仍在继续，不是致命错误。
                print("[boot] wifi connect:", e)
        _t0 = time.ticks_ms()
        while not _sta.isconnected() and \
                time.ticks_diff(time.ticks_ms(), _t0) < 15000:
            time.sleep_ms(200)
        print("[boot] wifi", _sta.ifconfig()[0], "free=", gc.mem_free())
except Exception as e:
    print("[boot] wifi preinit fail:", e)
