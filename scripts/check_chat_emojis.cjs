/* Browser regression check with an isolated in-memory chat API; no real messages. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const output = process.env.EMOJI_QA_OUTPUT || path.join(require('node:os').tmpdir(), 'saedam-emoji-qa');
fs.mkdirSync(output, {recursive:true});
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
// catalog.json records the assets kept on disk; catalog.js is what the picker loads.
// Unused packs were pruned (scripts/chat_emoji_catalog.py --prune), so the two now match.
const catalog = JSON.parse(read('static/chat-emoji/catalog.json'));
const catalogJs = read('static/chat-emoji/catalog.js');
const shown = JSON.parse(catalogJs.slice(catalogJs.indexOf('['), catalogJs.lastIndexOf(']') + 1));
const stickerPacks = ['noto', 'openmoji', 'saedamgirl'];
const stickerCount = shown.reduce((sum, entry) => sum + stickerPacks.filter(pack => entry[pack]).length, 0);
const miniCount = shown.filter(entry => entry.mini !== false).length;
assert(shown.length > 300 && stickerCount > 380, 'Picker keeps a usable set of emojis');
assert.equal(new Set(catalog.map(entry => entry.id)).size, catalog.length, 'No duplicate emoji IDs');
const markers = catalog.flatMap(entry => stickerPacks.filter(pack => entry[pack]).map(pack => `${pack}:${entry.label}`));
assert.equal(new Set(markers).size, markers.length, 'Message markers are unique within each sticker pack');
const catalogById = new Map(catalog.map(entry => [entry.id, entry]));
assert(shown.every(entry => catalogById.has(entry.id)), 'Every shown emoji is recorded in catalog.json');
// Remote packs (animated Noto) are served from Google's CDN and kept out of the repo,
// so they carry a source URL and no local path; local packs must exist on disk.
assert(catalog.every(entry => stickerPacks.every(pack => !entry[pack]
    || (entry[pack].src ? fs.existsSync(path.join(root, entry[pack].src))
                        : /^https:\/\/fonts\.gstatic\.com\//.test(entry[pack].source)))),
    'catalog.json points at a file on disk or at the official CDN');
let html = read('templates/chat_popup.html').replace("{% include 'components/chat_emoji_panel.html' %}", read('templates/components/chat_emoji_panel.html'));
html = html.replace(/\{\{\s*url_for\('static', filename='([^']+)'\)\s*\}\}/g, '/static/$1')
    .replace(/\{\{ partner \| tojson \}\}/g, '"동료"')
    .replace(/\{\{ current_user \| tojson \}\}/g, '"테스트사용자"')
    .replace(/\{\{ chat_user_(?:icons|profile_paths) \| default\(\{\}\) \| tojson \}\}/g, '{}')
    .replace(/\{\{ partner \}\}/g, '동료');
assert(!html.includes('{{') && !html.includes('{%'), 'All template placeholders resolved');
for (const entry of catalog) for (const pack of stickerPacks) {
    if (!entry[pack] || !entry[pack].src) continue;  // CDN-hosted packs have nothing local to hash.
    const data = fs.readFileSync(path.join(root, entry[pack].src));
    assert.equal(crypto.createHash('sha256').update(data).digest('hex'), entry[pack].sha256);
    assert(!/<script|\sonload\s*=/i.test(data.toString()), 'SVG is an image asset');
}

(async () => {
    const browser = await chromium.launch({channel:'chrome', headless:true});
    try {
        const context = await browser.newContext({viewport:{width:745,height:760}});
        let messages = [{id:1, message_uid:'test-1', sender:'동료', content:'오늘도 수고 많으셨어요! 함께 힘내요 😊', sent_at:'2026-09-06T09:30:00Z'}];
        const errors = [], sent = [];
        let failSend = false;
        const room = {is_group:false, display_name:'동료', members:[], is_admin:false};
        await context.route('**/*', async route => {
            const request = route.request(), url = new URL(request.url());
            // Animated stickers load from Google's CDN; serve a local stand-in so the
            // run stays offline and deterministic.
            if (url.hostname === 'fonts.gstatic.com') {
                return route.fulfill({body: fs.readFileSync(path.join(root, 'static/chat-emoji/openmoji/1f600.svg')), contentType: 'image/svg+xml'});
            }
            if (url.hostname !== '127.0.0.1') return route.abort();
            if (url.pathname === '/chat') return route.fulfill({contentType:'text/html',body:html});
            if (url.pathname.startsWith('/static/')) {
                const file = path.join(root, decodeURIComponent(url.pathname));
                const types = {'.js':'text/javascript','.css':'text/css','.gif':'image/gif','.png':'image/png','.webp':'image/webp','.svg':'image/svg+xml','.html':'text/html'};
                return route.fulfill({body:fs.readFileSync(file),contentType:types[path.extname(file)] || 'text/plain'});
            }
            if (url.pathname === '/send_message') {
                const match = request.postData().match(/name="content"\r\n\r\n([\s\S]*?)\r\n--/);
                sent.push(match ? match[1] : '');
                await new Promise(resolve => setTimeout(resolve, 120));
                if (failSend) return route.fulfill({status:500,json:{message:'테스트 전송 실패'}});
                messages.push({id:messages.length+1, message_uid:'test-'+(messages.length+1), sender:'테스트사용자', content:sent.at(-1), sent_at:'2026-09-06T09:31:00Z'});
                return route.fulfill({json:{status:'success'}});
            }
            if (url.pathname.startsWith('/get_chat_history/')) return route.fulfill({json:{messages,room,has_more:false}});
            return route.fulfill({json:{room,polls:[],requests:[],statuses:{},presets:[]}});
        });
        const page = await context.newPage();
        page.on('pageerror', error => errors.push(error.message));
        page.on('dialog', dialog => dialog.dismiss());
        await page.goto('http://127.0.0.1:8766/chat');
        await page.locator('#chatBox [data-message-id="1"]').waitFor();
        await page.locator('#emojiToggle').click();
        assert.equal(await page.locator('.emoji-tile').count(), 84);
        assert.equal(await page.locator('#emojiResultCount').innerText(), stickerCount.toLocaleString('ko-KR') + '개');
        assert.equal(await page.locator('#emojiPacks, .emoji-panel-heading, #emojiHint, .emoji-credits').count(), 0);
        const closeBox = await page.locator('#emojiClose').boundingBox();
        const miniBox = await page.locator('#emojiMiniTab').boundingBox();
        assert(closeBox.x > miniBox.x + miniBox.width && Math.abs(closeBox.y - miniBox.y) < 15, 'Close sits right of mini tab');
        await page.locator('#emojiResults').evaluate(grid => { grid.scrollTop = grid.scrollHeight; });
        await page.waitForFunction(() => document.querySelectorAll('.emoji-tile').length > 84);
        assert.equal(await page.locator('.emoji-tile').count(), 168, 'Scrolling adds the next batch');
        const panel = await page.locator('#chatEmojiPicker').boundingBox();
        const main = await page.locator('#chatMain').boundingBox();
        assert.equal(panel.x, 0); assert.equal(panel.width, 320); assert.equal(main.x, 320);
        await page.getByRole('button', {name:'새담걸',exact:true}).click();
        assert.equal(await page.locator('.emoji-tile').count(), 23);
        const customImage = page.locator('.emoji-tile[data-emoji-pack="saedamgirl"] img').first();
        assert.equal(await customImage.getAttribute('draggable'), 'false');
        assert.equal(await customImage.evaluate(img => !img.dispatchEvent(new DragEvent('dragstart', {bubbles:true, cancelable:true}))), true);
        await page.locator('#chatInput').fill('작성 중인 초안');
        const doubleClickSentBefore = sent.length;
        await page.getByRole('button',{name:'확인해볼게요 새담걸 이모티콘 선택',exact:true}).dblclick();
        await page.waitForFunction(() => document.querySelectorAll('#chatBox .chat-sticker img').length === 1);
        assert.equal(sent.length, doubleClickSentBefore + 1, 'Double click sends one sticker immediately');
        assert.equal(sent.at(-1), '[새담걸 이모티콘: 확인해볼게요]');
        assert.equal(await page.locator('#chatInput').inputValue(), '작성 중인 초안', 'Immediate sticker send preserves the draft');
        assert(!(await page.locator('#chatEmojiSelection').isVisible()));
        const customStickerBox = await page.locator('#chatBox .emoji-art--saedamgirl').last().boundingBox();
        assert.equal(Math.round(customStickerBox.width), 150);
        assert.equal(Math.round(customStickerBox.height), 150);
        await page.locator('#chatBox').evaluate(box => {
            for (let index = 0; index < 20; index += 1) {
                const filler = document.createElement('div');
                filler.textContent = `스크롤 고정 확인 ${index + 1}`;
                box.appendChild(filler);
            }
            box.scrollTop = box.scrollHeight;
        });
        await page.getByRole('button',{name:'오케이 새담걸 이모티콘 선택',exact:true}).click();
        assert(await page.locator('#chatEmojiSelection').isVisible());
        await page.waitForFunction(() => {
            const box = document.getElementById('chatBox');
            return box.scrollHeight - box.scrollTop - box.clientHeight < 2;
        });
        await page.locator('#emojiSelectionCancel').click();
        await page.getByRole('button', {name:'여행·교통',exact:true}).click();
        assert.equal(await page.locator('#emojiResultsTitle').innerText(), '여행·교통');
        assert((await page.locator('.emoji-tile').count()) > 0);
        await page.getByRole('button', {name:'전체',exact:true}).click();
        await page.locator('#emojiMiniTab').click();
        assert.equal(await page.locator('#emojiResultCount').innerText(), miniCount.toLocaleString('ko-KR') + '개');
        assert.equal(await page.locator('.emoji-tile').count(), 84);
        await page.locator('#chatInput').fill('안녕 하세요');
        await page.locator('#chatInput').evaluate(input => input.setSelectionRange(3,3));
        await page.getByRole('button', {name:'활짝 삽입',exact:true}).click();
        assert.equal(await page.locator('#chatInput').inputValue(), '안녕 😀하세요');
        await page.locator('#emojiStickerTab').click();
        await page.getByRole('button',{name:'활짝 움직이는 이모티콘 선택',exact:true}).click();
        assert(await page.locator('#chatEmojiSelection').isVisible());
        failSend = true;
        await page.evaluate(() => sendChatReply());
        assert(await page.locator('#chatEmojiSelection').isVisible());
        assert.equal(await page.locator('#chatInput').inputValue(), '안녕 😀하세요');
        failSend = false;
        const countBefore = sent.length;
        await page.evaluate(() => Promise.all([sendChatReply(), sendChatReply()]));
        assert.equal(sent.length, countBefore+1, 'Repeated send is guarded');
        assert(sent.at(-1).includes('[움직이는 이모티콘: 활짝]'));
        assert.equal(await page.locator('#chatBox .chat-sticker img').count(), 2);
        assert(!(await page.locator('#chatEmojiSelection').isVisible()));
        await page.getByRole('button', {name:'새담걸',exact:true}).click();
        await page.getByRole('button',{name:'오케이 새담걸 이모티콘 선택',exact:true}).click();
        await page.evaluate(() => sendChatReply());
        assert(sent.at(-1).includes('[새담걸 이모티콘: 오케이]'));
        assert.equal(await page.locator('#chatBox .chat-sticker img').count(), 3);
        assert(!(await page.locator('#chatEmojiSelection').isVisible()));
        await page.getByRole('button',{name:'최근',exact:true}).click();
        assert.equal(await page.locator('.emoji-tile').count(), 3);
        const unsafe = await page.evaluate(() => ChatEmoji.renderText('<img src=x onerror=alert(1)>[움직이는 이모티콘: 가짜]'));
        assert(!unsafe.includes('<img')); assert(unsafe.includes('&lt;img'));
        await page.reload();
        await page.locator('#emojiToggle').click();
        await page.getByRole('button',{name:'최근',exact:true}).click();
        assert.equal(await page.locator('.emoji-tile').count(), 3, 'Recent choices persist');
        await page.getByRole('button',{name:'전체',exact:true}).click();
        await page.screenshot({path:path.join(output,'desktop.png')});
        // Incoming/group history uses the same validated image rendering.
        room.is_group = true; room.display_name = '업무 공유'; room.members = ['동료','테스트사용자'];
        messages.push({id:5,message_uid:'test-5',sender:'동료',content:'[컬러 이모티콘: 활짝]',sent_at:'2026-09-06T09:32:00Z'});
        await page.reload();
        await page.locator('#chatBox [data-message-id="5"]').waitFor();
        assert.equal(await page.locator('#chatBox .chat-sticker img').count(), 4);
        await page.setViewportSize({width:390,height:715});
        await page.locator('#emojiToggle').click();
        assert(await page.locator('#chatEmojiPicker').isVisible());
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
        await page.screenshot({path:path.join(output,'mobile.png')});
        await page.getByRole('button',{name:'활짝 움직이는 이모티콘 선택',exact:true}).click();
        await page.locator('#chatEmojiPicker').waitFor({state:'hidden'});
        assert(!(await page.locator('#chatEmojiPicker').isVisible()));
        assert(await page.locator('#chatEmojiSelection').isVisible());
        await page.keyboard.press('Escape');
        assert(!(await page.locator('#chatEmojiSelection').isVisible()));
        assert.equal(await page.locator('#chatInput').evaluate(input => input === document.activeElement), true);
        await page.locator('#emojiToggle').click();
        await page.keyboard.press('Escape');
        assert(!(await page.locator('#chatEmojiPicker').isVisible()));
        // Broken assets must retain a usable Unicode fallback.
        await page.locator('#chatBox .chat-sticker img').first().evaluate(img => img.dispatchEvent(new Event('error')));
        assert.equal(await page.locator('#chatBox .emoji-art.failed').count(), 1);
        // Reloading while the picker-expanded popup is wide must restore its saved geometry.
        await page.addInitScript(() => {
            window.__emojiWindowGeometryCalls = [];
            window.resizeTo = (width, height) => window.__emojiWindowGeometryCalls.push(['resize', width, height]);
            window.moveTo = (x, y) => window.__emojiWindowGeometryCalls.push(['move', x, y]);
        });
        await page.reload();
        await page.evaluate(() => sessionStorage.setItem('saedam.chat.emoji.window-size.v1', JSON.stringify({
            width:420, height:680, x:12, y:34, expandedWidth:window.outerWidth
        })));
        await page.reload();
        await page.waitForFunction(() => window.__emojiWindowGeometryCalls.length >= 2);
        assert.deepEqual(await page.evaluate(() => window.__emojiWindowGeometryCalls.slice(0, 2)), [
            ['resize', 420, 680], ['move', 12, 34]
        ]);
        assert.equal(await page.evaluate(() => sessionStorage.getItem('saedam.chat.emoji.window-size.v1')), null);
        assert.deepEqual(errors, []);
        console.log(JSON.stringify({status:'PASS',downloaded:catalog.length,mini:miniCount,stickers:stickerCount,checks:'asset hashes, tabs/categories/recent, double-click immediate send + draft preservation, 새담걸 150px rendering, failed send/retry, duplicate send, direct/group history, safe rendering, mobile/desktop, fallback, F5 popup-size restoration',screenshots:output}));
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
