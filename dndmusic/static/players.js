'use strict';
const $ = (s) => document.querySelector(s);
let state = null, busy = false;

function token() {
  let t = null;
  try { t = localStorage.getItem('player-token'); } catch (e) { /* private mode */ }
  if (!t) {
    const b = crypto.getRandomValues(new Uint8Array(16));
    t = [...b].map((x) => x.toString(16).padStart(2, '0')).join('');
    try { localStorage.setItem('player-token', t); } catch (e) { /* kept for this page load only */ }
  }
  return t;
}
const TOKEN = token();

function render() {
  const s = state;
  const on = s && s.voting && s.track_id !== null;
  $('#track').textContent = s && s.name ? s.name : 'Nothing playing';
  $('#status').textContent = !s ? 'Connecting…'
    : !s.voting ? 'Voting is off. Ask the DM to set the number of players.'
    : s.track_id === null ? 'Waiting for music.'
    : s.skipping ? 'Skipping this track…'
    : s.my_vote === 'like' ? 'You liked this track.'
    : s.my_vote === 'dislike' ? 'You disliked this track.'
    : 'How is this track?';
  for (const v of ['like', 'dislike']) {
    const b = $(`#${v}`);
    b.disabled = !on || busy;
    b.classList.toggle('on', !!s && s.my_vote === v);
    b.setAttribute('aria-pressed', String(!!s && s.my_vote === v));
  }
  $('#tally').textContent = on
    ? `Likes ${s.likes} · Dislikes ${s.dislikes} of ${s.needed} needed to skip`
    : '';
}

async function refresh() {
  try {
    const r = await fetch(`/api/players/state?token=${TOKEN}`);
    state = await r.json();
    $('#err').textContent = '';
  } catch (e) { $('#err').textContent = 'Lost contact with the music server.'; }
  render();
}

async function vote(v) {
  if (!state || busy) return;
  const vote = state.my_vote === v ? 'none' : v;   // pressing your vote again takes it back
  busy = true;
  render();
  try {
    const r = await fetch('/api/players/vote', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token: TOKEN, track_id: state.track_id, vote})});
    const body = await r.json();
    if (r.status === 409) { $('#err').textContent = 'That track just changed.'; await refresh(); }
    else if (!r.ok) $('#err').textContent = body.detail || 'Vote failed.';
    else { state = body; $('#err').textContent = ''; }
  } catch (e) { $('#err').textContent = 'Lost contact with the music server.'; }
  busy = false;
  render();
}

$('#like').addEventListener('click', () => vote('like'));
$('#dislike').addEventListener('click', () => vote('dislike'));
refresh();
setInterval(refresh, 2000);
