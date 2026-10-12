/* reload_guard.js —— 新版本自动提醒
 *
 * 为什么需要它：改完前端代码，用户那个一直开着的标签页里跑的还是旧脚本。
 * 服务端早就是新版了、curl 下来也确实是新版，但用户屏幕上就是"没修好" ——
 * 这种问题最难排查，因为从服务端看一切正常。
 *
 * 做法：每 45 秒问一次服务端构建号（web 目录最新文件的修改时间），
 * 跟页面加载时注入的 window.__BUILD__ 不一致就在顶部挂一条提示，
 * 点一下刷新即可。不强制跳转 —— 用户可能正在读一份方案。
 */
(function () {
  'use strict';
  var my = window.__BUILD__ || '0';
  if (my === '0') return;                       // 服务没注入版本号（比如 file:// 打开）就不折腾

  var bar = null;

  function show() {
    if (bar) return;
    bar = document.createElement('div');
    bar.className = 'update-bar';
    bar.innerHTML = '界面有新版本，<a href="javascript:location.reload()">点此刷新</a>'
      + '（或按 Ctrl+F5）。刷新前这一页跑的还是旧代码。';
    document.body.insertBefore(bar, document.body.firstChild);
  }

  async function check() {
    try {
      var r = await fetch('/api/build', { cache: 'no-store' });
      if (!r.ok) return;
      var d = await r.json();
      if (d.build && String(d.build) !== String(my)) show();
    } catch (e) { /* 服务没起来就算了，不打扰 */ }
  }

  setInterval(check, 45000);
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) check();               // 切回这个标签时立刻查一次
  });
})();
