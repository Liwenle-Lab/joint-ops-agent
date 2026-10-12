/* force_admin_ui.js —— 兵力池管理面（新增/编辑/删除/导入/导出/变更记录）
 *
 * 为什么单独一个文件：force_view.js 是"给人看"的渲染，本文件是"改数据"的操作面。
 * 混在一起的话，将来想再做一个只读视图（比如汇报用的看板）就得连写操作一起带上，
 * 容易出事 —— 只读的地方不该有能力删兵力。
 *
 * 三条约束决定了这里的写法：
 *  1. 删除必须二次确认，且确认框里要写清"会连带删掉什么"。
 *     删一个旅连带删十几条装备，用户点之前得知道。
 *  2. 服务端返回的 field 要标到对应输入框上。只弹一句"操作失败"，
 *     用户只能靠猜是哪个字段不对。
 *  3. 改完全量重取（reload），不做局部刷新。兵力池的总数、视图、明细是联动的，
 *     自己在前端算增量迟早和数据库对不上。
 *
 * 暴露：window.ForceAdminUI.mount({toolbar, getData, reload, apiBase})
 */
'use strict';

(function () {
  const el = window.ForceView.el;
  const esc = window.ForceView.esc;

  const SVC = ['陆军', '海军', '空军'];
  const READY_OPTS = [
    { v: '', t: '（不填）' }, { v: '一级', t: '一级' },
    { v: '二级', t: '二级' }, { v: '三级', t: '三级' },
  ];

  let ctx = null;
  let anchors = [];          // 驻地候选（来自 /api/env/anchors），防止用户填了没配锚点的驻地

  /* ── 网络 ─────────────────────────────────────── */
  async function api(path, method, body) {
    const opt = { method: method || 'GET', cache: 'no-store' };
    if (body !== undefined) {
      opt.headers = { 'Content-Type': 'application/json' };
      opt.body = JSON.stringify(body);
    }
    const r = await fetch((ctx.apiBase || '') + path, opt);
    let d = null;
    try { d = await r.json(); } catch (e) { /* 非 JSON 兜底 */ }
    if (!r.ok) {
      const det = d && d.detail;
      const msg = (det && det.msg) || (typeof det === 'string' ? det : '')
        || (d && d.error) || ('请求失败 HTTP ' + r.status);
      const err = new Error(msg);
      err.field = (det && det.field) || '';
      err.status = r.status;
      throw err;
    }
    return d;
  }

  /* ── 小部件 ───────────────────────────────────── */
  function btn(label, cls, fn) {
    const b = el('button', 'btn ' + (cls || 'btn-ghost'), esc(label));
    b.type = 'button';
    if (fn) b.addEventListener('click', fn);
    return b;
  }

  function field(label, node, hint, key) {
    const w = el('div', 'fld');
    w.appendChild(el('label', 'fld-lb', esc(label)));
    if (key) node.dataset.field = key;
    w.appendChild(node);
    w.appendChild(el('div', 'fld-hint', esc(hint || '')));
    return w;
  }

  function input(value, ph) {
    const i = el('input', 'inp');
    i.type = 'text';
    i.value = value === undefined || value === null ? '' : String(value);
    if (ph) i.placeholder = ph;
    return i;
  }

  function numInput(value, ph) {
    const i = el('input', 'inp');
    i.type = 'number';
    i.min = '0';
    i.step = '1';
    i.value = value === undefined || value === null ? '' : String(value);
    if (ph) i.placeholder = ph;
    return i;
  }

  function select(value, options) {
    const s = el('select', 'inp');
    options.forEach(o => {
      const v = typeof o === 'string' ? o : o.v;
      const t = typeof o === 'string' ? o : o.t;
      const op = el('option', '', esc(t));
      op.value = v;
      if (String(value) === String(v)) op.selected = true;
      s.appendChild(op);
    });
    return s;
  }

  /* datalist 必须挂在 input 外面（input 是空元素，塞进去浏览器不会认），
     所以做成一个全局单例挂 body 上，多个表单共用一个候选列表。 */
  function baseInput(value) {
    const i = input(value, '如 东山基地');
    const dl = document.getElementById('baseList');
    if (dl) { i.setAttribute('list', 'baseList'); return i; }
    const nd = el('datalist');
    nd.id = 'baseList';
    anchors.forEach(a => {
      const o = el('option');
      o.value = a.base;
      nd.appendChild(o);
    });
    document.body.appendChild(nd);
    i.setAttribute('list', 'baseList');
    return i;
  }

  /* ── 通用弹窗 ─────────────────────────────────── */
  function dialog(opt) {
    const mask = el('div', 'modal-mask');
    const card = el('div', 'modal' + (opt.wide ? ' modal-wide' : ''));
    const head = el('div', 'modal-hd');
    head.appendChild(el('div', 'modal-title', esc(opt.title)));
    const x = el('button', 'modal-x', '✕');
    x.type = 'button';
    head.appendChild(x);
    card.appendChild(head);
    if (opt.sub) card.appendChild(el('div', 'modal-sub', opt.sub));
    const body = el('div', 'modal-bd');
    (opt.body || []).forEach(n => body.appendChild(n));
    card.appendChild(body);
    const msg = el('div', 'modal-msg hidden');
    card.appendChild(msg);
    const foot = el('div', 'modal-ft');
    (opt.footer || []).forEach(n => foot.appendChild(n));
    card.appendChild(foot);
    mask.appendChild(card);
    document.body.appendChild(mask);

    function close() {
      document.removeEventListener('keydown', onEsc);
      if (mask.parentNode) mask.parentNode.removeChild(mask);
    }
    function onEsc(e) { if (e.key === 'Escape') close(); }

    x.addEventListener('click', close);
    mask.addEventListener('click', e => { if (e.target === mask) close(); });
    document.addEventListener('keydown', onEsc);

    function showErr(err) {
      msg.className = 'modal-msg err';
      msg.textContent = err.message || String(err);
      if (err.field) {
        card.querySelectorAll('.inp').forEach(i => i.classList.remove('bad'));
        const f = card.querySelector('.inp[data-field="' + err.field + '"]');
        if (f) { f.classList.add('bad'); f.focus(); }
      }
      card.querySelector('.modal-bd').scrollTop = 0;
    }
    function showOk(text) {
      msg.className = 'modal-msg ok';
      msg.textContent = text;
    }
    // 第一个输入框自动聚焦，省一次鼠标
    setTimeout(() => {
      const f = card.querySelector('.inp');
      if (f && !f.disabled && f.type !== 'file') f.focus();
    }, 30);
    return { close, showErr, showOk, card, body, msg };
  }

  function toast(text, kind) {
    const t = el('div', 'toast ' + (kind || 'ok'), esc(text));
    document.body.appendChild(t);
    setTimeout(() => t.classList.add('in'), 10);
    setTimeout(() => {
      t.classList.remove('in');
      setTimeout(() => { if (t.parentNode) t.parentNode.removeChild(t); }, 300);
    }, kind === 'err' ? 7000 : 3400);
  }

  async function refresh(reason) {
    await ctx.reload();
    if (reason) toast(reason);
  }

  /* ── 通用确认框 ───────────────────────────────── */
  /* 不用 window.confirm：没法写清连带影响（"会一起删 3 件装备"），
     而且原生框在自动化里会阻塞页面。 */
  function confirmDialog(opt) {
    const body = [];
    if (opt.warn) body.push(el('div', 'del-warn', esc(opt.warn)));
    if ((opt.details || []).length) {
      const list = el('div', 'del-list');
      opt.details.forEach(t => list.appendChild(el('div', 'del-li', esc(t))));
      body.push(list);
    }
    (opt.body || []).forEach(n => body.push(n));
    let dlg;
    const go = btn(opt.okText || '确认', opt.danger ? 'btn-danger' : 'btn-primary',
      async () => {
        try { await opt.onOk(dlg); }
        catch (e) { dlg.showErr(e); }
      });
    dlg = dialog({
      title: opt.title, sub: opt.sub, body: body,
      footer: [btn('取消', 'btn-ghost', () => dlg.close()), go], wide: !!opt.wide,
    });
    return dlg;
  }

  /* ── 表单：单位 ───────────────────────────────── */
  function unitForm(unit) {
    const isEdit = !!unit;
    const fId = input(unit && unit.unit_id, '如 A-901（全库唯一）');
    const fName = input(unit && unit.unit_name, '如 第1合成旅');
    const fSvc = select(unit ? unit.service : '陆军', SVC);
    const fBranch = input(unit && unit.branch, '如 合成兵种 / 水面舰艇');
    const fPer = numInput(unit && unit.personnel, '如 4000');
    const fBase = baseInput(unit && unit.base);
    const fReady = select(unit ? unit.readiness : '', READY_OPTS);

    const r1 = el('div', 'fld-row');
    r1.appendChild(field('单位编号 *', fId, '全库唯一。改它会连同装备一起迁到新编号', 'unit_id'));
    r1.appendChild(field('单位名称 *', fName, '', 'unit_name'));
    const r2 = el('div', 'fld-row');
    r2.appendChild(field('军种 *', fSvc, '', 'service'));
    r2.appendChild(field('兵种', fBranch, '', 'branch'));
    const r3 = el('div', 'fld-row');
    r3.appendChild(field('人员 *', fPer, '整数，如 4000', 'personnel'));
    r3.appendChild(field('驻地', fBase,
      anchors.length ? '有候选，优先选已有基地（保证环境查询可用）' : '', 'base'));
    const r4 = el('div', 'fld-row');
    r4.appendChild(field('战备状态', fReady, '只能是 一级/二级/三级', 'readiness'));
    r4.appendChild(el('div', 'fld'));

    let dlg;
    const submit = btn(isEdit ? '保存修改' : '添加', 'btn-primary', async () => {
      const payload = {
        unit: {
          unit_id: fId.value.trim(), unit_name: fName.value.trim(),
          service: fSvc.value, branch: fBranch.value.trim(),
          personnel: fPer.value.trim(), base: fBase.value.trim(),
          readiness: fReady.value,
        },
      };
      if (isEdit) payload.orig_id = unit.unit_id;
      try {
        const r = await api('/api/force/units', 'POST', payload);
        dlg.close();
        await refresh((r.action === 'create' ? '已添加单位 ' : '已保存单位 ')
          + r.unit.unit_id + '（' + r.unit.service + ' ' + r.unit.personnel + ' 人）');
        (r.warnings || []).forEach(w => toast(w, 'warn'));
      } catch (e) { dlg.showErr(e); }
    });
    dlg = dialog({
      title: isEdit ? '编辑单位' : '新增单位',
      sub: esc(isEdit
        ? '改的是全军名册，保存后参谋下次取数立即是新值'
        : '保存后立即进入兵力池，参谋下次拟案就会用到它'),
      body: [r1, r2, r3, r4],
      footer: [btn('取消', 'btn-ghost', () => dlg.close()), submit],
    });
    // 表单里回车即提交，比每次去点按钮快
    dlg.card.addEventListener('keydown', e => {
      if (e.key === 'Enter' && e.target.tagName === 'INPUT') { e.preventDefault(); submit.click(); }
    });
  }

  /* ── 表单：装备 ───────────────────────────────── */
  function unitOptions(selUnitId) {
    const units = (ctx.getData() || {}).units || [];
    const out = [];
    SVC.forEach(sv => {
      const list = units.filter(u => u.service === sv);
      if (!list.length) return;
      out.push({ v: '__h_' + sv, t: '── ' + sv + ' ──' });
      list.forEach(u => out.push({ v: u.unit_id, t: u.unit_id + ' ' + u.unit_name }));
    });
    const first = out.find(o => !o.v.startsWith('__h_'));
    const s = select(selUnitId || (first && first.v) || '', out);
    // 分组标题不可选
    [...s.options].forEach(o => { if (o.value.startsWith('__h_')) o.disabled = true; });
    return s;
  }

  function eqForm(row) {
    const isEdit = !!row;
    const fUnit = unitOptions(row && row.unit_id);
    const fModel = input(row && row.model, '如 99A / 052D');
    const fType = input(row && row.etype, '如 主战坦克 / 驱逐舰');
    const fCnt = numInput(row && row.count, '如 56');

    const r1 = el('div', 'fld-row');
    r1.appendChild(field('所属单位 *', fUnit, '这项装备挂在哪个单位名下', 'unit_id'));
    r1.appendChild(el('div', 'fld'));
    const r2 = el('div', 'fld-row');
    r2.appendChild(field('型号 *', fModel, '同一单位里型号不能重复', 'model'));
    r2.appendChild(field('类型', fType, '', 'etype'));
    const r3 = el('div', 'fld-row');
    r3.appendChild(field('数量 *', fCnt, '整数件数', 'count'));
    r3.appendChild(el('div', 'fld'));

    let dlg;
    const submit = btn(isEdit ? '保存修改' : '添加', 'btn-primary', async () => {
      const payload = {
        equipment: {
          unit_id: fUnit.value, model: fModel.value.trim(),
          etype: fType.value.trim(), count: fCnt.value.trim(),
        },
      };
      if (isEdit) payload.orig = { unit_id: row.unit_id, model: row.model };
      // 新增时禁止悄悄覆盖同型号：要改现有那条请走编辑
      else payload.create_only = true;
      try {
        await api('/api/force/equipment', 'POST', payload);
        dlg.close();
        await refresh((isEdit ? '已保存装备 ' : '已添加装备 ')
          + fModel.value.trim() + ' × ' + fCnt.value.trim());
      } catch (e) { dlg.showErr(e); }
    });
    dlg = dialog({
      title: isEdit ? '编辑装备' : '新增装备',
      sub: esc(isEdit
        ? '改「所属单位」或「型号」等于把这条记录换个身份（旧的那条会移除）'
        : '给某个单位添加一项装备。若该单位已有同型号，请用明细里的「编辑」改数量'),
      body: [r1, r2, r3],
      footer: [btn('取消', 'btn-ghost', () => dlg.close()), submit],
    });
    dlg.card.addEventListener('keydown', e => {
      if (e.key === 'Enter' && e.target.tagName === 'INPUT') { e.preventDefault(); submit.click(); }
    });
  }

  /* ── 表单：共享资源容量 ───────────────────────── */
  function resForm(r, cap) {
    const fCap = numInput(cap, '整数');
    let dlg;
    const submit = btn('保存', 'btn-primary', async () => {
      try {
        const out = await api('/api/force/resources/' + encodeURIComponent(r.res_key),
          'PUT', { capacity: fCap.value.trim() });
        dlg.close();
        await refresh('已把「' + r.res_name + '」容量改为 ' + out.resource.capacity);
        if (out.hint) toast(out.hint, 'warn');
      } catch (e) { dlg.showErr(e); }
    });
    dlg = dialog({
      title: '修改共享资源容量',
      sub: esc('这是三军共享的额度，参谋申索总量不得超过它，终验也会按这个数卡'),
      body: [field(r.res_name + ' 容量 *', fCap, '整数', 'capacity')],
      footer: [btn('取消', 'btn-ghost', () => dlg.close()), submit],
    });
  }

  /* ── 删除 ─────────────────────────────────────── */
  function delUnit(u) {
    const eqN = ((ctx.getData() || {}).equipment_rows || [])
      .filter(e => e.unit_id === u.unit_id).length;
    confirmDialog({
      title: '确认删除单位？',
      sub: esc(u.unit_id + ' ' + u.unit_name + '（' + u.service + ' · ' + u.personnel + ' 人）'),
      warn: '⚠ 删除不可撤销，且会立即影响参谋下次拟案。',
      details: [
        '该单位的 ' + u.personnel + ' 名人员会从' + u.service + '总数里扣掉',
        eqN ? '名下 ' + eqN + ' 条装备记录会一起删除（不会留给别的单位）'
            : '该单位名下没有装备记录',
        'seed/' + (u.service === '陆军' ? 'army' : u.service === '海军' ? 'navy' : 'airforce')
          + '.json 会同步更新，重建库也不会复活它',
      ],
      okText: '确认删除', danger: true, wide: true,
      onOk: async dlg => {
        const r = await api('/api/force/units/' + encodeURIComponent(u.unit_id), 'DELETE');
        dlg.close();
        await refresh('已删除 ' + r.unit_id
          + (r.equipment_deleted ? '（连带 ' + r.equipment_deleted + ' 条装备）' : ''));
      },
    });
  }

  function delEq(e) {
    confirmDialog({
      title: '确认删除装备？',
      sub: esc(e.unit_id + ' ' + e.unit_name + ' · ' + e.model + ' × ' + e.count),
      warn: '⚠ 删除不可撤销。',
      details: [
        '会从「' + e.model + '」的型号汇总里扣掉 ' + e.count + ' 件',
        '所属单位本身不受影响',
      ],
      okText: '确认删除', danger: true,
      onOk: async dlg => {
        await api('/api/force/equipment?unit_id='
          + encodeURIComponent(e.unit_id) + '&model=' + encodeURIComponent(e.model),
          'DELETE');
        dlg.close();
        await refresh('已删除装备 ' + e.model);
      },
    });
  }

  /* ── 导入 JSON ────────────────────────────────── */
  function importDialog() {
    const fSvc = select('', [{ v: '', t: '自动识别（按数据里的「军种」）' }]
      .concat(SVC.map(s => ({ v: s, t: '数据没写军种时，全部补成 ' + s }))));
    const fMode = select('merge', [
      { v: 'merge', t: '合并（推荐）—— 按编号更新已有、新增没有的' },
      { v: 'replace', t: '替换 —— 把本次涉及的军种整军种覆盖掉' },
    ]);
    const fFile = el('input', 'inp inp-file');
    fFile.type = 'file';
    fFile.accept = '.json,.txt,application/json';
    const fText = el('textarea', 'inp inp-ta');
    fText.placeholder = '把 JSON 粘在这里，或点上面的「选择文件」…\n'
      + '形如：[{"id":"A-901","单位":"示例合成旅","军种":"陆军","人员":4000,\n'
      + '        "驻地":"东山基地","战备状态":"一级",\n'
      + '        "主要装备":[{"型号":"99A","类型":"主战坦克","数量":50}]}]';
    const preview = el('div', 'imp-prev', '还没有内容。选择文件或粘贴后会先在这里校验。');

    function parse() {
      const raw = (fText.value || '').trim();
      if (!raw) return { err: '内容为空' };
      let data;
      try { data = JSON.parse(raw); }
      catch (e) { return { err: 'JSON 格式有问题：' + e.message }; }
      if (data && !Array.isArray(data) && typeof data === 'object') {
        for (const k of ['units', 'items', 'data', '单位']) {
          if (Array.isArray(data[k])) { data = data[k]; break; }
        }
      }
      if (!Array.isArray(data)) return { err: '最外层必须是数组 [ ... ]' };
      if (!data.length) return { err: '数组是空的' };
      const bySvc = {};
      let noSvc = 0;
      const ids = [];
      data.forEach(u => {
        if (!u || typeof u !== 'object') return;
        const sv = u['军种'] || u.service || fSvc.value || '';
        const key = sv || '（未标军种）';
        bySvc[key] = (bySvc[key] || 0) + 1;
        if (!sv) noSvc += 1;
        ids.push(u['id'] || u.unit_id || '');
      });
      const dups = [...new Set(ids.filter((v, i) => v && ids.indexOf(v) !== i))];
      return { data, bySvc, noSvc, dups };
    }

    function renderPreview() {
      const p = parse();
      preview.className = 'imp-prev';
      if (p.err) {
        preview.className = 'imp-prev bad';
        preview.textContent = '✗ ' + p.err;
        return null;
      }
      const parts = Object.entries(p.bySvc).map(([k, v]) => k + ' ' + v + ' 条');
      let txt = '✓ 读到 ' + p.data.length + ' 条单位：' + parts.join('、');
      if (p.noSvc) {
        txt += '\n⚠ ' + p.noSvc + ' 条没写「军种」，'
          + (fSvc.value ? '会补成「' + fSvc.value + '」' : '需要在上面选一个目标军种');
      }
      if (p.dups.length) txt += '\n⚠ 有重复编号：' + p.dups.join('、') + '（导入会被拒绝）';
      preview.textContent = txt;
      if (p.dups.length || (p.noSvc && !fSvc.value)) preview.className = 'imp-prev warn';
      return p;
    }

    fText.addEventListener('input', renderPreview);
    fSvc.addEventListener('change', renderPreview);
    fFile.addEventListener('change', () => {
      const f = fFile.files && fFile.files[0];
      if (!f) return;
      const rd = new FileReader();
      rd.onload = () => { fText.value = String(rd.result || ''); renderPreview(); };
      rd.readAsText(f, 'utf-8');
    });

    let dlg;
    async function doImport(p, closeDlg) {
      try {
        const r = await api('/api/force/import', 'POST', {
          items: p.data, mode: fMode.value, default_service: fSvc.value,
        });
        closeDlg.close();
        await refresh('导入完成：新增 ' + r.created + ' · 更新 ' + r.updated
          + (r.deleted ? ' · 替换删除 ' + r.deleted : ''));
        (r.warnings || []).forEach(w => toast(w, 'warn'));
      } catch (e) { closeDlg.showErr(e); }
    }

    const go = btn('开始导入', 'btn-primary', () => {
      const p = renderPreview();
      if (!p) return;
      if (fMode.value === 'replace') {
        const svcList = Object.keys(p.bySvc).filter(k => k !== '（未标军种）');
        confirmDialog({
          title: '确认整军种替换？',
          sub: esc('这个模式会先删掉涉及军种的现有单位，再导入新的'),
          warn: '⚠ 会删除原有数据，不可撤销。',
          details: [
            '涉及军种：' + (svcList.length ? svcList.join('、') : '（未标军种）'),
            '这些军种下现有的单位与装备都会被清掉，再按你给的数据重建',
            '其它军种不受影响',
          ],
          okText: '确认替换', danger: true, wide: true,
          onOk: async d => { d.close(); await doImport(p, dlg); },
        });
        return;
      }
      doImport(p, dlg);
    });

    const r34 = el('div', 'fld-row');
    r34.appendChild(field('③ 目标军种', fSvc, '数据里写了「军种」就以数据为准', ''));
    r34.appendChild(field('④ 导入方式', fMode, '', ''));
    const w5 = el('div', 'fld');
    w5.appendChild(el('label', 'fld-lb', '⑤ 导入前校验'));
    w5.appendChild(preview);

    dlg = dialog({
      title: '导入兵力数据（JSON）',
      sub: esc('导入即写入全军名册：参谋下次取数就是新值，同时回写 seed/*.json，'
        + '重建库不会丢。整批一个事务 —— 有一条不合格，整批都不会进库。'),
      body: [
        field('① 选择 JSON 文件', fFile, '支持 .json 文件，编码用 UTF-8'),
        field('② 或直接粘贴内容', fText, '', 'items'),
        r34, w5,
      ],
      footer: [btn('取消', 'btn-ghost', () => dlg.close()), go],
      wide: true,
    });
  }

  /* ── 导出 / 模板 / 变更记录 ───────────────────── */
  function textDialog(title, sub, text, filename) {
    const ta = el('textarea', 'inp inp-ta inp-ta-ro');
    ta.value = text;
    ta.readOnly = true;
    let dlg;
    const copy = btn('复制到剪贴板', 'btn-ghost', async () => {
      try {
        await navigator.clipboard.writeText(text);
        dlg.showOk('已复制 ' + text.length + ' 个字符到剪贴板');
      } catch (e) { dlg.showErr(new Error('复制失败，请手动全选复制：' + e.message)); }
    });
    const dl = btn('下载为文件', 'btn-primary', () => {
      const blob = new Blob([text], { type: 'application/json;charset=utf-8' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(a.href), 3000);
      dlg.showOk('已下载：' + filename + '（一般在浏览器的「下载」里）');
    });
    dlg = dialog({ title: title, sub: sub, body: [ta], footer: [copy, dl], wide: true });
  }

  async function openExport() {
    try {
      const d = await api('/api/force/export', 'GET');
      const name = '兵力池导出_' + new Date().toISOString().slice(0, 10) + '.json';
      textDialog('导出兵力池（' + d.count + ' 个单位）',
        esc('格式与 seed/*.json 完全一致，导出的文件可以直接再导入回来（改完也认）'),
        JSON.stringify(d.units, null, 2), name);
    } catch (e) { toast(e.message, 'err'); }
  }

  async function openTemplate() {
    try {
      const d = await api('/api/force/template', 'GET');
      textDialog('JSON 模板与注意事项',
        esc('照着这个写就行。下载后用记事本改，再点「导入 JSON」导进来'),
        JSON.stringify(d['样例'], null, 2)
        + '\n\n/* 写数据时注意\n'
        + (d['_注意'] || []).map((t, i) => ' * ' + (i + 1) + '. ' + t).join('\n')
        + '\n */',
        '兵力池导入模板.json');
    } catch (e) { toast(e.message, 'err'); }
  }

  async function openChangelog() {
    let d;
    try { d = await api('/api/force/changelog?limit=80', 'GET'); }
    catch (e) { toast(e.message, 'err'); return; }
    const ACT = { create: '新增', update: '修改', delete: '删除', import: '导入' };
    const box = el('div', 'log-list');
    if (!(d.changes || []).length) {
      box.appendChild(el('div', 'log-empty', '还没有改过任何东西'));
    }
    const fmt = o => {
      if (!o) return '—';
      if (o.capacity !== undefined) return '容量 ' + o.capacity;
      if (o.count !== undefined) return (o.etype || '') + ' × ' + o.count;
      if (o.personnel !== undefined) {
        return o.service + ' ' + o.personnel + ' 人'
          + (o.equipment && o.equipment.length ? ' · 装备 ' + o.equipment.length + ' 类' : '');
      }
      return JSON.stringify(o);
    };
    (d.changes || []).forEach(c => {
      const row = el('div', 'log-item');
      const hd = el('div', 'log-hd');
      hd.appendChild(el('span', 'log-act act-' + c.action, esc(ACT[c.action] || c.action)));
      hd.appendChild(el('span', 'log-tg mono', esc(c.target)));
      hd.appendChild(el('span', 'log-ts', esc(c.ts)));
      row.appendChild(hd);
      row.appendChild(el('div', 'log-diff', esc(fmt(c.before) + '  →  ' + fmt(c.after))));
      box.appendChild(row);
    });
    dialog({
      title: '兵力变更记录',
      sub: esc('只增不删：重建兵力池也不会清掉这些记录，方便回答"这个数字什么时候变的"'),
      body: [box], footer: [], wide: true,
    });
  }

  /* ── 工具栏 ───────────────────────────────────── */
  function buildToolbar() {
    const bar = ctx.toolbar;
    bar.innerHTML = '';
    const mk = (label, cls, title, fn) => {
      const b = el('button', 'btn ' + cls, esc(label));
      b.type = 'button';
      b.title = title;
      b.setAttribute('data-act', label);
      b.addEventListener('click', fn);
      return b;
    };
    bar.appendChild(mk('+ 新增单位', 'btn-primary', '手动添加一个单位', () => unitForm(null)));
    bar.appendChild(mk('+ 新增装备', 'btn-ghost', '给单位添加装备', () => eqForm(null)));
    bar.appendChild(mk('导入 JSON', 'btn-ghost', '批量导入陆军/海军/空军的兵力数据',
      importDialog));
    bar.appendChild(mk('导出当前', 'btn-ghost', '导出成与 seed 相同格式的 JSON', openExport));
    bar.appendChild(mk('JSON 模板', 'btn-ghost', '看字段怎么写', openTemplate));
    bar.appendChild(mk('变更记录', 'btn-ghost', '谁在什么时候改了什么', openChangelog));
  }

  async function loadAnchors() {
    try {
      const d = await api('/api/env/anchors', 'GET');
      anchors = d.anchors || [];
    } catch (e) { anchors = []; }
  }

  function mount(opts) {
    ctx = opts;
    buildToolbar();
    loadAnchors();
  }

  window.ForceAdminUI = {
    mount, unitForm, eqForm, resForm, delUnit, delEq, confirmDialog, importDialog,
    changelogDialog: openChangelog, templateDialog: openTemplate,
    exportDialog: openExport, toast,
  };
})();
