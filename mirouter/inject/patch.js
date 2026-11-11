/* ---- Router Control Console entry (compact, dedup safe) ---- */
;(function () {
  var MARK = 'rcc-entry-mob';
  var MARK_FLOAT = 'rcc-entry';

  function mkBtn(block) {
    var a = document.createElement('a');
    a.href = '/manage/';
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = '\u2699\ufe0f \u8def\u7531\u63a7\u5236\u53f0';
    if (block) {
      a.style.cssText = 'display:inline-block;text-align:center;' +
        'background:#ff6a00;color:#fff;padding:9px 18px;border-radius:20px;' +
        'font-size:14px;font-weight:600;text-decoration:none;margin-top:12px;' +
        'box-shadow:0 3px 10px rgba(255,106,0,.3);line-height:1.3;';
    } else {
      a.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:2147483647;' +
        'background:#ff6a00;color:#fff;padding:8px 15px;border-radius:20px;' +
        'font:600 13px/1.3 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;' +
        'text-decoration:none;box-shadow:0 3px 10px rgba(0,0,0,.25);';
    }
    return a;
  }

  function alreadyPresent() {
    return !!(document.getElementById(MARK) ||
              document.getElementById(MARK_FLOAT) ||
              document.getElementById('rcc-box-spa') ||
              document.querySelector('.rcc-box') ||
              document.querySelector('a[href="/manage/"]'));
  }

  function trySpa() {
    var form = document.querySelector('.form');
    if (!form) return false;
    if (alreadyPresent()) return true;
    var box = document.createElement('div');
    box.id = MARK;
    box.style.cssText = 'text-align:center;width:100%;';
    box.appendChild(mkBtn(true));
    if (form.parentNode) form.parentNode.insertBefore(box, form.nextSibling);
    else form.appendChild(box);
    return true;
  }

  function tryLegacy() {
    if (alreadyPresent()) return true;
    var mob = document.getElementById('admin-mob');
    var pc = document.getElementById('admin-PC');
    var target = null, anchor = null;
    if (mob && mob.offsetParent !== null) {
      target = mob; anchor = mob.querySelector('.notice') || mob.lastElementChild;
    } else if (pc && pc.offsetParent !== null) {
      target = pc; anchor = pc.querySelector('.mobile-ft') || pc.lastElementChild;
    }
    if (!target) return false;
    var box = document.createElement('div');
    box.id = MARK;
    box.style.cssText = 'text-align:center;width:100%;';
    box.appendChild(mkBtn(true));
    if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(box, anchor.nextSibling);
    else target.appendChild(box);
    return true;
  }

  function floatBtn() {
    if (alreadyPresent()) return;
    var a = mkBtn(false);
    a.id = MARK_FLOAT;
    a.title = '\u8def\u7531\u63a7\u5236\u53f0';
    (document.body || document.documentElement).appendChild(a);
  }

  function boot() {
    var tries = 0, done = false;
    function tick() {
      if (done) return;
      if (trySpa() || tryLegacy()) { done = true; return; }
      if (++tries >= 30) { done = true; floatBtn(); }
    }
    tick();
    var timer = setInterval(function () {
      tick();
      if (done) clearInterval(timer);
    }, 200);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
