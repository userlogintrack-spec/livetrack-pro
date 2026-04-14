// LiveVisitorHub - Service Worker for PWA
const CACHE_NAME = 'livetrack-v2';
const OFFLINE_URL = '/dashboard/';

// Assets to cache for offline
const PRECACHE_URLS = [
    '/static/manifest.json',
    'https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap',
    'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css',
];

// Install: cache essential assets
self.addEventListener('install', function(event) {
    event.waitUntil(
        caches.open(CACHE_NAME).then(function(cache) {
            return cache.addAll(PRECACHE_URLS);
        }).then(function() {
            return self.skipWaiting();
        })
    );
});

// Activate: clean old caches
self.addEventListener('activate', function(event) {
    event.waitUntil(
        caches.keys().then(function(keys) {
            return Promise.all(
                keys.filter(function(key) { return key !== CACHE_NAME; })
                    .map(function(key) { return caches.delete(key); })
            );
        }).then(function() {
            return self.clients.claim();
        })
    );
});

// Fetch: network-first for HTML, cache-first for assets
self.addEventListener('fetch', function(event) {
    var request = event.request;

    // Skip non-GET and WebSocket requests
    if (request.method !== 'GET' || request.url.includes('/ws/')) return;

    // API requests: network only
    if (request.url.includes('/api/') || request.url.includes('/dashboard/api/')) {
        return;
    }

    // Static assets: cache-first
    if (request.url.includes('/static/') || request.url.includes('fonts.googleapis') || request.url.includes('cdnjs.cloudflare')) {
        event.respondWith(
            caches.match(request).then(function(cached) {
                if (cached) return cached;
                return fetch(request).then(function(response) {
                    if (response.ok) {
                        var clone = response.clone();
                        caches.open(CACHE_NAME).then(function(cache) { cache.put(request, clone); });
                    }
                    return response;
                });
            })
        );
        return;
    }

    // HTML pages: network-first with offline fallback
    if (request.headers.get('Accept') && request.headers.get('Accept').includes('text/html')) {
        event.respondWith(
            fetch(request).then(function(response) {
                var clone = response.clone();
                caches.open(CACHE_NAME).then(function(cache) { cache.put(request, clone); });
                return response;
            }).catch(function() {
                return caches.match(request).then(function(cached) {
                    return cached || caches.match(OFFLINE_URL);
                });
            })
        );
        return;
    }
});

// Push notifications for new chats
self.addEventListener('push', function(event) {
    var data = {};
    try { data = event.data.json(); } catch(e) { data = {title: 'LiveVisitorHub', body: event.data ? event.data.text() : 'New notification'}; }

    event.waitUntil(
        self.registration.showNotification(data.title || 'LiveVisitorHub', {
            body: data.body || 'You have a new notification',
            icon: '/static/images/icon-192.png',
            badge: '/static/images/icon-192.png',
            tag: data.tag || 'livetrack-notification',
            data: { url: data.url || '/dashboard/' },
            vibrate: [200, 100, 200],
        })
    );
});

// Handle notification click
self.addEventListener('notificationclick', function(event) {
    event.notification.close();
    var url = event.notification.data && event.notification.data.url ? event.notification.data.url : '/dashboard/';
    event.waitUntil(
        self.clients.matchAll({type: 'window'}).then(function(clients) {
            for (var i = 0; i < clients.length; i++) {
                if (clients[i].url.includes('/dashboard/') && 'focus' in clients[i]) {
                    clients[i].navigate(url);
                    return clients[i].focus();
                }
            }
            return self.clients.openWindow(url);
        })
    );
});

