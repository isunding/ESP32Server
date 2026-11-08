/* ESP32 终端服务器 · 公共前端
 * 各页面共用：取数、格式化、顶栏与页签、提示条
 */
"use strict";

var MR = (function () {

  var PAGES = [
    { href: "/", title: "首页" },
    { href: "/weather.html", title: "天气" },
    { href: "/market.html", title: "行情" },
    { href: "/news.html", title: "新闻" },
    { href: "/quake.html", title: "地震" },
    { href: "/radio.html", title: "电台" },
    { href: "/info.html", title: "本机" },
    { href: "/ntp.html", title: "NTP" },
    { href: "/iptv.html", title: "IPTV" }
  ];

  function $(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function api(path) {
    return fetch(path, { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  function fmtBytes(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
    return (n / 1073741824).toFixed(2) + " GB";
  }

  function fmtDur(sec) {
    sec = Number(sec) || 0;
    var d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
    if (d) return d + " 天 " + h + " 小时";
    if (h) return h + " 小时 " + m + " 分";
    return m + " 分钟";
  }

  function pad(n) { return n < 10 ? "0" + n : "" + n; }
  function hhmm(d) { return pad(d.getHours()) + ":" + pad(d.getMinutes()); }
  function mmdd(s) { return s ? s.substr(5, 5) : "--"; }

  function currentPage() {
    var p = location.pathname;
    if (p === "" || p === "/" || p === "/index.html") return "/";
    return p.replace(/^\/+/, "/");
  }

  /* 顶栏 + 页签。title 为空时不显示返回键。
     挂载完就自动开始心跳：以前要求每页自己调 healthTick()，漏掉一页
     右上角就永远停在初始文案「连接中…」。 */
  function mountTop(title) {
    var cur = currentPage();
    var nav = PAGES.map(function (p) {
      var on = p.href === cur ? " on" : "";
      return '<a class="' + on.trim() + '" href="' + p.href + '">' + p.title + "</a>";
    }).join("");

    var back = cur === "/" ? "" :
      '<a class="back" href="/" title="返回首页">‹</a>';

    var html =
      '<div class="top">' + back +
      "<h1>" + esc(title) + "</h1>" +
      '<div class="st"><span class="dot" id="hDot"></span><span id="hIp">连接中…</span></div>' +
      "</div>" +
      '<div class="nav">' + nav + "</div>";

    var host = document.getElementById("topbar");
    if (host) {
      host.innerHTML = html;
      startBeat();
    }
  }

  var tickBusy = null, tickAt = 0, tickTimer = 0;

  /* 心跳：取一次 /api/health，把 IP 与状态点刷到顶栏。
     重复调用复用同一次请求，多页共用一个 15 秒定时器。 */
  function healthTick() {
    if (tickBusy) return tickBusy;
    if (MR.device && Date.now() - tickAt < 800) return Promise.resolve(MR.device);
    tickBusy = api("/api/health").then(function (h) {
      var dot = $("hDot"), ip = $("hIp");
      if (dot) dot.className = "dot beat " + (h.router_ok ? "on" : "err");
      if (ip) {
        ip.textContent = h.ip;
        ip.title = (h.router_ok ? "路由器可达" : "路由器不可达") + " · RSSI " + h.rssi +
          " · 运行 " + fmtDur(h.up) + " · 空闲内存 " + fmtBytes(h.mem_free);
      }
      MR.device = h;
      return h;
    }).catch(function () {
      var dot = $("hDot"), ip = $("hIp");
      if (dot) dot.className = "dot beat err";
      // 掉了也别把 IP 抹成占位文案，留着最后已知的地址更有用
      if (ip) {
        ip.textContent = MR.device ? MR.device.ip : "离线";
        ip.title = "设备无响应";
      }
      return null;
    }).then(function (r) {
      tickBusy = null;
      tickAt = Date.now();
      return r;
    });
    return tickBusy;
  }

  function startBeat() {
    if (tickTimer) return;
    healthTick();
    tickTimer = setInterval(healthTick, 15000);
  }

  var toastTimer = 0;
  function toast(msg, ms) {
    var el = document.getElementById("toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "toast";
      el.className = "toast";
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.className = "toast on";
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.className = "toast"; }, ms || 2600);
  }

  function empty(msg) { return '<span class="muted small">' + esc(msg) + "</span>"; }

  return {
    $: $, esc: esc, api: api, fmtBytes: fmtBytes, fmtDur: fmtDur,
    hhmm: hhmm, mmdd: mmdd, mountTop: mountTop, healthTick: healthTick,
    startBeat: startBeat,
    toast: toast, empty: empty, PAGES: PAGES, device: null
  };
})();
