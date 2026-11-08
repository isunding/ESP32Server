# -*- coding: utf-8 -*-
"""WiFi 连接与热点回退。

策略：连不上就开热点，人在手机上连热点改配置，改完重启即可。
"""

import network
import time
import gc


class Net:
    def __init__(self, ssid, password, hostname="server",
                 ap_ssid="ESP32-Setup", ap_pass="esp32setup123"):
        self.ssid = ssid
        self.password = password
        self.hostname = hostname
        self.ap_ssid = ap_ssid
        self.ap_pass = ap_pass
        self.sta = None
        self.ap = None

    def attach(self):
        """接管 boot.py 已初始化好的 STA 单例，绝不重复 active(True)。"""
        try:
            self.sta = network.WLAN(network.STA_IF)
        except Exception as e:
            print("[wifi] attach fail:", e)
            self.sta = None
        return self.sta

    def begin(self):
        """只发起一次连接：不等待、不 disconnect、不阻塞主循环。

        驱动正在自动关联时 disconnect() 会把它打断，反而永远连不上。
        connect() 抛 "Wifi Internal Error" 也忽略，关联仍在继续。
        """
        if self.is_up():
            return True
        if not self.sta:
            self.attach()
        if not self.sta:
            return False
        try:
            self.sta.connect(self.ssid, self.password)
        except Exception as e:
            print("[wifi] connect() 非致命:", e)
        return False

    def connect(self, timeout=15, yield_cb=None):
        try:
            network.hostname(self.hostname)
        except Exception:
            pass
        gc.collect()
        # 冷启动偶发 "WiFi Out of Memory / Failed to deinit Wi-Fi driver"：
        # 上一次的 WiFi 缓冲区还没释放干净，连 WLAN() 构造都可能抛异常。
        # 以前这里一抛异常程序就崩到 REPL，80 端口永远起不来，表现就是
        # 「断电重启后彻底卡死」。回收堆后重试几次，仍不行就交给上层开 AP
        # （页面至少还能访问），绝不把异常抛穿主程序。
        self.sta = None
        try:
            self.sta = network.WLAN(network.STA_IF)
            # boot.py 已在堆最空时把驱动拉起，这里别再 active(True) 一次：
            # 重复初始化可能触发 ESP-IDF 的 deinit/reinit 路径又吃掉 buffer。
            if not self.sta.active():
                self.sta.active(True)
        except OSError as e:
            # 不要在原地反复重试：ESP-IDF 的静态 rx buffer 只会越试越少
            # （实测 4 -> 0 -> 0）。交给上层复位重试，绝不把异常抛穿主程序。
            print("[wifi] 驱动初始化失败:", e)
            self.sta = None
            return False
        try:
            if self.sta.isconnected():
                return True
        except OSError:
            pass
        try:
            self.sta.connect(self.ssid, self.password)
        except Exception as e:
            # "Wifi Internal Error" 常见于驱动已带着 NVS 凭据自动重连、
            # 此处又调用一次 connect()。该异常不致命，关联仍会继续，
            # 只需继续等待 isconnected()。若此处返回失败，上层会改为
            # 开启 AP，混合模式占用额外内存，可能导致 bind 失败。
            print("[wifi] connect() 非致命，继续等待:", e)
            gc.collect()
        t0 = time.ticks_ms()
        while not self.sta.isconnected():
            if time.ticks_diff(time.ticks_ms(), t0) > timeout * 1000:
                return False
            # 等 DHCP 期间顺手处理 HTTP，别让单线程主循环整体停摆
            if yield_cb:
                try:
                    yield_cb()
                except Exception:
                    pass
            time.sleep_ms(250)
        gc.collect()
        return True

    def start_ap(self):
        try:
            self.ap = network.WLAN(network.AP_IF)
            self.ap.active(True)
            try:
                self.ap.config(essid=self.ap_ssid, password=self.ap_pass,
                               authmode=network.AUTH_WPA_WPA2_PSK)
            except Exception:
                try:
                    self.ap.config(essid=self.ap_ssid)
                except Exception:
                    pass
            print("[wifi] AP mode:", self.ap_ssid)
        except Exception as e:
            # WiFi 驱动整体不可用时开 AP 也会 OOM，不能让它把程序崩到 REPL
            print("[wifi] AP 启动失败:", e)
            self.ap = None

    def stop_ap(self):
        if self.ap:
            try:
                self.ap.active(False)
            except Exception:
                pass
            self.ap = None

    def ip(self):
        try:
            if self.ap and self.ap.active():
                return self.ap.ifconfig()[0]
        except Exception:
            pass
        try:
            if self.sta and self.sta.isconnected():
                return self.sta.ifconfig()[0]
        except Exception:
            pass
        return "0.0.0.0"

    def rssi(self):
        try:
            if self.sta and self.sta.isconnected():
                return self.sta.status("rssi")
        except Exception:
            pass
        return 0

    def is_up(self):
        try:
            return bool(self.sta and self.sta.isconnected())
        except Exception:
            return False

    def reconnect(self, yield_cb=None):
        """断线后重连，返回是否成功。等待期间同样协作式让出。"""
        if self.is_up():
            return True
        try:
            if self.sta:
                self.sta.disconnect()
        except Exception:
            pass
        time.sleep_ms(300)
        return self.connect(timeout=10, yield_cb=yield_cb)
