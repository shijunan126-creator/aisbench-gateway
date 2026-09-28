/* AISBench 网关前端
 *
 * 图表遵循 dataviz 规范：
 *  - 绝不使用双轴：吞吐（req/s、token/s）与时延（ms）量纲不同，拆成两张图
 *  - 每个图都配「表格」视图（浅色模式下部分分类色对比度 <3:1，规范要求 relief）
 *  - 颜色跟随「运行记录」这个实体：槽位在首次出现时分配并固定，勾选变化不会重新着色
 *  - ≥2 系列必定有图例；只在端点做选择性直标，不给每个点标数字
 */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const state = {
  config: null,
  datasets: [],
  jobs: [],
  results: [],
  mode: 'perf',
  selected: new Set(),
  detailJob: null,
  logTimer: null,
  colorSlots: new Map(),   // job_id -> slot index，全局稳定
};

const SERIES = ['--series-1','--series-2','--series-3','--series-4',
                '--series-5','--series-6','--series-7','--series-8'];

function cssvar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/* 颜色槽位按**勾选**分配，不是按"渲染过"分配。

   以前是 `state.colorSlots.size % 8`：列表里每渲染一条就占一个槽位，
   而对比页会把全部运行都渲染出来（实测 74 条），于是槽位每 8 个一轮回 ——
   第 1 条和第 9 条必然同色，而这两条**可以同时被勾选**（守门只管勾选数量，
   管不了槽位冲突），结果两条运行在图上完全同色、不可区分。
   这正好打在"多结果对比"这个主用法上。

   现在：勾选时占用一个当前空闲的槽位，取消勾选就释放。
   同一条运行在选中期间颜色不变（规范要求的"颜色跟随实体"仍然成立），
   而**同时展示的**几条永远不会撞色。 */
function colorFor(jobId) {
  if (state.colorSlots.has(jobId)) {
    return cssvar(SERIES[state.colorSlots.get(jobId)]);
  }
  const used = new Set(state.colorSlots.values());
  let slot = 0;
  while (used.has(slot) && slot < SERIES.length) slot++;
  if (slot >= SERIES.length) slot = state.colorSlots.size % SERIES.length;  // 兜底，不该发生
  state.colorSlots.set(jobId, slot);
  return cssvar(SERIES[slot]);
}

/* 该运行当前有没有占用颜色槽位（没勾选的显示成中性灰块，不占色） */
function colorOf(jobId) {
  return state.colorSlots.has(jobId) ? colorFor(jobId) : 'var(--border)';
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`;
    try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (e) {}
    throw new Error(msg);
  }
  return r.headers.get('content-type')?.includes('json') ? r.json() : r.text();
}

let toastTimer = null;
function toast(msg, isError) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast show' + (isError ? ' error' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = 'toast'; }, isError ? 7000 : 3200);
}

const fmt = (v, d = 2) => (v === null || v === undefined || isNaN(v)) ? '—'
  : Number(v).toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d });
const fmtInt = (v) => (v === null || v === undefined) ? '—' : Number(v).toLocaleString('zh-CN');

function ts(sec) {
  if (!sec) return '—';
  const d = new Date(sec * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getMonth()+1}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/* ------------------------------------------------------------ 标签页 */
$$('nav button').forEach(b => b.addEventListener('click', () => {
  $$('nav button').forEach(x => x.classList.toggle('active', x === b));
  $$('.tab').forEach(t => t.classList.toggle('active', t.id === 'tab-' + b.dataset.tab));
  if (b.dataset.tab === 'compare') loadResults();
  if (b.dataset.tab === 'data') loadDatasets();
}));

$('#theme-toggle').addEventListener('click', () => {
  const cur = document.documentElement.getAttribute('data-theme');
  const next = cur === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('gw-theme', next);
  redrawCharts();
});
if (localStorage.getItem('gw-theme')) {
  document.documentElement.setAttribute('data-theme', localStorage.getItem('gw-theme'));
}

/* ------------------------------------------------------------ 初始化 */
async function init() {
  try {
    state.config = await api('/api/config');
  } catch (e) {
    toast('加载配置失败: ' + e.message, true);
    return;
  }
  renderContainerPill();
  const sel = $('#api_type');
  sel.innerHTML = state.config.api_types
    .map(a => `<option value="${a.id}">${a.label}</option>`).join('');
  sel.value = 'chat_stream';

  renderTokenizerOptions();

  const d = state.config.defaults || {};
  if (d.max_out_len) $('#max_out_len').value = d.max_out_len;
  if (d.concurrency) $('#concurrency').value = d.concurrency;
  if (d.num_prompts) $('#num_prompts').value = d.num_prompts;
  if (d.num_warmups !== undefined) $('#num_warmups').value = d.num_warmups;

  bindForm();
  await loadDatasets();
  await loadJobs();
}

/* tokenizer 路径：页面上只填/选**模型目录名**（如 Qwen3.5-35B-A3B），
   提交时后端自动解析成容器内完整路径。
   候选来自 /api/config 的 tokenizer_candidates：config.ini 里配的模型目录
   （或其父目录）+ 数据目录 models/ 下扫到的。 */
function renderTokenizerOptions() {
  const cands = state.config?.tokenizer_candidates || [];
  const mount = state.config?.models_mount || '/work/models';
  $('#tok-options').innerHTML = cands.map(c =>
    `<option value="${escapeHtml(c.name)}"></option>`).join('');
  const bases = state.config?.tokenizer_dirs || [];
  $('#tok-hint-rest').textContent =
    `，用于本地 token 计数。填模型目录名即可（下拉可选），也可填完整容器内路径。` +
    (bases.length
      ? `已扫描 ${bases.join('、')}（里面的模型文件夹会自动列出，目前 ${cands.length} 个）。`
      : `也可以把 tokenizer 放进数据目录的 ${mount}/ 下。`);
  // 只有一个候选时直接填上，省得手输
  if (cands.length === 1 && !$('#tokenizer_path').value) {
    $('#tokenizer_path').value = cands[0].name;
  }
}

/* 右上角状态：网关跑在容器里，没法重建自己，所以这里只如实反映自检结果。
   出问题时提示用户去宿主上执行 ./start.sh，而不是给一个点了没用的按钮。 */
function renderContainerPill(check) {
  const c = check || state.config?.selfcheck || {};
  const pill = $('#container-pill');
  const name = state.config?.container_name || '网关';

  if (c.ok) {
    pill.textContent = `服务正常 · ais_bench 就绪`;
    pill.className = 'pill ok';
    pill.title = [
      `数据目录：${c.data_dir}`,
      `磁盘剩余：${c.disk_free_gb ?? '?'} GB`,
      `Python：${c.python}`,
      c.ais_bench_version ? `ais_bench：${c.ais_bench_version}` : '',
    ].filter(Boolean).join('\n');
    pill.style.cursor = 'help';
    pill.onclick = null;
    return;
  }

  const problems = c.problems || ['环境异常'];
  pill.textContent = `服务异常 · ${problems.length} 项`;
  pill.className = 'pill bad';
  pill.title = problems.join('\n') + '\n\n在宿主上执行 ./start.sh 重建容器后重试。';
  pill.style.cursor = 'pointer';
  pill.onclick = async () => {
    try {
      const r = await api('/api/selfcheck', { method: 'POST' });
      renderContainerPill(r);
      toast(r.ok ? '自检通过' : '仍有问题：' + r.problems.join('；'), !r.ok);
    } catch (e) { toast('自检失败: ' + e.message, true); }
  };
}

/* ------------------------------------------------------------ 表单 */
function bindForm() {
  $$('#mode-seg button').forEach(b => b.addEventListener('click', () => {
    state.mode = b.dataset.mode;
    $$('#mode-seg button').forEach(x => x.classList.toggle('active', x === b));
    syncForm();
  }));

  $('#dataset').addEventListener('change', () => { syncVariants(); syncForm(); });
  $('#gen_len_mode').addEventListener('change', syncForm);
  $('#submit-btn').addEventListener('click', submitJob);
  $('#refresh-jobs').addEventListener('click', loadJobs);
  $('#detail-back').addEventListener('click', () => {
    state.detailJob = null;
    stopLog();
    $('#detail-card').style.display = 'none';
    $('#form-card').style.display = '';
    $('#list-card').style.display = '';
  });
  $('#refresh-datasets').addEventListener('click', loadDatasets);
  $('#refresh-results').addEventListener('click', loadResults);
  $('#compare-kind').addEventListener('change', loadResults);
}

function syncVariants() {
  const ds = state.datasets.find(d => d.family === $('#dataset').value);
  $('#variant').innerHTML = ds
    ? ds.variants.map(v => `<option value="${v.id}">${v.label}</option>`).join('')
    : '';
  if (ds) $('#variant').value = ds.default_variant;
}

function syncForm() {
  const isPerf = state.mode === 'perf';
  const ds = state.datasets.find(d => d.family === $('#dataset').value);
  const family = ds?.family;
  const isGen = family === 'prefix_gen';

  $('#perf-params').style.display = isPerf ? '' : 'none';
  $('#acc-num-field').style.display = isPerf ? 'none' : '';
  $('#syn-adv').style.display = family === 'synthetic' ? '' : 'none';
  $('#gen-adv').style.display = (isGen && isPerf) ? '' : 'none';
  $('#sweep-field').style.display = isPerf ? '' : 'none';

  const needsTok = family === 'synthetic' || family === 'sharegpt' || isGen;
  $('#tok-field').style.display = needsTok ? '' : 'none';

  // 随机数据集的请求自带 max_out_len，aisbench 会让它覆盖模型配置里的值
  // （日志原话：Dataset-specified max_out_len has highest priority）。
  // 不提示的话，用户填了「最大输出长度」却没生效会一头雾水。
  //
  // 合成数据集相反：它的每条数据只有正文，没有 max_out_len 字段，
  // 所以模型配置里的值生效，这一项照常可用。
  const maxoutHint = $('#maxout-hint');
  if (family === 'synthetic') {
    maxoutHint.textContent = '随机数据集不使用此项，实际输出长度由下方「输出长度」决定';
    $('#max_out_len').disabled = true;
  } else {
    maxoutHint.textContent = isGen ? '合成数据集使用此项作为每条请求的输出长度' : '';
    $('#max_out_len').disabled = false;
  }

  // 请求数由「数据条数」决定（数据集里就那么多行），两个数字并排摆着只会打架
  $('#num_prompts').disabled = isGen;
  $('#num-prompt-hint').textContent = isGen ? '合成数据集不用此项，请求数 = 下方「数据条数」' : '';

  syncGenLenMode();

  $('#tok-hint-label').textContent = isGen ? '合成数据集必填' : '随机数据集/sharegpt 必填';

  $('#mode-hint').textContent = isPerf
    ? '压测吞吐与时延，产物含逐请求分位指标'
    : '跑数据集并评分；随机数据集、sharegpt 与合成数据集不支持精度模式';

  if (ds) {
    let h = ds.note || '';
    if (!ds.ready && !ds.builtin) h = (h ? h + ' · ' : '') + '⚠ 数据未下载，请到「数据集」页下载';
    if (ds.perf_only && !isPerf) h = (h ? h + ' · ' : '') + '⚠ 该数据集仅支持性能模式';
    $('#dataset-hint').textContent = h;
  }
  $('#submit-btn').disabled = !!ds && !ds.ready && !ds.builtin;
}

/* 输入长度的三种模式各用一组输入框，只显示当前模式那一组。
   三组同时摆出来，用户很容易只填一组、另两组留着默认值就提交。 */
function syncGenLenMode() {
  const mode = $('#gen_len_mode').value;
  $('#gen-fixed-field').style.display = mode === 'fixed' ? '' : 'none';
  $$('.gen-uni').forEach(e => e.style.display = mode === 'uniform' ? '' : 'none');
  $$('.gen-gauss').forEach(e => e.style.display = mode === 'gauss' ? '' : 'none');
}

function collectParams() {
  const num = (id) => { const v = $(id).value; return v === '' ? null : Number(v); };
  const p = {
    base_url: $('#base_url').value.trim(),
    api_type: $('#api_type').value,
    model: $('#model').value.trim(),
    api_key: $('#api_key').value,
    dataset: $('#dataset').value,
    variant: $('#variant').value,
    temperature: num('#temperature'),
    ignore_eos: $('#ignore_eos').checked,
    max_out_len: num('#max_out_len'),
    num_warmups: num('#num_warmups'),
  };
  const tok = $('#tokenizer_path').value;
  if (tok) p.tokenizer_path = tok;

  // 并发数两种模式都生效：精度模式下它决定跑得多快，性能模式下它就是压测并发
  p.concurrency = num('#concurrency');

  if (state.mode === 'accuracy') {
    p.num_prompts = num('#acc_num_prompts');
  }

  if (state.mode === 'perf') {
    p.num_prompts = num('#num_prompts');
    p.request_rate = num('#request_rate');
    const list = $('#concurrency_list').value.trim();
    if (list) p.concurrency_list = list;
    if (p.dataset === 'synthetic') {
      p.input_len_min = num('#input_len_min');
      p.input_len_max = num('#input_len_max');
      p.output_len_min = num('#output_len_min');
      p.output_len_max = num('#output_len_max');
      p.input_dist = $('#input_dist').value;
      p.output_dist = $('#output_dist').value;
    }
    if (p.dataset === 'prefix_gen') {
      p.gen_len_mode = $('#gen_len_mode').value;
      p.gen_input_len = num('#gen_input_len');
      p.gen_len_min = num('#gen_len_min');
      p.gen_len_max = num('#gen_len_max');
      p.gen_len_mean = num('#gen_len_mean');
      p.gen_len_std = num('#gen_len_std');
      p.gen_num = num('#gen_num');
      // 比例保持字符串：后端要同时接受 "50%" 和 "0.5"
      p.gen_prefix_ratio = $('#gen_prefix_ratio').value.trim();
      p.gen_prefix_num = num('#gen_prefix_num');
      p.gen_dp = num('#gen_dp');
      p.gen_seed = num('#gen_seed');
      p.gen_warmup = $('#gen_warmup').checked;
      p.collect_metrics = $('#gen_metrics').checked;
    }
  }
  return p;
}

async function submitJob() {
  const btn = $('#submit-btn');
  btn.disabled = true;
  $('#submit-hint').textContent = '提交中…';
  try {
    const res = await api('/api/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        mode: state.mode,
        params: collectParams(),
        // 备注是任务级的信息，不属于压测参数，所以放在 params 外面
        note: $('#job_note').value.trim(),
      }),
    });
    const n = (res.job_ids || []).length;
    toast(res.parent_id ? `已提交并发扫描，共 ${n} 个子任务` : '已提交，任务排队中');
    $('#job_note').value = '';       // 备注只对刚提交的这次有意义，别带到下一次
    $('#submit-hint').textContent = '';
    await loadJobs();
  } catch (e) {
    toast('提交失败: ' + e.message, true);
    $('#submit-hint').textContent = '';
  } finally {
    btn.disabled = false;
    syncForm();
  }
}

/* 备注编辑。用 prompt 而不是自建弹窗：这是个顺手的标注功能，
   自建弹窗要多写一套焦点/键盘/点击外部关闭的处理，不划算。
   真嫌 prompt 丑的话再换成内联编辑。 */
async function saveNote(jid, note) {
  try {
    await api(`/api/jobs/${jid}/note`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ note: (note || '').trim() }),
    });
    toast((note || '').trim() ? '备注已保存' : '备注已清除');
    await loadJobs();
    await refreshDetail();
  } catch (e) {
    toast('保存失败: ' + e.message, true);
  }
}

async function editNote(jid) {
  const j = state.jobs.find(x => x.id === jid);
  const cur = (j?.note || '');
  const next = window.prompt(
    '备注（留空则清除）\n写清楚这次测的是什么，多组参数对比时一眼能认出来：', cur);
  if (next === null) return;                    // 用户取消
  try {
    await api(`/api/jobs/${jid}/note`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ note: next }),
    });
    toast(next.trim() ? '备注已保存' : '备注已清除');
    await loadJobs();
    if (state.detailJob === jid) refreshDetail();
  } catch (e) {
    toast('保存失败: ' + e.message, true);
  }
}

/* ------------------------------------------------------------ 任务列表 */
async function loadJobs() {
  try {
    state.jobs = await api('/api/jobs');
  } catch (e) { toast('加载任务失败: ' + e.message, true); return; }
  if (!state.detailJob) renderJobs();
}

/* 有任务在排队/运行时自动刷新列表（压测一跑几十分钟，状态与
   "2/2 档完成"不刷新的话用户只能一直手点"刷新"；全部到终态后自动停）。 */
setInterval(() => {
  if (document.hidden) return;
  const active = (j) => ['queued', 'running'].includes(j.status);
  if (state.jobs.some(j => active(j) || (j.children || []).some(active))) loadJobs();
}, 4000);

function statusHtml(s) {
  const names = { queued: '排队中', running: '运行中', succeeded: '成功',
                  failed: '失败', cancelled: '已取消' };
  return `<span class="status ${s}">${names[s] || s}</span>`;
}

function renderJobs() {
  const el = $('#job-list');
  if (!state.jobs.length) {
    el.innerHTML = '<div class="empty">还没有任务。填好上面的表单，点「提交测试」。</div>';
    return;
  }
  const rows = state.jobs.map(j => {
    const p = j.params || {};
    const ds = state.datasets.find(d => d.family === p.dataset);
    const dur = j.finished_at && j.started_at
      ? ((j.finished_at - j.started_at) / 60).toFixed(1) + ' 分' : '—';
    const isSweep = j.kind === 'sweep';
    const kids = isSweep ? (j.children || []) : [];
    const done = kids.filter(k => k.status === 'succeeded').length;
    const desc = isSweep
      ? `并发 ${p.concurrency_list} · ${done}/${kids.length} 档完成`
      : `${ds?.label || p.dataset} · 并发 ${p.concurrency ?? '—'} · ${p.num_prompts ?? '—'} 请求`;
    // 备注优先显示：它是用户自己写的"这条是什么"，比自动标签好认。
    // 写了备注时把自动标签降级成小字，两组信息都还在。
    const note = (j.note || '').trim();
    const auto = j.label || j.id;
    const name = note
      ? `<div>${escapeHtml(note)}</div><div class="sub">${escapeHtml(auto)}</div>`
      : `<div>${escapeHtml(auto)}</div>`;
    return `<tr data-id="${j.id}">
      <td>${statusHtml(j.status)}</td>
      <td>${name}<div class="sub mono">${j.id}${isSweep ? ' · 扫描' : ''}</div></td>
      <td>${desc}</td>
      <td>${j.mode === 'accuracy' ? '精度' : '性能'}</td>
      <td>${ts(j.created_at)}</td>
      <td class="num">${dur}</td>
      <td style="text-align:right;white-space:nowrap">
        <button class="ghost sm" data-note="${j.id}" title="编辑备注">备注</button>
        <button class="ghost sm" data-open="${j.id}">详情</button>
      </td>
    </tr>`;
  }).join('');

  el.innerHTML = `<table>
    <thead><tr>
      <th>状态</th><th>任务</th><th>配置</th><th>模式</th>
      <th>创建时间</th><th class="num">耗时</th><th></th>
    </tr></thead><tbody>${rows}</tbody></table>`;

  $$('[data-open]', el).forEach(b => b.addEventListener('click', () => openDetail(b.dataset.open)));
  $$('[data-note]', el).forEach(b => b.addEventListener('click', () => editNote(b.dataset.note)));
}

/* ------------------------------------------------------------ 任务详情 */
async function openDetail(id) {
  state.detailJob = id;
  $('#form-card').style.display = 'none';
  $('#list-card').style.display = 'none';
  $('#detail-card').style.display = '';
  $('#detail-body').innerHTML = '<div class="empty">加载中…</div>';
  await refreshDetail();
  startLog(id);
}

/* 合成数据集的实测情况。
   实测长度必然和设定值有出入（tokenize↔decode 有损，加上 chat 模板开销），
   不把实测值摆出来，用户很容易以为是参数没生效。 */
function genActualLenHtml(p) {
  const s = p._gen_stats;
  if (!s || s.actual_len_avg === undefined) return '';
  const dev = s.length_deviation;
  const devTxt = (dev === undefined || dev === null)
    ? '' : `，与目标相差 ${(dev * 100).toFixed(1)}%`;
  let extra = '';
  if (s.prefix_pool) {
    extra = ` · 前缀 ${s.prefix_len ?? s.prefix_len_max} token × ${s.prefix_pool} 种`
          + ` · 中间唯一 token ${s.unique_tokens}`;
  }
  // 回读是抽样的（长数据集全量重切太慢），如实标出来
  const sampled = s.measured_rows && s.rows && s.measured_rows < s.rows
    ? `，抽样 ${s.measured_rows}/${s.rows} 条` : '';
  return `<tr><th>实测长度</th><td>${s.actual_len_min} ~ ${s.actual_len_max}`
       + ` <span class="sub">均值 ${fmt(s.actual_len_avg, 1)}${devTxt}${extra}${sampled}</span></td></tr>`;
}

/* prefix cache 命中率（本次测试期间，按 /metrics 增量算出来的）。
   分端点、分 engine 展开：合在一起看不出「某个 DP 域根本没吃到前缀」，
   而那正是预热没做对时的典型症状。 */
function prefixHitHtml(p) {
  const h = p._prefix_hit;
  if (!h) return '';
  const pct = (r) => (r === null || r === undefined) ? '—' : (r * 100).toFixed(2) + '%';
  const pair = (d) => (!d || !d.queries) ? '—'
    : `${fmtInt(d.hits)} / ${fmtInt(d.queries)}`;

  const t = h.total || {};
  const total = (t.hbm) || {};
  const ext = (t.external) || {};

  let rows = '';
  for (const pod of h.pods || []) {
    for (const e of pod.engines) {
      rows += `<tr>
        <td class="mono">${escapeHtml(pod.pod)} · engine${e.engine}</td>
        <td class="num">${pct(e.hbm.rate)}</td>
        <td class="num sub">${pair(e.hbm)}</td>
        <td class="num">${pct(e.external.rate)}</td>
        <td class="num sub">${pair(e.external)}</td></tr>`;
    }
    if ((pod.engines || []).length > 1) {
      rows += `<tr><td class="sub">${escapeHtml(pod.pod)} 小计</td>
        <td class="num sub">${pct(pod.total.hbm.rate)}</td>
        <td class="num sub">${pair(pod.total.hbm)}</td>
        <td class="num sub">${pct(pod.total.external.rate)}</td>
        <td class="num sub">${pair(pod.total.external)}</td></tr>`;
    }
  }

  let notes = '';
  for (const s of h.skipped || []) {
    notes += `<p class="sub" style="margin:6px 0 0">跳过 ${escapeHtml(s.pod)}：${escapeHtml(s.reason)}</p>`;
  }
  if (h.note) notes += `<p class="sub" style="margin:6px 0 0">${escapeHtml(h.note)}</p>`;
  if (!h.ok && !notes) {
    notes = '<p class="sub" style="margin:6px 0 0">没有取到有效的 prefix cache 指标。'
          + '请确认被测服务是 vLLM 且暴露了 /metrics。</p>';
  }

  return `<div class="card" style="margin-top:14px">
    <h2>Prefix cache 命中率</h2>
    <p class="sub">本次测试期间的实际命中情况，由压测前后两次读取 <code>/metrics</code> 的增量算出。</p>
    <div class="row" style="align-items:baseline;gap:16px;margin:10px 0 4px">
      <div>
        <div class="sub" style="font-size:11.5px;margin-bottom:2px">全部端点合计</div>
        <span style="font-size:30px;font-weight:600">${pct(total.rate)}</span>
        <span class="sub" style="margin-left:8px">命中 ${fmtInt(total.hits)} / 查询 ${fmtInt(total.queries)} token</span>
      </div>
      ${ext.queries ? `<div class="sub">外部缓存命中率 ${pct(ext.rate)}
        （命中 ${fmtInt(ext.hits)} / 查询 ${fmtInt(ext.queries)}）</div>` : ''}
    </div>
    ${rows ? `<table style="margin-top:8px"><thead><tr>
      <th>端点 / DP 域</th><th class="num">命中率</th><th class="num">命中/查询</th>
      <th class="num">外部命中率</th><th class="num">外部 命中/查询</th>
    </tr></thead><tbody>${rows}</tbody></table>` : ''}
    ${notes}
  </div>`;
}

async function refreshDetail() {
  const id = state.detailJob;
  if (!id) return;
  let job;
  try { job = await api('/api/jobs/' + id); }
  catch (e) { $('#detail-body').innerHTML = `<div class="empty">加载失败: ${e.message}</div>`; return; }

  const p = job.params || {};
  const ds = state.datasets.find(d => d.family === p.dataset);
  $('#detail-cancel').style.display = ['queued','running'].includes(job.status) ? '' : 'none';
  $('#detail-cancel').onclick = async () => {
    await api(`/api/jobs/${id}/cancel`, { method: 'POST' });
    toast('已请求取消'); refreshDetail();
  };
  $('#detail-delete').onclick = async () => {
    if (!confirm('删除该任务记录？（磁盘产物保留）')) return;
    await api('/api/jobs/' + id, { method: 'DELETE' });
    $('#detail-back').click(); loadJobs();
  };
  $('#detail-reparse').onclick = async () => {
    try {
      const r = await api(`/api/jobs/${id}/reparse`, { method: 'POST' });
      toast(`已重新解析 ${r.reparsed} 条结果`);
      refreshDetail();
    } catch (e) { toast('重新解析失败: ' + e.message, true); }
  };

  const head = `
    <div class="row" style="margin-bottom:4px">
      <h2 style="margin:0">${escapeHtml(job.label || id)}</h2>
      ${statusHtml(job.status)}
      <span class="pill">${job.mode === 'accuracy' ? '精度' : '性能'}</span>
      ${job.kind === 'sweep' ? '<span class="pill">并发扫描</span>' : ''}
    </div>
    <p class="sub mono">${id} · 创建于 ${ts(job.created_at)}${job.finished_at ? ' · 耗时 ' + ((job.finished_at - job.started_at)/60).toFixed(1) + ' 分' : ''}</p>
    <div class="row" style="gap:8px;margin:10px 0 4px;max-width:760px">
      <input id="detail-note" maxlength="200" placeholder="备注：这次测的是什么？（会盖过上面的自动标题，并带到结果对比里）"
             style="flex:1">
      <button class="ghost sm" id="detail-note-save">保存备注</button>
    </div>
    <table style="margin-top:10px">
      <tbody>
        <tr><th style="width:130px">数据集</th><td>${escapeHtml(ds?.label || p.dataset || '—')}
            <span class="mono sub">${escapeHtml(p.variant || '')}</span></td></tr>
        <tr><th>接口</th><td class="mono">${escapeHtml(p.api_type || '—')} → ${escapeHtml(p.base_url || '—')}</td></tr>
        <tr><th>模型</th><td class="mono">${escapeHtml(p.model || '(自动探测)')}</td></tr>
        ${job.mode === 'perf' ? `<tr><th>并发 / 请求数</th><td>${
            p.concurrency ?? '—'} / ${
            // 合成数据集的请求数来自数据集行数，不走 num_prompts，
            // 照着 num_prompts 显示会是「—」,容易被当成参数丢了
            p.dataset === 'prefix_gen' ? (p.gen_num ?? '—') : (p.num_prompts ?? '—')}</td></tr>` : ''}
        ${p._gen_summary ? `<tr><th>合成数据集</th><td>${escapeHtml(p._gen_summary)}${
            p._gen_cached ? ' <span class="sub">（复用已有产物）</span>' : ''}</td></tr>` : ''}
        ${genActualLenHtml(p)}
        ${p.tokenizer_path ? `<tr><th>Tokenizer</th><td class="mono">${escapeHtml(p.tokenizer_path)}</td></tr>` : ''}
      </tbody>
    </table>`;

  let body = head;

  if (job.error) {
    body += `<div class="card" style="margin-top:14px;border-left:3px solid var(--status-critical)">
      <h2>错误</h2><p class="sub">${escapeHtml(job.error)}</p></div>`;
  }

  if (job.kind === 'sweep') {
    body += `<div class="card" style="margin-top:14px"><h2>子任务</h2>
      <table><thead><tr><th>并发</th><th>状态</th><th class="num">请求吞吐</th>
      <th class="num">输出吞吐</th><th class="num">TTFT 均值</th><th class="num">TPOT 均值</th><th></th></tr></thead><tbody>`;
    for (const k of (job.children || [])) {
      const rs = k.status === 'succeeded'
        ? (await api('/api/jobs/' + k.id)).results : [];
      const m = rs[0]?.metrics || {};
      body += `<tr><td class="num">${(k.params||{}).concurrency ?? '—'}</td>
        <td>${statusHtml(k.status)}</td>
        <td class="num">${fmt(m.request_throughput)}</td>
        <td class="num">${fmt(m.output_token_throughput)}</td>
        <td class="num">${fmt(m.ttft_average, 1)}</td>
        <td class="num">${fmt(m.tpot_average, 1)}</td>
        <td style="text-align:right"><button class="ghost sm" data-open="${k.id}">详情</button></td></tr>`;
    }
    body += '</tbody></table></div>';
  }

  const res = job.results || [];
  if (res.length) {
    body += job.mode === 'accuracy' ? accuracyDetailHtml(res) : perfDetailHtml(res);
  }

  body += prefixHitHtml(p);

  if (job.kind === 'sweep') {
    // 扫描父任务本身不执行，没有自己的日志；子任务的日志在各自详情里
    body += `<div class="card" style="margin-top:14px">
      <h3 style="font-size:13.5px;margin:0 0 4px">运行日志</h3>
      <p class="sub">并发扫描的父任务只做汇总，不产生自己的日志。点上方子任务的「详情」可以看各自的运行日志。</p>
    </div>`;
  } else {
    // 注意：不要在这里放「产物目录」链接 —— artifacts 接口只提供文件，
    // 指向目录会 404。单个产物的链接在同数据集的性能结果卡片里。
    //
    // 默认折叠：用户要看的是上面的结果表格和命中率，日志是排查时的兜底。
    // 任务在跑时保持展开（生成数据集的实时进度在这里）；跑完自动收起。
    const running = ['queued', 'running'].includes(job.status);
    body += `<details class="card log-fold" id="log-fold" ${running ? 'open' : ''}>
      <summary>运行日志${running ? '（实时）' : '<span class="sub">排查时展开</span>'}</summary>
      ${job.run_dir ? `<p class="sub mono" style="margin:8px 0">产物: ${escapeHtml(job.run_dir)}</p>` : ''}
      <pre class="log" id="log-pre">连接中…</pre>
    </details>`;
  }

  $('#detail-body').innerHTML = body;
  $$('[data-open]', $('#detail-body')).forEach(b =>
    b.addEventListener('click', () => openDetail(b.dataset.open)));

  // 备注用 JS 赋值而不是拼进 HTML 属性：属性里要做转义，
  // 少转一个引号就会被备注内容截断，这类坑不值得踩
  const noteInput = $('#detail-note');
  if (noteInput) {
    noteInput.value = job.note || '';
    const save = $('#detail-note-save');
    save.addEventListener('click', () => saveNote(id, noteInput.value));
    noteInput.addEventListener('keydown', ev => {
      if (ev.key === 'Enter') saveNote(id, noteInput.value);
    });
  }

  if (job.kind === 'sweep') drawSweepCharts('#detail-body', await api('/api/jobs/' + id));
}

function relArtifact(runDir) {
  const marker = '/outputs/';
  const i = runDir.indexOf(marker);
  return i >= 0 ? runDir.slice(i + marker.length) : runDir;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
}

/* 性能结果：端到端指标表 + 分位指标表，两者量纲不同，分开呈现 */
function perfDetailHtml(res) {
  const m = res[0].metrics || {};
  const common = [
    ['最大并发', m.max_concurrency, 0], ['实际平均并发', m.concurrency, 2],
    ['总请求数', m.total_requests, 0], ['成功请求', m.success_requests, 0],
    ['失败请求', m.failed_requests, 0], ['压测时长 (ms)', m.duration_ms, 1],
    ['请求吞吐 (req/s)', m.request_throughput, 3],
    ['输入 token 吞吐 (token/s)', m.input_token_throughput, 1],
    ['输出 token 吞吐 (token/s)', m.output_token_throughput, 1],
    ['总 token 吞吐 (token/s)', m.total_token_throughput, 1],
  ];
  const perc = [
    ['E2EL 端到端时延 (ms)', 'e2el'], ['TTFT 首 token 时延 (ms)', 'ttft'],
    ['TPOT 每 token 时延 (ms)', 'tpot'], ['ITL token 间间隔 (ms)', 'itl'],
    ['输入 token 数', 'input_tokens'], ['输出 token 数', 'output_tokens'],
    ['单请求输出速度 (token/s)', 'output_token_speed'],
  ];
  const statCols = ['average','min','max','median','p75','p90','p99'];

  let html = `<div class="card" style="margin-top:14px">
    <h2>端到端性能</h2>
    <table><tbody>${common.map(([k,v,d]) =>
      `<tr><th>${k}</th><td class="num">${fmt(v, d)}</td></tr>`).join('')}</tbody></table>
  </div>`;

  html += `<div class="card" style="margin-top:14px">
    <h2>单请求分位指标</h2>
    <p class="sub">N 为参与统计的成功请求数；TPOT/ITL 仅统计有 decode 阶段的请求。</p>
    <table><thead><tr><th>指标</th>
      ${statCols.map(c => `<th class="num">${c === 'average' ? '均值' : c.toUpperCase()}</th>`).join('')}
      <th class="num">N</th></tr></thead><tbody>`;
  for (const [label, key] of perc) {
    if (m[`${key}_average`] === undefined) continue;
    html += `<tr><td>${label}</td>${statCols.map(c =>
      `<td class="num">${fmt(m[`${key}_${c}`], key.includes('tokens') && !key.includes('speed') ? 1 : 2)}</td>`
    ).join('')}<td class="num">${fmtInt(m[`${key}_n`])}</td></tr>`;
  }
  html += '</tbody></table></div>';

  const art = res[0].artifacts || {};
  const links = [];
  if (art.plot) links.push(`<a class="pill" style="text-decoration:none" href="/api/artifacts/${escapeHtml(art.plot)}" target="_blank">请求时间线 / 并发图</a>`);
  if (art.rps_plot) links.push(`<a class="pill" style="text-decoration:none" href="/api/artifacts/${escapeHtml(art.rps_plot)}" target="_blank">RPS 分布图</a>`);
  if (art.details) links.push(`<a class="pill" style="text-decoration:none" href="/api/artifacts/${escapeHtml(art.details)}" target="_blank">逐请求明细 jsonl</a>`);
  if (links.length) html += `<div class="card" style="margin-top:14px"><h2>aisbench 原生可视化</h2>
    <p class="sub">由 aisbench 自身生成，含逐请求耗时拆解与全程并发曲线。</p>
    <div class="row" style="margin-top:10px">${links.join('')}</div></div>`;

  return html;
}

function accuracyDetailHtml(res) {
  let html = `<div class="card" style="margin-top:14px"><h2>精度得分</h2>
    <table><thead><tr><th>数据集</th><th>指标</th><th class="num">得分</th></tr></thead><tbody>`;
  for (const r of res) {
    const m = r.metrics || {};
    const entries = Object.entries(m).filter(([k]) => k !== 'main_score');
    if (!entries.length) {
      html += `<tr><td>${escapeHtml(r.dataset)}</td><td colspan="2" class="sub">未解析到得分指标</td></tr>`;
      continue;
    }
    entries.forEach(([k, v], i) => {
      html += `<tr>${i === 0 ? `<td rowspan="${entries.length}">${escapeHtml(r.dataset)}</td>` : ''}
        <td>${k}</td><td class="num">${fmt(v)}</td></tr>`;
    });
  }
  return html + '</tbody></table></div>';
}

/* ------------------------------------------------------------ 日志流 */
function stopLog() {
  if (state.logTimer) { clearInterval(state.logTimer); state.logTimer = null; }
}

/* 日志缓冲区独立于 DOM。
   原因：任务处于终态时 refreshDetail() 会重建整块 #detail-body，
   直接把日志区打回初始状态；而轮询此时已经停止，界面就永远停在"连接中…"。
   把已收到的内容存在这里，重建 DOM 后重新画上去即可。

   缓冲区有上限：长压测的 run.log 能有几百 MB，全部攒在内存里再把整段
   塞进 <pre> 会把浏览器标签页卡死。只保留最近一段，截断处在开头注明。 */
const LOG_KEEP_BYTES = 1_500_000;
const logState = { id: null, primed: false, offset: 0, buf: '', truncated: false, lastStatus: null };

function fmtBytes(n) {
  if (n >= 1e9) return (n / 1e9).toFixed(1) + ' GB';
  if (n >= 1e6) return (n / 1e6).toFixed(0) + ' MB';
  return Math.max(1, Math.round(n / 1e3)) + ' KB';
}

function paintLog() {
  const el = $('#log-pre');
  if (!el) return;
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
  const head = logState.truncated
    ? `…（日志很长，开头 ${fmtBytes(logState.dropped)} 已省略，只显示最新部分；完整内容见产物 run.log）\n` : '';
  el.textContent = head + (logState.buf || '（暂无输出）');
  if (atBottom) el.scrollTop = el.scrollHeight;
}

function startLog(id) {
  stopLog();
  if (logState.id !== id) {
    logState.id = id;
    logState.primed = false;
    logState.offset = 0;
    logState.buf = '';
    logState.truncated = false;
    logState.dropped = 0;
    logState.lastStatus = null;
  }
  paintLog();

  const tick = async () => {
    if (state.detailJob !== id) { stopLog(); return; }
    try {
      // 首次打开直接从**末尾**取最近一段（tail 参数）：用户关心的是最新输出，
      // 从头把几百 MB 的 run.log 逐块下载完没有意义。失败的原因网关已抽出来
      // 放在详情页的「错误」卡片里，不靠翻日志。
      const q = logState.primed
        ? `offset=${logState.offset}`
        : `offset=0&tail=${LOG_KEEP_BYTES}`;
      const r = await api(`/api/jobs/${id}/log?` + q);
      logState.primed = true;
      if (r.skipped) {
        logState.truncated = true;
        logState.dropped = r.skipped;
      }
      if (r.text) {
        logState.offset = r.offset;
        logState.buf += r.text;
        if (logState.buf.length > LOG_KEEP_BYTES) {
          const drop = logState.buf.length - LOG_KEEP_BYTES;
          // 按行截断，避免把半行留在开头
          const nl = logState.buf.indexOf('\n', drop);
          logState.buf = logState.buf.slice(nl >= 0 ? nl + 1 : drop);
          logState.truncated = true;
          logState.dropped = (logState.dropped || 0) + drop;
        }
        paintLog();
      }
      if (['succeeded', 'failed', 'cancelled'].includes(r.status)) {
        stopLog();
        // 只在状态**首次**变成终态时刷新详情（补上结果卡片）。
        // 刷新后必须重画日志，否则就会回到上面说的"连接中…"。
        if (logState.lastStatus !== r.status) {
          logState.lastStatus = r.status;
          await refreshDetail();
          paintLog();
        }
      }
    } catch (e) { stopLog(); }
  };
  tick();
  state.logTimer = setInterval(tick, 1500);
}

/* ------------------------------------------------------------ 数据集 */
async function loadDatasets() {
  try { state.datasets = await api('/api/datasets'); }
  catch (e) { toast('加载数据集失败: ' + e.message, true); return; }
  renderDatasets();
  const cur = $('#dataset').value;
  const sel = $('#dataset');
  sel.innerHTML = state.datasets.map(d =>
    `<option value="${d.family}">${d.label}${d.ready || d.builtin ? '' : '（未下载）'}</option>`).join('');
  if (cur && state.datasets.some(d => d.family === cur)) sel.value = cur;
  syncVariants();
  syncForm();
}

function renderDatasets() {
  const anyActive = state.datasets.some(d => ['downloading','extracting'].includes(d.download.status));
  const rows = state.datasets.map(d => {
    let action = '';
    if (d.builtin) {
      action = '<span class="sub">内置，无需下载</span>';
    } else if (['downloading','extracting'].includes(d.download.status)) {
      action = `<div style="min-width:150px">
        <div class="sub">${escapeHtml(d.download.message || '处理中')}</div>
        <div class="bar-track" style="margin-top:4px">
          <div class="bar-fill" style="width:${d.download.progress || 0}%"></div></div>
      </div>`;
    } else {
      const label = d.ready ? '重新下载' : '下载';
      action = `<button class="ghost sm" data-dl="${d.family}">${label}</button>`;
    }
    const state_ = d.builtin ? '<span class="pill ok">内置</span>'
      : d.ready ? '<span class="pill ok">已就绪</span>'
      : ['downloading','extracting'].includes(d.download.status) ? '<span class="pill">下载中</span>'
      : '<span class="pill bad">未下载</span>';
    const size = d.size_mb ? `~${d.size_mb >= 1 ? d.size_mb.toFixed(0) + ' MB' : '<1 MB'}` : '';
    return `<tr>
      <td><div>${escapeHtml(d.label)}</div>
          ${d.note ? `<div class="sub">${escapeHtml(d.note)}</div>` : ''}</td>
      <td class="mono">${escapeHtml(d.family)}</td>
      <td>${d.variants.length} 个变体</td>
      <td>${d.perf_only ? '仅性能' : '精度+性能'}</td>
      <td>${size}</td>
      <td>${state_}</td>
      <td style="text-align:right">${action}</td>
    </tr>`;
  }).join('');

  $('#dataset-list').innerHTML = `<table>
    <thead><tr><th>数据集</th><th>标识</th><th>变体</th><th>支持模式</th>
      <th>大小</th><th>状态</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;

  $$('[data-dl]').forEach(b => b.addEventListener('click', async () => {
    try {
      const r = await api(`/api/datasets/${b.dataset.dl}/download`, { method: 'POST' });
      toast(r.message);
      loadDatasets();
    } catch (e) { toast('下载失败: ' + e.message, true); }
  }));

  if (anyActive) setTimeout(loadDatasets, 1500);
}

/* ------------------------------------------------------------ 结果对比 */
async function loadResults() {
  const kind = $('#compare-kind').value;
  try { state.results = await api('/api/results?kind=' + kind); }
  catch (e) { toast('加载结果失败: ' + e.message, true); return; }

  // 与列表对账：切到精度结果、或某条任务被删掉之后，那些 id 仍留在
  // state.selected 里 —— 它们的复选框已经不存在，用户**没有任何 UI 手段取消勾选**，
  // 累积到 8 条就会把后续的勾选全部拦住（"最多同时对比 8 条"却一条也取消不掉），
  // 只能刷新页面。同时释放它们占的颜色槽位。
  const alive = new Set(state.results.map(r => r.job_id));
  for (const jid of [...state.selected]) {
    if (!alive.has(jid)) {
      state.selected.delete(jid);
      state.colorSlots.delete(jid);
    }
  }
  renderResults();
  renderCompareCharts();
}

function renderResults() {
  const el = $('#result-list');
  const byJob = new Map();
  for (const r of state.results) {
    if (!byJob.has(r.job_id)) byJob.set(r.job_id, []);
    byJob.get(r.job_id).push(r);
  }
  if (!byJob.size) {
    el.innerHTML = '<div class="empty">还没有结果。跑完一次测试后这里会出现可对比的记录。</div>';
    return;
  }
  const kind = $('#compare-kind').value;
  const rows = [...byJob.entries()].map(([jid, rs]) => {
    const r0 = rs[0];
    const p = r0.job_params || {};
    // 色块只对**已勾选**的运行上色（它才是图里真正用的颜色）；
    // 没勾选的显示成中性边框色，避免"看着有色其实没画"的误解
    const color = colorOf(jid);
    const checked = state.selected.has(jid) ? 'checked' : '';
    const conc = r0.concurrency ?? p.concurrency ?? '—';
    const ds = state.datasets.find(d => d.family === p.dataset);
    let summary;
    if (kind === 'perf') {
      const m = r0.metrics || {};
      summary = `<span class="num">吞吐 ${fmt(m.request_throughput, 2)} req/s · 输出 ${fmt(m.output_token_throughput, 1)} token/s · TTFT ${fmt(m.ttft_average,1)} ms</span>`;
    } else {
      summary = rs.map(r => `${r.dataset} ${fmt(r.metrics?.main_score)}`).join(' · ') || '—';
    }
    return `<tr>
      <td><input type="checkbox" data-sel="${jid}" ${checked} style="width:auto"></td>
      <td><span data-swatch="${jid}" style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${color};margin-right:7px"></span>
          ${escapeHtml(r0.job_note || r0.job_label || jid)}
          ${r0.job_note ? `<div class="sub">${escapeHtml(r0.job_label || '')}</div>` : ''}</td>
      <td>${escapeHtml(ds?.label || p.dataset || '—')}</td>
      <td class="num">${kind === 'perf' ? conc : '—'}</td>
      <td class="mono">${escapeHtml(p.api_type || '—')}</td>
      <td class="mono">${escapeHtml(p.model || '(自动)')}</td>
      <td>${ts(r0.created_at)}</td>
      <td>${summary}</td>
    </tr>`;
  }).join('');

  el.innerHTML = `<table>
    <thead><tr><th></th><th>运行</th><th>数据集</th><th class="num">并发</th>
      <th>接口</th><th>模型</th><th>时间</th><th>概要</th></tr></thead>
    <tbody>${rows}</tbody></table>`;

  $$('[data-sel]').forEach(cb => cb.addEventListener('change', () => {
    const jid = cb.dataset.sel;
    if (cb.checked) {
      if (state.selected.size >= SERIES.length) {
        cb.checked = false;
        toast(`最多同时对比 ${SERIES.length} 条（分类色只有 8 个槽位，再多就无法保证可辨识）`, true);
        return;
      }
      state.selected.add(jid);
      colorFor(jid);              // 占住一个空闲槽位，保证同时展示的几条不撞色
    } else {
      state.selected.delete(jid);
      state.colorSlots.delete(jid);   // 释放槽位，给后面勾选的用
    }
    // 只更新这一行的色块。**不要整表重渲染** —— 那会把 DOM 换掉，
    // 正在交互的复选框和滚动位置都会丢。
    const sw = document.querySelector(`[data-swatch="${jid}"]`);
    if (sw) sw.style.background = colorOf(jid);
    renderCompareCharts();
  }));
}

async function renderCompareCharts() {
  const el = $('#compare-charts');
  const ids = [...state.selected];
  if (ids.length < 1) {
    el.innerHTML = '<div class="card"><div class="empty">勾选上方记录以生成对比图。</div></div>';
    return;
  }
  const kind = $('#compare-kind').value;
  let data;
  try { data = await api('/api/results/compare?job_ids=' + ids.join(',')); }
  catch (e) { toast('加载对比数据失败: ' + e.message, true); return; }

  if (kind === 'accuracy') {
    el.innerHTML = chartCard('acc-score', '各数据集得分', '得分', true);
    drawAccuracyCompare(data.runs);
  } else {
    // 吞吐和时延必须分开：吞吐是 req/s 与 token/s，时延是 ms。
    // 请求吞吐（个位数到几十）与 token 吞吐（上百到上千）量级差太多，
    // 塞进同一根轴会让小的一方完全看不见，所以拆成小倍数图。
    el.innerHTML =
      chartCard('cmp-rps', '请求吞吐对比', 'req/s', true) +
      chartCard('cmp-tok', '输出 token 吞吐对比', 'token/s', true) +
      chartCard('cmp-lat', '时延对比', '毫秒', true) +
      chartCard('cmp-bars', '单指标横向对比', '', true);
    drawPerfCompare(data.runs);
  }
}

function chartCard(id, title, sub, withTable) {
  return `<div class="card">
    <div class="chart-head">
      <h3>${title}</h3>
      ${sub ? `<span class="sub">${sub}</span>` : ''}
      <div class="spacer"></div>
      ${withTable ? `<div class="viewtoggle" data-toggle="${id}">
        <button class="active" data-view="chart">图</button>
        <button data-view="table">表格</button></div>` : ''}
    </div>
    <div class="chartview" id="chart-${id}"><div class="chart" id="${id}"></div></div>
    <div class="tableview" id="table-${id}"></div>
  </div>`;
}

function bindToggles(root = document) {
  $$('[data-toggle]', root).forEach(tg => {
    const id = tg.dataset.toggle;
    $$('button', tg).forEach(b => b.addEventListener('click', () => {
      $$('button', tg).forEach(x => x.classList.toggle('active', x === b));
      const isChart = b.dataset.view === 'chart';
      $(`#chart-${id}`).classList.toggle('hidden', !isChart);
      $(`#table-${id}`).classList.toggle('active', !isChart);
      if (isChart && window.Plotly) {
        const gd = document.getElementById(id);
        if (gd && gd.data) Plotly.Plots.resize(gd);
      }
    }));
  });
}

/* Plotly 通用布局，颜色全部走 CSS 变量，深浅色切换时重绘即可 */
function baseLayout(extra = {}) {
  return Object.assign({
    paper_bgcolor: cssvar('--surface-1'),
    plot_bgcolor: cssvar('--surface-1'),
    font: { family: 'system-ui, -apple-system, "Segoe UI", sans-serif',
            size: 12, color: cssvar('--text-secondary') },
    margin: { l: 62, r: 26, t: 12, b: 46 },
    hoverlabel: { bgcolor: cssvar('--surface-1'), bordercolor: cssvar('--axis'),
                  font: { color: cssvar('--text-primary'), size: 12 } },
    showlegend: true,
    legend: { orientation: 'h', y: -0.22, x: 0, font: { size: 11.5 },
              bgcolor: 'rgba(0,0,0,0)' },
    xaxis: { gridcolor: cssvar('--grid'), zeroline: false, linecolor: cssvar('--axis'),
             tickfont: { color: cssvar('--text-secondary') }, title: { font: { size: 11.5 } } },
    yaxis: { gridcolor: cssvar('--grid'), zeroline: false, linecolor: cssvar('--axis'),
             tickfont: { color: cssvar('--text-secondary') }, title: { font: { size: 11.5 } } },
  }, extra);
}

const PLOT_CFG = { responsive: true, displayModeBar: false };

function draw(gd, traces, layout) {
  if (!window.Plotly) return;
  Plotly.react(gd, traces, layout, PLOT_CFG);
}

/* 图表的表格孪生视图 */
/* 表格视图孪生。**每一格都要转义** —— 调用方传进来的多是用户可控内容
   （最典型的是运行记录的 label，它优先取用户写的备注）。这里是 innerHTML，
   不转义就是存储型 XSS：备注里写 <img onerror>，别人一勾选就执行。
   （实测复现过。） */
function tableHtml(headers, rows) {
  return `<table><thead><tr>${headers.map((h, i) =>
    `<th class="${i ? 'num' : ''}">${escapeHtml(h)}</th>`).join('')}</tr></thead>
    <tbody>${rows.map(r => `<tr>${r.map((c, i) =>
      `<td class="${i ? 'num' : ''}">${escapeHtml(c)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}

/* ---- 精度对比 ---- */
function drawAccuracyCompare(runs) {
  const datasets = [...new Set(runs.flatMap(r => r.results.map(x => x.dataset)))];
  const traces = runs.map(r => {
    const byDs = Object.fromEntries(r.results.map(x => [x.dataset, x.metrics?.main_score]));
    return {
      type: 'bar',
      name: r.label,
      x: datasets,
      y: datasets.map(d => byDs[d] ?? null),
      marker: { color: colorFor(r.job_id) },
      hovertemplate: '%{x}<br>%{y:.2f}<extra>' + r.label + '</extra>',
    };
  });
  const layout = baseLayout({
    barmode: 'group', bargap: 0.35, bargroupgap: 0.08,
    xaxis: Object.assign(baseLayout().xaxis, { title: { text: '数据集' } }),
    yaxis: Object.assign(baseLayout().yaxis, { title: { text: '得分' } }),
  });
  draw($('#acc-score'), traces, layout);

  const rows = datasets.map(d => {
    const cells = [d];
    runs.forEach(r => {
      const x = r.results.find(y => y.dataset === d);
      cells.push(fmt(x?.metrics?.main_score));
    });
    return cells;
  });
  $('#table-acc-score').innerHTML = tableHtml(['数据集', ...runs.map(r => r.label)], rows);
  bindToggles($('#compare-charts'));
}

/* 指标选择器里的一项长这样：[标签, 指标键, 画图前的换算?, 显示格式化?]。
   取数和显示**必须走同一套换算**（下面两个 helper），否则会画出
   柱高到 50、标签却写 0.51% 这种自相矛盾的图。 */
function metricValue(run, spec) {
  const v = run.results?.[0]?.metrics?.[spec[1]] ?? null;
  return spec[2] ? spec[2](v) : v;
}
function metricLabel(v, spec) {
  return spec[3] ? spec[3](v) : fmt(v);
}

/* ---- 性能对比 ---- */
function drawPerfCompare(runs) {
  const labels = runs.map(r => r.label);

  const pick = (r, key) => {
    const m = r.results[0]?.metrics || {};
    return m[key] ?? null;
  };

  // 单个指标一张图（小倍数）：颜色跟随运行记录，和上方表格的色块一一对应，
  // 所以不需要图例，但保留每根柱的值标签便于直接读数。
  const singleMetric = (sel, key, unit, title) => {
    draw($(sel), [{
      type: 'bar',
      x: labels,
      y: runs.map(r => pick(r, key)),
      marker: {
        color: runs.map(r => colorFor(r.job_id)),
        line: { color: cssvar('--surface-1'), width: 2 },  // 2px 表面描边分隔相邻柱
      },
      text: runs.map(r => fmt(pick(r, key))),
      textposition: 'outside',
      textfont: { color: cssvar('--text-secondary'), size: 11 },
      hovertemplate: `%{x}<br>${title}: %{y:.2f} ${unit}<extra></extra>`,
      showlegend: false,
    }], baseLayout({
      bargap: 0.45, showlegend: false, margin: { l: 68, r: 26, t: 24, b: 78 },
      yaxis: Object.assign(baseLayout().yaxis, { title: { text: `${title} (${unit})` } }),
      xaxis: Object.assign(baseLayout().xaxis, { tickangle: -18 }),
    }));
  };
  singleMetric('#cmp-rps', 'request_throughput', 'req/s', '请求吞吐');
  singleMetric('#cmp-tok', 'output_token_throughput', 'token/s', '输出 token 吞吐');

  // 时延：全部以毫秒为单位，可以共用一根轴
  const latSpecs = [
    ['TTFT 均值', 'ttft_average'], ['TTFT P99', 'ttft_p99'],
    ['TPOT 均值', 'tpot_average'], ['TPOT P99', 'tpot_p99'],
    ['E2EL 均值', 'e2el_average'], ['E2EL P99', 'e2el_p99'],
  ];
  const lTraces = latSpecs.map(([label, key], i) => ({
    type: 'bar', name: label,
    x: labels, y: runs.map(r => pick(r, key)),
    marker: { color: cssvar(SERIES[i % SERIES.length]) },
    hovertemplate: `%{x}<br>%{y:.2f} ms<extra>${label}</extra>`,
  }));
  draw($('#cmp-lat'), lTraces, baseLayout({
    barmode: 'group', bargap: 0.35, bargroupgap: 0.08,
    yaxis: Object.assign(baseLayout().yaxis, { title: { text: '毫秒 (ms)' } }),
  }));

  // 单指标横向对比：一次只看一个指标，避免多轴混淆
  // [标签, 指标键, 画图前的换算?, 对**换算后**的值做显示的格式化?]
  //
  // 命中率存的是 0~1 的小数，直接画出来是根几乎看不见的柱子，
  // 所以画之前乘 100，轴上按百分数读。
  //
  // 注意顺序：格式化函数拿到的是**换算后**的值。两者串起来用下面两个
  // helper，不要各写各的 —— 柱高用换算值、标签用原始值的话，
  // 会出现"柱子到 50、标签写 0.51%"这种自相矛盾的图。
  const asPct = (v) => (v === null || v === undefined) ? null : v * 100;
  const showPct = (v) => (v === null || v === undefined || isNaN(v)) ? '—' : v.toFixed(2) + '%';
  const metricSel = [
    ['请求吞吐 (req/s)', 'request_throughput'],
    ['输出 token 吞吐 (token/s)', 'output_token_throughput'],
    ['Prefix 命中率 (%)', 'prefix_hit_rate', asPct, showPct],
    ['TTFT 均值 (ms)', 'ttft_average'],
    ['TTFT P99 (ms)', 'ttft_p99'],
    ['TPOT 均值 (ms)', 'tpot_average'],
    ['TPOT P99 (ms)', 'tpot_p99'],
    ['E2EL P99 (ms)', 'e2el_p99'],
    ['最大并发', 'max_concurrency'],
  ];
  $('#cmp-bars').innerHTML = '';
  const sel = document.createElement('select');
  sel.style.cssText = 'width:auto;margin-bottom:12px';
  sel.innerHTML = metricSel.map(([l, k]) => `<option value="${k}">${l}</option>`).join('');
  const plot = document.createElement('div');
  plot.className = 'chart';
  $('#cmp-bars').appendChild(sel);
  $('#cmp-bars').appendChild(plot);

  const render = () => {
    const key = sel.value;
    const spec = metricSel.find(s => s[1] === key);
    const traces = [{
      type: 'bar', x: labels, y: runs.map(r => metricValue(r, spec)),
      marker: {
        color: runs.map(r => colorFor(r.job_id)),
        line: { color: cssvar('--surface-1'), width: 2 },   // 2px 表面描边分隔相邻柱
      },
      text: runs.map(r => metricLabel(metricValue(r, spec), spec)),
      textposition: 'outside',
      textfont: { color: cssvar('--text-secondary'), size: 11 },
      hovertemplate: `%{x}<br>${spec[0]}: %{y}<extra></extra>`,
      showlegend: false,
    }];
    draw(plot, traces, baseLayout({
      bargap: 0.45, showlegend: false, margin: { l: 62, r: 26, t: 22, b: 74 },
      yaxis: Object.assign(baseLayout().yaxis, { title: { text: spec[0] } }),
      xaxis: Object.assign(baseLayout().xaxis, { tickangle: -18 }),
    }));
  };
  sel.addEventListener('change', render);
  render();

  // 表格孪生
  $('#table-cmp-rps').innerHTML = tableHtml(
    ['运行', '请求吞吐 (req/s)'],
    runs.map(r => [r.label, fmt(pick(r, 'request_throughput'))]));
  $('#table-cmp-tok').innerHTML = tableHtml(
    ['运行', '输出 token 吞吐 (token/s)'],
    runs.map(r => [r.label, fmt(pick(r, 'output_token_throughput'))]));
  $('#table-cmp-lat').innerHTML = tableHtml(
    ['运行', ...latSpecs.map(s => s[0] + ' (ms)')],
    runs.map(r => [r.label, ...latSpecs.map(s => fmt(pick(r, s[1])))]));
  $('#table-cmp-bars').innerHTML = tableHtml(
    ['运行', ...metricSel.map(s => s[0])],
    runs.map(r => [r.label, ...metricSel.map(s => metricLabel(metricValue(r, s), s))]));

  bindToggles($('#compare-charts'));
}

/* ---- 并发扫描曲线 ---- */
async function drawSweepCharts(rootSel, job) {
  const series = job.series || [];
  if (!series.length) return;
  const root = $(rootSel);
  const id = 'sweep-charts';
  const holder = document.createElement('div');
  holder.id = id;
  // 插到结果卡片之前
  const anchor = $('#detail-body .card');
  root.insertBefore(holder, anchor);

  holder.innerHTML =
    chartCard('sw-tput', '并发 - 吞吐', '随并发上升至拐点后趋平即达饱和', true) +
    chartCard('sw-lat', '并发 - 时延', '毫秒；拐点之后通常开始劣化', true) +
    chartCard('sw-tok', '并发 - 单请求输出速度', 'token/s', true);

  const xs = series.map(p => p.concurrency);

  // 并发轴：跨度大（>=8 倍）用对数，否则线性。
  // 跨度小时用对数会凭空多出无意义的刻度（例如只有 2 和 4 时冒出个 3）。
  const lo = Math.min(...xs), hi = Math.max(...xs);
  const xAxis = (titleTxt) => Object.assign(baseLayout().xaxis, {
    title: { text: titleTxt },
    type: (lo > 0 && hi / lo >= 8) ? 'log' : 'linear',
    ...(lo > 0 && hi / lo >= 8 ? { dtick: 'D1' } : {}),
  });

  const line = (name, ys, color, unit) => ({
    type: 'scatter', mode: 'lines+markers', name,
    x: xs, y: ys,
    line: { color, width: 2, shape: 'linear' },
    marker: { color, size: 9, line: { color: cssvar('--surface-1'), width: 2 } },
    hovertemplate: `并发 %{x}<br>${name}: %{y:.2f}${unit}<extra></extra>`,
  });

  draw($('#sw-tput'), [
    line('请求吞吐 (req/s)', series.map(p => p.request_throughput), cssvar('--series-1'), ' req/s'),
    line('输出 token 吞吐 (token/s)', series.map(p => p.output_token_throughput), cssvar('--series-2'), ' token/s'),
  ], baseLayout({
    xaxis: xAxis('并发数'),
    yaxis: Object.assign(baseLayout().yaxis, { title: { text: '吞吐' } }),
  }));

  draw($('#sw-lat'), [
    line('TTFT 均值 (ms)', series.map(p => p.ttft_average), cssvar('--series-1'), ' ms'),
    line('TTFT P99 (ms)', series.map(p => p.ttft_p99), cssvar('--series-3'), ' ms'),
    line('TPOT 均值 (ms)', series.map(p => p.tpot_average), cssvar('--series-4'), ' ms'),
    line('E2EL 均值 (ms)', series.map(p => p.e2el_average), cssvar('--series-5'), ' ms'),
  ], baseLayout({
    xaxis: xAxis('并发数'),
    yaxis: Object.assign(baseLayout().yaxis, { title: { text: '毫秒 (ms)' } }),
  }));

  draw($('#sw-tok'), [
    line('单请求输出速度 (token/s)', series.map(p => p.output_token_speed_average ?? p.metrics?.output_token_speed_average),
         cssvar('--series-1'), ' token/s'),
  ], baseLayout({
    showlegend: false,
    xaxis: xAxis('并发数'),
    yaxis: Object.assign(baseLayout().yaxis, { title: { text: 'token/s' } }),
  }));

  $('#table-sw-tput').innerHTML = tableHtml(
    ['并发', '请求吞吐 req/s', '输出 token/s', '成功', '失败'],
    series.map(p => [p.concurrency, fmt(p.request_throughput), fmt(p.output_token_throughput),
                     fmtInt(p.success_requests), fmtInt(p.failed_requests)]));
  $('#table-sw-lat').innerHTML = tableHtml(
    ['并发', 'TTFT 均值', 'TTFT P99', 'TPOT 均值', 'E2EL 均值', 'E2EL P99'],
    series.map(p => [p.concurrency, fmt(p.ttft_average, 1), fmt(p.ttft_p99, 1),
                     fmt(p.tpot_average, 2), fmt(p.e2el_average, 1), fmt(p.e2el_p99, 1)]));
  $('#table-sw-tok').innerHTML = tableHtml(
    ['并发', '输出速度 token/s'],
    series.map(p => [p.concurrency,
      fmt(p.output_token_speed_average ?? p.metrics?.output_token_speed_average)]));

  bindToggles(holder);
}

function redrawCharts() {
  // 深浅色切换后重绘（颜色取自 CSS 变量）
  if (state.detailJob) refreshDetail();
  if ($('#tab-compare').classList.contains('active')) renderCompareCharts();
}

init();
