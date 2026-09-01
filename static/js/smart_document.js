(function () {
    'use strict';

    const byId = (id) => document.getElementById(id);
    const prompt = byId('documentPrompt');
    const counter = byId('promptCounter');
    const fileInput = byId('documentFiles');
    const dropzone = byId('documentDropzone');
    const fileList = byId('documentFileList');
    const deliveryFileInput = byId('deliveryFiles');
    const deliveryDropzone = byId('deliveryDropzone');
    const deliveryFileList = byId('deliveryFileList');
    const generateButton = byId('generateDocument');
    const progress = byId('documentProgress');
    const progressTitle = byId('progressTitle');
    const progressMessage = byId('progressMessage');
    const progressPercent = byId('progressPercent');
    const progressBar = byId('progressBar');
    const progressLog = byId('progressLog');
    const managementModal = byId('managementModal');
    const editDocumentModal = byId('editDocumentModal');
    const emailDocumentModal = byId('emailDocumentModal');
    // 상단 메뉴바가 만드는 별도 stacking context(.navbar z-index:1000)에 눌리지 않도록
    // 모달을 .content-container 밖으로 꺼내 body 바로 아래에 둔다.
    [managementModal, editDocumentModal, emailDocumentModal].forEach((modal) => { if (modal) document.body.appendChild(modal); });
    const managementFeedback = byId('managementFeedback');
    const openAiStatusText = byId('openAiStatusText');
    const composeCompany = byId('composeCompany');
    const composeTemplate = byId('composeTemplate');
    const composeRecipient = byId('composeRecipient');

    let selectedFiles = [];
    let selectedDeliveryFiles = [];
    let settingsState = { csrfToken: '', settings: null };
    let workspaceState = { companies: [], templates: [], recipients: [], senders: [], history: [] };
    let currentDocument = null;
    let currentHistoryId = null;
    let currentPreviewMode = 'draft';
    const TEMPLATE_ITEM_COUNT = 7;
    const allowedExtensions = new Set(['pdf', 'hwp', 'hwpx', 'doc', 'docx']);
    const allowedDeliveryExtensions = new Set(['pdf', 'hwp', 'hwpx', 'doc', 'docx', 'xls', 'xlsx', 'png', 'jpg', 'jpeg', 'webp', 'zip']);
    const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
    const fileKey = (file) => `${file.name}:${file.size}:${file.lastModified}`;
    const fileSize = (bytes) => bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    const formatNumber = (value) => Number(value || 0).toLocaleString('ko-KR');

    function nonJsonErrorMessage(response) {
        if (response.redirected && /login/i.test(response.url || '')) return '로그인 세션이 만료되었습니다. 페이지를 새로고침한 뒤 다시 로그인해주세요.';
        if (response.status === 404) return '요청한 스마트 공문 API를 찾을 수 없습니다. 최신 서버가 실행 중인지 확인해주세요.';
        if (response.status === 413) return '첨부파일 용량이 서버 제한을 초과했습니다.';
        if (response.status >= 500) return `서버 내부 오류가 발생했습니다. (HTTP ${response.status})`;
        return `요청 응답 형식이 올바르지 않습니다. (HTTP ${response.status})`;
    }

    async function apiRequest(path, options = {}) {
        const method = options.method || 'GET';
        const isFormData = options.body instanceof FormData;
        const headers = { Accept: 'application/json', ...(options.headers || {}) };
        if (method !== 'GET') {
            if (!isFormData) headers['Content-Type'] = 'application/json';
            headers['X-CSRF-Token'] = settingsState.csrfToken;
        }
        const response = await fetch(`/smart-document/api/${path}`, { ...options, method, headers });
        const isJson = (response.headers.get('Content-Type') || '').includes('application/json');
        const data = isJson ? await response.json().catch(() => ({})) : {};
        if (!response.ok || data.status !== 'success') {
            const error = new Error(data.message || nonJsonErrorMessage(response));
            error.data = data;
            throw error;
        }
        return data;
    }

    function setManagementFeedback(message = '', type = '') {
        managementFeedback.textContent = message;
        managementFeedback.style.color = type === 'error' ? '#c43d3d' : type === 'success' ? '#087b67' : '';
    }
    function syncCounter() { counter.textContent = `${prompt.value.length.toLocaleString('ko-KR')} / 3,000`; }
    function setWorkflow(activeStep) {
        const order = ['request', 'generate', 'preview', 'saved'];
        const activeIndex = Math.max(0, order.indexOf(activeStep));
        document.querySelectorAll('[data-flow-step]').forEach((item) => {
            const index = order.indexOf(item.dataset.flowStep);
            item.classList.toggle('is-current', index === activeIndex);
            item.classList.toggle('is-complete', index < activeIndex);
        });
    }

    function renderFiles(files, container) {
        container.innerHTML = files.map((file, index) => `
            <div class="sd-file-item"><i class="fa-regular fa-file-lines"></i><div><strong title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</strong><small>${fileSize(file.size)}</small></div><button class="sd-remove-file" type="button" data-file-index="${index}" aria-label="파일 제거"><i class="fa-solid fa-xmark"></i></button></div>
        `).join('');
    }
    function addFiles(files, selected, container, input, allowed) {
        const known = new Set(selected.map(fileKey));
        Array.from(files).forEach((file) => {
            const extension = file.name.split('.').pop().toLowerCase();
            if (allowed.has(extension) && !known.has(fileKey(file))) { selected.push(file); known.add(fileKey(file)); }
        });
        renderFiles(selected, container); input.value = '';
    }

    function syncModalOpenState() {
        const open = [managementModal, editDocumentModal, emailDocumentModal].some((item) => item && !item.hidden);
        document.body.classList.toggle('sd-modal-open', open);
    }
    function applyOpenAiStatus(settings) {
        const connected = settings && settings.source !== 'none';
        if (openAiStatusText) {
            openAiStatusText.textContent = !settings
                ? '설정 조회 실패'
                : connected
                    ? `SaeDam AI Preset ${settings.preset_id} - ${settings.model_short_name}.`
                    : 'SaeDam AI Preset 미설정';
        }
    }
    async function loadAiSettings() {
        try {
            const data = await apiRequest('settings');
            settingsState.csrfToken = data.csrf_token; settingsState.settings = data.settings;
            applyOpenAiStatus(data.settings);
        } catch (error) { applyOpenAiStatus(null); }
    }

    function renderComposeSelects() {
        const previous = { company: composeCompany.value, template: composeTemplate.value, recipient: composeRecipient.value };
        composeCompany.innerHTML = workspaceState.companies.length ? workspaceState.companies.map((item) => `<option value="${item.id}">${escapeHtml(item.name)}${item.is_default ? ' · 기본' : ''}</option>`).join('') : '<option value="">회사 정보를 등록해주세요</option>';
        composeTemplate.innerHTML = workspaceState.templates.length ? workspaceState.templates.map((item) => `<option value="${item.id}">${escapeHtml(item.name)}${item.is_default ? ' · 기본' : ''}</option>`).join('') : '<option value="">템플릿을 등록해주세요</option>';
        composeRecipient.innerHTML = '<option value="">요청 내용에서 수신처 판단</option>' + workspaceState.recipients.map((item) => `<option value="${item.id}">${escapeHtml(item.organization)}${item.name ? ` · ${escapeHtml(item.name)}` : ''}</option>`).join('');
        if (workspaceState.companies.some((item) => String(item.id) === previous.company)) composeCompany.value = previous.company;
        else if (workspaceState.companies.length) composeCompany.value = String((workspaceState.companies.find((item) => item.is_default) || workspaceState.companies[0]).id);
        if (workspaceState.templates.some((item) => String(item.id) === previous.template)) composeTemplate.value = previous.template;
        else if (workspaceState.templates.length) composeTemplate.value = String((workspaceState.templates.find((item) => item.is_default) || workspaceState.templates[0]).id);
        if (workspaceState.recipients.some((item) => String(item.id) === previous.recipient)) composeRecipient.value = previous.recipient;
    }
    function renderCompanyList() {
        byId('companyList').innerHTML = workspaceState.companies.length ? workspaceState.companies.map((item) => {
            const sealThumb = item.has_seal
                ? `<img class="sd-company-seal-thumb" src="${escapeHtml(item.seal_url)}?v=${encodeURIComponent(item.updated_at || '')}" alt="${escapeHtml(item.name)} 직인">`
                : '<span class="sd-company-seal-thumb is-empty">직인<br>미등록</span>';
            return `<article class="sd-resource-card has-seal">${sealThumb}<div><h4>${escapeHtml(item.name)} ${item.is_default ? '<em>기본</em>' : ''}</h4><p>대표 ${escapeHtml(item.representative)} · 문서번호 ${escapeHtml(item.document_prefix)}-연도-순번<br>${item.has_seal ? '직인 등록 완료' : '직인 미등록'}</p></div><div class="sd-resource-actions"><button data-edit-company="${item.id}">수정</button><button class="danger" data-delete-company="${item.id}">삭제</button></div></article>`;
        }).join('') : '<div class="sd-empty-list">발송 회사를 먼저 등록해주세요.</div>';
    }
    function renderTemplateList() {
        byId('templateList').innerHTML = workspaceState.templates.length ? workspaceState.templates.map((item) => {
            const enabledCount = (item.items || []).filter((entry) => entry.enabled).length;
            const summary = `입력내용 ${enabledCount}개 사용${item.greeting_enabled ? ' · 인사말' : ''}${item.closing_enabled ? ' · 맺음말' : ''}`;
            return `<article class="sd-resource-card"><div><h4>${escapeHtml(item.name)} ${item.is_default ? '<em>기본</em>' : ''}</h4><p>${escapeHtml(item.instruction || '별도 작성 지침 없음')}<br><small>${escapeHtml(summary)}</small></p></div><div class="sd-resource-actions"><button data-edit-template="${item.id}">수정</button><button class="danger" data-delete-template="${item.id}">삭제</button></div></article>`;
        }).join('') : '<div class="sd-empty-list">등록된 템플릿이 없습니다.</div>';
    }
    function renderRecipientList() {
        byId('recipientList').innerHTML = workspaceState.recipients.length ? workspaceState.recipients.map((item) => `<article class="sd-resource-card"><div><h4>${escapeHtml(item.organization)}</h4><p>${escapeHtml(item.name || '담당자 미지정')} · ${escapeHtml(item.email)}</p></div><div class="sd-resource-actions"><button data-edit-recipient="${item.id}">수정</button><button class="danger" data-delete-recipient="${item.id}">삭제</button></div></article>`).join('') : '<div class="sd-empty-list">공문 발송용 수신자를 등록해주세요.</div>';
    }
    function renderHistoryList() {
        byId('historyList').innerHTML = workspaceState.history.length ? workspaceState.history.map((item) => {
            const sent = Number(item.sent_count || 0) > 0;
            const viewButton = sent
                ? `<button data-view-sent-history="${item.id}">발송공문 보기</button>`
                : `<button data-view-history="${item.id}">작성 공문 보기</button>`;
            return `<article class="sd-history-row"><div><h4>${escapeHtml(item.document_number)} · ${escapeHtml(item.subject)}</h4><p>${sent ? `이메일 발송 ${formatNumber(item.sent_count)}회` : '초안 저장'} · 수신 ${escapeHtml(item.recipient)} · 발송일 ${escapeHtml(item.dispatch_date || item.issue_date)} · ${formatNumber(item.total_tokens)} tokens</p></div><div class="sd-resource-actions">${viewButton}<button class="danger" data-delete-history="${item.id}">기록 삭제</button></div></article>`;
        }).join('') : '<div class="sd-empty-list">아직 작성한 공문 기록이 없습니다.</div>';
    }
    function renderWorkspace() { renderComposeSelects(); renderCompanyList(); renderTemplateList(); renderRecipientList(); renderHistoryList(); }
    async function loadWorkspace() {
        try {
            const data = await apiRequest('workspace');
            workspaceState = { companies: data.companies || [], templates: data.templates || [], recipients: data.recipients || [], senders: data.senders || [], history: data.history || [] };
            renderWorkspace();
        } catch (error) { setManagementFeedback(error.message, 'error'); }
    }

    function activateManagementTab(tab) {
        document.querySelectorAll('[data-management-tab]').forEach((button) => button.classList.toggle('active', button.dataset.managementTab === tab));
        document.querySelectorAll('[data-panel]').forEach((panel) => { panel.hidden = panel.dataset.panel !== tab; });
        const labels = { companies: '회사 정보', templates: '공문 템플릿', recipients: '수신자 목록', history: '사용 기록' };
        byId('managementTitle').textContent = labels[tab] || '스마트 공문 관리';
    }
    function openManagement(tab = 'companies', message = '') { activateManagementTab(tab); setManagementFeedback(message, message ? 'error' : ''); managementModal.hidden = false; document.body.classList.add('sd-modal-open'); }
    function closeManagement() { managementModal.hidden = true; syncModalOpenState(); }
    function resetCompanyForm() { byId('companyForm').reset(); byId('companyId').value = ''; byId('companyDocumentPrefix').value = '새담'; }
    function renderTemplateItemRows() {
        const container = byId('templateItems');
        container.innerHTML = Array.from({ length: TEMPLATE_ITEM_COUNT }, (_, index) => `
            <div class="sd-template-item-row" data-item-index="${index}">
                <span class="sd-template-item-no">${index + 1}</span>
                <input type="text" class="sd-template-item-label" placeholder="예) 파견 사유 및 강사 세부사항">
                <label class="sd-inline-check"><input type="checkbox" class="sd-template-item-enabled"> 입력사용</label>
                <label class="sd-inline-check"><input type="checkbox" class="sd-template-item-ai" disabled> Ai 가 제목내용 작성</label>
            </div>
        `).join('');
    }
    function templateItemRows() { return Array.from(byId('templateItems').querySelectorAll('.sd-template-item-row')); }
    function getTemplateItems() {
        return templateItemRows().map((row) => ({
            label: row.querySelector('.sd-template-item-label').value.trim(),
            enabled: row.querySelector('.sd-template-item-enabled').checked,
            ai_generate: row.querySelector('.sd-template-item-ai').checked,
        }));
    }
    function setTemplateItems(items) {
        const list = Array.isArray(items) ? items : [];
        templateItemRows().forEach((row, index) => {
            const item = list[index] || {};
            row.querySelector('.sd-template-item-label').value = item.label || '';
            row.querySelector('.sd-template-item-enabled').checked = Boolean(item.enabled);
            const aiCheckbox = row.querySelector('.sd-template-item-ai');
            aiCheckbox.checked = Boolean(item.ai_generate);
            aiCheckbox.disabled = !item.enabled;
            row.classList.toggle('is-locked', !item.enabled);
        });
    }
    function enforceTemplateItemOrder(changedRow) {
        const rows = templateItemRows();
        const changedIndex = Number(changedRow.dataset.itemIndex);
        const enabledCheckbox = changedRow.querySelector('.sd-template-item-enabled');
        if (enabledCheckbox.checked) {
            const previousAllEnabled = rows.slice(0, changedIndex).every((row) => row.querySelector('.sd-template-item-enabled').checked);
            if (!previousAllEnabled) {
                enabledCheckbox.checked = false;
                setManagementFeedback(`${changedIndex}번을 먼저 체크해야 ${changedIndex + 1}번을 체크할 수 있습니다.`, 'error');
                return;
            }
        } else {
            rows.slice(changedIndex).forEach((row) => { row.querySelector('.sd-template-item-enabled').checked = false; });
        }
        rows.forEach((row) => {
            const enabled = row.querySelector('.sd-template-item-enabled').checked;
            const aiCheckbox = row.querySelector('.sd-template-item-ai');
            aiCheckbox.disabled = !enabled;
            if (!enabled) aiCheckbox.checked = false;
            row.classList.toggle('is-locked', !enabled);
        });
    }
    function resetTemplateForm() {
        byId('templateForm').reset(); byId('templateId').value = ''; byId('templateClosing').value = '끝.';
        byId('templateGreetingEnabled').checked = true; byId('templateClosingEnabled').checked = true;
        setTemplateItems([]);
    }
    function resetRecipientForm() { byId('recipientForm').reset(); byId('recipientId').value = ''; }

    async function saveCompany(event) {
        event.preventDefault(); const id = byId('companyId').value;
        const payload = { name: byId('companyName').value.trim(), representative: byId('companyRepresentative').value.trim(), business_number: byId('companyBusinessNumber').value.trim(), address: byId('companyAddress').value.trim(), phone: byId('companyPhone').value.trim(), email: byId('companyEmail').value.trim(), document_prefix: byId('companyDocumentPrefix').value.trim(), is_default: byId('companyDefault').checked };
        try {
            const data = await apiRequest(id ? `companies/${id}` : 'companies', { method: id ? 'PUT' : 'POST', body: JSON.stringify(payload) });
            const seal = byId('companySeal').files[0];
            if (seal) { const formData = new FormData(); formData.append('seal', seal, seal.name); await apiRequest(`companies/${data.company.id}/seal`, { method: 'POST', body: formData }); }
            await loadWorkspace(); resetCompanyForm(); setManagementFeedback(seal ? '회사 정보와 직인을 저장했습니다.' : data.message, 'success');
        } catch (error) { setManagementFeedback(error.message, 'error'); }
    }
    async function saveTemplate(event) {
        event.preventDefault(); const id = byId('templateId').value;
        const payload = {
            name: byId('templateName').value.trim(), instruction: byId('templateInstruction').value.trim(),
            subject: byId('templateSubject').value.trim(), recipient: byId('templateRecipient').value.trim(),
            greeting: byId('templateGreeting').value.trim(), closing: byId('templateClosing').value.trim(),
            greeting_enabled: byId('templateGreetingEnabled').checked, closing_enabled: byId('templateClosingEnabled').checked,
            items: getTemplateItems(), is_default: byId('templateDefault').checked,
        };
        try { const data = await apiRequest(id ? `templates/${id}` : 'templates', { method: id ? 'PUT' : 'POST', body: JSON.stringify(payload) }); await loadWorkspace(); resetTemplateForm(); setManagementFeedback(data.message, 'success'); }
        catch (error) { setManagementFeedback(error.message, 'error'); }
    }
    async function saveRecipient(event) {
        event.preventDefault(); const id = byId('recipientId').value;
        const payload = { organization: byId('recipientOrganization').value.trim(), name: byId('recipientName').value.trim(), email: byId('recipientEmail').value.trim(), memo: byId('recipientMemo').value.trim() };
        try { const data = await apiRequest(id ? `recipients/${id}` : 'recipients', { method: id ? 'PUT' : 'POST', body: JSON.stringify(payload) }); await loadWorkspace(); resetRecipientForm(); setManagementFeedback(data.message, 'success'); }
        catch (error) { setManagementFeedback(error.message, 'error'); }
    }

    function progressStep(percent, title, message, log = '') {
        progressTitle.textContent = title; progressMessage.textContent = message; progressPercent.textContent = `${percent}%`; progressBar.style.width = `${percent}%`;
        if (log) progressLog.insertAdjacentHTML('beforeend', `<li>${escapeHtml(log)}</li>`);
    }
    function markdownTableCells(value) {
        const text = String(value || '').trim();
        if (!text.startsWith('|') || !text.endsWith('|')) return null;
        const cells = text.slice(1, -1).split('|').map((item) => item.trim());
        return cells.length >= 2 ? cells : null;
    }
    function renderDocumentTable(title, headers, rows) {
        if (!Array.isArray(headers) || !headers.length) return '';
        const columnCount = Math.min(headers.length, 8);
        const safeHeaders = headers.slice(0, columnCount);
        const safeRows = (rows || []).slice(0, 50).filter(Array.isArray).map((row) => {
            const cells = row.slice(0, columnCount);
            while (cells.length < columnCount) cells.push('');
            return cells;
        });
        return `<section class="sd-doc-table-block">${title ? `<h4>${escapeHtml(title)}</h4>` : ''}<div class="sd-doc-table-wrap"><table class="sd-doc-table"><thead><tr>${safeHeaders.map((item) => `<th>${escapeHtml(item)}</th>`).join('')}</tr></thead><tbody>${safeRows.map((row) => `<tr>${row.map((item) => `<td>${escapeHtml(item)}</td>`).join('')}</tr>`).join('')}</tbody></table></div></section>`;
    }
    function renderDocumentBody(paragraphs, bodyTables = {}) {
        const attachedTables = bodyTables && typeof bodyTables === 'object' ? bodyTables : {};
        const renderedTables = new Set();
        let mainNumber = 0;
        const items = (paragraphs || []).flatMap((value) => {
            const lines = String(value || '').split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
            return lines.length > 1 && lines.some(markdownTableCells) ? lines : [value];
        });
        const output = [];
        let index = 0;
        while (index < items.length) {
            const firstTableRow = markdownTableCells(items[index]);
            if (firstTableRow) {
                const markdownRows = [];
                while (index < items.length) {
                    const row = markdownTableCells(items[index]);
                    if (!row) break;
                    markdownRows.push(row);
                    index += 1;
                }
                const rows = markdownRows.slice(1).filter((row) => !row.every((cell) => /^:?-{3,}:?$/.test(cell)));
                output.push(renderDocumentTable('', markdownRows[0], rows));
                continue;
            }
            const value = items[index];
            index += 1;
            const paragraph = String(value || '').trim();
            if (!paragraph) continue;
            const numbered = paragraph.match(/^(\d+)\s*[.)]\s*([\s\S]*)$/);
            const subNumbered = paragraph.match(/^([가-힣])\s*[.)]\s*([\s\S]*)$/);
            let marker;
            let content;
            let className = 'sd-doc-paragraph';
            if (numbered) {
                mainNumber = Math.max(mainNumber, Number(numbered[1]) || 0);
                marker = `${numbered[1]}.`;
                content = numbered[2];
            } else if (subNumbered) {
                marker = `${subNumbered[1]}.`;
                content = subNumbered[2];
                className += ' is-sub-item';
            } else {
                mainNumber += 1;
                marker = `${mainNumber}.`;
                content = paragraph;
            }
            output.push(`<p class="${className}"><span>${escapeHtml(marker)}</span><span>${escapeHtml(content).replace(/\n/g, '<br>')}</span></p>`);
            if (numbered) {
                const itemKey = numbered[1];
                const attached = attachedTables[itemKey];
                if (attached && !renderedTables.has(itemKey)) {
                    renderedTables.add(itemKey);
                    output.push(renderDocumentTable(attached.title || '', attached.headers || [], attached.rows || []));
                }
            }
        }
        return output.join('');
    }
    function renderStructuredTables(tables) {
        return (tables || []).map((table) => renderDocumentTable(table.title || '', table.headers || [], table.rows || [])).join('');
    }
    function renderDocumentPreview(docData, historyId = currentHistoryId, renderedHtml = '', viewMode = 'draft') {
        currentDocument = { ...docData, body: [...(docData.body || [])] };
        currentHistoryId = historyId ? Number(historyId) : null;
        currentPreviewMode = viewMode === 'sent' ? 'sent' : 'draft';
        const body = renderDocumentBody(docData.body, docData.body_tables);
        const tables = renderStructuredTables(docData.tables);
        const deliveryAttachments = Array.isArray(docData.delivery_attachments) ? docData.delivery_attachments : [];
        const attachments = deliveryAttachments.length ? `<section class="sd-doc-attachments"><strong>붙임</strong><ol>${deliveryAttachments.map((item) => `<li>${escapeHtml(item.filename || item)}</li>`).join('')}</ol></section>` : '';
        const seal = docData.seal_url ? `<img class="sd-doc-seal" src="${escapeHtml(docData.seal_url)}?v=${Date.now()}" alt="회사 직인">` : '<span class="sd-seal-missing">(직인 미등록)</span>';
        const fallbackHtml = `<article class="sd-official-document">
            <header class="sd-doc-header"><span>SAEDAM OFFICIAL DOCUMENT</span><h3>${escapeHtml(docData.title || '공 문')}</h3></header>
            <table class="sd-doc-meta"><tbody><tr><th>문서번호</th><td>${escapeHtml(docData.document_number || '확인 필요')}</td><th>발송일</th><td>${escapeHtml(docData.dispatch_date || docData.issue_date || docData.date || '확인 필요')}</td></tr><tr><th>수&nbsp;&nbsp;&nbsp;&nbsp;신</th><td colspan="3">${escapeHtml(docData.recipient || '확인 필요')}</td></tr><tr><th>발&nbsp;&nbsp;&nbsp;&nbsp;신</th><td colspan="3">${escapeHtml(docData.sender_company || docData.sender || '확인 필요')}</td></tr><tr class="sd-doc-subject"><th>제&nbsp;&nbsp;&nbsp;&nbsp;목</th><td colspan="3">${escapeHtml(docData.subject || '')}</td></tr></tbody></table>
            <section class="sd-doc-content">${body}${tables}${attachments}${docData.closing ? `<p class="sd-doc-closing">${escapeHtml(docData.closing)}</p>` : ''}</section>
            <div class="sd-doc-signature"><strong>${escapeHtml(docData.sender_company || docData.sender || '')} 대표 ${escapeHtml(docData.representative || '')}</strong>${seal}</div>
            <footer class="sd-doc-footer"><span>${escapeHtml(docData.company_address || '')}<br>담당 연락처 · ${escapeHtml(docData.contact || '확인 필요')}</span></footer></article>`;
        byId('documentPreview').innerHTML = renderedHtml || fallbackHtml;
        byId('editDocumentButton').disabled = !currentHistoryId || currentPreviewMode === 'sent';
        byId('emailDocumentButton').disabled = !currentHistoryId || currentPreviewMode === 'sent';
        byId('pdfDocumentButton').disabled = !currentHistoryId;
    }
    function clearDocumentPreview() {
        currentDocument = null; currentHistoryId = null; currentPreviewMode = 'draft';
        byId('documentPreview').innerHTML = '<div class="sd-paper-placeholder"><span><i class="fa-regular fa-file-lines"></i></span><strong>작성된 공문이 여기에 표시됩니다</strong><p>위에서 요청 내용과 참고 파일을 입력한 뒤<br>AI 공문 작성 버튼을 눌러주세요.</p></div>';
        ['editDocumentButton', 'emailDocumentButton', 'pdfDocumentButton'].forEach((id) => { byId(id).disabled = true; });
    }
    async function downloadDocumentPdf() {
        if (!currentHistoryId) return;
        const button = byId('pdfDocumentButton');
        const original = button.innerHTML; button.disabled = true; button.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> 생성 중';
        try {
            const query = currentPreviewMode === 'sent' ? '?sent=1' : '';
            const response = await fetch(`/smart-document/api/history/${currentHistoryId}/pdf${query}`, { headers: { Accept: 'application/pdf, application/json' } });
            if (!response.ok) {
                const data = await response.json().catch(() => ({}));
                throw new Error(data.message || `PDF 생성에 실패했습니다. (HTTP ${response.status})`);
            }
            const blob = await response.blob();
            const url = URL.createObjectURL(blob); const link = document.createElement('a');
            link.href = url; link.download = `${currentDocument.document_number || '공문'}.pdf`; document.body.appendChild(link); link.click(); link.remove();
            window.setTimeout(() => URL.revokeObjectURL(url), 1000);
        } catch (error) { window.alert(error.message); }
        finally { button.disabled = false; button.innerHTML = original; }
    }
    function setActionFeedback(elementId, message = '', type = '') {
        const element = byId(elementId);
        element.textContent = message;
        element.classList.toggle('is-error', type === 'error');
        element.classList.toggle('is-success', type === 'success');
    }
    function openEditDocument() {
        if (!currentDocument || !currentHistoryId) return;
        byId('editDocumentSubject').value = currentDocument.subject || '';
        byId('editDocumentRecipient').value = currentDocument.recipient || '';
        byId('editAssignmentStart').value = currentDocument.assignment_start || '';
        byId('editAssignmentEnd').value = currentDocument.assignment_end || '';
        byId('editDocumentGreeting').value = currentDocument.greeting || '';
        byId('editDocumentBody').value = (currentDocument.body || []).join('\n\n');
        byId('editDocumentClosing').value = currentDocument.closing || '';
        byId('editDocumentContact').value = currentDocument.contact || '';
        setActionFeedback('editDocumentFeedback');
        editDocumentModal.hidden = false; syncModalOpenState();
        window.setTimeout(() => byId('editDocumentSubject').focus(), 20);
    }
    function closeEditDocument() { editDocumentModal.hidden = true; syncModalOpenState(); }
    async function saveEditedDocument() {
        if (!currentHistoryId) return;
        const body = byId('editDocumentBody').value.split(/\n\s*\n/).map((item) => item.trim()).filter(Boolean);
        const payload = {
            subject: byId('editDocumentSubject').value.trim(), recipient: byId('editDocumentRecipient').value.trim(),
            assignment_start: byId('editAssignmentStart').value.trim(), assignment_end: byId('editAssignmentEnd').value.trim(),
            greeting: byId('editDocumentGreeting').value.trim(), body,
            closing: byId('editDocumentClosing').value.trim(), contact: byId('editDocumentContact').value.trim()
        };
        const button = byId('saveEditedDocument'); button.disabled = true; button.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> 저장 중';
        setActionFeedback('editDocumentFeedback', '수정 내용을 저장하고 있습니다.');
        try {
            const data = await apiRequest(`history/${currentHistoryId}`, { method: 'PATCH', body: JSON.stringify(payload) });
            renderDocumentPreview(data.document, data.history_id, data.rendered_html, 'draft'); await loadWorkspace();
            setActionFeedback('editDocumentFeedback', data.message, 'success'); window.setTimeout(closeEditDocument, 650);
        } catch (error) { setActionFeedback('editDocumentFeedback', error.message, 'error'); }
        finally { button.disabled = false; button.innerHTML = '<i class="fa-solid fa-floppy-disk"></i> 수정 저장'; }
    }
    function buildEmailDocumentSubject(document) {
        const companyName = String(document?.sender_company || document?.sender || '').trim();
        let documentSubject = String(document?.subject || '공문').trim();
        if (/요청\s*$/.test(documentSubject)) {
            documentSubject = documentSubject.replace(/요청\s*$/, '공문');
        } else if (!/공문\s*$/.test(documentSubject)) {
            documentSubject = `${documentSubject} 공문`;
        }
        return [companyName, documentSubject].filter(Boolean).join(' ');
    }
    function openEmailDocument() {
        if (!currentDocument || !currentHistoryId) return;
        const senderSelect = byId('emailSender');
        senderSelect.innerHTML = '<option value="">발송 계정을 선택해주세요</option>' + workspaceState.senders.map((item) => `<option value="${item.id}">${escapeHtml(item.label)} · ${escapeHtml(item.email)} · ${escapeHtml(item.provider_label || '')}</option>`).join('');
        if (workspaceState.senders.length === 1) senderSelect.value = String(workspaceState.senders[0].id);
        const recipientSelect = byId('emailSavedRecipient');
        recipientSelect.innerHTML = '<option value="">직접 입력</option>' + workspaceState.recipients.map((item) => `<option value="${item.id}">${escapeHtml(item.organization)}${item.name ? ` · ${escapeHtml(item.name)}` : ''} · ${escapeHtml(item.email)}</option>`).join('');
        const savedRecipient = workspaceState.recipients.find((item) => Number(item.id) === Number(currentDocument.recipient_id));
        if (savedRecipient) recipientSelect.value = String(savedRecipient.id);
        byId('emailRecipientAddress').value = currentDocument.recipient_email || savedRecipient?.email || '';
        byId('emailDocumentSubject').value = buildEmailDocumentSubject(currentDocument);
        const deliveryAttachments = Array.isArray(currentDocument.delivery_attachments) ? currentDocument.delivery_attachments : [];
        byId('emailAttachmentSummary').innerHTML = deliveryAttachments.length
            ? `<strong><i class="fa-solid fa-paperclip"></i> 함께 발송할 붙임파일 ${deliveryAttachments.length}개</strong><ul>${deliveryAttachments.map((item) => `<li>${escapeHtml(item.filename || item)}${item.size ? ` · ${fileSize(item.size)}` : ''}</li>`).join('')}</ul>`
            : '<strong><i class="fa-solid fa-paperclip"></i> 함께 발송할 붙임파일 없음</strong><span>공문 내용만 이메일 본문으로 발송됩니다.</span>';
        setActionFeedback('emailDocumentFeedback', workspaceState.senders.length ? '' : '등록된 발송 계정이 없습니다. 스마트명세서 발송계정 메뉴에서 먼저 계정을 등록하고 연결 테스트를 완료해주세요.', workspaceState.senders.length ? '' : 'error');
        emailDocumentModal.hidden = false; syncModalOpenState();
    }
    function closeEmailDocument() { emailDocumentModal.hidden = true; syncModalOpenState(); }
    function applySavedRecipient() {
        const recipient = workspaceState.recipients.find((item) => String(item.id) === byId('emailSavedRecipient').value);
        if (recipient) byId('emailRecipientAddress').value = recipient.email;
    }
    async function sendDocumentEmail() {
        if (!currentHistoryId) return;
        const payload = { sender_id: byId('emailSender').value, recipient_email: byId('emailRecipientAddress').value.trim(), subject: byId('emailDocumentSubject').value.trim() };
        const button = byId('sendDocumentEmail'); button.disabled = true; button.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> 발송 중';
        setActionFeedback('emailDocumentFeedback', '메일 서버에 연결하여 공문을 발송하고 있습니다.');
        try {
            const data = await apiRequest(`history/${currentHistoryId}/send-email`, { method: 'POST', body: JSON.stringify(payload) });
            setActionFeedback('emailDocumentFeedback', `${data.message} (${data.recipient_email})`, 'success'); await loadWorkspace(); window.setTimeout(closeEmailDocument, 900);
        } catch (error) { setActionFeedback('emailDocumentFeedback', error.message, 'error'); }
        finally { button.disabled = false; button.innerHTML = '<i class="fa-regular fa-paper-plane"></i> 이메일 발송'; }
    }
    function showGenerationError(message, code = '') {
        progress.hidden = false; progress.classList.remove('is-complete'); progress.classList.add('is-error');
        progressTitle.textContent = '공문 작성에 실패했습니다.'; progressMessage.textContent = message; progressPercent.textContent = '오류'; progressBar.style.width = '100%'; progressLog.innerHTML = `<li>${escapeHtml(message)}</li>`; setWorkflow('request');
        if (code === 'COMPANY_NOT_CONFIGURED') window.setTimeout(() => openManagement('companies', message), 250);
        if (code === 'TEMPLATE_NOT_CONFIGURED') window.setTimeout(() => openManagement('templates', message), 250);
    }
    async function runPreparationMessages() {
        if (!prompt.value.trim()) { prompt.focus(); prompt.closest('.sd-prompt-shell').style.borderColor = '#d45353'; window.setTimeout(() => { prompt.closest('.sd-prompt-shell').style.borderColor = ''; }, 900); return; }
        if (!composeCompany.value) return openManagement('companies', '공문을 작성하려면 발송 회사 정보를 먼저 등록해주세요.');
        if (!composeTemplate.value) return openManagement('templates', '공문을 작성하려면 사용할 템플릿을 먼저 등록해주세요.');
        generateButton.disabled = true; progress.hidden = false; progress.classList.remove('is-complete', 'is-error'); progressLog.innerHTML = ''; setWorkflow('generate');
        progressStep(10, 'OpenAI 연결 설정을 확인하고 있습니다.', '메뉴 API 키를 우선 확인합니다.', 'API 연결 설정 확인');
        try {
            if (!settingsState.csrfToken) await loadAiSettings();
            if (!settingsState.settings || settingsState.settings.source === 'none') { showGenerationError('OpenAI API 키가 등록되지 않았습니다. AI 연결 설정에서 API 키를 저장해주세요.', 'OPENAI_NOT_CONFIGURED'); return; }
            const formData = new FormData(); formData.append('prompt', prompt.value.trim()); formData.append('company_id', composeCompany.value); formData.append('template_id', composeTemplate.value); formData.append('recipient_id', composeRecipient.value);
            selectedFiles.forEach((file) => formData.append('reference_files', file, file.name));
            selectedDeliveryFiles.forEach((file) => formData.append('delivery_files', file, file.name));
            progressStep(35, '참고자료를 분석하고 AI 공문을 작성하고 있습니다.', selectedFiles.length ? `${selectedFiles.length}개 참고자료의 원문과 요청 내용을 함께 처리하고 있습니다.` : '요청 내용을 바탕으로 공문을 작성하고 있습니다.', `AI 참고자료 ${selectedFiles.length}개 · 메일 붙임파일 ${selectedDeliveryFiles.length}개`);
            const data = await apiRequest('generate', { method: 'POST', body: formData }); setWorkflow('preview');
            progressStep(85, '문서번호와 회사 정보를 공문폼에 적용하고 있습니다.', '시행일·파견일·직인·발송정보를 미리보기에 구성합니다.', 'AI 공문 초안 수신');
            renderDocumentPreview(data.document, data.history_id, data.rendered_html, 'draft'); (data.warnings || []).forEach((item) => progressLog.insertAdjacentHTML('beforeend', `<li>${escapeHtml(item)}</li>`));
            progressStep(100, 'AI 공문 작성과 기록 저장이 완료되었습니다.', `${data.document.document_number} · ${data.model} · 토큰 ${formatNumber(data.usage?.total_tokens)}개 사용`, '공문 미리보기 및 사용 기록 저장 완료');
            progress.classList.add('is-complete'); setWorkflow('saved'); await loadWorkspace(); byId('documentPreviewTitle').scrollIntoView({ behavior: 'smooth', block: 'start' });
        } catch (error) { showGenerationError(error.message, error.data?.code || ''); }
        finally { generateButton.disabled = false; }
    }

    async function handleManagementAction(event) {
        const button = event.target.closest('button'); if (!button) return;
        const find = (items, id) => items.find((item) => String(item.id) === String(id));
        if (button.dataset.editCompany) {
            const item = find(workspaceState.companies, button.dataset.editCompany); if (!item) return;
            byId('companyId').value = item.id; byId('companyName').value = item.name; byId('companyRepresentative').value = item.representative; byId('companyBusinessNumber').value = item.business_number; byId('companyAddress').value = item.address; byId('companyPhone').value = item.phone; byId('companyEmail').value = item.email; byId('companyDocumentPrefix').value = item.document_prefix; byId('companyDefault').checked = item.is_default; byId('companyName').focus();
        } else if (button.dataset.editTemplate) {
            const item = find(workspaceState.templates, button.dataset.editTemplate); if (!item) return;
            byId('templateId').value = item.id; byId('templateName').value = item.name; byId('templateInstruction').value = item.instruction; byId('templateSubject').value = item.subject || ''; byId('templateRecipient').value = item.recipient || ''; byId('templateGreeting').value = item.greeting; byId('templateClosing').value = item.closing; byId('templateGreetingEnabled').checked = item.greeting_enabled; byId('templateClosingEnabled').checked = item.closing_enabled; setTemplateItems(item.items); byId('templateDefault').checked = item.is_default; byId('templateName').focus();
        } else if (button.dataset.editRecipient) {
            const item = find(workspaceState.recipients, button.dataset.editRecipient); if (!item) return;
            byId('recipientId').value = item.id; byId('recipientOrganization').value = item.organization; byId('recipientName').value = item.name; byId('recipientEmail').value = item.email; byId('recipientMemo').value = item.memo; byId('recipientOrganization').focus();
        } else if (button.dataset.deleteCompany || button.dataset.deleteTemplate || button.dataset.deleteRecipient) {
            const kind = button.dataset.deleteCompany ? 'companies' : button.dataset.deleteTemplate ? 'templates' : 'recipients';
            const id = button.dataset.deleteCompany || button.dataset.deleteTemplate || button.dataset.deleteRecipient;
            if (!window.confirm('선택한 항목을 삭제하시겠습니까?')) return;
            try { const data = await apiRequest(`${kind}/${id}`, { method: 'DELETE' }); await loadWorkspace(); setManagementFeedback(data.message, 'success'); }
            catch (error) { setManagementFeedback(error.message, 'error'); }
        } else if (button.dataset.viewHistory || button.dataset.viewSentHistory) {
            const id = button.dataset.viewSentHistory || button.dataset.viewHistory;
            const sent = Boolean(button.dataset.viewSentHistory);
            try { const data = await apiRequest(`history/${id}${sent ? '?sent=1' : ''}`); renderDocumentPreview(data.history.document, data.history.id, data.history.rendered_html, data.history.view_mode); closeManagement(); setWorkflow('saved'); byId('documentPreviewTitle').scrollIntoView({ behavior: 'smooth', block: 'start' }); }
            catch (error) { setManagementFeedback(error.message, 'error'); }
        } else if (button.dataset.deleteHistory) {
            const id = Number(button.dataset.deleteHistory);
            if (!window.confirm('이 공문의 작성기록과 이메일 발송기록을 모두 삭제하시겠습니까?')) return;
            try {
                const data = await apiRequest(`history/${id}`, { method: 'DELETE' });
                if (currentHistoryId === id) clearDocumentPreview();
                await loadWorkspace(); setManagementFeedback(data.message, 'success');
            } catch (error) { setManagementFeedback(error.message, 'error'); }
        }
    }

    prompt.addEventListener('input', syncCounter);
    fileInput.addEventListener('change', (event) => addFiles(event.target.files, selectedFiles, fileList, fileInput, allowedExtensions));
    deliveryFileInput.addEventListener('change', (event) => addFiles(event.target.files, selectedDeliveryFiles, deliveryFileList, deliveryFileInput, allowedDeliveryExtensions));
    fileList.addEventListener('click', (event) => { const button = event.target.closest('[data-file-index]'); if (button) { selectedFiles.splice(Number(button.dataset.fileIndex), 1); renderFiles(selectedFiles, fileList); } });
    deliveryFileList.addEventListener('click', (event) => { const button = event.target.closest('[data-file-index]'); if (button) { selectedDeliveryFiles.splice(Number(button.dataset.fileIndex), 1); renderFiles(selectedDeliveryFiles, deliveryFileList); } });
    ['dragenter', 'dragover'].forEach((name) => dropzone.addEventListener(name, (event) => { event.preventDefault(); dropzone.classList.add('is-dragging'); }));
    ['dragleave', 'drop'].forEach((name) => dropzone.addEventListener(name, (event) => { event.preventDefault(); dropzone.classList.remove('is-dragging'); }));
    dropzone.addEventListener('drop', (event) => addFiles(event.dataTransfer.files, selectedFiles, fileList, fileInput, allowedExtensions));
    ['dragenter', 'dragover'].forEach((name) => deliveryDropzone.addEventListener(name, (event) => { event.preventDefault(); deliveryDropzone.classList.add('is-dragging'); }));
    ['dragleave', 'drop'].forEach((name) => deliveryDropzone.addEventListener(name, (event) => { event.preventDefault(); deliveryDropzone.classList.remove('is-dragging'); }));
    deliveryDropzone.addEventListener('drop', (event) => addFiles(event.dataTransfer.files, selectedDeliveryFiles, deliveryFileList, deliveryFileInput, allowedDeliveryExtensions)); generateButton.addEventListener('click', runPreparationMessages);
    document.querySelectorAll('[data-management]').forEach((button) => button.addEventListener('click', () => openManagement(button.dataset.management)));
    document.querySelectorAll('[data-management-tab]').forEach((button) => button.addEventListener('click', () => activateManagementTab(button.dataset.managementTab)));
    byId('closeManagement').addEventListener('click', closeManagement); byId('closeManagementFooter').addEventListener('click', closeManagement);
    byId('companyForm').addEventListener('submit', saveCompany); byId('templateForm').addEventListener('submit', saveTemplate); byId('recipientForm').addEventListener('submit', saveRecipient);
    byId('resetCompanyForm').addEventListener('click', resetCompanyForm); byId('resetTemplateForm').addEventListener('click', resetTemplateForm); byId('resetRecipientForm').addEventListener('click', resetRecipientForm);
    byId('companyList').addEventListener('click', handleManagementAction); byId('templateList').addEventListener('click', handleManagementAction); byId('recipientList').addEventListener('click', handleManagementAction); byId('historyList').addEventListener('click', handleManagementAction);
    byId('templateItems').addEventListener('change', (event) => { const row = event.target.closest('.sd-template-item-row'); if (row && event.target.classList.contains('sd-template-item-enabled')) enforceTemplateItemOrder(row); });
    byId('editDocumentButton').addEventListener('click', openEditDocument); byId('closeEditDocument').addEventListener('click', closeEditDocument); byId('cancelEditDocument').addEventListener('click', closeEditDocument); byId('saveEditedDocument').addEventListener('click', saveEditedDocument);
    byId('pdfDocumentButton').addEventListener('click', downloadDocumentPdf);
    byId('emailDocumentButton').addEventListener('click', openEmailDocument); byId('closeEmailDocument').addEventListener('click', closeEmailDocument); byId('cancelEmailDocument').addEventListener('click', closeEmailDocument); byId('emailSavedRecipient').addEventListener('change', applySavedRecipient); byId('sendDocumentEmail').addEventListener('click', sendDocumentEmail);
    document.addEventListener('keydown', (event) => { if (event.key === 'Escape') { if (!emailDocumentModal.hidden) closeEmailDocument(); else if (!editDocumentModal.hidden) closeEditDocument(); else if (!managementModal.hidden) closeManagement(); } });
    async function initializeSmartDocument() {
        await loadAiSettings();
        await loadWorkspace();
    }

    renderTemplateItemRows(); syncCounter(); setWorkflow('request'); initializeSmartDocument();
})();
