import { test } from 'node:test';
import assert from 'node:assert/strict';
import { slug, setFor, iconFor, pick, plan } from '../dndmusic/static/bg.js';

const m = { sets: { 'boss-fight': ['b/1.webp', 'b/2.webp', 'b/3.webp'], solo: ['s/1.webp'], empty: [] },
            generic: ['g/1.webp', 'g/2.webp'] };
const playing = (name, track = 1, pool = 1) => ({ now_playing: { name, track_id: track, pool_id: pool } });

test('slug', () => {
  assert.equal(slug('Boss fight'), 'boss-fight');
  assert.equal(slug('  Sea -- Battle! '), 'sea-battle');
  assert.equal(slug(undefined), '');
});

test('setFor falls back to generic', () => {
  assert.deepEqual(setFor(m, 'Boss fight'), m.sets['boss-fight']);
  assert.deepEqual(setFor(m, 'Custom'), m.generic);
  assert.deepEqual(setFor(m, 'empty'), m.generic);
  assert.deepEqual(setFor(null, 'x'), []);
});

test('pick never repeats current', () => {
  const set = m.sets['boss-fight'];
  for (const r of [0, 0.3, 0.5, 0.99, 1]) assert.notEqual(pick(set, 'b/2.webp', () => r), 'b/2.webp');
  const seen = new Set([0, 0.4, 0.8].map((r) => pick(set, 'b/1.webp', () => r)));
  assert.deepEqual([...seen].sort(), ['b/2.webp', 'b/3.webp']);
});

test('pick with one element returns it; empty gives null', () => {
  assert.equal(pick(['x'], 'x', () => 0.5), 'x');
  assert.equal(pick([], null), null);
});

test('plan: nothing playing is plain ground, once', () => {
  assert.deepEqual(plan({ key: '1:1', url: 'b/1.webp' }, { now_playing: null }, m), { url: null, key: null });
  assert.equal(plan({ key: null, url: null }, { now_playing: null }, m), null);
  assert.equal(plan(null, { now_playing: null }, m), null);
});

test('plan: same track is no change, new track picks a different image', () => {
  const first = plan(null, playing('Boss fight'), m, () => 0);
  assert.equal(first.url, 'b/1.webp');
  assert.equal(plan(first, playing('Boss fight'), m), null);
  const next = plan(first, playing('Boss fight', 2), m, () => 0);
  assert.notEqual(next.url, first.url);
  assert.ok(m.sets['boss-fight'].includes(next.url));
});

test('plan: unknown scene uses generic; same pool new track also re-picks', () => {
  const p = plan(null, playing('Dragon tea party'), m, () => 0.9);
  assert.ok(m.generic.includes(p.url));
  assert.equal(plan(p, playing('Dragon tea party', 1, 2), m, () => 0).key, '2:1');
});

test('plan: missing manifest means plain ground', () => {
  assert.deepEqual(plan(null, playing('Boss fight'), { sets: {}, generic: [] }), { url: null, key: '1:1' });
});

test('iconFor: known slug, unknown, Custom, no manifest', () => {
  const mi = { icons: { 'boss-fight': 'icons/boss-fight.webp', custom: 'icons/x.webp' } };
  assert.equal(iconFor(mi, 'Boss fight'), 'icons/boss-fight.webp');
  assert.equal(iconFor(mi, 'Tavern'), null);
  assert.equal(iconFor(mi, 'Custom'), null);
  assert.equal(iconFor(mi, 'constructor'), null);
  assert.equal(iconFor(null, 'Boss fight'), null);
  assert.equal(iconFor({}, 'Boss fight'), null);
});

test('setFor: name, then art, then generic', () => {
  const mm = { ...m, sets: { ...m.sets, castle: ['c/1.webp'] } };
  assert.deepEqual(setFor(mm, 'Boss fight', 'castle'), mm.sets['boss-fight']);
  assert.deepEqual(setFor(mm, 'My battle', 'castle'), ['c/1.webp']);
  assert.deepEqual(setFor(mm, 'My battle', 'nope'), mm.generic);
  assert.deepEqual(setFor(mm, 'My battle'), mm.generic);
  assert.deepEqual(setFor(mm, 'My battle', 'empty'), mm.generic);
});

test('plan uses now_playing.art', () => {
  const st = { now_playing: { name: 'Free', art: 'solo', track_id: 1, pool_id: 1 } };
  assert.equal(plan(null, st, m).url, 's/1.webp');
});
