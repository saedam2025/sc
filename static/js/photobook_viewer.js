(() => {
  /* 콘텐츠 영역(.content-container)이 z-index:10 으로 별도 쌓임 맥락을 만들기 때문에
     그 안에서는 z-index 를 아무리 올려도 상단 메뉴(z-index:1000)를 넘지 못한다.
     뷰어를 body 바로 아래로 옮겨 맥락 밖으로 빼낸 뒤 위에 올린다. */
  const viewerRoot = document.getElementById('pbViewer');
  if (viewerRoot && viewerRoot.parentElement !== document.body) {
    document.body.appendChild(viewerRoot);
  }

  /* 권 이동은 상단 [이전 권]·[다음 권]과 마지막 장의 이어보기 바로만 한다.
     넘김 화살표는 권을 바꾸지 않고 끝에 닿았다는 안내만 잠깐 보여 준다. */
  const endNav = document.getElementById('pbEndNav');
  const toast = document.getElementById('pbToast');
  let toastTimer = null;
  function notify(message, duration) {
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove('show'), duration || 1300);
  }

  const dataNode = document.getElementById('pbPageData');
  if (!dataNode) return;
  const pages = JSON.parse(dataNode.textContent || '[]');
  if (!pages.length) return;

  const stage = document.getElementById('pbStage');
  const book = document.getElementById('pbBook');
  const leftSheet = document.getElementById('pbLeft');
  const rightSheet = document.getElementById('pbRight');
  const range = document.getElementById('pbRange');
  const current = document.getElementById('pbCurrent');
  const prevBtn = document.getElementById('pbPrev');
  const nextBtn = document.getElementById('pbNext');
  const wide = matchMedia('(min-width: 860px)');
  const FLIP_MS = 760;
  const Fold = window.PhotobookFold;
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  let activeTurn = null;

  let index = 0;      // 펼침면이면 왼쪽 페이지, 한 장 보기면 현재 페이지
  let busy = false;
  let drag = null;

  // 모바일에서도 실제 책처럼 왼쪽·오른쪽 두 장을 펼친다.
  // (사진이 한 장뿐인 전자책만 한 장 보기로 동작한다)
  function isSpread() { return pages.length > 1; }
  function step() { return isSpread() ? 2 : 1; }

  function normalize(value) {
    const last = pages.length - 1;
    const clamped = Math.max(0, Math.min(value, last));
    return isSpread() ? Math.floor(clamped / 2) * 2 : clamped;
  }

  // 사진 비율이 제각각이어도 한 권 안에서는 같은 크기의 종이로 보이도록
  // 전체 사진의 중앙값 비율을 종이 비율로 삼는다.
  function pageRatio() {
    const ratios = pages
      .map(page => (page.w > 0 && page.h > 0) ? page.w / page.h : 0)
      .filter(Boolean)
      .sort((a, b) => a - b);
    const median = ratios.length ? ratios[Math.floor(ratios.length / 2)] : 0.75;
    return Math.min(1.6, Math.max(0.55, median));
  }
  const paperRatio = pageRatio();

  function layout() {
    if (!stage) return;
    book.classList.toggle('is-single', !isSpread());
    // 좁은 화면에서는 넘김 버튼이 사진 위에 겹치므로 좌우 여백을 줄인다.
    const sideReserve = wide.matches ? 76 : 12;
    const availableWidth = Math.max(200, stage.clientWidth - sideReserve);
    const availableHeight = Math.max(180, stage.clientHeight - (wide.matches ? 20 : 8));
    const bookRatio = isSpread() ? paperRatio * 2 : paperRatio;
    let width = availableWidth;
    let height = width / bookRatio;
    if (height > availableHeight) {
      height = availableHeight;
      width = height * bookRatio;
    }
    book.style.width = `${Math.round(width)}px`;
    book.style.height = `${Math.round(height)}px`;
  }

  function imageMarkup(pageIndex) {
    const page = pages[pageIndex];
    if (!page) return '';
    return `<img src="${page.url}" alt="${page.no}페이지" draggable="false">`
      + `<span class="pb-pageno">${page.no}</span>`;
  }

  function fillSheet(sheet, pageIndex) {
    sheet.classList.toggle('blank', !pages[pageIndex]);
    sheet.innerHTML = imageMarkup(pageIndex);
  }

  // 사진을 종이 안에 원본 비율 그대로 담을 때의 실제 그려지는 영역
  function fitBox(page, boxWidth, boxHeight) {
    const ratio = (page.w > 0 && page.h > 0) ? page.w / page.h : boxWidth / boxHeight;
    let width = boxWidth;
    let height = boxWidth / ratio;
    if (height > boxHeight) { height = boxHeight; width = boxHeight * ratio; }
    return { width, height, offsetX: (boxWidth - width) / 2, offsetY: (boxHeight - height) / 2 };
  }

  const imageCache = new Map();
  function pageImage(page) {
    if (!page) return null;
    if (!imageCache.has(page.url)) {
      const image = new Image();
      image.onload = () => {
        if (activeTurn) {
          activeTurn.textures = null;
          paintFold(activeTurn);
        }
      };
      image.src = page.url;
      imageCache.set(page.url, image);
    }
    return imageCache.get(page.url);
  }

  function texture(pageIndex, state, mirror) {
    const canvas = document.createElement('canvas');
    canvas.width = Math.ceil(state.width * state.dpr);
    canvas.height = Math.ceil(state.height * state.dpr);
    const ctx = canvas.getContext('2d');
    ctx.scale(state.dpr, state.dpr);
    if (mirror) { ctx.translate(state.width, 0); ctx.scale(-1, 1); }
    ctx.fillStyle = '#fdfcf9';
    ctx.fillRect(0, 0, state.width, state.height);
    const page = pages[pageIndex];
    const image = pageImage(page);
    if (image && image.complete && image.naturalWidth) {
      const fit = fitBox(page, state.width, state.height);
      ctx.drawImage(image, fit.offsetX, fit.offsetY, fit.width, fit.height);
    }
    if (page) {
      const onLeft = (pageIndex % 2) === 0;
      const x = onLeft ? 22 : state.width - 22;
      const y = state.height - 19;
      ctx.fillStyle = 'rgba(15,23,42,.55)';
      ctx.beginPath();
      ctx.roundRect(x - 12, y - 11, 24, 22, 11);
      ctx.fill();
      ctx.fillStyle = '#fff';
      ctx.font = 'bold 11px sans-serif';
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText(String(page.no), x, y);
    }
    return canvas;
  }

  function path(ctx, polygon, matrix) {
    const [a, b, c, d, e, f] = matrix;
    polygon.forEach((p, i) => {
      const x = a * p.x + c * p.y + e, y = b * p.x + d * p.y + f;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.closePath();
  }

  function paintFold(state) {
    if (!state.textures) state.textures = {
      front: texture(state.frontIndex, state, state.direction < 0),
      back: texture(state.backIndex, state, state.direction > 0),
    };
    const { ctx, canvas, dpr, pad, width, height } = state;
    const fold = Fold.geometry(width, height, state.grabY, state.requested);
    state.point = fold.point;
    state.fold = fold;
    const pieces = Fold.strips(width, height, fold, wide.matches ? 40 : 24);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.setTransform(dpr, 0, 0, dpr, dpr * (pad + state.spine), dpr * pad);
    ctx.scale(state.direction, 1);
    function draw(piece) {
      ctx.save();
      ctx.beginPath();
      const [a, b, c, d, e, f] = piece.matrix;
      const projected = piece.polygon.map(p => ({
        x: a * p.x + c * p.y + e, y: b * p.x + d * p.y + f,
      }));
      const center = projected.reduce((p, q) => ({ x: p.x + q.x / projected.length,
        y: p.y + q.y / projected.length }), { x: 0, y: 0 });
      // Overlap in screen space, across the crease normal. Source-space
      // overlap vanishes when a curved strip is almost edge-on.
      const polygon = projected.map(p => {
        if (fold.flat) return p;
        const side = Math.sign((p.x - center.x) * fold.nx + (p.y - center.y) * fold.ny);
        return { x: p.x + fold.nx * side * 0.35, y: p.y + fold.ny * side * 0.35 };
      });
      path(ctx, polygon, [1, 0, 0, 1, 0, 0]);
      ctx.clip();
      ctx.transform(...piece.matrix);
      ctx.drawImage(piece.back ? state.textures.back : state.textures.front, 0, 0, width, height);
      if (piece.shade) {
        ctx.fillStyle = `rgba(18,24,32,${piece.shade})`;
        ctx.fillRect(0, 0, width, height);
      }
      ctx.restore();
    }
    pieces.filter(p => !p.back).forEach(draw);
    if (!fold.flat) {
      // One silhouette casts a single soft shadow, without strip boundaries.
      ctx.save();
      ctx.beginPath();
      pieces.filter(p => p.back).forEach(p => path(ctx, p.polygon, p.matrix));
      ctx.shadowColor = 'rgba(0,0,0,.32)';
      ctx.shadowBlur = Math.max(2, fold.radius * 0.7) * dpr;
      ctx.shadowOffsetX = state.direction * fold.radius * 0.25 * dpr;
      ctx.shadowOffsetY = fold.radius * 0.3 * dpr;
      ctx.fillStyle = 'rgba(0,0,0,.16)';
      ctx.fill();
      ctx.restore();
    }
    pieces.filter(p => p.back).forEach(draw);
  }

  function setFoldPoint(state, point) {
    state.requested = point;
    paintFold(state);
  }

  function render() {
    layout();
    if (isSpread()) {
      fillSheet(leftSheet, index);
      fillSheet(rightSheet, index + 1);
    } else {
      fillSheet(rightSheet, index);
    }
    const end = Math.min(index + step(), pages.length);
    current.textContent = end > index + 1 ? `${index + 1}–${end}` : String(index + 1);
    const atStart = index === 0;
    const atEnd = end >= pages.length;
    // 펼침면에서는 왼쪽 쪽번호가 기준이라 마지막 장에서도 게이지가 덜 찬 것처럼
    // 보인다. 마지막 장이면 눈금을 끝까지 채워 진행도를 그대로 보여 준다.
    range.value = atEnd ? pages.length : index + 1;

    // 끝에 닿은 화살표는 흐리게만 두고, 눌리면 안내를 띄운다(권 이동은 하지 않는다).
    prevBtn.classList.toggle('is-spent', atStart);
    nextBtn.classList.toggle('is-spent', atEnd);
    prevBtn.setAttribute('aria-disabled', String(atStart));
    nextBtn.setAttribute('aria-disabled', String(atEnd));
    prevBtn.title = atStart ? '첫 페이지입니다' : '이전 장';
    nextBtn.title = atEnd ? '마지막 페이지입니다' : '다음 장';
    if (endNav) endNav.classList.toggle('show', atEnd);

    preloadAround(index);
  }

  /* 넘기는 순간 사진이 빈 종이로 보이지 않도록 앞뒤 몇 장을 미리 받아 둔다. */
  const preloaded = new Set();
  function preloadAround(from) {
    for (let offset = -2; offset <= 4; offset += 1) {
      const page = pages[from + offset];
      if (!page || preloaded.has(page.url)) continue;
      preloaded.add(page.url);
      pageImage(page);
    }
  }

  function beginTurn(direction, grabY) {
    const target = normalize(index + direction * step());
    if (target === index) return null;
    const width = isSpread() ? book.clientWidth / 2 : book.clientWidth;
    const height = book.clientHeight;
    const pad = Math.ceil(Math.max(width, height) * 0.55);
    const canvas = document.createElement('canvas');
    canvas.className = 'pb-curl';
    canvas.setAttribute('aria-hidden', 'true');
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const canvasWidth = book.clientWidth + pad * 2;
    const canvasHeight = height + pad * 2;
    canvas.width = Math.ceil(canvasWidth * dpr);
    canvas.height = Math.ceil(canvasHeight * dpr);
    Object.assign(canvas.style, { left: `${-pad}px`, top: `${-pad}px`,
      width: `${canvasWidth}px`, height: `${canvasHeight}px` });
    const state = { canvas, ctx: canvas.getContext('2d'), width, height, pad, dpr,
      spine: isSpread() ? width : 0, direction, target,
      frontIndex: direction > 0 ? index + 1 : index,
      backIndex: direction > 0 ? target : target + 1,
      grabY: grabY === undefined ? height * 0.92 : Math.max(0, Math.min(height, grabY)),
      automatic: grabY === undefined,
    };
    state.requested = { x: width, y: state.grabY };
    activeTurn = state;
    // Render the moving page before exposing the page underneath.
    paintFold(state);
    if (direction > 0) fillSheet(rightSheet, target + 1);
    else fillSheet(leftSheet, target);
    book.appendChild(canvas);
    return state;
  }

  function finishTurn(state, commit) {
    if (state.drawRaf) cancelAnimationFrame(state.drawRaf);
    setFoldPoint(state, state.requested);
    const from = state.point;
    const to = { x: commit ? -state.width : state.width, y: state.grabY };
    const distance = Math.hypot(to.x - from.x, to.y - from.y);
    const duration = reducedMotion.matches ? 0 : Math.max(220,
      FLIP_MS * Math.min(1, distance / (state.width * 2)));
    const started = performance.now();
    const run = now => {
      const t = duration ? Math.min(1, (now - started) / duration) : 1;
      const eased = 1 - (1 - t) ** 3;
      const lift = state.automatic ? (state.height * 0.5 - state.grabY) * 0.55 * Math.sin(Math.PI * eased) : 0;
      setFoldPoint(state, { x: from.x + (to.x - from.x) * eased,
        y: from.y + (to.y - from.y) * eased + lift });
      if (t < 1) { state.raf = requestAnimationFrame(run); return; }
      state.canvas.remove();
      activeTurn = null;
      if (commit) index = state.target;
      render();
      busy = false;
    };
    state.raf = requestAnimationFrame(run);
  }

  function turn(direction) {
    if (busy || drag) return;
    if (normalize(index + direction * step()) === index) {
      notify(direction > 0 ? '마지막 페이지입니다.' : '첫 페이지입니다.');
      return;
    }
    busy = true;
    const state = beginTurn(direction);
    if (!state) { busy = false; return; }
    finishTurn(state, true);
  }

  function jump(pageNumber) {
    if (busy || drag) return;
    index = normalize(pageNumber - 1);
    render();
  }

  prevBtn.addEventListener('click', () => turn(-1));
  nextBtn.addEventListener('click', () => turn(1));
  range.addEventListener('input', () => jump(+range.value));

  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && isFullscreen()) { leaveFullscreen(); return; }
    if (event.target.matches('input, textarea, select, button, a')) return;
    if (event.key === 'ArrowLeft') turn(-1);
    if (event.key === 'ArrowRight' || event.key === ' ') { event.preventDefault(); turn(1); }
  });

  // Keep the original grab point: both horizontal and vertical travel affect
  // the diagonal crease. Capture retains the drag outside the book bounds.
  stage.addEventListener('pointerdown', event => {
    if (busy || drag || !event.isPrimary || event.button !== 0 || !book.contains(event.target)) return;
    const rect = book.getBoundingClientRect();
    drag = { pointerId: event.pointerId, x: event.clientX, y: event.clientY,
      at: performance.now(), rect, state: null,
      direction: event.clientX >= rect.left + rect.width / 2 ? 1 : -1 };
    stage.setPointerCapture(event.pointerId);
  });

  function moveDrag(event) {
    if (!drag || event.pointerId !== drag.pointerId) return;
    const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
    if (!drag.state) {
      if (Math.hypot(dx, dy) < 5) return;
      busy = true;
      drag.state = beginTurn(drag.direction, drag.y - drag.rect.top);
      if (!drag.state) {
        busy = false;
        notify(drag.direction > 0 ? '마지막 페이지입니다.' : '첫 페이지입니다.');
        endDrag(event);
        return;
      }
    }
    const state = drag.state;
    state.requested = { x: state.width + drag.direction * dx, y: state.grabY + dy };
    if (!state.drawRaf) state.drawRaf = requestAnimationFrame(() => {
      state.drawRaf = null;
      paintFold(state);
    });
  }
  stage.addEventListener('pointermove', moveDrag);

  function endDrag(event) {
    if (!drag || event.pointerId !== drag.pointerId) return;
    // Pointerup can carry a final position without a preceding pointermove.
    if (event.type === 'pointerup' && drag.state) moveDrag(event);
    const { state, at, pointerId } = drag;
    drag = null;
    if (stage.hasPointerCapture(pointerId)) stage.releasePointerCapture(pointerId);
    if (!state) return;
    const point = Fold.geometry(state.width, state.height, state.grabY, state.requested).point;
    const travelled = state.width - point.x;
    const flick = performance.now() - at < 320 && travelled > Math.max(44, state.width * 0.2);
    finishTurn(state, event.type === 'pointerup' && (travelled > state.width * 0.56 || flick));
  }
  ['pointerup', 'pointercancel', 'lostpointercapture'].forEach(type => stage.addEventListener(type, endDrag));

  wide.addEventListener('change', () => { if (!busy) render(); });
  window.addEventListener('resize', () => { if (!busy) layout(); });

  /* 전체화면.
     모바일에서도 브라우저 전체화면 API를 먼저 불러 주소창·탭바까지 감춘다.
     (안드로이드 크롬·삼성 인터넷 등) 성공하면 뷰어 위아래 바도 함께 접어
     사진만 남기고, 전체화면 API가 없는 기기(아이폰 사파리)에서는 바만 접는
     자체 몰입 모드로 물러난다. */
  const fullscreenBtn = document.getElementById('pbFullscreen');
  const immersiveExit = document.getElementById('pbImmersiveExit');
  const immersiveOnly = matchMedia('(max-width: 760px), (pointer: coarse)');
  const isApple = /iP(hone|od|ad)/.test(navigator.platform || '')
    || (/Mac/.test(navigator.platform || '') && navigator.maxTouchPoints > 1);

  // 이 화면에서만 노치·홈바 영역까지 화면을 쓰도록 viewport 를 넓힌다.
  const viewportMeta = document.querySelector('meta[name="viewport"]');
  if (viewportMeta && !/viewport-fit/.test(viewportMeta.content || '')) {
    viewportMeta.content = (viewportMeta.content || '') + ', viewport-fit=cover';
  }

  function nativeFullscreen() {
    return document.fullscreenElement || document.webkitFullscreenElement || null;
  }

  function requestNative() {
    const request = viewerRoot.requestFullscreen || viewerRoot.webkitRequestFullscreen;
    if (!request) return Promise.reject(new Error('unsupported'));
    try {
      // 구형 사파리는 Promise 를 돌려주지 않으므로 감싸서 통일한다.
      return Promise.resolve(request.call(viewerRoot, { navigationUI: 'hide' }));
    } catch (error) {
      return Promise.reject(error);
    }
  }

  function exitNative() {
    const exit = document.exitFullscreen || document.webkitExitFullscreen;
    if (!exit) return;
    try { exit.call(document); } catch (error) { /* 이미 나간 상태 */ }
  }

  function syncFullscreenBtn(on) {
    fullscreenBtn.innerHTML = on
      ? '<i class="fa-solid fa-compress"></i><span>나가기</span>'
      : '<i class="fa-solid fa-expand"></i><span>전체화면</span>';
    setTimeout(() => { if (!busy) layout(); }, 120);
  }

  function setImmersive(on) {
    viewerRoot.classList.toggle('is-immersive', on);
    syncFullscreenBtn(on || !!nativeFullscreen());
  }

  function isFullscreen() {
    return !!nativeFullscreen() || viewerRoot.classList.contains('is-immersive');
  }

  let safariHintShown = false;
  function enterFullscreen() {
    // 좁은 화면·터치 기기에서는 뷰어 바까지 접어 사진만 남긴다.
    if (immersiveOnly.matches) setImmersive(true);
    else syncFullscreenBtn(true);
    requestNative().catch(() => {
      setImmersive(true);
      if (isApple && !safariHintShown) {
        safariHintShown = true;
        notify('사파리는 주소창을 숨길 수 없어요. [공유] → [홈 화면에 추가]로 열면 완전한 전체화면이 됩니다.', 4600);
      }
    });
  }

  function leaveFullscreen() {
    if (nativeFullscreen()) exitNative();
    setImmersive(false);
  }

  fullscreenBtn.addEventListener('click', () => {
    if (isFullscreen()) leaveFullscreen();
    else enterFullscreen();
  });
  if (immersiveExit) immersiveExit.addEventListener('click', leaveFullscreen);

  ['fullscreenchange', 'webkitfullscreenchange'].forEach(type => {
    document.addEventListener(type, () => {
      const on = !!nativeFullscreen();
      // 시스템 제스처(뒤로가기 등)로 빠져나오면 접어 둔 바도 함께 돌려준다.
      if (!on) viewerRoot.classList.remove('is-immersive');
      syncFullscreenBtn(on);
    });
  });

  // 넓은 화면으로 바뀌면 몰입 모드를 풀어 상·하단 바를 돌려준다.
  immersiveOnly.addEventListener('change', event => {
    if (!event.matches && !nativeFullscreen()) setImmersive(false);
  });

  // 주소창이 접히거나 펴질 때(모바일)마다 실제 보이는 높이에 맞춰 다시 그린다.
  if (window.visualViewport) {
    let viewportRaf = null;
    const syncViewport = () => {
      if (viewportRaf) return;
      viewportRaf = requestAnimationFrame(() => {
        viewportRaf = null;
        viewerRoot.style.setProperty('--pb-vh', window.visualViewport.height + 'px');
        if (!busy) layout();
      });
    };
    window.visualViewport.addEventListener('resize', syncViewport);
    syncViewport();
  }

  render();
  window.addEventListener('load', layout);
})();
