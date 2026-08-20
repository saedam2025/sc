(() => {
    'use strict';
    const cfg = window.SAEDAM_CHAT_PUSH_CONFIG || {};
    let reg = null;
    let subscribed = false;
    const emit = () => window.dispatchEvent(new CustomEvent('chat-push-state-changed', {detail:{subscribed}}));
    const supported = () => 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
    const ios = () => /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    const standalone = () => window.matchMedia?.('(display-mode: standalone)').matches || navigator.standalone === true;
    function manifest(){ if(cfg.manifestUrl && !document.querySelector('link[rel="manifest"]')){ const l=document.createElement('link'); l.rel='manifest'; l.href=cfg.manifestUrl; document.head.appendChild(l); } }
    function keyArray(s){ const p='='.repeat((4-s.length%4)%4); const b=(s+p).replace(/-/g,'+').replace(/_/g,'/'); const raw=atob(b); return Uint8Array.from([...raw].map(c=>c.charCodeAt(0))); }
    async function registration(){ if(reg) return reg; reg=await navigator.serviceWorker.register(cfg.swUrl,{scope:'/'}); await navigator.serviceWorker.ready; return reg; }
    async function save(sub){ const r=await fetch(cfg.subscribeUrl,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(sub.toJSON())}); const d=await r.json().catch(()=>({})); if(!r.ok) throw new Error(d.message||'푸시 구독 저장 실패'); }
    async function init(){ manifest(); if(!supported()){subscribed=false;emit();return false;} const r=await registration(); const s=await r.pushManager.getSubscription(); subscribed=!!s; if(s) await save(s).catch(console.warn); emit(); return subscribed; }
    async function enable(){ manifest(); if(!supported()) throw new Error('이 브라우저는 Web Push를 지원하지 않습니다.'); if(ios()&&!standalone()) throw new Error('아이폰/아이패드는 Safari 공유 버튼 → 홈 화면에 추가 후, 홈 화면의 새담인트라넷에서 알림을 켜주세요.'); let p=Notification.permission; if(p==='default') p=await Notification.requestPermission(); if(p!=='granted') throw new Error('브라우저 알림 권한을 허용해주세요.'); const r=await registration(); let s=await r.pushManager.getSubscription(); if(!s){ const kr=await fetch(cfg.publicKeyUrl,{credentials:'same-origin'}); const kd=await kr.json().catch(()=>({})); if(!kr.ok||!kd.public_key) throw new Error(kd.message||'VAPID 공개키 오류'); s=await r.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:keyArray(kd.public_key)}); } await save(s); subscribed=true; emit(); return true; }
    async function disable(){ const r=await registration(); const s=await r.pushManager.getSubscription(); if(s){ try{ await fetch(cfg.unsubscribeUrl,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({endpoint:s.endpoint})}); } finally { await s.unsubscribe(); } } subscribed=false; emit(); return false; }
    async function toggle(){ return subscribed ? disable() : enable(); }
    async function test(){ const r=await fetch(cfg.testUrl,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:'{}'}); const d=await r.json().catch(()=>({})); if(!r.ok) throw new Error(d.message||'테스트 실패'); return d; }
    window.SaedamChatPush={init,enable,disable,toggle,test,isSupported:supported,isSubscribed:()=>subscribed};
})();
