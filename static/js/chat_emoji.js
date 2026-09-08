/* Local, allowlisted sticker assets. Message text is never interpreted as HTML. */
(() => {
    'use strict';
    const entries = window.SAEDAM_CHAT_EMOJIS || [];
    const byId = new Map(entries.map(entry => [entry.id, entry]));
    const packs = {
        noto: {token:'움직이는', title:' · 움직임', aria:' 움직이는 이모티콘 선택', retired:true},
        openmoji: {token:'컬러', title:'', aria:' 이모티콘 선택', retired:true},
        saedamgirl: {token:'새담걸', title:' · 새담걸', aria:' 새담걸 이모티콘 선택'},
        office: {token:'사무용품', title:' · 사무용품', aria:' 사무용품 이모티콘 선택'},
        vehicle: {token:'탈것', title:' · 탈것', aria:' 탈것 이모티콘 선택'}
    };
    const packNames = Object.keys(packs);
    /* 이모티콘 탭에는 사내에서 만든 팩만 올린다. 물러난 팩은 목록에만 남아,
       그 스티커를 이미 주고받은 지난 대화가 글자로 깨지지 않게 한다. */
    const pickerPacks = packNames.filter(pack => !packs[pack].retired);
    const escape = value => String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
    const token = (entry, pack) => `[${packs[pack].token} 이모티콘: ${entry.label}]`;
    const tokenMap = new Map();
    entries.forEach(entry => packNames.forEach(pack => {
        if (entry[pack]) tokenMap.set(token(entry, pack), {entry, pack});
    }));
    const tokenKinds = [...new Set(Object.values(packs).map(pack => pack.token))].join('|');
    const byToken = new RegExp(`\\[(?:${tokenKinds}) 이모티콘: [^\\]\\r\\n]{1,110}\\]`, 'g');
    const art = (entry, pack) => `<span class="emoji-art emoji-art--${pack}"><img src="${escape(entry[pack].src)}" alt="${escape(entry.label)}" draggable="false" loading="lazy" decoding="async" width="112" height="112"><span aria-hidden="true">${entry.emoji}</span></span>`;
    const state = {tab:'sticker', category:'전체', selected:null, recent:[], windowSize:null};
    let filteredItems = [], renderedCount = 0, mobileCloseTimer = null;
    const PAGE_SIZE = 84;
    const storageKey = 'saedam.chat.emoji.recent.v1';
    const windowSizeStorageKey = 'saedam.chat.emoji.window-size.v1';
    try {
        const saved = JSON.parse(localStorage.getItem(storageKey) || '[]');
        if (Array.isArray(saved)) state.recent = saved.filter(item => item && byId.has(item.id) && ['mini', ...packNames].includes(item.pack) && (item.pack === 'mini' || byId.get(item.id)[item.pack])).slice(0,32);
    } catch (_) { /* Storage can be disabled by the browser. */ }
    const el = id => document.getElementById(id);

    function savedWindowSize() {
        try {
            const value = JSON.parse(sessionStorage.getItem(windowSizeStorageKey) || 'null');
            return value && ['width','height','x','y','expandedWidth'].every(key => Number.isFinite(value[key])) ? value : null;
        } catch (_) { return null; }
    }

    function storeWindowSize(value) {
        try { sessionStorage.setItem(windowSizeStorageKey, JSON.stringify(value)); } catch (_) {}
    }

    function forgetWindowSize() {
        try { sessionStorage.removeItem(windowSizeStorageKey); } catch (_) {}
    }

    function resizeToOriginal(original) {
        if (!original || Math.abs(window.outerWidth - original.expandedWidth) >= 40) return false;
        try {
            window.resizeTo(original.width, original.height);
            window.moveTo(original.x, original.y);
            return true;
        } catch (_) { return false; }
    }

    function restoreWindowSize() {
        const original = state.windowSize || savedWindowSize();
        state.windowSize = null;
        resizeToOriginal(original);
        forgetWindowSize();
    }

    function remember(entry, pack) {
        state.recent = [{id:entry.id, pack}, ...state.recent.filter(item => item.id !== entry.id || item.pack !== pack)].slice(0,32);
        try { localStorage.setItem(storageKey, JSON.stringify(state.recent)); } catch (_) {}
    }

    function renderCategories() {
        const available = state.tab === 'mini'
            ? entries.filter(entry => entry.mini !== false)
            : entries.filter(entry => pickerPacks.some(pack => entry[pack]));
        const categories = ['전체', '최근', ...new Set(available.map(entry => entry.category))];
        if (!categories.includes(state.category)) state.category = '전체';
        el('emojiCategories').replaceChildren();
        categories.forEach(category => {
            const button = document.createElement('button');
            button.type = 'button';
            button.textContent = category;
            button.setAttribute('aria-pressed', String(category === state.category));
            button.onclick = () => { state.category = category; renderCategories(); render(); };
            el('emojiCategories').appendChild(button);
        });
    }

    function render() {
        const mini = state.tab === 'mini';
        let items = mini ? entries.filter(entry => entry.mini !== false).map(entry => ({entry, pack:'mini'}))
            : entries.flatMap(entry => pickerPacks.filter(pack => entry[pack]).map(pack => ({entry, pack})));
        if (state.category === '최근') {
            items = state.recent.filter(item => mini ? item.pack === 'mini' : pickerPacks.includes(item.pack))
                .map(item => ({entry:byId.get(item.id), pack:item.pack}));
        } else if (state.category !== '전체') items = items.filter(item => item.entry.category === state.category);
        filteredItems = items;
        el('emojiResultsTitle').textContent = state.category;
        el('emojiResultCount').textContent = `${filteredItems.length.toLocaleString('ko-KR')}개`;
        const grid = el('emojiResults');
        grid.classList.toggle('is-mini', mini);
        grid.setAttribute('aria-labelledby', mini ? 'emojiMiniTab' : 'emojiStickerTab');
        grid.replaceChildren();
        renderedCount = 0;
        appendResults();
        if (!filteredItems.length) {
            const empty = document.createElement('p');
            empty.className = 'emoji-empty';
            empty.textContent = state.category === '최근' ? '사용한 이모티콘이 여기에 모여요.' : '표시할 이모티콘이 없어요.';
            grid.appendChild(empty);
        }
        grid.scrollTop = 0;
    }

    function updateSelection() {
        el('emojiResults').querySelectorAll('.emoji-tile').forEach(button => {
            button.setAttribute('aria-pressed', String(!!state.selected && button.dataset.emojiId === state.selected.entry.id && button.dataset.emojiPack === state.selected.pack));
        });
    }

    function chatIsNearBottom() {
        const box = el('chatBox');
        return !box || box.scrollHeight - box.scrollTop - box.clientHeight < 90;
    }

    function keepChatAtBottom(shouldKeep) {
        if (!shouldKeep) return;
        requestAnimationFrame(() => {
            const box = el('chatBox');
            if (box) box.scrollTop = box.scrollHeight;
        });
    }

    function appendResults() {
        const mini = state.tab === 'mini';
        const grid = el('emojiResults');
        const fragment = document.createDocumentFragment();
        filteredItems.slice(renderedCount, renderedCount + PAGE_SIZE).forEach(({entry, pack:itemPack}) => {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'emoji-tile';
            button.dataset.emojiId = entry.id;
            button.dataset.emojiPack = itemPack;
            button.title = entry.label + (mini ? '' : packs[itemPack].title + ' · 더블클릭 즉시 전송');
            button.setAttribute('aria-label', entry.label + (mini ? ' 삽입' : packs[itemPack].aria));
            button.setAttribute('aria-pressed', String(!!state.selected && state.selected.entry.id === entry.id && state.selected.pack === itemPack));
            if (mini) button.textContent = entry.emoji;
            else button.innerHTML = art(entry, itemPack) + `<span>${escape(entry.label)}</span>`;
            button.onclick = () => {
                if (mini) {
                    const input = el('chatInput');
                    input.setRangeText(entry.emoji, input.selectionStart, input.selectionEnd, 'end');
                    input.dispatchEvent(new Event('input', {bubbles:true}));
                    input.focus();
                    remember(entry, 'mini');
                } else {
                    const keepBottom = chatIsNearBottom();
                    state.selected = {entry, pack:itemPack};
                    el('chatEmojiSelection').hidden = false;
                    el('emojiSelectionArt').innerHTML = art(entry, itemPack);
                    if (window.innerWidth <= 700) {
                        clearTimeout(mobileCloseTimer);
                        mobileCloseTimer = setTimeout(() => { close(); el('chatInput').focus(); }, 280);
                    } else {
                        updateSelection();
                        el('chatInput').focus();
                    }
                    keepChatAtBottom(keepBottom);
                }
            };
            if (!mini) button.ondblclick = event => {
                event.preventDefault();
                clearTimeout(mobileCloseTimer);
                mobileCloseTimer = null;
                if (window.innerWidth <= 700 && !el('chatEmojiPicker').hidden) {
                    close();
                    el('chatInput').focus();
                }
                if (typeof window.sendChatReply === 'function') window.sendChatReply({stickerOnly:true});
            };
            fragment.appendChild(button);
        });
        renderedCount = Math.min(filteredItems.length, renderedCount + PAGE_SIZE);
        grid.appendChild(fragment);
    }

    function close() {
        clearTimeout(mobileCloseTimer);
        mobileCloseTimer = null;
        el('chatEmojiPicker').hidden = true;
        document.body.classList.remove('emoji-panel-open');
        el('emojiToggle').setAttribute('aria-expanded', 'false');
        restoreWindowSize();
        el('emojiToggle').focus();
    }

    function toggle() {
        if (!el('chatEmojiPicker').hidden) return close();
        if (window.opener && window.innerWidth <= 700) {
            const original = {width:window.outerWidth, height:window.outerHeight, x:window.screenX, y:window.screenY};
            const newWidth = Math.min(original.width + 320, window.screen.availWidth);
            if (newWidth > 700) {
                const screenLeft = window.screen.availLeft || 0;
                try {
                    window.resizeTo(newWidth, original.height);
                    if (window.outerWidth > original.width + 100) {
                        state.windowSize = {...original, expandedWidth:window.outerWidth};
                        storeWindowSize(state.windowSize);
                        window.moveTo(Math.max(screenLeft, original.x - (window.outerWidth - original.width)), original.y);
                    }
                } catch (_) { /* Tabs and mobile browsers use the left drawer. */ }
            }
        }
        el('chatEmojiPicker').hidden = false;
        document.body.classList.add('emoji-panel-open');
        el('emojiToggle').setAttribute('aria-expanded', 'true');
        renderCategories(); render();
        el('emojiResults').focus();
    }

    function clear() {
        const keepBottom = chatIsNearBottom();
        state.selected = null;
        el('chatEmojiSelection').hidden = true;
        el('emojiSelectionArt').replaceChildren();
        if (!el('chatEmojiPicker').hidden) updateSelection();
        keepChatAtBottom(keepBottom);
    }

    window.ChatEmoji = {
        toggle, close, clear,
        compose(text) { return [state.selected ? token(state.selected.entry, state.selected.pack) : '', text].filter(Boolean).join('\n'); },
        hasSticker(text) { return Array.from(String(text || '').matchAll(byToken)).some(match => tokenMap.has(match[0])); },
        sent(content) {
            const current = state.selected;
            if (current && content.includes(token(current.entry, current.pack))) {
                remember(current.entry, current.pack); clear();
            }
        },
        renderText(text) {
            const source = String(text || '');
            const sticker = this.hasSticker(source);
            /* 스티커는 한 줄을 통째로 차지하므로 앞뒤에 붙은 줄바꿈은 버린다. */
            const chunk = (raw, trimStart, trimEnd) => {
                let value = raw;
                if (sticker && trimStart) value = value.replace(/^[\r\n]+/, '');
                if (sticker && trimEnd) value = value.replace(/[\r\n]+$/, '');
                if (!value) return '';
                const body = escape(value).replace(/\n/g, '<br>');
                return sticker ? `<span class="chat-text">${body}</span>` : body;
            };
            let output = '', offset = 0, index = 0;
            for (const match of source.matchAll(byToken)) {
                output += chunk(source.slice(offset, match.index), index > 0, true);
                const known = tokenMap.get(match[0]);
                output += known ? `<span class="chat-sticker" title="${escape(known.entry.label)}">${art(known.entry, known.pack)}</span>` : escape(match[0]);
                offset = match.index + match[0].length;
                index += 1;
            }
            return output + chunk(source.slice(offset), index > 0, false);
        }
    };

    document.addEventListener('DOMContentLoaded', () => {
        if (el('chatEmojiPicker').hidden && savedWindowSize()) restoreWindowSize();
        el('emojiClose').addEventListener('click', close);
        el('emojiSelectionCancel').addEventListener('click', clear);
        el('emojiResults').addEventListener('scroll', () => {
            const grid = el('emojiResults');
            if (grid.scrollHeight - grid.scrollTop - grid.clientHeight < 280 && renderedCount < filteredItems.length) appendResults();
        }, {passive:true});
        document.querySelectorAll('[data-emoji-tab]').forEach(button => {
            button.addEventListener('click', () => {
                state.tab = button.dataset.emojiTab;
                document.querySelectorAll('[data-emoji-tab]').forEach(tab => {
                    tab.setAttribute('aria-selected', String(tab === button));
                    tab.tabIndex = tab === button ? 0 : -1;
                });
                renderCategories(); render();
            });
            button.addEventListener('keydown', event => {
                if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
                event.preventDefault();
                const tabs = Array.from(document.querySelectorAll('[data-emoji-tab]'));
                const next = event.key === 'Home' ? tabs[0] : event.key === 'End' ? tabs[1] : tabs.find(tab => tab !== button);
                next.click(); next.focus();
            });
        });
        document.addEventListener('keydown', event => {
            if (event.key !== 'Escape') return;
            let handled = false;
            if (state.selected) { clear(); handled = true; }
            if (!el('chatEmojiPicker').hidden) { close(); handled = true; }
            if (handled) {
                event.preventDefault();
                el('chatInput').focus();
            }
        });
        document.addEventListener('click', event => {
            if (window.innerWidth <= 700 && !el('chatEmojiPicker').hidden && !el('chatEmojiPicker').contains(event.target) && !el('emojiToggle').contains(event.target)) close();
        });
        document.addEventListener('dragstart', event => {
            if (event.target.matches && event.target.matches('.emoji-art img')) event.preventDefault();
        });
        document.addEventListener('error', event => {
            if (event.target.matches && event.target.matches('.emoji-art img')) event.target.parentElement.classList.add('failed');
        }, true);
    });
    window.addEventListener('beforeunload', () => resizeToOriginal(state.windowSize || savedWindowSize()));
})();
