const CHAT_NOTIFY_ICON = '/static/chat_notify_icon.png';
const CHAT_NOTIFY_ACTIONS = [
    { action: 'open', title: '답장하기' },
    { action: 'read', title: '읽음 처리' }
];

self.addEventListener('install', event => {
    event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', event => {
    event.waitUntil(self.clients.claim());
});

self.addEventListener('push', event => {
    let d={};
    try { d=event.data ? event.data.json() : {}; }
    catch(e) { d={body:event.data ? event.data.text() : '새 메시지가 도착했습니다.'}; }
    event.waitUntil((async()=>{
        const ws=await self.clients.matchAll({type:'window',includeUncontrolled:true});
        if(ws.some(c=>c.visibilityState==='visible')) return;
        await self.registration.showNotification(d.title||'새담 사내메신저',{
            body:d.body||'새 메시지가 도착했습니다.', tag:d.tag||'saedam-chat', renotify:true,
            icon:d.icon||CHAT_NOTIFY_ICON, badge:CHAT_NOTIFY_ICON,
            actions:CHAT_NOTIFY_ACTIONS, requireInteraction:true,
            data:{url:d.url||'/', partner:d.partner||''}
        });
    })());
});

// 이미 열려 있는 대화창 팝업인지 판별한다.
function isChatPopupClient(client) {
    try { return new URL(client.url).pathname.startsWith('/chat_popup/'); }
    catch (e) { return false; }
}

// 알림 본문을 누르거나 [답장하기]를 누르면 대화창을 열고,
// [읽음 처리]를 누르면 창을 열지 않고 읽음만 표시한다.
self.addEventListener('notificationclick', event => {
    event.notification.close();
    const data=event.notification.data||{};
    const partner=data.partner||'';

    if(event.action==='read'){
        event.waitUntil((async()=>{
            if(partner){
                try{
                    await fetch('/api/chat/mark-read',{
                        method:'POST', credentials:'include',
                        headers:{'Content-Type':'application/json'},
                        body:JSON.stringify({partner:partner})
                    });
                }catch(e){ /* 오프라인이면 다음 접속 때 목록에서 정리된다 */ }
            }
            const ws=await self.clients.matchAll({type:'window',includeUncontrolled:true});
            for(const c of ws){ c.postMessage({type:'chat-read', partner:partner}); }
        })());
        return;
    }

    const u=new URL(data.url||'/',self.location.origin).href;
    event.waitUntil((async()=>{
        const ws=await self.clients.matchAll({type:'window',includeUncontrolled:true});

        // 1) 이미 그 대화창이 떠 있으면 그 창을 앞으로 가져온다.
        for(const c of ws){ if(c.url===u && 'focus' in c) return c.focus(); }

        // 2) 메인화면이 열려 있으면 거기서 열게 한다.
        //    openWindow()는 창 크기를 지정할 수 없어 전체 탭으로 열리기 때문이다.
        const mainClient=ws.find(c=>!isChatPopupClient(c));
        if(mainClient && partner){
            mainClient.postMessage({type:'chat-open', partner:partner});
            if('focus' in mainClient) await mainClient.focus();
            return;
        }

        // 3) 열린 창이 하나도 없을 때만 새 창으로 연다.
        if(self.clients.openWindow) return self.clients.openWindow(u);
    })());
});
