'use strict';

const assert = require('assert');

let registerCalls = 0;
let subscribeCalls = 0;
let updateCalls = 0;
let saveCalls = 0;

const subscription = {
    endpoint: 'https://push.example/subscription',
    options: {applicationServerKey: new Uint8Array([1, 2, 3]).buffer},
    toJSON() {
        return {endpoint: this.endpoint, keys: {p256dh: 'key', auth: 'auth'}};
    },
    unsubscribe: async () => true
};

const worker = {
    scope: 'https://intranet.example/',
    update: async () => { updateCalls += 1; },
    pushManager: {
        getSubscription: async () => null,
        subscribe: async () => {
            subscribeCalls += 1;
            if (subscribeCalls === 1) {
                const error = new Error('Registration failed - push service error');
                error.name = 'AbortError';
                throw error;
            }
            return subscription;
        }
    }
};

global.window = {
    SAEDAM_CHAT_PUSH_CONFIG: {
        swUrl: '/chat-push-sw.js',
        publicKeyUrl: '/api/chat/push/public-key',
        subscribeUrl: '/api/chat/push/subscribe',
        unsubscribeUrl: '/api/chat/push/unsubscribe',
        testUrl: '/api/chat/push/test',
        manifestUrl: '/static/saedam_manifest.webmanifest'
    },
    PushManager: function PushManager() {},
    isSecureContext: true,
    matchMedia: () => ({matches: false}),
    setTimeout: callback => callback(),
    dispatchEvent: () => undefined
};
global.CustomEvent = function CustomEvent(name, init) { this.type = name; this.detail = init.detail; };
global.document = {
    querySelector: () => ({rel: 'manifest'}),
    createElement: () => ({}),
    head: {appendChild: () => undefined}
};
Object.defineProperty(global, 'navigator', {configurable: true, value: {
    userAgent: 'Test Browser',
    platform: 'Win32',
    maxTouchPoints: 0,
    serviceWorker: {
        register: async () => {
            registerCalls += 1;
            if (registerCalls === 1) throw new Error('temporary worker failure');
            return worker;
        },
        ready: Promise.resolve(worker)
    }
}});
global.Notification = {
    permission: 'granted',
    requestPermission: async () => 'granted'
};
window.Notification = global.Notification;
global.fetch = async url => {
    if (url.indexOf('public-key') >= 0) {
        return {ok: true, json: async () => ({public_key: 'AQID'})};
    }
    if (url.indexOf('subscribe') >= 0) saveCalls += 1;
    return {ok: true, json: async () => ({status: 'success'})};
};

require('../static/js/chat_push.js');

(async () => {
    await assert.rejects(window.SaedamChatPush.init(), /temporary worker failure/);
    assert.strictEqual(registerCalls, 1, '첫 서비스워커 등록 실패가 확인되어야 한다.');

    const enabled = await window.SaedamChatPush.toggle();
    assert.strictEqual(enabled, true);
    assert.strictEqual(registerCalls, 2, '실패한 등록 Promise를 버리고 다음 클릭에서 재시도해야 한다.');
    assert.strictEqual(subscribeCalls, 2, '일시적인 push service 오류 뒤 한 번 재시도해야 한다.');
    assert.strictEqual(updateCalls, 1, '재시도 전 서비스워커를 갱신해야 한다.');
    assert.strictEqual(saveCalls, 1, '성공한 구독을 서버에 저장해야 한다.');
    assert.strictEqual(window.SaedamChatPush.isSubscribed(), true);
    assert.strictEqual(window.SaedamChatPush.isBusy(), false);
    console.log('chat push recovery test: ok');
})().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
