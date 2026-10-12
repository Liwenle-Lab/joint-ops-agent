/* app.js —— 控制台前端
 *
 * 两条约束决定了这里的写法：
 *  1. 必须 POST 发 SSE（任务文本不适合塞 URL）→ EventSource 用不了，只能手写 fetch + ReadableStream 解析。
 *  2. 全链路要跑 1~3 分钟 → 界面必须持续给反馈。流水线、日志、计时器都是为此存在，不是装饰。
 */
'use strict';

const $ = (s) => document.querySelector(s);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
};
const esc = (s) => String(s ?? '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

/* ── 全局报错可视化 ───────────────────────────────
   血的教训：`chatReset` 未定义时，点「历史会话」抛 ReferenceError、函数当场中断，
   界面上就是"点了没反应"，用户没办法、我也只剩猜。
   所以任何未捕获异常 / 未处理 Promise 拒绝，都必须在页面上红条显示出来。 */
function showFatal(msg) {
  let b = document.getElementById('fatalBar');
  if (!b) {
    b = el('div', 'fatal-bar');
    b.id = 'fatalBar';
    document.body.insertBefore(b, document.body.firstChild);
  }
  const line = el('div', '', esc(msg));
  b.appendChild(line);
  b.style.display = 'block';
  console.error('[致命]', msg);
}
window.addEventListener('error', (e) => {
  showFatal('脚本出错：' + (e.message || e.error) +
            (e.filename ? `（${String(e.filename).split('/').pop()}:${e.lineno}）` : ''));
});
window.addEventListener('unhandledrejection', (e) => {
  showFatal('有个操作没完成：' + (((e.reason || {}).message) || e.reason));
});

const S = {
  threadId: localStorage.getItem('threadId') || '',
  running: false,
  t0: 0,
  caps: {},
  lastResult: null,
  planMeta: '',
  turns: 0,               // 当前会话已进行的轮次，用于"记忆 N 轮"
};

/* ── 流水线定义 ──────────────────────────────────
   alias 用来匹配后端 trace 的 tag。staff 会拆成三份并行，各自单独打点。 */
const STAGES = [
  { id: 'intent',    name: '意图路由',   al: ['intent'] },
  { id: 'commander', name: '指挥拆解',   al: ['commander'] },
  { id: 'dispatch',  name: '任务分发',   al: ['dispatch'] },
  { id: 'staff',     name: '参谋规划',   al: ['staff'], multi: true },
  { id: 'collect',   name: '汇总冲撞',   al: ['collect'] },
  { id: 'negotiate', name: '协商/收敛',  al: ['negotiate', 'check'] },
  { id: 'arbitrate', name: '联合仲裁',   al: ['arbitrate'] },
  { id: 'validate',  name: '终验闸门',   al: ['validate'] },
];
const hit = (st, tag) => st.al.some(a => tag === a || tag.startsWith(a + ':'));

/* ── 顶栏 ──────────────────────────────────────── */
function setBusy(on, label) {
  const b = $('#busyBadge');
  b.className = 'pill ' + (on ? 'pill-busy' : '');
  b.textContent = on ? (label || '执行中') : '空闲';
}

async function loadHealth() {
  try {
    const h = await (await fetch('/api/health')).json();
    const c = await (await fetch('/api/constraints')).json();
    // 兵力上限与四类资源放同一张表里，指标卡才能显示"值 / 容量"
    S.caps = Object.assign({}, c.resource_cap || {}, { force: c.force_cap });
    const badge = $('#modelBadge');
    badge.textContent = h.mocked ? 'MOCK 模式' : h.model_main;
    badge.className = 'pill ' + (h.mocked ? 'pill-mock' : 'pill-live');
  } catch (e) {
    $('#modelBadge').textContent = '后端未连接';
  }
}

/* ── 流水线渲染 ────────────────────────────────── */
/* 参谋三个军种是并行的，流水线下方单独一行显示谁回来了 */
function setStaffNote(txt) {
  const n = $('#staffNote');
  n.textContent = txt;
  n.classList.toggle('hidden', !txt);
}

function initPipeline() {
  const box = $('#pipeline');
  box.innerHTML = '';
  STAGES.forEach(st => {
    const n = el('div', 'step');
    n.id = 'st-' + st.id;
    const dot = el('span', 'dot', '');
    dot.id = 'dot-' + st.id;
    n.appendChild(dot);
    n.appendChild(el('span', 'step-lb', st.name));
    box.appendChild(n);
  });
}

function resetPipeline() {
  STAGES.forEach(st => {
    const n = $('#st-' + st.id);
    n.className = 'step';
    delete n.dataset.done;                 // 参谋的"谁回来了"要一并清掉
    $('#dot-' + st.id).textContent = '';
  });
  setStaffNote('');
}

function markStage(tag) {
  let idx = STAGES.findIndex(st => hit(st, tag));
  if (idx < 0) return;
  const st = STAGES[idx];

  // 参谋三个军种并行：分别记下谁回来了
  if (st.multi) {
    const svc = tag.split(':')[1];
    if (svc) {
      const node = $('#st-' + st.id);
      const done = new Set((node.dataset.done || '').split('|').filter(Boolean));
      done.add(svc);
      node.dataset.done = [...done].join('|');
      setStaffNote('参谋已回：' + [...done].join(' / '));
      // 未到齐仍标 active，到齐才算完成
      node.className = 'step ' + (done.size >= 3 ? 'done' : 'active');
      $('#dot-' + st.id).textContent = done.size >= 3 ? '✓' : '';
      return;
    }
  }

  const node = $('#st-' + st.id);
  node.className = 'step done';
  $('#dot-' + st.id).textContent = '✓';
  // 其余仍在 active 的节点收尾为 done
  STAGES.forEach((o, i) => {
    if (i !== idx) {
      const on = $('#st-' + o.id);
      if (on.className.includes('active')) {
        on.className = 'step done';
        $('#dot-' + o.id).textContent = '✓';
      }
    }
  });

  // 点亮下一站，让"正在跑哪一步"可见（节点自身只在完成时才有事件）
  const nxt = STAGES[idx + 1];
  if (nxt && nxt.id !== st.id) {
    const on = $('#st-' + nxt.id);
    if (!on.className.includes('done')) {
      on.className = 'step active';
      $('#dot-' + nxt.id).textContent = '';
    }
  }
}

function markFailed() {
  STAGES.forEach(st => {
    const n = $('#st-' + st.id);
    if (n.className.includes('active')) {
      n.className = 'step fail';
      $('#dot-' + st.id).textContent = '✕';
    }
  });
}

/* ── 日志 ──────────────────────────────────────── */
function logClear() {
  $('#log').innerHTML = '';
}
function logPush(cls, timeTxt, tag, msg) {
  const box = $('#log');
  const empty = box.querySelector('.log-empty');
  if (empty) empty.remove();
  const line = el('div', 'log-line ' + (cls || ''));
  line.appendChild(el('span', 'log-t', esc(timeTxt)));
  line.appendChild(el('span', 'log-tag', esc(tag)));
  line.appendChild(el('span', 'log-msg', esc(msg)));
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

/* ── 结果渲染 ──────────────────────────────────── */
function renderVerdict(v) {
  const card = $('#verdict');
  const ok = v.ok === true;
  card.className = 'verdict ' + (ok ? 'pass' : 'fail');   // 覆盖 hidden，直接显示
  $('#verdictCode').textContent = v.code || (ok ? 'PASS' : 'FAIL');
  const metas = [];
  if (v.unit_count !== undefined) metas.push(`单位 ${v.unit_count}`);
  $('#verdictMeta').textContent = metas.join(' · ');
  const ul = $('#verdictList');
  ul.innerHTML = '';
  (v.violations || []).forEach(t => ul.appendChild(el('li', '', esc(t))));
}

function renderResourceKV(alloc) {
  const grid = el('div', 'kv-grid');
  Object.entries(alloc || {}).forEach(([k, v]) => {
    // 正常路径下 v 已是后端归一好的数字；仍兼容 dict 只是兜底，
    // 且这里绝不自己求和——"预备队不算投入"这类规则只应存在于后端。
    let num = v;
    if (typeof v === 'object' && v !== null) {
      num = (v['总计'] ?? v['合计'] ?? v['total'] ?? null);
      if (num === null) {
        num = Object.entries(v)
          .filter(([kk]) => !/预备|reserve|合计|总计|total/i.test(kk))
          .reduce((a, [, vv]) => a + (typeof vv === 'number' ? vv : 0), 0);
      }
    }
    const cap = S.caps[k];
    const over = typeof cap === 'number' && typeof num === 'number' && num > cap;
    const kv = el('div', 'kv' + (typeof cap === 'number' ? (over ? ' bad' : '') : ''));
    kv.appendChild(el('span', 'kv-k', esc(k)));
    kv.appendChild(el('span', 'kv-v' + (typeof cap === 'number'
      ? (over ? ' over' : ' fit') : ''), esc(num ?? '—')));
    if (typeof cap === 'number') {
      kv.appendChild(el('span', 'kv-cap', `容量 ${cap}`));
    }
    grid.appendChild(kv);
  });
  return grid;
}

function renderSection(title, body) {
  const sec = el('div', 'sec');
  sec.appendChild(el('div', 'sec-title', esc(title)));
  if (Array.isArray(body)) {
    if (body.length && typeof body[0] === 'object' && body[0].start !== undefined) {
      const list = el('ul', 'ts-list');
      body.forEach(it => {
        const li = el('li', 'ts-item');
        li.appendChild(el('span', 'ts-range', esc(`${it.start} → ${it.end}`)));
        li.appendChild(el('span', 'ts-act', esc(it.action || '')));
        list.appendChild(li);
      });
      sec.appendChild(list);
    } else {
      const list = el('ul');
      body.forEach(t => list.appendChild(
        el('li', '', esc(typeof t === 'object' ? JSON.stringify(t) : t))));
      sec.appendChild(list);
    }
  } else if (typeof body === 'object' && body !== null) {
    sec.appendChild(renderResourceKV(body));
  } else {
    sec.appendChild(el('div', 'sec-body', esc(body)));
  }
  return sec;
}

function renderResult(r) {
  const planBox = $('#planBox');
  planBox.innerHTML = '';

  if (r.answer) {
    const ab = $('#answerBox');
    ab.classList.remove('hidden');
    ab.textContent = r.answer;
  } else {
    $('#answerBox').classList.add('hidden');
  }

  const fp = r.final_plan || {};
  if (Object.keys(fp).length) {
    for (const [k, v] of Object.entries(fp)) {
      if (k === '数字来源') continue;
      // 资源分配用后端归一后的数字（预备队不计入投入），其余章节原样渲染
      planBox.appendChild(renderSection(
        k, (k === '资源分配' && Object.keys(r.resource_used || {}).length)
          ? r.resource_used : v));
    }
  } else {
    const empty = r.clarify_question
      ? ('需要澄清：' + r.clarify_question)
      : (r.route === 'single_domain'
        ? '单域查询已直接作答，无需联合方案'
        : '本次未产出联合方案');
    planBox.appendChild(el('div', 'log-empty', esc(empty)));
  }

  // 参谋方案
  const stBox = $('#staffBox');
  stBox.innerHTML = '';
  if (!(r.staff || []).length) {
    stBox.appendChild(el('div', 'log-empty', '本次未调用参谋'));
  }
  (r.staff || []).forEach(s => {
    const card = el('div', 'svc-card svc-' + s.domain);
    const head = el('div', 'svc-head');
    head.appendChild(el('span', 'svc-name', esc(s.domain)));
    const asks = Object.entries(s.asks || {})
      .map(([k, v]) => `${k} ${v}`).join(' · ') || '—';
    head.appendChild(el('span', 'mono dim', esc(asks)));
    card.appendChild(head);

    if (s.answer) {
      card.appendChild(el('div', 'sec-body', esc(s.answer)));
    }
    (s.units || []).forEach(u => {
      const row = el('div', 'unit-row');
      row.appendChild(el('span', '', esc(u.unit_id)));
      row.appendChild(el('span', '', esc(
        u.personnel !== undefined ? `${u.personnel} 人` : '')));
      card.appendChild(row);
    });
    (s.timeline || []).forEach(t => {
      const row = el('div', 'unit-row');
      row.appendChild(el('span', 'ts-range', esc(`${t.start} → ${t.end}`)));
      row.appendChild(el('span', 'ts-act', esc(t.action || '')));
      card.appendChild(row);
    });
    stBox.appendChild(card);
  });

  renderVerdict(r.validation || {});

  if (r.plan_version) {
    let meta = `v${r.plan_version}`;
    if (r.is_incremental) {
      meta += ` · 增量(变更 ${(r.changed_keys || []).join(',') || '无'})`;
      if ((r.reused || []).length) meta += ` · 复用 ${r.reused.join('/')}`;
      if ((r.impacted || []).length) meta += ` · 重算 ${r.impacted.join('/')}`;
    }
    // 存起来由 done 分支统一输出：这里直接 push 会跑到结果气泡前面去，顺序就乱了
    S.planMeta = meta;
  } else {
    S.planMeta = '';
  }
  loadBlackboard(r.bb_session);
}

/* ── 黑板 ──────────────────────────────────────── */
async function loadBlackboard(session) {
  const box = $('#bbBox');
  const q = session ? ('?session=' + encodeURIComponent(session)) : '';
  try {
    const d = await (await fetch('/api/blackboard' + q)).json();
    $('#bbSession').textContent = d.session ? ('session ' + d.session) : '';
    box.innerHTML = '';
    if (!(d.rounds || []).length) {
      box.appendChild(el('div', 'log-empty', '本次未发生资源冲突，无需协商'));
      return;
    }
    const byRound = {};
    d.rounds.forEach(r => { (byRound[r.round] ||= []).push(r); });
    Object.entries(byRound).forEach(([rnd, items]) => {
      const wrap = el('div', 'bb-round');
      wrap.appendChild(el('div', 'bb-round-hd', '第 ' + rnd + ' 轮'));
      items.forEach(it => {
        const e = el('div', 'bb-entry svc-' + it.speaker);
        e.appendChild(el('span', 'bb-spk', esc(it.speaker + '  ')));
        const asks = Object.entries(it.asks || {}).map(([k, v]) => `${k} ${v}`).join(' · ');
        e.appendChild(el('span', 'mono dim', esc(asks || '')));
        if (it.concession) {
          e.appendChild(el('div', 'bb-conc',
            `<span class="bb-label">让步</span>${esc(it.concession)}`));
        }
        if (it.objection) {
          e.appendChild(el('div', 'bb-obj',
            `<span class="bb-label">反对</span>${esc(it.objection)}`));
        }
        wrap.appendChild(e);
      });
      box.appendChild(wrap);
    });
  } catch (e) {
    box.innerHTML = '<div class="log-empty">黑板读取失败</div>';
  }
}

/* ── 会话记忆 ──────────────────────────────────── */
function setMemTag(n) {
  const t = $('#memTag');
  t.textContent = n > 0 ? ('记忆 ' + n + ' 轮') : '记忆 0 轮';
  t.className = 'thread-tag' + (n > 0 ? ' on' : '');
}

/* 一轮的"回话"怎么显示。
   ★ 顺序不能颠倒：单域直答（answer）优先，其次才是方案。
   规划轮次在服务端已把"继承来的 answer"清空（State 里 answer 是持久通道，
   规划轮次不写它，所以快照里留着的是上一轮那句快问回答 —— 不清就会串轮），
   所以这里不会再把上一轮的回答当成这一轮的结论。 */
function resultSummary(r) {
  r = r || {};
  if (r.clarify_question) return '需要澄清：' + r.clarify_question;
  if (r.route === 'out_of_scope') return '【领域外】本系统只处理联合作战筹划任务。';
  if (r.answer) return r.answer;
  /* ★ has_new_plan === false（注意是 false，不是 undefined）：
     历史接口明确告诉我们——快照里这份 final_plan 是上一轮继承来的，
     本轮并没有产出新方案。不拦在这里，气泡就会写成
     "已生成联合决心方案（v2）· 终验 PASS"，把一次没跑完的轮次说成成功。
     典型场景：执行中途报错时，State 里挂着的仍是上一版的方案和终验结果。
     界面不许替系统撒谎，所以这里要如实说"没有新方案、右栏是上一版"。
     实时执行的结果对象没有这个字段（undefined），照常按本轮成果渲染。 */
  if (r.has_new_plan === false) {
    const n = Object.keys(r.final_plan || {}).length;
    return n ? '本轮没有产出新方案 —— 右栏显示的仍是上一版（' + n + ' 节）。'
             : '本轮没有产出结果。';
  }
  const secs = Object.keys(r.final_plan || {});
  const code = (r.validation || {}).code;
  if (secs.length) {
    return `已生成联合决心方案${r.plan_version ? '（v' + r.plan_version + '）' : ''}：`
      + secs.join('、') + (code ? ` · 终验 ${code}` : '');
  }
  if (code) return `已生成联合方案，终验 ${code}`;
  return '（本轮未留下文字结论）';
}

function turnSummary(t) {
  /* ★ 中断轮次必须先说清楚。
     State 是累积的：一轮崩在中间时，快照里挂着的 final_plan / validation
     全是上一轮继承来的。不拦在这里，气泡就会显示成"已生成联合方案，终验 PASS"
     —— 用户明明看到过报错，回头翻历史却成了成功的一轮。
     这是本项目最不能有的错：界面不许替系统撒谎。 */
  if (t.failed) {
    const why = (t.error || '未知原因').slice(0, 90);
    return '⚠ 本轮执行中断（未产出结果）：' + why;
  }
  if (t.answer) return t.answer;          // 历史接口已把这个字段按轮次归位
  /* ★ has_new_plan === false：快照里这份方案是上一轮继承来的，本轮没产出。
     不拦住的话，下面的 resultSummary 会把它渲染成
     "已生成联合决心方案（v2）· 终验 PASS" —— 把一次没跑完的轮次说成成功。
     语气要分两种：
     · full_chain（规划轮次）本该出方案却没出 = 多半中途断了，要说"没跑完"；
     · 单域快问本来就不出方案，这是正常的，不该用报警的口吻。 */
  const r = t.result || {};
  if (r.has_new_plan === false) {
    const n = Object.keys(r.final_plan || {}).length;
    if (!n) return '本轮没有产出结果。';
    return t.route === 'full_chain'
      ? '本轮没有跑完（未产出新方案），右栏仍是更早一版（' + n + ' 节）。'
      : '本轮没有产出新方案，右栏显示的是上一版（' + n + ' 节）。';
  }
  return resultSummary(r);
}

/* 这一轮是不是"本该出方案却没出"。
   规划轮次（full_chain）正常都会产出方案；没产出多半是中途断了。
   与 t.failed（服务端明确留痕的中断）分开：这个是靠结果反推的，
   用于覆盖"崩溃发生在留痕机制之前"的老历史 —— 说不出原因，但绝不假装成功。 */
function isUnfinishedPlan(t) {
  if (!t || t.failed) return false;
  if (t.route !== 'full_chain') return false;      // 快问/澄清/领域外本来就不出方案
  const r = t.result || {};
  return r.has_new_plan === false && !r.clarify_question;
}

function turnMeta(t) {
  if (t.failed) {
    return (t.ts ? t.ts.slice(11, 19) + ' · ' : '')
      + (t.no_trace ? '没能留下执行记录' : '下方方案是更早一轮的，不是本轮的');
  }
  /* 规划轮次没产出方案时，快照里的版本号与资源占用全是继承来的。
     照常列出来（"v2 · 增量(deadline_days) · transport 50"）会让人
     以为这一轮真的产出了 v2 —— 同样是在替系统撒谎。只报时间 + 一句实话。 */
  if (isUnfinishedPlan(t)) {
    return (t.ts ? t.ts.slice(11, 19) + ' · ' : '')
      + '本轮没有产出方案，右栏是更早一轮的';
  }
  const bits = [];
  if (t.ts) bits.push(t.ts.slice(11, 19));
  if (t.plan_version) bits.push('v' + t.plan_version);
  if (t.is_incremental) {
    bits.push('增量' + ((t.changed_keys || []).length
      ? '(' + t.changed_keys.join(',') + ')' : ''));
  }
  const res = Object.entries(t.resource_used || {})
    .map(([k, v]) => k + ' ' + v).join(' · ');
  if (res) bits.push(res);
  return bits.join(' · ');
}

async function loadSessions() {
  const box = $('#histList');
  box.innerHTML = '<div class="log-empty">读取中…</div>';
  try {
    const d = await (await fetch('/api/sessions')).json();
    box.innerHTML = '';
    const list = d.sessions || [];
    if (!list.length) {
      box.appendChild(el('div', 'log-empty', '还没有历史会话'));
      return [];
    }
    list.forEach(s => {
      const it = el('div', 'hist-item' + (s.thread_id === S.threadId ? ' cur' : ''));
      it.appendChild(el('div', 'hist-t', esc(s.title)));
      it.appendChild(el('div', 'hist-m',
        esc(`${s.date} ${s.last} · ${s.turns} 轮 · ${s.thread_id}`)));
      it.addEventListener('click', () => openSession(s.thread_id));
      box.appendChild(it);
    });
    return list;
  } catch (e) {
    box.innerHTML = '<div class="log-empty">历史会话读取失败</div>';
    return [];
  }
}

/* 打开一个历史会话：会话区还原对话，右栏还原最后那版方案。
   还原完仍然可继续追问 —— 服务端记忆接着这一轮往上叠加。 */
async function openSession(tid, silent) {
  if (!tid) return 0;
  if (S.running) {
    // 以前这里静默 return，用户点了没反应也不知道为什么 —— 必须说出来
    pushMeta('正在筹划中，等这一轮跑完再切换会话。');
    return 0;
  }
  S.threadId = tid;
  localStorage.setItem('threadId', tid);
  $('#threadTag').textContent = '会话 ' + tid;
  chatReset();
  pushMeta('正在载入会话 ' + tid + ' …');
  if (!silent) $('#histPanel').classList.add('hidden');
  let d;
  try {
    const r = await fetch('/api/history?thread_id=' + encodeURIComponent(tid),
                          { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    d = await r.json();
  } catch (e) {
    chatReset();
    pushMeta('历史读取失败：' + e.message + '（服务还在跑吗？）');
    showFatal('读取历史会话失败：' + e.message);
    return 0;
  }
  chatReset();                                 // 去掉"正在载入"那行
  const turns = d.turns || [];
  setMemTag(turns.length);
  if (!turns.length) {
    pushMeta('这个会话里还没有可还原的轮次。');
    return 0;
  }
  // 这个会话已有的评价一起取回来回显 —— 否则用户看到一个空的反馈条，
  // 会以为上次点的没生效，再点一遍（后端会覆盖，但观感很糟）。
  const fbMap = {};
  try {
    const fr = await fetch('/api/feedback?thread_id=' + encodeURIComponent(tid),
                           { cache: 'no-store' });
    if (fr.ok) {
      const fj = await fr.json();
      (fj.items || []).forEach(x => { fbMap[x.turn_index] = x; });
    }
  } catch (_) { /* 反馈是增值功能，读不到不影响会话还原 */ }

  turns.forEach((t, i) => {
    const sum = turnSummary(t);
    pushMsg('me', t.text);
    /* 中断、以及"规划轮次没产出方案"，都用警示色。
       语气平实但外观要能一眼看出异常 —— 否则用户扫过去会以为这轮正常。 */
    pushMsg('ai', sum, (t.failed || isUnfinishedPlan(t)) ? 'warn' : '');
    pushMeta(turnMeta(t));
    /* 中断的轮次不给反馈按钮：它压根没有回答可供评价。
       turn_index 照旧用 i+1（=第几轮），跳过一次渲染不会让后续轮次错位。 */
    if (!t.failed) {
      pushFeedback(i + 1, { question: t.text, result: t.result, summary: sum },
                   fbMap[i + 1]);
    }
  });

  /* 右栏只认"真产出过方案"的那一轮。
     单域快问轮次不产出新方案，快照里的 final_plan 是从上一轮继承来的 ——
     拿它渲染右栏，等于把上一版的方案冒充成本轮成果。 */
  let pi = -1;
  turns.forEach((t, i) => { if (t.has_new_plan) pi = i; });
  const lastR = turns[turns.length - 1].result || {};
  if (pi >= 0) {
    S.lastResult = turns[pi].result;
    S.planMeta = '';
    renderResult(turns[pi].result);
    if (pi !== turns.length - 1) {
      pushMeta(`右栏显示的是第 ${pi + 1} 轮生成的方案（本会话目前最近一版）。`);
    }
  } else if (lastR.answer) {
    renderResult(lastR);                       // 纯单域问答的会话：只在答案框里给结论
  } else {
    S.lastResult = null;
    $('#planBox').innerHTML =
      '<div class="log-empty">这个会话里还没生成过联合方案</div>';
    $('#staffBox').innerHTML = '<div class="log-empty">本次未调用参谋</div>';
    $('#answerBox').classList.add('hidden');
    $('#verdict').className = 'verdict hidden';
  }

  pushMeta(`已载入 ${turns.length} 轮历史。`);
  pushRestoreActions((turns[pi >= 0 ? pi : turns.length - 1].result) || {});
  return turns.length;
}

/* ── 会话区 ────────────────────────────────────── */
/* 清空会话区（切会话/开新会话时用）。
   注意：清掉的只是"屏幕上的字"，记忆不在这里 —— 每轮提问都存在服务端的
   checkpoints 表里，所以刷新、关浏览器、重启服务都还在，点「历史会话」
   随时能整轮翻回来。前端刻意不做第二份缓存，避免两边对不上。 */
function chatReset() {
  const log = $('#chatLog');
  if (log) log.innerHTML = '';
  S.lastResult = null;
}

function pushMsg(who, text, cls) {
  const log = $('#chatLog');
  const m = el('div', 'msg msg-' + who);
  m.appendChild(el('div', 'bubble' + (cls ? ' ' + cls : ''), esc(text)));
  log.appendChild(m);
  log.scrollTop = log.scrollHeight;
}
function pushMeta(text) {
  const log = $('#chatLog');
  log.appendChild(el('div', 'msg-meta', esc(text)));
  log.scrollTop = log.scrollHeight;
}

/* ── 浮动提示 ──────────────────────────────────── */
let _toastEl = null, _toastTimer = 0;
function toast(text, kind) {
  if (!_toastEl) {
    _toastEl = el('div', 'toast');
    document.body.appendChild(_toastEl);
  }
  _toastEl.className = 'toast' + (kind ? ' ' + kind : '');
  _toastEl.textContent = text;
  requestAnimationFrame(() => _toastEl.classList.add('in'));
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => _toastEl.classList.remove('in'), 3400);
}

/* ── 会话尾部的可点入口 ──────────────────────────
   以前"已载入 N 轮历史（继续追问会接着这个会话）"只是一行灰字 ——
   说了能继续，却没给出可点的东西。现在统一渲染成一排按钮。
   run 用函数而不是链接：追问是"把话填进输入框再发出去"，不是跳页面。 */
function pushActions(items) {
  if (!items.length) return;
  const log = $('#chatLog');
  const wrap = el('div', 'act-row');
  items.forEach(it => {
    const b = el('button', 'act-chip' + (it.primary ? ' primary' : ''), esc(it.label));
    if (it.hint) b.title = it.hint;
    b.addEventListener('click', () => it.run());
    wrap.appendChild(b);
  });
  log.appendChild(wrap);
  log.scrollTop = log.scrollHeight;
}

function focusInput() {
  const box = $('#input');
  box.focus();
  box.scrollIntoView({ block: 'nearest' });
  toast('输入框已就绪：接着这个会话往下问就行', 'ok');
}

function askNow(text) {
  const box = $('#input');
  box.value = text;
  box.focus();
  send();
}

/* 上下文相关的"一键追问"。刻意用确定性模板，而不是让模型现编：
   建议点下去就会真的发出去，编错了就等于把用户带沟里。 */
function suggestNext(r) {
  if (!r) return [];
  if (r.clarify_question) return ['陆军 3 个单位、海军 2 个支队，5 天内投送'];
  if (r.route === 'single_domain') {
    return ['按这个口径再来一份海军的', '基于这个结果做一份联合筹划方案'];
  }
  if (Object.keys(r.final_plan || {}).length) {
    return ['运输机运力减半，重新做一版', '把时间压到 2 天以内，再给一版'];
  }
  return [];
}

/* 实时回答之后：给"继续追问"入口 + 两条一键追问 */
function pushFollowUps(r) {
  const items = [{ label: '继续追问', hint: '把光标放到输入框，接着这个会话往下问',
                   run: focusInput, primary: true }];
  suggestNext(r).forEach(s => items.push({
    label: '试一试：' + s, hint: '点一下就直接发出去（在同一会话里追问）',
    run: () => askNow(s),
  }));
  pushActions(items);
}

/* 会话还原之后：继续追问 + 翻历史 + 开新会话，都是真能点的 */
function pushRestoreActions(suggestFrom) {
  const items = [
    { label: '继续追问', hint: '接着这个会话往下问', run: focusInput, primary: true },
    { label: '翻历史会话', hint: '看看别的会话聊过什么', run: async () => {
      $('#histPanel').classList.remove('hidden');
      await loadSessions();
    } },
    { label: '开新会话', hint: '从零开始（旧会话不会丢）', run: () => $('#newSession').click() },
  ];
  suggestNext(suggestFrom).forEach(s => items.push({
    label: '试一试：' + s, hint: '点一下就直接发出去（接着这个会话往下问）',
    run: () => askNow(s),
  }));
  pushActions(items);
}

/* ── 答案反馈（点赞 / 点踩）────────────────────────
   为什么要做：这套系统的"学习"是把反馈带进下一轮筹划的提示词
   （见 src/tools/feedback.py 的 digest），不是训练模型权重 —— 本机跑不动
   微调，这么做才诚实。所以反馈必须锚定到"哪一轮的哪句回答"，
   否则下一轮拿到手只是一句没有指代的抱怨。
   身份 = (会话号, 第几轮)：一轮只有一条，改主意就是覆盖。 */
const FB_REASONS = ['数字对不上', '没说依据', '太笼统', '时间线不可行',
                    '兵力/资源分配不合理', '没按我的要求改', '格式难读', '其它'];

async function refreshFeedbackStat() {
  const t = $('#fbStat');
  if (!t) return;
  try {
    const d = await (await fetch('/api/feedback/summary', { cache: 'no-store' })).json();
    if (!d.total) { t.textContent = '还没有反馈'; t.title = fallback(); return; }
    t.textContent = `反馈 ${d.up} 赞 / ${d.down} 踩`;
    t.title = d.digest || fallback();
  } catch (_) { t.textContent = ''; }
  function fallback() {
    return '点回答下方的 👍 / 👎 提交反馈；否定反馈会被带进下一轮筹划的提示词';
  }
}

function pushFeedback(turnIndex, turn, existing) {
  const row = el('div', 'fb-row');
  row.dataset.turn = String(turnIndex);
  const st = {
    rating: existing ? (existing.rating || 0) : 0,
    reasons: (existing && existing.reasons) || [],
    note: (existing && existing.note) || '',
  };
  let panel = null;
  let draft = { reasons: [], note: '' };

  function api(rating, reasons, note) {
    if (!S.threadId) return;
    const r0 = turn.result || {};
    fetch('/api/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        thread_id: S.threadId, turn_index: turnIndex, rating,
        reasons: reasons || [], note: note || '',
        question: turn.question || '', answer: turn.summary || '',
        route: r0.route || null, plan_version: r0.plan_version || null,
        has_plan: Object.keys(r0.final_plan || {}).length > 0,
      }),
    }).then(r => {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }).then(() => {
      st.rating = rating; st.reasons = reasons || []; st.note = note || '';
      draft = { reasons: st.reasons.slice(), note: st.note };
      render();
      toast(rating > 0 ? '已记下：这条回答有帮助'
                       : '已记下：这条不准确 —— 下一轮筹划会避开这个问题', 'ok');
      refreshFeedbackStat();
    }).catch(e => showFatal('反馈没提交成功：' + e.message));
  }

  function closePanel() { if (panel) { panel.remove(); panel = null; } }

  function buildPanel() {
    panel = el('div', 'fb-panel');
    panel.appendChild(el('div', 'fb-panel-hd',
      '哪里不好？选一条或多条 —— 会带进下一轮筹划的提示词'));
    const chips = el('div', 'fb-chips');
    FB_REASONS.forEach(name => {
      const c = el('button', 'fb-chip' + (draft.reasons.includes(name) ? ' on' : ''),
                   esc(name));
      c.addEventListener('click', () => {
        const i = draft.reasons.indexOf(name);
        if (i >= 0) draft.reasons.splice(i, 1); else draft.reasons.push(name);
        c.classList.toggle('on');
        const s = panel.querySelector('.fb-send');
        if (s) s.disabled = !draft.reasons.length;
      });
      chips.appendChild(c);
    });
    panel.appendChild(chips);
    const ta = el('textarea', 'fb-note');
    ta.rows = 2;
    ta.placeholder = '补充说明（可不填）：例如"第 3 步的运输机架次和资源表对不上"';
    ta.value = draft.note || '';
    ta.addEventListener('input', () => { draft.note = ta.value; });
    panel.appendChild(ta);
    const ft = el('div', 'fb-panel-ft');
    const cancel = el('button', 'btn btn-ghost btn-sm', '取消');
    cancel.addEventListener('click', closePanel);
    const sendB = el('button', 'btn btn-primary btn-sm fb-send', '提交反馈');
    sendB.disabled = !draft.reasons.length;
    sendB.addEventListener('click', () => {
      const rs = draft.reasons.slice(), nt = (draft.note || '').trim();
      closePanel();
      api(-1, rs, nt);
    });
    ft.appendChild(cancel);
    ft.appendChild(sendB);
    panel.appendChild(ft);
    row.appendChild(panel);
    ta.focus();
  }

  function render() {
    closePanel();
    row.innerHTML = '';
    if (st.rating) {
      const txt = st.rating > 0
        ? '已记下 👍 这条有帮助'
        : ('已记下 👎 这条不准确'
           + (st.reasons.length ? '（' + st.reasons.join('、') + '）' : ''));
      row.appendChild(el('span', 'fb-done ' + (st.rating > 0 ? 'up' : 'down'), esc(txt)));
      if (st.note) row.appendChild(el('span', 'fb-notetxt', '“' + esc(st.note) + '”'));
      const again = el('button', 'fb-mini', '改一下');
      again.title = '同一轮只有一条记录，改了会覆盖';
      again.addEventListener('click', () => {
        draft = { reasons: st.reasons.slice(), note: st.note || '' };
        st.rating = 0;
        render();
      });
      row.appendChild(again);
      return;
    }
    row.appendChild(el('span', 'fb-q', '这条回答'));
    const up = el('button', 'fb-btn up', '👍 有帮助');
    up.title = '记下"这类回答有用"，正面偏好也会带进下一轮';
    up.addEventListener('click', () => api(1, [], ''));
    const down = el('button', 'fb-btn down', '👎 不准确');
    down.title = '记下问题在哪，下一轮筹划会避开';
    down.addEventListener('click', () => {
      if (panel) { closePanel(); return; }      // 再点一次 = 收起
      buildPanel();
    });
    row.appendChild(up);
    row.appendChild(down);
  }

  render();
  $('#chatLog').appendChild(row);
  $('#chatLog').scrollTop = $('#chatLog').scrollHeight;
}

/* ── 主流程 ────────────────────────────────────── */
async function send() {
  if (S.running) return;
  const text = $('#input').value.trim();
  if (!text) return;

  S.running = true;
  $('#send').disabled = true;
  $('#input').value = '';
  setBusy(true, '执行中');
  resetPipeline();
  $('#verdict').className = 'verdict hidden';
  pushMsg('me', text);

  S.t0 = performance.now();
  const timer = setInterval(() => {
    $('#elapsed').textContent = ((performance.now() - S.t0) / 1000).toFixed(1) + 's';
  }, 100);

  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, thread_id: S.threadId }),
    });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);

    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      // SSE 以空行分帧；必须等齐完整一帧再解析，否则跨 chunk 会截断 JSON
      let sep;
      while ((sep = buf.indexOf('\n\n')) >= 0) {
        const frame = buf.slice(0, sep);
        buf = buf.slice(sep + 2);
        handleFrame(frame);
      }
    }
  } catch (e) {
    logPush('err', hhmmss(), 'NET', e.message);
    markFailed();
  } finally {
    clearInterval(timer);
    S.running = false;
    $('#send').disabled = false;
    setBusy(false);
  }
}

function handleFrame(frame) {
  let evName = 'log', data = '';
  frame.split('\n').forEach(line => {
    if (line.startsWith('event: ')) evName = line.slice(7).trim();
    else if (line.startsWith('data: ')) data += line.slice(6);
  });
  if (!data) return;
  let d;
  try { d = JSON.parse(data); } catch (_) { return; }

  switch (evName) {
    case 'node':
      markStage(d.tag);
      logPush('', d.t || hhmmss(), d.tag, d.msg);
      break;
    case 'llm':
      logPush('llm', hhmmss(), d.role,
        `${d.sec}s · ${d.chars}字 · ${d.finish}`);
      break;
    case 'warn':
      logPush('warn', hhmmss(), '重试', d.msg);
      break;
    case 'status':
      logPush('warn', hhmmss(), '排队', d.msg);
      break;
    case 'begin':
      S.threadId = d.thread_id;
      localStorage.setItem('threadId', S.threadId);
      $('#threadTag').textContent = '会话 ' + S.threadId;
      break;
    case 'hb':
      break;
    case 'done': {
      $('#elapsed').textContent = d.sec + 's';
      S.lastResult = d.result;
      renderResult(d.result);
      const r = d.result;
      const summary = resultSummary(r);
      pushMsg('ai', summary);
      pushMeta(`耗时 ${d.sec}s · 主力 ${r.calls.main} / 轻量 ${r.calls.light} 次调用`
        + (S.planMeta ? ` · ${S.planMeta}` : ''));
      S.turns += 1;
      setMemTag(S.turns);
      pushFeedback(S.turns, { question: text, result: r, summary });
      pushFollowUps(r);
      if (!((r.validation || {}).ok)) markFailed();
      break;
    }
    case 'error':
      logPush('err', hhmmss(), 'ERROR', d.msg);
      markFailed();
      pushMsg('ai', '执行失败：' + d.msg);
      break;
  }
}

const hhmmss = () => new Date().toTimeString().slice(0, 8);

/* ── 右栏切页 ──────────────────────────────────── */
function activateTab(name) {
  document.querySelectorAll('.tab').forEach(x =>
    x.classList.toggle('active', x.dataset.tab === name));
  document.querySelectorAll('.tabpane').forEach(x =>
    x.classList.toggle('active', x.id === 'tab-' + name));
}

/* ── 绑定 ──────────────────────────────────────── */
function bind() {
  $('#send').addEventListener('click', send);
  $('#input').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });
  $('#clearLog').addEventListener('click', logClear);
  $('#loadBB').addEventListener('click', () => loadBlackboard());

  $('#histBtn').addEventListener('click', async () => {
    const p = $('#histPanel');
    const opening = p.classList.contains('hidden');
    p.classList.toggle('hidden', !opening);
    if (opening) {                            // 展开时才拉，省一次开机请求
      await loadSessions();
      refreshFeedbackStat();
    }
  });
  $('#histClose').addEventListener('click', () =>
    $('#histPanel').classList.add('hidden'));

  $('#newSession').addEventListener('click', () => {
    if (S.running) return;
    S.threadId = '';
    S.planMeta = '';
    S.turns = 0;
    setMemTag(0);
    localStorage.removeItem('threadId');
    $('#threadTag').textContent = '会话 —（下一问起新会话）';
    chatReset();
    $('#chatLog').innerHTML =
      '<div class="hint">已开新会话。<br>旧会话没丢，点左上方「历史会话」随时翻回来。</div>';
    logClear();
    resetPipeline();
    $('#planBox').innerHTML = '<div class="log-empty">执行后显示联合决心方案</div>';
    $('#staffBox').innerHTML = '<div class="log-empty">执行后显示各军种方案</div>';
    $('#verdict').className = 'verdict hidden';
  });

  document.querySelectorAll('.tab').forEach(t => {
    t.addEventListener('click', () => activateTab(t.dataset.tab));
  });
}

/* ── 启动 ──────────────────────────────────────── */
initPipeline();
bind();
loadHealth();
/* 老链接 /#force（兵力池曾是控制台的一个页签）现在已升级为独立页面，
   这里兜底跳转，避免旧书签或旧文档里的地址打开是空页面。 */
if ((location.hash || '').toLowerCase() === '#force') location.replace('/force');

(async function boot() {
  let n = 0;
  if (S.threadId) {
    // 有会话就还原：对话 + 右栏最后那版方案一起回来（服务端记忆在 db 里）
    $('#threadTag').textContent = '会话 ' + S.threadId;
    n = await openSession(S.threadId, true);
  }
  if (!n) {
    /* 首次打开（或换过浏览器）时 localStorage 里没有会话号；或者记着的那个会话
       已经空了。以前这里只显示"记忆 0 轮" + 空白页，看起来像"记忆丢了"。
       记忆其实一直在服务端，所以直接接上最近一次有内容的会话。 */
    setMemTag(0);
    const list = await loadSessions();
    // 挑最近一个「真有轮次」的会话，别接上一个空的
    const recent = list.find(s => (s.turns || 0) > 0) || list[0];
    if (recent) {
      n = await openSession(recent.thread_id, true);
      if (n) {
        pushMeta(`已自动接上最近一次会话（${recent.date} ${recent.last}）。`
          + '想从零开始点上方「新会话」'
          + (list.length > 1 ? `；另有 ${list.length - 1} 个历史会话可翻。` : '。'));
      }
    }
    if (!n && !list.length) {
      pushMeta('还没有历史会话。在下面输入一句任务就能开始，系统会自动记住。');
    }
  }
})();
/* 版本号：由服务端注入 window.__BUILD__（web 目录最新文件的修改时间）。
   它只有一个用途 —— 改完代码刷新页面，你（和我）能立刻确认浏览器跑的是新版。
   数字不变 = 还在用缓存，Ctrl+F5。 */
(function showBuild() {
  const t = $('#buildTag');
  if (!t) return;
  const b = window.__BUILD__ || '0';
  const d = b !== '0' ? new Date(Number(b) * 1000) : null;
  const p = (n) => String(n).padStart(2, '0');
  t.textContent = d ? `版本 ${p(d.getMonth() + 1)}${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
                    : '版本 ?';
})();

setInterval(() => { $('#clock').textContent = hhmmss(); }, 1000);
