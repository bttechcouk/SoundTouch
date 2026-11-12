// ── Spotify tab ──────────────────────────────────────────────────────────────
// Browse a linked Spotify account and play on the speakers. All Spotify calls
// go through the controller (/api/spotify/*), which holds the tokens, logs the
// speaker into Spotify and starts playback; see SpotifyClient in
// soundtouch_controller.py. Uses app.js globals: speakers, activeHost,
// lastState, toast, pollNow, switchTab, toggleSection.

const SP = {
  loaded: false, clientIdSet: false, accounts: [], account: null,
  speakerAccounts: {},          // host → Spotify accounts linked on that speaker
  view: 'home', stack: [],      // 'home' | 'search' | 'detail'
  home: null, homeLoading: false, showAllPlaylists: false,
  q: '', type: '', results: null, searchTimer: null, searchSeq: 0,
  detail: null,
};

function spEsc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
async function spApi(action, params = {}) {
  const r = await fetch('/api/spotify/' + action + '?' + new URLSearchParams(params));
  return r.json();
}
function spAcct() { return SP.account ? {account: SP.account} : {}; }
function spAccountName(uid) { return (SP.accounts.find(a => a.user_id === uid) || {}).display_name || uid; }
function spCanPlay(host) { return (SP.speakerAccounts[host] || []).includes(SP.account); }

// Placeholder cover when Spotify gives no artwork (Liked Songs, some mixes)
function spCover(card, cls = '') {
  if (card.image) return `<img class="sp-cover ${cls}" src="${spEsc(card.image)}" alt="" loading="lazy">`;
  const liked = card.type === 'collection';
  return `<div class="sp-cover sp-cover-ph ${liked ? 'liked' : ''} ${cls}">${liked ? '♥' : '♫'}</div>`;
}

// ── loading ──────────────────────────────────────────────────────────────────
async function spLoadAccounts() {
  try {
    const d = await spApi('accounts');
    SP.clientIdSet = d.client_id_set;
    SP.accounts = d.accounts || [];
    SP.speakerAccounts = Object.fromEntries((d.speakers || []).map(s => [s.host, s.accounts]));
    let saved = null; try { saved = localStorage.getItem('spAccount'); } catch (e) {}
    SP.account = SP.accounts.some(a => a.user_id === saved) ? saved : (SP.accounts[0] || {}).user_id || null;
    SP.loaded = true;
  } catch (e) { toast('Could not reach the controller'); }
}

async function spOpen() {
  if (!SP.loaded) await spLoadAccounts();
  spRender();
  if (SP.account && !SP.home && !SP.homeLoading) spLoadHome();
}

async function spLoadHome() {
  SP.homeLoading = true; spRender();
  const d = await spApi('home', spAcct()).catch(() => ({ok: false, error: 'Could not reach the controller'}));
  SP.homeLoading = false;
  if (!d.ok) { toast(d.error || 'Spotify unavailable'); SP.home = {error: d.error}; }
  else SP.home = d;
  spRender();
}

function spSwitchAccount(uid) {
  if (uid === SP.account) return;
  SP.account = uid; try { localStorage.setItem('spAccount', uid); } catch (e) {}
  SP.home = null; SP.results = null; SP.detail = null; SP.view = 'home'; SP.stack = [];
  spLoadHome();
}

// ── navigation ───────────────────────────────────────────────────────────────
function spGo(view) { SP.stack.push(SP.view); SP.view = view; spRender(); document.getElementById('page-spotify').scrollTop = 0; }
function spBack() { SP.view = SP.stack.pop() || 'home'; spRender(); }

async function spOpenItem(uri) {
  SP.detail = {loading: true, card: {name: '…'}};
  spGo('detail');
  const d = await spApi('item', {uri, ...spAcct()});
  if (!d.ok) { toast(d.error || 'Could not open that'); spBack(); return; }
  SP.detail = d; spRender();
}

function spOnSearchInput(v) {
  SP.q = v;
  clearTimeout(SP.searchTimer);
  if (v.trim().length < 2) { if (SP.view === 'search') { SP.results = null; spRenderBody(); } return; }
  SP.searchTimer = setTimeout(spSearch, 350);
}
async function spSearch() {
  const q = SP.q.trim(); if (q.length < 2) return;
  if (SP.view !== 'search') { SP.stack = ['home']; SP.view = 'search'; spSyncSearchBox(); }
  const seq = ++SP.searchSeq;
  const d = await spApi('search', {q, type: SP.type, ...spAcct()});
  if (seq !== SP.searchSeq) return;           // a newer search has started
  if (!d.ok) { toast(d.error || 'Search failed'); return; }
  SP.results = d; spRenderBody();
}
// Switching into search mode must not re-render the input (it would drop
// the keyboard mid-word) — just flip its state.
function spSyncSearchBox() {
  document.querySelector('.sp-search')?.classList.toggle('active', SP.view === 'search');
}
function spSetType(t) { SP.type = t; spSearch(); spRenderBody(); }
function spClearSearch() { SP.q = ''; SP.results = null; SP.view = 'home'; SP.stack = []; spRender(); }

// ── play / presets ───────────────────────────────────────────────────────────
async function spPlay(uri, hosts, offset) {
  hosts = hosts || (activeHost ? [activeHost] : []);
  if (!hosts.length) { toast('Pick a speaker first'); return; }
  const blocked = hosts.filter(h => !spCanPlay(h));
  if (blocked.length) {
    const n = (speakers.find(s => s.host === blocked[0]) || {}).name || blocked[0];
    toast(`${n} isn't linked to ${spAccountName(SP.account)}'s Spotify`); return;
  }
  const names = hosts.map(h => (speakers.find(s => s.host === h) || {}).name || h);
  toast(`Starting on ${names.join(' + ')}…`);
  if (navigator.vibrate) navigator.vibrate(8);
  const p = {uri, hosts: hosts.join(','), ...spAcct()};
  if (offset !== undefined && offset !== null && offset !== '') p.offset = offset;
  const d = await spApi('play', p).catch(() => ({ok: false, error: 'Could not reach the controller'}));
  toast(d.ok ? `Playing on ${d.playing_on.join(' + ')}` : (d.error || "Couldn't start playback"));
  if (d.ok) setTimeout(pollNow, 800);
}

function spPlayTrack(i) {
  const d = SP.detail; if (!d) return;
  const t = d.tracks[i]; const ctx = d.card;
  if (ctx.type === 'artist') spPlay(t.album_uri || t.uri, null, t.album_uri ? t.uri : undefined);
  else if (ctx.type === 'collection') spPlay(ctx.uri, null, String(t.position));
  else if (ctx.type === 'track') spPlay(t.uri);
  else spPlay(ctx.uri, null, t.uri);
}

// Bottom sheet: pick speakers, or a preset slot
function spSheet(html) {
  spCloseSheet();
  const el = document.createElement('div');
  el.id = 'sp-sheet';
  el.innerHTML = `<div class="sp-sheet-backdrop" onclick="spCloseSheet()"></div>
    <div class="sp-sheet"><div class="sp-grab"></div>${html}</div>`;
  document.body.appendChild(el);
}
function spCloseSheet() { document.getElementById('sp-sheet')?.remove(); }

function spSpeakerSheet() {
  const c = SP.detail.card;
  const rows = [...speakers].sort((a, b) => a.name.localeCompare(b.name)).map(s => {
    const ok = spCanPlay(s.host);
    return `<label class="sp-spk${ok ? '' : ' off'}">
      <input type="checkbox" value="${spEsc(s.host)}" ${s.host === activeHost && ok ? 'checked' : ''} ${ok ? '' : 'disabled'}
             onchange="spSheetCount()">
      <span class="sp-box"></span><span class="sp-spk-name">${spEsc(s.name)}</span>
      <span class="sp-spk-model">${ok ? spEsc((s.model || '').replace(/^SoundTouch\s*/, 'ST ')) : 'No Spotify account'}</span>
    </label>`;
  }).join('');
  spSheet(`<h3>Play “${spEsc(c.name)}”</h3>
    <div class="sp-sheet-sub">Playing as <b>${spEsc(spAccountName(SP.account))}</b> · pick one speaker, or several to play in sync</div>
    <div class="sp-spk-list">${rows}</div>
    <button class="sp-btn-play" id="sp-sheet-go" onclick="spSheetPlay()">▶ Play</button>`);
  spSheetCount();
}
function spSheetHosts() { return [...document.querySelectorAll('#sp-sheet input:checked')].map(i => i.value); }
function spSheetCount() {
  const n = spSheetHosts().length, b = document.getElementById('sp-sheet-go');
  if (b) { b.disabled = !n; b.textContent = n > 1 ? `▶ Play on ${n} speakers` : '▶ Play'; }
}
function spSheetPlay() {
  const hosts = spSheetHosts(); if (!hosts.length) return;
  // the active speaker leads the group if it's ticked
  hosts.sort((a, b) => (b === activeHost) - (a === activeHost));
  spCloseSheet(); spPlay(SP.detail.card.uri, hosts);
}

function spPresetSheet() {
  const c = SP.detail.card;
  if (!activeHost) { toast('Pick a speaker first'); return; }
  const presets = (lastState && lastState.presets) || [];
  const tiles = [1, 2, 3, 4, 5, 6].map(n => {
    const p = presets.find(x => String(x.id) === String(n)) || {};
    return `<button class="sp-slot" onclick="spSavePreset(${n}, ${JSON.stringify(p.name || '').replace(/"/g, '&quot;')})">
      <b>${n}</b><span>${spEsc(p.name || 'Empty')}</span></button>`;
  }).join('');
  const spkName = (speakers.find(s => s.host === activeHost) || {}).name || activeHost;
  spSheet(`<h3>Save “${spEsc(c.name)}” to a preset</h3>
    <div class="sp-sheet-sub">On ${spEsc(spkName)} — tap a slot</div>
    <div class="sp-slots">${tiles}</div>
    <label class="sw-pill" style="margin-top:12px;display:inline-flex"><span>All speakers</span>
      <input type="checkbox" id="sp-preset-all"><span class="sw-track"></span></label>`);
}
async function spSavePreset(slot, currentName) {
  const all = document.getElementById('sp-preset-all')?.checked;
  if (currentName && !confirm(`Replace preset ${slot} (${currentName})${all ? ' on every speaker that can play it' : ''}?`)) return;
  const c = SP.detail.card;
  const d = await spApi('save-preset', {host: activeHost, slot, uri: c.uri, name: c.name,
                                        image: c.image || '', all: !!all, ...spAcct()});
  spCloseSheet();
  if (!d.ok) { toast(d.error || 'Save failed'); return; }
  const res = Object.entries(d.results || {});
  const saved = res.filter(([, v]) => v === 'saved').length;
  const missed = res.filter(([, v]) => v !== 'saved').map(([n]) => n);
  toast(all ? `Saved to preset ${slot} on ${saved} speaker${saved === 1 ? '' : 's'}${missed.length ? ` · not ${missed.join(', ')}` : ''}`
            : `Saved to preset ${slot}`);
  setTimeout(pollNow, 400);
}

// ── rendering ────────────────────────────────────────────────────────────────
function spRender() {
  const root = document.getElementById('sp-root'); if (!root) return;
  if (!SP.loaded) { root.innerHTML = '<p class="sp-empty">Loading…</p>'; return; }
  if (!SP.clientIdSet || !SP.accounts.length) {
    root.innerHTML = `<div class="sp-empty-card">
      <div class="sp-empty-icon">♫</div>
      <b>Link your Spotify account</b>
      <p>Browse your playlists, library and search, and play them on any speaker.</p>
      <button class="sp-btn-play" onclick="spGoSettings()">Set up in Settings</button></div>`;
    return;
  }
  const pills = SP.accounts.map(a => `<button class="sp-acct-pill${a.user_id === SP.account ? ' on' : ''}"
      onclick="spSwitchAccount('${spEsc(a.user_id)}')"><span class="sp-avatar">${spEsc((a.display_name || '?')[0].toUpperCase())}</span>${spEsc(a.display_name)}</button>`).join('');
  const searching = SP.view === 'search';
  root.innerHTML = `
    ${SP.view === 'detail' ? '' : `<div class="sp-top">
      <div class="sp-accts">${pills}</div><div class="sp-powered">Content from Spotify</div></div>
      <div class="sp-search${searching ? ' active' : ''}">
        <span>⌕</span><input id="sp-q" type="search" placeholder="Search songs, albums, artists, playlists"
          value="${spEsc(SP.q)}" oninput="spOnSearchInput(this.value)" onkeydown="if(event.key==='Enter'){this.blur();spSearch()}"
          autocomplete="off" autocapitalize="off" spellcheck="false">
        <button class="sp-x" onclick="spClearSearch()">✕</button></div>`}
    <div id="sp-body"></div>`;
  spRenderBody();
}

function spRenderBody() {
  const el = document.getElementById('sp-body'); if (!el) return;
  if (SP.view === 'search') el.innerHTML = spSearchHtml();
  else if (SP.view === 'detail') el.innerHTML = spDetailHtml();
  else el.innerHTML = spHomeHtml();
}

function spTile(c) {
  return `<div class="sp-tile" onclick="spOpenItem('${spEsc(c.uri)}')">${spCover(c)}
    <div class="sp-t">${spEsc(c.name)}</div><div class="sp-s">${spEsc(c.sub)}</div></div>`;
}

function spHomeHtml() {
  const h = SP.home;
  if (!h || SP.homeLoading) return '<p class="sp-empty">Loading your Spotify…</p>';
  if (h.error) return `<p class="sp-empty">${spEsc(h.error)}<br><button class="mc-btn" onclick="spLoadHome()">Try again</button></p>`;
  const lib = [h.liked, ...h.playlists];
  const shown = SP.showAllPlaylists ? lib : lib.slice(0, 18);
  return `
    ${h.recent.length ? `<div class="sp-h">Jump back in</div><div class="sp-row">${h.recent.map(spTile).join('')}</div>` : ''}
    <div class="sp-h">Your library</div>
    <div class="sp-grid">${shown.map(spTile).join('')}</div>
    ${lib.length > shown.length ? `<button class="sp-more" onclick="SP.showAllPlaylists=true;spRenderBody()">Show all ${lib.length - 1} playlists</button>` : ''}
    ${h.albums.length ? `<div class="sp-h">Saved albums</div><div class="sp-grid">${h.albums.map(spTile).join('')}</div>` : ''}`;
}

function spSearchHtml() {
  const chips = [['', 'All'], ['playlist', 'Playlists'], ['album', 'Albums'], ['artist', 'Artists'], ['track', 'Songs']]
    .map(([t, l]) => `<button class="sp-chip${SP.type === t ? ' on' : ''}" onclick="spSetType('${t}')">${l}</button>`).join('');
  const r = SP.results;
  if (!r) return `<div class="sp-chips">${chips}</div><p class="sp-empty">${SP.q.trim().length < 2 ? 'Type at least two letters' : 'Searching…'}</p>`;
  // Songs, artists and albums first: Spotify's playlist search is weak
  const order = SP.type ? [SP.type + 's'] : ['artists', 'albums', 'tracks', 'playlists'];
  const rows = order.flatMap(k => (r[k] || []).slice(0, SP.type ? 20 : (k === 'tracks' ? 5 : 3))).map(c => {
    const label = {artist: 'Artist', album: 'Album', track: 'Song', playlist: 'Playlist'}[c.type] || '';
    const open = c.type === 'track' ? `spPlay('${spEsc(c.album_uri || c.uri)}', null, ${c.album_uri ? `'${spEsc(c.uri)}'` : 'undefined'})`
                                    : `spOpenItem('${spEsc(c.uri)}')`;
    const action = c.type === 'artist' ? '<span class="sp-go">›</span>'
      : `<button class="sp-playbtn" onclick="event.stopPropagation();${c.type === 'track' ? open : `spPlay('${spEsc(c.uri)}')`}">▶</button>`;
    return `<div class="sp-res" onclick="${open}">${spCover(c, c.type === 'artist' ? 'round' : '')}
      <div class="sp-res-txt"><b>${spEsc(c.name)}</b><small>${label}${c.sub && c.type !== 'artist' ? ' · ' + spEsc(c.sub) : ''}</small></div>${action}</div>`;
  }).join('');
  return `<div class="sp-chips">${chips}</div>${rows || '<p class="sp-empty">No results</p>'}`;
}

function spDetailHtml() {
  const d = SP.detail;
  if (!d || d.loading) return `<button class="sp-back" onclick="spBack()">‹ Back</button><p class="sp-empty">Loading…</p>`;
  const c = d.card;
  const kind = {playlist: 'Playlist', album: 'Album', artist: 'Artist', track: 'Song', collection: 'Your library'}[c.type] || '';
  const spkName = (speakers.find(s => s.host === activeHost) || {}).name || 'speaker';
  const tracks = (d.tracks || []).map((t, i) => `<div class="sp-trk" onclick="spPlayTrack(${i})">
      <div class="sp-n">${i + 1}</div><div class="sp-trk-txt"><b>${spEsc(t.name)}</b><small>${spEsc(t.sub)}</small></div>
      <div class="sp-d">${t.duration_ms ? Math.floor(t.duration_ms / 60000) + ':' + String(Math.floor(t.duration_ms / 1000) % 60).padStart(2, '0') : ''}</div></div>`).join('');
  const albums = (d.albums || []).length ? `<div class="sp-h">Albums</div><div class="sp-grid">${d.albums.map(spTile).join('')}</div>` : '';
  const note = c.readonly && !d.tracks.length
    ? '<p class="sp-note">Spotify doesn\'t let apps read the songs in its own playlists — but it plays fine.</p>' : '';
  return `<button class="sp-back" onclick="spBack()">‹ Back</button>
    <div class="sp-detail-head">${spCover(c, 'big' + (c.type === 'artist' ? ' round' : ''))}
      <div class="sp-meta"><small>${kind}</small><h1>${spEsc(c.name)}</h1><p>${spEsc(c.sub)}</p></div></div>
    <div class="sp-actions">
      <button class="sp-btn-play" onclick="spPlay('${spEsc(c.uri)}')">▶ Play on ${spEsc(spkName)}</button>
      <button class="sp-btn-ghost sp-caret" onclick="spSpeakerSheet()" title="Choose speakers">▾</button>
      ${c.type === 'track' ? '' : '<button class="sp-btn-ghost" onclick="spPresetSheet()">☆ Preset</button>'}
    </div>${note}${tracks}${albums}`;
}

function spGoSettings() {
  switchTab('settings');
  const sec = document.getElementById('sec-spotify');
  if (sec && sec.style.display === 'none') toggleSection('sec-spotify', 'chev-spotify');
  setTimeout(() => sec?.scrollIntoView({behavior: 'smooth', block: 'start'}), 150);
}

// ── Settings → Spotify: linked accounts + paste-back login ───────────────────
async function spLoadSettings() {
  await spLoadAccounts();
  const el = document.getElementById('sp-settings'); if (!el) return;
  if (!SP.clientIdSet) {
    el.innerHTML = '<p class="sp-set-p">No Spotify Client ID is configured on the controller (see docs/plans/spotify-browser.md → Setup).</p>';
    return;
  }
  const rows = SP.accounts.map(a => `<div class="sp-acct-row"><span class="sp-avatar">${spEsc((a.display_name || '?')[0].toUpperCase())}</span>
      <div class="sp-acct-who">${spEsc(a.display_name)}<small>${spEsc(a.user_id)}${a.product ? ' · ' + spEsc(a.product) : ''}</small></div>
      <button class="mc-btn danger" onclick="spUnlink('${spEsc(a.user_id)}')">Unlink</button></div>`).join('');
  el.innerHTML = `
    <p class="sp-set-p">Link each Spotify account (both Duo accounts) to browse and play it here.</p>
    ${rows || '<p class="sp-set-p">No accounts linked yet.</p>'}
    <div class="sp-link">
      <div class="sp-step"><span class="sp-num">1</span><div><b>Open the Spotify login</b>
        <p>Log in as the account you're linking and tap Agree. For the second Duo account, open it in a private tab so it doesn't log in as you.</p>
        <button class="mc-btn primary" onclick="spStartLogin()">Open Spotify login</button></div></div>
      <div class="sp-step"><span class="sp-num">2</span><div><b>Paste the address you land on</b>
        <p>Spotify then opens a page that won't load (127.0.0.1) — that's expected. Copy the whole address from the address bar and paste it here.</p>
        <textarea id="sp-paste" rows="3" placeholder="http://127.0.0.1:8888/api/spotify/callback?code=…"></textarea>
        <button class="mc-btn primary" onclick="spFinishLogin()">Link account</button></div></div>
    </div>
    <p class="sp-note">Anyone who can open this app on your network can browse and play from linked accounts. It can't change playlists or see payment details.</p>`;
}
async function spStartLogin() {
  const d = await spApi('login');
  if (!d.ok) { toast(d.error || 'Could not start the login'); return; }
  window.open(d.url, '_blank');
}
async function spFinishLogin() {
  const url = document.getElementById('sp-paste').value.trim();
  if (!url) { toast('Paste the address first'); return; }
  const d = await spApi('link', {url});
  if (!d.ok) { toast(d.error || 'Linking failed'); return; }
  toast(`Linked ${d.account.display_name}`);
  SP.home = null; SP.loaded = false;
  spLoadSettings();
}
async function spUnlink(uid) {
  if (!confirm(`Unlink ${spAccountName(uid)}'s Spotify from this app?`)) return;
  await spApi('unlink', {account: uid});
  SP.home = null; SP.loaded = false;
  if (SP.account === uid) SP.account = null;
  toast('Account unlinked'); spLoadSettings();
}
