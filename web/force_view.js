/* force_view.js —— 兵力池视图（控制台与独立页面共用同一份实现）
 *
 * 为什么独立成文件：兵力池在 2026-09-24 从"控制台右栏的一个隐藏页签"升级为
 * 与控制台、地图并列的顶层页面。渲染逻辑若各写一份，改样式必漏一处 —— 这是
 * 典型的"修了界面 A、忘了界面 B"。所以抽出来，两边 include 同一份。
 *
 * 刻意自带 el/esc 而不依赖 app.js：本文件要在没有控制台 DOM 的独立页面上跑，
 * 不能假设 #chatLog 之类存在，也不能假设别人用什么工具函数。自带最小实现，
 * 换来的是零依赖、可直接挂任何容器。
 *
 * 暴露：renderForceInto(box, data) / loadForceInto(box)
 */
'use strict';

(function () {
  const el = (tag, cls, html) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html !== undefined) n.innerHTML = html;
    return n;
  };
  const esc = (s) => String(s ?? '').replace(/[&<>]/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));

  const SVC_CLS = { '陆军': 'army', '海军': 'navy', '空军': 'air' };

  function bar(pct, cls) {
    const wrap = el('div', 'bar');
    const inner = el('div', 'bar-in ' + (cls || ''));
    inner.style.width = Math.max(0, Math.min(100, pct || 0)) + '%';
    wrap.appendChild(inner);
    return wrap;
  }

  function forceTable(headers, rows) {
    const t = el('table', 'ftable');
    const tr = el('tr');
    headers.forEach(h => tr.appendChild(el('th', '', h)));
    t.appendChild(el('thead')).appendChild(tr);
    const tb = el('tbody');
    rows.forEach(cells => {
      const r = el('tr');
      // 分组行：{__group: '陆军 · 12 条'} 占满整行。
      // 表按军种拼接，不给个间隔行的话，40+ 行的明细要自己数到哪换军种了。
      if (cells && cells.__group) {
        const gtd = el('td', 'grp-td', esc(cells.__group));
        gtd.colSpan = headers.length;
        r.appendChild(gtd);
        tb.appendChild(r);
        return;
      }
      cells.forEach(cell => {
        // 单元格可以是 [文本, 类名]，也可以直接给一个节点（操作按钮那一列）
        if (cell instanceof Node) { r.appendChild(el('td', 'op-td')).appendChild(cell); return; }
        const [txt, cls] = cell;
        r.appendChild(el('td', cls || '', esc(txt)));
      });
      tb.appendChild(r);
    });
    t.appendChild(tb);
    return t;
  }

  function opBtn(label, cls, title, fn) {
    const b = el('button', 'op-btn ' + (cls || ''), esc(label));
    b.type = 'button';
    if (title) b.title = title;
    b.addEventListener('click', fn);
    return b;
  }

  function opCell(btns) {
    const w = el('span', 'op-wrap');
    btns.forEach(b => w.appendChild(b));
    return w;
  }

  /* opts.actions 给了就在表格右侧多一列「操作」。
     不给就是纯只读视图 —— 控制台那边不需要改兵力，不需要那列按钮。 */
  function renderForceInto(box, d, opts) {
    const A = (opts || {}).actions || null;
    box.innerHTML = '';
    const services = d.services || [];
    // 条形基准取三军合计：三根条相加正好等于"合计"那一根，图本身自证
    const total = services.reduce((a, s) => a + (s.personnel_total || 0), 0) || 1;

    box.appendChild(el('div', 'sec-title', '三军种兵力池'));
    const grid = el('div', 'force-grid');
    services.forEach(s => {
      const c = el('div', 'fcard svc-' + (s.service || ''));
      const hd = el('div', 'fcard-hd');
      hd.appendChild(el('span', 'fcard-name', esc(s.service)));
      hd.appendChild(el('span', 'fcard-badge', esc(s.unit_count) + ' 个单位'));
      c.appendChild(hd);
      c.appendChild(el('div', 'fnum',
        esc(s.personnel_total) + '<span class="fnum-lb"> 人</span>'));
      c.appendChild(bar((s.personnel_total || 0) / total * 100, SVC_CLS[s.service] || ''));
      const sub = el('div', 'fcard-sub');
      sub.appendChild(el('span', '', '装备 ' + esc(s.equipment_total) + ' 件'));
      sub.appendChild(el('span', '', '占三军 '
        + ((s.personnel_total || 0) / total * 100).toFixed(1) + '%'));
      c.appendChild(sub);
      grid.appendChild(c);
    });

    const fcap = (d.cap || {}).force_cap;
    const tc = el('div', 'fcard fcard-total');
    const thd = el('div', 'fcard-hd');
    thd.appendChild(el('span', 'fcard-name', '三军合计'));
    thd.appendChild(el('span', 'fcard-badge',
      services.length + ' 个军种 · ' + (d.units || []).length + ' 个单位'));
    tc.appendChild(thd);
    tc.appendChild(el('div', 'fnum', esc(total) + '<span class="fnum-lb"> 人</span>'));
    /* ★ 这里不能拿总员额去比 force_cap。force_cap 是"单次行动可投入的兵力上限"
       （HR-01 用它卡投入量），全军在编 39800 本来就会大于它 —— 画成占比条会
       读成"超限 132%"，等于把正确的数据讲成错的结论。只用文字并列两个口径。 */
    const tsub = el('div', 'fcard-sub');
    tsub.appendChild(el('span', '', '装备 ' + services
      .reduce((a, s) => a + (s.equipment_total || 0), 0) + ' 件'));
    if (typeof fcap === 'number') {
      tsub.appendChild(el('span', '', '单次行动投入上限 ' + fcap));
    }
    tc.appendChild(tsub);
    /* 合计卡不画进度条：这里没有"占满"这件事，画一条 100% 的条只会让人
       去找它代表什么。三根军种条已经自证与合计的关系。 */
    grid.appendChild(tc);
    box.appendChild(grid);

    // 共享资源：条长按各资源容量归一，一眼看出哪类资源最紧
    const res = d.resources || [];
    const rc = (d.cap || {}).resource_cap || {};
    const maxC = Math.max(1, ...res.map(r =>
      (typeof rc[r.res_key] === 'number' ? rc[r.res_key] : r.capacity) || 0));
    const secR = el('div', 'sec');
    secR.appendChild(el('div', 'sec-title', '共享资源容量（参谋申索不得超过这条线）'));
    res.forEach(r => {
      const cap = typeof rc[r.res_key] === 'number' ? rc[r.res_key] : r.capacity;
      const row = el('div', 'res-row');
      row.appendChild(el('span', 'res-nm', esc(r.res_name)));
      row.appendChild(bar((cap || 0) / maxC * 100, 'res'));
      row.appendChild(el('span', 'res-v', esc(cap)));
      if (A && A.editRes) {
        row.appendChild(opBtn('改容量', '', '修改这项共享资源的容量上限',
          () => A.editRes(r, cap)));
      }
      secR.appendChild(row);
    });
    box.appendChild(secR);

    const units = d.units || [];
    const secU = el('div', 'sec');
    const uh = el('div', 'sec-head');
    uh.appendChild(el('div', 'sec-title', '下属单位（' + units.length + ' 个）'));
    if (A && A.addUnit) {
      uh.appendChild(opBtn('+ 新增单位', 'primary', '手动添加一个单位（陆军/海军/空军）',
        () => A.addUnit()));
    }
    secU.appendChild(uh);
    const uHead = ['番号', '单位名称', '军种', '兵种', '人数', '驻地', '战备'];
    const uRows = [];
    ['陆军', '海军', '空军'].forEach(sv => {
      const rs = units.filter(u => u.service === sv);
      if (!rs.length) return;
      uRows.push({ __group: sv + ' · ' + rs.length + ' 个单位 · '
        + rs.reduce((a, u) => a + (u.personnel || 0), 0) + ' 人' });
      rs.forEach(u => {
        const cells = [[u.unit_id, 'mono'], [u.unit_name, 'nm2'], [u.service],
                       [u.branch], [u.personnel, 'mono'], [u.base], [u.readiness]];
        if (A) {
          cells.push(opCell([
            opBtn('编辑', '', '修改这个单位（人数、驻地、战备状态…）', () => A.editUnit(u)),
            opBtn('删除', 'danger', '删除这个单位（连同它的装备）', () => A.delUnit(u)),
          ]));
        }
        uRows.push(cells);
      });
    });
    if (A) uHead.push('操作');
    secU.appendChild(forceTable(uHead, uRows));
    box.appendChild(secU);

    const equip = d.equipment || [];
    const secE = el('div', 'sec');
    secE.appendChild(el('div', 'sec-title', '装备型号汇总（' + equip.length + ' 类）'));
    const cols = el('div', 'force-cols');
    ['陆军', '海军', '空军'].forEach(sv => {
      const rows = equip.filter(e => e.service === sv);
      if (!rows.length) return;
      const card = el('div', 'ecard svc-' + sv);
      card.appendChild(el('div', 'ecard-hd',
        esc(sv) + '<span class="dim"> ' + rows.length + ' 类</span>'));
      rows.forEach(e => {
        const r = el('div', 'erow');
        r.appendChild(el('span', 'erow-m mono', esc(e.model)));
        r.appendChild(el('span', 'erow-t', esc(e.etype)));
        r.appendChild(el('span', 'erow-n mono', esc(e.total)));
        card.appendChild(r);
      });
      cols.appendChild(card);
    });
    secE.appendChild(cols);
    box.appendChild(secE);

    /* 装备明细：必须带"所属单位"。汇总视图答不了"这条要改的是谁"——
       同一个型号在不同单位各有一行，(单位, 型号) 才是这条记录的身份。 */
    const erows = d.equipment_rows || [];
    const secD = el('div', 'sec');
    const dh = el('div', 'sec-head');
    dh.appendChild(el('div', 'sec-title', '装备明细（' + erows.length + ' 条，可增删改）'));
    if (A && A.addEq) {
      dh.appendChild(opBtn('+ 新增装备', 'primary', '给某个单位添加一项装备',
        () => A.addEq()));
    }
    secD.appendChild(dh);
    const dHead = ['所属单位', '军种', '型号', '类型', '数量'];
    const dRows = [];
    ['陆军', '海军', '空军'].forEach(sv => {
      const rs = erows.filter(e => e.service === sv);
      if (!rs.length) return;
      dRows.push({ __group: sv + ' · ' + rs.length + ' 条 · 合计 '
        + rs.reduce((a, e) => a + (e.count || 0), 0) + ' 件' });
      rs.forEach(e => {
        const cells = [[e.unit_id + ' ' + e.unit_name, 'nm2'], [e.service],
                       [e.model, 'mono'], [e.etype], [e.count, 'mono']];
        if (A) {
          cells.push(opCell([
            opBtn('编辑', '', '改这项装备的单位/型号/类型/数量',
              () => A.editEq(e)),
            opBtn('删除', 'danger', '删除这项装备', () => A.delEq(e)),
          ]));
        }
        dRows.push(cells);
      });
    });
    if (A) dHead.push('操作');
    if (dRows.length) {
      secD.appendChild(forceTable(dHead, dRows));
    } else {
      secD.appendChild(el('div', 'log-empty', '还没有装备记录'));
    }
    box.appendChild(secD);
  }

  async function loadForceInto(box, apiBase, opts) {
    box.innerHTML = '<div class="log-empty">正在读取兵力池…</div>';
    try {
      const r = await fetch((apiBase || '') + '/api/force/overview', { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const d = await r.json();
      renderForceInto(box, d, opts);
      return d;
    } catch (e) {
      box.innerHTML = '<div class="log-empty">兵力池读取失败：' + esc(e.message) + '</div>';
      return null;
    }
  }

  window.ForceView = { renderForceInto, loadForceInto, bar, opBtn, opCell, el, esc };
})();
