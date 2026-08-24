(function () {
    'use strict';

    var cfg = window.SAEDAM_CHAT_PUSH_CONFIG || {};
    var activeRegistration = null;
    var registrationPromise = null;
    var actionPromise = null;
    var subscribed = false;
    var busy = false;

    function emit(error) {
        window.dispatchEvent(new CustomEvent('chat-push-state-changed', {
            detail: {
                subscribed: subscribed,
                busy: busy,
                error: error ? String(error.message || error) : ''
            }
        }));
    }

    function supported() {
        return 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
    }

    function isIos() {
        return /iPad|iPhone|iPod/.test(navigator.userAgent) ||
            (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    }

    function isStandalone() {
        var media = window.matchMedia ? window.matchMedia('(display-mode: standalone)') : null;
        return !!((media && media.matches) || navigator.standalone === true);
    }

    function ensureManifest() {
        if (!cfg.manifestUrl || document.querySelector('link[rel="manifest"]')) return;
        var link = document.createElement('link');
        link.rel = 'manifest';
        link.href = cfg.manifestUrl;
        document.head.appendChild(link);
    }

    function keyArray(value) {
        var key = String(value || '').trim();
        var padding = '='.repeat((4 - key.length % 4) % 4);
        var base64 = (key + padding).replace(/-/g, '+').replace(/_/g, '/');
        var raw = atob(base64);
        var result = new Uint8Array(raw.length);
        for (var i = 0; i < raw.length; i += 1) result[i] = raw.charCodeAt(i);
        return result;
    }

    function sameApplicationServerKey(subscription, expectedKey) {
        var options = subscription && subscription.options;
        var current = options && options.applicationServerKey;
        if (!current) return true;
        var actual = new Uint8Array(current);
        if (actual.length !== expectedKey.length) return false;
        for (var i = 0; i < actual.length; i += 1) {
            if (actual[i] !== expectedKey[i]) return false;
        }
        return true;
    }

    function readableError(error, stage) {
        var name = String(error && error.name || '');
        var message = String(error && error.message || error || '');
        var lower = message.toLowerCase();

        if (name === 'NotAllowedError' || name === 'PermissionDeniedError') {
            return new Error('브라우저의 이 사이트 알림 권한이 차단되어 있습니다. 주소창 왼쪽 사이트 설정에서 알림을 허용한 뒤 다시 눌러주세요.');
        }
        if (name === 'InvalidCharacterError' || lower.indexOf('applicationserverkey') >= 0) {
            return new Error('서버의 VAPID 공개키 형식이 올바르지 않습니다. 관리자에게 Render 환경변수 설정 확인을 요청해주세요.');
        }
        if (
            name === 'AbortError' ||
            lower.indexOf('push service') >= 0 ||
            lower.indexOf('registration failed') >= 0
        ) {
            return new Error('브라우저 푸시 서비스에 연결하지 못했습니다. 브라우저를 최신 버전으로 업데이트하고 완전히 종료한 뒤 다시 실행해주세요. 계속 실패하면 사내 방화벽·프록시·보안 프로그램이 Chrome/Edge 푸시 서비스(FCM/WNS)를 차단하는지 확인해주세요.');
        }
        if (name === 'InvalidStateError') {
            return new Error('이 PC에 저장된 푸시 구독 상태가 손상되었습니다. 브라우저를 완전히 종료한 뒤 다시 시도해주세요.');
        }
        if (name === 'NetworkError' || lower.indexOf('network') >= 0) {
            return new Error(stage === 'worker'
                ? '푸시 서비스워커를 불러오지 못했습니다. 인터넷 연결과 사이트 인증서(HTTPS)를 확인한 뒤 새로고침해주세요.'
                : '서버와 통신하지 못했습니다. 인터넷 연결을 확인한 뒤 다시 시도해주세요.');
        }
        return new Error(message || '휴대폰 푸시 알림 설정에 실패했습니다.');
    }

    async function registration() {
        if (activeRegistration) return activeRegistration;
        if (registrationPromise) return registrationPromise;
        if (!window.isSecureContext) {
            throw new Error('푸시 알림은 HTTPS 보안 주소에서만 사용할 수 있습니다.');
        }

        registrationPromise = (async function () {
            try {
                var registered = await navigator.serviceWorker.register(cfg.swUrl, {scope: '/'});
                var ready = await navigator.serviceWorker.ready;
                activeRegistration = ready.scope === registered.scope ? ready : registered;
                return activeRegistration;
            } catch (error) {
                activeRegistration = null;
                throw readableError(error, 'worker');
            } finally {
                registrationPromise = null;
            }
        }());
        return registrationPromise;
    }

    async function jsonFetch(url, options, fallbackMessage) {
        var response = await fetch(url, options);
        var data = await response.json().catch(function () { return {}; });
        if (!response.ok) throw new Error(data.message || fallbackMessage);
        return data;
    }

    function save(subscription) {
        return jsonFetch(cfg.subscribeUrl, {
            method: 'POST',
            credentials: 'same-origin',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(subscription.toJSON())
        }, '푸시 구독을 서버에 저장하지 못했습니다.');
    }

    function removeFromServer(endpoint) {
        if (!endpoint) return Promise.resolve();
        return fetch(cfg.unsubscribeUrl, {
            method: 'POST',
            credentials: 'same-origin',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({endpoint: endpoint})
        }).then(function () { return undefined; });
    }

    async function publicKey() {
        var data = await jsonFetch(cfg.publicKeyUrl, {
            credentials: 'same-origin',
            cache: 'no-store'
        }, 'VAPID 공개키를 불러오지 못했습니다.');
        if (!data.public_key) throw new Error('VAPID 공개키가 설정되지 않았습니다.');
        try {
            return keyArray(data.public_key);
        } catch (error) {
            throw readableError(error, 'key');
        }
    }

    async function createSubscription(worker, applicationServerKey) {
        try {
            return await worker.pushManager.subscribe({
                userVisibleOnly: true,
                applicationServerKey: applicationServerKey
            });
        } catch (firstError) {
            var text = String(firstError && firstError.message || '').toLowerCase();
            var retryable = (firstError && firstError.name === 'AbortError') ||
                text.indexOf('push service') >= 0 || text.indexOf('registration failed') >= 0;
            if (!retryable) throw readableError(firstError, 'subscribe');

            try { await worker.update(); } catch (ignore) { /* subscribe 결과로 판정 */ }
            await new Promise(function (resolve) { window.setTimeout(resolve, 350); });
            try {
                return await worker.pushManager.subscribe({
                    userVisibleOnly: true,
                    applicationServerKey: applicationServerKey
                });
            } catch (secondError) {
                throw readableError(secondError, 'subscribe');
            }
        }
    }

    async function init() {
        ensureManifest();
        if (!supported()) {
            subscribed = false;
            emit();
            return false;
        }
        try {
            var worker = await registration();
            var subscription = await worker.pushManager.getSubscription();
            subscribed = !!subscription;
            if (subscription) await save(subscription).catch(function (error) { console.warn(error); });
            emit();
            return subscribed;
        } catch (error) {
            subscribed = false;
            emit(error);
            throw error;
        }
    }

    async function enable() {
        ensureManifest();
        if (!supported()) throw new Error('이 브라우저는 Web Push를 지원하지 않습니다. Chrome, Edge, Firefox 또는 모바일 Safari 최신 버전을 사용해주세요.');
        if (!window.isSecureContext) throw new Error('푸시 알림은 HTTPS 보안 주소에서만 사용할 수 있습니다.');
        if (isIos() && !isStandalone()) throw new Error('아이폰/아이패드는 Safari 공유 버튼 → 홈 화면에 추가 후, 홈 화면의 새담인트라넷에서 알림을 켜주세요.');

        var permission = Notification.permission;
        if (permission === 'default') permission = await Notification.requestPermission();
        if (permission !== 'granted') throw readableError({name: 'NotAllowedError'}, 'permission');

        var worker = await registration();
        var applicationServerKey = await publicKey();
        var subscription = await worker.pushManager.getSubscription();

        if (subscription && !sameApplicationServerKey(subscription, applicationServerKey)) {
            await removeFromServer(subscription.endpoint).catch(function () { return undefined; });
            await subscription.unsubscribe();
            subscription = null;
        }
        if (!subscription) subscription = await createSubscription(worker, applicationServerKey);
        await save(subscription);
        subscribed = true;
        return true;
    }

    async function disable() {
        var worker = await registration();
        var subscription = await worker.pushManager.getSubscription();
        if (subscription) {
            try {
                await removeFromServer(subscription.endpoint);
            } finally {
                await subscription.unsubscribe();
            }
        }
        subscribed = false;
        return false;
    }

    async function toggle() {
        if (actionPromise) return actionPromise;
        busy = true;
        emit();
        actionPromise = subscribed ? disable() : enable();
        try {
            var result = await actionPromise;
            emit();
            return result;
        } catch (error) {
            emit(error);
            throw error;
        } finally {
            busy = false;
            actionPromise = null;
            emit();
        }
    }

    function test() {
        return jsonFetch(cfg.testUrl, {
            method: 'POST',
            credentials: 'same-origin',
            headers: {'Content-Type': 'application/json'},
            body: '{}'
        }, '테스트 푸시 전송에 실패했습니다.');
    }

    window.SaedamChatPush = {
        init: init,
        enable: enable,
        disable: disable,
        toggle: toggle,
        test: test,
        isSupported: supported,
        isSubscribed: function () { return subscribed; },
        isBusy: function () { return busy; }
    };
}());
