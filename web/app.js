// ── State ────────────────────────────────────────────────────────────────────
let speakers=[], activeHost=null, pollTimer=null, lastArt="", lastState=null;

// ── PWA install prompt ───────────────────────────────────────────────────────
let _installPrompt = null;
window.addEventListener('beforeinstallprompt', e => {
  e.preventDefault();
  _installPrompt = e;
  if (!localStorage.getItem('pwa-dismissed')) {
    document.getElementById('install-banner')?.classList.add('show');
  }
});
window.addEventListener('appinstalled', () => {
  document.getElementById('install-banner')?.classList.remove('show');
  _installPrompt = null;
});
async function installPWA() {
  if (!_installPrompt) return;
  _installPrompt.prompt();
  const { outcome } = await _installPrompt.userChoice;
  _installPrompt = null;
  document.getElementById('install-banner')?.classList.remove('show');
}
function dismissInstall() {
  document.getElementById('install-banner')?.classList.remove('show');
  localStorage.setItem('pwa-dismissed', '1');
}

// ── Boot ─────────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  fetchSpeakers(false); schedPoll();
  const savedTab = localStorage.getItem('activeTab');
  if (savedTab) switchTab(savedTab);
});

// ── Page Visibility — pause polls when tab is hidden ─────────────────────────
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    clearTimeout(pollTimer);
    clearTimeout(bgPollTimer);
  } else {
    pollNow();
    bgPollAll();
  }
});

// ── Tabs ─────────────────────────────────────────────────────────────────────
function collapseAll(pageId) {
  const page = document.getElementById(pageId);
  if (!page) return;
  page.querySelectorAll('.qr-body').forEach(b => b.style.display = 'none');
  page.querySelectorAll('.qr-chevron').forEach(c => c.classList.remove('open'));
}
const TAB_ORDER = ['player', 'manage', 'groups', 'settings'];
function switchTab(name) {
  const prev = document.querySelector('.tab.active')?.dataset?.tab;
  document.querySelectorAll('.tab').forEach(t =>
    t.classList.toggle('active', t.dataset.tab === name));
  // slide direction follows the tab order; plain fade on first paint / same tab
  const dir = prev && prev !== name
    ? (TAB_ORDER.indexOf(name) > TAB_ORDER.indexOf(prev) ? 'slide-right' : 'slide-left') : '';
  document.querySelectorAll('.page').forEach(p => {
    const vis = p.id === 'page-' + name;
    p.classList.remove('slide-left', 'slide-right');
    p.classList.toggle('visible', vis);
    if (vis && dir) p.classList.add(dir);
  });
  const ind = document.getElementById('tab-indicator');
  if (ind) ind.style.transform = `translateX(${Math.max(0, TAB_ORDER.indexOf(name)) * 100}%)`;
  closePresets();
  collapseAll('page-' + name);
  if (name === 'manage')   { /* sections load on expand */ }
  if (name === 'groups')   { loadGroups(); }
  if (name === 'settings') { /* sections load on expand */ }
  localStorage.setItem('activeTab', name);
}

// ── Speakers ─────────────────────────────────────────────────────────────────
async function fetchSpeakers(overlay) {
  if (overlay) showScanning("Scanning network…");
  try { speakers = await (await fetch('/api/speakers')).json(); } catch(e){}
  if (overlay) hideScanning();
  renderRooms();
  if (!activeHost && speakers.length) setActive(speakers[0].host);
}
async function rescan() {
  const b = document.getElementById('scan-btn');
  b.classList.add('spinning'); b.disabled = true;
  showScanning("Scanning for speakers…");
  try { speakers = await (await fetch('/api/scan')).json(); } catch(e){}
  hideScanning(); b.classList.remove('spinning'); b.disabled = false;
  renderRooms();
  if (!activeHost && speakers.length) setActive(speakers[0].host);
  toast(speakers.length ? `Found ${speakers.length} speaker${speakers.length>1?'s':''}` : 'No speakers found');
}
function renderRooms() {
  const el = document.getElementById('rooms-list');
  if (!speakers.length) { el.innerHTML='<div id="no-speakers">No speakers found</div>';
    document.getElementById('all-vol-row')?.classList.remove('visible'); return; }
  // Single speaker: full-width; 2+: 2-column grid
  el.style.gridTemplateColumns = speakers.length === 1 ? '1fr' : 'repeat(2,1fr)';
  el.innerHTML = speakers.map(s=>`
    <div class="room-chip${s.host===activeHost?' active':''}"
         id="chip-${s.host.replace(/\./g,'_')}"
         onclick="setActive('${s.host}')">
      <span class="dot"></span>
      <span class="chip-eq"><span class="chip-eq-bar"></span><span class="chip-eq-bar"></span><span class="chip-eq-bar"></span></span>
      <span class="name">${s.name}</span>${s.has_backup===false?'<span class="chip-warn" title="No preset backup">⚠</span>':''}</div>`).join('');
  updateAlarmSpeakerSelect();
}
function setActive(h) {
  activeHost=h; clearTimeout(pollTimer); renderRooms(); pollNow();
  const tab = document.querySelector('.tab.active')?.dataset?.tab;
  if (tab === 'manage') {
    const sec = document.getElementById('sec-manage-backup');
    if (sec && sec.style.display !== 'none') loadBackupInfo();
  }
  if (tab === 'groups')   loadGroups();
  if (tab === 'settings') {
    const sec = document.getElementById('sec-speaker');
    if (sec && sec.style.display !== 'none') loadSpeakerInfo();
    const secU = document.getElementById('sec-upnp-stations');
    if (secU && secU.style.display !== 'none') loadUpnpStations();
  }
}

// ── Polling ──────────────────────────────────────────────────────────────────
const speakerErrors = {};
function schedPoll() { pollTimer = setTimeout(pollNow, 3000); }
async function pollNow() {
  clearTimeout(pollTimer);
  if (!activeHost) { schedPoll(); return; }
  try {
    applyState(await (await fetch('/api/state?host='+activeHost)).json());
    speakerErrors[activeHost] = 0;
    setChipOffline(activeHost, false);
  } catch(e) {
    speakerErrors[activeHost] = (speakerErrors[activeHost]||0) + 1;
    if (speakerErrors[activeHost] >= 2) setChipOffline(activeHost, true);
  }
  schedPoll();
}
function setChipOffline(host, offline) {
  const chip = document.getElementById('chip-'+host.replace(/\./g,'_'));
  if (chip) chip.classList.toggle('offline', offline);
}

// Background poll — updates playing/offline state for all non-active speakers
// Persistently offline speakers (≥5 consecutive failures) are checked every
// 5th cycle (~60 s) instead of every cycle (~12 s) to avoid pointless traffic.
let bgPollTimer = null;
const bgPollSkip = {};  // host → cycles remaining to skip
async function bgPollAll() {
  for (const s of speakers) {
    if (s.host === activeHost) continue;
    // Back-off: skip this cycle if the counter is still running
    if (bgPollSkip[s.host] > 0) { bgPollSkip[s.host]--; continue; }
    try {
      const st = await (await fetch('/api/ping?host='+s.host)).json();
      speakerErrors[s.host] = 0;
      bgPollSkip[s.host] = 0;
      setChipOffline(s.host, !st.online);
      const chip = document.getElementById('chip-'+s.host.replace(/\./g,'_'));
      if (chip) chip.classList.toggle('playing', st.playing);
    } catch(e) {
      speakerErrors[s.host] = (speakerErrors[s.host]||0) + 1;
      if (speakerErrors[s.host] >= 2) setChipOffline(s.host, true);
      // After 5 consecutive failures start skipping 4 out of every 5 cycles
      if (speakerErrors[s.host] >= 5) bgPollSkip[s.host] = 4;
    }
  }
  bgPollTimer = setTimeout(bgPollAll, 12000);
}
setTimeout(bgPollAll, 5000); // stagger start so it doesn't clash with boot poll
function setTrackName(text) {
  const el = document.getElementById('track-name');
  if (!el) return;
  el.classList.remove('marquee');
  el.style.removeProperty('--sw');
  let span = el.querySelector('span');
  if (!span) { el.innerHTML='<span></span>'; span = el.querySelector('span'); }
  span.textContent = text;
  requestAnimationFrame(() => {
    if (span.scrollWidth > el.offsetWidth + 2) {
      el.style.setProperty('--sw', `-${span.scrollWidth - el.offsetWidth + 24}px`);
      el.classList.add('marquee');
    }
  });
}
function applyState(d) {
  if (!d) return; lastState = d;
  const track = d.track||(d.source||'—'), artist = d.artist||d.album||'';
  setTrackName(track); setText('track-artist',artist);
  const badge=document.getElementById('source-badge');
  badge.textContent=d.source||''; badge.style.display=d.source?'':'none';
  const gbadge=document.getElementById('group-badge');
  if (d.group_role==='master') {
    gbadge.textContent=`GROUP MASTER (${d.group_members||0})`; gbadge.style.display='';
  } else if (d.group_role==='member') {
    gbadge.textContent='GROUP MEMBER'; gbadge.style.display='';
  } else {
    gbadge.style.display='none';
  }
  // art + ambient glow
  const artEl=document.getElementById('art'), ph=document.getElementById('art-placeholder');
  const glowEl=document.getElementById('art-glow');
  if (d.art && d.art!==lastArt) {
    lastArt=d.art; const tmp=new Image();
    tmp.onload=()=>{
      artEl.src=d.art; artEl.classList.remove('hidden'); ph.style.display='none';
      if(glowEl){glowEl.src=d.art; glowEl.classList.add('visible');}
    };
    tmp.onerror=()=>{
      artEl.classList.add('hidden'); ph.style.display='';
      if(glowEl){glowEl.src=''; glowEl.classList.remove('visible');}
    };
    tmp.src=d.art;
    updateBackground(d.art);   // full-bleed living background
  } else if (!d.art) {
    artEl.classList.add('hidden'); ph.style.display='';
    if(glowEl){glowEl.src=''; glowEl.classList.remove('visible');}
    updateBackground('');
  }
  // EQ visualiser + play button ring (.playing also drives the icon morph)
  document.getElementById('eq-bars')?.classList.toggle('playing', d.playing);
  document.getElementById('btn-play').classList.toggle('playing', d.playing);
  // power button — highlight while playing
  document.getElementById('btn-power').classList.toggle('playing', d.playing);
  // mute button
  const muteBtn=document.getElementById('btn-mute');
  muteBtn.classList.toggle('muted', !!d.muted);
  document.getElementById('ico-mute-lines').style.display=d.muted?'none':'';
  document.getElementById('ico-mute-cross').style.display=d.muted?'':'none';
  // volume
  const sl=document.getElementById('vol-slider');
  if (!sl.matches(':active')) { sl.value=d.volume; updateVol(d.volume); }
  // chip
  const chip=document.getElementById('chip-'+activeHost.replace(/\./g,'_'));
  if (chip) { chip.classList.toggle('playing',d.playing); chip.classList.add('active'); }
  // presets — populate dropdown art-tile grid
  renderPresetGrid(d.presets || []);
}

// ── Preset art tiles ──────────────────────────────────────────────────────────
// Tiles show the preset's containerArt; UPNP presets pointing at our DLNA
// redirect fall back to the custom station's art_url (fetched once, cached).
let _presetSig='', _stationArt=null, _stationArtLoading=false;

function presetArt(p) {
  if (p.art) return p.art;
  const loc = p.location || '';
  // TuneIn presets carry no containerArt, but the station id maps to a
  // public logo CDN (verified pattern used by the TuneIn apps themselves)
  if (p.source === 'TUNEIN') {
    const m = loc.match(/\/station\/(s\d+)/);
    if (m) return `https://cdn-radiotime-logos.tunein.com/${m[1]}d.png`;
  }
  // Our own stations: UPNP DLNA redirects and LOCAL_INTERNET_RADIO descriptors
  if (!loc.includes('/dlna/stream/') && !loc.includes('/api/station-desc/')) return '';
  const sid = loc.split('/').pop();
  if (_stationArt) return _stationArt[sid] || '';
  if (!_stationArtLoading) {
    _stationArtLoading = true;
    fetch('/api/stations').then(r=>r.json()).then(list=>{
      _stationArt = {};
      list.forEach(s => { _stationArt[s.id] = s.art_url || ''; });
      _presetSig = '';                       // force re-render with art
      if (lastState) applyState(lastState);
    }).catch(()=>{ _stationArtLoading = false; });
  }
  return '';
}

function renderPresetGrid(presets) {
  const g = document.getElementById('presets-grid');
  const sig = JSON.stringify(presets.map(p=>[p?.name||'', presetArt(p||{})]));
  if (sig === _presetSig) return;
  _presetSig = sig;
  g.innerHTML = '';
  for (let i=0; i<6; i++) {
    const p = presets[i]||{}, nm = p.name||'', art = presetArt(p);
    const div = document.createElement('div');
    div.className = 'preset' + (nm ? '' : ' empty');
    div.innerHTML = (art
        ? `<div class="preset-art" style="background-image:url('${art.replace(/'/g,'%27')}')"></div>`
        : `<div class="preset-art preset-art-ph">${nm?'&#9835;':''}</div>`)
      + `<div class="preset-shade"></div>
         <div class="preset-num">${i+1}</div>
         <div class="preset-name">${nm||'—'}</div>`;
    div.onclick=(e)=>{ ripple(div,e); if(navigator.vibrate)navigator.vibrate(8); cmd('preset'+(i+1)); closePresets(); };
    g.appendChild(div);
  }
}

// ── Living background ─────────────────────────────────────────────────────────
// The current album art drives a slow-drifting, blurred full-bleed backdrop.
// Two layers crossfade so track changes melt rather than flash.
let _bgWhich='b';            // last layer shown; first call flips to 'a'

function updateBackground(artUrl){
  const a=document.getElementById('bg-art-a'), b=document.getElementById('bg-art-b');
  if(!a||!b) return;
  if(!artUrl){ a.classList.remove('show'); b.classList.remove('show'); return; }
  const next=_bgWhich==='a'?b:a, cur=_bgWhich==='a'?a:b;
  const probe=new Image();
  probe.onload=()=>{
    next.style.backgroundImage=`url("${artUrl}")`;
    next.classList.add('show');
    cur.classList.remove('show');
    _bgWhich=_bgWhich==='a'?'b':'a';
  };
  probe.onerror=()=>{};
  probe.src=artUrl;
}

// ── Volume ───────────────────────────────────────────────────────────────────
let volTooltipTimer=null;
function onVolInput(v) {
  updateVol(v);
  const tip=document.getElementById('vol-tooltip');
  tip.textContent=v; tip.classList.add('visible');
  clearTimeout(volTooltipTimer);
  volTooltipTimer=setTimeout(()=>tip.classList.remove('visible'), 1200);
}
function updateVol(v) {
  const pct=v+'%';
  document.getElementById('vol-track').style.setProperty('--pct',pct);
  document.getElementById('vol-slider').style.setProperty('--pct',pct);
  document.getElementById('vol-tooltip').style.left=pct;
}
let volD=null;
function sendVol(v) { clearTimeout(volD); volD=setTimeout(()=>{
  if (activeHost) fetch(`/api/cmd?host=${activeHost}&action=volume&value=${v}`);
}, 200); }
function nudgeVol(delta) {
  const s = document.getElementById('vol-slider');
  const v = Math.min(100, Math.max(0, parseInt(s.value) + delta));
  s.value = v; onVolInput(v); sendVol(v);
}

// ── Commands ─────────────────────────────────────────────────────────────────
function ripple(el, e) {
  const r = document.createElement('span');
  r.className = 'ripple';
  const rect = el.getBoundingClientRect();
  const size = Math.max(rect.width, rect.height);
  const x = (e.clientX||rect.left+rect.width/2) - rect.left - size/2;
  const y = (e.clientY||rect.top+rect.height/2) - rect.top - size/2;
  r.style.cssText=`width:${size}px;height:${size}px;left:${x}px;top:${y}px`;
  el.appendChild(r);
  r.addEventListener('animationend', ()=>r.remove());
}
async function cmd(a, el, e) {
  if (el && e) ripple(el, e);
  if (navigator.vibrate) navigator.vibrate(8);
  if (!activeHost) { toast('No speaker selected'); return; }
  await fetch(`/api/cmd?host=${activeHost}&action=${a}`);
  setTimeout(pollNow,500);
}

// ── Preset backup ────────────────────────────────────────────────────────────
async function backupPresets() {
  if (!activeHost) { toast('Select a speaker first'); return; }
  try {
    const r = await fetch(`/api/presets/backup?host=${activeHost}`);
    const d = await r.json();
    toast(`Backed up ${(d.presets||[]).length} presets`);
    loadBackupInfo();
  } catch(e) { toast('Backup failed'); }
}
async function restorePresets() {
  if (!activeHost) { toast('Select a speaker first'); return; }
  if (!confirm('Restore backed-up presets to this speaker? This will overwrite current presets.')) return;
  try {
    const r = await fetch(`/api/presets/restore?host=${activeHost}`);
    const d = await r.json();
    toast(d.ok ? `Restored ${d.count} presets` : (d.error||'Restore failed'));
  } catch(e) { toast('Restore failed'); }
}
async function loadBackupInfo() {
  if (!activeHost) return;
  try {
    const r = await fetch(`/api/presets/backup-info?host=${activeHost}`);
    const d = await r.json();
    const el = document.getElementById('backup-status');
    if (d.backed_up) {
      el.innerHTML = `Last backup: <strong style="color:var(--gold)">${d.backed_up}</strong>
                      — ${(d.presets||[]).length} presets saved`;
      const list = document.getElementById('backup-list');
      list.innerHTML = (d.presets||[]).map((p,i)=>`
        <div class="manage-card">
          <div class="mc-left">
            <div class="mc-name">${p.name||('Preset '+(i+1))}</div>
            <div class="mc-meta">${p.source} ${p.location?'• '+p.location:''}</div>
          </div>
        </div>`).join('');
    } else {
      el.textContent = 'No backup yet for this speaker.';
      document.getElementById('backup-list').innerHTML = '';
    }
  } catch(e){}
}

// ── Preset health check ──────────────────────────────────────────────────────
window._healthPresets = {};  // id → {name, location, source}
async function checkPresetHealth() {
  if (!activeHost) { toast('Select a speaker first'); return; }
  const btn = document.getElementById('btn-health-check');
  const box = document.getElementById('health-results');
  btn.textContent = 'Checking…'; btn.disabled = true;
  box.style.display = 'none'; box.innerHTML = '';
  try {
    const d = await (await fetch(`/api/presets/health?host=${activeHost}`)).json();
    if (d.error) { toast('Could not fetch preset data'); return; }
    // Store preset data keyed by id so onclick buttons can reference without escaping issues
    window._healthPresets = {};
    d.presets.forEach(p => { window._healthPresets[p.id] = p; });
    const summary = d.at_risk === 0
      ? `<div class="health-summary all-safe">✓ All ${d.total} presets are safe — no cloud dependency</div>`
      : `<div class="health-summary has-risk">⚠ ${d.at_risk} of ${d.total} preset${d.at_risk>1?'s':''} at risk — will stop working after the Bose cloud shuts down on 6 May 2026</div>`;
    const cards = d.presets.map(p => {
      const srcLine = p.label ? `${p.label} (${p.source})` : (p.source || 'Empty slot');
      const sug = p.suggestion ? `<div class="health-sug">→ ${p.suggestion}</div>` : '';
      const replBtn = (p.risk==='high'||p.risk==='unknown') ? `
        <button class="mc-btn" onclick="prefillCustomStation('${p.id}')"
          style="margin-top:6px;font-size:10px;padding:4px 8px">
          + Use as Custom Station template
        </button>` : '';
      return `<div class="health-card risk-${p.risk}">
        <div class="health-dot"></div>
        <div style="flex:1;min-width:0">
          <div class="health-name">Preset ${p.id}${p.name?' — '+p.name:''}</div>
          <div class="health-source">${srcLine}</div>
          ${sug}
          ${replBtn}
        </div>
      </div>`;
    }).join('');
    const note = d.data_source === 'backup'
      ? '<p style="font-size:11px;color:var(--fg3);margin-bottom:8px">⚠ Speaker offline — results based on last backup</p>' : '';
    box.innerHTML = note + summary + cards;
    box.style.display = 'block';
  } catch(e) { toast('Health check failed'); }
  finally { btn.textContent = '● Health Check'; btn.disabled = false; }
}

function prefillCustomStation(presetId) {
  const p = window._healthPresets[presetId];
  if (!p) return;
  const isDirectUrl = /^https?:\/\//i.test(p.location||'');
  // Switch to Presets tab and open Custom Radio Stations section
  switchTab('manage');
  const body = document.getElementById('sec-stations');
  const chev = document.getElementById('chev-stations');
  if (body && body.style.display === 'none') {
    body.style.display = 'block';
    if (chev) chev.classList.add('open');
    loadStations();
  }
  // Fill the form
  document.getElementById('st-name').value = p.name || '';
  document.getElementById('st-url').value  = isDirectUrl ? (p.location||'') : '';
  document.getElementById('st-art').value  = '';
  document.getElementById('st-search-results').style.display = 'none';
  // Scroll and auto-search if no direct URL
  const form = document.getElementById('add-form');
  if (form) form.scrollIntoView({behavior:'smooth', block:'center'});
  if (isDirectUrl) {
    toast('Form pre-filled — review then click Add Station');
    document.getElementById('st-url').focus();
  } else {
    // Auto-search RadioBrowser so the user can pick a direct stream
    setTimeout(() => searchStationStream(), 400);
  }
}

async function searchStationStream(nameOverride) {
  const name = nameOverride || document.getElementById('st-name').value.trim();
  if (!name) { toast('Enter a station name first'); return; }
  const resultsEl = document.getElementById('st-search-results');
  resultsEl.style.display = 'block';
  resultsEl.innerHTML = '<div class="sr-item" style="cursor:default;color:var(--fg3)">Searching…</div>';
  try {
    const d = await (await fetch(`/api/stations/stream-search?q=${encodeURIComponent(name)}`)).json();
    if (d.error || !d.length) {
      resultsEl.innerHTML = '<div class="sr-item" style="cursor:default;color:var(--fg3)">No results found — try a shorter name or paste the URL manually</div>';
      return;
    }
    resultsEl.innerHTML = d.map((s,i) => `
      <div class="sr-item" onclick="pickStreamResult(${i})">
        ${s.favicon ? `<img class="sr-logo" src="${s.favicon}" onerror="this.style.display='none'">` : '<div class="sr-logo"></div>'}
        <div class="sr-info">
          <div class="sr-name">${s.name}</div>
          <div class="sr-meta">${[s.country, s.bitrate?s.bitrate+'kbps':'', s.codec].filter(Boolean).join(' · ')}</div>
          <div class="sr-meta" style="font-size:9px;opacity:.6">${s.url}</div>
        </div>
        <div class="sr-use">Use ›</div>
      </div>`).join('');
    window._streamResults = d;
  } catch(e) {
    resultsEl.innerHTML = '<div class="sr-item" style="cursor:default;color:var(--fg3)">Search failed</div>';
  }
}

function pickStreamResult(idx) {
  const s = (window._streamResults||[])[idx];
  if (!s) return;
  document.getElementById('st-url').value  = s.url;
  if (!document.getElementById('st-name').value) document.getElementById('st-name').value = s.name;
  if (s.favicon && !document.getElementById('st-art').value) document.getElementById('st-art').value = s.favicon;
  document.getElementById('st-search-results').style.display = 'none';
  toast('Stream URL selected — click Add Station to save');
}

// ── Custom stations ──────────────────────────────────────────────────────────
async function loadStations() {
  try {
    const stations = await (await fetch('/api/stations')).json();
    const el = document.getElementById('stations-list');
    if (!stations.length) { el.innerHTML='<p style="font-size:12px;color:var(--fg3)">No custom stations yet.</p>'; return; }
    el.innerHTML = stations.map(s=>`
      <div class="manage-card">
        <div class="mc-left">
          <div class="mc-name">${s.name}</div>
          <div class="mc-meta">${s.stream_url}</div>
        </div>
        <div class="mc-actions">
          <button class="mc-btn" onclick="playStation('${s.id}')">Play</button>
          <button class="mc-btn" onclick="pushStation('${s.id}')">Set Preset</button>
          <button class="mc-btn danger" onclick="deleteStation('${s.id}')">✕</button>
        </div>
      </div>`).join('');
  } catch(e){}
}
async function addStation() {
  const name = document.getElementById('st-name').value.trim();
  const url  = document.getElementById('st-url').value.trim();
  const art  = document.getElementById('st-art').value.trim();
  if (!name || !url) { toast('Name and URL are required'); return; }
  try {
    await fetch('/api/stations/add', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name, stream_url:url, art_url:art})
    });
    document.getElementById('st-name').value='';
    document.getElementById('st-url').value='';
    document.getElementById('st-art').value='';
    toast('Station added'); loadStations();
  } catch(e) { toast('Failed to add station'); }
}
async function deleteStation(id) {
  if (!confirm('Delete this station?')) return;
  await fetch(`/api/stations/delete?id=${id}`);
  toast('Deleted'); loadStations();
}
async function playStation(id) {
  if (!activeHost) { toast('Select a speaker first'); return; }
  await fetch(`/api/stations/play?host=${activeHost}&id=${id}`);
  toast('Playing…'); setTimeout(pollNow,1000);
}
async function pushStation(id) {
  if (!activeHost) { toast('Select a speaker first'); return; }
  const n = prompt('Which preset slot? (1-6)','1');
  if (!n || n<1 || n>6) return;
  await fetch(`/api/stations/set-preset?host=${activeHost}&id=${id}&slot=${n}`);
  toast(`Saved to preset ${n}`); setTimeout(pollNow,1000);
}

// ── Presets dropdown ─────────────────────────────────────────────────────────
function togglePresets() {
  const clip     = document.getElementById('presets-clip');
  const backdrop = document.getElementById('presets-backdrop');
  const btn      = document.getElementById('preset-toggle');
  const isOpen   = clip.classList.contains('open');
  if (isOpen) {
    closePresets();
  } else {
    // Position the clip right below the header (the tab bar is fixed at
    // the bottom, so the header is the panel's visual anchor)
    const hdr = document.querySelector('header');
    clip.style.top = (hdr.offsetTop + hdr.offsetHeight) + 'px';
    clip.classList.add('open');
    backdrop.classList.add('open');
    btn.classList.add('open');
  }
}
function closePresets() {
  document.getElementById('presets-clip').classList.remove('open');
  document.getElementById('presets-backdrop').classList.remove('open');
  document.getElementById('preset-toggle').classList.remove('open');
}

// ── Groups ────────────────────────────────────────────────────────────────────
async function loadGroups() {
  if (!activeHost) {
    document.getElementById('group-status').innerHTML =
      '<p style="font-size:12px;color:var(--fg3)">Select a speaker first.</p>';
    document.getElementById('group-builder').innerHTML = '';
    return;
  }
  let zone;
  try { zone = await (await fetch('/api/group?host='+activeHost)).json(); }
  catch(e){ return; }

  const statusEl  = document.getElementById('group-status');
  const builderEl = document.getElementById('group-builder');

  // ─ Status card ─────────────────────────────────────────────────────────────
  if (zone.is_master) {
    const count = (zone.members||[]).length;
    statusEl.innerHTML = `<div class="manage-card">
      <div class="mc-left">
        <div class="mc-name">🔊 Group Master</div>
        <div class="mc-meta">${count} speaker${count!==1?'s':''} grouped</div>
      </div>
      <div class="mc-actions">
        <button class="mc-btn danger" onclick="dissolveGroup()">Dissolve</button>
      </div></div>`;
  } else if (zone.is_slave) {
    statusEl.innerHTML = `<div class="manage-card">
      <div class="mc-left">
        <div class="mc-name">🔉 Group Member</div>
        <div class="mc-meta">Following master at ${zone.master_ip||'unknown'}</div>
      </div></div>`;
  } else {
    statusEl.innerHTML =
      '<p style="font-size:12px;color:var(--fg3);margin-bottom:12px">Not in a group. Add speakers below to create one.</p>';
  }

  // Slaves can't be used to add/remove — only the master can
  if (zone.is_slave) { builderEl.innerHTML=''; return; }

  const others = speakers.filter(s=>s.host!==activeHost);
  if (!others.length) {
    builderEl.innerHTML =
      '<p style="font-size:12px;color:var(--fg3)">No other speakers found. Tap Scan to search again.</p>';
    return;
  }

  const memberIPs = new Set((zone.members||[]).map(m=>m.ip).filter(ip=>ip!==activeHost));
  builderEl.innerHTML = `
    <div class="section-label" style="margin-bottom:8px">Other Speakers</div>
    ${others.map(s=>`
      <div class="manage-card">
        <div class="mc-left">
          <div class="mc-name">${s.name}</div>
          <div class="mc-meta">${s.host}${memberIPs.has(s.host)?' · In group':''}</div>
        </div>
        <div class="mc-actions">
          ${memberIPs.has(s.host)
            ? `<button class="mc-btn danger" onclick="fetch('/api/cmd?host=${s.host}&action=power').then(()=>setTimeout(loadGroups,700))">Power Off</button>`
            : `<button class="mc-btn primary" onclick="addToGroup('${s.host}')">Add</button>`}
        </div>
      </div>`).join('')}
    <button class="mc-btn primary" onclick="groupAll()"
      style="width:calc(100% - 0px);margin-top:10px;padding:10px">
      Group All Speakers
    </button>`;
}

async function addToGroup(slaveHost) {
  if (!activeHost) return;
  let zone;
  try { zone = await (await fetch('/api/group?host='+activeHost)).json(); } catch(e){ return; }
  const existing = (zone.members||[]).map(m=>m.ip).filter(ip=>ip!==activeHost);
  if (!existing.includes(slaveHost)) existing.push(slaveHost);
  await fetch(`/api/group/create?master=${activeHost}&slaves=${existing.join(',')}`);
  toast('Group updated'); setTimeout(loadGroups, 700);
}

async function removeFromGroup(slaveHost) {
  if (!activeHost) return;
  let zone;
  try { zone = await (await fetch('/api/group?host='+activeHost)).json(); } catch(e){ return; }
  const remaining = (zone.members||[]).map(m=>m.ip).filter(ip=>ip!==activeHost && ip!==slaveHost);
  if (remaining.length) {
    await fetch(`/api/group/create?master=${activeHost}&slaves=${remaining.join(',')}`);
  } else {
    await fetch('/api/group/remove?host='+activeHost);
  }
  toast('Group updated'); setTimeout(loadGroups, 700);
}

async function dissolveGroup() {
  if (!confirm('Dissolve this group? All speakers will play independently.')) return;
  await fetch('/api/group/remove?host='+activeHost);
  toast('Group dissolved'); setTimeout(loadGroups, 700);
}

async function groupAll() {
  if (!activeHost) return;
  const slaves = speakers.filter(s=>s.host!==activeHost).map(s=>s.host).join(',');
  if (!slaves) { toast('No other speakers to group'); return; }
  await fetch(`/api/group/create?master=${activeHost}&slaves=${slaves}`);
  toast('All speakers grouped'); setTimeout(loadGroups, 700);
}


// ── Bass ──────────────────────────────────────────────────────────────────────
let bassTooltipTimer=null;
async function loadBass() {
  const row = document.getElementById('bass-row');
  if (!row || !activeHost) { if(row) row.style.display='none'; return; }
  try {
    const d = await (await fetch('/api/bass?host='+activeHost)).json();
    if (d.available) {
      document.getElementById('bass-slider').min = d.min;
      document.getElementById('bass-slider').max = d.max;
      document.getElementById('bass-slider').value = d.current;
      updateBass(d.current, d.min, d.max);
      row.style.display = 'block';
    } else {
      row.style.display = 'none';
    }
  } catch(e) { row.style.display='none'; }
}
function onBassInput(v) {
  const sl = document.getElementById('bass-slider');
  updateBass(v, parseInt(sl.min), parseInt(sl.max));
  const tip = document.getElementById('bass-tooltip');
  tip.textContent = v; tip.classList.add('visible');
  clearTimeout(bassTooltipTimer);
  bassTooltipTimer = setTimeout(()=>tip.classList.remove('visible'), 1200);
}
function updateBass(v, min=-9, max=0) {
  const pct = ((parseInt(v)-min)/(max-min)*100)+'%';
  const track = document.getElementById('bass-track');
  const slider = document.getElementById('bass-slider');
  if(track) { track.style.setProperty('--pct',pct); document.getElementById('bass-tooltip').style.left=pct; }
  if(slider) slider.style.setProperty('--pct',pct);
}
let bassD=null;
function sendBass(v) { clearTimeout(bassD); bassD=setTimeout(()=>{
  if (activeHost) fetch(`/api/cmd?host=${activeHost}&action=bass&value=${v}`);
}, 200); }

// ── Backup All ────────────────────────────────────────────────────────────────
async function backupAll() {
  const st = document.getElementById('backup-all-status');
  if(st) st.textContent = 'Backing up…';
  try {
    const d = await (await fetch('/api/presets/backup-all')).json();
    const ok = d.results.filter(r=>r.ok).length;
    const fail = d.results.filter(r=>!r.ok).length;
    if(st) st.textContent = `✓ ${ok} backed up${fail?' — '+fail+' failed':''}`;
    // Refresh speaker list so warning badges update
    await fetchSpeakers(false);
  } catch(e) { if(st) st.textContent='Backup failed'; }
}

// ── Rename ────────────────────────────────────────────────────────────────────
async function renameSpeaker() {
  const input = document.getElementById('rename-input');
  if (!input || !activeHost) return;
  const name = input.value.trim();
  if (!name) return;
  try {
    const d = await (await fetch(`/api/rename?host=${activeHost}&name=${encodeURIComponent(name)}`)).json();
    if (d.ok) {
      const sp = speakers.find(s=>s.host===activeHost);
      if (sp) sp.name = d.name;
      renderRooms();
      toast('Speaker renamed');
    }
  } catch(e) { toast('Rename failed'); }
}

// ── Settings — Speaker info ───────────────────────────────────────────────────
async function loadSpeakerInfo() {
  const el = document.getElementById('speaker-info');
  if (!el) return;
  if (!activeHost) {
    el.innerHTML = '<p style="font-size:12px;color:var(--fg3)">Select a speaker to view its details.</p>';
    return;
  }
  el.innerHTML = '<p style="font-size:12px;color:var(--fg3)">Loading…</p>';
  try {
    const d = await (await fetch('/api/device-info?host='+activeHost)).json();
    const sigColour = {Poor:'#ef4444',Fair:'#f59e0b',Good:'#4caf50',Excellent:'#4caf50'}[d.wifi_signal]||'var(--fg2)';
    const spotifyBadge = d.spotify_connect
      ? `<span style="font-size:10px;font-weight:700;color:#1db954;background:rgba(29,185,84,.1);
           border:1px solid rgba(29,185,84,.3);padding:2px 8px;border-radius:10px;
           letter-spacing:.04em;margin-left:6px">Spotify Connect</span>` : '';
    el.innerHTML = `<div style="display:flex;align-items:center;gap:6px;margin-bottom:10px">
      <span style="font-size:14px;font-weight:700;color:var(--white)">${d.name||d.model||'Speaker'}</span>
      ${spotifyBadge}
    </div>
    <table class="speaker-info-table">
      <tr><td>Model</td><td>${d.model||'—'}</td></tr>
      <tr><td>Firmware</td><td>${d.firmware||'—'}</td></tr>
      <tr><td>IP Address</td><td>${d.ip||activeHost}</td></tr>
      <tr><td>MAC Address</td><td>${d.mac||'—'}</td></tr>
      <tr><td>Serial Number</td><td>${d.serial||'—'}</td></tr>
      <tr><td>Device ID</td><td>${d.device_id||'—'}</td></tr>
      <tr><td>Country / Region</td><td>${[d.country,d.region].filter((v,i,a)=>v&&a.indexOf(v)===i).join(' / ')||'—'}</td></tr>
      ${d.wifi_ssid?`<tr><td>Wi-Fi Network</td><td>${d.wifi_ssid}</td></tr>`:''}
      ${d.wifi_signal?`<tr><td>Signal Strength</td><td style="color:${sigColour};font-weight:700">${d.wifi_signal}${d.wifi_band?' · '+d.wifi_band:''}</td></tr>`:''}
    </table>
    <div style="margin-top:14px;display:flex;gap:8px;align-items:center">
      <input id="rename-input" style="flex:1;background:var(--surface2);border:1px solid var(--border);
        color:var(--fg1);border-radius:8px;padding:6px 10px;font-size:13px"
        value="${d.name||''}" placeholder="Speaker name">
      <button class="mc-btn primary" onclick="renameSpeaker()">Rename</button>
    </div>
    <div id="bass-row" style="display:none;margin-top:16px">
      <div style="font-size:12px;color:var(--fg3);font-weight:600;margin-bottom:6px;text-transform:uppercase;letter-spacing:.06em">Bass</div>
      <div style="display:flex;align-items:center;gap:10px">
        <span class="bass-label">−</span>
        <div id="bass-track" style="flex:1;position:relative;padding-top:22px">
          <div id="bass-tooltip">0</div>
          <input type="range" id="bass-slider" min="-9" max="0" value="0"
                 oninput="onBassInput(this.value)" onchange="sendBass(this.value)">
        </div>
        <span class="bass-label">+</span>
      </div>
    </div>`;
    loadBass();
  } catch(e) {
    el.innerHTML = '<p style="font-size:12px;color:var(--fg3)">Could not load device info.</p>';
  }
}

// ── Settings — Radio Presets (UPNP speakers) ──────────────────────────────────
async function loadUpnpStations() {
  const el = document.getElementById('upnp-stations-list');
  if (!el) return;
  if (!activeHost) {
    el.innerHTML = '<p style="font-size:12px;color:var(--fg3)">Select a speaker to view its stored radio presets.</p>';
    return;
  }
  el.innerHTML = '<p style="font-size:12px;color:var(--fg3)">Loading…</p>';
  try {
    const [stateData, allStations] = await Promise.all([
      fetch('/api/state?host=' + activeHost).then(r => r.json()),
      fetch('/api/stations').then(r => r.json())
    ]);
    const stationMap = {};
    allStations.forEach(s => { stationMap[s.id] = s; });
    const upnpPresets = (stateData.presets || []).filter(p =>
      p.source === 'UPNP' && p.location && p.location.includes('/dlna/stream/')
    );
    if (!upnpPresets.length) {
      el.innerHTML = '<p style="font-size:12px;color:var(--fg3)">No radio presets stored on this speaker.</p>';
      return;
    }
    el.innerHTML = upnpPresets.map(p => {
      const sid     = p.location.split('/').pop();
      const station = stationMap[sid] || {};
      const art     = station.art_url || '';
      const name    = p.name || station.name || sid;
      const artHtml = art
        ? `<img src="${art}" alt="" style="width:44px;height:44px;border-radius:8px;object-fit:cover;flex-shrink:0" onerror="this.style.display='none'">`
        : `<div style="width:44px;height:44px;border-radius:8px;background:var(--surface2);flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:18px">&#127925;</div>`;
      return `<div class="manage-card">
        ${artHtml}
        <div class="mc-left">
          <div class="mc-name">${name}</div>
          <div class="mc-meta">Preset ${p.id}</div>
        </div>
        <div class="mc-actions">
          <button class="mc-btn primary" onclick="playStation('${sid}')">Play</button>
        </div>
      </div>`;
    }).join('');
  } catch(e) {
    el.innerHTML = '<p style="font-size:12px;color:var(--fg3)">Could not load radio presets.</p>';
  }
}

// ── Settings — Alexa / Matter QR ─────────────────────────────────────────────
async function loadAlexaQR() {
  const box    = document.getElementById('qr-box');
  const manual = document.getElementById('qr-manual');
  const status = document.getElementById('qr-status');
  const badge  = document.getElementById('qr-status-badge');
  if (box) box.textContent = 'Loading…';
  try {
    const d = await (await fetch('/api/matter/qr')).json();
    if (box) box.textContent = d.qrText || '(QR not available)';
    if (manual) manual.textContent = d.manualPairingCode ? 'Manual code: ' + d.manualPairingCode : '';
    const ok = d.commissioned;
    if (badge) { badge.textContent = ok ? '✓ Commissioned' : 'Not commissioned';
                 badge.className = 'qr-collapse-badge ' + (ok ? 'ok' : 'warn'); }
    if (status) { status.textContent = ok
        ? '✓ Commissioned with Alexa — devices are available'
        : 'Not yet commissioned — Add Device → Other → Matter in the Alexa app';
      status.style.color = ok ? '#4caf50' : 'var(--fg2)'; }
  } catch(e) {
    if (box)   box.textContent = 'Bridge not running';
    if (badge) { badge.textContent = 'offline'; badge.className = 'qr-collapse-badge warn'; }
    if (status){ status.textContent = 'systemctl --user start soundtouch-matter';
                 status.style.color = 'var(--fg3)'; }
  }
}
function toggleSection(bodyId, chevronId) {
  const body    = document.getElementById(bodyId);
  const chevron = document.getElementById(chevronId);
  const opening = body.style.display === 'none';
  body.style.display = opening ? 'block' : 'none';
  if (chevron) chevron.classList.toggle('open', opening);
  if (opening && bodyId === 'sec-speaker')       loadSpeakerInfo();
  if (opening && bodyId === 'sec-alexa')         loadAlexaQR();
  if (opening && bodyId === 'sec-manage-backup') loadBackupInfo();
  if (opening && bodyId === 'sec-upnp-stations')  loadUpnpStations();
  if (opening && bodyId === 'sec-stations')      loadStations();
  if (opening && bodyId === 'sec-scenes')        loadScenes();
  if (opening && bodyId === 'sec-alarms')        loadAlarms();
  if (opening && bodyId === 'sec-announce')      loadAnnounceSection();
}
function loadAnnounceSection() {
  const container = document.getElementById('main-ann-speakers');
  if (!container || container.children.length) return;
  speakers.forEach(sp => {
    const id = 'main-ann-chk-' + sp.host.replace(/\./g,'_');
    const row = document.createElement('label');
    row.style.cssText = 'display:flex;align-items:center;gap:10px;cursor:pointer;padding:7px 10px;' +
      'background:var(--surface2);border-radius:8px;border:1px solid var(--border)';
    row.innerHTML = `<input type="checkbox" id="${id}" checked style="accent-color:var(--blue);width:15px;height:15px">` +
      `<span style="font-size:13px;font-weight:600">${sp.name}</span>`;
    container.appendChild(row);
  });
  const volEl = document.getElementById('main-ann-vol');
  const lblEl = document.getElementById('main-ann-vol-lbl');
  if (volEl && lblEl) volEl.oninput = function() {
    lblEl.textContent = this.value;
    this.style.background = `linear-gradient(to right,var(--blue) ${this.value}%,var(--surface2) ${this.value}%)`;
  };
}
async function sendMainAnnounce() {
  const text = document.getElementById('main-ann-text').value.trim();
  const statusEl = document.getElementById('main-ann-status');
  if (!text) { statusEl.textContent = 'Please enter a message.'; return; }
  const hosts = speakers
    .filter(sp => document.getElementById('main-ann-chk-' + sp.host.replace(/\./g,'_'))?.checked)
    .map(sp => sp.host);
  if (!hosts.length) { statusEl.textContent = 'Select at least one speaker.'; return; }
  const volume = parseInt(document.getElementById('main-ann-vol').value);
  statusEl.textContent = 'Sending…';
  try {
    const r = await fetch('/api/tts/announce', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({text, hosts, volume})});
    const d = await r.json();
    statusEl.textContent = d.ok
      ? `Announcing on ${d.speakers} speaker${d.speakers!==1?'s':''}…`
      : 'Error: ' + (d.error||'unknown');
  } catch(e) { statusEl.textContent = 'Request failed.'; }
}
function toggleQR() {
  const body    = document.getElementById('qr-body');
  const chevron = document.getElementById('qr-chevron');
  const opening = body.style.display === 'none';
  body.style.display = opening ? 'block' : 'none';
  chevron.classList.toggle('open', opening);
  if (opening) loadAlexaQR();
}

// ── Helpers ──────────────────────────────────────────────────────────────────
function setText(id,v) { const e=document.getElementById(id); if(e) e.textContent=v; }
let toastT;
function toast(m) {
  const t=document.getElementById('toast');
  t.textContent=m; t.classList.add('show');
  clearTimeout(toastT); toastT=setTimeout(()=>t.classList.remove('show'),2400);
}
function showScanning(m) { document.getElementById('scan-label').textContent=m||'Scanning…';
  document.getElementById('scanning').classList.add('show'); }
function hideScanning() { document.getElementById('scanning').classList.remove('show'); }

// ── Backup JSON editor ────────────────────────────────────────────────────────
async function openBackupEditor() {
  if (!activeHost) { toast('Select a speaker first'); return; }
  const errEl = document.getElementById('backup-json-error');
  const ta    = document.getElementById('backup-json-editor');
  errEl.style.display = 'none';
  ta.value = 'Loading…';
  openModal('backup-json-modal');
  const spk = speakers.find(s => s.host === activeHost);
  document.getElementById('backup-json-title').textContent =
    'Backup JSON' + (spk ? ' — ' + spk.name : '');
  try {
    const d = await (await fetch('/api/presets/backup-json?host=' + activeHost)).json();
    if (d.error) {
      ta.value = '// No backup found for this speaker.\n// Click "Backup Now" first, then reopen.';
    } else {
      ta.value = JSON.stringify(d, null, 2);
    }
  } catch(e) { ta.value = '// Failed to load backup.'; }
}

async function _postBackupJson() {
  const ta    = document.getElementById('backup-json-editor');
  const errEl = document.getElementById('backup-json-error');
  errEl.style.display = 'none';
  let data;
  try { data = JSON.parse(ta.value); }
  catch(e) {
    errEl.textContent = 'Invalid JSON: ' + e.message;
    errEl.style.display = '';
    return null;
  }
  try {
    const r = await fetch('/api/presets/backup-json?host=' + activeHost, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data)
    });
    const d = await r.json();
    if (!d.ok) {
      errEl.textContent = d.error || 'Save failed';
      errEl.style.display = '';
      return null;
    }
    return true;
  } catch(e) {
    errEl.textContent = 'Save failed';
    errEl.style.display = '';
    return null;
  }
}

async function saveBackupJson() {
  if (await _postBackupJson()) { toast('Backup saved'); }
}

async function saveAndRestoreJson() {
  if (!await _postBackupJson()) return;
  try {
    const d = await (await fetch('/api/presets/restore?host=' + activeHost)).json();
    if (d.ok) {
      toast('Saved & restored ' + d.count + ' preset' + (d.count !== 1 ? 's' : ''));
      closeModal('backup-json-modal');
      setTimeout(pollNow, 600);
    } else {
      const errEl = document.getElementById('backup-json-error');
      errEl.textContent = d.error || 'Restore failed';
      errEl.style.display = '';
    }
  } catch(e) {
    const errEl = document.getElementById('backup-json-error');
    errEl.textContent = 'Restore failed';
    errEl.style.display = '';
  }
}

// ── All-speaker volume ────────────────────────────────────────────────────────
let allVolTT=null;
function onAllVolInput(v) {
  const pct=v+'%';
  document.getElementById('all-vol-track').style.setProperty('--pct',pct);
  document.getElementById('all-vol-slider').style.setProperty('--pct',pct);
  const tip=document.getElementById('all-vol-tip');
  tip.style.left=pct; tip.textContent=v; tip.classList.add('visible');
  clearTimeout(allVolTT); allVolTT=setTimeout(()=>tip.classList.remove('visible'),1200);
}
function sendAllVol(v) { fetch(`/api/volume/all?value=${v}`); }
function nudgeAllVol(delta) {
  const s=document.getElementById('all-vol-slider');
  const v=Math.min(100,Math.max(0,parseInt(s.value)+delta));
  s.value=v; onAllVolInput(v); sendAllVol(v);
}

// ── Scenes ────────────────────────────────────────────────────────────────────
async function loadScenes() {
  const el=document.getElementById('scenes-list');
  if(!el)return;
  try {
    const scenes=await(await fetch('/api/scenes')).json();
    if(!scenes.length){
      el.innerHTML='<p style="font-size:12px;color:var(--fg3);margin-bottom:10px">No scenes saved yet.</p>';
      return;
    }
    el.innerHTML=scenes.map(s=>`
      <div class="manage-card">
        <div class="mc-left">
          <div class="mc-name">${s.name}</div>
          <div class="mc-meta">Preset ${s.preset} · ${[s.master,...(s.slaves||[])].length} speaker(s)</div>
        </div>
        <div class="mc-actions">
          <button class="mc-btn primary" onclick="activateScene('${s.id}')">Play</button>
          <button class="mc-btn danger" onclick="deleteScene('${s.id}')">✕</button>
        </div>
      </div>`).join('');
  }catch(e){}
}

async function saveScene() {
  if(!activeHost){toast('Select a speaker first');return;}
  const name=document.getElementById('scene-name-input').value.trim();
  if(!name){toast('Enter a scene name');return;}
  const presetSlot=parseInt(document.getElementById('scene-preset-input').value)||1;
  // Capture zone members
  let slaves=[];
  try{const z=await(await fetch('/api/group?host='+activeHost)).json();
      slaves=(z.members||[]).map(m=>m.ip).filter(ip=>ip!==activeHost);}catch(e){}
  // Capture volumes
  const hosts=[activeHost,...slaves];
  const volumes={};
  await Promise.all(hosts.map(async h=>{
    try{const st=await(await fetch('/api/state?host='+h)).json(); volumes[h]=st.volume||30;}catch(e){}
  }));
  try{
    await fetch('/api/scenes',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name,master:activeHost,slaves,volumes,preset:presetSlot})});
    document.getElementById('scene-name-input').value='';
    toast('Scene saved'); loadScenes();
  }catch(e){toast('Failed to save scene');}
}

async function activateScene(id) {
  try{
    const d=await(await fetch('/api/scenes/activate?id='+encodeURIComponent(id))).json();
    toast(d.ok?'Scene activated':'Could not activate scene');
    if(d.ok)setTimeout(pollNow,1200);
  }catch(e){toast('Failed');}
}

async function deleteScene(id) {
  if(!confirm('Delete this scene?'))return;
  await fetch('/api/scenes/delete?id='+encodeURIComponent(id));
  toast('Scene deleted'); loadScenes();
}

// ── Alarms ────────────────────────────────────────────────────────────────────
const ALARM_DAYS=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];

function updateAlarmSpeakerSelect() {
  const sel=document.getElementById('alarm-speaker-select');
  if(!sel)return;
  const cur=sel.value;
  sel.innerHTML='<option value="">Select a speaker…</option>'+
    speakers.map(s=>`<option value="${s.host}"${s.host===cur?' selected':''}>${s.name}</option>`).join('');
}

function _alarmHtml(a, closeModalId) {
  const dayStr=a.days.length===7?'Every day':
    (a.days.length===5&&!a.days.includes(5)&&!a.days.includes(6)?'Weekdays':
     a.days.map(d=>ALARM_DAYS[d]).join(', '));
  const spk=speakers.find(s=>s.host===a.host);
  const closeArg=closeModalId?`,'${closeModalId}'`:'';
  return`<div class="manage-card">
    <div class="mc-left">
      <div class="mc-name">${a.name} · ${a.time}</div>
      <div class="mc-meta">${spk?spk.name:a.host} · Preset ${a.preset} · ${dayStr}${a.volume!=null?' · Vol '+a.volume:''}</div>
    </div>
    <div class="mc-actions">
      <button class="mc-btn${a.enabled?' primary':''}" onclick="toggleAlarm('${a.id}',${!a.enabled}${closeArg})">${a.enabled?'On':'Off'}</button>
      <button class="mc-btn danger" onclick="deleteAlarm('${a.id}'${closeArg})">✕</button>
    </div>
  </div>`;
}

async function loadAlarms() {
  const el=document.getElementById('alarms-list');
  if(!el)return;
  updateAlarmSpeakerSelect();
  try{
    const alarms=await(await fetch('/api/alarms')).json();
    el.innerHTML=alarms.length
      ?alarms.map(a=>_alarmHtml(a)).join('')
      :'<p style="font-size:12px;color:var(--fg3);margin-bottom:10px">No alarms set.</p>';
  }catch(e){}
}

async function addAlarm() {
  const host=document.getElementById('alarm-speaker-select').value;
  if(!host){toast('Select a speaker');return;}
  const time=document.getElementById('alarm-time').value;
  if(!time){toast('Set a time');return;}
  const days=[];
  document.querySelectorAll('.alarm-day-chk:checked').forEach(cb=>days.push(parseInt(cb.value)));
  if(!days.length){toast('Select at least one day');return;}
  const name=document.getElementById('alarm-name').value.trim()||'Alarm';
  const preset=parseInt(document.getElementById('alarm-preset').value)||1;
  const volRaw=document.getElementById('alarm-vol').value;
  const volume=volRaw!==''?parseInt(volRaw):null;
  try{
    await fetch('/api/alarms',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name,host,preset,time,days,volume})});
    document.getElementById('alarm-name').value='';
    toast('Alarm saved'); loadAlarms();
  }catch(e){toast('Failed to save alarm');}
}

async function deleteAlarm(id, modalId) {
  if(!confirm('Delete this alarm?'))return;
  await fetch('/api/alarms/delete?id='+id);
  toast('Alarm deleted'); loadAlarms();
  if(modalId) _refreshAlarmsModal();
}

async function toggleAlarm(id, enabled, modalId) {
  await fetch(`/api/alarms/toggle?id=${id}&enabled=${enabled}`);
  loadAlarms();
  if(modalId) _refreshAlarmsModal();
}

// ── Modals ────────────────────────────────────────────────────────────────────
function openModal(id)  { document.getElementById(id)?.classList.add('open'); }
function closeModal(id) { document.getElementById(id)?.classList.remove('open'); }

async function _refreshScenesModal() {
  const el=document.getElementById('scenes-modal-body'); if(!el)return;
  try{
    const scenes=await(await fetch('/api/scenes')).json();
    if(!scenes.length){el.innerHTML='<p style="font-size:12px;color:var(--fg3)">No scenes saved yet.</p>';return;}
    el.innerHTML=scenes.map(s=>`
      <div class="manage-card">
        <div class="mc-left">
          <div class="mc-name">${s.name}</div>
          <div class="mc-meta">Preset ${s.preset} · ${[s.master,...(s.slaves||[])].length} speaker(s)</div>
        </div>
        <div class="mc-actions">
          <button class="mc-btn primary" onclick="activateScene('${s.id}');closeModal('scenes-modal')">Play</button>
          <button class="mc-btn danger" onclick="deleteSceneModal('${s.id}')">✕</button>
        </div>
      </div>`).join('');
  }catch(e){el.innerHTML='<p style="font-size:12px;color:var(--fg3)">Failed to load scenes.</p>';}
}

async function _refreshAlarmsModal() {
  const el=document.getElementById('alarms-modal-body'); if(!el)return;
  try{
    const alarms=await(await fetch('/api/alarms')).json();
    el.innerHTML=alarms.length
      ?alarms.map(a=>_alarmHtml(a,'alarms-modal')).join('')
      :'<p style="font-size:12px;color:var(--fg3)">No alarms set.</p>';
  }catch(e){}
}

async function openScenesModal() { openModal('scenes-modal'); await _refreshScenesModal(); }
async function openAlarmsModal() { openModal('alarms-modal'); await _refreshAlarmsModal(); }

async function deleteSceneModal(id) {
  if(!confirm('Delete this scene?'))return;
  await fetch('/api/scenes/delete?id='+encodeURIComponent(id));
  toast('Scene deleted'); loadScenes(); _refreshScenesModal();
}
