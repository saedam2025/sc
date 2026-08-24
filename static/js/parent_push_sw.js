self.addEventListener('install', event => {
    event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', event => {
    event.waitUntil(self.clients.claim());
});

self.addEventListener('push', event => {
    let data = {};
    try {
        data = event.data ? event.data.json() : {};
    } catch (error) {
        data = {body: event.data ? event.data.text() : '새담 방과후학교 알림이 도착했습니다.'};
    }
    event.waitUntil(self.registration.showNotification(data.title || '새담 방과후학교', {
        body: data.body || '새로운 알림이 도착했습니다.',
        tag: data.tag || 'saedam-parent-notice',
        renotify: true,
        icon: '/static/favicon.ico',
        badge: '/static/favicon.ico',
        data: {url: data.url || '/parent/'}
    }));
});

self.addEventListener('notificationclick', event => {
    event.notification.close();
    const target = new URL((event.notification.data || {}).url || '/parent/', self.location.origin).href;
    event.waitUntil((async () => {
        const windows = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
        for (const client of windows) {
            if (client.url === target && 'focus' in client) return client.focus();
        }
        if (self.clients.openWindow) return self.clients.openWindow(target);
    })());
});
