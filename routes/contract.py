from flask import Blueprint, render_template, request, jsonify, redirect, url_for, session, send_file, abort
import pandas as pd
import os
import sys
import zipfile
import io
import pdfkit
import yagmail
import json
import sqlite3
import re
import shutil
import base64
import mimetypes
from datetime import datetime, timedelta, timezone
from hashids import Hashids
from html import escape

# PDF 페이지 분할을 위한 라이브러리 (서버에 pip install PyPDF2 필요)
try:
    from PyPDF2 import PdfReader, PdfWriter
except ImportError:
    PdfReader, PdfWriter = None, None

# 배포 환경에서 routes 패키지를 찾지 못하는 에러 방지
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Blueprint 생성 (인트라넷 통합용)
contract_bp = Blueprint('contract', __name__)

hashids = Hashids(salt="saedam_secret_salt", min_length=8)

# --- [저장 경로 설정: 인트라넷 구조에 맞춤] ---
if os.path.exists('/mnt/data'):
    MOUNT_PATH = '/mnt/data'
else:
    MOUNT_PATH = os.getcwd()

# 기존 엑셀 대신 SQLite 파이썬 DB 사용
DB_FILE = os.path.join(MOUNT_PATH, 'contracts.db')
CONTRACTS_DIR = os.path.join(MOUNT_PATH, 'contracts')
TERMS_DIR = os.path.join(MOUNT_PATH, 'terms') # 수정: 서버 렌더 환경에 맞추어 MOUNT_PATH로 이동
CATEGORIES_FILE = os.path.join(MOUNT_PATH, 'categories.json')
COMPANY_SETTINGS_FILE = os.path.join(MOUNT_PATH, 'company_settings.json')
COMPANY_STAMP_DIR = os.path.join(MOUNT_PATH, 'company_stamps')

if not os.path.exists(CONTRACTS_DIR):
    os.makedirs(CONTRACTS_DIR)
if not os.path.exists(TERMS_DIR):
    os.makedirs(TERMS_DIR)
if not os.path.exists(COMPANY_STAMP_DIR):
    os.makedirs(COMPANY_STAMP_DIR)

# [설정] wkhtmltopdf 경로 설정 (배포 환경 대응)
# [설정] wkhtmltopdf 경로 설정 (로컬 Windows + Render/Linux 동시 대응)
def find_wkhtmltopdf():
    # 1) Render 환경변수로 직접 지정한 경우
    env_path = os.environ.get('WKHTMLTOPDF_PATH')
    if env_path and os.path.exists(env_path):
        return env_path

    # 2) Render/Linux 기본 경로 후보
    linux_candidates = [
        '/usr/bin/wkhtmltopdf',
        '/usr/local/bin/wkhtmltopdf',
        '/opt/render/project/src/.apt/usr/bin/wkhtmltopdf'
    ]
    for path in linux_candidates:
        if os.path.exists(path):
            return path

    # 3) Windows 로컬 기본 설치 경로 후보
    windows_candidates = [
        r'C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe',
        r'C:\Program Files (x86)\wkhtmltopdf\bin\wkhtmltopdf.exe'
    ]
    for path in windows_candidates:
        if os.path.exists(path):
            return path

    # 4) PATH에 잡혀 있는 경우
    found = shutil.which('wkhtmltopdf')
    if found:
        return found

    return None


WKHTMLTOPDF_PATH = find_wkhtmltopdf()

try:
    if WKHTMLTOPDF_PATH:
        PDF_CONFIG = pdfkit.configuration(wkhtmltopdf=WKHTMLTOPDF_PATH)
    else:
        PDF_CONFIG = None
except Exception as e:
    print("PDF_CONFIG 생성 실패:", e)
    PDF_CONFIG = None
SENDER_EMAIL = os.environ.get('MAIL_USERNAME')
SENDER_PASSWORD = os.environ.get('MAIL_PASSWORD')
ADMIN_PASSWORD = 'school97$$'
KST = timezone(timedelta(hours=9))

# 계약구분 동적 관리 함수
DEFAULT_CATEGORIES = ['방과후강사', '맞춤형강사', '코디사업자', '코디근로자', '안전코디', '직원근로자', '직원사업자', '원어민근로자', '원어민사업자']

def load_categories():
    if os.path.exists(CATEGORIES_FILE):
        try:
            with open(CATEGORIES_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return DEFAULT_CATEGORIES.copy()

def save_categories(cats):
    with open(CATEGORIES_FILE, 'w', encoding='utf-8') as f:
        json.dump(cats, f, ensure_ascii=False)


# --- [회사 정보/도장 설정: /mnt/data 지속 저장] ---
CONTRACT_TITLES_FILE = os.path.join(MOUNT_PATH, 'contract_titles.json')
MAX_COMPANY_PROFILES = 3


def _make_profile_id():
    return datetime.now(KST).strftime('%Y%m%d%H%M%S%f')


def normalize_company_settings(settings):
    """기존 단일 회사설정 JSON도 3세트 프로필 구조로 자동 변환한다."""
    default_profile = {
        "id": "default",
        "label": "기본 회사",
        "company_name": "(사)새담청소년교육문화원",
        "representative_title": "이사장",
        "representative_name": "",
        "stamp_filename": ""
    }

    if not isinstance(settings, dict):
        return {"active_profile_id": "default", "profiles": [default_profile]}

    # 새 구조
    if isinstance(settings.get("profiles"), list):
        profiles = []
        for i, profile in enumerate(settings.get("profiles", [])):
            if not isinstance(profile, dict):
                continue
            merged = default_profile.copy()
            merged.update(profile)
            if not str(merged.get("id", "")).strip():
                merged["id"] = _make_profile_id()
            if not str(merged.get("label", "")).strip():
                merged["label"] = f"회사 {i + 1}"
            profiles.append(merged)

        if not profiles:
            profiles = [default_profile]

        profiles = profiles[:MAX_COMPANY_PROFILES]
        active_profile_id = str(settings.get("active_profile_id", "")).strip()
        if not any(p.get("id") == active_profile_id for p in profiles):
            active_profile_id = profiles[0].get("id", "default")

        return {"active_profile_id": active_profile_id, "profiles": profiles}

    # 구버전 단일 구조 자동 변환
    migrated = default_profile.copy()
    migrated.update({
        "id": "default",
        "label": str(settings.get("label", "기본 회사")).strip() or "기본 회사",
        "company_name": str(settings.get("company_name", default_profile["company_name"])).strip() or default_profile["company_name"],
        "representative_title": str(settings.get("representative_title", default_profile["representative_title"])).strip() or default_profile["representative_title"],
        "representative_name": str(settings.get("representative_name", "")).strip(),
        "stamp_filename": str(settings.get("stamp_filename", "")).strip()
    })
    return {"active_profile_id": migrated["id"], "profiles": [migrated]}


def load_company_settings():
    """계약서 PDF 위탁자 영역에 찍힐 회사 프로필 3세트를 불러온다."""
    settings = None
    if os.path.exists(COMPANY_SETTINGS_FILE):
        try:
            with open(COMPANY_SETTINGS_FILE, 'r', encoding='utf-8') as f:
                settings = json.load(f)
        except Exception as e:
            print("회사 정보 설정 불러오기 실패:", e)

    normalized = normalize_company_settings(settings)

    # 구버전 파일이면 새 구조로 한 번 저장해서 이후 관리가 편하게 한다.
    try:
        save_company_settings(normalized)
    except Exception:
        pass

    return normalized


def save_company_settings(settings):
    """회사 정보 설정을 /mnt/data/company_settings.json 에 저장한다."""
    normalized = normalize_company_settings(settings)
    with open(COMPANY_SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)


def get_active_company_profile(settings=None):
    settings = normalize_company_settings(settings or load_company_settings())
    active_id = settings.get("active_profile_id")
    for profile in settings.get("profiles", []):
        if profile.get("id") == active_id:
            return profile
    return settings.get("profiles", [])[0]


def get_company_stamp_abs_path(profile):
    """등록된 도장 파일 경로를 반환하고, 없으면 기존 static/stamp7.png로 fallback한다."""
    stamp_filename = str(profile.get("stamp_filename", "")).strip()
    if stamp_filename:
        candidate = os.path.abspath(os.path.join(COMPANY_STAMP_DIR, os.path.basename(stamp_filename)))
        if os.path.exists(candidate):
            return candidate

    return os.path.abspath(os.path.join(os.getcwd(), 'static', 'stamp7.png'))


def get_company_stamp_src(profile):
    """wkhtmltopdf에서 도장 이미지가 누락되지 않도록 파일을 base64 data URI로 변환한다."""
    stamp_path = get_company_stamp_abs_path(profile)
    if not stamp_path or not os.path.exists(stamp_path):
        return ""

    mime_type, _ = mimetypes.guess_type(stamp_path)
    if not mime_type:
        mime_type = "image/png"

    try:
        with open(stamp_path, 'rb') as f:
            encoded = base64.b64encode(f.read()).decode('ascii')
        return f"data:{mime_type};base64,{encoded}"
    except Exception as e:
        print("도장 이미지 base64 변환 실패:", e)
        return ""


def get_company_context(profile_id=None):
    """회사 설정값을 원본/HTML escape 버전으로 함께 반환한다."""
    settings = load_company_settings()

    profile = None
    if profile_id:
        for p in settings.get("profiles", []):
            if p.get("id") == profile_id:
                profile = p
                break
    if profile is None:
        profile = get_active_company_profile(settings)

    raw_company_name = str(profile.get("company_name", "")).strip() or "(사)새담청소년교육문화원"
    raw_representative_title = str(profile.get("representative_title", "")).strip() or "이사장"
    raw_representative_name = str(profile.get("representative_name", "")).strip()

    return {
        "settings": settings,
        "profile": profile,
        "profile_id": profile.get("id"),
        "company_name_raw": raw_company_name,
        "representative_title_raw": raw_representative_title,
        "representative_name_raw": raw_representative_name,
        "company_name": escape(raw_company_name),
        "representative_title": escape(raw_representative_title),
        "representative_name": escape(raw_representative_name),
        "company_owner_text": " ".join([escape(v) for v in [raw_company_name, raw_representative_title, raw_representative_name] if v]),
        "stamp_src": get_company_stamp_src(profile)
    }


def default_company_title(contract_type, company_name):
    title_map = {
        "방과후강사": f"{company_name} 위탁교육계약서",
        "맞춤형강사": f"{company_name} 위탁교육계약서",
        "코디근로자": f"{company_name} 센터장 계약서",
        "코디사업자": f"{company_name} 센터장 계약서",
        "원어민근로자": "방과후 영어 원어민 강사 위탁 계약서",
        "원어민사업자": "방과후 영어 원어민 강사 위탁 계약서",
        "안전코디": f"{company_name} 위수탁계약서",
        "직원근로자": f"{company_name} 근로계약서",
        "직원사업자": f"{company_name} 위탁업무계약서"
    }
    return title_map.get(contract_type, f"{company_name} 계약서 ({contract_type})")


def load_contract_titles():
    if os.path.exists(CONTRACT_TITLES_FILE):
        try:
            with open(CONTRACT_TITLES_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception as e:
            print("계약서 제목 설정 불러오기 실패:", e)
    return {}


def save_contract_titles(titles):
    with open(CONTRACT_TITLES_FILE, 'w', encoding='utf-8') as f:
        json.dump(titles, f, ensure_ascii=False, indent=2)


def make_company_title(contract_type, company_name):
    """계약구분별 계약서 제목. 저장된 제목이 있으면 회사명 치환자를 반영한다."""
    saved_title = str(load_contract_titles().get(contract_type, "")).strip()

    if saved_title:
        result = saved_title

        # 띄어쓰기 있는 형태 / 없는 형태 모두 처리
        result = result.replace("{{ data.회사명 }}", company_name)
        result = result.replace("{{data.회사명}}", company_name)

        result = result.replace("{{ company.name }}", company_name)
        result = result.replace("{{company.name}}", company_name)

        result = result.replace("{{ company.company_name }}", company_name)
        result = result.replace("{{company.company_name}}", company_name)

        return result

    return default_company_title(contract_type, company_name)


def apply_company_text(html, company_ctx):
    """계약서 양식 안의 회사 관련 치환자만 현재 설정값으로 바꾼다."""
    if not html:
        return ""

    raw_company_name = company_ctx.get("company_name_raw", "(사)새담청소년교육문화원")
    raw_representative_title = company_ctx.get("representative_title_raw", "이사장")
    raw_representative_name = company_ctx.get("representative_name_raw", "")

    result = str(html)

    replacements = {
        "{{ company.name }}": raw_company_name,
        "{{ company.company_name }}": raw_company_name,
        "{{ company.representative_title }}": raw_representative_title,
        "{{ company.representative_name }}": raw_representative_name,
        "{{ data.회사명 }}": raw_company_name,
        "{{ data.대표직함 }}": raw_representative_title,
        "{{ data.대표자명 }}": raw_representative_name
    }

    for old_text, new_text in replacements.items():
        result = result.replace(old_text, new_text)

    return result

def format_value(val):
    if not val or pd.isna(val) or str(val).strip() == "":
        return ""
    val = str(val).strip()
    try:
        num = float(val)
        if 0 < num < 1:
            return f"{int(num * 100)}%"
        if num >= 100:
            return "{:,}".format(int(num))
    except ValueError:
        pass
    return val

# --- [계약서 양식 렌더링/조건부 행 숨김 공통 함수] ---
# CKEditor는 <script>, 함수, 일부 style/class 속성을 저장 과정에서 정리할 수 있으므로
# 계약서 양식에는 {{ data.필드명 }} 같은 치환문자만 두고,
# 실제 출력/미리보기/PDF 생성 직전에 서버에서 조건부 행을 삭제한다.
MONEY_COLUMNS = ['수수료', '보조금', '경력수당', '직책수당', '기타']


def is_empty_contract_value(val):
    """계약서에서 행을 숨길 값인지 판단한다. 0, 0원, 0.0, 공백, None, nan 등을 빈 값으로 본다."""
    if val is None:
        return True
    try:
        if pd.isna(val):
            return True
    except Exception:
        pass

    raw = str(val).strip()
    if raw == "":
        return True

    lowered = raw.lower()
    if lowered in ["none", "nan", "null"]:
        return True

    cleaned = raw.replace(",", "").replace("원", "").replace(" ", "").strip()
    if cleaned in ["", "0", "0.0", "0.00", "-"]:
        return True

    try:
        return float(cleaned) == 0
    except Exception:
        return False


def row_has_field_marker(row_html, field):
    """
    해당 <tr>이 특정 필드용 행인지 판단한다.
    1) 신규 권장 방식: <tr data-show-if="경력수당">
    2) 기존 방식: {{ data.경력수당 }} 또는 {{ style.경력수당 }} 포함 행
    """
    field_escaped = re.escape(field)
    return bool(
        re.search(r'data-show-if\s*=\s*(["\'])\s*' + field_escaped + r'\s*\1', row_html, flags=re.IGNORECASE)
        or re.search(r'\{\{\s*data\.' + field_escaped + r'\s*\}\}', row_html)
        or re.search(r'\{\{\s*style\.' + field_escaped + r'\s*\}\}', row_html)
    )


def remove_empty_conditional_rows(html, user_data):
    """
    수수료/보조금/경력수당/직책수당/기타가 비어 있거나 0이면
    그 필드 치환문자가 들어 있는 <tr> 전체를 삭제한다.
    CKEditor가 style 속성을 날려도 {{ data.경력수당 }}만 남아 있으면 동작한다.
    """
    if not html:
        return ""

    result = str(html)
    tr_pattern = re.compile(r'<tr\b[^>]*>.*?</tr>', flags=re.IGNORECASE | re.DOTALL)

    for field in MONEY_COLUMNS:
        value = user_data.get(field, "")
        if is_empty_contract_value(value):
            def _replace_row(match):
                row_html = match.group(0)
                return "" if row_has_field_marker(row_html, field) else row_html
            result = tr_pattern.sub(_replace_row, result)

    # 남아 있는 data-show-if 속성은 최종 HTML/PDF에 노출될 필요가 없으므로 제거
    result = re.sub(r'\sdata-show-if\s*=\s*(["\']).*?\1', '', result, flags=re.IGNORECASE | re.DOTALL)
    return result


def render_contract_template(raw_html, user_data, columns):
    """
    계약서 양식 공통 렌더러.
    - 조건부 행 삭제를 먼저 수행
    - {{ data.필드명 }} 치환
    - 기존 {{ style.필드명 }} 방식도 하위 호환 처리
    """
    c = remove_empty_conditional_rows(raw_html, user_data)

    for col in columns:
        raw_val = user_data.get(col, '')
        val = format_value(raw_val) if col in MONEY_COLUMNS else str(raw_val or '')
        c = c.replace(f"{{{{ data.{col} }}}}", val)
        display_style = "display:none" if is_empty_contract_value(raw_val) else "display:table-row"
        c = c.replace(f"{{{{ style.{col} }}}}", display_style)

    return c

# --- [DB 관련 공통 함수 (엑셀 대체)] ---
def init_db():
    columns = [
        '계약구분', '수탁학교명', '부서명', '성명', '주민번호', '수수료', '보조금', '경력수당', '직책수당', '기타', '근무시간', '계약기간',
        '비고1', '비고2', '비고3', '비고4', 'email', '연락처', '거주지', '계약완료일시', '연도', '파일명', 'IP'
    ]
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='contracts'")
    if not cursor.fetchone():
        cols_def = ", ".join([f"'{col}' TEXT" for col in columns])
        cursor.execute(f"CREATE TABLE contracts ({cols_def})")
    conn.commit()
    conn.close()

def get_contracts_df():
    if not os.path.exists(DB_FILE):
        init_db()
    conn = sqlite3.connect(DB_FILE)
    try:
        df = pd.read_sql_query("SELECT * FROM contracts", conn)
    except Exception:
        init_db()
        df = pd.read_sql_query("SELECT * FROM contracts", conn)
    conn.close()
    return df.fillna("").astype(str)

def save_contracts_df(df):
    conn = sqlite3.connect(DB_FILE)
    # 기존 엑셀 덮어쓰기 로직과 동일하게 동작하도록 replace 사용
    df.to_sql('contracts', conn, if_exists='replace', index=False)
    conn.close()

init_db()

# --- [관리자 기능 로직] ---

@contract_bp.route('/admin', methods=['GET', 'POST'])
def admin_page():
    if not session.get('user_name'):
        return "<script>alert('인트라넷 로그인이 필요한 페이지입니다.'); location.href='/login_page';</script>"

    user_level = session.get('user_level')
    if user_level is None or int(user_level) > 5:
        return f"<script>alert('접근 권한이 없습니다. (현재 레벨: {user_level})'); location.href='/';</script>"

    page = request.args.get('page', 1, type=int)
    per_page = 10
    s_year, s_cat, s_school, s_dept, s_name = (
        request.args.get('year', ''), 
        request.args.get('category', ''), 
        request.args.get('school', ''), 
        request.args.get('dept', ''), 
        request.args.get('name', '')
    )

    try:
        full_df = get_contracts_df()
        total_count = len(full_df)
        completed_count = len(full_df[full_df['계약완료일시'].str.strip() != ""])
        pending_count = total_count - completed_count
        completion_rate = round((completed_count / total_count * 100), 1) if total_count > 0 else 0

        df = full_df.copy().sort_index(ascending=False)
        
        if s_year: df = df[df['연도'].astype(str).str.contains(s_year)]
        if s_cat:
            if s_cat == '미작성':
                df = df[df['계약완료일시'].astype(str).str.strip() == ""]
            else:
                df = df[df['계약구분'] == s_cat]
        if s_school: df = df[df['수탁학교명'] == s_school]
        if s_dept: df = df[df['부서명'] == s_dept]
        if s_name: df = df[df['성명'].str.contains(s_name)]

        years = sorted([str(y) for y in full_df['연도'].unique() if y != ""], reverse=True)
        schools = sorted([s for s in full_df['수탁학교명'].unique() if s != ""])
        depts = sorted([d for d in full_df['부서명'].unique() if d != ""])

        total_pages = (len(df) // per_page) + (1 if len(df) % per_page > 0 else 0)
        filtered_count = len(df)
        items = df.iloc[(page-1)*per_page : page*per_page].to_dict('records')
        
        page_indices = df.index[(page-1)*per_page : page*per_page]
        for i, item in enumerate(items):
            item['orig_idx'] = page_indices[i]

        display_size = 20 
        start_page = max(1, ((page - 1) // display_size) * display_size + 1)
        end_page = min(total_pages, start_page + display_size - 1)

        cat_list = load_categories()

        return render_template('contract/c_admin_.html', 
                               items=items, total_pages=total_pages, current_page=page,
                               start_page=start_page, end_page=end_page,
                               total_count=total_count, completed_count=completed_count,
                               pending_count=pending_count, completion_rate=completion_rate,
                               filtered_count=filtered_count, years=years, 
                               schools=schools, depts=depts, categories_list=cat_list)
    except Exception as e: 
        return f"에러: {str(e)}"

@contract_bp.route('/admin/categories', methods=['GET', 'POST'])
def manage_categories():
    if int(session.get('user_level', 99)) > 5:
        return jsonify({'status': 'error', 'message': '권한이 없습니다.'}), 403
    
    old_cats = load_categories()
    
    if request.method == 'GET':
        return jsonify({'status': 'success', 'categories': old_cats})
    else:
        new_cats = request.json.get('categories', [])
        
        # 계약형식을 삭제할 경우, 관련 텍스트 양식 파일도 서버에서 즉시 삭제
        removed_cats = set(old_cats) - set(new_cats)
        for cat in removed_cats:
            term1_path = os.path.join(TERMS_DIR, f"{cat}.txt")
            term2_path = os.path.join(TERMS_DIR, f"{cat}2.txt")
            if os.path.exists(term1_path):
                os.remove(term1_path)
            if os.path.exists(term2_path):
                os.remove(term2_path)
                
        save_categories(new_cats)
        return jsonify({'status': 'success', 'message': '계약구분이 저장되었습니다.'})


@contract_bp.route('/admin/company_settings', methods=['GET', 'POST', 'DELETE'])
def company_settings():
    """양식관리 화면에서 계약서에 찍힐 회사명/대표/도장 프로필을 최대 3세트까지 관리한다."""
    if int(session.get('user_level', 99)) > 5:
        return jsonify({'status': 'error', 'message': '권한이 없습니다.'}), 403

    settings = load_company_settings()

    def profile_for_view(profile):
        item = profile.copy()
        stamp_filename = str(item.get("stamp_filename", "")).strip()
        item["stamp_url"] = ""
        if stamp_filename and os.path.exists(os.path.join(COMPANY_STAMP_DIR, os.path.basename(stamp_filename))):
            item["stamp_url"] = url_for('contract.company_stamp_file', filename=os.path.basename(stamp_filename))
        return item

    if request.method == 'GET':
        return jsonify({
            "status": "success",
            "active_profile_id": settings.get("active_profile_id"),
            "profiles": [profile_for_view(p) for p in settings.get("profiles", [])],
            "max_profiles": MAX_COMPANY_PROFILES
        })

    if request.method == 'DELETE':
        data = request.get_json(silent=True) or {}
        profile_id = str(data.get('profile_id', '')).strip()
        profiles = settings.get("profiles", [])

        if len(profiles) <= 1:
            return jsonify({"status": "error", "message": "회사 정보는 최소 1개 이상 필요합니다."}), 400

        target = next((p for p in profiles if p.get("id") == profile_id), None)
        if not target:
            return jsonify({"status": "error", "message": "삭제할 회사 정보를 찾을 수 없습니다."}), 404

        settings["profiles"] = [p for p in profiles if p.get("id") != profile_id]
        if settings.get("active_profile_id") == profile_id:
            settings["active_profile_id"] = settings["profiles"][0].get("id")
        save_company_settings(settings)

        return jsonify({"status": "success", "message": "회사 정보 세트가 삭제되었습니다."})

    profile_id = request.form.get('profile_id', '').strip()
    action = request.form.get('action', 'save').strip()
    label = request.form.get('label', '').strip()
    company_name = request.form.get('company_name', '').strip()
    representative_title = request.form.get('representative_title', '').strip()
    representative_name = request.form.get('representative_name', '').strip()

    profiles = settings.get("profiles", [])

    if action == 'add' or not profile_id:
        if len(profiles) >= MAX_COMPANY_PROFILES:
            return jsonify({"status": "error", "message": f"회사 정보는 최대 {MAX_COMPANY_PROFILES}세트까지만 등록할 수 있습니다."}), 400
        profile_id = _make_profile_id()
        profile = {
            "id": profile_id,
            "label": label or f"회사 {len(profiles) + 1}",
            "company_name": company_name or "(사)새담청소년교육문화원",
            "representative_title": representative_title or "이사장",
            "representative_name": representative_name,
            "stamp_filename": ""
        }
        profiles.append(profile)
        settings["active_profile_id"] = profile_id
    else:
        profile = next((p for p in profiles if p.get("id") == profile_id), None)
        if not profile:
            return jsonify({"status": "error", "message": "수정할 회사 정보 세트를 찾을 수 없습니다."}), 404

        if label:
            profile["label"] = label
        if company_name:
            profile["company_name"] = company_name
        if representative_title:
            profile["representative_title"] = representative_title
        profile["representative_name"] = representative_name
        settings["active_profile_id"] = profile_id

    stamp_file = request.files.get('stamp_file')
    if stamp_file and stamp_file.filename:
        ext = os.path.splitext(stamp_file.filename)[1].lower()
        if ext not in ['.png', '.jpg', '.jpeg']:
            return jsonify({
                "status": "error",
                "message": "도장 이미지는 PNG, JPG, JPEG 파일만 등록할 수 있습니다."
            }), 400

        stamp_filename = f"company_stamp_{profile_id}_{datetime.now(KST).strftime('%Y%m%d_%H%M%S')}{ext}"
        stamp_path = os.path.join(COMPANY_STAMP_DIR, stamp_filename)
        stamp_file.save(stamp_path)
        profile["stamp_filename"] = stamp_filename

    settings["profiles"] = profiles[:MAX_COMPANY_PROFILES]
    save_company_settings(settings)

    return jsonify({
        "status": "success",
        "message": "계약서 회사 정보 세트가 저장되었습니다.",
        "active_profile_id": profile_id
    })


@contract_bp.route('/admin/company_stamp/<path:filename>')
def company_stamp_file(filename):
    """양식관리 화면에서 등록된 도장 이미지를 미리보기로 보여준다."""
    if int(session.get('user_level', 99)) > 5:
        return "권한이 없습니다.", 403

    safe_name = os.path.basename(filename)
    file_path = os.path.join(COMPANY_STAMP_DIR, safe_name)

    if not os.path.exists(file_path):
        return "파일을 찾을 수 없습니다.", 404

    return send_file(file_path)


@contract_bp.route('/admin/upload_excel', methods=['POST'])
def upload_excel():
    if int(session.get('user_level', 99)) > 5:
        return jsonify({'status': 'error', 'message': '권한이 없습니다.'}), 403

    if 'excel_file' not in request.files: return jsonify({'status': 'error', 'message': '파일 없음'}), 400
    file = request.files['excel_file']
    try:
        new_df = pd.read_excel(file, dtype=str)
        target_cols = ['수수료', '보조금', '경력수당', '직책수당', '기타']
        for col in target_cols:
            if col in new_df.columns:
                new_df[col] = new_df[col].apply(format_value)
        if '연도' not in new_df.columns: new_df['연도'] = ""
        else: new_df['연도'] = new_df['연도'].fillna("")

        existing_df = get_contracts_df()
        combined_df = pd.concat([existing_df, new_df], ignore_index=True)
        save_contracts_df(combined_df)
        return jsonify({'status': 'success', 'message': f'{len(new_df)}명의 계약정보가 추가 되었습니다.'})
    except Exception as e: return jsonify({'status': 'error', 'message': str(e)}), 500

@contract_bp.route('/admin/add', methods=['POST'])
def admin_add():
    if int(session.get('user_level', 99)) > 5:
        return jsonify({'status': 'error', 'message': '권한이 없습니다.'}), 403

    try:
        new_data = request.json
        df = get_contracts_df()
        new_row = {
            '계약구분': new_data.get('계약구분', '방과후강사'), '수탁학교명': new_data.get('수탁학교명'),
            '부서명': new_data.get('부서명'), '성명': new_data.get('성명'), '주민번호': new_data.get('주민번호'),
            '수수료': format_value(new_data.get('수수료', '0')), '보조금': format_value(new_data.get('보조금', '0')),
            '경력수당': format_value(new_data.get('경력수당', '0')), '직책수당': format_value(new_data.get('직책수당', '0')),
            '기타': format_value(new_data.get('기타', '0')), '근무시간': new_data.get('근무시간', ''),
            '계약기간': new_data.get('계약기간', ''), 
            '비고1': new_data.get('비고1', ''), '비고2': new_data.get('비고2', ''),
            '비고3': new_data.get('비고3', ''), '비고4': new_data.get('비고4', ''),
            '연도': "", '계약완료일시': "", '파일명': "", 'IP': ""
        }
        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
        save_contracts_df(df)
        return jsonify({'status': 'success'})
    except Exception as e: return jsonify({'status': 'error', 'message': str(e)}), 500

@contract_bp.route('/admin/delete', methods=['POST'])
def delete_contracts():
    if int(session.get('user_level', 99)) > 5:
        return jsonify({'status': 'error', 'message': '권한이 없습니다.'}), 403

    indices = request.json.get('indices', [])
    try:
        df = get_contracts_df()
        for idx in [int(i) for i in indices]:
            if idx in df.index:
                filename = df.at[idx, '파일명']
                if filename and not pd.isna(filename):
                    p = os.path.join(CONTRACTS_DIR, str(filename))
                    if os.path.exists(p): os.remove(p)
        df = df.drop([int(i) for i in indices])
        save_contracts_df(df)
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error", "message": str(e)})

@contract_bp.route('/admin/download_selected')
def download_selected_contracts():
    if int(session.get('user_level', 99)) > 5:
        return "<script>alert('권한이 없습니다.'); history.back();</script>", 403

    id_param = request.args.get('ids', '')
    page_range = request.args.get('range', 'all')
    if not id_param:
        return "<script>alert('선택된 항목이 없습니다.'); history.back();</script>", 400
        
    try:
        target_indices = [int(i) for i in id_param.split(',')]
        df = get_contracts_df()
        memory_file = io.BytesIO()
        
        with zipfile.ZipFile(memory_file, 'w') as zf:
            file_count = 0
            for idx in target_indices:
                if idx in df.index:
                    filename = df.at[idx, '파일명']
                    if filename:
                        file_path = os.path.join(CONTRACTS_DIR, filename)
                        if os.path.exists(file_path):
                            if page_range == '1-2' and PdfReader is not None:
                                try:
                                    reader = PdfReader(file_path)
                                    writer = PdfWriter()
                                    for p in range(min(2, len(reader.pages))):
                                        writer.add_page(reader.pages[p])
                                    
                                    pdf_bytes = io.BytesIO()
                                    writer.write(pdf_bytes)
                                    pdf_bytes.seek(0)
                                    zf.writestr(filename, pdf_bytes.read())
                                    file_count += 1
                                except Exception as pdf_e:
                                    print(f"PDF Split Error: {pdf_e}")
                                    zf.write(file_path, arcname=filename)
                                    file_count += 1
                            else:
                                zf.write(file_path, arcname=filename)
                                file_count += 1
            
            if file_count == 0:
                return "<script>alert('선택한 항목 중 작성 완료된 PDF 파일이 없습니다.'); history.back();</script>"
        
        memory_file.seek(0)
        current_time = datetime.now(KST).strftime('%Y%m%d_%H%M%S')
        return send_file(
            memory_file,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f'selected_contracts_{current_time}.zip'
        )
    except Exception as e:
        return f"다운로드 중 오류 발생: {str(e)}", 500

@contract_bp.route('/admin/terms', methods=['GET'])
def get_terms():
    if int(session.get('user_level', 99)) > 5:
        return jsonify({'status': 'error', 'message': '권한이 없습니다.'}), 403

    contract_type = request.args.get('type', '방과후강사')
    term1_path = os.path.join(TERMS_DIR, f"{contract_type}.txt")
    term2_path = os.path.join(TERMS_DIR, f"{contract_type}2.txt")

    def read_file(path):
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return f.read()
        return ""

    company_ctx = get_company_context()
    titles = load_contract_titles()
    saved_title = str(titles.get(contract_type, '')).strip()
    default_title = default_company_title(contract_type, company_ctx['company_name_raw'])

    return jsonify({
        "status": "success",
        "content1": read_file(term1_path),
        "content2": read_file(term2_path),
        "title": saved_title or default_title,
        "default_title": default_title,
        "title_saved": bool(saved_title)
    })

@contract_bp.route('/admin/terms', methods=['POST'])
def save_terms():
    if int(session.get('user_level', 99)) > 5:
        return jsonify({'status': 'error', 'message': '권한이 없습니다.'}), 403

    data = request.json
    contract_type = data.get('type')
    if not contract_type:
        return jsonify({'status': 'error', 'message': '계약구분이 필요합니다.'}), 400

    content1 = data.get('content1', '')
    content2 = data.get('content2', '')
    contract_title = str(data.get('title', '')).strip()

    if not os.path.exists(TERMS_DIR):
        os.makedirs(TERMS_DIR)

    term1_path = os.path.join(TERMS_DIR, f"{contract_type}.txt")
    term2_path = os.path.join(TERMS_DIR, f"{contract_type}2.txt")

    try:
        with open(term1_path, 'w', encoding='utf-8') as f:
            f.write(content1)
        with open(term2_path, 'w', encoding='utf-8') as f:
            f.write(content2)

        titles = load_contract_titles()
        if contract_title:
            titles[contract_type] = contract_title
        else:
            titles.pop(contract_type, None)
        save_contract_titles(titles)

        return jsonify({"status": "success", "message": "계약서 양식과 계약서 제목이 성공적으로 저장되었습니다."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@contract_bp.route('/admin/preview', methods=['POST'])
def preview_contract():
    if int(session.get('user_level', 99)) > 5:
        return "<script>alert('권한이 없습니다.'); history.back();</script>", 403

    contract_type = request.form.get('type', '방과후강사')
    
    df_columns = ['계약구분', '수탁학교명', '부서명', '성명', '주민번호', '수수료', '보조금', '경력수당', '직책수당', '기타', '근무시간', '계약기간', '비고1', '비고2', '비고3', '비고4']
    
    user_data = {
        '계약구분': contract_type,
        '수탁학교명': request.form.get('school', ''),
        '부서명': request.form.get('dept', ''),
        '성명': request.form.get('name', ''),
        '주민번호': request.form.get('ssn', ''),
        '수수료': format_value(request.form.get('fee', '0')),
        '보조금': format_value(request.form.get('subsidy', '0')),
        '경력수당': format_value(request.form.get('career', '0')),
        '직책수당': format_value(request.form.get('position', '0')),
        '기타': format_value(request.form.get('etc', '0')),
        '근무시간': request.form.get('work_time', ''),
        '계약기간': request.form.get('period', ''),
        '비고1': request.form.get('note1', ''),
        '비고2': request.form.get('note2', ''),
        '비고3': request.form.get('note3', ''),
        '비고4': request.form.get('note4', ''),
        'orig_idx': 0 
    }

    company_ctx = get_company_context()
    user_data['회사명'] = company_ctx['company_name_raw']
    user_data['대표직함'] = company_ctx['representative_title_raw']
    user_data['대표자명'] = company_ctx['representative_name_raw']
    preview_title = request.form.get('title', '').strip()

    if preview_title:
        preview_title = (
            preview_title
            .replace("{{ data.회사명 }}", company_ctx['company_name_raw'])
            .replace("{{data.회사명}}", company_ctx['company_name_raw'])
            .replace("{{ company.name }}", company_ctx['company_name_raw'])
            .replace("{{company.name}}", company_ctx['company_name_raw'])
            .replace("{{ company.company_name }}", company_ctx['company_name_raw'])
            .replace("{{company.company_name}}", company_ctx['company_name_raw'])
        )
        user_data['계약서제목'] = preview_title
    else:
        user_data['계약서제목'] = make_company_title(
            contract_type,
            company_ctx['company_name_raw']
        )

    content1_raw = request.form.get('content1', '')
    content2_raw = request.form.get('content2', '')

    def replace_tags(c):
        rendered = render_contract_template(c, user_data, df_columns + ['회사명', '대표직함', '대표자명', '계약서제목'])
        return apply_company_text(rendered, company_ctx)

    user_data['terms_content1'] = replace_tags(content1_raw)
    user_data['terms_content2'] = replace_tags(content2_raw)

    return render_template('contract/contract.html', data=user_data, company=company_ctx)

# --- [강사 서비스 로직] ---

@contract_bp.route('/')
def home():
    return redirect(url_for('contract.login'))

@contract_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        data = request.json if request.is_json else request.form.to_dict()
        name = data.get('name')
        ssn_raw = data.get('ssn', '')
        ssn_last4 = data.get('ssn_last4')
        ssn = ssn_raw.replace("-", "")
        
        try:
            df = get_contracts_df()
            user_rows = df[(df['성명'] == name) & (df['주민번호'].astype(str).str.replace("-", "") == ssn)]
            if not user_rows.empty and ssn[-4:] == ssn_last4:
                session['contract_user_name'] = name
                session['contract_user_ssn'] = ssn_raw 
                return redirect(url_for('contract.contract_list'))
            return "<script>alert('정보가 일치하지 않습니다.'); history.back();</script>"
        except Exception as e:
            return f"에러: {str(e)}"
    return render_template('contract/login.html')

@contract_bp.route('/list')
def contract_list():
    if 'contract_user_name' not in session: return redirect(url_for('contract.login'))
    df = get_contracts_df()
    my_contracts_df = df[(df['성명'] == session['contract_user_name']) & (df['주민번호'].astype(str) == session['contract_user_ssn']) & (df['계약완료일시'] == "")]
    contracts = []
    for idx, row in my_contracts_df.iterrows():
        item = row.to_dict()
        item['safe_id'] = hashids.encode(idx) 
        contracts.append(item)
    return render_template('contract/list.html', contracts=contracts, name=session['contract_user_name'])

@contract_bp.route('/contract/<string:safe_id>')
def contract(safe_id):
    if 'contract_user_name' not in session: return redirect(url_for('contract.login'))
    decoded = hashids.decode(safe_id)
    if not decoded: return abort(404)
    orig_idx = decoded[0]
    try:
        df = get_contracts_df()
        if orig_idx >= len(df): return abort(404)
        target_row = df.iloc[orig_idx]
        if target_row['성명'] != session.get('contract_user_name'):
            return "<script>alert('접근 권한이 없습니다.'); location.href='/contract/list';</script>"
        
        user_data = target_row.to_dict()
        user_data['orig_idx'] = orig_idx
        contract_type = user_data.get('계약구분', '방과후강사')

        company_ctx = get_company_context()
        user_data['회사명'] = company_ctx['company_name_raw']
        user_data['대표직함'] = company_ctx['representative_title_raw']
        user_data['대표자명'] = company_ctx['representative_name_raw']
        user_data['계약서제목'] = make_company_title(contract_type, company_ctx['company_name_raw'])
        
        def load_and_replace(filename):
            path = os.path.join(TERMS_DIR, filename)
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    rendered = render_contract_template(f.read(), user_data, list(df.columns) + ['회사명', '대표직함', '대표자명', '계약서제목'])
                    return apply_company_text(rendered, company_ctx)
            return ""
            
        user_data['terms_content1'] = load_and_replace(f"{contract_type}.txt")
        user_data['terms_content2'] = load_and_replace(f"{contract_type}2.txt")
        return render_template('contract/contract.html', data=user_data, company=company_ctx)
    except Exception as e: return f"에러: {str(e)}", 500

@contract_bp.route('/save_contract', methods=['POST'])
def save_contract():
    data = request.get_json(silent=True) or request.form.to_dict()
    if not data:
        return jsonify({"status": "error", "message": "데이터 없음"}), 400

    idx = int(data['orig_idx'])
    now_dt = datetime.now(KST)

    try:
        df = get_contracts_df()

        if str(df.at[idx, '성명']) != session.get('contract_user_name'):
            return jsonify({"status": "error", "message": "잘못된 접근입니다."}), 403

        contract_type = str(df.at[idx, '계약구분']).strip()

        company_ctx = get_company_context()
        company_name_raw = company_ctx['company_name_raw']
        company_name = company_ctx['company_name']
        company_owner_text = company_ctx['company_owner_text']
        stamp_src = company_ctx['stamp_src']

        contract_meta = {
            "방과후강사": {
                "title": "새담청소년교육문화원 위탁교육계약서",
                "school_label": "수탁학교 :",
                "part_label": "담당부서 :"
            },
            "맞춤형강사": {
                "title": "새담청소년교육문화원 위탁교육계약서",
                "school_label": "수탁학교 :",
                "part_label": "담당부서 :"
            },
            "코디근로자": {
                "title": "새담청소년교육문화원 센터장 계약서",
                "school_label": "수탁학교 :",
                "part_label": "직책 :"
            },
            "코디사업자": {
                "title": "새담청소년교육문화원 센터장 계약서",
                "school_label": "수탁학교 :",
                "part_label": "직책 :"
            },
            "원어민근로자": {
                "title": "방과후 영어 원어민 강사 위탁 계약서",
                "school_label": "School Name :",
                "part_label": "Part :"
            },
            "원어민사업자": {
                "title": "방과후 영어 원어민 강사 위탁 계약서",
                "school_label": "School Name :",
                "part_label": "Part :"
            },
            "안전코디": {
                "title": "새담청소년교육문화원 위수탁계약서",
                "school_label": "수탁학교 :",
                "part_label": "직책 :"
            },
            "직원근로자": {
                "title": "새담청소년교육문화원 근로계약서",
                "school_label": "기관명 :",
                "part_label": "직책 :"
            },
            "직원사업자": {
                "title": "새담청소년교육문화원 위탁업무계약서",
                "school_label": "기관명 :",
                "part_label": "위탁업무 :"
            }
        }

        meta = contract_meta.get(contract_type, {
            "title": f"새담청소년교육문화원 계약서 ({contract_type})",
            "school_label": "수탁학교 :",
            "part_label": "담당부서 :"
        })

        # doc_title은 위에서 회사 설정값을 반영해 생성한다.
        school_label = meta["school_label"]
        part_label = meta["part_label"]

        school_value = str(df.at[idx, '수탁학교명']) if pd.notna(df.at[idx, '수탁학교명']) else ""
        part_value = str(df.at[idx, '부서명']) if pd.notna(df.at[idx, '부서명']) else ""
        name_value = str(df.at[idx, '성명']) if pd.notna(df.at[idx, '성명']) else ""
        ssn_value = str(df.at[idx, '주민번호']) if pd.notna(df.at[idx, '주민번호']) else ""

        if contract_type in ['직원근로자', '직원사업자']:
            school_value = company_name_raw

        phone_value = str(data.get('phone', '')).strip()
        email_value = str(data.get('email', '')).strip()
        address_value = str(data.get('address', '')).strip()
        signature_value = data.get('signature', '')

        doc_title = escape(make_company_title(contract_type, company_name_raw))

        # PDF 렌더링용 데이터
        pdf_user_data = {}
        for col in df.columns:
            pdf_user_data[col] = str(df.at[idx, col]) if pd.notna(df.at[idx, col]) else ""

        pdf_user_data['수탁학교명'] = school_value
        pdf_user_data['부서명'] = part_value
        pdf_user_data['성명'] = name_value
        pdf_user_data['주민번호'] = ssn_value
        pdf_user_data['연락처'] = phone_value
        pdf_user_data['email'] = email_value
        pdf_user_data['거주지'] = address_value
        pdf_user_data['회사명'] = company_name_raw
        pdf_user_data['대표직함'] = company_ctx['representative_title_raw']
        pdf_user_data['대표자명'] = company_ctx['representative_name_raw']
        pdf_user_data['계약서제목'] = doc_title

        # PDF 상단 정보 영역 - grid 대신 table 사용
        pdf_header = f"""
        <h1 class="pdf-title">{doc_title}</h1>

        <table class="pdf-info-table">
            <colgroup>
                <col style="width: 90px;">
                <col style="width: 290px;">
                <col style="width: 90px;">
                <col style="width: 290px;">
            </colgroup>
            <tr>
                <th>{school_label}</th>
                <td>{school_value}</td>
                <th>{part_label}</th>
                <td>{part_value}</td>
            </tr>
            <tr>
                <th>성명 :</th>
                <td>{name_value}</td>
                <th>주민번호 :</th>
                <td>{ssn_value}</td>
            </tr>
            <tr>
                <th>연락처 :</th>
                <td>{phone_value}</td>
                <th>이메일 :</th>
                <td>{email_value}</td>
            </tr>
            <tr>
                <th>거주지 :</th>
                <td colspan="3">{address_value}</td>
            </tr>
        </table>
        """

        signature_section = f"""
        <div class="signature-area" style="margin-top: 0px; position: relative; min-height: 150px;">
            <p style="text-align: center; margin-bottom: 50px;">{now_dt.strftime('%Y년 %m월 %d일')}</p>

            <div style="float: left; width: 50%; position: relative;">
                <p><b>[위탁자]</b></p>
                <p style="font-size: 20px; line-height: 1.6;">
                    {company_owner_text}
                    {f'<img src="{stamp_src}" style="position: absolute; right: 0; bottom: -10px; width: 90px;">' if stamp_src else ''}
                </p>
            </div>

            <div style="float: right; width: 45%;">
                <p><b>[수탁자]</b></p>
                <p>
                    성명: {name_value}<br>
                    서명:
                    <img src="{signature_value}" style="width: 150px; border-bottom: 1px solid #000;">
                </p>
            </div>

            <div style="clear: both;"></div>
        </div>
        """

        def get_cleaned_content(filename):
            path = os.path.join(TERMS_DIR, filename)
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    rendered = render_contract_template(f.read(), pdf_user_data, list(df.columns) + ['회사명', '대표직함', '대표자명', '계약서제목'])
                    return apply_company_text(rendered, company_ctx)
            return ""

        content1 = get_cleaned_content(f"{contract_type}.txt")
        content2 = get_cleaned_content(f"{contract_type}2.txt")

        html_content = f"""
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{
                    font-family: 'Noto Sans KR', 'Malgun Gothic', sans-serif;
                    color: #111;
                    font-size: 16px;
                    line-height: 1.7;
                }}

                .pdf-title {{
                    text-align: center;
                    font-size: 24px;
                    font-weight: 900;
                    text-decoration: underline;
                    margin: 20px 0 35px;
                }}

                /* 상단 정보표는 진짜 table로 고정 */
                .pdf-info-table {{
                    width: 100%;
                    border-collapse: collapse;
                    margin: 0 0 30px 0;
                    table-layout: fixed;
                }}

                .pdf-info-table th {{
                    text-align: left;
                    font-weight: 800;
                    font-size: 14px;
                    padding: 8px 8px 8px 0;
                    white-space: nowrap;
                    vertical-align: middle;
                    border: none;
                }}

                .pdf-info-table td {{
                    text-align: left;
                    font-size: 14px;
                    font-weight: 500;
                    padding: 8px 6px;
                    vertical-align: middle;
                    border: none;
                    border-bottom: 1px solid #dcdcdc;
                    word-break: break-all;
                }}

                /* 계약조항 내부 표 스타일만 따로 적용 */
                .contract-body table {{
                    width: 100%;
                    border-collapse: collapse;
                    margin-top: 15px;
                    margin-bottom: 15px;
                }}

                .contract-body th,
                .contract-body td {{
                    border: 1px solid #333;
                    padding: 8px;
                    word-break: break-all;
                }}

                .contract-body tr {{
                    page-break-inside: avoid;
                }}

                tr[class*="display:none"] {{
                    display: none !important;
                }}

                [data-show-if] {{
                    display: table-row;
                }}
            </style>
        </head>
        <body>
            {pdf_header}

            <div class="contract-body">
                {content1}
            </div>

            {signature_section}

            {f'<div style="page-break-before:always"></div>{pdf_header}<div class="contract-body">{content2}</div>{signature_section}' if content2 else ''}
        </body>
        </html>
        """

        filename = f"{contract_type}_{now_dt.strftime('%Y%m%d_%H%M%S')}.pdf"
        pdf_path = os.path.join(CONTRACTS_DIR, filename)

        if PDF_CONFIG:
            pdfkit.from_string(
                html_content,
                pdf_path,
                configuration=PDF_CONFIG,
                options={
                    'encoding': "UTF-8",
                    'enable-local-file-access': None,
                    'page-size': 'A4',
                    'margin-top': '20mm',
                    'margin-right': '18mm',
                    'margin-bottom': '20mm',
                    'margin-left': '18mm'
                }
            )

            user_ip = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()

            df.at[idx, '연도'] = str(now_dt.year)
            df.at[idx, '연락처'] = phone_value
            df.at[idx, 'email'] = email_value
            df.at[idx, '거주지'] = address_value
            df.at[idx, '계약완료일시'] = now_dt.strftime('%Y-%m-%d %H:%M:%S')
            df.at[idx, '파일명'] = filename
            df.at[idx, 'IP'] = user_ip

            save_contracts_df(df)

            try:
                recipients = [addr for addr in [email_value, SENDER_EMAIL] if addr]
                if recipients:
                    yag = yagmail.SMTP(SENDER_EMAIL, SENDER_PASSWORD)
                    yag.send(
                        to=recipients,
                        subject=f"[완료] {name_value}님 계약서",
                        contents="계약이 완료되었습니다.",
                        attachments=pdf_path
                    )
            except Exception as mail_e:
                print("메일 발송 오류:", mail_e)

            return jsonify({"status": "success", "message": "계약이 완료되어 메일로 전송 되었습니다."})

        return jsonify({
            "status": "error",
            "message": f"PDF 엔진 오류: wkhtmltopdf를 찾을 수 없습니다. 현재 감지 경로={WKHTMLTOPDF_PATH}"
        }), 500

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@contract_bp.route('/download_pdf/<int:idx>')
def download_pdf(idx):
    try:
        df = get_contracts_df()
        if idx not in df.index:
            return "해당 기록을 찾을 수 없습니다.", 404
            
        filename = df.at[idx, '파일명']
        if not filename or filename == "":
            return "생성된 PDF 파일 정보가 없습니다.", 404
            
        pdf_path = os.path.join(CONTRACTS_DIR, str(filename))
        if not os.path.exists(pdf_path):
            return f"서버에서 파일을 찾을 수 없습니다. (파일명: {filename})", 404
            
        return send_file(
            pdf_path, 
            mimetype='application/pdf',
            as_attachment=False, 
            download_name=filename
        )
    except Exception as e:
        return f"파일 불러오기 중 오류 발생: {str(e)}", 500


@contract_bp.route('/admin/preview_pdf/<int:idx>')
def preview_pdf(idx):
    if int(session.get('user_level', 99)) > 5:
        return "<script>alert('권한이 없습니다.'); history.back();</script>", 403

    try:
        df = get_contracts_df()
        if idx not in df.index:
            return "해당 기록을 찾을 수 없습니다.", 404

        contract_type = str(df.at[idx, '계약구분']).strip()
        
        # 회사 정보 가져오기
        company_ctx = get_company_context()
        company_name_raw = company_ctx['company_name_raw']
        company_owner_text = company_ctx['company_owner_text']
        stamp_src = company_ctx['stamp_src']

        # 계약구분별 타이틀/라벨 설정
        contract_meta = {
            "방과후강사": {"school_label": "수탁학교 :", "part_label": "담당부서 :"},
            "맞춤형강사": {"school_label": "수탁학교 :", "part_label": "담당부서 :"},
            "코디근로자": {"school_label": "수탁학교 :", "part_label": "직책 :"},
            "코디사업자": {"school_label": "수탁학교 :", "part_label": "직책 :"},
            "원어민근로자": {"school_label": "School Name :", "part_label": "Part :"},
            "원어민사업자": {"school_label": "School Name :", "part_label": "Part :"},
            "안전코디": {"school_label": "수탁학교 :", "part_label": "직책 :"},
            "직원근로자": {"school_label": "기관명 :", "part_label": "직책 :"},
            "직원사업자": {"school_label": "기관명 :", "part_label": "위탁업무 :"}
        }

        meta = contract_meta.get(contract_type, {"school_label": "수탁학교 :", "part_label": "담당부서 :"})
        school_label = meta["school_label"]
        part_label = meta["part_label"]

        # 값 설정
        school_value = str(df.at[idx, '수탁학교명']) if pd.notna(df.at[idx, '수탁학교명']) else ""
        part_value = str(df.at[idx, '부서명']) if pd.notna(df.at[idx, '부서명']) else ""
        name_value = str(df.at[idx, '성명']) if pd.notna(df.at[idx, '성명']) else ""
        ssn_value = str(df.at[idx, '주민번호']) if pd.notna(df.at[idx, '주민번호']) else ""

        if contract_type in ['직원근로자', '직원사업자']:
            school_value = company_name_raw

        phone_value = str(df.at[idx, '연락처']) if pd.notna(df.at[idx, '연락처']) else ""
        email_value = str(df.at[idx, 'email']) if pd.notna(df.at[idx, 'email']) else ""
        address_value = str(df.at[idx, '거주지']) if pd.notna(df.at[idx, '거주지']) else ""

        doc_title = escape(make_company_title(contract_type, company_name_raw))

        # PDF 렌더링용 사용자 데이터 매핑
        pdf_user_data = {}
        for col in df.columns:
            pdf_user_data[col] = str(df.at[idx, col]) if pd.notna(df.at[idx, col]) else ""

        pdf_user_data.update({
            '수탁학교명': school_value, '부서명': part_value,
            '성명': name_value, '주민번호': ssn_value,
            '회사명': company_name_raw, '계약서제목': doc_title,
            '대표직함': company_ctx['representative_title_raw'],
            '대표자명': company_ctx['representative_name_raw']
        })

        # PDF 상단 정보 표
        pdf_header = f"""
        <h1 class="pdf-title">{doc_title}</h1>
        <table class="pdf-info-table">
            <colgroup>
                <col style="width: 90px;"><col style="width: 290px;">
                <col style="width: 90px;"><col style="width: 290px;">
            </colgroup>
            <tr><th>{school_label}</th><td>{school_value}</td><th>{part_label}</th><td>{part_value}</td></tr>
            <tr><th>성명 :</th><td>{name_value}</td><th>주민번호 :</th><td>{ssn_value}</td></tr>
            <tr><th>연락처 :</th><td>{phone_value}</td><th>이메일 :</th><td>{email_value}</td></tr>
            <tr><th>거주지 :</th><td colspan="3">{address_value}</td></tr>
        </table>
        """

        now_dt = datetime.now(KST)
        
        # 하단 서명 영역 (미작성 상태 반영)
        signature_section = f"""
        <div class="signature-area" style="margin-top: 0px; position: relative; min-height: 150px;">
            <p style="text-align: center; margin-bottom: 50px;">{now_dt.strftime('%Y년 %m월 %d일')}</p>
            <div style="float: left; width: 50%; position: relative;">
                <p><b>[위탁자]</b></p>
                <p style="font-size: 20px; line-height: 1.6;">
                    {company_owner_text}
                    {f'<img src="{stamp_src}" style="position: absolute; right: 0; bottom: -10px; width: 90px;">' if stamp_src else ''}
                </p>
            </div>
            <div style="float: right; width: 45%;">
                <p><b>[수탁자]</b></p>
                <p>
                    성명: {name_value}<br>
                    서명: <span style="color:#aaa; font-size:14px; font-weight:bold;">(미작성)</span>
                </p>
            </div>
            <div style="clear: both;"></div>
        </div>
        """

        def get_cleaned_content(filename):
            path = os.path.join(TERMS_DIR, filename)
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    rendered = render_contract_template(f.read(), pdf_user_data, list(df.columns) + ['회사명', '대표직함', '대표자명', '계약서제목'])
                    return apply_company_text(rendered, company_ctx)
            return ""

        content1 = get_cleaned_content(f"{contract_type}.txt")
        content2 = get_cleaned_content(f"{contract_type}2.txt")

        html_content = f"""
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: 'Noto Sans KR', 'Malgun Gothic', sans-serif; color: #111; font-size: 16px; line-height: 1.7; }}
                .pdf-title {{ text-align: center; font-size: 24px; font-weight: 900; text-decoration: underline; margin: 20px 0 35px; }}
                .pdf-info-table {{ width: 100%; border-collapse: collapse; margin: 0 0 30px 0; table-layout: fixed; }}
                .pdf-info-table th {{ text-align: left; font-weight: 800; font-size: 14px; padding: 8px 8px 8px 0; white-space: nowrap; vertical-align: middle; border: none; }}
                .pdf-info-table td {{ text-align: left; font-size: 14px; font-weight: 500; padding: 8px 6px; vertical-align: middle; border: none; border-bottom: 1px solid #dcdcdc; word-break: break-all; }}
                .contract-body table {{ width: 100%; border-collapse: collapse; margin-top: 15px; margin-bottom: 15px; }}
                .contract-body th, .contract-body td {{ border: 1px solid #333; padding: 8px; word-break: break-all; }}
                .contract-body tr {{ page-break-inside: avoid; }}
                tr[class*="display:none"] {{ display: none !important; }}
                [data-show-if] {{ display: table-row; }}
            </style>
        </head>
        <body>
            {pdf_header}
            <div class="contract-body">{content1}</div>
            {signature_section}
            {f'<div style="page-break-before:always"></div>{pdf_header}<div class="contract-body">{content2}</div>{signature_section}' if content2 else ''}
        </body>
        </html>
        """

        if PDF_CONFIG:
            # 두 번째 인자를 False로 주어 서버에 파일로 저장하지 않고 bytes로 직접 반환
            pdf_bytes = pdfkit.from_string(
                html_content,
                False,
                configuration=PDF_CONFIG,
                options={
                    'encoding': "UTF-8", 'enable-local-file-access': None,
                    'page-size': 'A4', 'margin-top': '20mm', 'margin-right': '18mm',
                    'margin-bottom': '20mm', 'margin-left': '18mm'
                }
            )
            return send_file(
                io.BytesIO(pdf_bytes),
                mimetype='application/pdf',
                as_attachment=False,
                download_name=f'preview_{name_value}.pdf'
            )
        else:
            return "PDF 엔진 오류: wkhtmltopdf를 찾을 수 없습니다.", 500

    except Exception as e:
        return f"PDF 생성 중 오류 발생: {str(e)}", 500


@contract_bp.route('/admin/logout')
def admin_logout():
    return redirect(url_for('logout'))

@contract_bp.route('/admin/send_remind_mail', methods=['POST'])
def send_remind_mail():
    if int(session.get('user_level', 99)) > 5:
        return jsonify({'status': 'error', 'message': '권한이 없습니다.'}), 403

    indices = request.json.get('indices', [])
    if not indices:
        return jsonify({'status': 'error', 'message': '대상자가 선택되지 않았습니다.'}), 400

    try:
        df = get_contracts_df()
        yag = yagmail.SMTP(SENDER_EMAIL, SENDER_PASSWORD)
        
        base_url = request.host_url.rstrip('/') 
        auth_url = f"{base_url}/contract/login"
        
        success_count = 0
        for idx in [int(i) for i in indices]:
            if idx in df.index:
                target = df.loc[idx]
                target_email = str(target.get('email', '')).strip()
                target_name = target.get('성명', '')
                school_name = target.get('수탁학교명', '')

                if "@" in target_email:
                    subject = f"[재안내] {target_name}님, 새담청소년교육문화원 전자계약 체결 요청"
                    
                    contents = f"""
                    <div style="font-family: 'Malgun Gothic', sans-serif; line-height: 1.6; color: #333;">
                        <h2 style="color: #002c63;">안녕하세요, {target_name}님.</h2>
                        <p>새담청소년교육문화원입니다.</p>
                        <p>현재 <b>[{school_name}]</b> 관련 전자계약 서명이 완료되지 않아 재안내 드립니다.</p>
                        <p>아래 버튼을 클릭하여 본인인증 후 계약서 작성을 완료해 주시기 바랍니다.</p>
                        
                        <div style="margin: 30px 0;">
                            <a href="{auth_url}" style="background-color: #004ea2; color: white; padding: 15px 25px; text-decoration: none; border-radius: 5px; font-weight: bold; display: inline-block;">계약서 작성하러 가기 (본인인증)</a>
                        </div>
                        
                        <p style="font-size: 0.9rem; color: #666;">* 본 메일은 시스템에 의해 자동 발송되었습니다.</p>
                        <hr style="border: 0; border-top: 1px solid #eee;">
                        <p style="font-size: 0.8rem; color: #888;">(사)새담청소년교육문화원 | 경기도 수원시 팔달구</p>
                    </div>
                    """
                    yag.send(to=target_email, subject=subject, contents=contents)
                    success_count += 1

        return jsonify({"status": "success", "message": f"{success_count}명에게 독촉 메일을 발송했습니다."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})