const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.resolve(__dirname, '..');
const templatePath = path.join(root, 'templates', 'memo.html');
const source = fs.readFileSync(templatePath, 'utf8');
const scripts = [...source.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)]
    .map(match => match[1])
    .filter(body => body.trim());

if (!scripts.length) throw new Error('memo.html에서 인라인 스크립트를 찾지 못했습니다.');

scripts.forEach((body, index) => {
    const withoutJinja = body
        .replace(/\{\{[\s\S]*?\}\}/g, '0')
        .replace(/\{%[\s\S]*?%\}/g, '');
    new vm.Script(withoutJinja, {filename: `memo-inline-${index + 1}.js`});
});

const requiredTokens = [
    'refreshMeditationDailyScenes();',
    'showNextMeditationSound()',
    "data-theme-id=\"${theme.id}\"",
    "getMeditationSceneSound(scene, meditationSoundRotation)",
    "meditation/scenes/korea-spring.png",
    "classicalMoonlight",
    "jazzBossa",
    "musicboxLullaby"
];

requiredTokens.forEach(token => {
    if (!source.includes(token)) throw new Error(`필수 명상 기능 누락: ${token}`);
});

const themeMatches = [...source.matchAll(/\{id:'([^']+)'[\s\S]*?images:\[([\s\S]*?)\n\s{8}\]\}/g)];
if (themeMatches.length < 19) throw new Error(`명상 테마 수 부족: ${themeMatches.length}`);
themeMatches.forEach(([, themeId, images]) => {
    const imageCount = (images.match(/^\s*\['/gm) || []).length;
    if (imageCount < 3) throw new Error(`${themeId} 테마 이미지가 ${imageCount}장뿐입니다.`);
});

['korea-spring.png', 'korea-summer.png', 'korea-autumn.png'].forEach(filename => {
    const assetPath = path.join(root, 'static', 'meditation', 'scenes', filename);
    if (!fs.existsSync(assetPath) || fs.statSync(assetPath).size < 100_000) {
        throw new Error(`로컬 명상 풍경 에셋 누락: ${filename}`);
    }
});

console.log(`memo.html 스크립트 문법, ${themeMatches.length}개 테마의 다중 이미지, 날짜 랜덤 및 배경음 전환 확인 완료`);
