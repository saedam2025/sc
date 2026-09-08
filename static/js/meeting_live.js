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

  let current = null;      // 지금 확대해서 보고 있는 안건
  let frames = [];         // 지금 안건의 자료를 쪽 단위로 펼친 목록
  let framePos = -1;       // 확대해서 보고 있는 쪽 (-1이면 썸네일 목록)
  let toastTimer = null;

  function notify(message, duration) {
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove('show'), duration || 1800);
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
    if (!current) return;
    const agenda = current;
    const payload = {
      minutes: minutesText.value,
      decision: decisionText.value,
      decision_status: decisionStatus.value,
    };
    decisionState.textContent = '저장 중…';
    try {
      const response = await fetch(decisionUrlBase + agenda.id, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.message || '저장하지 못했습니다.');
      agenda.minutes = payload.minutes;
      agenda.decision = payload.decision;
      agenda.decision_status = payload.decision_status;
      decisionState.textContent = '저장됨 ' + (data.saved_at || '');
      renderList();
      if (!quiet) notify('안건 기록을 저장했습니다.');
    } catch (error) {
      decisionState.textContent = '저장 실패';
      notify(error.message || '저장 중 오류가 발생했습니다.', 2600);
    }
  }

  let decisionTimer = null;
  function scheduleDecisionSave() {
    clearTimeout(decisionTimer);
    decisionState.textContent = '입력 중…';
    decisionTimer = setTimeout(() => saveDecision(true), 1600);
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
      const response = await fetch(`/meeting/${meetingId}/live/agenda`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, summary: addSummary.value }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.message || '안건을 추가하지 못했습니다.');
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
  let transcriptTimer = null;
  async function saveTranscript(quiet) {
    transcriptState.textContent = '저장 중…';
    try {
      const response = await fetch(transcriptUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ transcript: transcript.value }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.message || '저장하지 못했습니다.');
      transcriptState.textContent = '저장됨 ' + (data.saved_at || '') + ' · ' + (data.length || 0) + '자';
      if (!quiet) notify('받아쓰기를 저장했습니다.');
    } catch (error) {
      transcriptState.textContent = '저장 실패';
    }
  }
  function scheduleTranscriptSave() {
    clearTimeout(transcriptTimer);
    transcriptState.textContent = '입력 중…';
    transcriptTimer = setTimeout(() => saveTranscript(true), 2500);
  }
  transcript.addEventListener('input', scheduleTranscriptSave);

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
  const recBtn = document.getElementById('mtRecBtn');
  const recLabel = document.getElementById('mtRecLabel');
  const recDot = document.getElementById('mtRecDot');
  const recText = document.getElementById('mtRecText');
  const recItems = document.getElementById('mtRecItems');
  const recCount = document.getElementById('mtRecCount');
  let recorder = null;
  let chunks = [];
  let recStartedAt = 0;
  let recTimer = null;
  let recordings = [];
  try {
    recordings = JSON.parse(document.getElementById('mtRecData').textContent || '[]');
  } catch (error) {
    recordings = [];
  }

  // 회차마다 쌓인 녹음을 목록으로 보여 준다.
  function renderRecordings() {
    if (!recItems) return;
    recItems.innerHTML = '';
    if (recCount) recCount.textContent = recordings.length;
    if (!recordings.length) {
      const empty = document.createElement('p');
      empty.className = 'mt-rec-empty';
      empty.textContent = '아직 녹음이 없습니다. [녹음 시작]을 누르면 회차마다 이어서 저장됩니다.';
      recItems.appendChild(empty);
      return;
    }
    recordings.forEach(item => {
      const row = document.createElement('div');
      row.className = 'mt-rec-item';
      const no = document.createElement('b');
      no.textContent = item.no + '회차';
      const label = document.createElement('span');
      label.textContent = `${item.length} · ${item.size_kb}KB`;
      label.title = item.filename;
      const play = document.createElement('a');
      play.href = `/meeting/${meetingId}/recording/${item.id}`;
      play.target = '_blank';
      play.rel = 'noopener';
      play.title = '새 창에서 듣기';
      play.innerHTML = '<i class="fa-solid fa-circle-play"></i>';
      row.append(no, label, play);
      recItems.appendChild(row);
    });
  }

  function recElapsed() {
    const seconds = Math.floor((Date.now() - recStartedAt) / 1000);
    const mm = String(Math.floor(seconds / 60)).padStart(2, '0');
    const ss = String(seconds % 60).padStart(2, '0');
    return { seconds, text: `${mm}:${ss}` };
  }

  async function startRecording() {
    if (!navigator.mediaDevices || !window.MediaRecorder) {
      notify('이 브라우저에서는 녹음을 지원하지 않습니다.', 3000);
      return;
    }
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (error) {
      notify('마이크 사용을 허용해야 녹음할 수 있습니다.', 3200);
      return;
    }
    chunks = [];
    recorder = new MediaRecorder(stream);
    recorder.ondataavailable = event => { if (event.data && event.data.size) chunks.push(event.data); };
    recorder.onstop = async () => {
      stream.getTracks().forEach(track => track.stop());
      const elapsed = recElapsed();
      const blob = new Blob(chunks, { type: recorder.mimeType || 'audio/webm' });
      chunks = [];
      if (!blob.size) return;
      recText.textContent = '업로드 중…';
      const form = new FormData();
      const extension = (recorder.mimeType || '').includes('ogg') ? 'ogg' : 'webm';
      form.append('recording', blob, `meeting_${meetingId}_${Date.now()}.${extension}`);
      form.append('seconds', String(elapsed.seconds));
      try {
        const response = await fetch(recordingUrl, { method: 'POST', body: form });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.message || '업로드하지 못했습니다.');
        recordings = data.recordings || recordings;
        renderRecordings();
        recText.textContent = `녹음 ${data.count || recordings.length}개 저장됨 (마지막 ${elapsed.text})`;
        notify(`녹음을 ${data.count || recordings.length}번째로 이어 붙였습니다.`);
      } catch (error) {
        recText.textContent = '녹음 저장 실패';
        notify(error.message || '녹음 저장 중 오류가 발생했습니다.', 3000);
      }
    };
    recorder.start(1000);
    recStartedAt = Date.now();
    recDot.classList.add('is-on');
    recBtn.classList.add('is-on');
    recLabel.textContent = '녹음 정지';
    recTimer = setInterval(() => { recText.textContent = '녹음 중 ' + recElapsed().text; }, 1000);
    recText.textContent = '녹음 중 00:00';
  }

  function stopRecording() {
    if (recorder && recorder.state !== 'inactive') recorder.stop();
    recorder = null;
    clearInterval(recTimer);
    recDot.classList.remove('is-on');
    recBtn.classList.remove('is-on');
    recLabel.textContent = '녹음 시작';
  }

  recBtn.addEventListener('click', () => {
    if (recorder && recorder.state === 'recording') stopRecording();
    else startRecording();
  });

  // ------------------------------------------------------------ AI 회의록
  const minutesBtn = document.getElementById('mtMinutesBtn');
  if (minutesBtn) {
    minutesBtn.addEventListener('click', async () => {
      if (!confirm('지금까지 기록한 안건 논의·결정과 받아쓰기 내용으로 AI 회의록을 만들까요?\n실행항목은 메인화면 달력에도 자동으로 등록됩니다.')) return;
      if (sttOn) stopStt();
      if (recorder && recorder.state === 'recording') stopRecording();
      if (isDirty()) await saveDecision(true);
      await saveTranscript(true);

      minutesBtn.disabled = true;
      const original = minutesBtn.innerHTML;
      minutesBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i><span>회의록 작성 중…</span>';
      notify('AI가 회의록을 작성하고 있습니다. 잠시만 기다려 주세요.', 6000);
      try {
        const response = await fetch(minutesUrl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ transcript: transcript.value }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.message || '회의록을 만들지 못했습니다.');
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
  window.addEventListener('beforeunload', event => {
    if (!isDirty()) return;
    event.preventDefault();
    event.returnValue = '';
  });

  renderList();
  renderRecordings();
  if (agendas.length) select(agendas[0].id);
})();
