(function () {
    'use strict';

    const root = document.getElementById('globalSearch');
    if (!root) return;

    const input = document.getElementById('globalSearchInput');
    const panel = document.getElementById('globalSearchPanel');
    const resultsElement = document.getElementById('globalSearchResults');
    const summary = document.getElementById('globalSearchSummary');
    const endpoint = root.dataset.searchUrl;
    let debounceTimer = 0;
    let requestController = null;
    let selectedIndex = -1;
    let resultLinks = [];

    const openPanel = () => {
        panel.hidden = false;
        root.classList.add('is-open');
        input.setAttribute('aria-expanded', 'true');
    };

    const closePanel = () => {
        panel.hidden = true;
        root.classList.remove('is-open', 'is-loading');
        input.setAttribute('aria-expanded', 'false');
        setSelected(-1);
    };

    const emptyState = (icon, title, message) => {
        resultsElement.replaceChildren();
        const state = document.createElement('div');
        state.className = 'global-search-empty';
        const body = document.createElement('div');
        const iconElement = document.createElement('i');
        iconElement.className = `fa-solid ${icon}`;
        const strong = document.createElement('strong');
        strong.textContent = title;
        const span = document.createElement('span');
        span.textContent = message;
        body.append(iconElement, strong, span);
        state.append(body);
        resultsElement.append(state);
        resultLinks = [];
        setSelected(-1);
    };

    const setLoading = (loading) => {
        root.classList.toggle('is-loading', loading);
        input.setAttribute('aria-busy', String(loading));
    };

    const setSelected = (index) => {
        resultLinks.forEach((link) => link.classList.remove('is-selected'));
        if (!resultLinks.length || index < 0) {
            selectedIndex = -1;
            input.removeAttribute('aria-activedescendant');
            return;
        }
        selectedIndex = (index + resultLinks.length) % resultLinks.length;
        const active = resultLinks[selectedIndex];
        active.classList.add('is-selected');
        active.id = active.id || `global-search-result-${selectedIndex}`;
        input.setAttribute('aria-activedescendant', active.id);
        active.scrollIntoView({ block: 'nearest' });
    };

    const openResult = (link) => {
        if (!link) return;
        window.location.assign(link.getAttribute('href') || '');
    };

    const renderResult = (item, index) => {
        const link = document.createElement('a');
        link.className = 'global-search-result';
        link.href = item.url || '#';
        link.setAttribute('role', 'option');
        link.dataset.resultIndex = String(index);

        if (item.thumbnail) {
            const image = document.createElement('img');
            image.className = 'global-search-result-thumb';
            image.src = item.thumbnail;
            image.alt = '';
            image.loading = 'lazy';
            image.addEventListener('error', () => {
                const fallback = document.createElement('span');
                fallback.className = 'global-search-result-icon';
                const fallbackIcon = document.createElement('i');
                fallbackIcon.className = `fa-solid ${item.icon || 'fa-file'}`;
                fallback.append(fallbackIcon);
                image.replaceWith(fallback);
            }, { once: true });
            link.append(image);
        } else {
            const icon = document.createElement('span');
            icon.className = 'global-search-result-icon';
            const iconElement = document.createElement('i');
            iconElement.className = `fa-solid ${item.icon || 'fa-file'}`;
            icon.append(iconElement);
            link.append(icon);
        }

        const body = document.createElement('span');
        body.className = 'global-search-result-body';
        const titleRow = document.createElement('span');
        titleRow.className = 'global-search-result-title-row';
        const title = document.createElement('span');
        title.className = 'global-search-result-title';
        title.textContent = item.title || '제목 없음';
        const date = document.createElement('span');
        date.className = 'global-search-result-date';
        date.textContent = item.date || '';
        titleRow.append(title, date);
        body.append(titleRow);

        if (item.snippet) {
            const snippet = document.createElement('span');
            snippet.className = 'global-search-result-snippet';
            snippet.textContent = item.snippet;
            body.append(snippet);
        }
        if (item.meta) {
            const meta = document.createElement('span');
            meta.className = 'global-search-result-meta';
            meta.textContent = item.meta;
            body.append(meta);
        }
        link.append(body);
        link.addEventListener('mouseenter', () => setSelected(resultLinks.indexOf(link)));
        return link;
    };

    const renderResults = (payload) => {
        const items = Array.isArray(payload.results) ? payload.results : [];
        resultsElement.replaceChildren();
        resultLinks = [];
        setSelected(-1);

        if (!items.length) {
            summary.textContent = `“${payload.query || input.value.trim()}” 검색 결과 없음`;
            emptyState('fa-magnifying-glass', '검색 결과가 없습니다', '다른 단어나 문서 제목, 작성자 이름으로 검색해 보세요.');
            return;
        }

        summary.textContent = `${payload.source_count || 0}개 영역 · ${payload.total || items.length}건 표시`;
        const groups = new Map();
        items.forEach((item) => {
            const key = `${item.source}|${item.source_label}`;
            if (!groups.has(key)) groups.set(key, []);
            groups.get(key).push(item);
        });

        let globalIndex = 0;
        groups.forEach((groupItems) => {
            const group = document.createElement('section');
            group.className = 'global-search-group';
            const heading = document.createElement('div');
            heading.className = 'global-search-group-title';
            const icon = document.createElement('i');
            icon.className = `fa-solid ${groupItems[0].icon || 'fa-file'}`;
            const label = document.createElement('span');
            label.textContent = groupItems[0].source_label || '검색 결과';
            const count = document.createElement('span');
            count.className = 'global-search-group-count';
            count.textContent = String(groupItems.length);
            heading.append(icon, label, count);
            group.append(heading);
            groupItems.forEach((item) => {
                const link = renderResult(item, globalIndex++);
                resultLinks.push(link);
                group.append(link);
            });
            resultsElement.append(group);
        });
    };

    const search = async () => {
        const query = input.value.trim().replace(/\s+/g, ' ');
        openPanel();
        if (query.length < 2) {
            if (requestController) requestController.abort();
            setLoading(false);
            summary.textContent = '검색어를 2글자 이상 입력하세요.';
            emptyState('fa-keyboard', '무엇을 찾고 계신가요?', '게시글, 결재문서, 학교업무, 갤러리 등 일반 메뉴 데이터를 검색합니다.');
            return;
        }

        if (requestController) requestController.abort();
        requestController = new AbortController();
        setLoading(true);
        summary.textContent = `“${query}” 검색 중…`;
        try {
            const response = await fetch(`${endpoint}?q=${encodeURIComponent(query)}`, {
                headers: { Accept: 'application/json' },
                signal: requestController.signal,
                credentials: 'same-origin'
            });
            const payload = await response.json();
            if (!response.ok || payload.status !== 'success') {
                throw new Error(payload.message || '검색 결과를 불러오지 못했습니다.');
            }
            if (query !== input.value.trim().replace(/\s+/g, ' ')) return;
            renderResults(payload);
        } catch (error) {
            if (error.name === 'AbortError') return;
            summary.textContent = '검색 오류';
            emptyState('fa-triangle-exclamation', '검색할 수 없습니다', error.message || '잠시 후 다시 시도해주세요.');
        } finally {
            setLoading(false);
        }
    };

    input.addEventListener('focus', () => {
        openPanel();
        if (!resultsElement.children.length) search();
    });

    input.addEventListener('input', () => {
        window.clearTimeout(debounceTimer);
        debounceTimer = window.setTimeout(search, 260);
    });

    input.addEventListener('keydown', (event) => {
        if (event.key === 'ArrowDown') {
            event.preventDefault();
            setSelected(selectedIndex + 1);
        } else if (event.key === 'ArrowUp') {
            event.preventDefault();
            setSelected(selectedIndex <= 0 ? resultLinks.length - 1 : selectedIndex - 1);
        } else if (event.key === 'Enter' && selectedIndex >= 0) {
            event.preventDefault();
            openResult(resultLinks[selectedIndex]);
        } else if (event.key === 'Escape') {
            event.preventDefault();
            closePanel();
            input.blur();
        }
    });

    document.addEventListener('keydown', (event) => {
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
            event.preventDefault();
            input.focus();
            input.select();
            openPanel();
        }
    });

    document.addEventListener('pointerdown', (event) => {
        if (!root.contains(event.target)) closePanel();
    });
})();
