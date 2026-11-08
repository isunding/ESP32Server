# -*- coding: utf-8 -*-
"""极简非阻塞 HTTP 服务，只依赖 MicroPython 内置的 socket。

设计取舍：
- 单线程。每轮 poll() 最多处理若干连接，主循环不会被长连接堵死。
- 静态文件分块发送（1KB），避免把整页读进内存。
- 支持预压缩：存在 xxx.gz 且浏览器接受 gzip 时直接吐 .gz。
"""

import socket
import gc
import time

try:                     # MicroPython：毫秒计数器会回绕，必须用 ticks_diff
    _ticks_ms = time.ticks_ms
    _ticks_diff = time.ticks_diff
except AttributeError:   # PC 上的单测桩
    def _ticks_ms():
        return int(time.monotonic() * 1000)

    def _ticks_diff(a, b):
        return a - b


CTYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class Req:
    __slots__ = ("method", "path", "query", "headers", "body")

    def __init__(self, method, path, query, headers, body):
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body

    def arg(self, key, default=""):
        for kv in self.query.split("&"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                if k == key:
                    return v
        return default

    def json(self):
        import json as _json
        if not self.body:
            return None
        try:
            return _json.loads(self.body)
        except Exception:
            return None


class Resp:
    __slots__ = ("body", "code", "ctype", "headers", "file")

    def __init__(self, body=b"", code=200, ctype="text/plain; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.body = body
        self.code = code
        self.ctype = ctype
        self.headers = headers or []
        self.file = None


def json_resp(obj, code=200):
    import json as _json
    return Resp(_json.dumps(obj), code, CTYPES[".json"], [("Cache-Control", "no-store")])


def file_resp(filename, ctype=None, max_age=0):
    """把 flash 上的文件当成响应体，分块发送（用于天气这种几十 KB 的 JSON）。"""
    ext = "." + filename.rsplit(".", 1)[-1] if "." in filename else ""
    r = Resp(b"", 200, ctype or CTYPES.get(ext, "application/octet-stream"),
             [("Cache-Control", "max-age=%d" % max_age if max_age else "no-store")])
    r.file = filename
    return r


def text(code, msg):
    return Resp(msg, code)


def _has(seq, obj):
    """按身份判断，避免 socket 的 == 语义差异；不分配任何容器。"""
    for x in seq:
        if x is obj:
            return True
    return False


class HttpSrv:
    def __init__(self, port=80):
        self.port = port
        self.routes = {}
        self.files = {}
        self.sock = None
        self.accepted = 0
        self._pending = []      # [(conn, ticks_ms)]：已接入、还没发请求的连接
        self._draining = []     # [(conn, ticks_ms)]：已回完、等客户端先关的连接
        self._in_poll = 0       # 重入深度：网络阻塞任务里回调 poll() 时用
        self._rlist = []        # 复用：select 的 fd 列表（避免每轮新建）
        self._keep = []         # 复用：本轮仍未就绪的挂起连接

    # ---------- 注册 ----------
    def route(self, method, path, fn):
        self.routes[(method, path)] = fn

    def serve_file(self, path, filename, mtime_cache=None):
        """path 是 URL，filename 是 flash 上的文件。自动探测同名 .gz。"""
        self.files[path] = filename

    # ---------- 生命周期 ----------
    def start(self):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", self.port))
        # backlog 只能给 3：ESP32 上 TCP PCB 在**三次握手完成时**就分配了，
        # 不是被 accept 时才分配。原来 listen(12) 等于允许 12 条连接先占着
        # PCB 排队等我们 accept，而池子总共才 10 个左右——浏览器一次预连
        # 6~8 条就把池子吃光，真实请求连 SYN 都没地方放，外部表现是整站
        # 卡死一分钟。压到 3 之后，多出来的 SYN 会被丢弃，浏览器按 TCP 规则
        # 自己重传（慢 1~3 秒但一定成功），不会再出现"占满即死"。
        s.listen(3)
        s.settimeout(0.02)
        self.sock = s
        print("[web] http://0.0.0.0:%d" % self.port)

    def poll(self, budget=4):
        """一轮事件循环：listen socket 和「已接入但还没发请求」的连接一起交给
        同一个 select。

        旧实现给每个 accept 到的连接做 0.25s 探活。浏览器打开一个页面会预连
        若干条「TCP 建好但不发请求」的连接，主循环就被按着 0.25s × N 睡过去
        （实测 6 条约 1.5s、8 条约 2.0s）；这期间 listen 队列里的新 SYN 没人
        accept，backlog 满了 lwIP 直接丢弃，外部看到的就是「页面卡住 / 空响应」。

        现在没有任何「为某个连接空等」的代码：
        - 只有 select 报告可读的连接才去处理，没数据的零成本挂起
        - 挂起超过 3 秒仍没数据就关掉（浏览器自己会重连）
        - select 的 30ms 超时是**必须的**：ESP32 上 lwIP 跑在独立任务里，
          零超时轮询会把网络任务饿死，实测连 ping 都丢包
        """
        # 允许一层重入（深度上限 2）：处理某个请求时若内部要等路由器 CGI，
        # rap 会回调 poll()，此时 _in_poll 已是 1，不放行的话「让出」就是
        # 死代码——实测并发时另一个请求被整段 CGI 卡住 1.6 秒。
        # 但必须有上限：嵌套再套嵌套会无限递归，所以到 2 就直接返回。
        if self._in_poll > 2:
            self._in_poll = 0      # 上次异常残留的自愈，别把自己永久锁死
        if not self.sock or self._in_poll >= 2:
            return
        import select
        self._in_poll += 1
        try:
            now = _ticks_ms()

            # 1) 清掉超时还没发来请求头的挂起连接
            #
            # 1.0 秒而不是 3 秒：手机浏览器（Safari / 微信）打开一个页面会预连
            # 6~8 条 TCP，这些连接在拿到 HTML 之前不发数据，但**每条都占着一个
            # lwIP TCP PCB**（实测池子只有 10 个左右）。原来留 3 秒，等于预连接
            # 独占一半池子三秒，真实请求只能抢剩下的几条，抢不到就被 RST——
            # 外部表现就是「手机上打开某页卡死」。
            if self._pending:
                keep = []
                for conn, t0 in self._pending:
                    if _ticks_diff(now, t0) > 1000:
                        # 预连接一直不发请求：**不主动关**，交给客户端关。
                        # 服务端主动 close 会让 PCB 停在 TIME_WAIT 60 秒，
                        # 而池子只有 10 个——浏览器一次预连 6~8 条，两三页就攒满。
                        self._draining.append((conn, now))
                    else:
                        keep.append((conn, t0))
                self._pending = keep

            # 2) listen + 所有挂起连接，一次 select 等
            #
            # 这里的 list / 临时容器全部复用，不每轮新建：poll() 每 30ms 跑一次，
            # 一天近 300 万次，每次 new 一个 list+set 会把堆切碎，跑几小时后
            # 空闲堆从 80KB 掉到十几 KB，最终 select/accept 申请失败——这正是
            # 「跑一段时间后卡死」的来源之一。
            rl = self._rlist
            del rl[:]
            rl.append(self.sock)
            for conn, _t in self._pending:
                rl.append(conn)
            for conn, _t in self._draining:
                rl.append(conn)
            try:
                ready, _, _ = select.select(rl, [], [], 0.03)
            except ValueError:
                # 列表里混进了已关闭的连接（fd = -1）。这里必须**逐个关掉**再清空，
                # 只清空列表会把 fd 漏掉——漏几次之后 accept 就再也拿不到 fd，
                # 外部表现就是「整站无响应，连 health 都超时」。
                for conn, _t in self._pending:
                    self._drop(conn)
                for conn, _t in self._draining:
                    self._drop(conn)
                self._pending = []
                self._draining = []
                return
            except OSError:
                return
            if not ready:
                return

            # 3) 回收「已经回完、客户端也关了」的连接：此时服务端是被动关闭，
            #    PCB 不再进 TIME_WAIT，可以马上复用。
            if self._draining:
                keepd = []
                for conn, t0 in self._draining:
                    if _has(ready, conn):
                        try:
                            conn.recv(32)      # 客户端已 FIN，返回 b""
                        except Exception:
                            pass
                        self._drop(conn)
                    elif _ticks_diff(now, t0) > 8000:
                        # 8 秒还不关（个别客户端赖着），只好主动关
                        self._drop(conn)
                    else:
                        keepd.append((conn, t0))
                self._draining = keepd
                # 兜底：等关的连接堆太多也会吃光 PCB
                while len(self._draining) > 10:
                    c, _t = self._draining.pop(0)
                    self._drop(c)

            # 4) 先处理已就绪的挂起连接
            #
            # 关键：处理前必须把这些连接从 _pending 里**摘走并置空**。
            # 否则 _serve() 内部的让出回调（等 CGI 时）会再跑一次 poll，
            # 看到同一个连接还挂在列表里——超过 3 秒就把它 close 掉，
            # 外层随后 send 就报 WinError 10038 / EBADF，请求直接断。
            # 内层这时看到的 _pending 是空列表，只会往里 append 新连接，
            # 处理完再把它们并回外层。
            if self._pending:
                todo = self._pending
                self._pending = []
                still = []
                for item in todo:
                    if _has(ready, item[0]):
                        self._serve(item[0])
                    else:
                        still.append(item)
                if still or self._pending:
                    still.extend(self._pending)
                    self._pending = still

            # 4) 再 accept 新连接：只登记，不等它发数据
            if _has(ready, self.sock):
                for _ in range(budget):
                    try:
                        conn, _addr = self.sock.accept()
                    except Exception:
                        break
                    try:
                        conn.settimeout(1.0)
                    except Exception:
                        pass
                    self._pending.append((conn, _ticks_ms()))
                # 挂起上限 3：lwIP 的 PCB 池约 10 个，浏览器一次就预连 6~8 条。
                # 全收下等于把池子交给「建了连接却不发请求」的预连接，真实请求
                # 只能被 RST。只收 3 条、多的当场关掉（浏览器会自己重连），
                # 就能留出 7 个 PCB 给真正要数据的连接。
                while len(self._pending) > 3:
                    old, _t = self._pending.pop(0)
                    self._draining.append((old, _ticks_ms()))
        finally:
            self._in_poll -= 1

    def _drop(self, conn):
        try:
            conn.close()
        except Exception:
            pass

    def _serve(self, conn):
        done = False
        try:
            done = self._handle(conn)
        except Exception as e:
            print("[web] err:", e)
        finally:
            # 回完的响应不立刻 close，先放进 _draining 等客户端关。
            #
            # 这是躲 TIME_WAIT 的唯一办法（MicroPython 没有 SO_LINGER，不能
            # 用 RST 跳过）：服务端主动 close 会让 PCB 在 TIME_WAIT 停 60 秒，
            # 而 lwIP 总共只有 10 个 PCB——一次页面访问产生 2~8 个，开几页就
            # 把池子攒满，之后整站一分钟内连不上（就是「卡死」）。
            # 改成发完等客户端先关：浏览器收到 Connection: close + 完整
            # Content-Length 会立刻关，服务端随后读到 EOF 再 close，
            # 被动关闭不进 TIME_WAIT，PCB 马上能复用。
            if done:
                self._draining.append((conn, _ticks_ms()))
            else:
                self._drop(conn)
            self.accepted += 1
            # 每个请求结束都回收一次。
            #
            # 原来是「每 20 个请求 collect 一次」，实测 10 个请求就能让空闲堆
            # 从 70KB 掉到 21KB（读头拼接、headers dict、JSON 序列化留下的
            # 循环引用），下一次 accept 或 malloc 就失败——这才是「开两三个
            # 页面后整站连 health 都没反应」的真凶。collect 一次约 3ms，
            # 相比一次请求 200~400ms 可以忽略。
            gc.collect()

    # ---------- 处理 ----------
    def _read_head(self, conn):
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(512)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 4096:
                break
        head, _, rest = buf.partition(b"\r\n\r\n")
        return head, rest

    def _handle(self, conn):
        head, rest = self._read_head(conn)
        if not head:
            return
        try:
            line = head.split(b"\r\n", 1)[0].decode()
            method, target, _ = line.split(" ", 2)
        except Exception:
            return

        path, _, query = target.partition("?")
        headers = {}
        for h in head.split(b"\r\n")[1:]:
            try:
                k, v = h.decode().split(":", 1)
                headers[k.strip().lower()] = v.strip()
            except Exception:
                pass

        body = b""
        n = headers.get("content-length")
        if n:
            try:
                need = int(n)
            except Exception:
                need = 0
            body = rest
            while len(body) < need:
                chunk = conn.recv(min(512, need - len(body)))
                if not chunk:
                    break
                body += chunk
            body = body[:need]

        req = Req(method, path, query, headers, body)

        fn = self.routes.get((method, path))
        if fn:
            r = fn(req)
        elif path in self.files:
            self._send_static(conn, req, self.files[path])
            return True
        else:
            r = text(404, "not found")

        if r is None:
            r = Resp(b"")
        self._send(conn, req, r)
        del req
        return True

    def _send(self, conn, req, resp):
        if resp.file:
            self._send_from_file(conn, req, resp)
            return
        reason = {200: "OK", 202: "Accepted", 302: "Found", 400: "Bad Request", 404: "Not Found",
                  405: "Method Not Allowed", 500: "Internal Server Error"}.get(resp.code, "OK")
        head = "HTTP/1.1 %d %s\r\n" % (resp.code, reason)
        head += "Content-Type: " + resp.ctype + "\r\n"
        head += "Content-Length: %d\r\n" % len(resp.body)
        for k, v in resp.headers:
            head += "%s: %s\r\n" % (k, v)
        head += "Connection: close\r\n\r\n"
        conn.send(head.encode())
        mv = memoryview(resp.body)
        for i in range(0, len(mv), 1024):
            conn.send(mv[i:i + 1024])

    def _send_from_file(self, conn, req, resp):
        import os
        try:
            size = os.stat(resp.file)[6]
            f = open(resp.file, "rb")
        except Exception:
            self._send(conn, req, text(404, "missing " + resp.file))
            return
        head = "HTTP/1.1 200 OK\r\n"
        head += "Content-Type: " + resp.ctype + "\r\n"
        head += "Content-Length: %d\r\n" % size
        for k, v in resp.headers:
            head += "%s: %s\r\n" % (k, v)
        head += "Connection: close\r\n\r\n"
        conn.send(head.encode())
        try:
            while True:
                buf = f.read(1024)
                if not buf:
                    break
                conn.send(buf)
        except Exception:
            pass
        finally:
            try:
                f.close()
            except Exception:
                pass

    def _send_static(self, conn, req, filename):
        accept = req.headers.get("accept-encoding", "")
        use_gz = "gzip" in accept
        ctype = CTYPES.get("." + filename.rsplit(".", 1)[-1], "application/octet-stream")
        real = filename
        gz = False
        if use_gz:
            try:
                import os
                d = filename.rsplit("/", 1)[0] if "/" in filename else "."
                base = filename.rsplit("/", 1)[-1]
                # os.listdir 返回裸文件名，不能拿带目录的全路径去比
                if (base + ".gz") in os.listdir(d):
                    real = filename + ".gz"
                    gz = True
            except Exception:
                real = filename

        try:
            import os
            size = os.stat(real)[6]
            f = open(real, "rb")
        except Exception:
            self._send(conn, req, text(404, "missing " + filename))
            return

        head = "HTTP/1.1 200 OK\r\n"
        head += "Content-Type: " + ctype + "\r\n"
        if gz:
            head += "Content-Encoding: gzip\r\n"
        head += "Content-Length: %d\r\n" % size
        # 页面一律 no-store：改完页面刷一下就是新的（以前统一缓存 7 天，
        # 改完文案还显示旧版本，很容易被误判成「没保存成功」）。
        # theme.css / ui.js 这类资源给 1 小缓存：一次浏览少两条连接，
        # 对单线程服务器来说就是少两次排队；改这两个文件时按 Ctrl+F5。
        if filename.endswith(".html"):
            head += "Cache-Control: no-store\r\n"
        else:
            head += "Cache-Control: max-age=3600\r\n"
        head += "Connection: close\r\n\r\n"
        conn.send(head.encode())
        try:
            while True:
                buf = f.read(1024)
                if not buf:
                    break
                conn.send(buf)
        except Exception:
            pass
        finally:
            try:
                f.close()
            except Exception:
                pass
