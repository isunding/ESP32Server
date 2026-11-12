/* ESP32 终端服务器 · 公共前端
 * 各页面共用：取数、格式化、顶栏与页签、提示条
 */
"use strict";

var MR = (function () {

  var PAGES = [
    { href: "/", title: "首页" },
    { href: "/weather.html", title: "天气" },
    { href: "/quake.html", title: "地震" },
    { href: "/news.html", title: "新闻" },
    { href: "/market.html", title: "行情" },
    { href: "/iptv.html", title: "直播" },
    { href: "/radio.html", title: "电台" },
    { href: "/info.html", title: "本机" },
    { href: "/ntp.html", title: "校时" }
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

  /* 写接口：统一按 {"ok":0,"msg":...} 判失败，异常信息直接给用户看 */
  function post(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (!r.ok || !d.ok) throw new Error(d.msg || ("HTTP " + r.status));
        return d;
      });
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
    if (sec < 60) return Math.round(sec) + " 秒";
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

  /* 板载指示灯心跳开关。图标画在 IP 右侧，点一下切一次，状态存板上配置。
     显示与否取决于 /api/health 的 led_hw：没有可控指示灯的设备不出现按钮。 */
  var LED_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" ' +
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
    'stroke-linejoin="round" aria-hidden="true"><path d="M2 12h4l2.5-7 5 14 2.5-7h8"/></svg>';

  function setLedBtn(on, hw) {
    var b = $("hLed");
    if (!b) return;
    b.style.display = hw ? "" : "none";
    b.className = "hbtn" + (on ? " on" : "");
    b.setAttribute("data-on", on ? "1" : "0");
    b.setAttribute("aria-pressed", on ? "true" : "false");
    b.title = on ? "板载心跳：开（点击关闭）" : "板载心跳：关（点击开启）";
  }

  function toggleLed() {
    var b = $("hLed");
    if (!b || b.getAttribute("data-busy") === "1") return;
    var want = b.getAttribute("data-on") === "1" ? 0 : 1;
    b.setAttribute("data-busy", "1");
    post("/api/led", { on: want }).then(function (r) {
      b.removeAttribute("data-busy");
      setLedBtn(r.on ? 1 : 0, 1);
      toast(r.on ? "板载心跳已开启" : "板载心跳已关闭", 1600);
    }).catch(function (e) {
      b.removeAttribute("data-busy");
      toast(e && e.message ? e.message : "切换失败");
    });
  }

  /* 深浅色切换。空值表示跟随系统，选中后写入 localStorage 锁定。
     图标画的是“点一下会切到的模式”：现在亮色给月亮，现在暗色给太阳。 */
  var THEME_KEY = "mr.theme";
  var SUN_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" ' +
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
    'stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/>' +
    '<path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2"/>' +
    '<path d="M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
  var MOON_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" ' +
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
    'stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>';

  function themePref() {
    try { return localStorage.getItem(THEME_KEY) || ""; } catch (e) { return ""; }
  }

  /* 判定顺序：DOM 上的 data-theme（构建时注入的引导脚本已按 localStorage 打过）
     → localStorage → 系统配色。少了第一项，手动锁定后图标会画错方向。 */
  function themeNow() {
    var d = document.documentElement.getAttribute("data-theme");
    if (d === "light" || d === "dark") return d;
    var t = themePref();
    if (t === "light" || t === "dark") return t;
    return (window.matchMedia &&
      window.matchMedia("(prefers-color-scheme: light)").matches) ? "light" : "dark";
  }

  function paintThemeBtn() {
    var b = $("hTheme");
    if (!b) return;
    var light = themeNow() === "light";
    b.innerHTML = light ? MOON_SVG : SUN_SVG;
    b.title = light ? "切换到深色" : "切换到浅色";
    b.setAttribute("aria-label", b.title);
  }

  function toggleTheme() {
    var next = themeNow() === "light" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
    paintThemeBtn();
  }

  /* 跟随系统时，系统配色变了要把图标跟着换掉 */
  if (window.matchMedia) {
    var mq = window.matchMedia("(prefers-color-scheme: light)");
    var onScheme = function () { if (!themePref()) paintThemeBtn(); };
    if (mq.addEventListener) mq.addEventListener("change", onScheme);
    else if (mq.addListener) mq.addListener(onScheme);
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
      '<div class="st"><span class="dot" id="hDot"></span><span id="hIp">连接中…</span>' +
      '<button class="hbtn" id="hLed" type="button" data-on="0" ' +
      'aria-pressed="false" title="板载心跳">' + LED_SVG + "</button>" +
      '<button class="hbtn" id="hTheme" type="button" title="切换深浅色"></button></div>' +
      "</div>" +
      '<div class="nav">' + nav + "</div>";

    var host = document.getElementById("topbar");
    if (host) {
      host.innerHTML = html;
      var lb = $("hLed");
      if (lb) lb.onclick = toggleLed;
      var tb = $("hTheme");
      if (tb) tb.onclick = toggleTheme;
      paintThemeBtn();
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
      setLedBtn(h.led_on ? 1 : 0, h.led_hw ? 1 : 0);
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
    $: $, esc: esc, api: api, post: post, fmtBytes: fmtBytes, fmtDur: fmtDur,
    hhmm: hhmm, mmdd: mmdd, mountTop: mountTop, healthTick: healthTick,
    startBeat: startBeat,
    toast: toast, empty: empty, PAGES: PAGES, device: null
  };
})();
