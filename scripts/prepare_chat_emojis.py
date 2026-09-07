"""Download the curated chat emoji assets. Python standard library only.

Run from the repository root. Existing assets are reused; a failed download
aborts publication so an existing catalog cannot silently lose stickers.
"""
import concurrent.futures
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1] / 'static' / 'chat-emoji'
GROUPS = {
    '표정': '😀|활짝 😃|신나요 😄|웃음 😁|방긋 😆|깔깔 😅|휴 😂|눈물나게웃음 🤣|빵터짐 😊|미소 😇|천사 🙂|좋아요 🙃|장난 😉|윙크 😍|반했어요 🥰|사랑스러워 😘|뽀뽀 😋|맛있어요 😛|메롱 😜|장난꾸러기 🤪|신나게 😎|멋져요 🤓|공부 🧐|살펴보기 🤩|최고 🥳|파티 😏|씨익 😌|편안 😔|시무룩 😢|슬퍼요 😭|엉엉 🥺|부탁해요 😤|흥 😡|화나요 🤯|충격 😳|깜짝 😱|놀랐어요 😰|초조 😥|아쉬워 😓|식은땀 🤗|포옹 🤔|생각중 🤭|웃음참기 🤫|조용히 🤥|거짓말 😶|말없이 😐|무표정 🙄|글쎄요 😬|난감 😮|놀람 😲|깜짝이야 🥱|하품 😴|졸려요 🤤|군침 😪|졸음 🤧|감기 🤒|몸살 🤕|아파요 🥵|더워요 🥶|추워요 😷|마스크 🤠|카우보이 🤑|부자 🤡|광대 👻|유령 👽|외계인 🤖|로봇 🎃|호박 😈|장꾸 💩|똥',
    '마음·인사': '👋|안녕하세요 🤚|잠깐 ✋|손들기 🖖|인사 👌|오케이 ✌️|브이 🤞|행운 🤟|사랑해 🤘|록 🤙|연락해 👍|엄지척 👎|아쉬워요 👊|파이팅 ✊|힘내요 🤛|주먹인사 🤜|주먹약속 👏|박수 🙌|만세 👐|환영 🤲|두손 🙏|감사합니다 🤝|악수 💪|할수있어 ❤️|사랑 🧡|주황하트 💛|노랑하트 💚|초록하트 💙|파랑하트 💜|보라하트 🖤|검정하트 🤍|하양하트 💖|반짝하트 💗|커지는마음 💓|두근두근 💞|마음나누기 💕|하트둘 💘|사랑의화살 💝|선물하트 💔|속상해 💌|마음편지 💋|입맞춤',
    '축하·업무': '🎉|축하해요 🎊|축제 🎈|풍선 🎁|선물 🎂|생일 🥂|건배 🍾|축하샴페인 🏆|수고했어요 🥇|일등 🏅|메달 💯|완벽해 ✅|확인완료 ☑️|체크 ✔️|완료 📌|공지 📍|위치 📎|첨부 📝|기록 📋|업무목록 📅|일정 ⏰|시간 ⏳|기다림 💻|작업중 📞|전화 📣|알려요 📢|안내 💡|아이디어 🔍|확인중 🚀|출발 🎯|목표 🔥|열정 ✨|반짝 ⭐|별 🌟|빛나는별 💫|반짝반짝 ⚡|번개 💤|휴식 ☕|커피 🍵|차 🧋|음료',
    '동물·자연': '🐶|강아지 🐱|고양이 🐭|생쥐 🐹|햄스터 🐰|토끼 🦊|여우 🐻|곰 🐼|판다 🐨|코알라 🐯|호랑이 🦁|사자 🐮|소 🐷|돼지 🐸|개구리 🐵|원숭이 🙈|안볼래 🙉|안들려 🙊|비밀 🐧|펭귄 🐤|병아리 🦆|오리 🦉|부엉이 🦋|나비 🐝|꿀벌 🐢|거북이 🐙|문어 🐬|돌고래 🐳|고래 🦄|유니콘 🌸|벚꽃 🌹|장미 🌻|해바라기 🌷|튤립 🌱|새싹 🌿|풀잎 🍀|행운클로버 🌈|무지개 ☀️|햇살 🌙|달 ❄️|눈송이 ☔|우산 ⛄|눈사람',
    '일상·음식': '🍎|사과 🍊|귤 🍋|레몬 🍌|바나나 🍉|수박 🍇|포도 🍓|딸기 🍒|체리 🍑|복숭아 🍍|파인애플 🥑|아보카도 🍞|빵 🥐|크루아상 🥨|프레첼 🧀|치즈 🍔|햄버거 🍟|감자튀김 🍕|피자 🌭|핫도그 🍿|팝콘 🍚|밥 🍜|국수 🍣|초밥 🍙|주먹밥 🍦|아이스크림 🍩|도넛 🍪|쿠키 🍫|초콜릿 🍬|사탕 🍭|막대사탕 🥛|우유 🥤|음료수 ⚽|축구 🏀|농구 🎵|음악 🎶|노래 🎸|기타 🎮|게임 🏠|집 🚗|자동차 ✈️|여행 🛌|잠자기',
}


def fetch(url, path):
    if path.exists():
        return path.read_bytes()
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=90) as response:
                data = response.read()
            break
        except urllib.error.HTTPError as error:
            if error.code < 500 and error.code != 429:
                raise
            if attempt == 2:
                raise
            time.sleep(attempt + 1)
        except (TimeoutError, urllib.error.URLError):
            if attempt == 2:
                raise
            time.sleep(attempt + 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    api = json.loads(fetch('https://googlefonts.github.io/noto-emoji-animation/data/api.json', ROOT / 'noto-api.json'))
    openmoji = json.loads(fetch('https://cdn.jsdelivr.net/npm/openmoji@15.1.0/data/openmoji.json', ROOT / 'openmoji-api.json'))
    cldr_root = 'https://raw.githubusercontent.com/unicode-org/cldr-json/46.1.0/cldr-json/'
    annotations = json.loads(fetch(cldr_root + 'cldr-annotations-full/annotations/ko/annotations.json', ROOT / 'ko-annotations.json'))
    derived = json.loads(fetch(cldr_root + 'cldr-annotations-derived-full/annotationsDerived/ko/annotations.json', ROOT / 'ko-annotations-derived.json'))
    if '--metadata-only' in sys.argv:
        print('OpenMoji:', len(openmoji), 'Noto:', len(api['icons']))
        print(openmoji[0])
        print('CLDR:', list(annotations), list(derived))
        return
    normalize = lambda code: '-'.join(part.lower() for part in re.split('[-_]', code) if part.lower() != 'fe0f')
    animated_codes = {normalize(icon['codepoint']): icon['codepoint'] for icon in api['icons']}
    translations = {}
    for document, section in [(annotations, 'annotations'), (derived, 'annotationsDerived')]:
        for emoji, values in document[section]['annotations'].items():
            translations[emoji.replace('\ufe0f', '')] = values
    entries = []
    for category, values in GROUPS.items():
        for value in values.split():
            emoji, label = value.split('|')
            code = '-'.join(f'{ord(char):x}' for char in emoji if ord(char) != 0xfe0f)
            entries.append(dict(id=code, emoji=emoji, label=label, category=category))
    by_id = {entry['id']: entry for entry in entries}
    used_labels = {entry['label'] for entry in entries}
    category_names = {'smileys-emotion':'표정', 'people-body':'사람·인사', 'animals-nature':'동물·자연', 'food-drink':'일상·음식', 'travel-places':'여행·교통', 'activities':'놀이·축하', 'objects':'물건·업무', 'symbols':'기호', 'flags':'깃발'}
    for item in openmoji:
        if item['group'] not in category_names:
            continue  # Private-use designer icons are not portable mini emojis.
        code = normalize(item['hexcode'])
        if code in by_id:
            by_id[code]['openmoji_code'] = item['hexcode']
            continue
        emoji = item['emoji']
        translated = translations.get(emoji.replace('\ufe0f', ''), {})
        label = (translated.get('tts') or [item['annotation']])[0]
        if label in used_labels:
            label += ' ' + emoji
        if label in used_labels or len(label) > 110 or ']' in label:
            raise ValueError(f'Invalid or duplicate sticker label: {label}')
        used_labels.add(label)
        category = category_names[item['group']]
        if 'heart' in item.get('subgroups', '') or item.get('subgroups') == 'emotion':
            category = '마음·인사'
        entry = dict(id=code, emoji=emoji, label=label, category=category, keywords=' '.join(translated.get('default', []) + [item['annotation'], item.get('tags', '')]), openmoji_code=item['hexcode'])
        entries.append(entry)
        by_id[code] = entry
    # Keep every existing message marker stable; include newer Noto-only emoji too.
    for icon in api['icons']:
        code = normalize(icon['codepoint'])
        if code in by_id:
            continue
        emoji = ''.join(chr(int(part, 16)) for part in re.split('[-_]', icon['codepoint']))
        translated = translations.get(emoji.replace('\ufe0f', ''), {})
        fallback_name = icon['tags'][0].strip(':').replace('-', ' ')
        fallback_ko = {'distorted face':'일그러진 얼굴', 'fight':'싸움', 'hairy creature':'털북숭이 괴물', 'debris':'잔해', 'orca':'범고래', 'trombone':'트롬본', 'treasure':'보물상자'}
        label = (translated.get('tts') or [fallback_ko.get(fallback_name, fallback_name)])[0]
        if label in used_labels:
            label += ' ' + emoji
        used_labels.add(label)
        category = {'Smileys and emotions':'표정', 'People':'사람·인사', 'Animals and nature':'동물·자연', 'Food and drink':'일상·음식', 'Travel and places':'여행·교통', 'Activities':'놀이·축하', 'Objects':'물건·업무', 'Symbols':'기호', 'Flags':'깃발'}.get(icon['categories'][0], '기호')
        entry = dict(id=code, emoji=emoji, label=label, category=category, keywords=' '.join(translated.get('default', [])))
        entries.append(entry)
        by_id[code] = entry
    if len(used_labels) != len(entries):
        raise ValueError('Sticker labels must be unique for readable message markers')

    archive_path = Path(tempfile.gettempdir()) / 'saedam-openmoji-15.1.0.tgz'
    print('Downloading OpenMoji color assets...', flush=True)
    archive_data = fetch('https://registry.npmjs.org/openmoji/-/openmoji-15.1.0.tgz', archive_path)
    with tarfile.open(fileobj=io.BytesIO(archive_data), mode='r:gz') as archive:
        for entry in entries:
            if 'openmoji_code' not in entry:
                continue
            destination = ROOT / 'openmoji' / (entry['id'] + '.svg')
            if not destination.exists():
                member = archive.extractfile('package/color/svg/' + entry['openmoji_code'] + '.svg')
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(member.read())

    def asset_job(entry, pack):
        code = entry['id']
        if pack == 'noto':
            url = f'https://fonts.gstatic.com/s/e/notoemoji/latest/{animated_codes[code]}/512.webp'
            extension = 'webp'
        else:
            full_code = entry['openmoji_code']
            url = f'https://cdn.jsdelivr.net/npm/openmoji@15.1.0/color/svg/{full_code}.svg'
            extension = 'svg'
        path = ROOT / pack / f'{code}.{extension}'
        try:
            data = fetch(url, path)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return entry, pack, None
            raise
        return entry, pack, dict(src=f'/static/chat-emoji/{pack}/{path.name}', source=url, sha256=hashlib.sha256(data).hexdigest())

    jobs = []
    print('Downloading animated assets...', flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        for entry in entries:
            if 'openmoji_code' in entry:
                jobs.append(pool.submit(asset_job, entry, 'openmoji'))
            # All selected animated entries are checked against actual assets.
            if entry['id'] in animated_codes:
                jobs.append(pool.submit(asset_job, entry, 'noto'))
        for number, job in enumerate(concurrent.futures.as_completed(jobs), 1):
            entry, pack, asset = job.result()
            if asset:
                entry[pack] = asset
            if number % 250 == 0:
                print(f'Assets: {number}/{len(jobs)}', flush=True)
    fetch('https://raw.githubusercontent.com/hfg-gmuend/openmoji/15.1.0/LICENSE.txt', ROOT / 'OPENMOJI-LICENSE.txt')
    fetch('https://raw.githubusercontent.com/unicode-org/cldr-json/46.1.0/LICENSE', ROOT / 'UNICODE-LICENSE.txt')
    (ROOT / 'NOTO-LICENSE.txt').write_text(
        'Animated Noto Emoji — Copyright Google LLC\n'
        'Licensed under Creative Commons Attribution 4.0 International (CC BY 4.0).\n'
        'License: https://creativecommons.org/licenses/by/4.0/\n'
        'Full legal code: https://creativecommons.org/licenses/by/4.0/legalcode\n'
        'Source: https://googlefonts.github.io/noto-emoji-animation/\n'
        'WebP files are redistributed without modification.\n', encoding='utf-8')
    (ROOT / 'catalog.json').write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding='utf-8')
    browser_entries = [{key: ({'src': value['src']} if key in ('noto', 'openmoji') else value) for key, value in entry.items() if key != 'openmoji_code'} for entry in entries]
    (ROOT / 'catalog.js').write_text('window.SAEDAM_CHAT_EMOJIS = ' + json.dumps(browser_entries, ensure_ascii=False, separators=(',', ':')) + ';\n', encoding='utf-8')
    print(json.dumps({'mini': len(entries), 'noto': sum('noto' in e for e in entries), 'openmoji': sum('openmoji' in e for e in entries)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
