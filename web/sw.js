
const CACHE='soundtouch-v22';
self.addEventListener('install',e=>{
  e.waitUntil(caches.open(CACHE).then(c=>c.addAll(['/'])));
  self.skipWaiting();
});
self.addEventListener('activate',e=>{
  e.waitUntil(caches.keys().then(ks=>
    Promise.all(ks.filter(k=>k!==CACHE).map(k=>caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener('fetch',e=>{
  const u=new URL(e.request.url);
  if(u.pathname.startsWith('/api/')||u.pathname==='/sw.js'||e.request.method!=='GET')return;
  // Network first: the controller is on the LAN, so a fresh copy is quick,
  // and a deploy shows up on the next open rather than the one after (cache
  // first served the old app.js once after every change). The cache is only
  // the offline fallback.
  e.respondWith(fetch(e.request).then(r=>{
    if(r&&r.status===200&&r.type==='basic'){
      const copy=r.clone(); caches.open(CACHE).then(c=>c.put(e.request,copy));
    }
    return r;
  }).catch(()=>caches.match(e.request)));
});
