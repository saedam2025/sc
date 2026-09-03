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

    // 다른 창(진행표·사전질문지)의 변경을 알리는 메시지 종류와 같은 출처 방송채널.
    const INTERVIEW_SYNC_TYPES = new Set([
        'interview-sheet-updated', 'interview-questionnaire-submitted',
    ]);
    const syncChannel = (typeof BroadcastChannel !== 'undefined')
        ? new BroadcastChannel('saedam-interview') : null;

    const PAGE_SIZE = 10;
    const LIST_STAR_COUNT = 5;
    const LIST_STAR_GLYPHS = '<i class="fa-solid fa-star"></i>'.repeat(LIST_STAR_COUNT);
    let state = {
        csrfToken: '', candidates: [], filter: 'all', page: 1,
        selected: new Set(), pageManageableIds: [], handoff: false, handoffDone: false,
    };

    // 면접자에게 자리를 내준 상태인지 이 탭에 기억해 둔다. 면접자가 새로고침해도
    // 목록이 도로 나타나지 않아야 하고, 이미 전송을 마쳤다면 그 표시도 남아야 한다.
    const HANDOFF_KEY = 'saedam_interview_handoff';
    function readHandoff() {
        try { return window.sessionStorage.getItem(HANDOFF_KEY) || ''; }
        catch (error) { return ''; }
    }
    function writeHandoff(value) {
        try {
            if (value) window.sessionStorage.setItem(HANDOFF_KEY, value);
            else window.sessionStorage.removeItem(HANDOFF_KEY);
        } catch (error) { /* 저장 불가 브라우저에서도 감추기는 동작한다 */ }
    }

    const HANDOFF_WRITING = '면접자가 사전질문지를 작성하고 있습니다';
    const HANDOFF_DONE = '면접자가 사전질문지를 작성완료했습니다';

    // 캐릭터 말풍선 문구를 지금 상태에 맞춘다.
    function paintHandoffBubble() {
        const bubble = byId('handoffBubble');
        if (!bubble) return;
        const text = state.handoffDone ? HANDOFF_DONE : HANDOFF_WRITING;
        if (bubble.textContent !== text) {
            bubble.textContent = text;
            // 문구가 바뀔 때만 말풍선이 다시 톡 나타나게 한다.
            bubble.style.animation = 'none';
            void bubble.offsetWidth;
            bubble.style.animation = '';
        }
        byId('handoffNote').classList.toggle('is-done', state.handoffDone);
    }

    // 면접자가 사전질문지 창에서 전송을 누르면 말풍선이 '작성완료'로 바뀐다.
    function markQuestionnaireDone() {
        if (!state.handoff || state.handoffDone) return;
        state.handoffDone = true;
        writeHandoff('done');
        paintHandoffBubble();
    }

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


    // 면접일시는 '2026-09-03 (오후02:30)' 형태로 보여준다. 다른 일시(완료 처리 등)는
    // 기존 24시간 표기를 그대로 쓰므로 이 함수는 면접일시에만 사용한다.
    function interviewWhen(value) {
        const raw = String(value || '').trim();
        if (!raw) return null;
        const parsed = new Date(raw.replace(' ', 'T'));
        if (Number.isNaN(parsed.getTime())) return { day: raw, time: '' };
        const pad = (n) => String(n).padStart(2, '0');
        const hours = parsed.getHours();
        const meridiem = hours < 12 ? '오전' : '오후';
        const hour12 = hours % 12 === 0 ? 12 : hours % 12;
        return {
            day: `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())}`,
            time: `(${meridiem}${pad(hour12)}:${pad(parsed.getMinutes())})`,
        };
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

    // ------------------------------------------------------------ 필터칩 집계

    // 진행상태별 인원을 검색창 오른쪽 필터칩에 그대로 얹어준다.
    function renderSummary() {
        const rows = state.handoff ? [] : state.candidates;
        const counts = { scheduled: 0, ongoing: 0, completed: 0 };
        rows.forEach((item) => { counts[progressState(item)] += 1; });

        byId('cntAll').textContent = rows.length;
        byId('cntScheduled').textContent = counts.scheduled;
        byId('cntOngoing').textContent = counts.ongoing;
        byId('cntCompleted').textContent = counts.completed;
        byId('cntPass').textContent = rows.filter((item) => item.result === 'pass').length;
    }

    // ------------------------------------------------------------ 목록

    function progressState(item) {
        // 서버가 내려주는 값을 우선 사용하고, 옛 응답은 제출 여부로 판단한다.
        if (item.progress_state) return item.progress_state;
        if (item.is_completed) return 'completed';
        return item.has_answers ? 'ongoing' : 'scheduled';
    }

    function matchesFilter(item) {
        if (state.filter === 'scheduled') return progressState(item) === 'scheduled';
        if (state.filter === 'ongoing') return progressState(item) === 'ongoing';
        if (state.filter === 'completed') return item.is_completed;
        if (state.filter === 'pass') return item.result === 'pass';
        return true;
    }

    function whenCell(item) {
        const when = interviewWhen(item.interview_at);
        if (!when) return '<span class="iv-dash">미정</span>';
        return `${escapeHtml(when.day)}<small>${escapeHtml(when.time)}</small>`;
    }

    function statusCell(item) {
        const state_ = progressState(item);
        const progressClass = { scheduled: 'is-progress', ongoing: 'is-ongoing', completed: 'is-complete' }[state_];
        const progressText = item.progress_label
            || { scheduled: '면접예정', ongoing: '면접진행중', completed: '면접완료' }[state_];
        return `<span class="iv-tag ${progressClass}">${escapeHtml(progressText)}</span>`;
    }

    // 최종결과는 글자 대신 아이콘으로 보여준다(합격 하트 · 불합격 깨진 하트 · 보류 모래시계).
    const RESULT_ICONS = { pass: 'fa-heart', fail: 'fa-heart-crack', hold: 'fa-hourglass-half' };

    function resultCell(item) {
        if (!item.result_label) return '<span class="iv-dash">-</span>';
        const tone = item.result === 'pass' ? 'is-pass' : item.result === 'fail' ? 'is-fail' : 'is-hold';
        const icon = RESULT_ICONS[item.result] || RESULT_ICONS.hold;
        const label = escapeHtml(item.result_label);
        return `<span class="iv-result-icon ${tone}" role="img" title="${label}" aria-label="${label}"><i class="fa-solid ${icon}"></i></span>`;
    }

    // 결과안내 열. 합격·불합격이 정해진 지원자에게 '안내전송' 글자가 나오고,
    // 누르면 면접 진행표 창이 열리면서 안내전송 창이 바로 뜬다.
    const GUIDE_RESULTS = new Set(['pass', 'fail']);

    function guideCell(item) {
        if (!item.can_manage || !GUIDE_RESULTS.has(item.result)) return '<span class="iv-dash">-</span>';
        const notice = item.guide_notice || {};
        const sent = Boolean(notice.is_sent);
        const title = sent
            ? `${notice.sent_at || ''} 안내전송${notice.sent_by_name ? ` · ${notice.sent_by_name}` : ''}`
            : `${item.result === 'pass' ? '합격' : '불합격'} 안내메일 보내기`;
        return `<button type="button" class="iv-guide-link${sent ? ' is-sent' : ''}"
                    data-open-guide="${item.id}" title="${escapeHtml(title.trim())}">${
                    sent ? '다시전송' : '안내전송'}</button>`;
    }

    // 분당 타자수를 내지 못한 이유. 값이 비어 보이는 대신 까닭을 알려준다.
    const TYPING_NOTES = {
        none: '자판으로 입력한 기록이 없습니다. 붙여넣기만 했거나 다른 기기에서 옮겨 적었을 수 있습니다.',
        paste: '붙여넣은 글이 많아 타자 속도를 재지 않았습니다.',
        short: '실제 입력이 적어(40타·10초 미만) 타자 속도를 재지 않았습니다.',
        odd: '측정값이 사람이 낼 수 있는 범위를 벗어나 쓰지 않았습니다.',
    };
    const typingReason = (item) => TYPING_NOTES[item.typing_note]
        || '타자 속도를 재지 못했습니다.';
    // 기록에 남는 타수·분당 타자수는 실측값의 75%다. 숫자만 보고 헷갈리지 않도록
    // 마우스를 올리면 실제로 친 타수와 시간을 함께 알려준다.
    const typingSpeedHint = (item) => {
        const detail = item.typing_seconds ? ` (측정 ${item.typing_seconds}초)` : '';
        return `실제로 친 타수·속도의 75%만 인정해 기록한 값입니다.${detail}`;
    };

    function questionnaireCell(item) {
        // 글자를 눌러도 관리 열의 [사전질문지] 버튼과 같은 창이 열린다.
        let note;
        if (!item.has_answers) {
            note = '<span class="iv-progress-note is-wait"><i class="fa-regular fa-clipboard"></i> 미작성</span>';
        } else {
            // 타수는 '작성완료' 글씨 아래 줄에 따로 붙인다.
            // 속도를 못 쟀으면 빈칸 대신 친 타수와 그 까닭을 보여준다.
            let typing;
            if (item.typing_cpm) {
                typing = `<small title="${escapeHtml(typingSpeedHint(item))}">(${item.typing_cpm}타/분)</small>`;
            } else if (item.typing_strokes) {
                typing = `<small class="is-quiet" title="${escapeHtml(typingReason(item))}">(${item.typing_strokes}타 · 속도 미측정)</small>`;
            } else {
                typing = `<small class="is-quiet" title="${escapeHtml(typingReason(item))}">(타자 기록 없음)</small>`;
            }
            note = `<span class="iv-progress-note is-done"><span class="iv-progress-note-main"><i class="fa-solid fa-check"></i> 작성완료</span>${typing}</span>`;
        }
        return `<button type="button" class="iv-questionnaire-open" data-open-question="${item.id}"
            title="사전질문지 열기" aria-label="${escapeHtml(item.name)} 사전질문지 열기">${note}</button>`;
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
        // 감춘 동안에는 표를 아예 비워 둔다. display:none으로 가리기만 하면
        // 지원자 이름과 평가가 문서에 그대로 남는다.
        if (state.handoff) {
            candidateList.innerHTML = '';
            byId('candidatePaging').innerHTML = '';
            state.pageManageableIds = [];
            return;
        }
        const keyword = searchInput.value.trim().toLowerCase();
        const rows = state.candidates.filter((item) => matchesFilter(item) && (!keyword || [
            item.name, item.target_position, item.target_school,
        ].some((value) => String(value || '').toLowerCase().includes(keyword))));

        if (!rows.length) {
            state.page = 1;
            state.pageManageableIds = [];
            candidateList.innerHTML = `<tr><td class="iv-empty-cell" colspan="12">${
                state.candidates.length ? '조건에 맞는 면접 기록이 없습니다.'
                    : '등록된 면접자가 없습니다. 위쪽 <b>면접자 입력</b> 버튼으로 시작해주세요.'
            }</td></tr>`;
            renderPaging(1);
            renderSelection();
            return;
        }

        const totalPages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
        if (state.page > totalPages) state.page = totalPages;
        const offset = (state.page - 1) * PAGE_SIZE;

        const pageRows = rows.slice(offset, offset + PAGE_SIZE);
        state.pageManageableIds = pageRows.filter((item) => item.can_manage).map((item) => item.id);
        candidateList.innerHTML = pageRows.map((item, index) => `
            <tr class="iv-row${item.is_completed ? ' is-completed' : ''}">
                <td class="iv-cell-select" data-label="선택">${item.can_manage ? `<label class="iv-select-label">
                    <input type="checkbox" class="iv-select-box" data-select="${item.id}" aria-label="${escapeHtml(item.name)} 선택">
                    <span>선택</span>
                </label>` : ''}</td>
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
                <td class="iv-cell-materials" data-label="서류 · AI"><div class="iv-cell-stack">${materialCell(item)}</div></td>
                <td class="iv-cell-questionnaire" data-label="사전질문지"><div class="iv-cell-stack">${questionnaireCell(item)}</div></td>
                <td class="iv-cell-progress" data-label="진행 현황"><div class="iv-status-line">${statusCell(item)}</div></td>
                <td class="iv-cell-score" data-label="면접관 평가">${scoreCell(item)}</td>
                <td class="iv-cell-result" data-label="최종결과"><div class="iv-status-line">${resultCell(item)}</div></td>
                <td class="iv-cell-guide" data-label="결과안내"><div class="iv-status-line">${guideCell(item)}</div></td>
                <td class="iv-cell-actions" data-label="관리">
                    <div class="iv-actions-layout">
                        <span class="iv-action-sub">
                            <button data-open-question="${item.id}" title="사전질문지 열기" aria-label="사전질문지 열기"><i class="fa-regular fa-clipboard"></i><span>사전질문지</span></button>
                        </span>
                        <button class="primary iv-action-main" data-open-detail="${item.id}"><i class="fa-solid fa-arrow-up-right-from-square"></i><span>면접진행</span></button>
                    </div>
                </td>
            </tr>`).join('');
        renderPaging(totalPages);
        renderSelection();
    }

    // ------------------------------------------------------------ 선택 · 일괄처리

    function renderSelection() {
        // 목록에서 사라졌거나 권한이 없어진 항목은 선택에서 뺀다.
        const manageable = new Set(
            state.candidates.filter((item) => item.can_manage).map((item) => item.id)
        );
        Array.from(state.selected).forEach((id) => {
            if (!manageable.has(id)) state.selected.delete(id);
        });

        Array.from(candidateList.querySelectorAll('[data-select]')).forEach((box) => {
            box.checked = state.selected.has(Number(box.dataset.select));
            box.closest('tr').classList.toggle('is-selected', box.checked);
        });

        const pageIds = state.pageManageableIds;
        const pickedOnPage = pageIds.filter((id) => state.selected.has(id)).length;
        const selectAll = byId('selectAllRows');
        selectAll.checked = pageIds.length > 0 && pickedOnPage === pageIds.length;
        selectAll.indeterminate = pickedOnPage > 0 && pickedOnPage < pageIds.length;
        selectAll.disabled = !pageIds.length;

        const picked = state.selected.size;
        byId('bulkCount').textContent = picked ? `선택 ${picked}건` : '선택한 면접자 없음';
        byId('bulkEdit').disabled = picked !== 1;
        byId('bulkEdit').title = picked === 1 ? '' : '수정은 한 명만 선택했을 때 사용할 수 있습니다.';
        byId('bulkDelete').disabled = picked === 0;
    }

    function selectedCandidates() {
        return state.candidates.filter((item) => state.selected.has(item.id));
    }

    async function deleteSelected() {
        const picked = selectedCandidates();
        if (!picked.length) return;
        const names = picked.slice(0, 5).map((item) => item.name).join(', ');
        const more = picked.length > 5 ? ` 외 ${picked.length - 5}명` : '';
        if (!window.confirm(
            `${names}${more} · 총 ${picked.length}명의 면접 기록과 첨부파일을 모두 삭제하시겠습니까?`
        )) return;
        try {
            for (const item of picked) {
                await apiRequest(`candidates/${item.id}`, { method: 'DELETE' });
            }
            state.selected.clear();
            await loadCandidates();
        } catch (error) {
            window.alert(error.message);
            await loadCandidates();
        }
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
            renderSummary();
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
                if (created.candidate) openSheet(created.candidate.id, { analyze: files.length > 0 });
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

    function openSheet(id, options = {}) {
        const query = options.analyze ? '?analyze=1' : (options.guide ? '?guide=1' : '');
        const url = `/interview/sheet/${id}${query}`;
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
        if (!item) return;
        // 전송 완료를 목록에 알려야 해서 opener 연결(noopener 제외)을 유지한다.
        const opened = window.open(item.questionnaire_url, `ivQuestion${id}`,
            'width=880,height=960,resizable=yes,scrollbars=yes');
        if (!opened) {
            window.alert('팝업이 차단되어 사전질문지를 열지 못했습니다. 브라우저의 팝업 차단을 해제해주세요.');
            return;
        }
        opened.focus();
        // 사전질문지가 뜬 뒤에만 감춘다. 팝업이 막혔는데 목록까지 사라지면
        // 담당자가 아무것도 못 하게 된다.
        setHandoff(true);
    }

    // 면접자가 담당자 자리에서 사전질문지를 쓰는 동안 다른 지원자 목록이
    // 뒤에 남지 않도록 감춘다. 되돌리기는 제목 앞 큰 아이콘으로만 한다.
    function setHandoff(on, options = {}) {
        state.handoff = !!on;
        state.handoffDone = !!options.done;
        writeHandoff(state.handoff ? (state.handoffDone ? 'done' : 'writing') : '');
        paintHandoffBubble();
        const page = document.querySelector('.iv-page');
        const hero = byId('heroReveal');
        if (hero) {
            hero.setAttribute('aria-expanded', state.handoff ? 'false' : 'true');
            hero.title = state.handoff ? '면접자 목록 다시 열기' : '면접자 목록 열기';
            hero.setAttribute('aria-label', hero.title);
        }
        const apply = () => {
            if (page) page.classList.toggle('is-handoff', state.handoff);
            byId('handoffNote').hidden = !state.handoff;
            renderList();
            renderSummary();
        };
        // '스르륵' 사라지도록 잠깐 흐려진 뒤에 실제로 지운다.
        if (state.handoff && !options.instant && page) {
            page.classList.add('is-handoff-leaving');
            window.setTimeout(() => {
                page.classList.remove('is-handoff-leaving');
                apply();
            }, 380);
            return;
        }
        apply();
    }

    // ------------------------------------------------------------ 이벤트

    async function handleListClick(event) {
        const button = event.target.closest('button');
        if (!button) return;
        if (button.dataset.openDetail) return openSheet(button.dataset.openDetail);
        if (button.dataset.openGuide) return openSheet(button.dataset.openGuide, { guide: true });
        if (button.dataset.openQuestion) return openQuestionnaire(button.dataset.openQuestion);
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
    candidateList.addEventListener('change', (event) => {
        const box = event.target.closest('[data-select]');
        if (!box) return;
        const id = Number(box.dataset.select);
        if (box.checked) state.selected.add(id); else state.selected.delete(id);
        renderSelection();
    });
    byId('selectAllRows').addEventListener('change', (event) => {
        state.pageManageableIds.forEach((id) => {
            if (event.target.checked) state.selected.add(id); else state.selected.delete(id);
        });
        renderSelection();
    });
    byId('bulkEdit').addEventListener('click', () => {
        const picked = selectedCandidates();
        if (picked.length !== 1) return;
        openCandidateModal(picked[0]);
    });
    byId('bulkDelete').addEventListener('click', deleteSelected);
    searchInput.addEventListener('input', () => { state.page = 1; renderList(); });
    byId('candidatePaging').addEventListener('click', (event) => {
        const button = event.target.closest('button[data-page]');
        if (!button) return;
        state.page = Number(button.dataset.page) || 1;
        renderList();
        byId('listToolbar').scrollIntoView({ behavior: 'smooth', block: 'center' });
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
    // ------------------------------------------------------------ 다른 창의 변경 반영
    // 진행표·사전질문지는 별도 창에서 열리므로, 그쪽에서 저장이 끝나면 목록을 바로 다시 읽는다.
    // 창을 여는 방식(팝업 차단·opener 없음 등)에 따라 한 가지 경로만으로는 놓치는 경우가 있어
    // ① 같은 출처 방송채널 ② opener 메시지 ③ 창이 다시 활성화될 때, 세 가지를 함께 쓴다.
    let syncTimer = 0;

    function syncList() {
        window.clearTimeout(syncTimer);
        syncTimer = window.setTimeout(() => { loadCandidates(); }, 120);
    }

    // 사전질문지 전송 알림은 목록 새로고침과 함께 말풍선 문구도 바꾼다.
    function handleSyncMessage(data) {
        if (!data || !INTERVIEW_SYNC_TYPES.has(data.type)) return;
        if (data.type === 'interview-questionnaire-submitted') markQuestionnaireDone();
        syncList();
    }

    window.addEventListener('message', (event) => {
        if (event.origin !== window.location.origin) return;
        handleSyncMessage(event.data);
    });

    if (syncChannel) {
        syncChannel.addEventListener('message', (event) => handleSyncMessage(event.data));
    }

    // 진행표 창에서 작업하고 목록 창으로 돌아왔을 때도 최신 상태를 보여준다.
    window.addEventListener('focus', syncList);
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') syncList();
    });

    setupDropZone(byId('createDropZone'));

    // 제목 앞 큰 아이콘: 감춰 둔 면접자 목록을 다시 여는 유일한 통로.
    byId('heroReveal').addEventListener('click', () => {
        if (!state.handoff) return;
        setHandoff(false);
        loadCandidates();
    });

    // 새로고침으로 목록이 도로 나타나지 않도록 감춘 상태를 그대로 복원한다.
    const savedHandoff = readHandoff();
    setHandoff(!!savedHandoff, { instant: true, done: savedHandoff === 'done' });
    loadCandidates();
})();
