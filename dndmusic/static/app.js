'use strict';
const $ = (s) => document.querySelector(s);
const TEMPO_LABELS = {slow: 'Slow', medium: 'Medium', fast: 'Fast'};
const QUALITY_LABELS = {1: 'Primary school guitar lesson', 2: 'Regional orchestra on a good night', 3: '(almost) John Williams'};

const RUNPOD_DOCS = 'https://github.com/TheThirdAdmin/dnd-music-creator/blob/main/docs/runpod.md';

function cap(s) { return s.charAt(0).toUpperCase() + s.slice(1); }

function options(values, selected, label) {
  return values.map((v) => `<option value="${esc(v)}"${v === selected ? ' selected' : ''}>${esc(label(v))}</option>`).join('');
}

let vocab = null;
let state = null;
let editing = null;          // {mode: 'new' | 'edit' | 'custom', id}
let volumeBusy = false;
let qualityBusy = false;
let scenesHtml = '';
let toasts = [];             // [{msg, until}] transient banner rows

async function api(method, path, body) {
  const opts = {method, headers: {}};
  if (body !== undefined) {
    opts.headers['content-type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try {
      const j = await r.json();
      msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
    } catch (e) { /* keep statusText */ }
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function mmss(sec) {
  const s = Math.max(0, Math.floor(sec));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function fade() { return Number($('#fade').value); }

function store(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* private mode */ } }
function recall(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }

function toast(msg) {
  toasts = toasts.filter((t) => t.msg !== msg);
  toasts.push({msg, until: Date.now() + 5000});
  renderBanners();
  clearTimeout(toast.t);
  toast.t = setTimeout(renderBanners, 5100);
}

// ---- rendering

const EMBLEM = '/static/icon.svg';
function iconSrc(name) {   // scene icon from the manifest, else the generic emblem
  const p = bgMod && bgMod.iconFor(bgManifest, name);
  return p ? `/static/bg/${p}` : EMBLEM;
}

function renderBar() {
  const np = state.now_playing;
  const ni = $('#nowicon');
  ni.hidden = !np;
  if (np) { ni.src = iconSrc(np.icon || np.name); ni.alt = np.name; }
  let text = np ? `${np.name} · ${mmss(np.position_s)} / ${mmss(np.duration_s)}` : 'Nothing playing';
  if (np && state.votes && state.votes.players > 0) {
    text += ` · votes +${state.votes.likes} / -${state.votes.dislikes} of ${state.votes.needed}`;
  }
  if (state.pending) text += ' · generating next…';
  $('#now').textContent = text;
  const dur = np ? Number(np.duration_s) : 0;   // display-only track progress, same render path as the text
  const pct = dur > 0 ? Math.max(0, Math.min(100, (Number(np.position_s) || 0) / dur * 100)) : 0;
  $('#progress').hidden = !(dur > 0);
  $('#progress').setAttribute('aria-valuenow', String(Math.round(pct)));
  $('#progressfill').style.width = pct + '%';
  $('#favbtn').hidden = $('#banbtn').hidden = !np;
  $('#favbtn').classList.toggle('on', !!np && np.rating === 'fav');
  if (!volumeBusy) $('#volume').value = state.cast.volume;
  const c = state.cast;
  $('#caststatus').textContent = c.casting && c.targets.length
    ? c.targets.map((t) => `${t.name}: ${t.status}`).join(', ')
    : 'Not casting';
  const castBad = c.casting ? c.targets.filter((t) => /^(error|not found)/.test(t.status || '')) : [];
  $('#caststatus').classList.toggle('on', c.casting && !castBad.length);
  castBanner();
}

// One banner stack: transient messages, standing warnings, model download progress, "not casting".
function castBanner() { renderBanners(); }

function standingWarnings() {
  const gens = Object.entries((state && state.generators) || {});
  const down = gens.filter(([, h]) => h.ok === false);
  let msg = '';
  let pct = null;
  const dl = gens.map(([, h]) => /^downloading\s+(\d+)%/i.exec(h.status || '')).filter(Boolean).map((m) => +m[1]);
  if (dl.length) pct = Math.min(...dl);
  if (state.disk_low) msg = 'Disk almost full: generation paused.';
  else if (gens.length && down.length === gens.length && pct === null) msg = 'No generator reachable: playing from the library only.';
  const low = (state.status || []).filter((r) => r.floor_s && r.ready_s < r.floor_s);
  const rows = [];
  if (msg) rows.push({msg});
  if (!state.disk_low && low.length) {
    const parts = low.map((r) => `${r.role === 'playing' ? 'The playing scene' : `Next scene "${r.name}"`} has `
      + `${minutes(r.ready_s)} of music ready${r.role === 'playing' ? ' and will repeat' : ''}`
      + `${r.generating ? ' (more is being generated)' : ' (nothing is generating it right now)'}`);
    rows.push({msg: parts.join('. ') + '.'});
  }
  const c = state.cast;
  if (c && c.casting) {
    for (const t of c.targets) {
      if (/^(error|not found)/.test(t.status || '')) rows.push({msg: `Speaker ${t.name}: ${t.status}`});
    }
  }
  const rp = state.runpod;
  if (rp && rp.configured) {
    if (rp.blocked) rows.push({msg: rp.blocked});
    else if (rp.warming) {
      rows.push({msg: rp.cold_start_s
        ? `Warming up the generator on Runpod. First track in about ${Math.round(rp.cold_start_s)} s.`
        : 'Warming up the generator on Runpod. The first start can take 6-10 minutes (container download and model load); later starts are much quicker.'});
    }
    const b = rp.balance;
    if (b && b.low && b.usd !== null) {
      rows.push({msg: `Runpod balance is $${b.usd.toFixed(2)}${b.tracks_left === null ? '' : `: about ${b.tracks_left} tracks left`}.`});
    }
  }
  return {rows, pct};
}

function renderBanners() {
  if (!state) return;
  const now = Date.now();
  toasts = toasts.filter((t) => t.until > now);
  const {rows, pct} = standingWarnings();
  const all = toasts.map((t) => ({msg: t.msg})).concat(rows);
  if (pct !== null) all.push({msg: `Downloading the music models: ${pct}% (first start only). Until it finishes, only the library plays.`, pct});
  const c = state.cast;
  if (c && !c.casting && $('#listen').getAttribute('aria-pressed') !== 'true') {
    all.push({msg: 'Not casting. Pick speakers in Settings and press Cast, or press Listen here.'});
  }
  $('#banners').innerHTML = all.map((r) => r.pct === undefined
    ? `<div class="castbanner" role="status">${esc(r.msg)}</div>`
    : `<div class="castbanner info" role="status">${esc(r.msg)}<div class="dlbar"><div style="width:${r.pct}%"></div></div></div>`).join('');
}

function renderWarn() { renderBanners(); }

// Scene search: every word typed must appear in the scene's name, place, mood or tempo.
function sceneMatches(s, words) {
  const hay = `${s.name} ${s.location} ${s.mood} ${s.tempo}`.toLowerCase();
  return words.every((w) => hay.includes(w));
}

function renderScenes() {
  const playing = state.now_playing && state.now_playing.pool_id;
  const pending = state.pending && state.pending.pool_id;
  const words = $('#scenesearch').value.toLowerCase().split(/\s+/).filter(Boolean);
  const shown = words.length ? state.scenes.filter((s) => sceneMatches(s, words)) : state.scenes;
  const html = shown.length ? shown.map((s) => `
    <div class="card${s.pool_id === playing ? ' playing' : ''}${s.pool_id === pending ? ' pending' : ''}">
      <div class="head">
        <img class="sicon" src="${esc(iconSrc(s.icon || s.name))}" alt="${esc(s.name)}" width="48" height="48" loading="lazy">
        <div class="htext">
          <div class="title">${esc(s.name)}</div>
          <div class="meta">${esc(s.location)} · ${esc(s.mood)} · ${esc(s.tempo)}
            · ${s.ready} ready${state.jobs[s.pool_id] ? ' · generating' : ''}</div>
        </div>
      </div>
      <div class="row gap">
        <button class="primary" data-queue="${s.id}">Add to queue</button>
        <button data-apply="${s.id}">Play now</button>
        <button data-edit="${s.id}">Edit</button>
      </div>
    </div>`).join('') : `<p class="muted">${state.scenes.length ? 'No scene matches your search.' : 'No scenes yet. Add one below.'}</p>`;
  if (html !== scenesHtml) {
    scenesHtml = html;
    $('#scenes').innerHTML = html;
  }
}

let queueHtml = null;
function renderQueue() {
  const q = state.queue || [];
  const head = q.length && state.now_playing && state.now_playing.pool_id === q[0].pool_id;
  const html = q.length ? q.map((s, i) => `
    <div class="card qcard${i === 0 && head ? ' playing' : ''}" draggable="true" data-qi="${i}" title="Drag to reorder">
      <div class="head">
        <img class="sicon" src="${esc(iconSrc(s.icon || s.name))}" alt="" width="48" height="48" loading="lazy" draggable="false">
        <div class="htext">
          <div class="title">${esc(s.name)}</div>
          <div class="meta">${i === 0 && head ? 'Playing · ' : i === 0 ? 'Next up · ' : ''}${s.ready} ready${state.jobs[s.pool_id] ? ' · generating' : ''}</div>
        </div>
      </div>
      <div class="qgroup">
        <span class="qlabel muted">Queue</span>
        <div class="row gap">
          <button data-qup="${i}" aria-label="Move up in queue"${i === 0 ? ' disabled' : ''}>Up</button>
          <button data-qdown="${i}" aria-label="Move down in queue"${i === q.length - 1 ? ' disabled' : ''}>Down</button>
          <button data-qdel="${i}" aria-label="Remove from queue">Del</button>
        </div>
      </div>
    </div>`).join('') : '<p class="muted">The queue is empty. Add scenes below.</p>';
  if (html !== queueHtml) { queueHtml = html; $('#queue').innerHTML = html; }
  $('#nextscene').disabled = $('#nextscene2').disabled = !q.length || (q.length === 1 && !!head);
  $('#queuehint').textContent = q.length > 1 || (q.length === 1 && !head) ? 'Tracks keep playing until you press Next scene.' : '';
}

async function refresh() { state = await api('GET', '/api/state'); render(); }

async function putQueue(ids) {
  try { await api('PUT', '/api/queue', {ids}); await refresh(); } catch (e) { toast(e.message); }
}

async function nextScene() {
  try {
    const r = await api('POST', '/api/queue/next', {fade_s: fade()});
    if (r.pending) toast('Generating music for the next scene; it will fade in when ready.');
    await refresh();
  } catch (e) { toast(e.message); }
}

function minutes(s) {
  const m = s / 60;
  return `${m < 10 ? m.toFixed(1) : Math.round(m)} min`;
}

function renderStatus() {
  const rows = state.status || [];
  const html = rows.length ? rows.map((r) => {
    const goal = r.target_s || Math.max(r.ready_s, 1);
    const pct = Math.min(100, Math.round(100 * r.ready_s / goal));
    const low = r.floor_s && r.ready_s < r.floor_s;
    const role = r.role === 'playing' ? 'Playing' : r.role === 'next' ? 'Next up' : 'Queued';
    return `<div class="card status">
      <div class="title">${esc(r.name)} <small class="muted">${role}</small></div>
      <div class="meter" role="img" aria-label="${minutes(r.ready_s)} ready"><span class="${low ? 'low' : ''}" style="width:${pct}%"></span></div>
      <div class="meta">${r.tracks} track${r.tracks === 1 ? '' : 's'} · ${minutes(r.ready_s)} ready${r.target_s ? ` of ${minutes(r.target_s)}` : ''}${r.generating ? ` · generating ${r.generating}` : ''}${low ? ' · running low' : ''}</div>
    </div>`;
  }).join('') : '<p class="muted">Nothing playing or queued yet.</p>';
  if (html !== statusHtml) { statusHtml = html; $('#statusrows').innerHTML = html; }
  $('#statusgens').innerHTML = Object.entries(state.generators).map(([url, h]) => {
    const st = h.ok === null ? 'checking' : h.ok ? (h.busy ? 'busy' : 'ok') : (h.status && h.status !== 'ok' ? h.status : `down: ${h.error || ''}`);
    return `<li><span class="dot ${h.ok ? 'ok' : h.ok === false ? 'bad' : ''}"></span>${esc(url)} · ${esc(st)}</li>`;
  }).join('') || '<li class="muted">None configured</li>';
  $('#statusrunpod').innerHTML = runpodRows(state.runpod);
}
let statusHtml = null;

const usd = (n) => (n === null || n === undefined ? 'unknown' : `$${n.toFixed(2)}`);

// Cost figures are estimates until cold-start billing is checked against Runpod's own billing.
function lastRun(r) {   // " · rendered in 55 s · waited 290 s for a worker to start"
  if (!r) return '';
  return ` · rendered in ${Math.round(r.exec_s)} s` +
    (r.delay_s > 5 ? ` · waited ${Math.round(r.delay_s)} s for a worker to start` : '');
}

function runpodRows(rp) {
  if (!rp || !rp.configured) return '<li class="muted">Not configured (Settings)</li>';
  const c = rp.cost, b = rp.balance;
  const est = c.estimated ? ' (estimated)' : '';
  const src = {runpod: 'from Runpod', budget: 'from your budget', unknown: 'unknown'}[b.source] || b.source;
  const rows = [
    `<span class="dot ${rp.state === 'error' || rp.state === 'blocked' ? 'bad' : rp.state === 'ready' ? 'ok' : ''}"></span>${esc(rp.endpoint_id)} · ${esc(rp.state)}`,
    ...(rp.activity ? [esc(rp.activity.detail)] : []),
    `Last track${est}: ${c.last_track === null ? 'none yet' : usd(c.last_track)}${lastRun(c.last_run)}`,
    `This session${est}: ${usd(c.session)} · today: ${usd(c.today)} · this month: ${usd(c.month)}`,
    `Balance: ${usd(b.usd)} (${src})${b.tracks_left === null ? '' : ` · about ${b.tracks_left} tracks left${est}`}`,
  ];
  if (b.error) rows.push(`Balance read failed: ${esc(b.error)}`);
  return rows.map((r) => `<li>${r}</li>`).join('');
}

function renderGenerators() {
  $('#generators').innerHTML = Object.entries(state.generators).map(([url, h]) => {
    const st = h.ok === null ? 'checking' : h.ok ? (h.busy ? 'busy' : 'ok') : (h.status && h.status !== 'ok' ? h.status : `down: ${h.error || ''}`);
    return `<li><span class="dot ${h.ok ? 'ok' : h.ok === false ? 'bad' : ''}"></span>${esc(url)} · ${esc(st)}</li>`;
  }).join('') || '<li class="muted">None configured (GENERATOR_URLS)</li>';
  $('#runpoddocs').href = RUNPOD_DOCS;
  $('#streamurl').textContent = state.stream_url;
  $('#streamurl').href = state.stream_url;
}

function offeredQualities() {   // null while no generator has reported its levels
  const sets = Object.values(state.generators).map((h) => h.qualities).filter(Array.isArray);
  return sets.length ? new Set(sets.flat()) : null;
}

function renderQuality() {
  const q = $('#quality');
  if (!qualityBusy) q.value = state.quality;
  $('#qualitylabel').textContent = QUALITY_LABELS[q.value];
  const offered = offeredQualities();
  const missing = offered ? [1, 2, 3].filter((n) => !offered.has(n)) : [];
  document.querySelectorAll('.qmarks span').forEach((s) => s.classList.toggle('off', missing.includes(Number(s.dataset.q))));
  const hint = $('#qualityhint');
  hint.hidden = !missing.length;
  hint.textContent = missing.length ? `${missing.map((n) => `\u201c${QUALITY_LABELS[n]}\u201d`).join(' and ')} ${missing.length > 1 ? 'need' : 'needs'} a GPU generator.` : '';
}

async function onQualityChange() {
  const q = $('#quality');
  let n = Number(q.value);
  const offered = offeredQualities();
  if (offered && !offered.has(n)) {
    toast(`\u201c${QUALITY_LABELS[n]}\u201d needs a GPU generator.`);
    n = Math.max(...[...offered].filter((x) => x <= n), Math.min(...offered));
    q.value = n;
  }
  $('#qualitylabel').textContent = QUALITY_LABELS[n];
  try { await api('PUT', '/api/settings', {quality: n}); } catch (e) { toast(e.message); }
  qualityBusy = false;
}

// ---- favourites and library

async function rateNow(rating) {
  const np = state && state.now_playing;
  if (!np) return;
  try {
    await api('PUT', `/api/tracks/${np.track_id}/rating`, {rating});
    if (rating === 'banned') toast('Banned, skipping.');
  } catch (e) { toast(e.message); }
}

function gb(bytes) { return (bytes / 1024 ** 3).toFixed(2); }

function renderLibrary(lib) {
  const pct = lib.cap_bytes ? Math.min(100, (100 * lib.used_bytes) / lib.cap_bytes) : 0;
  $('#usagebar span').style.width = `${pct}%`;
  $('#usagebar').classList.toggle('full', pct >= 90);
  $('#usagetext').textContent = `${gb(lib.used_bytes)} of ${gb(lib.cap_bytes)} GB used · ${lib.track_count} tracks`;
  const warn = $('#librarywarn');
  warn.hidden = !(lib.over_cap || lib.cap_warning);
  warn.textContent = lib.over_cap
    ? `Favourites use ${gb(lib.used_bytes)} of ${gb(lib.cap_bytes)} GB. Raise the size in Settings.`
    : 'The size is small for your scenes: music will be regenerated often. Raise it in Settings.';
  $('#datapath').textContent = lib.data_path;
  const groups = new Map();
  for (const g of lib.favourites) {
    if (!groups.has(g.label)) groups.set(g.label, []);
    groups.get(g.label).push(g);
  }
  let html = lib.favourites.length ? '' : '<p class="muted">No favourites yet. Press Fav while a track plays.</p>';
  for (const [label, pools] of groups) {
    html += `<h2>${esc(label)}</h2>`;
    for (const p of pools) {
      html += `<div class="libpool"><div class="prompt">${esc(p.tags)}${p.lyrics ? ` · ${esc(p.lyrics)}` : ''}</div>
        <div class="row wrap"><button data-playpool="${p.pool_id}">Play on stream</button>
        <label>More <select data-morecount="${p.pool_id}">${[1, 2, 4, 8].map((n) => `<option${n === 4 ? ' selected' : ''}>${n}</option>`).join('')}</select></label></div>`;
      for (const t of p.tracks) {
        html += `<div class="libtrack"><span class="meta">${mmss(t.duration_s)} · seed ${t.seed ?? '?'} · played ${t.plays}</span>
          <button data-preview="${t.id}">Preview</button><button data-rate="${t.id}:normal">Un-fav</button>
          <button data-rate="${t.id}:banned" class="danger">Ban</button>
          <button data-more="${t.id}:${p.pool_id}">More like this</button></div>`;
      }
      html += '</div>';
    }
  }
  $('#favourites').innerHTML = html;
  $('#banned').innerHTML = lib.banned.length ? lib.banned.map((t) => `<div class="libtrack">
      <span class="meta">${esc(t.label)} · ${mmss(t.duration_s)}</span>
      <button data-preview="${t.id}">Preview</button><button data-rate="${t.id}:normal">Restore</button>
      <button data-delete="${t.id}" class="danger">Delete now</button></div>`).join('')
    : '<p class="muted">Nothing banned.</p>';
}

async function loadLibrary() {
  try { renderLibrary(await api('GET', '/api/library')); } catch (e) { toast(e.message); }
}

function previewTrack(id) {
  const a = $('#preview');
  const src = `/api/tracks/${id}/audio`;
  if (a.dataset.id === String(id) && !a.paused) { a.pause(); return; }
  a.dataset.id = String(id);
  a.src = src;
  a.play().catch((e) => toast(`Preview failed: ${e.message}`));
}

async function onLibraryClick(ev) {
  const b = ev.target.closest('button');
  if (!b) return;
  const d = b.dataset;
  try {
    if (d.preview) { previewTrack(d.preview); return; }
    if (d.rate) { const [id, rating] = d.rate.split(':'); await api('PUT', `/api/tracks/${id}/rating`, {rating}); }
    if (d.delete) await api('DELETE', `/api/tracks/${d.delete}`);
    if (d.playpool) await api('POST', '/api/apply', {pool_id: Number(d.playpool), fade_s: fade()});
    if (d.more) {
      const [id, pool] = d.more.split(':');
      const count = Number(document.querySelector(`select[data-morecount="${pool}"]`).value);
      await api('POST', `/api/tracks/${id}/more`, {count});
      toast(`Making ${count} more.`);
      return;
    }
    await loadLibrary();
  } catch (e) { toast(e.message); }
}

async function onCleanup() {
  try {
    const r = await api('POST', '/api/library/cleanup');
    toast(`Removed ${r.deleted_tracks} tracks, ${(r.freed_bytes / 1048576).toFixed(0)} MB.`);
    await loadLibrary();
  } catch (e) { toast(e.message); }
}

async function onImport(ev) {
  const file = ev.target.files[0];
  ev.target.value = '';
  if (!file) return;
  try {
    const res = await fetch('/api/library/import', {method: 'POST', body: file});
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || res.statusText);
    toast(`Imported ${body.added}, ${body.duplicates} already had, ${body.rejected.length} rejected.`);
    await loadLibrary();
  } catch (e) { toast(e.message); }
}

function renderPlayers() {
  const el = $('#players');
  if (document.activeElement !== el && state.votes) el.value = state.votes.players;
  $('#playersurl').textContent = `${location.origin}/players`;
}

function renderCap() {
  const s = $('#libcap');
  if (!capBusy) s.value = state.library_cap_gb;
  $('#libcaplabel').textContent = `${s.value} GB (about ${Math.round(s.value * 1024 / 3.4)} tracks)`;
}

let capBusy = false;

function renderCtl() {
  const b = $('#renderctl');
  b.textContent = state.paused ? 'Start rendering' : 'Stop playing & rendering';
  b.classList.toggle('primary', !!state.paused);
}

function render() {
  if (!state) return;
  renderCtl();
  renderCap();
  renderPlayers();
  renderBar();
  renderScenes();
  renderQueue();
  renderStatus();
  renderGenerators();
  renderQuality();
  renderBackground();
}

// ---- background (logic in bg.js; no images when the manifest is missing)

let bgMod = null, bgManifest = null, bgPrev = null, bgSeq = 0, bgFront = 'bgA';

async function initBackground() {
  try {
    const [mod, res] = await Promise.all([import('/static/bg.js'), fetch('/static/bg/manifest.json')]);
    const m = await res.json();
    if (!res.ok || !m || typeof m !== 'object') return;
    bgMod = mod; bgManifest = m;
    if (state) { renderBackground(); renderBar(); renderScenes(); }
  } catch (e) { /* plain ground */ }
}

async function renderBackground() {
  if (!bgMod || !state) return;
  const p = bgMod.plan(bgPrev, state, bgManifest);
  if (!p) return;
  bgPrev = p;
  const seq = ++bgSeq;
  const front = $('#' + bgFront), back = $('#' + (bgFront === 'bgA' ? 'bgB' : 'bgA'));
  if (!p.url) { front.classList.remove('on'); return; }
  // wide screens get the 1920x1080 copy in /static/bg-hd (same relative path); fall back to the 512 px one
  const urls = [`/static/bg/${p.url}`];
  if (window.matchMedia('(min-width: 900px)').matches) urls.unshift(`/static/bg-hd/${p.url}`);
  let url = null;
  for (const u of urls) {
    try {
      const img = new Image();
      img.src = u;
      await img.decode();
      url = u;
      break;
    } catch (e) { /* try the next */ }
  }
  if (!url) return;
  if (seq !== bgSeq) return; // a newer plan arrived while decoding
  back.style.backgroundImage = `url("${url}")`;
  back.classList.add('on');
  front.classList.remove('on');
  bgFront = back.id;
}

// ---- scenes

async function applyScene(id) {
  try {
    const r = await api('POST', '/api/apply', {scene_id: id, fade_s: fade()});
    if (r.pending) toast('Generating music for this scene; it will fade in when ready.');
  } catch (e) { toast(e.message); }
}

function openEditor(mode, scene) {
  editing = {mode, id: scene ? scene.id : null};
  const f = $('#editform');
  const s = scene || {name: mode === 'custom' ? 'Custom' : '', location: vocab.locations[0], mood: vocab.moods[0],
    tempo: 'medium', choir: false, start: 'build', extra: '', prompt: ''};
  $('#edittitle').textContent = mode === 'custom' ? 'Custom (not saved)'
    : mode === 'new' ? (s.saved === false ? 'New scene (from text)' : 'New scene') : 'Edit scene';
  f.name.value = s.name;
  f.location.innerHTML = options(vocab.locations, s.location, cap);
  f.mood.innerHTML = options(vocab.moods, s.mood, cap);
  f.tempo.innerHTML = options(vocab.tempos, s.tempo, (t) => TEMPO_LABELS[t] || t);
  f.choir.checked = !!s.choir;
  f.querySelectorAll('input[name=start]').forEach((i) => { i.checked = i.value === s.start; });
  f.extra.value = s.extra;
  f.prompt.value = s.prompt || '';
  renderIconPick(s.icon || '');
  const raw = !!s.prompt;   // advanced-mode scene: the prompt box replaces the pre-baked options
  $('#promptrow').hidden = !raw;
  f.querySelectorAll('.vocabfield').forEach((e) => { e.hidden = raw; });
  $('#savebtn').textContent = mode === 'custom' ? 'Save as scene' : 'Save';
  $('#applybtn').textContent = mode === 'custom' ? 'Play now' : 'Save & queue';
  $('#delbtn').hidden = mode !== 'edit';
  $('#editerr').textContent = '';
  $('#editor').showModal();
}

async function describeScene(ev) {
  ev.preventDefault();
  const adv = $('#advmode').checked;
  const box = adv ? $('#rawprompt') : $('#descr');
  const text = box.value.trim();
  if (!text) { toast(adv ? 'Write the prompt first.' : 'Describe the scene first.'); return; }
  const btn = $('#describego');
  btn.disabled = true;
  try {
    if (adv) {
      if (/<[^<>]*>/.test(text)) { toast('Replace the <tags> with your own words first.'); return; }
      openEditor('new', {name: '', location: vocab.locations[0], mood: vocab.moods[0], tempo: 'medium', choir: false,
        start: 'build', extra: '', prompt: text, saved: false});
      box.value = '';   // the prompt now lives in the editor: close advanced mode again
      $('#advmode').checked = false;
      setAdvanced();
      return;
    }
    const draft = await api('POST', '/api/scene-from-text', {text});
    openEditor('new', {...draft, saved: false});
    box.value = '';
  } catch (e) {
    toast(e.message);
  } finally {
    btn.disabled = false;
  }
}

const ADV_TEMPLATE = '<Genre or style> music for <place or situation>. <Mood or emotion>, <tempo: slow, medium or fast>. '
  + '<Lead instrument or orchestra section> plays <melody or rhythm>, with <supporting instrument or section> underneath. '
  + '<Optional: choir or vocal style>. <Sound character, e.g. close-miked or a spacious hall>. Instrumental.';

// icon + background picker in the editor: "Auto" or one of the starter scenes' icons
function renderIconPick(current) {
  const icons = (bgManifest && bgManifest.icons) || {};
  const slugs = Object.keys(icons).sort();
  $('#iconrow').hidden = !slugs.length;
  const item = (slug, label, img) => `<label title="${esc(label)}"><input type="radio" name="icon" value="${esc(slug)}"${slug === current ? ' checked' : ''}>
    ${img ? `<img src="/static/bg/${esc(img)}" alt="${esc(label)}" width="48" height="48" loading="lazy">` : '<span class="auto">Auto</span>'}
    <span class="cap">${esc(label)}</span></label>`;
  $('#iconpick').innerHTML = item('', 'Automatic', null) + slugs.map((s) => item(s, cap(s.replace(/-/g, ' ')), icons[s])).join('');
}

function setAdvanced() {
  const adv = $('#advmode').checked;
  $('#descr').hidden = adv;
  $('#rawprompt').hidden = !adv;
  $('#rawhelp').hidden = !adv;
  $('#descrlabel').textContent = adv ? 'Write the whole prompt' : 'Describe a scene';
  $('#describego').textContent = adv ? 'Next' : 'Describe';
  if (adv && !$('#rawprompt').value.trim()) $('#rawprompt').value = ADV_TEMPLATE;
  $('#describehint').textContent = adv
    ? 'The music prompt is sent as written, at most 70 words, so say "Instrumental" if you want no singing. Replace every <tag> with your own words, then name and save it on the next screen.'
    : 'Keep it short: a place and a mood, about 9 words. Only the first words are used in the music prompt; the rest is cut.';
}

function formScene() {
  const f = $('#editform');
  return {
    name: f.name.value.trim(),
    location: f.location.value,
    mood: f.mood.value,
    tempo: f.tempo.value,
    choir: f.choir.checked,
    start: (f.querySelector('input[name=start]:checked') || {value: 'build'}).value,
    extra: f.extra.value.trim(),
    prompt: f.prompt.value.trim(),
    icon: (f.querySelector('input[name=icon]:checked') || {value: ''}).value,
  };
}

async function onEditorSubmit(ev) {
  ev.preventDefault();
  const action = ev.submitter ? ev.submitter.value : 'save';
  const scene = formScene();
  try {
    if (action === 'delete') {
      if (!confirm(`Delete "${scene.name}"? Its music stays in the library.`)) return;
      await api('DELETE', `/api/scenes/${editing.id}`);
    } else if (action === 'apply' && editing.mode === 'custom') {
      const r = await api('POST', '/api/apply', {scene, fade_s: fade()});
      if (r.pending) toast('Generating custom music; it will fade in when ready.');
    } else {
      let saved;
      if (editing.mode === 'edit') saved = await api('PUT', `/api/scenes/${editing.id}`, scene);
      else saved = await api('POST', '/api/scenes', scene);
      if (action === 'apply') await api('PUT', '/api/queue', {ids: [...(state.queue || []).map((s) => s.id), saved.id]});
    }
    $('#editor').close();
  } catch (e) {
    $('#editerr').textContent = e.message;
  }
}

// ---- cast

async function loadDevices() {
  const box = $('#devices');
  try {
    const devices = await api('GET', '/api/cast/devices');
    const targets = new Set(state ? state.cast.targets.map((t) => t.uuid) : []);
    box.innerHTML = devices.length ? devices.map((d) => `
      <label class="device"><input type="checkbox" value="${esc(d.uuid)}"${targets.has(d.uuid) ? ' checked' : ''}>
        <span>${esc(d.name)}</span><small>${d.is_group ? 'group' : esc(d.model)}</small></label>`).join('')
      : '<p class="muted">No Cast devices found yet. Check the speakers are on and press Refresh.</p>';
  } catch (e) { box.innerHTML = `<p class="err">${esc(e.message)}</p>`; }
}

async function castStart() {
  const targets = [...document.querySelectorAll('#devices input:checked')].map((i) => i.value);
  if (!targets.length) { toast('Pick at least one speaker.'); return; }
  try { await api('POST', '/api/cast', {targets}); } catch (e) { toast(e.message); }
}

// ---- Runpod settings (the key is write-only: the server never sends it back)

const RP_NUM = {warn_below: '#rp-warn', floor: '#rp-floor', daily_cap: '#rp-cap', budget_usd: '#rp-budget'};

async function loadRunpod() {
  try {
    const c = await api('GET', '/api/runpod');
    $('#rp-endpoint').value = c.endpoint_id || '';
    $('#runpod-key').value = '';
    $('#runpod-key').placeholder = c.key_set ? 'Saved' : 'Runpod API key';
    for (const [k, sel] of Object.entries(RP_NUM)) $(sel).value = c[k] === null || c[k] === undefined ? '' : c[k];
  } catch (e) { toast(e.message); }
}

async function saveRunpod() {
  const body = {api_key: $('#runpod-key').value.trim()};
  const ep = $('#rp-endpoint').value.trim();
  if (ep) body.endpoint_id = ep;
  for (const [k, sel] of Object.entries(RP_NUM)) {
    const v = $(sel).value.trim();
    if (v !== '') body[k] = Number(v);
    else if (k === 'daily_cap' || k === 'budget_usd') body[k] = null;   // empty clears the limit
  }
  try {
    await api('PUT', '/api/runpod', body);
    $('#runpod-key').value = '';
    toast('Runpod settings saved.');
    await loadRunpod();
    await refresh();
  } catch (e) { toast(e.message); }
}

async function testRunpod() {
  try {
    const r = await api('POST', '/api/runpod/test');
    toast(r.ok ? `Runpod answers (${r.state}); balance ${r.balance === null ? 'unknown' : '$' + r.balance.toFixed(2)}.` : r.error);
  } catch (e) { toast(e.message); }
}

async function resetRunpod() {
  try { await api('POST', '/api/runpod/reset'); toast('Runpod errors cleared.'); await refresh(); } catch (e) { toast(e.message); }
}

// ---- wiring

function showTab(name) {
  document.querySelectorAll('.tab').forEach((t) => { t.hidden = t.id !== `tab-${name}`; });
  document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  store('tab', name);
  if (name === 'settings') { loadDevices(); loadRunpod(); }
  if (name === 'library') loadLibrary();
}

function connect() {
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  ws.onmessage = (ev) => { state = JSON.parse(ev.data); render(); };
  ws.onclose = () => setTimeout(connect, 2000);
}

async function init() {
  vocab = await api('GET', '/api/vocab');
  const savedFade = recall('fade');
  if (savedFade !== null) $('#fade').value = savedFade;
  $('#fadeval').textContent = fade();
  $('#fade').addEventListener('input', () => { $('#fadeval').textContent = fade(); store('fade', fade()); });
  $('#fadeout').addEventListener('click', () => api('POST', '/api/fadeout', {fade_s: fade()}).catch((e) => toast(e.message)));
  $('#renderctl').addEventListener('click', () =>
    api('POST', state && state.paused ? '/api/start' : '/api/stop', {fade_s: fade()}).then(refresh).catch((e) => toast(e.message)));
  $('#skip').addEventListener('click', () => api('POST', '/api/skip')
    .then((r) => { if (r && r.rendering) toast('Rendering one new track for this scene'); })
    .catch((e) => toast(e.message)));
  const listen = $('#listen'), player = $('#player');
  const listening = (on) => { listen.setAttribute('aria-pressed', String(on)); listen.textContent = on ? 'Stop listening' : 'Listen here'; castBanner(); };
  let connecting = false;
  const stopListening = () => { player.pause(); player.removeAttribute('src'); player.load(); listening(false); };
  listen.addEventListener('click', async () => {
    if (connecting) return;   // a second tap while the stream connects used to abort play() and show an error
    if (!player.paused || player.getAttribute('src')) { stopListening(); return; }
    connecting = true; listen.textContent = 'Connecting…';
    player.src = '/stream.mp3';   // fresh connection: joins the live stream, not a stale buffer
    try { await player.play(); listening(true); }
    catch (e) {
      player.removeAttribute('src'); listening(false);
      if (e.name !== 'AbortError') toast('Could not start the stream: ' + e.message);
    } finally { connecting = false; }
  });
  // The phone (or a media key, a call, Bluetooth) can pause or end the stream behind our back: reset so the next tap starts clean.
  const dropped = () => { if (!connecting && player.getAttribute('src')) { player.removeAttribute('src'); listening(false); } };
  player.addEventListener('pause', dropped);
  player.addEventListener('ended', dropped);
  player.addEventListener('error', () => { if (!connecting && player.getAttribute('src')) { player.removeAttribute('src'); listening(false); toast('The stream stopped.'); } });
  const vol = $('#volume');
  vol.addEventListener('pointerdown', () => { volumeBusy = true; });
  vol.addEventListener('change', async () => {
    try { await api('POST', '/api/volume', {volume: Number(vol.value)}); } catch (e) { toast(e.message); }
    volumeBusy = false;
  });
  vol.addEventListener('pointerup', () => { volumeBusy = false; });
  vol.addEventListener('pointercancel', () => { volumeBusy = false; });
  const qs = $('#quality');
  qs.addEventListener('pointerdown', () => { qualityBusy = true; });
  qs.addEventListener('input', () => { qualityBusy = true; $('#qualitylabel').textContent = QUALITY_LABELS[qs.value]; });
  qs.addEventListener('pointerup', () => { if (Number(qs.value) === state.quality) qualityBusy = false; });
  qs.addEventListener('pointercancel', () => { if (Number(qs.value) === state.quality) qualityBusy = false; });
  qs.addEventListener('change', onQualityChange);
  $('#favbtn').addEventListener('click', () => rateNow(state.now_playing && state.now_playing.rating === 'fav' ? 'normal' : 'fav'));
  $('#banbtn').addEventListener('click', () => rateNow('banned'));
  $('#tab-library').addEventListener('click', onLibraryClick);
  $('#cleanup').addEventListener('click', onCleanup);
  $('#rp-save').addEventListener('click', saveRunpod);
  $('#rp-test').addEventListener('click', testRunpod);
  $('#rp-reset').addEventListener('click', resetRunpod);
  $('#importfile').addEventListener('change', onImport);
  $('#players').addEventListener('change', async (ev) => {
    const n = Math.max(0, Math.min(20, Math.round(Number(ev.target.value) || 0)));
    try { await api('PUT', '/api/players', {count: n}); } catch (e) { toast(e.message); }
    ev.target.value = n;
  });
  const cap = $('#libcap');
  cap.addEventListener('input', () => { capBusy = true; $('#libcaplabel').textContent = `${cap.value} GB (about ${Math.round(cap.value * 1024 / 3.4)} tracks)`; });
  cap.addEventListener('change', async () => {
    try { await api('PUT', '/api/settings', {library_cap_gb: Number(cap.value)}); } catch (e) { toast(e.message); }
    capBusy = false;
  });
  $('#scenesearch').addEventListener('input', () => { if (state) renderScenes(); });
  $('#scenes').addEventListener('click', (ev) => {
    const b = ev.target.closest('button');
    if (!b) return;
    if (b.dataset.queue) putQueue([...(state.queue || []).map((s) => s.id), Number(b.dataset.queue)]);
    if (b.dataset.apply) applyScene(Number(b.dataset.apply));
    if (b.dataset.edit) openEditor('edit', state.scenes.find((s) => s.id === Number(b.dataset.edit)));
  });
  $('#nextscene').addEventListener('click', nextScene);
  $('#nextscene2').addEventListener('click', nextScene);
  $('#queue').addEventListener('click', (ev) => {
    const b = ev.target.closest('button');
    if (!b || b.disabled) return;
    const ids = (state.queue || []).map((s) => s.id);
    const d = b.dataset;
    if (d.qdel !== undefined) ids.splice(Number(d.qdel), 1);
    else if (d.qup !== undefined) { const i = Number(d.qup); [ids[i - 1], ids[i]] = [ids[i], ids[i - 1]]; }
    else if (d.qdown !== undefined) { const i = Number(d.qdown); [ids[i + 1], ids[i]] = [ids[i], ids[i + 1]]; }
    else return;
    putQueue(ids);
  });
  let dragFrom = null;
  const qcard = (ev) => ev.target.closest && ev.target.closest('.qcard');
  const qclear = () => document.querySelectorAll('#queue .dragover').forEach((c) => c.classList.remove('dragover'));
  $('#queue').addEventListener('dragstart', (ev) => {
    const c = qcard(ev);
    if (!c) return;
    dragFrom = Number(c.dataset.qi);
    ev.dataTransfer.effectAllowed = 'move';
    ev.dataTransfer.setData('text/plain', String(dragFrom));
    c.classList.add('dragging');
  });
  $('#queue').addEventListener('dragover', (ev) => {
    const c = qcard(ev);
    if (dragFrom === null || !c) return;
    ev.preventDefault();
    qclear();
    c.classList.add('dragover');
  });
  $('#queue').addEventListener('drop', (ev) => {
    const c = qcard(ev);
    if (dragFrom === null || !c) return;
    ev.preventDefault();
    const to = Number(c.dataset.qi), from = dragFrom;
    dragFrom = null;
    qclear();
    if (to === from) return;
    const ids = (state.queue || []).map((s) => s.id);
    ids.splice(to, 0, ids.splice(from, 1)[0]);
    putQueue(ids);
  });
  $('#queue').addEventListener('dragend', () => {
    dragFrom = null;
    qclear();
    document.querySelectorAll('#queue .dragging').forEach((c) => c.classList.remove('dragging'));
  });
  $('#advmode').addEventListener('change', setAdvanced);
  $('#newscene').addEventListener('click', () => openEditor('new'));
  $('#custom').addEventListener('click', () => openEditor('custom'));
  $('#describe').addEventListener('submit', describeScene);
  $('#editform').addEventListener('submit', onEditorSubmit);
  $('#cancelbtn').addEventListener('click', () => $('#editor').close());
  $('#refresh').addEventListener('click', loadDevices);
  $('#caststart').addEventListener('click', castStart);
  $('#caststop').addEventListener('click', () => api('POST', '/api/cast/stop').catch((e) => toast(e.message)));
  document.querySelectorAll('#tabs button').forEach((b) => b.addEventListener('click', () => showTab(b.dataset.tab)));
  showTab(recall('tab') || 'scenes');
  initBackground();
  connect();
}

init().catch((e) => toast(`Could not start: ${e.message}`));
