(function () {
    'use strict';

    const byId = (id) => document.getElementById(id);
    const CANDIDATE_ID = Number(window.IVS_CANDIDATE_ID || 0);
    const MAX_PANELISTS = Number(window.IVS_MAX_PANELISTS || 5);
    const MAX_ATTACHMENTS = Number(window.IVS_MAX_ATTACHMENTS || 20);
    const MAX_ATTACHMENT_TOTAL_BYTES = Number(window.IVS_MAX_ATTACHMENT_TOTAL_BYTES || (30 * 1024 * 1024));

    const state = { csrfToken: String(window.IVS_CSRF_TOKEN || ''), candidate: null, analyzing: false, focus: false };

    const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]
    ));
    const fileSize = (bytes) => (bytes < 1024 * 1024
        ? `${Math.max(1, Math.round(bytes / 1024))} KB`
        : `${(bytes / (1024 * 1024)).toFixed(1)} MB`);

    function formatDateTime(value) {
        const raw = String(value || '').trim();
        if (!raw) return '';
        const parsed = new Date(raw.replace(' ', 'T'));
        if (Number.isNaN(parsed.getTime())) return raw;
        const pad = (n) => String(n).padStart(2, '0');
        return `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())} ${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`;
    }

    // ------------------------------------------------------------ 별점 (5개 만점, 0.5개 단위)
    // DB 컬럼은 0~100 정수를 그대로 쓰고, 화면에서만 별 = 점수 / 20 으로 환산한다.
    const STAR_COUNT = 5;
    const STAR_GLYPHS = '<i class="fa-solid fa-star"></i>'.repeat(STAR_COUNT);
    const scoreToStars = (score) => (score === null || score === undefined || score === ''
        ? null : Math.round(Number(score) / 10) / 2);
    const starsToScore = (stars) => Math.round(Number(stars) * 20);
    // 평균은 0.5 단위로 반올림하지 않고 계산된 값을 그대로 보여준다.
    const averageStars = (score) => (Number(score) / 20).toFixed(1);
    const starLabel = (stars) => (stars === null ? '평가 대기' : `★ ${stars.toFixed(1)} / 5.0`);

    function starsMarkup(id, stars) {
        const value = stars === null ? 0 : stars;
        const hits = Array.from({ length: STAR_COUNT * 2 }, (unused, index) => {
            const step = (index + 1) / 2;
            return `<button type="button" data-star="${step}" aria-label="별 ${step}개"></button>`;
        }).join('');
        return `<div class="ivs-rating">
            <div class="ivs-stars" data-rating="${id}" data-value="${value}">
                <div class="ivs-stars-row ivs-stars-base">${STAR_GLYPHS}</div>
                <div class="ivs-stars-fill" style="width:${(value / STAR_COUNT) * 100}%"><div class="ivs-stars-row">${STAR_GLYPHS}</div></div>
                <div class="ivs-stars-hit">${hits}</div>
            </div>
            <span class="ivs-rating-value${stars === null ? ' is-empty' : ''}" data-rating-label="${id}">${
                stars === null ? '평가 전' : `${stars.toFixed(1)} <small>/ 5.0</small>`}</span>
            <button type="button" class="ivs-rating-clear" data-clear-rating="${id}">지우기</button>
        </div>`;
    }

    function paintStars(node, stars, preview = false) {
        node.querySelector('.ivs-stars-fill').style.width = `${(stars / STAR_COUNT) * 100}%`;
        if (preview) return;
        const label = node.parentElement.querySelector('[data-rating-label]');
        label.classList.toggle('is-empty', stars <= 0);
        label.innerHTML = stars > 0 ? `${stars.toFixed(1)} <small>/ 5.0</small>` : '평가 전';
    }

    function bindStars(root) {
        root.addEventListener('mouseover', (event) => {
            const hit = event.target.closest('[data-star]');
            if (hit) paintStars(hit.closest('.ivs-stars'), Number(hit.dataset.star), true);
        });
        root.addEventListener('mouseout', (event) => {
            const stars = event.target.closest('.ivs-stars');
            if (stars && !stars.contains(event.relatedTarget)) {
                paintStars(stars, Number(stars.dataset.value), true);
            }
        });
        root.addEventListener('click', (event) => {
            const hit = event.target.closest('[data-star]');
            if (hit) {
                const node = hit.closest('.ivs-stars');
                node.dataset.value = hit.dataset.star;
                paintStars(node, Number(hit.dataset.star));
                return;
            }
            const clear = event.target.closest('[data-clear-rating]');
            if (clear) {
                const node = root.querySelector(`.ivs-stars[data-rating="${clear.dataset.clearRating}"]`);
                node.dataset.value = '0';
                paintStars(node, 0);
            }
        });
    }

    function setFeedback(message = '', type = '') {
        const node = byId('sheetFeedback');
        node.textContent = message;
        node.classList.toggle('is-error', type === 'error');
        node.classList.toggle('is-success', type === 'success');
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
        if (data.csrf_token) state.csrfToken = data.csrf_token;
        return data;
    }

    function notifyOpener() {
        try {
            if (window.opener && !window.opener.closed) {
                window.opener.postMessage({ type: 'interview-sheet-updated', id: CANDIDATE_ID }, window.location.origin);
            }
        } catch (error) { /* 목록 창이 이미 닫힌 경우는 무시한다. */ }
    }

    const questionnaireLabels = Array.from(
        byId('questionnaireLabels').content.querySelectorAll('i')
    ).map((node) => ({ key: node.dataset.key, title: node.dataset.title }));

    // ------------------------------------------------------------ 글자 크기

    const ZOOM_STEPS = [1, 1.1, 1.2, 1.35, 1.5, 1.7];
    let zoomIndex = 1;

    function applyZoom() {
        const scale = ZOOM_STEPS[zoomIndex];
        document.documentElement.style.fontSize = `${(16 * scale).toFixed(2)}px`;
        const label = `${Math.round(scale * 100)}%`;
        byId('zoomLabel').textContent = label;
        byId('focusZoomLabel').textContent = label;
        try { window.localStorage.setItem('saedam_interview_sheet_zoom', String(zoomIndex)); } catch (error) { /* 저장 불가 브라우저 */ }
    }

    function initZoom() {
        try {
            const saved = Number(window.localStorage.getItem('saedam_interview_sheet_zoom'));
            if (Number.isInteger(saved) && saved >= 0 && saved < ZOOM_STEPS.length) zoomIndex = saved;
        } catch (error) { /* 저장 불가 브라우저 */ }
        applyZoom();
    }

    // ------------------------------------------------------------ 왼쪽 본문

    function renderMain(item) {
        const answers = item.answers;
        const answerHtml = answers
            ? questionnaireLabels.map((field, index) => {
                const value = String(answers[field.key] || '').trim();
                return `<div class="ivs-answer"><h4>${index + 1}. ${escapeHtml(field.title)}</h4>
                    <p class="${value ? '' : 'empty'}">${value ? escapeHtml(value) : '작성하지 않음'}</p></div>`;
            }).join('')
            : '<p class="ivs-hint">아직 사전질문지가 제출되지 않았습니다. 위 <b>사전질문지 열기</b> 버튼으로 면접자에게 작성하도록 안내해주세요.</p>';

        const filesHtml = item.attachments.length
            ? item.attachments.map((file) => `<div class="ivs-file-row">
                <i class="fa-regular fa-file-lines"></i>
                <div><strong>${escapeHtml(file.filename)}</strong><small>${fileSize(file.file_size)}</small></div>
                <div class="ivs-file-actions">
                    <a href="${file.download_url}">받기</a>
                    ${item.can_manage ? `<button data-delete-file="${file.id}">삭제</button>` : ''}
                </div></div>`).join('')
            : '<p class="ivs-hint">등록된 첨부파일이 없습니다. 오른쪽 AI 이력서 요약에서 이력서를 올려주세요.</p>';

        const panelistHtml = item.panelists.map((panelist) => {
            const stars = scoreToStars(panelist.score);
            return `<div class="ivs-panelist">
            <div class="ivs-panelist-head">
                <strong>${escapeHtml(panelist.name)}</strong>
                <span class="ivs-tag ${stars === null ? 'is-wait' : 'is-score'}">${starLabel(stars)}</span>
                ${item.can_manage ? `<button class="ivs-button ivs-button-danger" data-delete-panelist="${panelist.id}">삭제</button>` : ''}
            </div>
            ${starsMarkup(panelist.id, stars)}
            <div class="ivs-panelist-body">
                <textarea rows="2" placeholder="평가 내용" data-comment="${panelist.id}">${escapeHtml(panelist.comment || '')}</textarea>
                <button class="ivs-button ivs-button-primary" data-save-panelist="${panelist.id}">저장</button>
            </div>
        </div>`;
        }).join('');

        const resultButtons = ['pass', 'fail', 'hold'].map((key) => {
            const label = { pass: '합격', fail: '불합격', hold: '보류' }[key];
            const active = item.result === key ? ' is-active' : '';
            return `<button class="ivs-result-button is-${key}${active}" data-result="${key}">${label}</button>`;
        }).join('');

        byId('sheetMain').innerHTML = `
            <section class="ivs-card">
                <header class="ivs-card-head">
                    <div><span class="ivs-step">01</span><h2>면접 진행상태</h2><p>면접 완료 처리와 합격 여부를 저장합니다.</p></div>
                </header>
                <div class="ivs-status-line">
                    <span class="ivs-status-badge ${item.is_completed ? 'is-complete' : 'is-progress'}">${item.is_completed ? '면접완료' : '면접예정'}</span>
                    <span class="ivs-status-score">평균 ${item.average_score === null ? '-' : `★ ${averageStars(item.average_score)} / 5.0`}
                        <small>면접관 ${item.evaluated_count}/${item.panelist_count}명 입력</small></span>
                    ${item.result_label ? `<span class="ivs-status-result is-${item.result}">${escapeHtml(item.result_label)}</span>` : ''}
                    <button class="ivs-button ivs-button-focus" id="startFocus"><i class="fa-solid fa-expand"></i> 면접진행</button>
                    ${item.can_manage ? (item.is_completed
                        ? '<button class="ivs-button" id="reopenInterview">면접 진행중으로 되돌리기</button>'
                        : '<button class="ivs-button ivs-button-primary" id="completeInterview"><i class="fa-solid fa-circle-check"></i> 면접진행완료</button>') : ''}
                </div>
                ${item.can_manage && item.is_completed ? `<div class="ivs-result-row"><span>합격 여부</span>${resultButtons}</div>` : ''}
                ${item.completed_at ? `<p class="ivs-hint">완료 처리 ${escapeHtml(formatDateTime(item.completed_at))}</p>` : ''}
            </section>

            <section class="ivs-card">
                <header class="ivs-card-head">
                    <div><span class="ivs-step">02</span><h2>면접자 정보</h2><p>면접 전 준비 내용과 사전질문지 링크입니다.</p></div>
                </header>
                <div class="ivs-info-grid">
                    <div><dt>이름</dt><dd>${escapeHtml(item.name)}</dd></div>
                    <div><dt>대상 직급</dt><dd>${escapeHtml(item.target_position || '-')}</dd></div>
                    <div><dt>대상학교</dt><dd>${escapeHtml(item.target_school || '-')}</dd></div>
                    <div><dt>면접일시</dt><dd>${escapeHtml(formatDateTime(item.interview_at) || '-')}</dd></div>
                    <div class="full"><dt>면접 준비 메모</dt><dd class="pre">${escapeHtml(item.memo || '-')}</dd></div>
                    <div class="full"><dt>사전질문지 링크</dt><dd class="ivs-link-actions">
                        <button class="ivs-button" id="openQuestion"><i class="fa-solid fa-up-right-from-square"></i> 사전질문지 열기</button>
                        <button class="ivs-button" id="copyQuestion"><i class="fa-regular fa-copy"></i> 링크 복사</button>
                    </dd></div>
                </div>
            </section>

            <section class="ivs-card">
                <header class="ivs-card-head">
                    <div><span class="ivs-step">03</span><h2>면접자 사전질문지</h2><p>${item.has_answers
                        ? `제출 ${escapeHtml(formatDateTime(item.questionnaire_submitted_at))}` : '아직 제출되지 않았습니다.'}</p></div>
                    <div class="ivs-tag-group">
                        <span class="ivs-tag ${item.has_answers ? 'is-done' : 'is-wait'}">${item.has_answers ? '작성완료' : '미작성'}</span>
                        ${item.typing_cpm ? `<span class="ivs-tag is-speed">${item.typing_cpm}타/분</span>` : ''}
                    </div>
                </header>
                ${answerHtml}
            </section>

            <section class="ivs-card">
                <header class="ivs-card-head">
                    <div><span class="ivs-step">04</span><h2>첨부자료</h2><p>이력서·자기소개서·경력증명서 · 전체 ${fileSize(item.attachment_total_size || 0)}</p></div>
                    <span class="ivs-tag">${item.attachment_count}개</span>
                </header>
                ${filesHtml}
            </section>

            <section class="ivs-card">
                <header class="ivs-card-head">
                    <div><span class="ivs-step">05</span><h2>면접관 평가</h2><p>별 5개 만점 · 0.5개 단위 · ${item.evaluated_count}/${item.panelist_count}명 입력${
                        item.average_score === null ? '' : ` · 평균 ★ ${averageStars(item.average_score)}`}</p></div>
                </header>
                ${panelistHtml || '<p class="ivs-hint">면접관을 먼저 추가해주세요.</p>'}
                ${item.panelist_count < MAX_PANELISTS ? `<div class="ivs-inline-add">
                    <input type="text" id="newPanelistName" maxlength="60" placeholder="면접관 이름 (최대 ${MAX_PANELISTS}명)">
                    <button class="ivs-button" id="addPanelist"><i class="fa-solid fa-plus"></i> 면접관 추가</button>
                </div>` : `<p class="ivs-hint">면접관은 최대 ${MAX_PANELISTS}명까지 추가할 수 있습니다.</p>`}
            </section>`;
    }

    // ------------------------------------------------------------ 오른쪽 AI 요약

    function analysisGroup(title, values, icon) {
        const rows = Array.isArray(values) ? values : [];
        return `<div class="ivs-ai-group"><h4><i class="fa-solid ${icon}"></i>${escapeHtml(title)}</h4>
            ${rows.length ? `<ul>${rows.map((value) => `<li>${escapeHtml(value)}</li>`).join('')}</ul>`
                : '<p>확인된 내용이 없습니다.</p>'}</div>`;
    }

    function renderAi(item) {
        const analysis = item.resume_analysis;
        const badge = byId('aiBadge');
        badge.classList.remove('is-error');
        if (state.analyzing) {
            badge.innerHTML = '<i class="fa-solid fa-sparkles"></i> 분석중';
        } else if (analysis && analysis.is_ready) {
            badge.innerHTML = '<i class="fa-solid fa-circle-check"></i> 요약 완료';
        } else if (analysis && analysis.status === 'error') {
            badge.innerHTML = '<i class="fa-solid fa-triangle-exclamation"></i> 분석 실패';
            badge.classList.add('is-error');
        } else {
            badge.innerHTML = '<i class="fa-solid fa-sparkles"></i> 대기';
        }

        byId('resumeDropWrap').hidden = !item.can_manage;
        byId('analyzeResume').disabled = !item.attachment_count || state.analyzing;
        byId('uploadResume').disabled = state.analyzing;

        if (!analysis || (!analysis.is_ready && analysis.status !== 'error')) {
            const message = analysis && analysis.status === 'analyzing'
                ? 'AI가 이력서를 분석하고 있습니다…'
                : (item.attachment_count
                    ? '첨부된 이력서를 분석해 사진·학력·자격·경력을 정리할 수 있습니다.'
                    : '이력서를 먼저 첨부해주세요.');
            byId('aiBody').innerHTML = `<div class="ivs-ai-empty"><i class="fa-solid fa-file-waveform"></i>
                <div><strong>아직 AI 이력서 요약이 없습니다.</strong><p>${escapeHtml(message)}</p></div></div>`;
            return;
        }
        if (analysis.status === 'error') {
            byId('aiBody').innerHTML = `<div class="ivs-ai-empty is-error"><i class="fa-solid fa-triangle-exclamation"></i>
                <div><strong>이력서 분석에 실패했습니다.</strong>
                <p>${escapeHtml(analysis.error_message || '다시 분석해주세요.')}</p></div></div>`;
            return;
        }

        // 이력서를 교체하면 사진 주소는 같으므로 분석 시각을 붙여 새 사진을 받아온다.
        const photoUrl = analysis.photo_url
            ? `${analysis.photo_url}?v=${encodeURIComponent(analysis.analyzed_at || '')}` : '';
        const sourceFiles = Array.isArray(analysis.source_files) ? analysis.source_files : [];
        byId('aiBody').innerHTML = `<div class="ivs-ai-summary">
            <div class="ivs-ai-profile">
                ${photoUrl ? `<img src="${photoUrl}" alt="${escapeHtml(item.name)} 지원자 사진">`
                    : '<div class="ivs-ai-photo-empty"><i class="fa-regular fa-user"></i><span>사진을<br>찾지 못함</span></div>'}
                <div class="ivs-ai-profile-text">
                    <span>AI 핵심 요약</span>
                    <p>${escapeHtml(analysis.summary || '요약된 내용이 없습니다.')}</p>
                    <small>${analysis.analyzed_at ? `${escapeHtml(formatDateTime(analysis.analyzed_at))} 분석` : ''}${
                        sourceFiles.length ? ` · ${escapeHtml(sourceFiles.join(', '))}` : ''}</small>
                </div>
            </div>
            <div class="ivs-ai-groups">
                ${analysisGroup('학력', analysis.education, 'fa-graduation-cap')}
                ${analysisGroup('자격', analysis.qualifications, 'fa-certificate')}
                ${analysisGroup('경력', analysis.career, 'fa-briefcase')}
            </div>
            <p class="ivs-ai-notice"><i class="fa-solid fa-circle-info"></i> AI 요약은 원문 확인을 돕는 참고자료입니다. 면접 전 첨부 이력서와 대조해주세요.</p>
        </div>`;
    }

    // ------------------------------------------------------------ 면접 집중 모드
    // 면접 중에는 사전질문지와 AI 이력서 요약만 검은 배경으로 크게 본다.

    function focusGroup(title, values, icon) {
        const rows = Array.isArray(values) ? values : [];
        return `<div class="ivs-focus-group"><h4><i class="fa-solid ${icon}"></i>${escapeHtml(title)}</h4>
            ${rows.length ? `<ul>${rows.map((value) => `<li>${escapeHtml(value)}</li>`).join('')}</ul>`
                : '<p>확인된 내용이 없습니다.</p>'}</div>`;
    }

    function renderFocus(item) {
        const infoHtml = `<div class="ivs-focus-info">
            <div><dt>이름</dt><dd>${escapeHtml(item.name)}</dd></div>
            <div><dt>대상 직급</dt><dd>${escapeHtml(item.target_position || '-')}</dd></div>
            <div><dt>대상학교</dt><dd>${escapeHtml(item.target_school || '-')}</dd></div>
            <div><dt>면접일시</dt><dd>${escapeHtml(formatDateTime(item.interview_at) || '-')}</dd></div>
            ${item.memo ? `<div class="full"><dt>면접 준비 메모</dt><dd class="pre">${escapeHtml(item.memo)}</dd></div>` : ''}
        </div>`;

        const answers = item.answers;
        const answerHtml = answers
            ? questionnaireLabels.map((field, index) => {
                const value = String(answers[field.key] || '').trim();
                return `<div class="ivs-focus-answer"><h4>${index + 1}. ${escapeHtml(field.title)}</h4>
                    <p class="${value ? '' : 'empty'}">${value ? escapeHtml(value) : '작성하지 않음'}</p></div>`;
            }).join('')
            : '<div class="ivs-focus-empty">사전질문지가 아직 제출되지 않았습니다.</div>';

        const analysis = item.resume_analysis;
        let aiHtml;
        if (!analysis || !analysis.is_ready) {
            aiHtml = '<div class="ivs-focus-empty">AI 이력서 요약이 아직 없습니다.<br>나가기 후 이력서를 분석해주세요.</div>';
        } else {
            const photoUrl = analysis.photo_url
                ? `${analysis.photo_url}?v=${encodeURIComponent(analysis.analyzed_at || '')}` : '';
            aiHtml = `<div class="ivs-focus-profile">
                    ${photoUrl ? `<img src="${photoUrl}" alt="${escapeHtml(item.name)} 지원자 사진">`
                        : '<div class="ivs-focus-photo-empty"><i class="fa-regular fa-user"></i><span>사진을<br>찾지 못함</span></div>'}
                    <div class="ivs-focus-profile-text">
                        <span>AI 핵심 요약</span>
                        <p>${escapeHtml(analysis.summary || '요약된 내용이 없습니다.')}</p>
                    </div>
                </div>
                ${focusGroup('학력', analysis.education, 'fa-graduation-cap')}
                ${focusGroup('자격', analysis.qualifications, 'fa-certificate')}
                ${focusGroup('경력', analysis.career, 'fa-briefcase')}`;
        }

        // 첨부 이력서는 새 탭에서 바로 열어 원문을 확인한다.
        const filesHtml = item.attachments.length
            ? item.attachments.map((file) => `<a class="ivs-focus-file" href="${file.download_url}?inline=1" target="_blank" rel="noopener">
                <i class="fa-regular fa-file-lines"></i>
                <span><strong>${escapeHtml(file.filename)}</strong><small>${fileSize(file.file_size)}</small></span>
                <i class="fa-solid fa-arrow-up-right-from-square"></i></a>`).join('')
            : '<p class="ivs-focus-file-empty">첨부된 이력서가 없습니다.</p>';

        const panelistHtml = item.panelists.length
            ? item.panelists.map((panelist) => {
                const stars = scoreToStars(panelist.score);
                return `<div class="ivs-focus-panelist">
                    <div class="ivs-focus-panelist-head">
                        <strong>${escapeHtml(panelist.name)}</strong>
                        <span class="ivs-tag ${stars === null ? 'is-wait' : 'is-score'}">${starLabel(stars)}</span>
                    </div>
                    ${starsMarkup(panelist.id, stars)}
                    <div class="ivs-focus-panelist-body">
                        <textarea rows="2" placeholder="평가 내용" data-comment="${panelist.id}">${escapeHtml(panelist.comment || '')}</textarea>
                        <button class="ivs-focus-save" type="button" data-save-panelist="${panelist.id}"><i class="fa-solid fa-floppy-disk"></i> 저장</button>
                    </div>
                </div>`;
            }).join('')
            : '<div class="ivs-focus-empty">등록된 면접관이 없습니다. 나가기 후 면접관을 추가해주세요.</div>';

        byId('focusTitle').textContent = `${item.name} 면접 진행`;
        byId('focusBody').innerHTML = `
            <section class="ivs-focus-pane">
                <h3><i class="fa-regular fa-comments"></i> 면접자 정보 · 사전질문지
                    <em>${item.has_answers ? '작성완료' : '미작성'}${item.typing_cpm ? ` · ${item.typing_cpm}타/분` : ''}</em></h3>
                <div class="ivs-focus-scroll">${infoHtml}${answerHtml}</div>
            </section>
            <section class="ivs-focus-pane">
                <h3><i class="fa-solid fa-sparkles"></i> AI 이력서 요약
                    <em>${escapeHtml(item.target_position || item.target_school || '지원 정보 없음')}</em></h3>
                <div class="ivs-focus-scroll">
                    ${aiHtml}
                    <div class="ivs-focus-files">
                        <h4><i class="fa-regular fa-folder-open"></i> 첨부된 이력서 <small>눌러서 새 탭으로 열기</small></h4>
                        ${filesHtml}
                    </div>
                </div>
            </section>
            <section class="ivs-focus-pane is-wide">
                <h3><i class="fa-solid fa-star-half-stroke"></i> 면접관 평가
                    <span class="ivs-focus-feedback" id="focusFeedback" role="status"></span>
                    <em>별 5개 만점 · 0.5개 단위${item.average_score === null ? '' : ` · 평균 ★ ${averageStars(item.average_score)}`}</em></h3>
                <div class="ivs-focus-scroll ivs-focus-panelists">${panelistHtml}</div>
            </section>`;
    }

    function setFocusFeedback(message = '', type = '') {
        const node = byId('focusFeedback');
        if (!node) return;
        node.textContent = message;
        node.classList.toggle('is-error', type === 'error');
        node.classList.toggle('is-success', type === 'success');
    }

    async function handleFocusClick(event) {
        const button = event.target.closest('[data-save-panelist]');
        if (!button || button.disabled) return;
        const id = button.dataset.savePanelist;
        const root = byId('focusBody');
        button.disabled = true;
        setFocusFeedback('저장하고 있습니다…');
        try {
            const stars = Number(root.querySelector(`.ivs-stars[data-rating="${id}"]`).dataset.value || 0);
            await apiRequest(`panelists/${id}`, {
                method: 'PUT',
                body: JSON.stringify({
                    score: stars > 0 ? starsToScore(stars) : '',
                    comment: root.querySelector(`[data-comment="${id}"]`).value.trim(),
                }),
            });
            await loadCandidate();
            setFocusFeedback('면접 평가를 저장했습니다.', 'success');
            notifyOpener();
        } catch (error) {
            setFocusFeedback(error.message, 'error');
            button.disabled = false;
        }
    }

    function openFocus() {
        if (!state.candidate) return;
        state.focus = true;
        renderFocus(state.candidate);
        byId('focusMode').hidden = false;
        document.body.classList.add('ivs-focus-open');
        byId('exitFocus').focus();
    }

    function closeFocus() {
        state.focus = false;
        byId('focusMode').hidden = true;
        document.body.classList.remove('ivs-focus-open');
    }

    function render(item) {
        state.candidate = item;
        document.title = `${item.name} 면접 진행표`;
        byId('sheetTitle').textContent = `${item.name} 면접 진행표`;
        renderMain(item);
        renderAi(item);
        if (state.focus) renderFocus(item);
    }

    async function loadCandidate() {
        const data = await apiRequest(`candidates/${CANDIDATE_ID}`);
        render(data.candidate);
        return data.candidate;
    }

    // ------------------------------------------------------------ AI 분석 진행 그래픽

    const PROGRESS_STAGES = [
        { percent: 12, title: '첨부된 이력서를 확인하고 있습니다.', message: '분석할 수 있는 파일 형식인지 검사합니다.', log: '첨부 이력서 확인' },
        { percent: 26, title: '문서에서 글자를 뽑아내고 있습니다.', message: 'PDF·HWP·DOCX 본문 텍스트를 추출합니다.', log: '문서 본문 추출' },
        { percent: 42, title: '문서 속 사진 후보를 찾고 있습니다.', message: '증명사진 비율에 가까운 이미지를 골라냅니다.', log: '지원자 사진 탐색' },
        { percent: 58, title: 'AI 모델에 이력서를 전달하고 있습니다.', message: '원본과 추출 텍스트를 함께 올리는 중입니다.', log: 'AI 모델 요청 전송' },
        { percent: 74, title: '학력·자격·경력을 정리하고 있습니다.', message: 'AI가 항목별로 사실만 골라 정리합니다.', log: '항목별 요약 생성' },
        { percent: 88, title: '요약 결과를 다듬고 있습니다.', message: '개인정보를 걸러내고 최종 요약을 만듭니다.', log: '요약 결과 정리' },
    ];

    function createProgress() {
        const box = byId('aiProgress');
        const bar = byId('aiProgressBar');
        const percentNode = byId('aiProgressPercent');
        let timer = null;
        let index = 0;
        let percent = 4;

        const paint = (value, title, message, log) => {
            percent = value;
            bar.style.width = `${value}%`;
            percentNode.textContent = `${value}%`;
            if (title) byId('aiProgressTitle').textContent = title;
            if (message) byId('aiProgressMessage').textContent = message;
            if (log) byId('aiProgressLog').insertAdjacentHTML('beforeend', `<li>${escapeHtml(log)}</li>`);
        };

        box.hidden = false;
        box.classList.remove('is-complete', 'is-error');
        byId('aiProgressLog').innerHTML = '';
        paint(4, '이력서 분석을 시작합니다.', '첨부 파일을 서버로 보내고 있습니다.', '');

        const tick = () => {
            if (index < PROGRESS_STAGES.length) {
                const stage = PROGRESS_STAGES[index];
                index += 1;
                paint(stage.percent, stage.title, stage.message, stage.log);
            } else if (percent < 96) {
                // 응답이 늦어져도 멈춘 것처럼 보이지 않게 천천히 밀어 올린다.
                paint(Math.min(96, percent + 1), '', 'AI 응답을 기다리고 있습니다. 이력서 분량이 많으면 조금 더 걸립니다.', '');
            }
        };
        timer = window.setInterval(tick, 1400);
        window.setTimeout(tick, 350);

        return {
            finish(message) {
                window.clearInterval(timer);
                box.classList.add('is-complete');
                paint(100, 'AI 이력서 요약을 완료했습니다.', message || '사진·학력·자격·경력을 오른쪽에 정리했습니다.', '요약 저장 완료');
            },
            fail(message) {
                window.clearInterval(timer);
                box.classList.add('is-error');
                bar.style.width = '100%';
                percentNode.textContent = '오류';
                byId('aiProgressTitle').textContent = 'AI 이력서 분석에 실패했습니다.';
                byId('aiProgressMessage').textContent = message;
                byId('aiProgressLog').insertAdjacentHTML('beforeend', `<li>${escapeHtml(message)}</li>`);
            },
        };
    }

    async function runResumeAnalysis() {
        if (state.analyzing) return;
        state.analyzing = true;
        if (state.candidate) renderAi(state.candidate);
        setFeedback('AI가 이력서를 분석하고 있습니다…');
        const progress = createProgress();
        try {
            const data = await apiRequest(`candidates/${CANDIDATE_ID}/resume-analysis`, {
                method: 'POST', body: JSON.stringify({}),
            });
            state.analyzing = false;
            render(data.candidate);
            progress.finish();
            setFeedback('이력서 AI 분석을 완료했습니다.', 'success');
            notifyOpener();
        } catch (error) {
            state.analyzing = false;
            progress.fail(error.message);
            setFeedback(error.message, 'error');
            try { await loadCandidate(); } catch (loadError) { /* 목록 갱신 실패는 무시 */ }
        }
    }

    // ------------------------------------------------------------ 이력서 드롭존

    const resumeInput = byId('resumeFiles');
    const resumeList = byId('resumeFileList');

    function validateFiles(files, existingCount = 0, existingBytes = 0) {
        if (existingCount + files.length > MAX_ATTACHMENTS) {
            return `첨부파일은 최대 ${MAX_ATTACHMENTS}개까지 등록할 수 있습니다.`;
        }
        const total = files.reduce((sum, file) => sum + Number(file.size || 0), Number(existingBytes || 0));
        if (total > MAX_ATTACHMENT_TOTAL_BYTES) return '첨부파일 전체 용량은 30MB 이하만 등록할 수 있습니다.';
        return '';
    }

    function renderSelectedFiles() {
        const files = Array.from(resumeInput.files || []);
        if (!files.length) {
            resumeList.innerHTML = '<span>선택한 파일이 없습니다.</span>';
            return;
        }
        const total = files.reduce((sum, file) => sum + file.size, 0);
        resumeList.innerHTML = `<div><strong>${files.length}개 · ${fileSize(total)}</strong>
            <span>${files.map((file) => escapeHtml(file.name)).join(' · ')}</span></div>
            <button type="button" id="clearResumeFiles"><i class="fa-solid fa-xmark"></i> 비우기</button>`;
    }

    function setResumeFiles(files) {
        const selected = Array.from(files || []);
        if (!selected.length) return;
        const replace = byId('replaceResume').checked;
        const current = state.candidate;
        const error = validateFiles(
            selected,
            replace || !current ? 0 : current.attachment_count,
            replace || !current ? 0 : current.attachment_total_size,
        );
        if (error) {
            clearResumeFiles();
            setFeedback(error, 'error');
            return;
        }
        // 다른 이력서를 다시 끌어다 놓으면 이전 선택은 버리고 새 파일만 남긴다.
        const transfer = new DataTransfer();
        selected.forEach((file) => transfer.items.add(file));
        resumeInput.files = transfer.files;
        renderSelectedFiles();
        setFeedback(`${selected.length}개 파일을 선택했습니다. ${replace ? '기존 첨부를 교체' : '기존 첨부에 추가'}합니다.`);
    }

    function clearResumeFiles() {
        resumeInput.value = '';
        renderSelectedFiles();
    }

    function bindDropZone() {
        const zone = byId('resumeDropZone');
        zone.addEventListener('click', () => resumeInput.click());
        zone.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); resumeInput.click(); }
        });
        ['dragenter', 'dragover'].forEach((name) => zone.addEventListener(name, (event) => {
            event.preventDefault();
            zone.classList.add('is-dragging');
        }));
        ['dragleave', 'drop'].forEach((name) => zone.addEventListener(name, (event) => {
            event.preventDefault();
            zone.classList.remove('is-dragging');
        }));
        zone.addEventListener('drop', (event) => setResumeFiles(event.dataTransfer.files));
        resumeInput.addEventListener('change', () => setResumeFiles(resumeInput.files));
        resumeList.addEventListener('click', (event) => {
            if (event.target.closest('#clearResumeFiles')) {
                event.preventDefault();
                clearResumeFiles();
            }
        });
        renderSelectedFiles();
    }

    async function uploadResume() {
        const files = Array.from(resumeInput.files || []);
        if (!files.length) { resumeInput.click(); return; }
        const replace = byId('replaceResume').checked;
        if (replace && state.candidate && state.candidate.attachment_count
            && !window.confirm('기존 첨부파일과 이전 AI 요약을 지우고 새 이력서로 교체합니다. 계속할까요?')) {
            return;
        }
        const button = byId('uploadResume');
        button.disabled = true;
        setFeedback('이력서를 올리고 있습니다…');
        try {
            const formData = new FormData();
            files.forEach((file) => formData.append('files', file, file.name));
            if (replace) formData.append('replace', '1');
            const data = await apiRequest(`candidates/${CANDIDATE_ID}/attachments`, { method: 'POST', body: formData });
            render(data.candidate);
            // 업로드가 끝나면 다음 드롭이 이전 선택과 섞이지 않도록 즉시 비운다.
            clearResumeFiles();
            notifyOpener();
            await runResumeAnalysis();
        } catch (error) {
            setFeedback(error.message, 'error');
        } finally {
            byId('uploadResume').disabled = state.analyzing;
        }
    }

    // ------------------------------------------------------------ 본문 이벤트

    async function handleMainClick(event) {
        const button = event.target.closest('button');
        if (!button || button.disabled) return;
        try {
            if (button.id === 'startFocus') {
                openFocus();
                return;
            }
            if (button.id === 'openQuestion') {
                window.open(state.candidate.questionnaire_url, '_blank', 'noopener,width=880,height=960');
                return;
            }
            if (button.id === 'copyQuestion') {
                const url = new URL(state.candidate.questionnaire_url, window.location.origin).href;
                try {
                    await navigator.clipboard.writeText(url);
                    setFeedback('사전질문지 링크를 복사했습니다.', 'success');
                } catch (error) {
                    window.prompt('아래 링크를 복사해 면접자에게 전달해주세요.', url);
                }
                return;
            }

            button.disabled = true;
            if (button.id === 'completeInterview' || button.id === 'reopenInterview') {
                const completing = button.id === 'completeInterview';
                await apiRequest(`candidates/${CANDIDATE_ID}/status`, {
                    method: 'PUT',
                    body: JSON.stringify({
                        status: completing ? 'completed' : 'scheduled',
                        result: completing ? (state.candidate ? state.candidate.result : '') : '',
                    }),
                });
                await loadCandidate();
                setFeedback(completing ? '면접을 완료 처리했습니다. 합격 여부를 선택해주세요.' : '면접을 진행중 상태로 되돌렸습니다.', 'success');
            } else if (button.dataset.result) {
                await apiRequest(`candidates/${CANDIDATE_ID}/status`, {
                    method: 'PUT',
                    body: JSON.stringify({ status: 'completed', result: button.dataset.result }),
                });
                await loadCandidate();
                setFeedback('합격 여부를 저장했습니다.', 'success');
            } else if (button.dataset.savePanelist) {
                const id = button.dataset.savePanelist;
                const stars = Number(byId('sheetMain').querySelector(`.ivs-stars[data-rating="${id}"]`).dataset.value || 0);
                await apiRequest(`panelists/${id}`, {
                    method: 'PUT',
                    body: JSON.stringify({
                        score: stars > 0 ? starsToScore(stars) : '',
                        comment: byId('sheetMain').querySelector(`[data-comment="${id}"]`).value.trim(),
                    }),
                });
                await loadCandidate();
                setFeedback('면접 평가를 저장했습니다.', 'success');
            } else if (button.dataset.deletePanelist) {
                if (!window.confirm('이 면접관과 평가 내용을 삭제하시겠습니까?')) { button.disabled = false; return; }
                await apiRequest(`panelists/${button.dataset.deletePanelist}`, { method: 'DELETE' });
                await loadCandidate();
                setFeedback('면접관을 삭제했습니다.', 'success');
            } else if (button.id === 'addPanelist') {
                const name = byId('newPanelistName').value.trim();
                if (!name) { byId('newPanelistName').focus(); button.disabled = false; return; }
                await apiRequest(`candidates/${CANDIDATE_ID}/panelists`, { method: 'POST', body: JSON.stringify({ name }) });
                await loadCandidate();
                setFeedback('면접관을 추가했습니다.', 'success');
            } else if (button.dataset.deleteFile) {
                if (!window.confirm('이 첨부파일을 삭제하시겠습니까? 저장된 AI 요약도 함께 초기화됩니다.')) { button.disabled = false; return; }
                await apiRequest(`attachments/${button.dataset.deleteFile}`, { method: 'DELETE' });
                await loadCandidate();
                setFeedback('첨부파일을 삭제했습니다. 이력서를 다시 분석해주세요.', 'success');
            } else {
                button.disabled = false;
                return;
            }
            notifyOpener();
        } catch (error) {
            setFeedback(error.message, 'error');
            button.disabled = false;
        }
    }

    // ------------------------------------------------------------ 시작

    byId('sheetMain').addEventListener('click', handleMainClick);
    byId('analyzeResume').addEventListener('click', () => runResumeAnalysis());
    byId('uploadResume').addEventListener('click', uploadResume);
    byId('reloadSheet').addEventListener('click', async () => {
        try { await loadCandidate(); setFeedback('최신 내용을 불러왔습니다.', 'success'); }
        catch (error) { setFeedback(error.message, 'error'); }
    });
    byId('printSheet').addEventListener('click', () => window.print());
    byId('closeSheet').addEventListener('click', () => window.close());
    const zoomBy = (step) => {
        zoomIndex = Math.min(ZOOM_STEPS.length - 1, Math.max(0, zoomIndex + step));
        applyZoom();
    };
    byId('zoomIn').addEventListener('click', () => zoomBy(1));
    byId('zoomOut').addEventListener('click', () => zoomBy(-1));
    byId('focusZoomIn').addEventListener('click', () => zoomBy(1));
    byId('focusZoomOut').addEventListener('click', () => zoomBy(-1));
    byId('exitFocus').addEventListener('click', closeFocus);
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && state.focus) closeFocus();
    });

    byId('focusBody').addEventListener('click', handleFocusClick);
    bindStars(byId('sheetMain'));
    bindStars(byId('focusBody'));
    initZoom();
    bindDropZone();
    (async () => {
        try {
            const item = await loadCandidate();
            const autoAnalyze = new URLSearchParams(window.location.search).get('analyze') === '1';
            if (autoAnalyze && item.can_manage && item.attachment_count
                && !(item.resume_analysis && item.resume_analysis.is_ready)) {
                await runResumeAnalysis();
            }
        } catch (error) {
            byId('sheetMain').innerHTML = `<section class="ivs-card"><div class="ivs-ai-empty is-error">
                <i class="fa-solid fa-triangle-exclamation"></i>
                <div><strong>면접 정보를 불러오지 못했습니다.</strong><p>${escapeHtml(error.message)}</p></div></div></section>`;
        }
    })();
})();
