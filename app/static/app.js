const $ = (selector) => document.querySelector(selector);
const escapeHTML = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const names = {queued:'等待采集',running:'正在采集',needs_login:'等待登录 / 验证',completed:'已采集',partial:'部分完成',failed:'采集失败',interrupted:'已中断'};
const categories = {main:'主图',sku:'规格图',detail:'详情图'};
let selected = null, loggedIn = false, busy = false, timer, lastJob = null;
const requestKeys = new Map();

function message(text) { $('#message').textContent = text; $('#message').hidden = !text; }
function showLogin() { loggedIn = false; $('#workspace').hidden = true; $('#login-panel').hidden = false; $('#logout').hidden = true; }
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: {'Content-Type':'application/json', ...options.headers} });
  let result;
  try { result = await response.json(); } catch { throw new Error(`请求失败 (${response.status})`); }
  if (!response.ok) {
    if (response.status === 401) showLogin();
    const detail = result.detail;
    throw new Error(Array.isArray(detail) ? detail.map(i => i.msg || String(i)).join('\n') : detail || '操作未完成');
  }
  return result;
}
async function action(callback) {
  if (busy) return;
  busy = true; message('');
  document.querySelectorAll('button').forEach(b => { b.disabled = true; });
  try { await callback(); } catch (error) { message(error.message); }
  finally { busy = false; document.querySelectorAll('button').forEach(b => { b.disabled = false; }); }
}
function badge(status) {
  return `<span class="badge ${['needs_login','partial'].includes(status) ? 'warn' : ['failed','interrupted'].includes(status) ? 'fail' : ''}">${escapeHTML(names[status] || status)}</span>`;
}
function date(value) { return new Date(value).toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false,timeZone:'Asia/Shanghai'}); }
function money(value) { return value?.cents != null ? `¥${(value.cents / 100).toFixed(2)}` : value?.raw != null ? `${escapeHTML(value.raw)}（单位待核实）` : '未取得'; }
function section(number, title, content) { return `<section class="detail-section"><div class="section-title"><span class="number">${number}</span><h3>${title}</h3></div>${content}</section>`; }

function render(job) {
  lastJob = job;
  const product = job.product;
  const resume = ['needs_login','failed','partial','interrupted'].includes(job.status);
  let html = `<div class="detail-head"><div>${badge(job.status)}<p>${escapeHTML(job.message)}</p></div><div class="actions">${resume ? `<button id="resume">${job.status === 'needs_login' ? '继续采集' : '重新采集'}</button>` : ''}${job.download_ready ? `<a class="button primary" href="/api/jobs/${job.id}/download">下载资料与全部已取得图片 ↓</a>` : ''}</div></div>`;
  if (job.status === 'needs_login') html += `<div class="notice">请在采集浏览器中处理登录或验证，再点击“继续采集”。如果等待超时、窗口已关闭，点击后会重新打开。</div>`;
  if (!product) {
    html += `<div class="empty"><span class="empty-mark">${job.status === 'running' ? '↻' : '↗'}</span><h2>${escapeHTML(names[job.status])}</h2><p>${escapeHTML(job.message)}</p><a class="source-link" href="${escapeHTML(job.source_url)}" target="_blank" rel="noreferrer">查看原商品链接 ↗</a></div>`;
  } else {
    if (product.warnings.length) html += `<div class="notice">${product.warnings.map(escapeHTML).join('\n')}</div>`;
    html += section('01', '标题', `<p class="product-title">${escapeHTML(product.title)}</p><a class="source-link" href="${escapeHTML(product.canonical_url)}" target="_blank" rel="noreferrer">查看拼多多商品 ↗</a><p class="muted">商品 ID ${escapeHTML(product.goods_id)} · 采集于 ${escapeHTML(date(product.captured_at))}</p>`);
    const rows = product.skus.map(sku => `<tr><td>${escapeHTML(sku.label)}<br><span class="muted">${escapeHTML(sku.sku_id)}</span></td><td>${money(sku.group_price)}</td><td>${money(sku.normal_price)}</td><td>${money(sku.other_price)}</td><td>${escapeHTML(sku.price_note || '页面未注明')}</td></tr>`).join('');
    html += section('02', `商品规格价格 <span class="muted">${product.skus.length} 个规格</span>`, rows ? `<div class="table-scroll"><table><thead><tr><th>商品规格</th><th>拼团价</th><th>单买价</th><th>其他价格</th><th>价格条件</th></tr></thead><tbody>${rows}</tbody></table></div><p class="muted">展示采集时的价格；优惠条件和最终成交价请在原页面确认。</p>` : '<p class="muted">未取得规格价格。商品展示的最低价不会替代各规格价格。</p>');
    const summary = product.image_summary;
    const imageHeading = summary ? `已下载 ${summary.downloaded} / 发现 ${summary.discovered} 张` : `发现 ${product.images.length} 张，下载中`;
    const pictures = product.images.map((image,index) => `<figure class="image-card">${image.status === 'downloaded' ? `<a href="/api/jobs/${job.id}/images/${index}" target="_blank"><img loading="lazy" src="/api/jobs/${job.id}/images/${index}" alt="${escapeHTML(image.categories.map(c=>categories[c]).join(' / '))} ${index+1}"></a>` : `<div class="image-failed">${escapeHTML(image.error || '等待下载')}</div>`}<figcaption>${escapeHTML(image.categories.map(c=>categories[c]).join(' / '))}${image.width ? ` · ${image.width}×${image.height}` : ''}</figcaption></figure>`).join('');
    html += section('03', `商品图片 <span class="muted">${imageHeading}</span>`, pictures ? `<div class="image-grid">${pictures}</div>` : '<p class="muted">未取得商品图片。</p>');
    html += section('04', '商品注意事项', (product.notices.length ? product.notices.map(note => `<div class="note-item">${escapeHTML(note.text)}<span class="note-source">${note.kind === 'needs_review' ? '页面文字 · 请人工核对' : '商品数据中的说明'}</span></div>`).join('') : '<p class="muted">未取得可读取的注意事项文字，不代表商品没有注意事项。</p>') + '<p class="muted">图片内文字尚未识别，请同时查看详情图中的尺寸、使用限制和其他说明。</p>');
    if (product.properties.length) html += section('＋', '商品参数', `<div class="properties">${product.properties.map(p => `<div><span>${escapeHTML(p.name)}</span>${escapeHTML(p.value)}</div>`).join('')}</div>`);
  }
  $('#detail').innerHTML = html;
  $('#resume')?.addEventListener('click', () => action(async () => { render(await api(`/api/jobs/${selected}/resume`, {method:'POST'})); await refresh(); }));
}

async function refresh() {
  const jobs = await api('/api/jobs');
  loggedIn = true; $('#workspace').hidden = false; $('#login-panel').hidden = true; $('#logout').hidden = false;
  if (!selected && jobs.length) selected = jobs[0].id;
  $('#job-list').innerHTML = jobs.length ? jobs.map(job => `<button class="job-card ${job.id === selected ? 'active' : ''}" data-job="${job.id}"><span class="job-title">${escapeHTML(job.title || '正在读取商品资料')}</span><span class="job-meta">${badge(job.status)}<span>${escapeHTML(date(job.created_at))}</span></span></button>`).join('') : '<p class="muted">还没有采集记录</p>';
  const requestedId = selected;
  if (requestedId) {
    const job = await api(`/api/jobs/${requestedId}`);
    if (selected === requestedId && JSON.stringify(job) !== JSON.stringify(lastJob)) render(job);
  }
}
$('#job-list').addEventListener('click', event => { const card = event.target.closest('[data-job]'); if (card) action(async () => { selected = card.dataset.job; await refresh(); }); });
$('#refresh').addEventListener('click', () => action(refresh));
$('#login-form').addEventListener('submit', event => { event.preventDefault(); action(async () => { await api('/api/session', {method:'POST', body:JSON.stringify({token:$('#token').value.trim()})}); $('#token').value=''; await refresh(); }); });
$('#logout').addEventListener('click', () => action(async () => { await api('/api/session', {method:'DELETE'}); selected=null; lastJob=null; showLogin(); }));
$('#import-form').addEventListener('submit', event => {
  event.preventDefault(); action(async () => {
    const text = $('#share').value.trim();
    if (!requestKeys.has(text)) requestKeys.set(text, crypto.randomUUID());
    const job = await api('/api/jobs', {method:'POST', headers:{'Idempotency-Key':requestKeys.get(text)}, body:JSON.stringify({text})});
    requestKeys.delete(text); selected=job.id; $('#share').value=''; render(job); await refresh();
  });
});
async function poll() {
  if (loggedIn && !busy && !document.hidden) { try { await refresh(); } catch (error) { message(error.message); } }
  timer = setTimeout(poll, 2500);
}
action(refresh).then(poll);
window.addEventListener('pagehide', () => clearTimeout(timer));
