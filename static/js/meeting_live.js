/* 회의센터 · 실시간 회의진행 화면.

   - 왼쪽 안건을 클릭하면 가운데에서 크게 보여 주고, 그 안건의 자료를 쪽마다
     썸네일로 늘어놓는다. 썸네일을 누르면 화면에 가득 차게 확대한다.
   - 회의 중에 나온 공통 안건은 왼쪽 [안건 추가하기]로 그 자리에서 넣는다.
   - 오른쪽에서 안건별 논의·결정을 적으면 자동으로 서버에 저장된다.
   - 녹음(MediaRecorder)과 받아쓰기(음성 인식)를 켜 두면 회의가 끝난 뒤
     AI가 그 내용을 회의록으로 정리한다.                                        */
(() => {
  const root = document.getElementById('mtLive');
  const dataNode = document.getElementById('mtStageData');
  if (!root || !dataNode) return;

  /* 상단 인트라넷 메뉴(z-index 1000)보다 위에 덮으려면 쌓임 맥락 밖으로 빼야 한다. */
  if (root.parentElement !== document.body) document.body.appendChild(root);

  let agendas = [];
  try {
    agendas = JSON.parse(dataNode.textContent || '[]');
  } catch (error) {
    agendas = [];
  }

  const meetingId = root.dataset.meetingId;
  const decisionUrlBase = (root.dataset.decisionUrl || '').replace(/\/0$/, '/');
  const transcriptUrl = root.dataset.transcriptUrl;
  const recordingUrl = root.dataset.recordingUrl;
  const minutesUrl = root.dataset.minutesUrl;

  const listEl = document.getElementById('mtAgendaList');
  const stageNo = document.getElementById('mtStageNo');
  const stageTitle = document.getElementById('mtStageTitle');
  const stageSummary = document.getElementById('mtStageSummary');
  const stageOwner = document.getElementById('mtStageOwner');
  const thumbs = document.getElementById('mtThumbs');
  const stageFiles = document.getElementById('mtStageFiles');
  const zoom = document.getElementById('mtZoom');
  const zoomImg = document.getElementById('mtZoomImg');
  const zoomLabel = document.getElementById('mtZoomLabel');
  const zoomPrev = document.getElementById('mtZoomPrev');
  const zoomNext = document.getElementById('mtZoomNext');
  const zoomClose = document.getElementById('mtZoomClose');
  const agendaCount = document.getElementById('mtAgendaCount');
  const minutesText = document.getElementById('mtMinutesText');
  const decisionText = document.getElementById('mtDecisionText');
  const decisionStatus = document.getElementById('mtDecisionStatus');
  const decisionSave = document.getElementById('mtDecisionSave');
  const decisionState = document.getElementById('mtDecisionState');
  const transcript = document.getElementById('mtTranscript');
  const transcriptState = document.getElementById('mtTranscriptState');
  const toast = document.getElementById('mtToast');

  const alertBox = document.getElementById('mtAlert');

  let current = null;      // 지금 확대해서 보고 있는 안건
  let frames = [];         // 지금 안건의 자료를 쪽 단위로 펼친 목록
  let framePos = -1;       // 확대해서 보고 있는 쪽 (-1이면 썸네일 목록)
  let toastTimer = null;
  let sessionLost = false;

  function notify(message, duration) {
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove('show'), duration || 1800);
  }

  /* 화면 위쪽에 계속 남는 안내줄. 저장이 막힌 상태를 사용자가 놓치지 않게 한다. */
  function showAlert(html) {
    if (!alertBox) { notify(String(html).replace(/<[^>]+>/g, ''), 5000); return; }
    alertBox.innerHTML = html;
    alertBox.hidden = false;
  }

  function clearAlert() {
    if (!alertBox) return;
    alertBox.hidden = true;
    alertBox.innerHTML = '';
  }

  // ------------------------------------------------------- 서버 통신 공통부
  /* 로그인이 풀렸을 때를 일반 오류와 구분한다. 예전에는 서버가 로그인 화면을
     200으로 돌려주어, 실제로는 하나도 저장되지 않았는데도 화면에는 '저장됨'
     이라고 표시됐다. */
  class SessionError extends Error {
    constructor() {
      super('로그인이 풀려 저장하지 못했습니다.');
      this.name = 'SessionError';
    }
  }

  function markSessionLost() {
    if (sessionLost) return;
    sessionLost = true;
    showAlert(
      '<b>로그인이 풀려 저장되지 않고 있습니다.</b> '
      + '<a href="/" target="_blank" rel="noopener">새 창에서 다시 로그인</a>한 뒤 '
      + '이 화면으로 돌아와 [이 안건 기록 저장]을 눌러 주세요. '
      + '적어 두신 내용은 이 화면에 그대로 남아 있습니다.'
    );
  }

  function markSessionBack() {
    if (!sessionLost) return;
    sessionLost = false;
    clearAlert();
    notify('연결이 회복되어 다시 저장하고 있습니다.');
  }

  /* 모든 저장 요청은 이 함수를 통한다.
     - 스크립트 요청임을 알려(X-Requested-With) 서버가 401을 주도록 한다.
     - JSON이 아닌 응답(로그인 화면·오류 화면)은 성공으로 보지 않는다. */
  async function request(url, options) {
    const config = Object.assign({ credentials: 'same-origin', cache: 'no-store' }, options || {});
    config.headers = Object.assign({
      'X-Requested-With': 'XMLHttpRequest',
      Accept: 'application/json',
    }, config.headers || {});

    let response;
    try {
      response = await fetch(url, config);
    } catch (error) {
      throw new Error('서버에 연결하지 못했습니다. 인터넷 연결을 확인해 주세요.');
    }

    let data = null;
    if ((response.headers.get('Content-Type') || '').includes('application/json')) {
      data = await response.json().catch(() => null);
    }
    if (response.status === 401 || (data && data.code === 'login_required')) {
      markSessionLost();
      throw new SessionError();
    }
    if (!response.ok) {
      throw new Error((data && data.message) || `저장하지 못했습니다. (오류 ${response.status})`);
    }
    if (!data) {
      // 200이지만 JSON이 아니면 로그인 화면 등으로 넘어간 것이다.
      markSessionLost();
      throw new SessionError();
    }
    markSessionBack();
    return data;
  }

  function postJson(url, payload) {
    return request(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  }

  // ---------------------------------------------------------------- 안건 목록
  function renderList() {
    listEl.innerHTML = '';
    if (!agendas.length) {
      const empty = document.createElement('p');
      empty.className = 'mt-save-state';
      empty.style.padding = '10px';
      empty.textContent = '등록된 안건이 없습니다. [나가기]에서 안건을 먼저 등록해 주세요.';
      listEl.appendChild(empty);
      return;
    }
    agendas.forEach(agenda => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'mt-agenda-btn' + (current && current.id === agenda.id ? ' is-on' : '');
      button.dataset.agendaId = agenda.id;

      const no = document.createElement('b');
      no.textContent = agenda.no;

      const body = document.createElement('span');
      const title = document.createElement('span');
      title.textContent = agenda.title;
      body.appendChild(title);
      const sub = document.createElement('small');
      sub.textContent = (agenda.owner ? agenda.owner : '담당 미지정')
        + (agenda.materials.length ? ' · 자료 ' + agenda.materials.length + '개' : '');
      body.appendChild(sub);

      const dot = document.createElement('span');
      dot.className = 'mt-dot ' + (agenda.decision_status || 'pending');

      button.append(no, body, dot);
      button.addEventListener('click', () => select(agenda.id));
      listEl.appendChild(button);
    });
  }

  // ------------------------------------------- 가운데 : 자료 썸네일 · 확대 보기
  // 올린 파일 순서 → 그 파일의 쪽 순서로 한 줄로 펼친다.
  function buildFrames(agenda) {
    const list = [];
    (agenda ? agenda.materials : []).forEach(material => {
      (material.pages || []).forEach(page => {
        list.push({
          name: material.name,
          pageNo: page.no,
          pages: (material.pages || []).length,
          thumb: page.thumb,
          full: page.full,
        });
      });
    });
    return list;
  }

  function frameLabel(frame) {
    return frame.pages > 1 ? `${frame.name} · ${frame.pageNo}/${frame.pages}쪽` : frame.name;
  }

  function renderThumbs(agenda) {
    thumbs.innerHTML = '';
    frames = buildFrames(agenda);
    if (!frames.length) {
      const none = document.createElement('div');
      none.className = 'mt-stage-none';
      none.textContent = agenda && agenda.materials.length
        ? '이 안건의 자료는 그림으로 바꿀 수 없는 형식입니다. 아래에서 내려받아 주세요.'
        : '이 안건에는 등록된 자료가 없습니다.';
      thumbs.appendChild(none);
      return;
    }
    frames.forEach((frame, index) => {
      const tile = document.createElement('button');
      tile.type = 'button';
      tile.className = 'mt-thumb';
      tile.title = frameLabel(frame);

      const image = document.createElement('img');
      image.src = frame.thumb;
      image.alt = frameLabel(frame);
      image.loading = 'lazy';

      const caption = document.createElement('span');
      caption.className = 'mt-thumb-cap';
      caption.textContent = frame.name;
      const sub = document.createElement('small');
      sub.textContent = frame.pages > 1 ? `${frame.pageNo}/${frame.pages}쪽` : '1쪽';
      caption.appendChild(sub);

      tile.append(image, caption);
      tile.addEventListener('click', () => openZoom(index));
      thumbs.appendChild(tile);
    });
  }

  // 그림으로 바꿀 수 없는 자료는 아래에 내려받기 줄로 남긴다.
  function renderFiles(agenda) {
    stageFiles.innerHTML = '';
    (agenda ? agenda.materials : []).forEach(material => {
      if ((material.pages || []).length) return;
      const link = document.createElement('a');
      link.href = material.url + '?download=1';
      link.target = '_blank';
      link.rel = 'noopener';
      link.innerHTML = '<i class="fa-solid fa-file-arrow-down"></i>';
      const name = document.createElement('span');
      name.textContent = material.name;
      link.appendChild(name);
      stageFiles.appendChild(link);
    });
  }

  function openZoom(index) {
    if (!frames.length) return;
    framePos = Math.max(0, Math.min(index, frames.length - 1));
    const frame = frames[framePos];
    zoomImg.src = frame.full;
    zoomImg.alt = frameLabel(frame);
    zoomLabel.textContent = frameLabel(frame);
    zoomPrev.disabled = framePos === 0;
    zoomNext.disabled = framePos === frames.length - 1;
    zoom.hidden = false;
  }

  function closeZoom() {
    framePos = -1;
    zoom.hidden = true;
    zoomImg.removeAttribute('src');
  }

  zoomPrev.addEventListener('click', () => openZoom(framePos - 1));
  zoomNext.addEventListener('click', () => openZoom(framePos + 1));
  zoomClose.addEventListener('click', closeZoom);
  document.addEventListener('keydown', event => {
    if (zoom.hidden) return;
    if (event.target.matches('input, textarea, select')) return;
    if (event.key === 'Escape') closeZoom();
    if (event.key === 'ArrowLeft' && framePos > 0) openZoom(framePos - 1);
    if (event.key === 'ArrowRight' && framePos < frames.length - 1) openZoom(framePos + 1);
  });

  function select(agendaId) {
    const agenda = agendas.find(item => item.id === agendaId);
    if (!agenda) return;
    // 보던 안건의 입력 내용을 잃지 않도록 옮기기 전에 저장한다.
    if (current && isDirty()) saveDecision(true);

    current = agenda;
    stageNo.textContent = '안건 ' + agenda.no;
    stageTitle.textContent = agenda.title;
    stageSummary.textContent = agenda.summary || '등록된 안건 설명이 없습니다.';
    stageOwner.textContent = agenda.owner ? '담당 : ' + agenda.owner : '';
    minutesText.value = agenda.minutes || '';
    decisionText.value = agenda.decision || '';
    decisionStatus.value = agenda.decision_status || 'pending';
    decisionState.textContent = '';
    closeZoom();
    renderThumbs(agenda);
    renderFiles(agenda);
    renderList();
  }

  // ------------------------------------------------------------ 결정 저장
  function isDirty() {
    if (!current) return false;
    return minutesText.value !== (current.minutes || '')
      || decisionText.value !== (current.decision || '')
      || decisionStatus.value !== (current.decision_status || 'pending');
  }

  async function saveDecision(quiet) {
    if (!current) return false;
    const agenda = current;
    const payload = {
      minutes: minutesText.value,
      decision: decisionText.value,
      decision_status: decisionStatus.value,
    };
    decisionState.textContent = '저장 중…';
    try {
      const data = await request(decisionUrlBase + agenda.id, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      agenda.minutes = payload.minutes;
      agenda.decision = payload.decision;
      agenda.decision_status = payload.decision_status;
      decisionState.textContent = '저장됨 ' + (data.saved_at || '');
      renderList();
      if (!quiet) notify('안건 기록을 저장했습니다.');
      return true;
    } catch (error) {
      decisionState.textContent = error.name === 'SessionError' ? '로그인 필요' : '저장 실패';
      // 저장에 실패하면 다시 시도한다. 회의 중 잠깐 끊긴 인터넷 때문에
      // 적어 둔 논의·결정이 사라지지 않게 하기 위함이다.
      scheduleDecisionRetry();
      if (!quiet || error.name !== 'SessionError') {
        notify(error.message || '저장 중 오류가 발생했습니다.', 2600);
      }
      return false;
    }
  }

  let decisionTimer = null;
  let decisionRetryTimer = null;
  function scheduleDecisionSave() {
    clearTimeout(decisionTimer);
    decisionState.textContent = '입력 중…';
    decisionTimer = setTimeout(() => saveDecision(true), 1600);
  }
  function scheduleDecisionRetry() {
    clearTimeout(decisionRetryTimer);
    decisionRetryTimer = setTimeout(() => { if (isDirty()) saveDecision(true); }, 8000);
  }
  [minutesText, decisionText].forEach(node => node.addEventListener('input', scheduleDecisionSave));
  decisionStatus.addEventListener('change', () => saveDecision(true));
  decisionSave.addEventListener('click', () => saveDecision(false));

  // ------------------------------------------------------ 회의 중 안건 추가
  const addBtn = document.getElementById('mtAddAgendaBtn');
  const addForm = document.getElementById('mtAddAgendaForm');
  const addTitle = document.getElementById('mtNewAgendaTitle');
  const addSummary = document.getElementById('mtNewAgendaSummary');
  const addCancel = document.getElementById('mtAddCancel');

  function toggleAddForm(open) {
    addForm.hidden = !open;
    addBtn.hidden = open;
    if (open) addTitle.focus();
    else { addTitle.value = ''; addSummary.value = ''; }
  }

  addBtn.addEventListener('click', () => toggleAddForm(true));
  addCancel.addEventListener('click', () => toggleAddForm(false));
  addForm.addEventListener('submit', async event => {
    event.preventDefault();
    const title = addTitle.value.trim();
    if (!title) { addTitle.focus(); return; }
    const submit = addForm.querySelector('button[type=submit]');
    submit.disabled = true;
    try {
      const data = await postJson(`/meeting/${meetingId}/live/agenda`, {
        title, summary: addSummary.value,
      });
      agendas.push(data.agenda);
      if (agendaCount) agendaCount.textContent = agendas.length;
      toggleAddForm(false);
      select(data.agenda.id);
      notify('안건을 추가했습니다.');
    } catch (error) {
      notify(error.message || '안건 추가 중 오류가 발생했습니다.', 2800);
    } finally {
      submit.disabled = false;
    }
  });

  // ------------------------------------------------------------ 받아쓰기 저장
  /* 받아쓰기는 한 회의를 여러 참석자가 같이 적을 수 있다. 마지막으로 서버에서
     받은 판번호(revision)를 같이 보내면, 그 사이 다른 사람이 적은 내용이 있을 때
     서버가 두 기록을 합쳐 돌려준다(어느 쪽도 지워지지 않는다). */
  let transcriptTimer = null;
  let transcriptRetryTimer = null;
  let transcriptSaving = false;
  let transcriptRevision = Number(root.dataset.transcriptRevision || 0);
  let savedTranscript = transcript.value;

  function transcriptDirty() {
    return transcript.value !== savedTranscript;
  }

  async function saveTranscript(quiet) {
    if (transcriptSaving) return false;
    if (!transcriptDirty() && quiet) {
      return true;
    }
    transcriptSaving = true;
    const sending = transcript.value;
    transcriptState.textContent = '저장 중…';
    try {
      const data = await postJson(transcriptUrl, {
        transcript: sending,
        base_revision: transcriptRevision,
      });
      transcriptRevision = Number(data.revision || transcriptRevision);
      if (data.merged && typeof data.transcript === 'string') {
        // 그 사이 다른 참석자가 적은 내용이 있어 서버가 두 기록을 합쳐 주었다.
        const atBottom = transcript.scrollTop + transcript.clientHeight
          >= transcript.scrollHeight - 8;
        // 보내는 동안 내가 더 친 글자는 지우지 않고 뒤에 남긴다.
        const typedAfter = transcript.value.slice(sending.length);
        transcript.value = data.transcript + typedAfter;
        savedTranscript = data.transcript;
        if (atBottom) transcript.scrollTop = transcript.scrollHeight;
        notify('다른 참석자의 기록과 합쳤습니다.', 2400);
      } else {
        savedTranscript = sending;
      }
      transcriptState.textContent = '저장됨 ' + (data.saved_at || '') + ' · ' + (data.length || 0) + '자';
      clearTimeout(transcriptRetryTimer);
      if (!quiet) notify('받아쓰기를 저장했습니다.');
      return true;
    } catch (error) {
      transcriptState.textContent = error.name === 'SessionError'
        ? '로그인 필요 · 저장 안 됨' : '저장 실패 · 다시 시도합니다';
      clearTimeout(transcriptRetryTimer);
      transcriptRetryTimer = setTimeout(() => saveTranscript(true), 8000);
      if (!quiet) notify(error.message || '저장 중 오류가 발생했습니다.', 3000);
      return false;
    } finally {
      transcriptSaving = false;
    }
  }

  function scheduleTranscriptSave() {
    clearTimeout(transcriptTimer);
    transcriptState.textContent = '입력 중…';
    transcriptTimer = setTimeout(() => saveTranscript(true), 2000);
  }
  transcript.addEventListener('input', scheduleTranscriptSave);
  // 다른 칸으로 넘어갈 때는 기다리지 않고 바로 저장한다.
  transcript.addEventListener('blur', () => { if (transcriptDirty()) saveTranscript(true); });

  /* 화면을 닫거나 탭을 옮길 때 마지막 몇 초의 기록이 사라지지 않도록,
     응답을 기다리지 않는 sendBeacon으로 한 번 더 보낸다. */
  function flushTranscriptBeacon() {
    if (!transcriptDirty() || !navigator.sendBeacon) return;
    try {
      const body = new Blob([JSON.stringify({
        transcript: transcript.value,
        base_revision: transcriptRevision,
      })], { type: 'application/json' });
      if (navigator.sendBeacon(transcriptUrl, body)) savedTranscript = transcript.value;
    } catch (error) { /* 못 보내면 떠나기 전 경고창이 뜬다. */ }
  }

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') flushTranscriptBeacon();
    else if (transcriptDirty()) saveTranscript(true);
  });
  window.addEventListener('pagehide', flushTranscriptBeacon);

  function appendTranscript(text) {
    const line = String(text || '').trim();
    if (!line) return;
    const prefix = transcript.value && !transcript.value.endsWith('\n') ? '\n' : '';
    const stamp = new Date().toTimeString().slice(0, 5);
    transcript.value += `${prefix}[${stamp}] ${line}\n`;
    transcript.scrollTop = transcript.scrollHeight;
    scheduleTranscriptSave();
  }

  // ------------------------------------------------------------ 음성 인식
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const sttBtn = document.getElementById('mtSttBtn');
  const sttLabel = document.getElementById('mtSttLabel');
  const sttHint = document.getElementById('mtSttHint');
  let recognition = null;
  let sttOn = false;

  if (!SpeechRecognition) {
    sttBtn.disabled = true;
    sttLabel.textContent = '받아쓰기 미지원';
  }

  function startStt() {
    if (!SpeechRecognition || recognition) return;
    recognition = new SpeechRecognition();
    recognition.lang = 'ko-KR';
    recognition.continuous = true;
    recognition.interimResults = false;
    recognition.onresult = event => {
      for (let index = event.resultIndex; index < event.results.length; index += 1) {
        if (event.results[index].isFinal) {
          appendTranscript(event.results[index][0].transcript);
        }
      }
    };
    recognition.onerror = event => {
      if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
        notify('마이크 사용이 차단되어 받아쓰기를 시작할 수 없습니다.', 3200);
        stopStt();
      }
    };
    // 조용한 구간이 길면 브라우저가 인식을 멈추므로 켜 둔 동안에는 다시 시작한다.
    recognition.onend = () => {
      if (!sttOn) return;
      try { recognition.start(); } catch (error) { /* 이미 시작된 상태 */ }
    };
    sttOn = true;
    try {
      recognition.start();
    } catch (error) {
      sttOn = false;
      recognition = null;
      notify('받아쓰기를 시작하지 못했습니다.', 2600);
      return;
    }
    sttLabel.textContent = '받아쓰기 중지';
    sttBtn.classList.add('rec', 'is-on');
    if (sttHint) sttHint.textContent = '받아쓰기 중입니다. 말한 내용이 자동으로 아래에 적힙니다.';
  }

  function stopStt() {
    sttOn = false;
    if (recognition) {
      try { recognition.stop(); } catch (error) { /* 무시 */ }
      recognition = null;
    }
    sttLabel.textContent = '받아쓰기 시작';
    sttBtn.classList.remove('is-on');
    if (sttHint) sttHint.textContent = '받아쓰기를 멈췄습니다. 내용은 그대로 저장됩니다.';
    saveTranscript(true);
  }

  sttBtn.addEventListener('click', () => (sttOn ? stopStt() : startStt()));

  // ------------------------------------------------------------ 녹음
  /* 회의는 길다. 한 번에 다 담아 두었다가 끝날 때 통째로 올리면
     - 브라우저가 소리 전체를 메모리에 물고 있어야 하고,
     - 중간에 창이 닫히거나 업로드가 한 번 실패하면 회의 전체가 사라진다.
     그래서 정해진 시간마다 조각(회차)으로 끊어 그때그때 서버에 올린다.
     회의센터는 원래 회차별 녹음을 목록으로 보여 주므로 화면 구성은 그대로다. */
  const REC_SEGMENT_MS = 5 * 60 * 1000;   // 5분마다 한 회차로 끊어 올린다.
  const REC_CHUNK_MS = 2000;              // 2초마다 한 덩어리씩 받아 둔다.

  const recBtn = document.getElementById('mtRecBtn');
  const recLabel = document.getElementById('mtRecLabel');
  const recDot = document.getElementById('mtRecDot');
  const recText = document.getElementById('mtRecText');
  const recItems = document.getElementById('mtRecItems');
  const recCount = document.getElementById('mtRecCount');

  let recorder = null;         // 지금 돌아가는 MediaRecorder
  let recStream = null;        // 마이크 입력
  let recStartedAt = 0;        // 이번 조각을 시작한 시각
  let recTotalSeconds = 0;     // 이번 녹음에서 지금까지 담은 시간
  let recTimer = null;         // 화면의 경과시간 표시
  let recRotateTimer = null;   // 조각 나누기 예약
  let recWanted = false;       // 사용자가 [녹음 시작]을 누른 상태인지
  let recUploading = 0;        // 올리는 중인 조각 수
  let recordings = [];
  const recPending = [];       // 올리지 못해 기다리는 조각들

  try {
    recordings = JSON.parse(document.getElementById('mtRecData').textContent || '[]');
  } catch (error) {
    recordings = [];
  }

  /* 브라우저마다 만들 수 있는 형식이 다르다. 크롬·엣지는 webm, 사파리와
     아이폰은 mp4만 된다. 지원하는 형식을 골라 두고 서버에도 알려 준다. */
  function pickMimeType() {
    const candidates = [
      'audio/webm;codecs=opus', 'audio/webm',
      'audio/ogg;codecs=opus', 'audio/ogg',
      'audio/mp4;codecs=mp4a.40.2', 'audio/mp4',
    ];
    if (!window.MediaRecorder || !MediaRecorder.isTypeSupported) return '';
    return candidates.find(type => MediaRecorder.isTypeSupported(type)) || '';
  }

  function extensionFor(mimeType) {
    const base = String(mimeType || '').split(';')[0].toLowerCase();
    if (base.includes('ogg')) return 'ogg';
    if (base.includes('mp4')) return 'm4a';
    if (base.includes('mpeg')) return 'mp3';
    if (base.includes('wav')) return 'wav';
    return 'webm';
  }

  function fmt(seconds) {
    const value = Math.max(0, Math.floor(seconds));
    const mm = String(Math.floor(value / 60)).padStart(2, '0');
    const ss = String(value % 60).padStart(2, '0');
    return mm + ':' + ss;
  }

  function recStateText() {
    if (recWanted) {
      const live = recTotalSeconds + (recStartedAt ? (Date.now() - recStartedAt) / 1000 : 0);
      return '녹음 중 ' + fmt(live)
        + (recUploading ? ' · 저장 중 ' + recUploading + '개' : '')
        + (recPending.length ? ' · 대기 ' + recPending.length + '개' : '');
    }
    if (recUploading) return '녹음 저장 중… (' + recUploading + '개)';
    if (recPending.length) return '저장 못한 녹음 ' + recPending.length + '개';
    if (recordings.length) return '녹음 ' + recordings.length + '회차 저장됨';
    return '녹음 대기';
  }

  function paintRecState() {
    if (recText) recText.textContent = recStateText();
  }

  // 회차마다 쌓인 녹음을 목록으로 보여 준다(바로 듣거나 내려받을 수 있다).
  function renderRecordings() {
    if (!recItems) return;
    recItems.innerHTML = '';
    if (recCount) recCount.textContent = recordings.length;
    if (!recordings.length && !recPending.length) {
      const empty = document.createElement('p');
      empty.className = 'mt-rec-empty';
      empty.textContent = '아직 녹음이 없습니다. [녹음 시작]을 누르면 5분마다 한 회차씩 자동으로 저장됩니다.';
      recItems.appendChild(empty);
      return;
    }
    recordings.forEach(item => {
      const row = document.createElement('div');
      row.className = 'mt-rec-item';
      const no = document.createElement('b');
      no.textContent = item.no + '회차';
      const label = document.createElement('span');
      label.textContent = item.length + ' · ' + item.size_kb + 'KB';
      label.title = item.filename;

      const play = document.createElement('a');
      play.href = item.url || ('/meeting/' + meetingId + '/recording/' + item.id);
      play.target = '_blank';
      play.rel = 'noopener';
      play.title = '새 창에서 듣기';
      play.innerHTML = '<i class="fa-solid fa-circle-play"></i>';

      const save = document.createElement('a');
      save.href = item.download_url || ('/meeting/' + meetingId + '/recording/' + item.id + '?download=1');
      save.title = '내 PC로 내려받기';
      save.setAttribute('download', item.filename || '');
      save.innerHTML = '<i class="fa-solid fa-download"></i>';

      row.append(no, label, play, save);
      recItems.appendChild(row);
    });

    // 아직 서버에 올리지 못한 조각도 눈에 보이게 두고, 직접 내려받아 보관할 수 있게 한다.
    recPending.forEach((item, index) => {
      const row = document.createElement('div');
      row.className = 'mt-rec-item is-pending';
      const no = document.createElement('b');
      no.textContent = '대기';
      const label = document.createElement('span');
      label.textContent = fmt(item.seconds) + ' · 저장 실패';

      const retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'mt-rec-mini';
      retry.title = '다시 올리기';
      retry.innerHTML = '<i class="fa-solid fa-rotate-right"></i>';
      retry.addEventListener('click', () => flushPending(true));

      const save = document.createElement('a');
      // 목록을 다시 그릴 때마다 새 주소를 만들지 않도록 한 번만 만들어 둔다.
      if (!item.localUrl) item.localUrl = URL.createObjectURL(item.blob);
      save.href = item.localUrl;
      save.download = '회의녹음_' + meetingId + '_' + (index + 1) + '.' + item.extension;
      save.title = '이 조각을 내 PC에 보관';
      save.innerHTML = '<i class="fa-solid fa-download"></i>';

      row.append(no, label, retry, save);
      recItems.appendChild(row);
    });
  }

  async function uploadSegment(blob, seconds, mimeType) {
    const extension = extensionFor(mimeType);
    const form = new FormData();
    form.append('recording', blob, 'meeting_' + meetingId + '_' + Date.now() + '.' + extension);
    form.append('seconds', String(Math.round(seconds)));
    form.append('mime', mimeType || blob.type || '');
    const data = await request(recordingUrl, { method: 'POST', body: form });
    recordings = data.recordings || recordings;
    return data;
  }

  /* 조각 하나를 올린다. 실패하면 버리지 않고 대기 목록에 담아 두었다가
     [다시 올리기]를 누르거나 다음 조각을 저장할 때 함께 다시 시도한다. */
  async function saveSegment(blob, seconds, mimeType) {
    if (!blob || !blob.size) return;
    recUploading += 1;
    paintRecState();
    try {
      const data = await uploadSegment(blob, seconds, mimeType);
      renderRecordings();
      notify('녹음 ' + (data.count || recordings.length) + '회차를 저장했습니다.');
      flushPending(false);
    } catch (error) {
      recPending.push({ blob, seconds, mimeType, extension: extensionFor(mimeType) });
      renderRecordings();
      if (error.name !== 'SessionError') {
        notify(error.message || '녹음을 저장하지 못했습니다. 대기 목록에 담아 두었습니다.', 4000);
      }
    } finally {
      recUploading -= 1;
      paintRecState();
    }
  }

  async function flushPending(announce) {
    if (!recPending.length) return;
    const queue = recPending.splice(0, recPending.length);
    renderRecordings();
    for (const item of queue) {
      recUploading += 1;
      paintRecState();
      try {
        await uploadSegment(item.blob, item.seconds, item.mimeType);
      } catch (error) {
        recPending.push(item);
        if (announce && error.name !== 'SessionError') {
          notify(error.message || '아직 저장하지 못했습니다.', 3200);
        }
      } finally {
        recUploading -= 1;
      }
    }
    renderRecordings();
    paintRecState();
    if (announce && !recPending.length) notify('밀린 녹음을 모두 저장했습니다.');
  }

  /* MediaRecorder 한 개(=한 회차)를 시작한다. onstop에서 쓰는 값은 모두
     지역 변수로 붙잡아 둔다. 예전에는 정지 직후 공용 recorder 변수를 비워
     버려서 onstop이 돌 때 오류가 났고, 그 때문에 녹음이 한 번도 서버로
     올라가지 못했다. */
  function startSegment() {
    if (!recStream) return;
    const wanted = pickMimeType();
    let instance;
    try {
      instance = wanted
        ? new MediaRecorder(recStream, { mimeType: wanted, audioBitsPerSecond: 96000 })
        : new MediaRecorder(recStream);
    } catch (error) {
      notify('이 브라우저에서 녹음을 시작하지 못했습니다.', 3200);
      stopRecording();
      return;
    }
    const actualMime = instance.mimeType || wanted || 'audio/webm';
    const startedAt = Date.now();
    const chunks = [];

    instance.ondataavailable = event => {
      if (event.data && event.data.size) chunks.push(event.data);
    };
    instance.onerror = () => {
      notify('녹음 중 오류가 발생했습니다. [녹음 시작]을 다시 눌러 주세요.', 3600);
    };
    instance.onstop = () => {
      const seconds = (Date.now() - startedAt) / 1000;
      recTotalSeconds += seconds;
      const blob = new Blob(chunks, { type: actualMime });
      chunks.length = 0;
      if (blob.size) saveSegment(blob, seconds, actualMime);
      // 사용자가 아직 녹음 중이면 곧바로 다음 회차를 이어서 시작한다.
      if (recWanted && recStream) startSegment();
      else stopStream();
    };

    recorder = instance;
    recStartedAt = startedAt;
    instance.start(REC_CHUNK_MS);

    clearTimeout(recRotateTimer);
    recRotateTimer = setTimeout(() => {
      if (recorder && recorder.state === 'recording') recorder.stop();
    }, REC_SEGMENT_MS);
  }

  function stopStream() {
    if (recStream) {
      recStream.getTracks().forEach(track => track.stop());
      recStream = null;
    }
  }

  let recStarting = false;

  async function startRecording() {
    if (recStarting) return;          // 마이크 허용을 기다리는 동안의 두 번 누름 방지
    if (!window.isSecureContext) {
      notify('보안 연결(https)에서만 녹음할 수 있습니다. 주소가 https로 시작하는지 확인해 주세요.', 4200);
      return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.MediaRecorder) {
      notify('이 브라우저에서는 녹음을 지원하지 않습니다. 크롬이나 엣지에서 열어 주세요.', 3600);
      return;
    }
    recStarting = true;
    try {
      recStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      });
    } catch (error) {
      const denied = error && (error.name === 'NotAllowedError' || error.name === 'SecurityError');
      notify(denied
        ? '마이크 사용을 허용해야 녹음할 수 있습니다. 주소창의 자물쇠에서 마이크를 허용해 주세요.'
        : '마이크를 찾지 못했습니다. 연결 상태를 확인해 주세요.', 4000);
      recStream = null;
      return;
    } finally {
      recStarting = false;
    }

    recWanted = true;
    recTotalSeconds = 0;
    startSegment();
    if (!recorder) { recWanted = false; stopStream(); paintRecState(); return; }

    recDot.classList.add('is-on');
    recBtn.classList.add('is-on');
    recLabel.textContent = '녹음 정지';
    clearInterval(recTimer);
    recTimer = setInterval(paintRecState, 1000);
    paintRecState();
  }

  function stopRecording() {
    recWanted = false;
    clearTimeout(recRotateTimer);
    clearInterval(recTimer);
    recStartedAt = 0;
    // 남은 소리는 onstop이 모아 서버로 올린다. 마이크 정리도 거기서 한다.
    if (recorder && recorder.state !== 'inactive') {
      try { recorder.stop(); } catch (error) { stopStream(); }
    } else {
      stopStream();
    }
    recorder = null;
    recDot.classList.remove('is-on');
    recBtn.classList.remove('is-on');
    recLabel.textContent = '녹음 시작';
    paintRecState();
  }

  function isRecording() {
    return recWanted;
  }

  recBtn.addEventListener('click', () => (isRecording() ? stopRecording() : startRecording()));

  // ------------------------------------------------------------ AI 회의록
  const minutesBtn = document.getElementById('mtMinutesBtn');
  if (minutesBtn) {
    minutesBtn.addEventListener('click', async () => {
      if (!confirm('지금까지 기록한 안건 논의·결정과 받아쓰기 내용으로 AI 회의록을 만들까요?\n실행항목은 메인화면 달력에도 자동으로 등록됩니다.')) return;
      if (sttOn) stopStt();
      if (isRecording()) stopRecording();
      // 안건 기록과 받아쓰기를 먼저 확실히 저장한 뒤에 회의록을 만든다.
      if (isDirty() && !(await saveDecision(true))) {
        notify('안건 기록을 저장하지 못해 회의록 작성을 멈췄습니다.', 4000);
        return;
      }
      if (!(await saveTranscript(true))) {
        notify('받아쓰기를 저장하지 못해 회의록 작성을 멈췄습니다.', 4000);
        return;
      }

      minutesBtn.disabled = true;
      const original = minutesBtn.innerHTML;
      minutesBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i><span>회의록 작성 중…</span>';
      notify('AI가 회의록을 작성하고 있습니다. 잠시만 기다려 주세요.', 6000);
      try {
        const data = await postJson(minutesUrl, { transcript: transcript.value });
        notify(`회의록을 만들었습니다. 실행항목 ${data.tasks_added || 0}건을 달력에 등록했습니다.`, 2600);
        setTimeout(() => { window.location.href = data.redirect; }, 900);
      } catch (error) {
        minutesBtn.disabled = false;
        minutesBtn.innerHTML = original;
        notify(error.message || '회의록 작성 중 오류가 발생했습니다.', 5000);
      }
    });
  }

  // ------------------------------------------------------------ 전체화면
  const fullBtn = document.getElementById('mtFullBtn');

  function nativeFullscreen() {
    return document.fullscreenElement || document.webkitFullscreenElement || null;
  }

  fullBtn.addEventListener('click', () => {
    if (nativeFullscreen()) {
      const exit = document.exitFullscreen || document.webkitExitFullscreen;
      if (exit) { try { exit.call(document); } catch (error) { /* 이미 나감 */ } }
      return;
    }
    const request = root.requestFullscreen || root.webkitRequestFullscreen;
    if (!request) return;
    try { request.call(root, { navigationUI: 'hide' }); } catch (error) { /* 지원 안 함 */ }
  });
  ['fullscreenchange', 'webkitfullscreenchange'].forEach(type => {
    document.addEventListener(type, () => {
      fullBtn.innerHTML = nativeFullscreen()
        ? '<i class="fa-solid fa-compress"></i>'
        : '<i class="fa-solid fa-expand"></i>';
    });
  });

  // 모바일 주소창이 접히고 펴질 때마다 실제 보이는 높이에 맞춘다.
  if (window.visualViewport) {
    const syncViewport = () => root.style.setProperty('--mt-vh', window.visualViewport.height + 'px');
    window.visualViewport.addEventListener('resize', syncViewport);
    syncViewport();
  }

  // 저장하지 않은 기록이 있으면 화면을 떠나기 전에 알린다.
  // 받아쓰기·올리지 못한 녹음·녹음 중 상태까지 모두 확인한다.
  window.addEventListener('beforeunload', event => {
    if (!isDirty() && !transcriptDirty() && !recPending.length
        && !isRecording() && !recUploading) return;
    event.preventDefault();
    event.returnValue = '';
  });

  // 인터넷이 다시 붙으면 밀려 있던 저장을 스스로 이어서 끝낸다.
  window.addEventListener('online', () => {
    if (transcriptDirty()) saveTranscript(true);
    if (isDirty()) saveDecision(true);
    flushPending(true);
  });
  window.addEventListener('offline', () => {
    showAlert('<b>인터넷 연결이 끊겼습니다.</b> 적으신 내용은 화면에 그대로 남아 있고, '
      + '연결이 돌아오면 자동으로 저장됩니다.');
  });

  renderList();
  renderRecordings();
  paintRecState();
  if (agendas.length) select(agendas[0].id);
})();
