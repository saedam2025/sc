import base64
import html
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pandas as pd
from bs4 import BeautifulSoup
from flask import Blueprint, current_app, jsonify, render_template, request, session
from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment
from urllib.parse import urlparse

from .ai_mail import (
    _clean_text,
    _csrf_token,
    _decrypt_password,
    _encrypt_password,
    _is_valid_email,
    _json_data,
    _login_required,
    _mutating,
    _owner_emp_no,
    _plain_from_html,
    _sanitize_html,
    _sender_dict,
    _smtp_error_info,
    _smtp_login,
)
from .database import get_db


payroll_bp = Blueprint('payroll', __name__)

DEFAULT_FORMS = {
    'form_basic': {
        'name': '기본 급여명세서(근로자)',
        'filename': 'payroll/employee_worker.html',
        'description': '근로자 급여·공제 내역용 기본 명세서',
        'match_keywords': '직원근로자, 센터장근로자, 근로자, 임직원, 직원, 센터장, 코디',
    },
    'form_instructor': {
        'name': '방과후강사 명세서',
        'filename': 'payroll/teacher.html',
        'description': '방과후강사 및 선택형 강사 명세서',
        'match_keywords': '방과후강사, 선택형, 맞춤형, 방과후, 강사',
    },
    'form_contract': {
        'name': '계약직·사업소득 명세서',
        'filename': 'payroll/employee_business.html',
        'description': '계약직 및 사업소득 대상 명세서',
        'match_keywords': '직원사업자, 사업자, 계약직, 프리랜서, 용역',
    },
    'form_retired': {
        'name': '퇴직자 명세서',
        'filename': 'payroll/retired.html',
        'description': '퇴직자 정산용 명세서',
        'match_keywords': '퇴직자, 퇴직, 퇴사, 정산',
    },
}
MAX_BANNER_BYTES = 4 * 1024 * 1024
MAX_TEMPLATE_BYTES = 2 * 1024 * 1024
ASSET_LIMITS = {'banner': 10, 'logo': 5}
DATA_IMAGE_RE = re.compile(r'^data:image/(png|jpeg|gif|webp);base64,([A-Za-z0-9+/=\s]+)$', re.I)
TRANSPARENT_PIXEL = 'data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs='
EXCEL_HEADER_ROW = 3
EXCEL_DATA_START_ROW = EXCEL_HEADER_ROW + 1
EXCEL_META_SHEET = '__sheet_name'
EXCEL_META_ROW = '__excel_row'
EXCEL_META_FILE = '__source_filename'
EXCEL_META_FORM = '__form_key'
EXCEL_META_TYPE = '__detected_type'
AUTO_FORM_KEY = 'auto'
AUTO_FORM_NAME = '자동적용 (엑셀 1행 구분 기준)'

NAME_COLUMN_ALIASES = (
    '수신자명', '직원명', '강사명', '퇴직자명', '근로자명', '사원명',
    '성명', '이름', '대상자명', '수령인명', '수령인', '예금주',
)
EMAIL_COLUMN_ALIASES = ('이메일', '이메일주소', '메일', '메일주소', 'e-mail', 'email')
BANK_COLUMN_ALIASES = ('은행', '지급은행', '은행명')
ACCOUNT_COLUMN_ALIASES = ('계좌번호', '계좌 번호')
HOLDER_COLUMN_ALIASES = ('예금주', '예금주명', '계좌주', '계좌주명')
TYPE_COLUMN_ALIASES = ('강사구분', '직원구분', '대상자구분', '고용구분', '구분')

_form_environment = SandboxedEnvironment(autoescape=True, undefined=StrictUndefined)
_form_environment.globals.clear()

_status_lock = threading.Lock()
_mail_statuses = {}


def _db():
    conn = get_db()
    conn.execute('PRAGMA busy_timeout=5000')
    return conn


def _ok(message='', **payload):
    result = {'status': 'success', 'message': message}
    result.update(payload)
    return jsonify(result)


def _error(message, status=400, **payload):
    result = {'status': 'error', 'message': message}
    result.update(payload)
    return jsonify(result), status


def _owned(conn, table, row_id):
    return conn.execute(
        f'SELECT * FROM {table} WHERE id=? AND owner_emp_no=?',
        (row_id, _owner_emp_no()),
    ).fetchone()


def _status_for(owner):
    with _status_lock:
        return _mail_statuses.setdefault(owner, {
            'campaign_id': None,
            'is_running': False,
            'stop_requested': False,
            'total_count': 0,
            'sent_count': 0,
            'failed_count': 0,
            'processed_count': 0,
            'recent_completed': [],
            'current_recipient': {},
            'upcoming_scheduled': [],
            'sent_names': [],
            'errors': [],
        })


def _image_value(value, label='이미지'):
    raw = str(value or '').strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme in ('http', 'https') and parsed.netloc:
        if len(raw) > 2048:
            raise ValueError(f'{label} 웹링크는 2,048자 이하여야 합니다.')
        return raw
    match = DATA_IMAGE_RE.fullmatch(raw)
    if not match:
        raise ValueError(f'{label}는 PNG, JPG, GIF, WEBP 파일 또는 http/https 웹링크만 등록할 수 있습니다.')
    try:
        decoded = base64.b64decode(re.sub(r'\s+', '', match.group(2)), validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(f'{label} 이미지 데이터가 올바르지 않습니다.') from exc
    if not decoded or len(decoded) > MAX_BANNER_BYTES:
        raise ValueError(f'{label} 한 개의 크기는 4MB 이하여야 합니다.')
    return f'data:image/{match.group(1).lower()};base64,{base64.b64encode(decoded).decode("ascii")}'


def _banner_value(value):
    return _image_value(value, '배너')


def _asset_dict(row):
    item = dict(row)
    item['preview_url'] = (
        item['source_value'] if item.get('source_type') == 'url'
        else f'/payroll/api/assets/{item["id"]}/content'
    )
    item['source_url'] = item['source_value'] if item.get('source_type') == 'url' else ''
    item.pop('source_value', None)
    return item


def _assets_for_owner(conn, owner):
    return conn.execute('''
        SELECT * FROM payroll_image_assets
        WHERE owner_emp_no=?
        ORDER BY asset_kind ASC, updated_at DESC, id DESC
    ''', (owner,)).fetchall()


def _migrate_legacy_banners(conn, owner):
    rows = conn.execute('''
        SELECT id, name, banner1_data, banner2_data, banner1_asset_id, banner2_asset_id
        FROM payroll_workgroups WHERE owner_emp_no=?
        ORDER BY id ASC
    ''', (owner,)).fetchall()
    existing = conn.execute('''
        SELECT id, source_value FROM payroll_image_assets
        WHERE owner_emp_no=? AND asset_kind='banner'
    ''', (owner,)).fetchall()
    by_value = {row['source_value']: row['id'] for row in existing}
    count = len(existing)
    changed = False
    for group in rows:
        for index in (1, 2):
            column = f'banner{index}'
            value = group[f'{column}_data']
            if not value or group[f'{column}_asset_id']:
                continue
            asset_id = by_value.get(value)
            if not asset_id and count < ASSET_LIMITS['banner']:
                base_name = f'기존 배너 {count + 1}'
                name = base_name
                suffix = 2
                while conn.execute('''
                    SELECT 1 FROM payroll_image_assets
                    WHERE owner_emp_no=? AND asset_kind='banner' AND name=?
                ''', (owner, name)).fetchone():
                    name = f'{base_name} ({suffix})'
                    suffix += 1
                cursor = conn.execute('''
                    INSERT INTO payroll_image_assets (owner_emp_no, asset_kind, name, source_type, source_value)
                    VALUES (?, 'banner', ?, 'file', ?)
                ''', (owner, name, value))
                asset_id = cursor.lastrowid
                by_value[value] = asset_id
                count += 1
            if asset_id:
                conn.execute(
                    f'UPDATE payroll_workgroups SET {column}_asset_id=? WHERE id=? AND owner_emp_no=?',
                    (asset_id, group['id'], owner),
                )
                changed = True
    if changed:
        conn.commit()


def _safe_body_html(value, allow_empty=False):
    sanitized = _sanitize_html(value)
    if len(sanitized.encode('utf-8')) > MAX_TEMPLATE_BYTES:
        raise ValueError('메일 내용은 2MB 이하여야 합니다.')
    if not BeautifulSoup(sanitized, 'html.parser').get_text(strip=True) and '<img' not in sanitized.lower():
        if allow_empty:
            return ''
        raise ValueError('메일 내용을 입력해주세요.')
    return sanitized


def _group_dict(row, assets_by_id=None):
    item = dict(row)
    item['banner1'] = item.pop('banner1_data', None)
    item['banner2'] = item.pop('banner2_data', None)
    assets_by_id = assets_by_id or {}
    for key in ('banner1', 'banner2', 'logo'):
        asset = assets_by_id.get(item.get(f'{key}_asset_id'))
        item[f'{key}_asset'] = asset
        item[f'{key}_preview'] = asset.get('preview_url') if asset else item.get(key)
    if item.get('form_type') == AUTO_FORM_KEY:
        item['form_name'] = AUTO_FORM_NAME
    else:
        item['form_name'] = item.get('form_name') or '등록되지 않은 폼'
    return item


def _asset_map(conn, owner):
    assets = [_asset_dict(row) for row in _assets_for_owner(conn, owner)]
    return assets, {asset['id']: asset for asset in assets}


def _hydrate_group_assets(conn, group_row):
    group = dict(group_row)
    owner = group['owner_emp_no']
    selections = (
        ('banner1', 'banner'),
        ('banner2', 'banner'),
        ('logo', 'logo'),
    )
    for field, expected_kind in selections:
        asset_id = group.get(f'{field}_asset_id')
        value = None
        if asset_id:
            asset = conn.execute('''
                SELECT * FROM payroll_image_assets
                WHERE id=? AND owner_emp_no=? AND asset_kind=?
            ''', (asset_id, owner, expected_kind)).fetchone()
            if asset:
                value = asset['source_value']
        if field.startswith('banner') and not value:
            value = group.get(f'{field}_data')
        group[f'{field}_value'] = value
    return group


def _selected_asset_id(conn, owner, value, expected_kind, field_label):
    if value in (None, ''):
        return None
    try:
        asset_id = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{field_label} 선택값이 올바르지 않습니다.') from exc
    row = conn.execute('''
        SELECT id FROM payroll_image_assets
        WHERE id=? AND owner_emp_no=? AND asset_kind=?
    ''', (asset_id, owner, expected_kind)).fetchone()
    if not row:
        raise ValueError(f'선택한 {field_label} 이미지를 찾을 수 확인해주세요.')
    return asset_id


def _template_dict(row):
    item = dict(row)
    item['is_active'] = bool(item.get('is_active'))
    item['is_system'] = bool(item.get('is_system'))
    item['description'] = item.get('description') or item.get('subject') or ''
    item['value'] = item.get('template_key')
    item['label'] = item.get('name')
    item['match_keywords'] = item.get('match_keywords') or ''
    return item


def _clean_match_keywords(value):
    raw_keywords = re.split(r'[,;|\n\r]+', str(value or ''))
    cleaned = []
    seen = set()
    for raw_keyword in raw_keywords:
        keyword = re.sub(r'\s+', ' ', raw_keyword).strip(' ,;|')[:80]
        normalized = _normalize_excel_label(keyword)
        if not keyword or not normalized or normalized in seen:
            continue
        seen.add(normalized)
        cleaned.append(keyword)
        if len(cleaned) >= 50:
            break
    return ', '.join(cleaned)


def safe_amount(*args):
    try:
        if len(args) >= 2 and hasattr(args[0], 'get'):
            fallback = args[2] if len(args) >= 3 else 0
            value = args[0].get(args[1], fallback)
            if value is None or (isinstance(value, str) and not value.strip()):
                value = fallback
        else:
            value = args[0] if args else 0
        if value is None or pd.isna(value) or str(value).strip() == '':
            return '0'
        return f'{int(float(value)):,}'
    except Exception:
        return '0'


def _sample_row():
    return {
        '직원명': '홍길동', '강사명': '홍길동', '이메일': 'sample@example.com',
        '직원구분': '근로자', '직책': '강사', '과목': '창의수학', '강의명': '창의수학',
        '은행': '새담은행', '지급은행': '새담은행', '계좌번호': '123-456-789012',
        '업무일환산': 2500000, '비과세': 100000, '직책수당': 150000, '특근수당': 50000,
        '기타지급내역': 0, '기타내역': 0, '지급총액': 2800000,
        '국민연금': 100000, '건강보험': 90000, '장기요양': 12000, '고용보험': 25000,
        '고용보험료': 25000, '산재보험': 0, '산재보험료': 0, '특별고용보험': 0,
        '사업소득세': 84000, '사업주민세': 8400, '소득세': 70000, '근로소득세': 70000,
        '기타 공제내역': 0, '기타공제내역': 0, '공제총액': 305000, '차인지급액': 2495000,
        '비고': '등록된 명세서 폼의 미리보기입니다.', '기타사항': '',
    }


def _render_form_source(source, row=None, send_date='2026-07-25', logo_url='', ad1_url='', ad2_url=''):
    template = _form_environment.from_string(str(source or ''))
    return template.render(
        row=dict(row or _sample_row()),
        send_date=send_date,
        safe_amount=safe_amount,
        logo_url=logo_url,
        ad1_url=ad1_url,
        ad2_url=ad2_url,
    )


def _safe_form_source(value):
    source = str(value or '').strip()
    if not source:
        raise ValueError('명세서 HTML 소스를 입력해주세요.')
    if len(source.encode('utf-8')) > MAX_TEMPLATE_BYTES:
        raise ValueError('명세서 HTML 폼은 2MB 이하여야 합니다.')
    lowered = source.lower()
    if re.search(r'<\s*(script|iframe|object|embed|form)\b', lowered) or re.search(r'\son[a-z]+\s*=', lowered) or 'javascript:' in lowered:
        raise ValueError('명세서 폼에는 스크립트, 실행 이벤트, 외부 삽입 태그를 사용할 수 없습니다.')
    try:
        _render_form_source(
            source,
            logo_url='https://example.com/logo.jpg',
            ad1_url=TRANSPARENT_PIXEL,
            ad2_url=TRANSPARENT_PIXEL,
        )
    except TemplateError as exc:
        raise ValueError(f'명세서 폼 문법을 확인해주세요: {exc}') from exc
    return source


def _ensure_default_forms(conn, owner):
    conn.execute('''
        UPDATE payroll_mail_templates
        SET template_key='custom_' || id,
            description=COALESCE(NULLIF(description, ''), subject, '사용자 추가 명세서 폼')
        WHERE owner_emp_no=? AND (template_key IS NULL OR template_key='')
    ''', (owner,))
    template_root = os.path.join(current_app.root_path, 'templates')
    for key, form in DEFAULT_FORMS.items():
        existing = conn.execute(
            'SELECT id, match_keywords FROM payroll_mail_templates WHERE owner_emp_no=? AND template_key=?',
            (owner, key),
        ).fetchone()
        if existing:
            if existing['match_keywords'] is None:
                conn.execute(
                    'UPDATE payroll_mail_templates SET match_keywords=? WHERE id=? AND owner_emp_no=?',
                    (form['match_keywords'], existing['id'], owner),
                )
            continue
        by_name = conn.execute(
            'SELECT id FROM payroll_mail_templates WHERE owner_emp_no=? AND name=?',
            (owner, form['name']),
        ).fetchone()
        source_path = os.path.join(template_root, *form['filename'].split('/'))
        with open(source_path, 'r', encoding='utf-8') as source_file:
            source = source_file.read()
        if by_name:
            conn.execute('''
                UPDATE payroll_mail_templates
                SET template_key=?, description=?, source_filename=?, match_keywords=?, is_system=1,
                    is_active=1, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND owner_emp_no=?
            ''', (key, form['description'], form['filename'], form['match_keywords'], by_name['id'], owner))
        else:
            conn.execute('''
                INSERT INTO payroll_mail_templates (
                    owner_emp_no, template_key, name, subject, description,
                    source_filename, match_keywords, body_html, is_system, is_active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 1)
            ''', (owner, key, form['name'], form['description'], form['description'], form['filename'], form['match_keywords'], source))
    conn.commit()


def _form_by_key(conn, template_key):
    return conn.execute('''
        SELECT * FROM payroll_mail_templates
        WHERE owner_emp_no=? AND template_key=? AND is_active=1
    ''', (_owner_emp_no(), template_key)).fetchone()


def _excel_has_value(value):
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass
    return bool(str(value).strip())


def _excel_column(columns, *names):
    normalized = {_normalize_excel_label(column): column for column in columns}
    for name in names:
        column = normalized.get(_normalize_excel_label(name))
        if column is not None:
            return column
    return None


def _normalize_excel_label(value):
    return re.sub(r'[\s:：_\-]+', '', str(value or '')).strip().lower()


def _row_value(row, aliases, default=''):
    columns = {_normalize_excel_label(column): column for column in row.keys()}
    for alias in aliases:
        column = columns.get(_normalize_excel_label(alias))
        if column is not None and _excel_has_value(row.get(column)):
            return row.get(column)
    return default


def _match_form_for_type(value, templates):
    detected = _normalize_excel_label(value)
    if not detected:
        return '', '구분 텍스트가 비어 있습니다.'
    matches = []
    for template in templates:
        for keyword in re.split(r'[,;|\n\r]+', str(template['match_keywords'] or '')):
            normalized_keyword = _normalize_excel_label(keyword)
            if not normalized_keyword:
                continue
            if detected == normalized_keyword:
                score = (2, len(normalized_keyword))
            elif normalized_keyword in detected:
                score = (1, len(normalized_keyword))
            else:
                continue
            matches.append((score, template['template_key'], template['name'], keyword.strip()))
    if not matches:
        return '', f'"{value}"와 일치하는 1행 인식 텍스트가 없습니다.'
    best_score = max(match[0] for match in matches)
    best = [match for match in matches if match[0] == best_score]
    best_keys = {match[1] for match in best}
    if len(best_keys) > 1:
        form_names = ', '.join(sorted({match[2] for match in best}))
        return '', f'"{value}"가 여러 폼에 동시에 일치합니다: {form_names}'
    return best[0][1], ''


def _sheet_type_from_first_row(first_row):
    values = list(first_row.tolist()) if first_row is not None else []
    for index, value in enumerate(values):
        label = _normalize_excel_label(value)
        if not label or not label.endswith('구분'):
            continue
        for candidate in values[index + 1:]:
            if _excel_has_value(candidate):
                return str(candidate).strip()
    return ''


def _load_excel(file_storage):
    if not file_storage or not file_storage.filename:
        raise ValueError('엑셀 파일을 업로드해주세요.')
    if not file_storage.filename.lower().endswith('.xlsx'):
        raise ValueError('.xlsx 형식의 엑셀 파일만 등록할 수 있습니다.')
    try:
        workbook = pd.ExcelFile(file_storage)
        sheets = pd.read_excel(
            workbook,
            sheet_name=None,
            header=EXCEL_HEADER_ROW - 1,
            dtype=object,
        )
        first_rows = pd.read_excel(
            workbook,
            sheet_name=None,
            header=None,
            nrows=1,
            dtype=object,
        )
    except Exception as exc:
        raise ValueError(f'엑셀 파일을 읽을 수 없습니다: {exc}') from exc

    target_frames = []
    invalid_sheets = []
    skipped_count = 0
    for sheet_name, sheet_df in sheets.items():
        sheet_df.columns = [str(column).strip() for column in sheet_df.columns]
        columns = list(sheet_df.columns)
        bank_column = _excel_column(columns, *BANK_COLUMN_ALIASES)
        account_column = _excel_column(columns, *ACCOUNT_COLUMN_ALIASES)
        holder_column = _excel_column(columns, *HOLDER_COLUMN_ALIASES)
        email_column = _excel_column(columns, *EMAIL_COLUMN_ALIASES)
        name_column = _excel_column(columns, *NAME_COLUMN_ALIASES)
        type_column = _excel_column(columns, *TYPE_COLUMN_ALIASES)
        missing = []
        if not email_column:
            missing.append('이메일')
        if not bank_column:
            missing.append('은행')
        if not account_column:
            missing.append('계좌번호')
        if not holder_column:
            missing.append('예금주')
        if missing:
            invalid_sheets.append(f"{file_storage.filename} / {sheet_name}: {', '.join(missing)}")
            continue

        first_frame = first_rows.get(sheet_name)
        first_row = first_frame.iloc[0] if first_frame is not None and len(first_frame) else None
        sheet_type = _sheet_type_from_first_row(first_row)
        sheet_targets = []
        visible_columns = [column for column in sheet_df.columns if not column.startswith('Unnamed:')]
        for row_offset, (_, row) in enumerate(sheet_df.iterrows(), start=EXCEL_DATA_START_ROW):
            has_bank_identity = all(
                _excel_has_value(row.get(column))
                for column in (bank_column, account_column, holder_column)
            )
            if not has_bank_identity:
                if any(_excel_has_value(row.get(column)) for column in visible_columns):
                    skipped_count += 1
                continue
            target = row.to_dict()
            target['은행'] = target.get(bank_column, '')
            target['계좌번호'] = target.get(account_column, '')
            target['예금주'] = target.get(holder_column, '')
            target['이메일'] = target.get(email_column, '')
            target_name = target.get(name_column, '') if name_column else ''
            if not _excel_has_value(target_name):
                target_name = target.get(holder_column, '')
            target['수신자명'] = target_name
            target.setdefault('직원명', target_name)
            target.setdefault('강사명', target_name)
            detected_type = sheet_type or (target.get(type_column, '') if type_column else '')
            target[EXCEL_META_SHEET] = sheet_name
            target[EXCEL_META_ROW] = row_offset
            target[EXCEL_META_FILE] = file_storage.filename
            target[EXCEL_META_TYPE] = str(detected_type or '').strip()
            target[EXCEL_META_FORM] = ''
            sheet_targets.append(target)

        if sheet_targets:
            target_frames.append(pd.DataFrame(sheet_targets))

    if invalid_sheets:
        details = '; '.join(invalid_sheets)
        raise ValueError(
            f'모든 시트의 {EXCEL_HEADER_ROW}번째 행에서 필수 제목을 확인해주세요. {details}'
        )

    if target_frames:
        frame = pd.concat(target_frames, ignore_index=True, sort=False).fillna('')
    else:
        frame = pd.DataFrame()
    frame.attrs['sheet_names'] = list(sheets.keys())
    frame.attrs['sheet_count'] = len(sheets)
    frame.attrs['skipped_count'] = skipped_count
    frame.attrs['file_names'] = [file_storage.filename]
    frame.attrs['file_count'] = 1
    return frame


def _load_excels(file_storages):
    files = [file_storage for file_storage in file_storages if file_storage and file_storage.filename]
    if not files:
        raise ValueError('엑셀 파일을 한 개 이상 업로드해주세요.')
    frames = [_load_excel(file_storage) for file_storage in files]
    non_empty = [frame for frame in frames if len(frame)]
    combined = pd.concat(non_empty, ignore_index=True, sort=False).fillna('') if non_empty else pd.DataFrame()
    combined.attrs['sheet_count'] = sum(int(frame.attrs.get('sheet_count', 0)) for frame in frames)
    combined.attrs['skipped_count'] = sum(int(frame.attrs.get('skipped_count', 0)) for frame in frames)
    combined.attrs['file_names'] = [file_storage.filename for file_storage in files]
    combined.attrs['file_count'] = len(files)
    return combined


def _uploaded_excel_files():
    files = request.files.getlist('excel')
    if not files:
        single = request.files.get('excel')
        files = [single] if single else []
    return [file_storage for file_storage in files if file_storage and file_storage.filename]


def _inspect_rows(frame, form_names=None):
    form_names = form_names or {}
    rows = []
    errors = []
    for _, row in frame.iterrows():
        email = str(row.get('이메일', '')).strip().lower()
        source_file = str(row.get(EXCEL_META_FILE, '')).strip()
        sheet_name = str(row.get(EXCEL_META_SHEET, '')).strip()
        excel_row = int(row.get(EXCEL_META_ROW, 0) or 0)
        location_parts = [part for part in (source_file, f'{sheet_name} 시트' if sheet_name else '') if part]
        location = f"{' / '.join(location_parts)} {excel_row}행".strip()
        name = _recipient_name(row)
        issue = ''
        if name == '이름 없음':
            issue = '이름 열을 인식할 수 없습니다'
            errors.append(f'[{location}] 수신자 이름을 확인해주세요.')
        elif not email:
            issue = '이메일이 비어 있습니다'
            errors.append(f'[{location}] {name}: 이메일이 비어 있습니다.')
        elif not _is_valid_email(email):
            issue = '이메일 주소 형식 오류'
            errors.append(f'[{location}] {name}: 이메일 주소 형식을 확인해주세요.')
        elif not str(row.get(EXCEL_META_FORM, '')).strip() or str(row.get(EXCEL_META_FORM, '')).strip() not in form_names:
            issue = '명세서 폼 자동 판별 실패'
            detected_type = str(row.get(EXCEL_META_TYPE, '')).strip() or '비어 있음'
            errors.append(f'[{location}] {name}: 구분 "{detected_type}"에 적용할 명세서 폼을 확인해주세요.')
        rows.append({
            'sheet': sheet_name,
            'source_file': source_file,
            'excel_row': excel_row,
            'name': name,
            'email': email,
            'detected_type': str(row.get(EXCEL_META_TYPE, '')).strip() or '미지정',
            'form_key': str(row.get(EXCEL_META_FORM, '')).strip(),
            'form_name': form_names.get(str(row.get(EXCEL_META_FORM, '')).strip(), '판별 불가'),
            'status': 'error' if issue else 'ready',
            'message': issue or '발송 준비 완료',
        })
    if not rows:
        errors.append('발송 대상 행이 없습니다.')
    return rows, errors


def _replace_variables(value, target_name, send_date):
    result = str(value or '')
    for key in ('{이름}', '{{이름}}', '{{수신자명}}'):
        result = result.replace(key, target_name)
    for key in ('{지급일}', '{{지급일}}'):
        result = result.replace(key, send_date)
    return result


def _image_parts(value, content_id):
    if not value:
        return '', None
    parsed = urlparse(str(value))
    if parsed.scheme in ('http', 'https') and parsed.netloc:
        return str(value), None
    match = DATA_IMAGE_RE.fullmatch(str(value))
    if not match:
        return '', None
    payload = base64.b64decode(re.sub(r'\s+', '', match.group(2)))
    image = MIMEImage(payload, _subtype=match.group(1).lower())
    image.add_header('Content-ID', f'<{content_id}>')
    image.add_header('Content-Disposition', 'inline', filename=f'{content_id}.{match.group(1).lower()}')
    return f'cid:{content_id}', image


def _wrapped_email_image(image_url, alt):
    if not image_url:
        return ''
    return (
        '<div style="text-align:center;margin:16px 0">'
        f'<img src="{html.escape(image_url, quote=True)}" alt="{html.escape(alt, quote=True)}" '
        'style="max-width:100%;height:auto"></div>'
    )


def _build_message(row, group, sender, password, send_date, base_url):
    target_name = _recipient_name(row)
    target_email = str(row.get('이메일', '')).strip()
    top_url, top_image = _image_parts(group.get('banner1_value') or group.get('banner1_data'), 'payroll-banner-top')
    bottom_url, bottom_image = _image_parts(group.get('banner2_value') or group.get('banner2_data'), 'payroll-banner-bottom')
    logo_value = group.get('logo_value')
    if logo_value:
        logo_url, logo_image = _image_parts(logo_value, 'payroll-company-logo')
    else:
        logo_url, logo_image = base_url + '/static/logo01.jpg', None
    top_html = _wrapped_email_image(top_url, '광고 배너 1')
    bottom_html = _wrapped_email_image(bottom_url, '광고 배너 2')
    form_key = str(row.get(EXCEL_META_FORM, '')).strip()
    form_source = (group.get('form_sources') or {}).get(form_key) or group.get('form_source')
    statement = _render_form_source(
        form_source,
        row=dict(row),
        send_date=send_date,
        logo_url=logo_url,
        ad1_url=top_url or TRANSPARENT_PIXEL,
        ad2_url=bottom_url or TRANSPARENT_PIXEL,
    )
    body = _replace_variables(group.get('body_html'), target_name, send_date)
    if '{{명세서}}' in body:
        body = body.replace('{{명세서}}', statement)
    else:
        body = body + statement
    body = body.replace('{{상단배너}}', top_html).replace('{{하단배너}}', bottom_html)

    subject = _replace_variables(group.get('subject'), target_name, send_date)
    message = MIMEMultipart('related')
    alternative = MIMEMultipart('alternative')
    message.attach(alternative)
    message['Subject'] = subject
    message['From'] = f"{sender['label']} <{sender['email']}>"
    message['To'] = target_email
    alternative.attach(MIMEText(_plain_from_html(body), 'plain', 'utf-8'))
    alternative.attach(MIMEText(body, 'html', 'utf-8'))
    if top_image:
        message.attach(top_image)
    if bottom_image:
        message.attach(bottom_image)
    if logo_image:
        message.attach(logo_image)
    return message, target_name


def _recipient_name(row):
    return str(_row_value(row, NAME_COLUMN_ALIASES, '')).strip() or '이름 없음'


def _recipient_type(row):
    return str(row.get(EXCEL_META_TYPE) or _row_value(row, TYPE_COLUMN_ALIASES, '')).strip() or '미지정'


def _recipient_school(row):
    return str(_row_value(row, ('학교명', '학교', '근무학교', '기관명'), '')).strip()


def _create_campaign_recipients(conn, owner, campaign_id, frame):
    recipient_ids = []
    for _, row in frame.iterrows():
        source_file = str(row.get(EXCEL_META_FILE, '')).strip()
        sheet_name = str(row.get(EXCEL_META_SHEET, '')).strip()
        source_location = ' / '.join(part for part in (source_file, sheet_name) if part)
        cursor = conn.execute('''
            INSERT INTO payroll_campaign_recipients (
                campaign_id, owner_emp_no, sheet_name, excel_row,
                recipient_type, school_name, recipient_name, email, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued')
        ''', (
            campaign_id,
            owner,
            source_location,
            int(row.get(EXCEL_META_ROW, 0) or 0),
            _recipient_type(row),
            _recipient_school(row),
            _recipient_name(row),
            str(row.get('이메일', '')).strip(),
        ))
        recipient_ids.append(cursor.lastrowid)
    return recipient_ids


def _campaign_recipient_start(owner, campaign_id, recipient_id):
    conn = _db()
    try:
        conn.execute('''
            UPDATE payroll_campaign_recipients
            SET status='running', started_at=CURRENT_TIMESTAMP, error_message=''
            WHERE id=? AND campaign_id=? AND owner_emp_no=?
        ''', (recipient_id, campaign_id, owner))
        conn.commit()
    finally:
        conn.close()


def _campaign_recipient_finish(owner, campaign_id, recipient_id, result_status, error_message, elapsed_seconds):
    conn = _db()
    try:
        conn.execute('''
            UPDATE payroll_campaign_recipients
            SET status=?, error_message=?, elapsed_seconds=?,
                started_at=COALESCE(started_at, CURRENT_TIMESTAMP),
                finished_at=CURRENT_TIMESTAMP
            WHERE id=? AND campaign_id=? AND owner_emp_no=?
        ''', (
            result_status,
            str(error_message or '')[:2000],
            round(max(0.0, float(elapsed_seconds or 0)), 3),
            recipient_id,
            campaign_id,
            owner,
        ))
        conn.commit()
    finally:
        conn.close()


def _campaign_mark_remaining(owner, campaign_id, result_status, error_message):
    conn = _db()
    try:
        conn.execute('''
            UPDATE payroll_campaign_recipients
            SET status=?, error_message=?, finished_at=CURRENT_TIMESTAMP
            WHERE campaign_id=? AND owner_emp_no=? AND status='queued'
        ''', (result_status, str(error_message or '')[:2000], campaign_id, owner))
        conn.commit()
    finally:
        conn.close()


def _campaign_update(owner, campaign_id, status):
    conn = _db()
    try:
        conn.execute('''
            UPDATE payroll_campaigns
            SET status=?, processed_count=?, sent_count=?, failed_count=?, errors_json=?,
                updated_at=CURRENT_TIMESTAMP,
                started_at=COALESCE(started_at, CURRENT_TIMESTAMP),
                finished_at=CASE WHEN ? IN ('completed','completed_with_errors','cancelled','failed') THEN CURRENT_TIMESTAMP ELSE finished_at END
            WHERE id=? AND owner_emp_no=?
        ''', (
            status['campaign_status'], status['processed_count'], status['sent_count'],
            int(status.get('failed_count', len(status['errors']))),
            json.dumps(status['errors'], ensure_ascii=False), status['campaign_status'], campaign_id, owner,
        ))
        conn.commit()
    finally:
        conn.close()


def payroll_worker(app, owner, campaign_id, frame, recipient_ids, send_date, interval, base_url, group, sender):
    status = _status_for(owner)
    with app.app_context():
        status['campaign_status'] = 'running'
        _campaign_update(owner, campaign_id, status)
        try:
            password = _decrypt_password(sender['encrypted_app_password'])
            smtp = _smtp_login(sender['email'], password)
        except Exception as exc:
            status['errors'] = [f'발송계정 연결 실패: {exc}']
            status['failed_count'] = status['total_count']
            status['processed_count'] = status['total_count']
            status['is_running'] = False
            status['campaign_status'] = 'failed'
            _campaign_mark_remaining(owner, campaign_id, 'failed', f'발송계정 연결 실패: {exc}')
            _campaign_update(owner, campaign_id, status)
            return

        try:
            for position, (_, row) in enumerate(frame.iterrows()):
                if status.get('stop_requested'):
                    break
                recipient_id = recipient_ids[position]
                status['recent_completed'] = [
                    {
                        'type': _recipient_type(frame.iloc[i]), 
                        'name': _recipient_name(frame.iloc[i]), 
                        'email': str(frame.iloc[i].get('이메일', '')).strip()
                    }
                    for i in range(max(0, position - 3), position)
                ]
                status['current_recipient'] = {
                    'type': _recipient_type(row), 
                    'name': _recipient_name(row), 
                    'email': str(row.get('이메일', '')).strip()
                }
                status['upcoming_scheduled'] = [
                    {
                        'type': _recipient_type(frame.iloc[i]), 
                        'name': _recipient_name(frame.iloc[i]), 
                        'email': str(frame.iloc[i].get('이메일', '')).strip()
                    }
                    for i in range(position + 1, min(len(frame), position + 4))
                ]
                _campaign_recipient_start(owner, campaign_id, recipient_id)
                started = time.perf_counter()
                try:
                    message, target_name = _build_message(row, group, sender, password, send_date, base_url)
                    smtp.send_message(message)
                    status['sent_count'] += 1
                    status['sent_names'].append(target_name)
                    _campaign_recipient_finish(
                        owner, campaign_id, recipient_id, 'sent', '', time.perf_counter() - started
                    )
                except Exception as exc:
                    target_name = _recipient_name(row)
                    status['errors'].append(f'[{target_name}] {exc}')
                    status['failed_count'] += 1
                    _campaign_recipient_finish(
                        owner, campaign_id, recipient_id, 'failed', str(exc), time.perf_counter() - started
                    )
                status['processed_count'] += 1
                status['campaign_status'] = 'running'
                _campaign_update(owner, campaign_id, status)
                if position < len(frame) - 1 and not status.get('stop_requested'):
                    time.sleep(float(interval))
        finally:
            try:
                smtp.quit()
            except Exception:
                pass
        status['is_running'] = False
        status['recent_completed'] = []
        status['current_recipient'] = {}
        status['upcoming_scheduled'] = []
        if status.get('stop_requested'):
            status['campaign_status'] = 'cancelled'
            _campaign_mark_remaining(owner, campaign_id, 'cancelled', '사용자 요청으로 발송하지 않았습니다.')
        elif status.get('failed_count', 0):
            status['campaign_status'] = 'completed_with_errors'
        else:
            status['campaign_status'] = 'completed'
        _campaign_update(owner, campaign_id, status)


def _seconds_between(started_at, finished_at):
    if not started_at:
        return 0.0
    try:
        started = datetime.fromisoformat(str(started_at)).replace(tzinfo=timezone.utc)
        finished = (
            datetime.fromisoformat(str(finished_at)).replace(tzinfo=timezone.utc)
            if finished_at else datetime.now(timezone.utc)
        )
        return round(max(0.0, (finished - started).total_seconds()), 3)
    except (TypeError, ValueError):
        return 0.0


def _history_campaign_dict(row):
    item = dict(row)
    try:
        item['errors'] = json.loads(item.get('errors_json') or '[]')
    except (TypeError, ValueError, json.JSONDecodeError):
        item['errors'] = []
    item.pop('errors_json', None)
    item['duration_seconds'] = _seconds_between(item.get('started_at'), item.get('finished_at'))
    processed_count = int(item.get('processed_count') or 0)
    item['average_seconds'] = round(item['duration_seconds'] / processed_count, 3) if processed_count else 0.0
    return item


def _history_page(conn, owner, page=1, page_size=10):
    total_items = int(conn.execute(
        'SELECT COUNT(*) FROM payroll_campaigns WHERE owner_emp_no=?', (owner,)
    ).fetchone()[0])
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    page = max(1, min(int(page or 1), total_pages))
    rows = conn.execute('''
        SELECT * FROM payroll_campaigns
        WHERE owner_emp_no=?
        ORDER BY created_at DESC, id DESC
        LIMIT ? OFFSET ?
    ''', (owner, page_size, (page - 1) * page_size)).fetchall()
    return {
        'items': [_history_campaign_dict(row) for row in rows],
        'page': page,
        'page_size': page_size,
        'total_items': total_items,
        'total_pages': total_pages,
    }


@payroll_bp.route('/', strict_slashes=False)
@_login_required
def index():
    return render_template('payroll_form.html')


@payroll_bp.route('/api/bootstrap')
@_login_required
def bootstrap():
    conn = _db()
    try:
        owner = _owner_emp_no()
        _ensure_default_forms(conn, owner)
        _migrate_legacy_banners(conn, owner)
        assets, assets_by_id = _asset_map(conn, owner)
        groups = conn.execute('''
            SELECT g.*, f.name AS form_name
            FROM payroll_workgroups g
            LEFT JOIN payroll_mail_templates f
              ON f.owner_emp_no=g.owner_emp_no AND f.template_key=g.form_type AND f.is_active=1
            WHERE g.owner_emp_no=?
            ORDER BY g.updated_at DESC, g.id DESC
        ''', (owner,)).fetchall()
        senders = conn.execute('SELECT * FROM ai_mail_senders WHERE owner_emp_no=? AND is_active=1 ORDER BY updated_at DESC, id DESC', (_owner_emp_no(),)).fetchall()
        templates = conn.execute('''
            SELECT * FROM payroll_mail_templates
            WHERE owner_emp_no=? AND is_active=1
            ORDER BY is_system DESC, id ASC
        ''', (owner,)).fetchall()
        history_page = _history_page(conn, owner, 1, 10)
        forms = [_template_dict(row) for row in templates]
        return _ok(
            csrf_token=_csrf_token(),
            groups=[_group_dict(row, assets_by_id) for row in groups],
            assets=assets,
            senders=[_sender_dict(row) for row in senders],
            templates=forms,
            history=history_page['items'],
            history_pagination={key: value for key, value in history_page.items() if key != 'items'},
            forms=forms,
        )
    finally:
        conn.close()


@payroll_bp.route('/api/groups', methods=['POST'])
@_mutating
def create_group():
    data = _json_data()
    name = _clean_text(data.get('name'), 120)
    subject = _clean_text(data.get('subject'), 300)
    form_type = _clean_text(data.get('form_type'), 40)
    if not name or not subject:
        return _error('작업그룹명, 명세서 폼, 메일 제목을 모두 입력해주세요.')
    try:
        body_html = _safe_body_html(data.get('body_html'), allow_empty=True)
        banner1 = _banner_value(data.get('banner1')) if data.get('banner1') else None
        banner2 = _banner_value(data.get('banner2')) if data.get('banner2') else None
    except ValueError as exc:
        return _error(str(exc))
    conn = _db()
    try:
        owner = _owner_emp_no()
        banner1_asset_id = _selected_asset_id(conn, owner, data.get('banner1_asset_id'), 'banner', '광고 배너 1')
        banner2_asset_id = _selected_asset_id(conn, owner, data.get('banner2_asset_id'), 'banner', '광고 배너 2')
        logo_asset_id = _selected_asset_id(conn, owner, data.get('logo_asset_id'), 'logo', '회사 로고')
        if banner1_asset_id and banner1_asset_id == banner2_asset_id:
            return _error('광고 배너 1과 2는 서로 다른 이미지를 선택해주세요.')
        form = None if form_type == AUTO_FORM_KEY else _form_by_key(conn, form_type)
        if form_type != AUTO_FORM_KEY and not form:
            return _error('선택한 명세서 폼을 찾을 수 없습니다.')
        cursor = conn.execute('''
            INSERT INTO payroll_workgroups (
                owner_emp_no, name, form_type, subject, body_html,
                banner1_data, banner2_data, banner1_asset_id, banner2_asset_id, logo_asset_id,
                memo, template_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (owner, name, form_type, subject, body_html, banner1, banner2, banner1_asset_id, banner2_asset_id, logo_asset_id, _clean_text(data.get('memo'), 1000), data.get('template_id') or None))
        conn.commit()
        saved = dict(_owned(conn, 'payroll_workgroups', cursor.lastrowid))
        saved['form_name'] = AUTO_FORM_NAME if form_type == AUTO_FORM_KEY else form['name']
        _, assets_by_id = _asset_map(conn, owner)
        return _ok('작업그룹이 저장되었습니다.', group=_group_dict(saved, assets_by_id))
    except sqlite3.IntegrityError:
        conn.rollback()
        return _error('같은 이름의 작업그룹이 이미 있습니다.', 409)
    except ValueError as exc:
        conn.rollback()
        return _error(str(exc))
    finally:
        conn.close()


@payroll_bp.route('/api/groups/<int:group_id>', methods=['PATCH', 'PUT'])
@_mutating
def update_group(group_id):
    data = _json_data()
    conn = _db()
    try:
        current = _owned(conn, 'payroll_workgroups', group_id)
        if not current:
            return _error('작업그룹을 찾을 수 없습니다.', 404)
        name = _clean_text(data.get('name', current['name']), 120)
        subject = _clean_text(data.get('subject', current['subject']), 300)
        form_type = _clean_text(data.get('form_type', current['form_type']), 40)
        if not name or not subject:
            return _error('작업그룹 필수 항목을 확인해주세요.')
        form = None if form_type == AUTO_FORM_KEY else _form_by_key(conn, form_type)
        if form_type != AUTO_FORM_KEY and not form:
            return _error('선택한 명세서 폼을 찾을 수 없습니다.')
        body_html = _safe_body_html(data.get('body_html', current['body_html']), allow_empty=True)
        banner1 = _banner_value(data.get('banner1')) if 'banner1' in data else (None if 'banner1_asset_id' in data else current['banner1_data'])
        banner2 = _banner_value(data.get('banner2')) if 'banner2' in data else (None if 'banner2_asset_id' in data else current['banner2_data'])
        owner = _owner_emp_no()
        banner1_asset_id = _selected_asset_id(conn, owner, data.get('banner1_asset_id', current['banner1_asset_id']), 'banner', '광고 배너 1')
        banner2_asset_id = _selected_asset_id(conn, owner, data.get('banner2_asset_id', current['banner2_asset_id']), 'banner', '광고 배너 2')
        logo_asset_id = _selected_asset_id(conn, owner, data.get('logo_asset_id', current['logo_asset_id']), 'logo', '회사 로고')
        if banner1_asset_id and banner1_asset_id == banner2_asset_id:
            return _error('광고 배너 1과 2는 서로 다른 이미지를 선택해주세요.')
        conn.execute('''
            UPDATE payroll_workgroups SET name=?, form_type=?, subject=?, body_html=?, banner1_data=?, banner2_data=?,
                banner1_asset_id=?, banner2_asset_id=?, logo_asset_id=?, memo=?, template_id=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND owner_emp_no=?
        ''', (name, form_type, subject, body_html, banner1, banner2, banner1_asset_id, banner2_asset_id, logo_asset_id, _clean_text(data.get('memo', current['memo']), 1000), data.get('template_id') or None, group_id, owner))
        conn.commit()
        saved = dict(_owned(conn, 'payroll_workgroups', group_id))
        saved['form_name'] = AUTO_FORM_NAME if form_type == AUTO_FORM_KEY else form['name']
        _, assets_by_id = _asset_map(conn, owner)
        return _ok('작업그룹이 수정되었습니다.', group=_group_dict(saved, assets_by_id))
    except sqlite3.IntegrityError:
        conn.rollback()
        return _error('같은 이름의 작업그룹이 이미 있습니다.', 409)
    except ValueError as exc:
        conn.rollback()
        return _error(str(exc))
    finally:
        conn.close()


@payroll_bp.route('/api/groups/<int:group_id>', methods=['DELETE'])
@_mutating
def delete_group(group_id):
    conn = _db()
    try:
        if not _owned(conn, 'payroll_workgroups', group_id):
            return _error('작업그룹을 찾을 수 없습니다.', 404)
        conn.execute('DELETE FROM payroll_workgroups WHERE id=? AND owner_emp_no=?', (group_id, _owner_emp_no()))
        conn.commit()
        return _ok('작업그룹이 삭제되었습니다.')
    finally:
        conn.close()


@payroll_bp.route('/api/assets', methods=['POST'])
@_mutating
def create_asset():
    data = _json_data()
    kind = _clean_text(data.get('asset_kind'), 20)
    name = _clean_text(data.get('name'), 120)
    if kind not in ASSET_LIMITS or not name:
        return _error('이미지 종류와 이름을 확인해주세요.')
    try:
        source_value = _image_value(data.get('source_value'), '광고 이미지' if kind == 'banner' else '회사 로고')
    except ValueError as exc:
        return _error(str(exc))
    if not source_value:
        return _error('이미지 파일 또는 웹링크를 등록해주세요.')
    source_type = 'url' if urlparse(source_value).scheme in ('http', 'https') else 'file'
    conn = _db()
    try:
        owner = _owner_emp_no()
        count = int(conn.execute('''
            SELECT COUNT(*) FROM payroll_image_assets
            WHERE owner_emp_no=? AND asset_kind=?
        ''', (owner, kind)).fetchone()[0])
        if count >= ASSET_LIMITS[kind]:
            label = '광고 이미지' if kind == 'banner' else '회사 로고'
            return _error(f'{label}는 최대 {ASSET_LIMITS[kind]}개까지 등록할 수 있습니다.', 409)
        cursor = conn.execute('''
            INSERT INTO payroll_image_assets (owner_emp_no, asset_kind, name, source_type, source_value)
            VALUES (?, ?, ?, ?, ?)
        ''', (owner, kind, name, source_type, source_value))
        conn.commit()
        asset = _owned(conn, 'payroll_image_assets', cursor.lastrowid)
        return _ok('이미지 보관함에 등록했습니다.', asset=_asset_dict(asset))
    except sqlite3.IntegrityError:
        conn.rollback()
        return _error('같은 종류에 동일한 이름의 이미지가 이미 있습니다.', 409)
    finally:
        conn.close()


@payroll_bp.route('/api/assets/<int:asset_id>', methods=['PATCH', 'PUT'])
@_mutating
def update_asset(asset_id):
    data = _json_data()
    conn = _db()
    try:
        current = _owned(conn, 'payroll_image_assets', asset_id)
        if not current:
            return _error('이미지를 찾을 수 없습니다.', 404)
        name = _clean_text(data.get('name', current['name']), 120)
        if not name:
            return _error('이미지 이름을 입력해주세요.')
        source_value = current['source_value']
        if data.get('source_value'):
            source_value = _image_value(
                data.get('source_value'),
                '광고 이미지' if current['asset_kind'] == 'banner' else '회사 로고',
            )
        source_type = 'url' if urlparse(source_value).scheme in ('http', 'https') else 'file'
        conn.execute('''
            UPDATE payroll_image_assets
            SET name=?, source_type=?, source_value=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND owner_emp_no=?
        ''', (name, source_type, source_value, asset_id, _owner_emp_no()))
        conn.commit()
        return _ok('보관 이미지 정보를 수정했습니다.', asset=_asset_dict(_owned(conn, 'payroll_image_assets', asset_id)))
    except sqlite3.IntegrityError:
        conn.rollback()
        return _error('같은 종류에 동일한 이름의 이미지가 이미 있습니다.', 409)
    except ValueError as exc:
        conn.rollback()
        return _error(str(exc))
    finally:
        conn.close()


@payroll_bp.route('/api/assets/<int:asset_id>', methods=['DELETE'])
@_mutating
def delete_asset(asset_id):
    conn = _db()
    try:
        asset = _owned(conn, 'payroll_image_assets', asset_id)
        if not asset:
            return _error('이미지를 찾을 수 없습니다.', 404)
        used_count = int(conn.execute('''
            SELECT COUNT(*) FROM payroll_workgroups
            WHERE owner_emp_no=? AND (
                banner1_asset_id=? OR banner2_asset_id=? OR logo_asset_id=?
            )
        ''', (_owner_emp_no(), asset_id, asset_id, asset_id)).fetchone()[0])
        if used_count:
            return _error(f'이 이미지를 사용하는 작업그룹이 {used_count}개 있습니다. 작업그룹 선택을 먼저 변경해주세요.', 409)
        conn.execute('DELETE FROM payroll_image_assets WHERE id=? AND owner_emp_no=?', (asset_id, _owner_emp_no()))
        conn.commit()
        return _ok('보관함에서 이미지를 삭제했습니다.')
    finally:
        conn.close()


@payroll_bp.route('/api/assets/<int:asset_id>/content')
@_login_required
def asset_content(asset_id):
    conn = _db()
    try:
        asset = _owned(conn, 'payroll_image_assets', asset_id)
        if not asset:
            return _error('이미지를 찾을 수 없습니다.', 404)
        match = DATA_IMAGE_RE.fullmatch(asset['source_value'] or '')
        if not match:
            return _error('등록된 파일 이미지를 읽을 수 없습니다.', 404)
        payload = base64.b64decode(re.sub(r'\s+', '', match.group(2)))
        response = current_app.response_class(payload, mimetype=f'image/{match.group(1).lower()}')
        response.headers['Cache-Control'] = 'private, max-age=3600'
        return response
    finally:
        conn.close()


@payroll_bp.route('/api/senders', methods=['POST'])
@_mutating
def create_sender():
    data = _json_data()
    email = _clean_text(data.get('email'), 254).lower()
    label = _clean_text(data.get('label'), 120) or email
    if not _is_valid_email(email):
        return _error('구글 계정 메일주소 형식이 올바르지 않습니다.')
    try:
        encrypted = _encrypt_password(data.get('app_password'))
    except (ValueError, RuntimeError) as exc:
        return _error(str(exc))
    conn = _db()
    try:
        cursor = conn.execute('INSERT INTO ai_mail_senders (owner_emp_no, label, email, encrypted_app_password, is_active) VALUES (?, ?, ?, ?, 1)', (_owner_emp_no(), label, email, encrypted))
        conn.commit()
        return _ok('메일 발송 계정이 등록되었습니다.', sender=_sender_dict(_owned(conn, 'ai_mail_senders', cursor.lastrowid)))
    except sqlite3.IntegrityError:
        conn.rollback()
        return _error('같은 구글 계정이 이미 등록되어 있습니다.', 409)
    finally:
        conn.close()


@payroll_bp.route('/api/senders/<int:sender_id>', methods=['PATCH', 'PUT'])
@_mutating
def update_sender(sender_id):
    data = _json_data()
    conn = _db()
    try:
        current = _owned(conn, 'ai_mail_senders', sender_id)
        if not current:
            return _error('발송계정을 찾을 수 없습니다.', 404)
        email = _clean_text(data.get('email', current['email']), 254).lower()
        label = _clean_text(data.get('label', current['label']), 120) or email
        if not _is_valid_email(email):
            return _error('구글 계정 메일주소 형식이 올바르지 않습니다.')
        encrypted = current['encrypted_app_password']
        if data.get('app_password'):
            encrypted = _encrypt_password(data['app_password'])
        conn.execute('''UPDATE ai_mail_senders SET label=?, email=?, encrypted_app_password=?, last_test_status=NULL, last_test_error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=? AND owner_emp_no=?''', (label, email, encrypted, sender_id, _owner_emp_no()))
        conn.commit()
        return _ok('발송계정이 수정되었습니다.', sender=_sender_dict(_owned(conn, 'ai_mail_senders', sender_id)))
    except sqlite3.IntegrityError:
        conn.rollback()
        return _error('같은 구글 계정이 이미 등록되어 있습니다.', 409)
    except (ValueError, RuntimeError) as exc:
        conn.rollback()
        return _error(str(exc))
    finally:
        conn.close()


@payroll_bp.route('/api/senders/<int:sender_id>/test', methods=['POST'])
@_mutating
def test_sender(sender_id):
    conn = _db()
    try:
        sender = _owned(conn, 'ai_mail_senders', sender_id)
        if not sender:
            return _error('발송계정을 찾을 수 없습니다.', 404)
        try:
            smtp = _smtp_login(sender['email'], _decrypt_password(sender['encrypted_app_password']))
            smtp.quit()
            conn.execute("UPDATE ai_mail_senders SET last_tested_at=CURRENT_TIMESTAMP, last_test_status='success', last_test_error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?", (sender_id,))
            conn.commit()
            return _ok('구글 SMTP 인증에 성공했습니다.', sender=_sender_dict(_owned(conn, 'ai_mail_senders', sender_id)))
        except Exception as exc:
            code, friendly, _, detail, _ = _smtp_error_info(exc)
            message = f'{friendly} ({detail})' if detail else friendly
            conn.execute("UPDATE ai_mail_senders SET last_tested_at=CURRENT_TIMESTAMP, last_test_status='error', last_test_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (_clean_text(message, 1000), sender_id))
            conn.commit()
            return _error(message, 400, code=code, sender=_sender_dict(_owned(conn, 'ai_mail_senders', sender_id)))
    finally:
        conn.close()


@payroll_bp.route('/api/templates', methods=['POST'])
@_mutating
def create_template():
    data = _json_data()
    name = _clean_text(data.get('name'), 120)
    description = _clean_text(data.get('description'), 500) or '사용자 추가 명세서 폼'
    match_keywords = _clean_match_keywords(data.get('match_keywords'))
    if not name:
        return _error('명세서 폼 이름을 입력해주세요.')
    try:
        body_html = _safe_form_source(data.get('body_html'))
    except ValueError as exc:
        return _error(str(exc))
    conn = _db()
    try:
        template_key = f'custom_{uuid.uuid4().hex[:12]}'
        cursor = conn.execute('''
            INSERT INTO payroll_mail_templates (
                owner_emp_no, template_key, name, subject, description,
                source_filename, match_keywords, body_html, is_system, is_active
            ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, 0, 1)
        ''', (_owner_emp_no(), template_key, name, description, description, match_keywords, body_html))
        conn.commit()
        return _ok('명세서 HTML 폼이 저장되었습니다.', template=_template_dict(_owned(conn, 'payroll_mail_templates', cursor.lastrowid)))
    except sqlite3.IntegrityError:
        conn.rollback()
        return _error('같은 이름의 명세서 폼이 이미 있습니다.', 409)
    finally:
        conn.close()


@payroll_bp.route('/api/templates/<int:template_id>', methods=['PATCH', 'PUT'])
@_mutating
def update_template(template_id):
    data = _json_data()
    conn = _db()
    try:
        current = _owned(conn, 'payroll_mail_templates', template_id)
        if not current:
            return _error('명세서 HTML 폼을 찾을 수 없습니다.', 404)
        name = _clean_text(data.get('name', current['name']), 120)
        description = _clean_text(data.get('description', current['description'] or current['subject']), 500)
        match_keywords = _clean_match_keywords(data.get('match_keywords', current['match_keywords']))
        body_html = _safe_form_source(data.get('body_html', current['body_html']))
        conn.execute('''
            UPDATE payroll_mail_templates
            SET name=?, subject=?, description=?, match_keywords=?, body_html=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND owner_emp_no=?
        ''', (name, description, description, match_keywords, body_html, template_id, _owner_emp_no()))
        conn.commit()
        return _ok('명세서 HTML 폼이 수정되었습니다.', template=_template_dict(_owned(conn, 'payroll_mail_templates', template_id)))
    except sqlite3.IntegrityError:
        conn.rollback()
        return _error('같은 이름의 명세서 폼이 이미 있습니다.', 409)
    except ValueError as exc:
        conn.rollback()
        return _error(str(exc))
    finally:
        conn.close()


@payroll_bp.route('/api/templates/<int:template_id>', methods=['DELETE'])
@_mutating
def delete_template(template_id):
    conn = _db()
    try:
        template = _owned(conn, 'payroll_mail_templates', template_id)
        if not template:
            return _error('명세서 HTML 폼을 찾을 수 없습니다.', 404)
        if template['is_system']:
            return _error('기본 제공 폼은 삭제할 수 없습니다. 수정하거나 원본 복원 기능을 사용해주세요.', 409)
        used_count = conn.execute('''
            SELECT COUNT(*) FROM payroll_workgroups
            WHERE owner_emp_no=? AND form_type=?
        ''', (_owner_emp_no(), template['template_key'])).fetchone()[0]
        if used_count:
            return _error('이 폼을 사용하는 작업그룹이 있습니다. 작업그룹의 폼을 먼저 변경해주세요.', 409)
        conn.execute('UPDATE payroll_mail_templates SET is_active=0, updated_at=CURRENT_TIMESTAMP WHERE id=? AND owner_emp_no=?', (template_id, _owner_emp_no()))
        conn.commit()
        return _ok('명세서 HTML 폼이 삭제되었습니다.')
    finally:
        conn.close()


@payroll_bp.route('/api/templates/preview', methods=['POST'])
@_mutating
def preview_template():
    data = _json_data()
    conn = _db()
    try:
        source = _safe_form_source(data.get('body_html'))
        owner = _owner_emp_no()
        banner1_id = _selected_asset_id(conn, owner, data.get('banner1_asset_id'), 'banner', '광고 배너 1')
        banner2_id = _selected_asset_id(conn, owner, data.get('banner2_asset_id'), 'banner', '광고 배너 2')
        logo_id = _selected_asset_id(conn, owner, data.get('logo_asset_id'), 'logo', '회사 로고')
        selected = {}
        for key, asset_id in (('ad1_url', banner1_id), ('ad2_url', banner2_id), ('logo_url', logo_id)):
            if asset_id:
                selected[key] = conn.execute('''
                    SELECT source_value FROM payroll_image_assets WHERE id=? AND owner_emp_no=?
                ''', (asset_id, owner)).fetchone()['source_value']
        rendered = _render_form_source(
            source,
            send_date=_clean_text(data.get('send_date'), 20) or '2026-07-25',
            logo_url=selected.get('logo_url') or request.host_url.rstrip('/') + '/static/logo01.jpg',
            ad1_url=selected.get('ad1_url') or TRANSPARENT_PIXEL,
            ad2_url=selected.get('ad2_url') or TRANSPARENT_PIXEL,
        )
        return _ok('명세서 폼 미리보기를 만들었습니다.', rendered_html=rendered)
    except ValueError as exc:
        return _error(str(exc))
    finally:
        conn.close()


@payroll_bp.route('/api/templates/<int:template_id>/reset', methods=['POST'])
@_mutating
def reset_template(template_id):
    conn = _db()
    try:
        template = _owned(conn, 'payroll_mail_templates', template_id)
        if not template:
            return _error('명세서 HTML 폼을 찾을 수 없습니다.', 404)
        form = DEFAULT_FORMS.get(template['template_key'])
        if not template['is_system'] or not form:
            return _error('기본 제공 폼만 원본으로 복원할 수 있습니다.', 409)
        source_path = os.path.join(current_app.root_path, 'templates', *form['filename'].split('/'))
        with open(source_path, 'r', encoding='utf-8') as source_file:
            source = source_file.read()
        _safe_form_source(source)
        conn.execute('''
            UPDATE payroll_mail_templates
            SET name=?, subject=?, description=?, match_keywords=?, body_html=?, source_filename=?,
                is_active=1, updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND owner_emp_no=?
        ''', (form['name'], form['description'], form['description'], form['match_keywords'], source, form['filename'], template_id, _owner_emp_no()))
        conn.commit()
        return _ok('기본 HTML 파일 내용으로 복원했습니다.', template=_template_dict(_owned(conn, 'payroll_mail_templates', template_id)))
    finally:
        conn.close()


def _resolve_forms(conn, group, frame):
    errors = []
    forms = {}
    if group['form_type'] == AUTO_FORM_KEY:
        templates = conn.execute('''
            SELECT * FROM payroll_mail_templates
            WHERE owner_emp_no=? AND is_active=1
              AND match_keywords IS NOT NULL AND TRIM(match_keywords)<>''
            ORDER BY is_system DESC, id ASC
        ''', (_owner_emp_no(),)).fetchall()
        templates_by_key = {template['template_key']: template for template in templates}
        resolution_errors = {}
        for index, row in frame.iterrows():
            detected_type = str(row.get(EXCEL_META_TYPE, '')).strip()
            form_key, resolution_error = _match_form_for_type(detected_type, templates)
            frame.at[index, EXCEL_META_FORM] = form_key
            if resolution_error:
                resolution_errors[detected_type or '비어 있음'] = resolution_error
        errors.extend(resolution_errors.values())
        form_keys = sorted({str(value).strip() for value in frame[EXCEL_META_FORM] if str(value).strip()})
        for form_key in form_keys:
            form = templates_by_key.get(form_key)
            if form:
                forms[form_key] = form
            else:
                errors.append(f'적용할 명세서 폼을 찾을 수 없습니다: {form_key}')
    else:
        form_keys = [group['form_type']]
        if len(frame):
            frame[EXCEL_META_FORM] = group['form_type']
        for form_key in form_keys:
            form = _form_by_key(conn, form_key)
            if form:
                forms[form_key] = form
            else:
                errors.append(f'적용할 명세서 폼을 찾을 수 없습니다: {form_key}')
    return forms, errors


def _preflight(group, sender, frame, forms, form_errors=None):
    form_names = {key: form['name'] for key, form in forms.items()}
    rows, errors = _inspect_rows(frame, form_names)
    errors.extend(form_errors or [])
    warnings = []
    infos = []
    if sender['last_test_status'] != 'success':
        errors.insert(0, '선택한 발송계정의 연결 테스트를 먼저 완료해주세요.')
    if not group['subject']:
        errors.append('메일 제목이 없습니다.')
    if not forms:
        errors.append('지정된 명세서 폼을 사용할 수 없습니다.')
    elif len(frame):
        for form_key, form in forms.items():
            matching = frame[frame[EXCEL_META_FORM] == form_key]
            sample_row = matching.iloc[0] if len(matching) else frame.iloc[0]
            try:
                _render_form_source(
                    form['body_html'],
                    row=dict(sample_row),
                    logo_url=group.get('logo_value') or 'https://example.com/logo.jpg',
                    ad1_url=group.get('banner1_value') or TRANSPARENT_PIXEL,
                    ad2_url=group.get('banner2_value') or TRANSPARENT_PIXEL,
                )
            except TemplateError as exc:
                errors.append(f'{form["name"]} 렌더링 오류: {exc}')
    valid_count = sum(1 for row in rows if row['status'] == 'ready')
    file_count = int(frame.attrs.get('file_count', 0))
    sheet_count = int(frame.attrs.get('sheet_count', 0))
    skipped_count = int(frame.attrs.get('skipped_count', 0))
    if sheet_count:
        infos.append(f'엑셀 {file_count}개, 전체 {sheet_count}개 시트에서 발송 대상 {len(rows)}건을 확인했습니다.')
    if group['form_type'] == AUTO_FORM_KEY and forms:
        counts = {}
        for _, row in frame.iterrows():
            key = str(row.get(EXCEL_META_FORM, '')).strip()
            if key in form_names:
                counts[key] = counts.get(key, 0) + 1
        infos.append('자동적용 결과: ' + ', '.join(f'{form_names[key]} {count}건' for key, count in counts.items()))
    if skipped_count:
        infos.append(f'은행·계좌번호·예금주 값이 모두 갖춰지지 않은 {skipped_count}개 행은 정상적으로 발송 대상에서 제외했습니다.')
    if len(rows) > 500:
        warnings.append('한 번에 500명을 초과하면 발송 시간이 오래 걸릴 수 있습니다.')
    return {
        'ok': not errors and valid_count > 0,
        'total': len(rows),
        'ready': valid_count,
        'error_count': len(errors),
        'warning_count': len(warnings),
        'skipped_count': skipped_count,
        'errors': errors,
        'warnings': warnings,
        'infos': infos,
        'rows': rows[:100],
    }


@payroll_bp.route('/api/preflight', methods=['POST'])
@_mutating
def preflight():
    conn = _db()
    try:
        group_row = _owned(conn, 'payroll_workgroups', request.form.get('group_id', type=int))
        sender = _owned(conn, 'ai_mail_senders', request.form.get('sender_id', type=int))
        if not group_row or not sender:
            return _error('작업그룹과 발송계정을 다시 선택해주세요.')
        group = _hydrate_group_assets(conn, group_row)
        try:
            frame = _load_excels(_uploaded_excel_files())
        except ValueError as exc:
            return _error(str(exc))
        forms, form_errors = _resolve_forms(conn, group, frame)
        report = _preflight(group, sender, frame, forms, form_errors)
        if report['ok']:
            return _ok('발송 전 점검을 통과했습니다.', preflight=report)
        return _error('발송 전 점검에서 오류가 발견되었습니다.', 400, preflight=report)
    finally:
        conn.close()


@payroll_bp.route('/send', methods=['POST'])
@_mutating
def start_send():
    owner = _owner_emp_no()
    status = _status_for(owner)
    if status.get('is_running'):
        return _error('이미 다른 명세서 발송 작업이 진행 중입니다.', 409)
    conn = _db()
    try:
        group_row = _owned(conn, 'payroll_workgroups', request.form.get('group_id', type=int))
        sender_row = _owned(conn, 'ai_mail_senders', request.form.get('sender_id', type=int))
        if not group_row or not sender_row:
            return _error('작업그룹과 발송계정을 다시 선택해주세요.')
        try:
            excel_files = _uploaded_excel_files()
            frame = _load_excels(excel_files)
            group = _hydrate_group_assets(conn, group_row)
            forms, form_errors = _resolve_forms(conn, group, frame)
            report = _preflight(group, sender_row, frame, forms, form_errors)
        except ValueError as exc:
            return _error(str(exc))
        if not report['ok']:
            return _error('사전점검 오류를 먼저 해결해주세요.', 400, preflight=report)
        send_date = _clean_text(request.form.get('send_date'), 20)
        if not send_date:
            return _error('지급 기준일을 선택해주세요.')
        interval = max(0.5, min(float(request.form.get('interval', 2)), 60.0))
        cursor = conn.execute('''
            INSERT INTO payroll_campaigns (owner_emp_no, group_id, group_name, sender_id, sender_email, subject, source_filename, status, total_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?)
        ''', (
            owner, group_row['id'], group_row['name'], sender_row['id'], sender_row['email'],
            group_row['subject'], ', '.join(frame.attrs.get('file_names', [])), len(frame),
        ))
        campaign_id = cursor.lastrowid
        recipient_ids = _create_campaign_recipients(conn, owner, campaign_id, frame)
        conn.commit()
        status.clear()
        status.update({
            'campaign_id': campaign_id,
            'campaign_status': 'queued',
            'is_running': True,
            'stop_requested': False,
            'total_count': len(frame),
            'sent_count': 0,
            'failed_count': 0,
            'processed_count': 0,
            'recent_completed': [],
            'current_recipient': {},
            'upcoming_scheduled': [],
            'sent_names': [],
            'errors': [],
        })
        app = current_app._get_current_object()
        base_url = request.host_url.rstrip('/').replace('http://', 'https://') if 'localhost' not in request.host_url and '127.0.0.1' not in request.host_url else request.host_url.rstrip('/')
        group_data = group
        group_data['form_sources'] = {key: form['body_html'] for key, form in forms.items()}
        if group_row['form_type'] != AUTO_FORM_KEY and group_row['form_type'] in forms:
            group_data['form_source'] = forms[group_row['form_type']]['body_html']
        threading.Thread(target=payroll_worker, args=(app, owner, campaign_id, frame, recipient_ids, send_date, interval, base_url, group_data, dict(sender_row)), daemon=True).start()
        return _ok('명세서 발송을 시작했습니다.', campaign_id=campaign_id)
    finally:
        conn.close()


@payroll_bp.route('/stop', methods=['POST'])
@_mutating
def stop_send():
    status = _status_for(_owner_emp_no())
    status['stop_requested'] = True
    return _ok('발송 중단을 요청했습니다.')


@payroll_bp.route('/status')
@_login_required
def get_status():
    return jsonify(_status_for(_owner_emp_no()))


@payroll_bp.route('/api/history')
@_login_required
def history():
    conn = _db()
    try:
        result = _history_page(conn, _owner_emp_no(), request.args.get('page', 1, type=int), 10)
        return _ok(
            history=result['items'],
            pagination={key: value for key, value in result.items() if key != 'items'},
        )
    finally:
        conn.close()


@payroll_bp.route('/api/history/<int:campaign_id>')
@_login_required
def history_detail(campaign_id):
    conn = _db()
    try:
        campaign_row = _owned(conn, 'payroll_campaigns', campaign_id)
        if not campaign_row:
            return _error('발송이력을 찾을 수 없습니다.', 404)
        campaign = _history_campaign_dict(campaign_row)
        recipient_rows = conn.execute('''
            SELECT * FROM payroll_campaign_recipients
            WHERE campaign_id=? AND owner_emp_no=?
            ORDER BY id ASC
        ''', (campaign_id, _owner_emp_no())).fetchall()
        recipients = [dict(row) for row in recipient_rows]
        counts = {'sent': 0, 'failed': 0, 'cancelled': 0, 'waiting': 0}
        for recipient in recipients:
            recipient['elapsed_seconds'] = round(float(recipient.get('elapsed_seconds') or 0), 3)
            status_name = recipient.get('status')
            if status_name in counts:
                counts[status_name] += 1
            elif status_name in ('queued', 'running'):
                counts['waiting'] += 1
        campaign['recipient_counts'] = counts
        campaign['has_recipient_details'] = bool(recipients)
        return _ok(campaign=campaign, recipients=recipients)
    finally:
        conn.close()