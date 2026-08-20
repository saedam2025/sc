self.addEventListener('push', event => {
    let d={};
    try { d=event.data ? event.data.json() : {}; }
    catch(e) { d={body:event.data ? event.data.text() : '새 메시지가 도착했습니다.'}; }
    event.waitUntil((async()=>{
        const ws=await self.clients.matchAll({type:'window',includeUncontrolled:true});
        if(ws.some(c=>c.visibilityState==='visible')) return;
        await self.registration.showNotification(d.title||'새담 사내메신저',{
            body:d.body||'새 메시지가 도착했습니다.', tag:d.tag||'saedam-chat', renotify:true,
            data:{url:d.url||'/'}
        });
    })());
});
self.addEventListener('notificationclick', event => {
    event.notification.close();
    const u=new URL(event.notification.data?.url||'/',self.location.origin).href;
    event.waitUntil((async()=>{
        const ws=await self.clients.matchAll({type:'window',includeUncontrolled:true});
        for(const c of ws){ if(c.url===u && 'focus' in c) return c.focus(); }
        if(self.clients.openWindow) return self.clients.openWindow(u);
    })());
});
