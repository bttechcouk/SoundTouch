
const CACHE='soundtouch-v7';
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
  e.respondWith(caches.match(e.request).then(cached=>{
    const net=fetch(e.request).then(r=>{
      if(r&&r.status===200&&r.type==='basic'){
        caches.open(CACHE).then(c=>c.put(e.request,r.clone()));
      }return r;
    }).catch(()=>cached);
    return cached||net;
  }));
});
