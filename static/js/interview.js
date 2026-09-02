(function () {
    'use strict';

    const byId = (id) => document.getElementById(id);
    const candidateModal = byId('candidateModal');
    // 상단 메뉴바(.navbar)와 본문(.content-container)이 서로 다른 stacking context라
    // 모달을 body 바로 아래로 옮겨야 메뉴 위에 덮인다.
    if (candidateModal) document.body.appendChild(candidateModal);
    const candidateList = byId('candidateList');
    const searchInput = byId('searchInput');
    const MAX_ATTACHMENTS = Number(window.IV_MAX_ATTACHMENTS || 20);
    const MAX_ATTACHMENT_TOTAL_BYTES = Number(window.IV_MAX_ATTACHMENT_TOTAL_BYTES || (30 * 1024 * 1024));

    const PAGE_SIZE = 10;
    const LIST_STAR_COUNT = 5;
    const LIST_STAR_GLYPHS = '<i class="fa-solid fa-star"></i>'.repeat(LIST_STAR_COUNT);
    let state = { csrfToken: '', candidates: [], filter: 'all', page: 1 };

    const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]
    ));
    const fileSize = (bytes) => (bytes < 1024 * 1024
        ? `${Math.max(1, Math.round(bytes / 1024))} KB`
        : `${(bytes / (1024 * 1024)).toFixed(1)} MB`);

    function selectedPosition() {
        const choice = byId('fieldPositionChoice').value;
        return choice === '__other__' ? byId('fieldPositionOther').value.trim() : choice.trim();
    }

    function setPosition(value = '') {
        const choice = byId('fieldPositionChoice');
        const other = byId('fieldPositionOther');
        const known = Array.from(choice.options).some((option) => option.value === value && value !== '__other__');
        choice.value = known ? value : (value ? '__other__' : '');
        other.hidden = choice.value !== '__other__';
        other.required = choice.value === '__other__';
        other.value = known ? '' : value;
    }

    function syncOtherPosition() {
        const isOther = byId('fieldPositionChoice').value === '__other__';
        byId('fieldPositionOther').hidden = !isOther;
        byId('fieldPositionOther').required = isOther;
        if (isOther) window.setTimeout(() => byId('fieldPositionOther').focus(), 0);
    }

    function validateFiles(files, existingCount = 0, existingBytes = 0) {
        if (existingCount + files.length > MAX_ATTACHMENTS) {
            return `첨부파일은 최대 ${MAX_ATTACHMENTS}개까지 등록할 수 있습니다.`;
        }
        const total = files.reduce((sum, file) => sum + Number(file.size || 0), Number(existingBytes || 0));
        if (total > MAX_ATTACHMENT_TOTAL_BYTES) return '첨부파일 전체 용량은 30MB 이하만 등록할 수 있습니다.';
        return '';
    }

    function renderSelectedFiles(input, listNode) {
        if (!input || !listNode) return;
        const files = Array.from(input.files || []);
        if (!files.length) {
            listNode.innerHTML = '<span>첨부할 파일이 없습니다.</span>';
            return;
        }
        const total = files.reduce((sum, file) => sum + file.size, 0);
        listNode.innerHTML = `<div><strong>${files.length}개 · ${fileSize(total)}</strong><span>${files.map((file) => escapeHtml(file.name)).join(' · ')}</span></div>
            <button type="button" data-clear-input="${escapeHtml(input.id)}"><i class="fa-solid fa-xmark"></i> 비우기</button>`;
    }

    function setupDropZone(zone) {
        if (!zone || zone.dataset.dropBound === '1') return;
        zone.dataset.dropBound = '1';
        const input = byId(zone.dataset.fileInput);
        const listNode = byId(zone.dataset.fileList);
        const applyFiles = (files) => {
            const selected = Array.from(files || []);
            if (!selected.length) return;
            const error = validateFiles(selected);
            if (error) {
                input.value = '';
                renderSelectedFiles(input, listNode);
                setFeedback('candidateFeedback', error, 'error');
                return;
            }
            const transfer = new DataTransfer();
            selected.forEach((file) => transfer.items.add(file));
            input.files = transfer.files;
            setFeedback('candidateFeedback');
            renderSelectedFiles(input, listNode);
        };
        zone.addEventListener('click', () => input.click());
        zone.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); input.click(); }
        });
        ['dragenter', 'dragover'].forEach((name) => zone.addEventListener(name, (event) => {
            event.preventDefault();
            zone.classList.add('is-dragging');
        }));
        ['dragleave', 'drop'].forEach((name) => zone.addEventListener(name, (event) => {
            event.preventDefault();
            zone.classList.remove('is-dragging');
        }));
        zone.addEventListener('drop', (event) => applyFiles(event.dataTransfer.files));
        input.addEventListener('change', () => {
            const error = validateFiles(Array.from(input.files || []));
            if (error) {
                input.value = '';
                setFeedback('candidateFeedback', error, 'error');
            }
            renderSelectedFiles(input, listNode);
        });
        renderSelectedFiles(input, listNode);
    }

    function formatDateTime(value) {
        const raw = String(value || '').trim();
        if (!raw) return '';
        const parsed = new Date(raw.replace(' ', 'T'));
        if (Number.isNaN(parsed.getTime())) return raw;
        const pad = (n) => String(n).padStart(2, '0');
        return `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())} ${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`;
    }

    async function apiRequest(path, options = {}) {
        const method = options.method || 'GET';
        const isFormData = options.body instanceof FormData;
        const headers = { Accept: 'application/json', ...(options.headers || {}) };
        if (method !== 'GET') {
            if (!isFormData) headers['Content-Type'] = 'application/json';
            headers['X-CSRF-Token'] = state.csrfToken;
        }
        const response = await fetch(`/interview/api/${path}`, { ...options, method, headers });
        const isJson = (response.headers.get('Content-Type') || '').includes('application/json');
        const data = isJson ? await response.json().catch(() => ({})) : {};
        if (!response.ok || data.status !== 'success') {
            throw new Error(data.message || `요청을 처리하지 못했습니다. (HTTP ${response.status})`);
        }
        return data;
    }

    function setFeedback(elementId, message = '', type = '') {
        const node = byId(elementId);
        if (!node) return;
        node.textContent = message;
        node.classList.toggle('is-error', type === 'error');
        node.classList.toggle('is-success', type === 'success');
    }

    // ------------------------------------------------------------ 면접 진행 상황

    function renderFlow() {
        const rows = state.candidates;
        const total = rows.length;
        const answered = rows.filter((item) => item.has_answers).length;
        const evaluated = rows.filter((item) => item.evaluated_count > 0).length;
        const completed = rows.filter((item) => item.is_completed).length;
        const decided = rows.filter((item) => item.result).length;
        const passed = rows.filter((item) => item.result === 'pass').length;

        byId('flowRegister').textContent = total ? `면접자 ${total}명 등록` : '이력서·첨부자료 등록';
        byId('flowQuestionnaire').textContent = total ? `제출 ${answered}명 · 미제출 ${total - answered}명` : '면접자 사전 작성';
        byId('flowEvaluate').textContent = total ? `평가 입력 ${evaluated}명 · 완료 ${completed}명` : '면접관 점수·의견 입력';
        byId('flowResult').textContent = total ? `판정 ${decided}명 · 합격 ${passed}명` : '합격·불합격·보류 확정';

        // 앞 단계가 끝나야 다음 단계도 끝난 것으로 본다.
        const reached = [total > 0, answered === total, completed === total, decided === completed && completed > 0];
        const done = reached.map((value, index) => total > 0 && reached.slice(0, index + 1).every(Boolean));
        const steps = Array.from(byId('flowList').children);
        const currentIndex = done.findIndex((value) => !value);
        steps.forEach((node, index) => {
            node.classList.toggle('is-complete', done[index]);
            node.classList.toggle('is-current', index === (currentIndex < 0 ? steps.length - 1 : currentIndex));
        });
    }

    // ------------------------------------------------------------ 목록

    function matchesFilter(item) {
        if (state.filter === 'scheduled') return !item.is_completed;
        if (state.filter === 'completed') return item.is_completed;
        if (state.filter === 'pass') return item.result === 'pass';
        return true;
    }

    function whenCell(item) {
        const text = formatDateTime(item.interview_at);
        if (!text) return '<span class="iv-dash">미정</span>';
        const [day, time] = text.split(' ');
        return `${escapeHtml(day)}<small>${escapeHtml(time || '')}</small>`;
    }

    function statusCell(item) {
        const progress = item.is_completed
            ? '<span class="iv-tag is-complete">면접완료</span>'
            : '<span class="iv-tag is-progress">면접예정</span>';
        const decided = item.result_label
            ? `<span class="iv-tag ${item.result === 'pass' ? 'is-pass' : item.result === 'fail' ? 'is-fail' : 'is-hold'}">${escapeHtml(item.result_label)}</span>`
            : '';
        return `${progress}${decided}`;
    }

    function questionnaireCell(item) {
        if (!item.has_answers) {
            return '<span class="iv-progress-note is-wait"><i class="fa-regular fa-clipboard"></i> 미작성</span>';
        }
        const typing = item.typing_cpm ? `<small>${item.typing_cpm}타/분</small>` : '';
        return `<span class="iv-progress-note is-done"><i class="fa-solid fa-check"></i> 작성완료 ${typing}</span>`;
    }

    function materialCell(item) {
        if (!item.attachment_count) {
            return '<span class="iv-material-empty"><i class="fa-regular fa-file"></i> 첨부 없음</span>';
        }
        const analysis = item.resume_analysis && item.resume_analysis.is_ready
            ? '<span class="iv-material-ai"><i class="fa-solid fa-wand-magic-sparkles"></i> AI 요약 완료</span>'
            : '<span class="iv-material-ai is-pending">AI 요약 전</span>';
        return `<strong class="iv-material-count"><i class="fa-solid fa-paperclip"></i> 첨부 ${item.attachment_count}개</strong>${analysis}`;
    }

    function scoreCell(item) {
        // 면접 평가는 별 5개 만점(0.5개 단위)이며 점수는 별 x 20으로 저장한다.
        const hasScore = item.average_score !== null;
        const stars = hasScore ? Number(item.average_score) / 20 : 0;
        const score = hasScore ? stars.toFixed(1) : '-';
        const label = hasScore ? `평균 별점 ${score}점, 5점 만점` : '아직 입력된 평가가 없습니다.';
        return `<div class="iv-score-line">
            <strong class="iv-score-value${hasScore ? '' : ' is-empty'}">${score}</strong>
            <span class="iv-score-stars" role="img" aria-label="${label}">
                <span class="iv-score-stars-row iv-score-stars-base">${LIST_STAR_GLYPHS}</span>
                <span class="iv-score-stars-fill" style="width:${(stars / LIST_STAR_COUNT) * 100}%"><span class="iv-score-stars-row">${LIST_STAR_GLYPHS}</span></span>
            </span>
        </div><small>${item.evaluated_count}/${item.panelist_count}명 평가</small>`;
    }

    function renderList() {
        const keyword = searchInput.value.trim().toLowerCase();
        const rows = state.candidates.filter((item) => matchesFilter(item) && (!keyword || [
            item.name, item.target_position, item.target_school,
        ].some((value) => String(value || '').toLowerCase().includes(keyword))));

        const filterLabel = { all: '전체', scheduled: '면접예정', completed: '면접완료', pass: '합격' }[state.filter];
        byId('candidateCount').textContent = `${filterLabel} ${rows.length}건${
            keyword ? ` · "${keyword}" 검색` : ''} · 등록 ${state.candidates.length}건`;

        if (!rows.length) {
            state.page = 1;
            candidateList.innerHTML = `<tr><td class="iv-empty-cell" colspan="8">${
                state.candidates.length ? '조건에 맞는 면접 기록이 없습니다.'
                    : '등록된 면접자가 없습니다. 오른쪽 <b>면접자 입력</b> 버튼으로 시작해주세요.'
            }</td></tr>`;
            renderPaging(1);
            return;
        }

        const totalPages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
        if (state.page > totalPages) state.page = totalPages;
        const offset = (state.page - 1) * PAGE_SIZE;

        candidateList.innerHTML = rows.slice(offset, offset + PAGE_SIZE).map((item, index) => `
            <tr class="iv-row${item.is_completed ? ' is-completed' : ''}">
                <td class="iv-cell-number" data-label="번호">${rows.length - offset - index}</td>
                <td class="iv-cell-candidate" data-label="지원자">
                    <div class="iv-candidate-layout">
                        <span class="iv-candidate-copy">
                            <strong title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</strong>
                            <small title="${escapeHtml(item.target_position || '')}">${escapeHtml(item.target_position) || '직급 미정'}</small>
                        </span>
                    </div>
                </td>
                <td class="iv-cell-school" data-label="대상학교" title="${escapeHtml(item.target_school || '')}">${
                    item.target_school ? `<i class="fa-solid fa-school" aria-hidden="true"></i><span>${escapeHtml(item.target_school)}</span>` : '<span class="iv-dash">미정</span>'}</td>
                <td class="iv-cell-when" data-label="면접일시">${whenCell(item)}</td>
                <td class="iv-cell-progress" data-label="진행 현황"><div class="iv-cell-stack"><div class="iv-status-line">${statusCell(item)}</div>${questionnaireCell(item)}</div></td>
                <td class="iv-cell-materials" data-label="서류 · AI"><div class="iv-cell-stack">${materialCell(item)}</div></td>
                <td class="iv-cell-score" data-label="면접관 평가">${scoreCell(item)}</td>
                <td class="iv-cell-actions" data-label="관리">
                    <div class="iv-actions-layout">
                        <button class="primary iv-action-main" data-open-detail="${item.id}"><i class="fa-solid fa-arrow-up-right-from-square"></i><span>면접진행</span></button>
                        <span class="iv-action-sub">
                            <button data-open-question="${item.id}" title="면접자 사전질문지 열기" aria-label="면접자 사전질문지 열기"><i class="fa-regular fa-clipboard"></i><span>면접자 질문지</span></button>
                            ${item.can_manage ? `<button data-edit="${item.id}" title="면접자 정보 수정" aria-label="면접자 정보 수정"><i class="fa-regular fa-pen-to-square"></i><span>수정</span></button><button class="danger" data-delete="${item.id}" title="면접 기록 삭제" aria-label="면접 기록 삭제"><i class="fa-regular fa-trash-can"></i><span>삭제</span></button>` : ''}
                        </span>
                    </div>
                </td>
            </tr>`).join('');
        renderPaging(totalPages);
    }

    function renderPaging(totalPages) {
        const paging = byId('candidatePaging');
        if (!paging) return;
        if (totalPages <= 1) {
            paging.innerHTML = '';
            return;
        }
        // 증명발급관리 화면처럼 10쪽 단위로 끊어 보여준다.
        const startPage = Math.floor((state.page - 1) / 10) * 10 + 1;
        const endPage = Math.min(totalPages, startPage + 9);
        const link = (label, page) => `<li><button type="button" data-page="${page}">${label}</button></li>`;
        const parts = [];
        if (state.page > 1) parts.push(link('처음', 1));
        if (startPage > 1) parts.push(link('◀ 이전 10개', startPage - 1));
        if (state.page > 1) parts.push(link('◁ Pre', state.page - 1));
        for (let page = startPage; page <= endPage; page += 1) {
            parts.push(page === state.page
                ? `<li><span class="is-active">${page}</span></li>` : link(String(page), page));
        }
        if (state.page < totalPages) parts.push(link('Next ▷', state.page + 1));
        if (endPage < totalPages) parts.push(link('다음 10개 ▶', endPage + 1));
        if (state.page < totalPages) parts.push(link('끝', totalPages));
        paging.innerHTML = parts.join('');
    }

    async function loadCandidates() {
        try {
            const data = await apiRequest('candidates');
            state.csrfToken = data.csrf_token;
            state.candidates = data.candidates || [];
            renderList();
            renderFlow();
        } catch (error) {
            candidateList.innerHTML = `<tr><td class="iv-empty-cell" colspan="8">${escapeHtml(error.message)}</td></tr>`;
        }
    }

    // ------------------------------------------------------------ 등록/수정

    function openCandidateModal(candidate = null) {
        byId('candidateForm').reset();
        setPosition(candidate ? candidate.target_position || '' : '');
        byId('candidateId').value = candidate ? candidate.id : '';
        byId('candidateModalTitle').textContent = candidate ? '면접자 정보 수정' : '면접자 입력';
        byId('createFileRow').hidden = Boolean(candidate);
        if (candidate) {
            byId('fieldName').value = candidate.name || '';
            byId('fieldSchool').value = candidate.target_school || '';
            byId('fieldInterviewAt').value = candidate.interview_at || '';
            byId('fieldMemo').value = candidate.memo || '';
        }
        setFeedback('candidateFeedback');
        renderSelectedFiles(byId('fieldFiles'), byId('createFileList'));
        candidateModal.hidden = false;
        document.body.classList.add('iv-modal-open');
        window.setTimeout(() => byId('fieldName').focus(), 30);
    }

    function closeCandidateModal() {
        candidateModal.hidden = true;
        document.body.classList.remove('iv-modal-open');
    }

    async function submitCandidate(event) {
        event.preventDefault();
        const id = byId('candidateId').value;
        const name = byId('fieldName').value.trim();
        if (!name) { byId('fieldName').focus(); return; }
        const button = byId('submitCandidate');
        if (byId('fieldPositionChoice').value === '__other__' && !selectedPosition()) {
            byId('fieldPositionOther').focus();
            setFeedback('candidateFeedback', '기타 직급을 직접 입력해주세요.', 'error');
            return;
        }
        button.disabled = true;
        setFeedback('candidateFeedback', '저장하고 있습니다…');
        try {
            if (id) {
                await apiRequest(`candidates/${id}`, {
                    method: 'PUT',
                    body: JSON.stringify({
                        name,
                        target_position: selectedPosition(),
                        target_school: byId('fieldSchool').value.trim(),
                        interview_at: byId('fieldInterviewAt').value.trim(),
                        memo: byId('fieldMemo').value.trim(),
                    }),
                });
            } else {
                const formData = new FormData();
                formData.append('name', name);
                formData.append('target_position', selectedPosition());
                formData.append('target_school', byId('fieldSchool').value.trim());
                formData.append('interview_at', byId('fieldInterviewAt').value.trim());
                formData.append('memo', byId('fieldMemo').value.trim());
                const files = Array.from(byId('fieldFiles').files);
                const validationError = validateFiles(files);
                if (validationError) throw new Error(validationError);
                files.forEach((file) => formData.append('files', file, file.name));
                const created = await apiRequest('candidates', { method: 'POST', body: formData });
                await loadCandidates();
                closeCandidateModal();
                // 이력서를 함께 올렸으면 진행표 창에서 곧바로 AI 분석을 시작한다.
                if (created.candidate) openSheet(created.candidate.id, files.length > 0);
                return;
            }
            await loadCandidates();
            setFeedback('candidateFeedback', '저장했습니다.', 'success');
            window.setTimeout(closeCandidateModal, 400);
        } catch (error) {
            setFeedback('candidateFeedback', error.message, 'error');
        } finally {
            button.disabled = false;
        }
    }

    // ------------------------------------------------------------ 면접 진행표 (새 브라우저 창)

    function openSheet(id, autoAnalyze = false) {
        const url = `/interview/sheet/${id}${autoAnalyze ? '?analyze=1' : ''}`;
        const width = Math.min(1560, Math.max(1100, window.screen.availWidth - 80));
        const height = Math.max(720, window.screen.availHeight - 90);
        // 진행표 창이 저장 결과를 목록으로 알려야 해서 opener 연결(noopener 제외)을 유지한다.
        const features = `width=${width},height=${height},left=${Math.max(0, Math.round((window.screen.availWidth - width) / 2))},top=20,resizable=yes,scrollbars=yes`;
        const opened = window.open(url, `ivSheet${id}`, features);
        if (opened) opened.focus();
        else window.alert('팝업이 차단되어 면접 진행표를 열지 못했습니다. 브라우저의 팝업 차단을 해제해주세요.');
    }

    function openQuestionnaire(id) {
        const item = state.candidates.find((row) => row.id === Number(id));
        if (item) window.open(item.questionnaire_url, '_blank', 'noopener,width=880,height=960');
    }

    // ------------------------------------------------------------ 이벤트

    async function handleListClick(event) {
        const button = event.target.closest('button');
        if (!button) return;
        if (button.dataset.openDetail) return openSheet(button.dataset.openDetail);
        if (button.dataset.openQuestion) return openQuestionnaire(button.dataset.openQuestion);
        if (button.dataset.edit) {
            const item = state.candidates.find((row) => row.id === Number(button.dataset.edit));
            return openCandidateModal(item);
        }
        if (button.dataset.delete) {
            if (!window.confirm('이 면접 기록과 첨부파일을 모두 삭제하시겠습니까?')) return;
            try {
                await apiRequest(`candidates/${button.dataset.delete}`, { method: 'DELETE' });
                await loadCandidates();
            } catch (error) {
                window.alert(error.message);
            }
        }
    }

    function applyFilter(value) {
        state.filter = value;
        state.page = 1;
        Array.from(byId('filterChips').children).forEach((node) => {
            node.classList.toggle('is-active', node.dataset.filter === value);
        });
        renderList();
    }

    byId('filterChips').addEventListener('click', (event) => {
        const button = event.target.closest('button[data-filter]');
        if (button) applyFilter(button.dataset.filter);
    });

    byId('openCreate').addEventListener('click', () => openCandidateModal());

    byId('closeCandidateModal').addEventListener('click', closeCandidateModal);
    byId('cancelCandidate').addEventListener('click', closeCandidateModal);
    byId('candidateForm').addEventListener('submit', submitCandidate);
    byId('fieldPositionChoice').addEventListener('change', syncOtherPosition);
    candidateList.addEventListener('click', handleListClick);
    searchInput.addEventListener('input', () => { state.page = 1; renderList(); });
    byId('candidatePaging').addEventListener('click', (event) => {
        const button = event.target.closest('button[data-page]');
        if (!button) return;
        state.page = Number(button.dataset.page) || 1;
        renderList();
        byId('candidateCount').scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
    document.addEventListener('click', (event) => {
        const clear = event.target.closest('[data-clear-input]');
        if (!clear) return;
        event.preventDefault();
        event.stopPropagation();
        const input = byId(clear.dataset.clearInput);
        if (!input) return;
        input.value = '';
        const zone = document.querySelector(`[data-file-input="${clear.dataset.clearInput}"]`);
        renderSelectedFiles(input, zone ? byId(zone.dataset.fileList) : null);
    });
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && !candidateModal.hidden) closeCandidateModal();
    });
    // 진행표 창에서 저장이 끝나면 목록을 자동으로 갱신한다.
    window.addEventListener('message', (event) => {
        if (event.origin === window.location.origin && event.data && event.data.type === 'interview-sheet-updated') {
            loadCandidates();
        }
    });

    setupDropZone(byId('createDropZone'));
    loadCandidates();
})();
