// Scene background selection: pure logic, no DOM. Paths in the manifest are relative to /static/bg/.

export function slug(name) {
  return String(name || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
}

export function setFor(manifest, sceneName, art) {
  const sets = manifest && manifest.sets;
  const own = sets && sets[slug(sceneName)];
  if (Array.isArray(own) && own.length) return own;
  const byArt = art && sets && Object.prototype.hasOwnProperty.call(sets, art) ? sets[art] : null;
  if (Array.isArray(byArt) && byArt.length) return byArt;
  return (manifest && Array.isArray(manifest.generic)) ? manifest.generic : [];
}

// Icon path (relative to /static/bg/) for a scene name, or null: caller shows the emblem.
export function iconFor(manifest, sceneName) {
  const icons = manifest && manifest.icons;
  const s = slug(sceneName);
  if (!icons || !s || s === 'custom') return null;
  return Object.prototype.hasOwnProperty.call(icons, s) && icons[s] ? icons[s] : null;
}

export function pick(set, current, rand = Math.random) {
  if (!set.length) return null;
  const options = set.length > 1 ? set.filter((u) => u !== current) : set;
  return options[Math.min(options.length - 1, Math.floor(rand() * options.length))];
}

// prev: {key, url} of the last applied plan (or null). Returns null = no change,
// {url: null, key: null} = plain ground, {url, key} = fade to url.
export function plan(prev, state, manifest, rand = Math.random) {
  const np = state && state.now_playing;
  const prevUrl = prev ? prev.url : null;
  if (!np) return prevUrl === null ? null : { url: null, key: null };
  const key = `${np.pool_id}:${np.track_id}`;
  if (prev && prev.key === key) return null;
  const url = pick(setFor(manifest, np.icon || np.name, np.art), prevUrl, rand);
  return { url, key };
}
