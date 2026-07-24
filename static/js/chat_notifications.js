(function (window) {
    'use strict';

    const STORAGE_KEY = 'saedam.chat.alerts.enabled';
    const RECENT_MESSAGE_TTL_MS = 5000;
    const recentMessages = new Map();
    let audioContext = null;
    let inMemoryEnabled = true;

    function readEnabled() {
        try {
            inMemoryEnabled = window.localStorage.getItem(STORAGE_KEY) !== 'false';
        } catch (error) {
            // localStorage가 차단된 브라우저에서는 현재 화면의 설정을 유지한다.
        }
        return inMemoryEnabled;
    }

    function writeEnabled(enabled) {
        const nextValue = !!enabled;
        inMemoryEnabled = nextValue;
        try {
            window.localStorage.setItem(STORAGE_KEY, String(nextValue));
        } catch (error) {
            // 저장소가 차단된 환경에서도 현재 화면의 알림은 계속 동작한다.
        }
        window.dispatchEvent(new CustomEvent('chat-alert-setting-changed', {
            detail: { enabled: nextValue }
        }));
        return nextValue;
    }

    function getAudioContext() {
        if (audioContext) return audioContext;
        const AudioContext = window.AudioContext || window.webkitAudioContext;
        if (!AudioContext) return null;
        audioContext = new AudioContext();
        return audioContext;
    }

    function prime() {
        const context = getAudioContext();
        if (!context || context.state !== 'suspended') return;
        context.resume().catch(function () {});
    }

    function scheduleChime(context) {
        const now = context.currentTime + 0.015;
        const notes = [
            { frequency: 659.25, start: 0, duration: 0.14, volume: 0.16 },
            { frequency: 987.77, start: 0.13, duration: 0.22, volume: 0.13 }
        ];

        notes.forEach(function (note) {
            const oscillator = context.createOscillator();
            const gain = context.createGain();
            const noteStart = now + note.start;
            const noteEnd = noteStart + note.duration;

            oscillator.type = 'sine';
            oscillator.frequency.setValueAtTime(note.frequency, noteStart);
            gain.gain.setValueAtTime(0.0001, noteStart);
            gain.gain.exponentialRampToValueAtTime(note.volume, noteStart + 0.018);
            gain.gain.exponentialRampToValueAtTime(0.0001, noteEnd);
            oscillator.connect(gain);
            gain.connect(context.destination);
            oscillator.start(noteStart);
            oscillator.stop(noteEnd + 0.02);
        });
    }

    function playChime(force) {
        if (!force && !readEnabled()) return false;
        const context = getAudioContext();
        if (!context) return false;

        const play = function () {
            scheduleChime(context);
            return true;
        };
        if (context.state === 'suspended') {
            context.resume().then(play).catch(function () {});
            return true;
        }
        return play();
    }

    function claimMessage(messageKey) {
        const now = Date.now();
        recentMessages.forEach(function (timestamp, key) {
            if (now - timestamp > RECENT_MESSAGE_TTL_MS) recentMessages.delete(key);
        });
        if (!messageKey) return true;
        if (recentMessages.has(messageKey)) return false;
        recentMessages.set(messageKey, now);
        return true;
    }

    function notifyIncoming(event, options) {
        const settings = options || {};
        if (!event || settings.muted || !readEnabled()) return false;
        const messageKey = String(event.partner || '') + ':' + String(event.message_id || '');
        if (!claimMessage(messageKey)) return false;
        playChime(false);
        return true;
    }

    window.addEventListener('storage', function (event) {
        if (event.key === STORAGE_KEY) {
            window.dispatchEvent(new CustomEvent('chat-alert-setting-changed', {
                detail: { enabled: readEnabled() }
            }));
        }
    });
    window.addEventListener('pointerdown', prime, { once: true, passive: true });
    window.addEventListener('keydown', prime, { once: true });

    window.ChatMessageAlerts = {
        isEnabled: readEnabled,
        setEnabled: writeEnabled,
        toggle: function () {
            const enabled = writeEnabled(!readEnabled());
            if (enabled) {
                prime();
                playChime(true);
            }
            return enabled;
        },
        notifyIncoming: notifyIncoming,
        playPreview: function () {
            prime();
            return playChime(true);
        },
        prime: prime
    };
})(window);
