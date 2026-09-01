(function () {
    'use strict';

    const byId = (id) => document.getElementById(id);
    const candidateModal = byId('candidateModal');
    const detailModal = byId('detailModal');
    // 상단 메뉴바(.navbar)와 본문(.content-container)이 서로 다른 stacking context라
    // 모달을 body 바로 아래로 옮겨야 메뉴 위에 덮인다.
    [candidateModal, detailModal].forEach((modal) => { if (modal) document.body.appendChild(modal); });
    const candidateList = byId('candidateList');
    const searchInput = byId('searchInput');
    const MAX_PANELISTS = Number(window.IV_MAX_PANELISTS || 5);

    let state = { csrfToken: '', candidates: [] };
    let detailId = null;

    const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]
    ));
    const fileSize = (bytes) => (bytes < 1024 * 1024
        ? `${Math.max(1, Math.round(bytes / 1024))} KB`
        : `${(bytes / (1024 * 1024)).toFixed(1)} MB`);

    const questionnaireLabels = Array.from(
        byId('questionnaireLabels').content.querySelectorAll('i')
    ).map((node) => ({ key: node.dataset.key, title: node.dataset.title }));

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
        node.textContent = message;
        node.classList.toggle('is-error', type === 'error');
        node.classList.toggle('is-success', type === 'success');
    }

    function syncModalOpenState() {
        const open = [candidateModal, detailModal].some((item) => item && !item.hidden);
        document.body.classList.toggle('iv-modal-open', open);
    }

    // ------------------------------------------------------------ 목록

    function renderList() {
        const keyword = searchInput.value.trim().toLowerCase();
        const rows = state.candidates.filter((item) => !keyword || [
            item.name, item.target_position, item.target_school,
        ].some((value) => String(value || '').toLowerCase().includes(keyword)));

        byId('candidateCount').textContent = keyword
            ? `${rows.length}건 검색됨 · 전체 ${state.candidates.length}건`
            : `전체 ${state.candidates.length}건`;

        if (!rows.length) {
            candidateList.innerHTML = `<div class="iv-empty">${
                state.candidates.length ? '검색 조건에 맞는 면접 기록이 없습니다.'
                    : '등록된 면접자가 없습니다. 오른쪽 위 <b>면접자 입력</b>으로 시작해주세요.'
            }</div>`;
            return;
        }

        candidateList.innerHTML = rows.map((item) => {
            const tags = [];
            tags.push(item.is_completed
                ? '<span class="iv-tag is-complete">면접완료</span>'
                : '<span class="iv-tag is-progress">면접예정</span>');
            if (item.result_label) {
                tags.push(`<span class="iv-tag ${item.result === 'pass' ? 'is-pass' : item.result === 'fail' ? 'is-fail' : 'is-hold'}">${escapeHtml(item.result_label)}</span>`);
            }
            if (item.average_score !== null) tags.push(`<span class="iv-tag is-score">평균 ${item.average_score}점</span>`);
            if (item.target_position) tags.push(`<span class="iv-tag">${escapeHtml(item.target_position)}</span>`);
            if (item.target_school) tags.push(`<span class="iv-tag">${escapeHtml(item.target_school)}</span>`);
            tags.push(item.has_answers
                ? '<span class="iv-tag is-done">사전질문지 작성완료</span>'
                : '<span class="iv-tag is-wait">사전질문지 미작성</span>');
            if (item.attachment_count) tags.push(`<span class="iv-tag">첨부 ${item.attachment_count}</span>`);
            if (item.panelist_count) {
                tags.push(`<span class="iv-tag">면접관 ${item.evaluated_count}/${item.panelist_count}명 입력</span>`);
            }
            return `<article class="iv-card${item.is_completed ? ' is-completed' : ''}">
                <div>
                    <h3>${escapeHtml(item.name)}<span>${escapeHtml(formatDateTime(item.interview_at) || '면접일시 미정')}</span></h3>
                    <div class="iv-card-meta">${tags.join('')}</div>
                </div>
                <div class="iv-card-actions">
                    <button data-open-detail="${item.id}"><i class="fa-solid fa-clipboard-list"></i> 면접 진행표</button>
                    <button data-open-question="${item.id}"><i class="fa-solid fa-up-right-from-square"></i> 사전질문지 열기</button>
                    ${item.can_manage ? `<button data-edit="${item.id}">수정</button><button class="danger" data-delete="${item.id}">삭제</button>` : ''}
                </div>
            </article>`;
        }).join('');
    }

    async function loadCandidates() {
        try {
            const data = await apiRequest('candidates');
            state.csrfToken = data.csrf_token;
            state.candidates = data.candidates || [];
            renderList();
            if (detailId) {
                const current = state.candidates.find((item) => item.id === detailId);
                if (current) renderDetail(current);
            }
        } catch (error) {
            candidateList.innerHTML = `<div class="iv-empty">${escapeHtml(error.message)}</div>`;
        }
    }

    // ------------------------------------------------------------ 등록/수정

    function openCandidateModal(candidate = null) {
        byId('candidateForm').reset();
        byId('candidateId').value = candidate ? candidate.id : '';
        byId('candidateModalTitle').textContent = candidate ? '면접자 정보 수정' : '면접자 입력';
        byId('createFileRow').hidden = Boolean(candidate);
        if (candidate) {
            byId('fieldName').value = candidate.name || '';
            byId('fieldPosition').value = candidate.target_position || '';
            byId('fieldSchool').value = candidate.target_school || '';
            byId('fieldInterviewAt').value = candidate.interview_at || '';
            byId('fieldMemo').value = candidate.memo || '';
        }
        setFeedback('candidateFeedback');
        candidateModal.hidden = false;
        syncModalOpenState();
        window.setTimeout(() => byId('fieldName').focus(), 30);
    }
    function closeCandidateModal() { candidateModal.hidden = true; syncModalOpenState(); }

    async function submitCandidate(event) {
        event.preventDefault();
        const id = byId('candidateId').value;
        const name = byId('fieldName').value.trim();
        if (!name) { byId('fieldName').focus(); return; }
        const button = byId('submitCandidate');
        button.disabled = true;
        setFeedback('candidateFeedback', '저장하고 있습니다…');
        try {
            if (id) {
                await apiRequest(`candidates/${id}`, {
                    method: 'PUT',
                    body: JSON.stringify({
                        name,
                        target_position: byId('fieldPosition').value.trim(),
                        target_school: byId('fieldSchool').value.trim(),
                        interview_at: byId('fieldInterviewAt').value.trim(),
                        memo: byId('fieldMemo').value.trim(),
                    }),
                });
            } else {
                const formData = new FormData();
                formData.append('name', name);
                formData.append('target_position', byId('fieldPosition').value.trim());
                formData.append('target_school', byId('fieldSchool').value.trim());
                formData.append('interview_at', byId('fieldInterviewAt').value.trim());
                formData.append('memo', byId('fieldMemo').value.trim());
                Array.from(byId('fieldFiles').files).forEach((file) => formData.append('files', file, file.name));
                await apiRequest('candidates', { method: 'POST', body: formData });
            }
            await loadCandidates();
            setFeedback('candidateFeedback', '저장했습니다.', 'success');
            window.setTimeout(closeCandidateModal, 500);
        } catch (error) {
            setFeedback('candidateFeedback', error.message, 'error');
        } finally {
            button.disabled = false;
        }
    }

    // ------------------------------------------------------------ 상세(면접 진행표)

    function renderDetail(item) {
        detailId = item.id;
        byId('detailTitle').textContent = `${item.name} 면접 진행표`;

        const answers = item.answers;
        const answerHtml = answers
            ? questionnaireLabels.map((field, index) => {
                const value = String(answers[field.key] || '').trim();
                return `<div class="iv-answer"><h4>${index + 1}. ${escapeHtml(field.title)}</h4>
                    <p class="${value ? '' : 'empty'}">${value ? escapeHtml(value) : '작성하지 않음'}</p></div>`;
            }).join('')
            : `<p class="iv-hint">아직 사전질문지가 제출되지 않았습니다. 위 <b>사전질문지 열기</b> 버튼으로 면접자에게 작성하도록 안내해주세요.</p>`;

        const filesHtml = item.attachments.length
            ? item.attachments.map((file) => `<div class="iv-file-row">
                <i class="fa-regular fa-file-lines"></i>
                <div><strong>${escapeHtml(file.filename)}</strong><small>${fileSize(file.file_size)}</small></div>
                <div class="iv-file-actions">
                    <a href="${file.download_url}">받기</a>
                    ${item.can_manage ? `<button data-delete-file="${file.id}">삭제</button>` : ''}
                </div></div>`).join('')
            : '<p class="iv-hint">등록된 첨부파일이 없습니다.</p>';

        const panelistHtml = item.panelists.map((panelist) => `<div class="iv-panelist">
            <div class="iv-panelist-head">
                <strong>${escapeHtml(panelist.name)}</strong>
                ${panelist.score === null ? '<span class="iv-tag is-wait">평가 대기</span>'
                    : `<span class="iv-tag is-score">${panelist.score}점</span>`}
                ${item.can_manage ? `<button class="iv-button iv-button-danger" data-delete-panelist="${panelist.id}">삭제</button>` : ''}
            </div>
            <div class="iv-panelist-body">
                <input type="number" min="0" max="100" placeholder="점수" value="${panelist.score === null ? '' : panelist.score}" data-score="${panelist.id}">
                <textarea rows="2" placeholder="평가 내용" data-comment="${panelist.id}">${escapeHtml(panelist.comment || '')}</textarea>
                <button class="iv-button iv-button-primary" data-save-panelist="${panelist.id}">저장</button>
            </div>
        </div>`).join('');

        const resultButtons = ['pass', 'fail', 'hold'].map((key) => {
            const label = { pass: '합격', fail: '불합격', hold: '보류' }[key];
            const active = item.result === key ? ' is-active' : '';
            return `<button class="iv-result-button is-${key}${active}" data-result="${key}">${label}</button>`;
        }).join('');

        byId('detailBody').innerHTML = `
            <section class="iv-section iv-status-section">
                <div class="iv-status-line">
                    <span class="iv-status-badge ${item.is_completed ? 'is-complete' : 'is-progress'}">
                        ${item.is_completed ? '면접완료' : '면접예정'}
                    </span>
                    <span class="iv-status-score">평균 ${item.average_score === null ? '-' : `${item.average_score}점`}
                        <small>면접관 ${item.evaluated_count}/${item.panelist_count}명 입력</small></span>
                    ${item.result_label ? `<span class="iv-status-result is-${item.result}">${escapeHtml(item.result_label)}</span>` : ''}
                    ${item.can_manage ? (item.is_completed
                        ? '<button class="iv-button" id="reopenInterview">면접 진행중으로 되돌리기</button>'
                        : '<button class="iv-button iv-button-primary" id="completeInterview"><i class="fa-solid fa-circle-check"></i> 면접진행완료</button>') : ''}
                </div>
                ${item.can_manage && item.is_completed ? `<div class="iv-result-row">
                    <span>합격 여부</span>${resultButtons}
                </div>` : ''}
                ${item.completed_at ? `<p class="iv-hint">완료 처리 ${escapeHtml(formatDateTime(item.completed_at))}</p>` : ''}
            </section>

            <section class="iv-section">
                <h3>면접자 정보 <em>면접 전 준비</em></h3>
                <div class="iv-info-grid">
                    <div><dt>이름</dt><dd>${escapeHtml(item.name)}</dd></div>
                    <div><dt>대상 직급</dt><dd>${escapeHtml(item.target_position || '-')}</dd></div>
                    <div><dt>대상학교</dt><dd>${escapeHtml(item.target_school || '-')}</dd></div>
                    <div><dt>면접일시</dt><dd>${escapeHtml(formatDateTime(item.interview_at) || '-')}</dd></div>
                    <div class="full"><dt>면접 준비 메모</dt><dd class="pre">${escapeHtml(item.memo || '-')}</dd></div>
                    <div class="full"><dt>사전질문지 링크</dt><dd>
                        <button class="iv-button" data-open-question="${item.id}"><i class="fa-solid fa-up-right-from-square"></i> 새 창으로 열기</button>
                        <button class="iv-button" data-copy-question="${item.id}"><i class="fa-regular fa-copy"></i> 링크 복사</button>
                    </dd></div>
                </div>
            </section>

            <section class="iv-section">
                <h3>면접자 사전질문지 <em>${item.has_answers ? `제출 ${escapeHtml(formatDateTime(item.questionnaire_submitted_at))}` : '미제출'}</em></h3>
                ${answerHtml}
            </section>

            <section class="iv-section">
                <h3>첨부자료 <em>이력서·자기소개서·경력증명서</em></h3>
                ${filesHtml}
                ${item.can_manage ? `<div class="iv-inline-add">
                    <input type="file" id="detailFiles" multiple>
                    <button class="iv-button" id="uploadDetailFiles">파일 추가</button>
                </div>` : ''}
            </section>

            <section class="iv-section">
                <h3>면접관 평가 <em>${item.evaluated_count}/${item.panelist_count}명 입력${
                    item.average_score === null ? '' : ` · 평균 ${item.average_score}점`}</em></h3>
                ${panelistHtml || '<p class="iv-hint">면접관을 먼저 추가해주세요.</p>'}
                ${item.panelist_count < MAX_PANELISTS ? `<div class="iv-inline-add">
                    <input type="text" id="newPanelistName" maxlength="60" placeholder="면접관 이름 (최대 ${MAX_PANELISTS}명)">
                    <button class="iv-button" id="addPanelist"><i class="fa-solid fa-plus"></i> 면접관 추가</button>
                </div>` : `<p class="iv-hint">면접관은 최대 ${MAX_PANELISTS}명까지 추가할 수 있습니다.</p>`}
            </section>`;
    }

    function openDetail(id) {
        const item = state.candidates.find((row) => row.id === Number(id));
        if (!item) return;
        renderDetail(item);
        setFeedback('detailFeedback');
        detailModal.hidden = false;
        syncModalOpenState();
    }
    function closeDetail() { detailModal.hidden = true; detailId = null; syncModalOpenState(); }

    function openQuestionnaire(id) {
        const item = state.candidates.find((row) => row.id === Number(id));
        if (item) window.open(item.questionnaire_url, '_blank', 'noopener,width=880,height=960');
    }

    async function copyQuestionnaireLink(id) {
        const item = state.candidates.find((row) => row.id === Number(id));
        if (!item) return;
        const url = new URL(item.questionnaire_url, window.location.origin).href;
        try {
            await navigator.clipboard.writeText(url);
            setFeedback('detailFeedback', '사전질문지 링크를 복사했습니다.', 'success');
        } catch (error) {
            window.prompt('아래 링크를 복사해 면접자에게 전달해주세요.', url);
        }
    }

    // ------------------------------------------------------------ 이벤트

    async function handleDetailClick(event) {
        const button = event.target.closest('button');
        if (!button) return;

        if (button.dataset.openQuestion) return openQuestionnaire(button.dataset.openQuestion);
        if (button.dataset.copyQuestion) return copyQuestionnaireLink(button.dataset.copyQuestion);

        try {
            if (button.id === 'completeInterview' || button.id === 'reopenInterview') {
                const completing = button.id === 'completeInterview';
                button.disabled = true;
                const current = state.candidates.find((row) => row.id === detailId);
                await apiRequest(`candidates/${detailId}/status`, {
                    method: 'PUT',
                    body: JSON.stringify({
                        status: completing ? 'completed' : 'scheduled',
                        result: completing ? (current ? current.result : '') : '',
                    }),
                });
                await loadCandidates();
                setFeedback('detailFeedback', completing
                    ? '면접을 완료 처리했습니다. 합격 여부를 선택해주세요.'
                    : '면접을 진행중 상태로 되돌렸습니다.', 'success');
            } else if (button.dataset.result) {
                button.disabled = true;
                await apiRequest(`candidates/${detailId}/status`, {
                    method: 'PUT',
                    body: JSON.stringify({ status: 'completed', result: button.dataset.result }),
                });
                await loadCandidates();
                setFeedback('detailFeedback', '합격 여부를 저장했습니다.', 'success');
            } else if (button.dataset.savePanelist) {
                const id = button.dataset.savePanelist;
                button.disabled = true;
                await apiRequest(`panelists/${id}`, {
                    method: 'PUT',
                    body: JSON.stringify({
                        score: byId('detailBody').querySelector(`[data-score="${id}"]`).value.trim(),
                        comment: byId('detailBody').querySelector(`[data-comment="${id}"]`).value.trim(),
                    }),
                });
                await loadCandidates();
                setFeedback('detailFeedback', '면접 평가를 저장했습니다.', 'success');
            } else if (button.dataset.deletePanelist) {
                if (!window.confirm('이 면접관과 평가 내용을 삭제하시겠습니까?')) return;
                await apiRequest(`panelists/${button.dataset.deletePanelist}`, { method: 'DELETE' });
                await loadCandidates();
                setFeedback('detailFeedback', '면접관을 삭제했습니다.', 'success');
            } else if (button.id === 'addPanelist') {
                const name = byId('newPanelistName').value.trim();
                if (!name) { byId('newPanelistName').focus(); return; }
                await apiRequest(`candidates/${detailId}/panelists`, {
                    method: 'POST', body: JSON.stringify({ name }),
                });
                await loadCandidates();
                setFeedback('detailFeedback', '면접관을 추가했습니다.', 'success');
            } else if (button.id === 'uploadDetailFiles') {
                const files = Array.from(byId('detailFiles').files);
                if (!files.length) { byId('detailFiles').click(); return; }
                const formData = new FormData();
                files.forEach((file) => formData.append('files', file, file.name));
                button.disabled = true;
                setFeedback('detailFeedback', '파일을 올리고 있습니다…');
                await apiRequest(`candidates/${detailId}/attachments`, { method: 'POST', body: formData });
                await loadCandidates();
                setFeedback('detailFeedback', '첨부파일을 등록했습니다.', 'success');
            } else if (button.dataset.deleteFile) {
                if (!window.confirm('이 첨부파일을 삭제하시겠습니까?')) return;
                await apiRequest(`attachments/${button.dataset.deleteFile}`, { method: 'DELETE' });
                await loadCandidates();
                setFeedback('detailFeedback', '첨부파일을 삭제했습니다.', 'success');
            }
        } catch (error) {
            setFeedback('detailFeedback', error.message, 'error');
        } finally {
            button.disabled = false;
        }
    }

    async function handleListClick(event) {
        const button = event.target.closest('button');
        if (!button) return;
        if (button.dataset.openDetail) return openDetail(button.dataset.openDetail);
        if (button.dataset.openQuestion) return openQuestionnaire(button.dataset.openQuestion);
        if (button.dataset.edit) {
            const item = state.candidates.find((row) => row.id === Number(button.dataset.edit));
            return openCandidateModal(item);
        }
        if (button.dataset.delete) {
            if (!window.confirm('이 면접 기록과 첨부파일을 모두 삭제하시겠습니까?')) return;
            try {
                await apiRequest(`candidates/${button.dataset.delete}`, { method: 'DELETE' });
                if (detailId === Number(button.dataset.delete)) closeDetail();
                await loadCandidates();
            } catch (error) {
                window.alert(error.message);
            }
        }
    }

    byId('openCreate').addEventListener('click', () => openCandidateModal());
    byId('closeCandidateModal').addEventListener('click', closeCandidateModal);
    byId('cancelCandidate').addEventListener('click', closeCandidateModal);
    byId('candidateForm').addEventListener('submit', submitCandidate);
    byId('closeDetailModal').addEventListener('click', closeDetail);
    byId('closeDetailFooter').addEventListener('click', closeDetail);
    candidateList.addEventListener('click', handleListClick);
    byId('detailBody').addEventListener('click', handleDetailClick);
    searchInput.addEventListener('input', renderList);
    document.addEventListener('keydown', (event) => {
        if (event.key !== 'Escape') return;
        if (!detailModal.hidden) closeDetail();
        else if (!candidateModal.hidden) closeCandidateModal();
    });

    loadCandidates();
})();
