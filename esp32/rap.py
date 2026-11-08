# -*- coding: utf-8 -*-
"""路由器 CGI 客户端。

实测（nginx/1.12.2 + CGI）两条坑：

1. 响应是 **chunked** 编码，body 里夹着 `1a2b\\r\\n` 这样的块长度行，
   直接 json.loads 必然报语法错。必须按块解出来。
2. 天气聚合体接近 40KB。用 `buf += chunk` 反复拼接会把堆打散，
   在 80KB 空闲的 ESP32 上表现为 MemoryError。所以分两条通路：
   - 小 JSON（状态/流量/频道/NTP）：预分配 bytearray，一次性读入
   - 大 JSON（天气）：边收边写 flash 文件，常驻内存只有 1KB

不用 urequests：各固件对它的支持不一致，手写 socket 反而可控。
"""

import socket
import time
import gc


class _Reader:
    """带缓冲的 socket 读取器：readline / readinto（定长）/ readsome（尽量）。

    yield_cb 是「等路由器 CGI 回数据时顺手去处理 HTTP 请求」的回调。
    没有它，一次 2 秒的 CGI 就会让整个 Web 服务停摆 2 秒。
    """

    __slots__ = ("s", "buf", "yield_cb")

    def __init__(self, s, buf=b"", yield_cb=None):
        self.s = s
        self.buf = buf
        self.yield_cb = yield_cb

    def _yield(self):
        if self.yield_cb:
            try:
                self.yield_cb()
            except Exception:
                pass

    def _more(self):
        # 路由器 CGI 首块数据可能要 1 秒以上才回。以前 socket 超时 2 秒，
        # 这一次 recv 就把单线程主循环整个锁住：80 端口没人 accept，
        # 页面和 ping 一起失联。现在每次最多等 0.5 秒，超时先 yield
        # 处理 HTTP，再接着等，总上限 4 秒。
        deadline = time.ticks_add(time.ticks_ms(), 4000)
        while True:
            self._yield()
            try:
                c = self.s.recv(512)
            except OSError:
                if time.ticks_diff(time.ticks_ms(), deadline) >= 0:
                    raise
                continue
            if not c:
                raise OSError("eof")
            self.buf += c
            return

    def readline(self):
        while True:
            i = self.buf.find(b"\r\n")
            if i >= 0:
                line = self.buf[:i]
                self.buf = self.buf[i + 2:]
                return line
            self._more()

    def readinto(self, mv):
        n = 0
        want = len(mv)
        while n < want:
            if not self.buf:
                self._more()
            k = min(len(self.buf), want - n)
            mv[n:n + k] = self.buf[:k]
            self.buf = self.buf[k:]
            n += k
        return n

    def readsome(self, mv):
        if not self.buf:
            self._yield()
            try:
                c = self.s.recv(512)
            except OSError:
                return 0          # 这次没等到，交给下一轮，别阻塞主循环
            if not c:
                return 0
            self.buf = c
        k = min(len(self.buf), len(mv))
        mv[:k] = self.buf[:k]
        self.buf = self.buf[k:]
        return k


class RouterAPI:
    def __init__(self, host, port=80, timeout=2.0, yield_cb=None):
        # 局域网 CGI 正常几十毫秒就回。超时越短，单线程主循环被一次
        # 卡住的请求拖住的时间越短（4s 时实测会把 HTTP backlog 撑满）。
        # yield_cb 让等待期间也能处理 HTTP 请求，进一步消除这个空窗。
        self.host = host
        self.port = port
        self.timeout = timeout
        self.yield_cb = yield_cb
        self._cache = {}          # path -> (ticks_ms, value)
        self.last_ok = 0
        self.last_err = ""
        self.fail_count = 0

    # ---------------- 底层 ----------------
    def _begin(self, path):
        """发请求、读响应头。返回 (socket, _Reader, chunked, content_len)。"""
        if self.yield_cb:
            try:
                self.yield_cb()
            except Exception:
                pass
        # 纯 IP 绝不走 getaddrinfo：WiFi 刚连上时 LWIP 的 netconn 还没缓过来，
        # 解析一个数字地址都可能阻塞十几秒（C 层，Ctrl-C 都打不断）。
        # 主循环第一次 poll_status 就会调到这里，一卡就整个 Web 服务失联。
        if self.host and self.host.replace(".", "").isdigit():
            addr = (self.host, self.port)
        else:
            addr = socket.getaddrinfo(self.host, self.port)[0][-1]
        s = socket.socket()
        # connect 允许 0.5s（局域网正常几十毫秒），但**读数据阶段**要更短：
        # 每次 recv 阻塞多久，主循环就多久不响应 HTTP。0.5s 意味着切页要等
        # 半秒，0.12s 则基本无感——代价是超时后多转几圈，CPU 开销可忽略。
        s.settimeout(0.5)
        try:
            s.connect(addr)
            s.send(("GET %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n"
                    "Accept-Encoding: identity\r\n"
                    "User-Agent: esp32-terminal\r\n\r\n" % (path, self.host)).encode())
            s.settimeout(0.12)
            head = b""
            deadline = time.ticks_add(time.ticks_ms(), 4000)
            while b"\r\n\r\n" not in head:
                if self.yield_cb:
                    try:
                        self.yield_cb()
                    except Exception:
                        pass
                try:
                    c = s.recv(256)
                except OSError:
                    if time.ticks_diff(time.ticks_ms(), deadline) >= 0:
                        raise
                    continue
                if not c:
                    break
                head += c
                if len(head) > 2048:
                    break
        except Exception:
            s.close()
            self.fail_count += 1
            self.last_err = "connect"
            raise OSError("connect")

        head, _, rest = head.partition(b"\r\n\r\n")
        first = head.split(b"\r\n", 1)[0]
        if b" 200 " not in first:
            s.close()
            self.fail_count += 1
            self.last_err = first.decode()[:40]
            raise OSError("http")

        low = head.lower()
        chunked = b"chunked" in low
        clen = 0
        if not chunked:
            for line in low.split(b"\r\n"):
                if line.startswith(b"content-length:"):
                    try:
                        clen = int(line[15:].strip())
                    except Exception:
                        clen = 0

        self.fail_count = 0
        self.last_ok = time.ticks_ms()
        return s, _Reader(s, rest, self.yield_cb), chunked, clen

    # ---------------- 小响应：读进内存 ----------------
    def get(self, path, limit=24576):
        """返回 body 的 bytes。超过 limit 抛 OSError("oversize")。"""
        s = None
        try:
            s, r, chunked, clen = self._begin(path)
            buf = bytearray(limit)
            pos = 0
            if chunked:
                while True:
                    line = r.readline()
                    try:
                        n = int(line.split(b";")[0], 16)
                    except Exception:
                        break
                    if n == 0:
                        break
                    if pos + n > limit:
                        self.last_err = "oversize"
                        raise OSError("oversize")
                    r.readinto(memoryview(buf)[pos:pos + n])
                    pos += n
                    r.readline()          # 块尾 CRLF
            else:
                need = clen if clen else limit
                if need > limit:
                    need = limit
                pos = r.readinto(memoryview(buf)[:need])
            return bytes(buf[:pos])
        finally:
            if s:
                try:
                    s.close()
                except Exception:
                    pass

    # ---------------- 大响应：流式落盘 ----------------
    def download(self, path, filename, limit=262144):
        """把 body 直接写到 filename，内存占用恒定 1KB。

        先写 .tmp 再改名，避免半截文件被当成有效缓存。
        """
        import os
        tmp = filename + ".tmp"
        s = None
        f = None
        total = 0
        try:
            s, r, chunked, clen = self._begin(path)
            f = open(tmp, "wb")
            chunk = bytearray(1024)
            mv = memoryview(chunk)
            if chunked:
                while True:
                    line = r.readline()
                    try:
                        n = int(line.split(b";")[0], 16)
                    except Exception:
                        break
                    if n == 0:
                        break
                    if total + n > limit:
                        raise OSError("oversize")
                    while n:
                        k = min(n, len(chunk))
                        r.readinto(mv[:k])
                        f.write(mv[:k])
                        n -= k
                        total += k
                    r.readline()
            elif clen:
                while total < clen:
                    k = r.readsome(mv[:min(clen - total, len(chunk))])
                    if k <= 0:
                        break
                    f.write(mv[:k])
                    total += k
            else:
                while True:
                    k = r.readsome(mv)
                    if k <= 0:
                        break
                    f.write(mv[:k])
                    total += k
            f.close()
            f = None
            if total <= 0:
                raise OSError("empty")
            # 先试 rename 覆盖（littlefs 支持 POSIX 语义）；只有不支持的文件
            # 系统才退化成「先删后改名」。少了这个 remove，就不会出现
            # 「文件已删、新文件还没就位」的空窗——期间并发读会拿到 404。
            try:
                os.rename(tmp, filename)
            except OSError:
                try:
                    os.remove(filename)
                except Exception:
                    pass
                os.rename(tmp, filename)
            return total
        except Exception:
            if f:
                try:
                    f.close()
                except Exception:
                    pass
            try:
                os.remove(tmp)
            except Exception:
                pass
            raise
        finally:
            if s:
                try:
                    s.close()
                except Exception:
                    pass

    # ---------------- 解析型（小 JSON） ----------------
    def get_json(self, path, limit=8192):
        import json
        raw = self.get(path, limit)
        try:
            return json.loads(raw)
        except Exception as e:
            self.last_err = "json:%s" % e
            raise OSError("bad json")
        finally:
            del raw
            gc.collect()

    def cached_json(self, path, ttl_ms, limit=8192):
        hit = self._cache.get(path)
        now = time.ticks_ms()
        if hit and ttl_ms > 0:
            age = time.ticks_diff(now, hit[0])
            if 0 <= age < ttl_ms:
                return hit[1]
        try:
            val = self.get_json(path, limit)
        except OSError:
            if hit:
                return hit[1]      # 拉取失败退回旧值，页面不至于空白
            raise
        self._cache[path] = (now, val)
        return val

    # ---------------- 透传型（中等 JSON，交给浏览器） ----------------
    def cached_raw(self, path, ttl_ms, limit=24576):
        hit = self._cache.get(path)
        now = time.ticks_ms()
        if hit and ttl_ms > 0:
            age = time.ticks_diff(now, hit[0])
            if 0 <= age < ttl_ms:
                return hit[1]
        try:
            body = self.get(path, limit)
        except OSError:
            if hit:
                return hit[1]
            raise
        self._cache.pop(path, None)   # 先清旧值再存新值，避免峰值持有两份
        gc.collect()
        self._cache[path] = (now, body)
        return body

    # ---------------- 落盘型（大 JSON） ----------------
    @staticmethod
    def _fs_ok(filename):
        """缓存的落盘文件是否还真的在。

        只比内存里的字节数是不够的：flash 上的文件可能被删、断电丢、
        或者刚好处在上一次 rename 的空窗期。那种时候缓存「命中」只会
        返回一个长度，读文件却 404（现象是页面一直说数据不可用）。
        """
        import os
        try:
            return os.stat(filename)[6] > 0
        except OSError:
            return False

    def download_cached(self, path, filename, ttl_ms, limit=262144):
        hit = self._cache.get(path)
        now = time.ticks_ms()
        if hit and ttl_ms > 0:
            age = time.ticks_diff(now, hit[0])
            if 0 <= age < ttl_ms and self._fs_ok(filename):
                return hit[1]
        try:
            n = self.download(path, filename, limit)
        except OSError:
            if hit and self._fs_ok(filename):
                return hit[1]
            raise
        self._cache[path] = (now, n)
        return n

    def drop(self, path=None):
        if path:
            self._cache.pop(path, None)
        else:
            self._cache = {}
        gc.collect()


# ---------------------------------------------------------------- 轻量外网 GET
# 只干一件事：取一行行情文本（实测 173 字节 / 60ms）。
#
# 为什么不开 HTTPS：ESP32 上 mbedtls 握手要几十 KB 堆，一次就能把只剩
# 40KB 的板子打崩；行情站点同时提供 http，就没必要冒这个险。
# 为什么解析不用 decode()：返回体里第一个字段是 GBK 中文名，整串 decode
# 会抛 UnicodeError。这里按字节 split，只取纯数字字段，名称由配置给。
_q_ip = None


def _q_date(f):
    """从字段里倒着找出日期字段（YYYY-MM-DD），找不到返回空串。

    A 股与外汇的日期位置不同，而且外汇尾部有时多一个空字段，
    按「形状」找比硬编码下标可靠。
    """
    i = len(f) - 1
    while i >= 0:
        t = f[i]
        if len(t) == 10 and t[4:5] == b"-" and t[7:8] == b"-":
            return t.decode()
        i -= 1
    return ""


def _q_one(code, f):
    """把一条 `var hq_str_xxx="..."` 的字段解析成 dict，解析不了返回 None。

    两套格式（实测 2026-11）：
      A 股 sh/sz/bj : 名称,今开,昨收,现价,最高,最低,...,[30]日期,[31]时间
      外汇 fx_xxx   : [0]时间,[8]现价,[10]涨跌幅%,[11]涨跌额,...,日期
    外汇自带涨跌幅，A 股要自己按昨收算，所以分开处理。
    """
    n = len(f)
    if code[:3] == "fx_":
        if n < 12:
            return None
        try:
            price = float(f[8])
            pct = float(f[10])
            chg = float(f[11])
        except Exception:
            return None
        if price <= 0:
            return None
        return {"code": code, "price": price, "chg": chg, "pct": pct,
                "time": f[0].decode()[:5], "date": _q_date(f)}

    if n < 32:
        return None
    try:
        cur = float(f[3].decode())
        prev = float(f[2].decode())
    except Exception:
        return None
    if not prev:
        return None
    if cur <= 0:          # 长期停牌时现价为 0，直接当持平，别算出 -100%
        cur, chg = prev, 0.0
    else:
        chg = cur - prev
    return {"code": code, "price": cur, "chg": chg, "pct": chg * 100.0 / prev,
            "time": f[31].decode()[:5], "date": f[30].decode(),
            # 名称是 GBK 字节，固件里没有 gbk 编码表；hex 出去让浏览器用
            # TextDecoder("gbk") 还原（自选股要显示中文名）。
            "n": f[0].hex()}


def sina_quotes(codes, timeout=3.0):
    """一次取多个标的，返回 list（顺序与 codes 一致，取不到的标的不出现在结果里）。

    为什么批量：新浪支持 /list=a,b,c 用逗号连查，6 个标的只花一次 TCP 往返、
    约 1.2KB 响应；逐个查要 6 次握手，单线程主循环根本扛不住。

    只缓存 IP，不缓存任何行情数据（缓存与 TTL 由 main.py 管）。
    """
    global _q_ip
    if _q_ip is None:
        # 必须挑 AF_INET 那一条：有的解析器先返回 IPv6 的 4 元组，
        # 拿它去 connect 一个 AF_INET socket 会直接 TypeError。
        infos = socket.getaddrinfo("hq.sinajs.cn", 80)
        for af, _st, _pr, _nm, sa in infos:
            if af == socket.AF_INET:
                _q_ip = sa
                break
        if _q_ip is None:
            _q_ip = infos[0][-1]

    s = socket.socket()
    s.settimeout(timeout)
    buf = b""
    try:
        try:
            s.connect(_q_ip)
        except OSError:
            # IP 可能变了：丢掉缓存，下次重新解析
            _q_ip = None
            raise
        # 注意：新浪这个接口的路径是 /list=代码（等号，不是斜杠），
        # 写成 /list/code 会返回 404。
        s.send(b"GET /list=" + ",".join(codes).encode() + b" HTTP/1.0\r\n"
               b"Host: hq.sinajs.cn\r\n"
               b"Referer: https://finance.sina.com.cn\r\n"
               b"User-Agent: esp32-terminal\r\n\r\n")
        # 缓冲按标的数估：A 股一条约 200B，外汇约 230B，再留 200B 头部。
        cap = 200 + len(codes) * 260
        while len(buf) < cap:
            c = s.recv(256)
            if not c:
                break
            buf += c
            if buf.count(b'";') >= len(codes):
                break
    finally:
        try:
            s.close()
        except Exception:
            pass

    out = []
    pos = 0
    while True:
        k = buf.find(b"var hq_str_", pos)
        if k < 0:
            break
        eq = buf.find(b"=", k + 11)
        if eq < 0:
            break
        code = buf[k + 11:eq].decode()
        q1 = buf.find(b'"', eq)
        if q1 < 0:
            break
        q2 = buf.find(b'"', q1 + 1)
        if q2 < 0:
            break
        pos = q2 + 1
        d = _q_one(code, buf[q1 + 1:q2].split(b","))
        if d:
            out.append(d)
    if not out:
        raise OSError("quote")
    return out


def sina_quote(code, timeout=3.0):
    """单标的兼容封装：返回 {price, chg, pct, date, time}，失败抛 OSError。"""
    r = sina_quotes([code], timeout)
    d = r[0]
    d.pop("code", None)
    return d
