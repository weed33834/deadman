// deadman 移动端 Service Worker - PWA 离线缓存
// 策略：网络优先，失败降级到缓存（API 数据实时性要求高）
//       缓存优先（静态资源 mobile.html/manifest.json）

const CACHE_NAME = 'deadman-mobile-v1';
const STATIC_ASSETS = ['/m', '/mobile.html', '/manifest.json'];

// 安装：预缓存静态资源
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting())
  );
});

// 激活：清理旧缓存
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

// 请求拦截
self.addEventListener('fetch', (event) => {
  const req = event.request;

  // 仅处理 GET 请求
  if (req.method !== 'GET') return;

  const url = new URL(req.url);

  // API 请求：网络优先，失败降级缓存
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(
      fetch(req)
        .then((resp) => {
          // 缓存成功的 GET 响应
          if (resp.ok) {
            const clone = resp.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(req, clone));
          }
          return resp;
        })
        .catch(() => caches.match(req).then((cached) => cached || new Response('{"error":"offline"}', { headers: { 'Content-Type': 'application/json' } })))
    );
    return;
  }

  // 静态资源：缓存优先
  event.respondWith(
    caches.match(req).then((cached) => {
      if (cached) return cached;
      return fetch(req).then((resp) => {
        if (resp.ok) {
          const clone = resp.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(req, clone));
        }
        return resp;
      });
    })
  );
});
