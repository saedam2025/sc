"""면접 합격자 안내메일 - 준비서류 샘플 이미지 생성과 안내문 조립·발송.

면접관리에서 합격 처리한 지원자에게 출근 안내와 준비서류를 메일로 보낸다.
본문은 면접자 사전질문지와 같은 결(금색 테두리·한지 바탕·캐릭터)로 꾸미되,
메일 클라이언트가 CSS를 지우는 것을 감안해 표와 인라인 스타일만 사용한다.
"""

from __future__ import annotations

import io
import json
import os
import re
import smtplib
from datetime import datetime
from email.header import Header
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from html import escape
from typing import Any
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw, ImageFont

from routes.storage import APP_ROOT, PDF_FONT_ROOT, VERIFIED_PDF_FONT_ROOT


ORGANIZATION_NAME = '사단법인 새담청소년교육문화원'
MAIL_SENDER_LABEL = '새담청소년교육문화원'
MAIL_SETTINGS_PATH = os.path.join(str(APP_ROOT), 'mail_settings.json')
STATIC_ROOT = os.path.join(str(APP_ROOT), 'static')
# 안내메일에 함께 넣는 캐릭터와 기관 로고.
# 합격 안내는 축하하는 그림을, 불합격 안내는 정중히 인사하는 그림을 쓴다.
CHARACTER_IMAGE = os.path.join(STATIC_ROOT, 'girl_wel.png')
FAIL_CHARACTER_IMAGE = os.path.join(STATIC_ROOT, 'girl_re.png')
LOGO_IMAGE = os.path.join(STATIC_ROOT, 'logo01.gif')

# 안내 종류. 합격은 출근 안내와 준비서류를, 불합격은 결과와 인사말만 담는다.
NOTICE_KINDS = ('pass', 'fail')

MAX_SAMPLE_BYTES = 4 * 1024 * 1024
SAMPLE_MAX_EDGE = 900
EMAIL_RE = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]{2,}$')

# 준비서류 목록. (키, 제목, 안내문, 샘플 그림에 넣을 항목, 머리띠 색)
# '기타서류'는 담당자가 내용을 직접 적을 수 있도록 화면에서 별도 입력을 받는다.
GUIDE_DOCUMENTS = (
    {
        'key': 'resume',
        'label': '이력서',
        'note': '사진이 붙은 최신 이력서 1부 (면접 때 제출하셨다면 다시 준비하지 않으셔도 됩니다.)',
        'fields': ('성명 / 생년월일', '주소 / 연락처', '학력사항', '경력사항', '자격사항'),
        'accent': (25, 168, 135),
    },
    {
        'key': 'education',
        'label': '학력증명서(졸업장 등)',
        'note': '최종 학력의 졸업증명서 또는 졸업장 사본 1부',
        'fields': ('학교명 / 학과', '학위 구분', '입학일 / 졸업일', '발급기관 직인'),
        'accent': (73, 121, 232),
    },
    {
        'key': 'health',
        'label': '채용신체검사서',
        'note': '보건소 또는 병·의원에서 발급한 채용신체검사서 1부 (발급일로부터 3개월 이내)',
        'fields': ('성명 / 생년월일', '검사일 / 판정', '흉부 X선 / 혈압', '검사기관 / 의사 서명'),
        'accent': (219, 112, 76),
    },
    {
        'key': 'tuberculosis',
        'label': '잠복결핵검사서',
        'note': '보건소 또는 병·의원에서 발급한 잠복결핵감염 검사 결과서 1부',
        'fields': ('성명 / 생년월일', '검사방법(IGRA 등)', '검사일 / 결과', '검사기관 직인'),
        'accent': (156, 96, 196),
    },
    {
        'key': 'license',
        'label': '자격증 사본',
        'note': '보유하신 자격증 사본 각 1부 (원본은 첫 출근일에 지참해 주세요.)',
        'fields': ('자격 종목 / 등급', '자격번호', '취득일자', '발급기관'),
        'accent': (185, 154, 85),
    },
    {
        'key': 'career',
        'label': '경력증명서',
        'note': '이전 근무기관에서 발급한 경력증명서 1부',
        'fields': ('기관명 / 부서', '담당 업무 / 직위', '재직기간', '발급기관 직인'),
        'accent': (52, 138, 154),
    },
    {
        'key': 'etc',
        'label': '기타서류',
        'note': '아래 안내드린 서류를 함께 준비해 주세요.',
        'fields': ('서류명', '발급기관', '발급일자', '확인 직인'),
        'accent': (109, 123, 145),
    },
)
GUIDE_DOCUMENT_MAP = {item['key']: item for item in GUIDE_DOCUMENTS}
GUIDE_DOCUMENT_KEYS = tuple(item['key'] for item in GUIDE_DOCUMENTS)

_WEEKDAYS = ('월', '화', '수', '목', '금', '토', '일')


def document_catalog() -> list[dict[str, str]]:
    """화면에 뿌릴 준비서류 목록(내부 색상 값은 빼고 전달한다)."""
    return [
        {'key': item['key'], 'label': item['label'], 'note': item['note']}
        for item in GUIDE_DOCUMENTS
    ]


# ---------------------------------------------------------------- 한글 글꼴

_FONT_CACHE: dict[tuple[str, int], Any] = {}
_FONT_FILES: dict[str, str | None] = {}
_NANUM_URLS = {
    'regular': (
        'https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/nanumgothic/NanumGothic-Regular.ttf',
        'https://raw.githubusercontent.com/google/fonts/main/ofl/nanumgothic/NanumGothic-Regular.ttf',
    ),
    'bold': (
        'https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/nanumgothic/NanumGothic-Bold.ttf',
        'https://raw.githubusercontent.com/google/fonts/main/ofl/nanumgothic/NanumGothic-Bold.ttf',
    ),
}
_SYSTEM_FONTS = {
    'regular': (
        r'C:\Windows\Fonts\malgun.ttf',
        '/usr/share/fonts/truetype/nanum/NanumGothic.ttf',
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
        '/System/Library/Fonts/AppleSDGothicNeo.ttc',
    ),
    'bold': (
        r'C:\Windows\Fonts\malgunbd.ttf',
        '/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf',
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc',
        '/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc',
    ),
}


def _download_font(urls: tuple[str, ...], target: str) -> bool:
    """나눔고딕을 한 번만 내려받는다. 리눅스 서버에 한글 글꼴이 없을 때를 대비한다."""
    partial = target + '.part'
    for url in urls:
        try:
            request = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urlopen(request, timeout=40) as response, open(partial, 'wb') as output:
                output.write(response.read())
            if os.path.getsize(partial) < 100_000:
                raise ValueError('내려받은 글꼴 파일이 너무 작습니다.')
            os.replace(partial, target)
            return True
        except Exception:
            try:
                os.path.exists(partial) and os.remove(partial)
            except OSError:
                pass
    return False


def _font_file(weight: str) -> str | None:
    if weight in _FONT_FILES:
        return _FONT_FILES[weight]
    filename = 'NanumGothic-Bold.ttf' if weight == 'bold' else 'NanumGothic-Regular.ttf'
    candidates = [
        os.path.join(str(PDF_FONT_ROOT), filename),
        os.path.join(str(VERIFIED_PDF_FONT_ROOT), filename),
        *_SYSTEM_FONTS[weight],
    ]
    found = next((path for path in candidates if path and os.path.isfile(path)), None)
    if not found:
        target = os.path.join(str(PDF_FONT_ROOT), filename)
        try:
            os.makedirs(str(PDF_FONT_ROOT), exist_ok=True)
            if _download_font(_NANUM_URLS[weight], target):
                found = target
        except OSError:
            found = None
    _FONT_FILES[weight] = found
    return found


def _font(size: int, weight: str = 'regular') -> Any:
    key = (weight, size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    path = _font_file(weight)
    try:
        font = ImageFont.truetype(path, size) if path else ImageFont.load_default()
    except (OSError, ValueError):
        font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


# ---------------------------------------------------------------- 기본 샘플 그림

_BUILTIN_SAMPLE_CACHE: dict[str, bytes] = {}
SAMPLE_CANVAS = (600, 800)


def _text_width(draw: Any, text: str, font: Any) -> int:
    try:
        box = draw.textbbox((0, 0), text, font=font)
        return int(box[2] - box[0])
    except Exception:
        return len(text) * 10


def builtin_sample(doc_key: str) -> bytes:
    """준비서류 서식을 알아보기 쉽게 그린 '예시' 그림을 만든다.

    실제 발급 서류를 흉내 내는 것이 아니라 어떤 항목이 적혀 있어야 하는지
    보여주는 안내용 도해다. 오해가 없도록 큼직하게 '예시' 표시를 넣는다.
    """
    cached = _BUILTIN_SAMPLE_CACHE.get(doc_key)
    if cached:
        return cached
    document = GUIDE_DOCUMENT_MAP.get(doc_key) or GUIDE_DOCUMENTS[0]
    accent = document['accent']
    width, height = SAMPLE_CANVAS
    canvas = Image.new('RGB', (width, height), '#eef2f5')
    draw = ImageDraw.Draw(canvas)

    # 종이
    draw.rounded_rectangle((26, 26, width - 26, height - 26), radius=18, fill='#ffffff',
                           outline='#cbd6e2', width=2)
    # 제목 머리띠
    draw.rounded_rectangle((26, 26, width - 26, 140), radius=18, fill=accent)
    draw.rectangle((26, 116, width - 26, 140), fill=accent)
    title_font = _font(34, 'bold')
    title = str(document['label'])
    draw.text(((width - _text_width(draw, title, title_font)) / 2, 62), title,
              font=title_font, fill='#ffffff')

    # 항목 줄
    label_font = _font(21, 'bold')
    hint_font = _font(18)
    top = 190
    for index, field in enumerate(document['fields']):
        row_top = top + index * 96
        draw.rounded_rectangle((62, row_top, width - 62, row_top + 70), radius=12,
                               fill='#f7fafc', outline='#dde5ee', width=1)
        draw.rectangle((62, row_top + 12, 68, row_top + 58), fill=accent)
        draw.text((88, row_top + 12), str(field), font=label_font, fill='#2a3648')
        draw.line((88, row_top + 52, width - 92, row_top + 52), fill='#c7d3e0', width=2)
        draw.text((92, row_top + 44), '내용 기재란', font=hint_font, fill='#9fb0c2')

    # 발급기관 직인 자리
    stamp_center = (width - 132, height - 150)
    draw.ellipse((stamp_center[0] - 52, stamp_center[1] - 52,
                  stamp_center[0] + 52, stamp_center[1] + 52),
                 outline='#d05a5a', width=3)
    stamp_font = _font(24, 'bold')
    draw.text((stamp_center[0] - _text_width(draw, '직인', stamp_font) / 2, stamp_center[1] - 16),
              '직인', font=stamp_font, fill='#d05a5a')

    footer_font = _font(17)
    draw.text((62, height - 78), '※ 서식과 항목은 발급기관에 따라 다를 수 있습니다.',
              font=footer_font, fill='#8494a8')

    # '예시' 워터마크
    mark = Image.new('RGBA', (700, 250), (0, 0, 0, 0))
    mark_draw = ImageDraw.Draw(mark)
    mark_font = _font(150, 'bold')
    mark_text = '예 시'
    mark_draw.text(((700 - _text_width(mark_draw, mark_text, mark_font)) / 2, 10), mark_text,
                   font=mark_font, fill=(214, 74, 74, 52))
    small_font = _font(44, 'bold')
    mark_draw.text(((700 - _text_width(mark_draw, 'SAMPLE', small_font)) / 2, 186), 'SAMPLE',
                   font=small_font, fill=(214, 74, 74, 52))
    rotated = mark.rotate(22, expand=True, resample=Image.Resampling.BICUBIC)
    canvas.paste(rotated, ((width - rotated.width) // 2, (height - rotated.height) // 2), rotated)

    output = io.BytesIO()
    canvas.save(output, format='PNG', optimize=True)
    data = output.getvalue()
    _BUILTIN_SAMPLE_CACHE[doc_key] = data
    return data


_BRAND_CACHE: dict[str, dict[str, tuple[bytes, str]]] = {}


def _brand_image(path: str, box: tuple[int, int]) -> tuple[bytes, str] | None:
    """캐릭터·로고를 메일에 넣기 좋은 크기로 줄여 둔다(원본은 수백 KB라 그대로 못 쓴다).

    두 그림 모두 배경이 뚫려 있으므로 투명도를 그대로 살린 PNG로 저장한다.
    흰색으로 메워 버리면 한지색 본문 위에 흰 네모가 그대로 보인다.
    """
    if not os.path.isfile(path):
        return None
    try:
        with Image.open(path) as source:
            image = source.convert('RGBA')
            image.thumbnail(box, Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, format='PNG', optimize=True)
        return output.getvalue(), 'image/png'
    except Exception:
        return None


def brand_images(kind: str = 'pass') -> dict[str, tuple[bytes, str]]:
    """안내메일에 늘 들어가는 캐릭터와 기관 로고. 안내 종류에 따라 캐릭터가 다르다."""
    key = 'fail' if kind == 'fail' else 'pass'
    if key not in _BRAND_CACHE:
        images: dict[str, tuple[bytes, str]] = {}
        character = _brand_image(
            FAIL_CHARACTER_IMAGE if key == 'fail' else CHARACTER_IMAGE, (300, 300)
        )
        logo = _brand_image(LOGO_IMAGE, (260, 68))
        if character:
            images['character'] = character
        if logo:
            images['logo'] = logo
        _BRAND_CACHE[key] = images
    return dict(_BRAND_CACHE[key])


def normalize_sample_upload(data: bytes) -> tuple[bytes, str]:
    """담당자가 올린 샘플 사진을 메일에 넣기 좋은 크기의 JPEG으로 정리한다."""
    with Image.open(io.BytesIO(data)) as source:
        image = source.convert('RGB')
        image.thumbnail((SAMPLE_MAX_EDGE, SAMPLE_MAX_EDGE), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format='JPEG', quality=86, optimize=True)
    return output.getvalue(), 'image/jpeg'


# ---------------------------------------------------------------- 안내문 본문

def format_start_date(value: str) -> str:
    """'2026-09-10' -> '2026년 9월 10일 (목)'. 형식이 다르면 적힌 그대로 둔다."""
    raw = str(value or '').strip()
    if not raw:
        return ''
    try:
        parsed = datetime.strptime(raw[:10], '%Y-%m-%d').date()
    except ValueError:
        return raw
    return f'{parsed.year}년 {parsed.month}월 {parsed.day}일 ({_WEEKDAYS[parsed.weekday()]})'


def format_start_time(value: str) -> str:
    """'09:00' -> '오전 9시 00분'. 형식이 다르면 적힌 그대로 둔다."""
    raw = str(value or '').strip()
    if not raw:
        return ''
    match = re.match(r'^(\d{1,2}):(\d{2})', raw)
    if not match:
        return raw
    hour, minute = int(match.group(1)), match.group(2)
    if hour > 23:
        return raw
    meridiem = '오전' if hour < 12 else '오후'
    hour12 = hour % 12 or 12
    return f'{meridiem} {hour12}시 {minute}분'


def _html(value: object) -> str:
    return escape(str(value or ''), quote=True)


def _multiline_html(value: object) -> str:
    return _html(value).replace('\n', '<br>')


def selected_documents(documents: Any) -> list[dict[str, str]]:
    """화면에서 넘어온 준비서류 선택을 정해진 순서·형식으로 정리한다."""
    if isinstance(documents, str):
        try:
            documents = json.loads(documents or '[]')
        except (TypeError, ValueError, json.JSONDecodeError):
            documents = []
    chosen: dict[str, str] = {}
    for item in documents if isinstance(documents, list) else []:
        if isinstance(item, str):
            chosen.setdefault(item, '')
        elif isinstance(item, dict):
            key = str(item.get('key') or '').strip()
            if key:
                chosen[key] = str(item.get('detail') or '').strip()[:300]
    result = []
    for document in GUIDE_DOCUMENTS:
        if document['key'] not in chosen:
            continue
        result.append({
            'key': document['key'],
            'label': document['label'],
            'note': chosen[document['key']] or document['note'],
            'detail': chosen[document['key']],
        })
    return result


def _info_row(label: str, value: str, accent: str = '#0b7a63') -> str:
    return (
        '<tr>'
        f'<td style="padding:11px 14px;border-bottom:1px solid #efe6cf;background:#fbf7ec;'
        f'color:#7c6a44;font-size:13px;font-weight:700;white-space:nowrap;width:104px;">{_html(label)}</td>'
        f'<td style="padding:11px 16px;border-bottom:1px solid #efe6cf;color:{accent};'
        f'font-size:15px;font-weight:700;">{_multiline_html(value) or "-"}</td>'
        '</tr>'
    )


def render_mail_html(context: dict[str, Any], image_sources: dict[str, str]) -> str:
    """안내메일 본문(HTML)을 만든다.

    ``image_sources``는 그림 자리(character·logo·sample_<키>)마다 실제로 쓸 주소를
    담는다. 메일로 보낼 때는 ``cid:``, 화면 미리보기에서는 ``data:`` 주소를 넣는다.
    ``context['kind']``가 'fail'이면 불합격 안내문을 만든다.
    """
    if str(context.get('kind') or 'pass') == 'fail':
        return _fail_mail_html(context, image_sources)
    return _pass_mail_html(context, image_sources)


def _pass_mail_html(context: dict[str, Any], image_sources: dict[str, str]) -> str:
    """합격자에게 보내는 출근 안내 · 준비서류 안내문."""
    name = _html(context.get('name'))
    documents = context.get('documents') or []
    character = image_sources.get('character', '')
    logo = image_sources.get('logo', '')

    document_rows = []
    for index, document in enumerate(documents, start=1):
        sample = image_sources.get(f"sample_{document['key']}", '')
        sample_html = (
            '<div style="margin-top:12px;padding:10px;border:1px solid #e6ddc6;border-radius:10px;'
            'background:#fffdf7;text-align:center;">'
            f'<img src="{sample}" alt="{_html(document["label"])} 예시" '
            'style="display:block;margin:0 auto;max-width:260px;width:100%;height:auto;'
            'border:1px solid #e2e8ef;border-radius:8px;">'
            '<div style="margin-top:7px;color:#9a8a66;font-size:11px;">▲ 준비서류 예시 이미지</div>'
            '</div>'
        ) if sample else ''
        document_rows.append(
            '<tr><td style="padding:0 0 12px;">'
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
            'style="border:1px solid #e6ddc6;border-radius:12px;background:#ffffff;">'
            '<tr>'
            '<td width="42" valign="top" style="padding:14px 0 14px 14px;">'
            '<div style="width:28px;height:28px;line-height:28px;border-radius:50%;background:#0b7a63;'
            f'color:#ffffff;font-size:12px;font-weight:800;text-align:center;">{index}</div>'
            '</td>'
            '<td style="padding:14px 16px 14px 6px;">'
            f'<div style="color:#182231;font-size:15px;font-weight:800;">{_html(document["label"])}</div>'
            f'<div style="margin-top:4px;color:#6b7889;font-size:13px;line-height:1.6;">'
            f'{_multiline_html(document["note"])}</div>'
            f'{sample_html}'
            '</td></tr></table>'
            '</td></tr>'
        )
    documents_html = (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">'
        + ''.join(document_rows) + '</table>'
    ) if document_rows else (
        '<div style="padding:16px;border:1px dashed #e0d5b6;border-radius:11px;background:#fffdf7;'
        'color:#8b7a58;font-size:13px;">따로 준비하실 서류는 없습니다.</div>'
    )

    contact = str(context.get('contact') or '').strip()
    contact_line = (f'<br><span style="color:#6b7889;font-size:13px;">'
                    f'문의: {_html(contact)}</span>') if contact else ''
    extra_notes = str(context.get('extra_notes') or '').strip()
    extra_html = (
        '<div style="margin-top:22px;padding:16px 18px;border:1px solid #cfe9e0;border-radius:12px;'
        'background:#f2fbf8;">'
        '<div style="color:#0b7a63;font-size:12px;font-weight:800;letter-spacing:.08em;">기타 준비사항</div>'
        f'<div style="margin-top:7px;color:#2c3a4b;font-size:14px;line-height:1.75;">'
        f'{_multiline_html(extra_notes)}</div>'
        '</div>'
    ) if extra_notes else ''

    character_html = (
        f'<img src="{character}" alt="" width="150" '
        'style="display:block;margin:0 auto 6px;width:150px;max-width:46%;height:auto;">'
    ) if character else ''
    logo_html = (
        f'<img src="{logo}" alt="{_html(ORGANIZATION_NAME)}" '
        'style="display:block;margin:0 auto;height:34px;width:auto;">'
    ) if logo else (
        f'<div style="color:#8b7a58;font-size:13px;font-weight:700;">{_html(ORGANIZATION_NAME)}</div>'
    )

    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:26px 12px 44px;background:#eceff2;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
<tr><td align="center">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="640"
       style="width:100%;max-width:640px;">
<tr><td style="padding:13px;border-radius:20px;background:#f5eee0;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
       style="border:1px solid #ddd2b6;border-radius:14px;background:#fffdf6;">

<tr><td style="padding:30px 30px 22px;text-align:center;border-bottom:3px double #e3d3a8;">
    <div style="width:46px;height:46px;line-height:46px;margin:0 auto 12px;border-radius:50%;
                border:1px solid #e3d3a8;background:#ffffff;color:#b99a55;font-size:19px;">♥</div>
    <div style="color:#0b7a63;font-size:11px;font-weight:800;letter-spacing:.2em;">SAEDAM WELCOME GUIDE</div>
    <h1 style="margin:9px 0 0;color:#182231;font-size:23px;font-weight:800;letter-spacing:-.02em;">
        면접 합격을 진심으로 축하합니다</h1>
    {character_html}
    <div style="display:inline-block;margin-top:10px;padding:10px 22px;border-radius:11px;
                background:#eef8f5;color:#0b7a63;font-size:14px;font-weight:700;">
        <b>{name}</b> 님, {_html(ORGANIZATION_NAME)} 면접에 합격하셨습니다.</div>
</td></tr>

<tr><td style="padding:24px 30px 0;color:#3b4757;font-size:14px;line-height:1.85;">
    안녕하세요, {_html(ORGANIZATION_NAME)}입니다.<br>
    바쁘신 중에도 면접에 참여해 주셔서 감사드리며, <b>{name}</b> 님의 합격을 진심으로 축하드립니다.<br>
    첫 출근을 위해 아래 안내를 확인하시고 준비해 주시기 바랍니다.
</td></tr>

<tr><td style="padding:20px 30px 0;">
    <div style="color:#b99a55;font-size:11px;font-weight:800;letter-spacing:.16em;">FIRST DAY</div>
    <h2 style="margin:6px 0 12px;color:#182231;font-size:17px;font-weight:800;">출근 안내</h2>
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
           style="border:1px solid #efe6cf;border-radius:12px;overflow:hidden;">
        {_info_row('출근일', format_start_date(context.get('start_date')) or '담당자 안내 예정')}
        {_info_row('출근시간', format_start_time(context.get('start_time')) or '담당자 안내 예정')}
        {_info_row('출근장소', context.get('start_place') or '담당자 안내 예정')}
        {_info_row('담당자', context.get('contact') or '') if str(context.get('contact') or '').strip() else ''}
    </table>
</td></tr>

<tr><td style="padding:24px 30px 0;">
    <div style="color:#b99a55;font-size:11px;font-weight:800;letter-spacing:.16em;">CHECK LIST</div>
    <h2 style="margin:6px 0 12px;color:#182231;font-size:17px;font-weight:800;">준비서류</h2>
    {documents_html}
    {extra_html}
</td></tr>

<tr><td style="padding:24px 30px 0;">
    <div style="padding:14px 16px;border:1px dashed #e3d3a8;border-radius:11px;background:#fffaf0;
                color:#7c6a44;font-size:12px;line-height:1.75;">
        · 서류는 첫 출근일에 원본(또는 사본)으로 지참해 주세요.<br>
        · 준비가 어려운 서류가 있으면 미리 담당자에게 알려주시면 함께 방법을 찾아드립니다.<br>
        · 위 예시 이미지는 서류의 형태를 돕기 위한 안내용이며 실제 서식과 다를 수 있습니다.
    </div>
</td></tr>

<tr><td style="padding:26px 30px 30px;text-align:center;">
    <div style="color:#3b4757;font-size:14px;line-height:1.8;">
        함께 일하게 되어 기쁩니다. 첫 출근일에 뵙겠습니다.{contact_line}</div>
</td></tr>

<tr><td style="padding:18px 20px 22px;border-top:1px solid #e3d3a8;text-align:center;
               background:#fffaf0;border-radius:0 0 14px 14px;">
    {logo_html}
    <div style="margin-top:9px;color:#9a8a66;font-size:11px;line-height:1.7;">
        {_html(ORGANIZATION_NAME)}<br>
        본 메일은 새담 인트라넷 면접관리에서 발송되었습니다.
    </div>
</td></tr>

</table></td></tr></table>
</td></tr></table>
</body></html>"""


def _fail_mail_html(context: dict[str, Any], image_sources: dict[str, str]) -> str:
    """불합격자에게 보내는 결과 안내문.

    합격 안내와 같은 서식을 쓰되 축하 문구와 준비서류를 빼고, 지원해 주신 데 대한
    감사와 정중한 결과 안내만 담는다.
    """
    name = _html(context.get('name'))
    character = image_sources.get('character', '')
    logo = image_sources.get('logo', '')
    contact = str(context.get('contact') or '').strip()
    extra_notes = str(context.get('extra_notes') or '').strip()

    character_html = (
        f'<img src="{character}" alt="" width="128" '
        'style="display:block;margin:0 auto 6px;width:128px;max-width:40%;height:auto;">'
    ) if character else ''
    logo_html = (
        f'<img src="{logo}" alt="{_html(ORGANIZATION_NAME)}" '
        'style="display:block;margin:0 auto;height:34px;width:auto;">'
    ) if logo else (
        f'<div style="color:#8b7a58;font-size:13px;font-weight:700;">{_html(ORGANIZATION_NAME)}</div>'
    )
    extra_html = (
        '<div style="margin-top:20px;padding:16px 18px;border:1px solid #dfe6ee;border-radius:12px;'
        'background:#f7fafc;">'
        '<div style="color:#4a5a6d;font-size:12px;font-weight:800;letter-spacing:.08em;">추가 안내</div>'
        f'<div style="margin-top:7px;color:#2c3a4b;font-size:14px;line-height:1.75;">'
        f'{_multiline_html(extra_notes)}</div>'
        '</div>'
    ) if extra_notes else ''
    contact_html = (
        '<tr><td style="padding:20px 30px 0;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="border:1px solid #efe6cf;border-radius:12px;overflow:hidden;">'
        + _info_row('문의처', contact, '#4a5a6d') +
        '</table></td></tr>'
    ) if contact else ''

    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:26px 12px 44px;background:#eceff2;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
<tr><td align="center">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="640"
       style="width:100%;max-width:640px;">
<tr><td style="padding:13px;border-radius:20px;background:#f2eee6;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
       style="border:1px solid #ddd2b6;border-radius:14px;background:#fffdf6;">

<tr><td style="padding:30px 30px 22px;text-align:center;border-bottom:3px double #e3d3a8;">
    <div style="width:46px;height:46px;line-height:46px;margin:0 auto 12px;border-radius:50%;
                border:1px solid #e3d3a8;background:#ffffff;color:#b99a55;font-size:17px;">&#9993;</div>
    <div style="color:#7c6a44;font-size:11px;font-weight:800;letter-spacing:.2em;">SAEDAM INTERVIEW RESULT</div>
    <h1 style="margin:9px 0 0;color:#182231;font-size:22px;font-weight:800;letter-spacing:-.02em;">
        면접 결과를 안내드립니다</h1>
    {character_html}
    <div style="display:inline-block;margin-top:10px;padding:10px 22px;border-radius:11px;
                background:#f4f6f9;color:#405063;font-size:14px;font-weight:700;">
        <b>{name}</b> 님, {_html(ORGANIZATION_NAME)} 면접에 지원해 주셔서 감사합니다.</div>
</td></tr>

<tr><td style="padding:24px 30px 0;color:#3b4757;font-size:14px;line-height:1.85;">
    안녕하세요, {_html(ORGANIZATION_NAME)}입니다.<br>
    귀한 시간을 내어 저희 기관의 채용에 지원하고 면접에 참여해 주셔서 진심으로 감사드립니다.<br><br>
    제출해 주신 서류와 면접 내용을 바탕으로 신중하게 검토하였으나,
    아쉽게도 이번 채용에서는 <b>{name}</b> 님과 함께하지 못하게 되었습니다.<br><br>
    이번 결과는 {name} 님의 역량이 부족해서가 아니라, 모실 수 있는 인원과 담당 업무의 특성을
    함께 고려한 결과임을 널리 헤아려 주시기 바랍니다.
</td></tr>

<tr><td style="padding:22px 30px 0;">
    <div style="padding:14px 16px;border:1px dashed #e3d3a8;border-radius:11px;background:#fffaf0;
                color:#7c6a44;font-size:12px;line-height:1.75;">
        · 제출해 주신 서류는 관련 법령에 따라 일정 기간 보관한 뒤 안전하게 파기합니다.<br>
        · 서류 반환을 원하시면 담당자에게 알려주시면 안내해 드리겠습니다.<br>
        · 앞으로 좋은 기회가 있을 때 다시 지원해 주시면 반갑게 맞이하겠습니다.
    </div>
    {extra_html}
</td></tr>

{contact_html}

<tr><td style="padding:26px 30px 30px;text-align:center;">
    <div style="color:#3b4757;font-size:14px;line-height:1.8;">
        {name} 님의 앞날에 늘 좋은 일이 함께하기를 바랍니다.</div>
</td></tr>

<tr><td style="padding:18px 20px 22px;border-top:1px solid #e3d3a8;text-align:center;
               background:#fffaf0;border-radius:0 0 14px 14px;">
    {logo_html}
    <div style="margin-top:9px;color:#9a8a66;font-size:11px;line-height:1.7;">
        {_html(ORGANIZATION_NAME)}<br>
        본 메일은 새담 인트라넷 면접관리에서 발송되었습니다.
    </div>
</td></tr>

</table></td></tr></table>
</td></tr></table>
</body></html>"""


def _fail_mail_text(context: dict[str, Any]) -> str:
    """불합격 안내의 순수 글자 본문."""
    name = str(context.get('name') or '')
    lines = [
        f'{name} 님, {ORGANIZATION_NAME} 면접에 지원해 주셔서 감사합니다.',
        '',
        '제출해 주신 서류와 면접 내용을 바탕으로 신중하게 검토하였으나,',
        f'아쉽게도 이번 채용에서는 {name} 님과 함께하지 못하게 되었습니다.',
        '',
        '- 제출해 주신 서류는 관련 법령에 따라 일정 기간 보관한 뒤 안전하게 파기합니다.',
        '- 서류 반환을 원하시면 담당자에게 알려주시면 안내해 드리겠습니다.',
        '- 앞으로 좋은 기회가 있을 때 다시 지원해 주시면 반갑게 맞이하겠습니다.',
    ]
    extra_notes = str(context.get('extra_notes') or '').strip()
    if extra_notes:
        lines += ['', '[추가 안내]', extra_notes]
    contact = str(context.get('contact') or '').strip()
    if contact:
        lines += ['', f'문의처: {contact}']
    lines += ['', f'{name} 님의 앞날에 늘 좋은 일이 함께하기를 바랍니다.', '',
              ORGANIZATION_NAME, '본 메일은 새담 인트라넷 면접관리에서 발송되었습니다.']
    return '\n'.join(lines)


def render_mail_text(context: dict[str, Any]) -> str:
    """HTML을 못 보는 메일 프로그램을 위한 순수 글자 본문."""
    if str(context.get('kind') or 'pass') == 'fail':
        return _fail_mail_text(context)
    lines = [
        f"{context.get('name') or ''} 님, {ORGANIZATION_NAME} 면접에 합격하셨습니다.",
        '',
        '[출근 안내]',
        f"- 출근일: {format_start_date(context.get('start_date')) or '담당자 안내 예정'}",
        f"- 출근시간: {format_start_time(context.get('start_time')) or '담당자 안내 예정'}",
        f"- 출근장소: {str(context.get('start_place') or '담당자 안내 예정')}",
    ]
    contact = str(context.get('contact') or '').strip()
    if contact:
        lines.append(f'- 담당자: {contact}')
    lines += ['', '[준비서류]']
    documents = context.get('documents') or []
    if documents:
        lines += [f"{index}. {item['label']} - {item['note']}"
                  for index, item in enumerate(documents, start=1)]
    else:
        lines.append('따로 준비하실 서류는 없습니다.')
    contact = str(context.get('contact') or '').strip()
    contact_line = (f'<br><span style="color:#6b7889;font-size:13px;">'
                    f'문의: {_html(contact)}</span>') if contact else ''
    extra_notes = str(context.get('extra_notes') or '').strip()
    if extra_notes:
        lines += ['', '[기타 준비사항]', extra_notes]
    lines += ['', ORGANIZATION_NAME, '본 메일은 새담 인트라넷 면접관리에서 발송되었습니다.']
    return '\n'.join(lines)


# ---------------------------------------------------------------- 발송

def is_valid_email(value: object) -> bool:
    return bool(EMAIL_RE.match(str(value or '').strip()))


def mail_credentials() -> tuple[str, str]:
    """발송 계정을 환경변수에서 먼저 찾고, 없으면 mail_settings.json에서 읽는다.

    다른 기능(연락망·지출결의)과 같은 방식이라 Render에서는 환경변수만 넣어두면 된다.
    """
    sender_email = os.environ.get('MAIL_USERNAME', '').strip()
    sender_password = os.environ.get('MAIL_PASSWORD', '').strip()
    if (not sender_email or not sender_password) and os.path.exists(MAIL_SETTINGS_PATH):
        try:
            with open(MAIL_SETTINGS_PATH, encoding='utf-8') as source:
                settings = json.load(source)
            sender_email = sender_email or str(settings.get('MAIL_USERNAME') or settings.get('email') or '').strip()
            sender_password = sender_password or str(settings.get('MAIL_PASSWORD') or settings.get('password') or '').strip()
        except Exception:
            pass
    return sender_email, sender_password.replace(' ', '')


def _image_part(data: bytes, content_id: str, mime: str) -> MIMEImage:
    # 형식을 직접 지정한다. 파이썬 판에 따라 그림 형식 자동판별이 빠져 있을 수 있다.
    subtype = mime.split('/', 1)[1] if '/' in mime else 'png'
    part = MIMEImage(data, _subtype=subtype)
    part.add_header('Content-ID', f'<{content_id}>')
    part.add_header('Content-Disposition', 'inline')
    return part


def send_guide_mail(to_email: str, context: dict[str, Any],
                    images: dict[str, tuple[bytes, str]]) -> None:
    """안내메일을 보낸다. 실패하면 사유를 담은 RuntimeError를 올린다."""
    to_email = str(to_email or '').strip()
    if not is_valid_email(to_email):
        raise RuntimeError('받는 사람 이메일 주소 형식이 올바르지 않습니다.')
    sender_email, sender_password = mail_credentials()
    if not sender_email or not sender_password:
        raise RuntimeError(
            '메일 발송 계정이 설정되지 않았습니다. 서버의 MAIL_USERNAME·MAIL_PASSWORD를 확인해주세요.'
        )

    body_html = render_mail_html(context, {key: f'cid:{key}' for key in images})
    body_text = render_mail_text(context)
    title = ('님 면접 결과 안내' if str(context.get('kind') or 'pass') == 'fail'
             else '님 면접 합격 및 준비사항 안내')
    subject = f"[{MAIL_SENDER_LABEL}] {context.get('name') or ''} {title}".strip()

    message = MIMEMultipart('related')
    message['From'] = formataddr((str(Header(MAIL_SENDER_LABEL, 'utf-8')), sender_email))
    message['To'] = to_email
    message['Subject'] = Header(subject, 'utf-8')

    alternative = MIMEMultipart('alternative')
    alternative.attach(MIMEText(body_text, 'plain', 'utf-8'))
    alternative.attach(MIMEText(body_html, 'html', 'utf-8'))
    message.attach(alternative)
    for content_id, (data, mime) in images.items():
        message.attach(_image_part(data, content_id, mime))

    try:
        with smtplib.SMTP('smtp.gmail.com', 587, timeout=30) as server:
            server.starttls()
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, [to_email], message.as_string())
    except smtplib.SMTPAuthenticationError as exc:
        raise RuntimeError('메일 계정 인증에 실패했습니다. 앱 비밀번호를 확인해주세요.') from exc
    except smtplib.SMTPRecipientsRefused as exc:
        raise RuntimeError('받는 사람 메일 주소가 거부되었습니다.') from exc
    except Exception as exc:
        raise RuntimeError(f'메일 발송에 실패했습니다: {exc}') from exc
