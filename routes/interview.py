"""면접관리 - 면접자 사전등록, 사전질문지 수집, 면접관 평가 기록."""

from __future__ import annotations

import functools
import hmac
import json
import mimetypes
import os
import secrets
from datetime import datetime

from flask import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    make_response,
    render_template,
    request,
    session,
    url_for,
)

from .database import get_db
from .secure_files import (
    delete_file,
    encrypted_response,
    encrypted_storage_name,
    encrypt_bytes,
    encrypt_upload,
    original_filename,
    read_decrypted,
)
from .storage import INTERVIEW_UPLOADS
from services.interview_resume import (
    analyze_with_claude,
    analyze_with_openai,
    extract_candidate_photo,
    prepare_documents,
)

interview_bp = Blueprint('interview', __name__)

MAX_PANELISTS = 5
MAX_ATTACHMENTS = 20
MAX_ATTACHMENT_TOTAL_BYTES = 30 * 1024 * 1024
ALLOWED_ATTACHMENT_EXTENSIONS = {
    '.pdf', '.hwp', '.hwpx', '.doc', '.docx', '.xls', '.xlsx',
    '.ppt', '.pptx', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.zip',
}

# 사전질문지 문항. (필드명, 화면 제목, 안내문, 입력줄수, 파란색 덧붙임말)
QUESTIONNAIRE_FIELDS = (
    ('motivation', '지원동기', '우리 기관에 지원하신 이유를 자유롭게 적어주세요.', 5, ''),
    ('job_understanding', '지원 업무에 대한 지식 및 이해도', '지원하신 업무를 어떻게 이해하고 계신지 적어주세요.', 5, ''),
    ('related_experience', '관련 경험', '지원 업무와 관련된 경험을 적어주세요.', 5, ''),
    ('commute', '출퇴근 방법 및 예상 소요시간', '예) 자가용 30분 / 지하철 + 버스 50분', 3, ''),
    ('availability', '근무 가능일시 여부', '근무 시작 가능일과 가능한 요일·시간을 적어주세요.', 3, ''),
    ('other_notes', '기타 면접 전 확인사항', '면접 전에 알려주실 내용이 있으면 적어주세요.', 4,
     '(차량을 주차하셨다면 차량번호를 적어주세요.)'),
)
QUESTIONNAIRE_KEYS = tuple(field[0] for field in QUESTIONNAIRE_FIELDS)
# 사전질문지 작성 중 측정한 타자 기록. (분당 타자수, 총 타수, 실제 입력 시간)
TYPING_KEYS = ('typing_cpm', 'typing_strokes', 'typing_seconds')
TYPING_LIMITS = {'typing_cpm': 2000, 'typing_strokes': 200000, 'typing_seconds': 86400}


def _interview_when(value):
    """사전질문지 상단에 보여줄 면접 연도·날짜·시간을 나눠서 돌려준다."""
    raw = str(value or '').strip().replace('T', ' ')
    if not raw:
        return {}
    for pattern in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            parsed = datetime.strptime(raw, pattern)
        except ValueError:
            continue
        when = {'year': f'{parsed.year}년', 'date': f'{parsed.month}월 {parsed.day}일'}
        if pattern != '%Y-%m-%d':
            when['time'] = f'{parsed.hour:02d}:{parsed.minute:02d}'
        return when
    return {}


def _typing_stats(form):
    """면접자 화면이 보내온 타자 측정값을 믿을 수 있는 범위로 다듬는다."""
    stats = {}
    for key in TYPING_KEYS:
        try:
            number = int(float(form.get(key) or 0))
        except (TypeError, ValueError):
            number = 0
        stats[key] = max(0, min(number, TYPING_LIMITS[key]))
    return stats


def ensure_interview_schema(conn=None):
    """면접관리 메뉴가 사용하는 표준 스키마를 보장한다."""
    owns_connection = conn is None
    if owns_connection:
        conn = get_db()
    try:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS interview_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                target_position TEXT NOT NULL DEFAULT '',
                target_school TEXT NOT NULL DEFAULT '',
                interview_at TEXT NOT NULL DEFAULT '',
                memo TEXT NOT NULL DEFAULT '',
                questionnaire_token TEXT NOT NULL UNIQUE,
                questionnaire_submitted_at DATETIME,
                status TEXT NOT NULL DEFAULT 'scheduled',
                result TEXT NOT NULL DEFAULT '',
                completed_at DATETIME,
                completed_by TEXT NOT NULL DEFAULT '',
                completed_by_name TEXT NOT NULL DEFAULT '',
                completed_by_position TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL DEFAULT '',
                created_by_name TEXT NOT NULL DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_interview_candidates_at
            ON interview_candidates(interview_at DESC, id DESC);

            CREATE TABLE IF NOT EXISTS interview_answers (
                candidate_id INTEGER PRIMARY KEY,
                motivation TEXT NOT NULL DEFAULT '',
                job_understanding TEXT NOT NULL DEFAULT '',
                related_experience TEXT NOT NULL DEFAULT '',
                commute TEXT NOT NULL DEFAULT '',
                availability TEXT NOT NULL DEFAULT '',
                other_notes TEXT NOT NULL DEFAULT '',
                typing_cpm INTEGER NOT NULL DEFAULT 0,
                typing_strokes INTEGER NOT NULL DEFAULT 0,
                typing_seconds INTEGER NOT NULL DEFAULT 0,
                submitted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS interview_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                file_size INTEGER NOT NULL DEFAULT 0,
                uploaded_by TEXT NOT NULL DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_interview_attachments_candidate
            ON interview_attachments(candidate_id, id);

            CREATE TABLE IF NOT EXISTS interview_resume_analyses (
                candidate_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'pending',
                summary TEXT NOT NULL DEFAULT '',
                profile_json TEXT NOT NULL DEFAULT '{}',
                education_json TEXT NOT NULL DEFAULT '[]',
                qualifications_json TEXT NOT NULL DEFAULT '[]',
                career_json TEXT NOT NULL DEFAULT '[]',
                source_files_json TEXT NOT NULL DEFAULT '[]',
                photo_stored_name TEXT NOT NULL DEFAULT '',
                photo_mime TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                analyzed_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS interview_resume_analysis_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER NOT NULL,
                emp_no TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_interview_resume_history_candidate
            ON interview_resume_analysis_history(candidate_id, id);

            CREATE TABLE IF NOT EXISTS interview_panelists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                emp_no TEXT NOT NULL DEFAULT '',
                score INTEGER,
                comment TEXT NOT NULL DEFAULT '',
                evaluated_at DATETIME,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_interview_panelists_candidate
            ON interview_panelists(candidate_id, sort_order, id);
        ''')
        # 이미 만들어진 설치본에도 진행상태·합격여부 컬럼을 채워 넣는다.
        candidate_columns = {
            row['name'] if hasattr(row, 'keys') else row[1]
            for row in conn.execute('PRAGMA table_info(interview_candidates)').fetchall()
        }
        for column, definition in {
            'status': "TEXT NOT NULL DEFAULT 'scheduled'",
            'result': "TEXT NOT NULL DEFAULT ''",
            'completed_at': 'DATETIME',
            'completed_by': "TEXT NOT NULL DEFAULT ''",
            'completed_by_name': "TEXT NOT NULL DEFAULT ''",
            'completed_by_position': "TEXT NOT NULL DEFAULT ''",
        }.items():
            if column not in candidate_columns:
                conn.execute(f'ALTER TABLE interview_candidates ADD COLUMN {column} {definition}')
        # 이력서 요약의 지원자 기본정보(생년월일·거주지·연락처·이메일)도 뒤늦게 추가한다.
        analysis_columns = {
            row['name'] if hasattr(row, 'keys') else row[1]
            for row in conn.execute('PRAGMA table_info(interview_resume_analyses)').fetchall()
        }
        if 'profile_json' not in analysis_columns:
            conn.execute("ALTER TABLE interview_resume_analyses ADD COLUMN profile_json TEXT NOT NULL DEFAULT '{}'")
        # 사전질문지 분당 타자수도 기존 설치본에 뒤늦게 추가한다.
        answer_columns = {
            row['name'] if hasattr(row, 'keys') else row[1]
            for row in conn.execute('PRAGMA table_info(interview_answers)').fetchall()
        }
        for column, definition in {
            'typing_cpm': 'INTEGER NOT NULL DEFAULT 0',
            'typing_strokes': 'INTEGER NOT NULL DEFAULT 0',
            'typing_seconds': 'INTEGER NOT NULL DEFAULT 0',
        }.items():
            if column not in answer_columns:
                conn.execute(f'ALTER TABLE interview_answers ADD COLUMN {column} {definition}')
        conn.commit()
    finally:
        if owns_connection:
            conn.close()


def _emp_no():
    return str(session.get('emp_no') or '').strip()


def _user_level():
    try:
        return int(session.get('user_level', 99))
    except (TypeError, ValueError):
        return 99


def _success(message='', **payload):
    result = {'status': 'success', 'message': message}
    result.update(payload)
    return jsonify(result)


def _error(message, status=400, code='INTERVIEW_ERROR'):
    return jsonify({'status': 'error', 'message': message, 'code': code}), status


def _text(value, limit=500):
    return str(value or '').strip()[:limit]


def _login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not _emp_no():
            return _error('로그인이 필요합니다.', 401, 'AUTH_REQUIRED')
        return view(*args, **kwargs)
    return wrapped


def _csrf_token():
    token = session.get('interview_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['interview_csrf_token'] = token
    return token


def _mutating(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not _emp_no():
            return _error('로그인이 필요합니다.', 401, 'AUTH_REQUIRED')
        expected = session.get('interview_csrf_token') or ''
        supplied = request.headers.get('X-CSRF-Token', '')
        if not expected or not supplied or not hmac.compare_digest(str(expected), str(supplied)):
            return _error('보안 토큰이 일치하지 않습니다. 페이지를 새로고침해주세요.', 403, 'CSRF_INVALID')
        return view(*args, **kwargs)
    return wrapped


def _can_manage(candidate):
    """등록자 본인이거나 실장(레벨 3) 이상이면 수정·삭제할 수 있다."""
    if not candidate:
        return False
    return str(candidate['created_by'] or '') == _emp_no() or _user_level() <= 3


def _upload_dir():
    os.makedirs(str(INTERVIEW_UPLOADS), exist_ok=True)
    return str(INTERVIEW_UPLOADS)


def _row_get(row, key, default=None):
    """마이그레이션 직후 컬럼이 아직 없을 수 있는 sqlite3.Row에서 안전하게 읽는다."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


RESULT_LABELS = {'pass': '합격', 'fail': '불합격', 'hold': '보류'}
# 진행현황은 DB의 status(scheduled·completed)에 사전질문지 제출 여부를 더해
# 예정 → 진행중 → 완료 3단계로 보여준다.
PROGRESS_LABELS = {
    'scheduled': '면접예정',
    'ongoing': '면접진행중',
    'completed': '면접완료',
}


def _json_list(value):
    try:
        parsed = json.loads(str(value or '[]'))
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def _json_dict(value):
    try:
        parsed = json.loads(str(value or '{}'))
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _analysis_dict(row):
    if not row:
        return None
    status = str(row['status'] or 'pending')
    return {
        'status': status,
        'is_ready': status == 'ready',
        'summary': str(row['summary'] or ''),
        'profile': _json_dict(_row_get(row, 'profile_json', '{}')),
        'education': _json_list(row['education_json']),
        'qualifications': _json_list(row['qualifications_json']),
        'career': _json_list(row['career_json']),
        'source_files': _json_list(row['source_files_json']),
        'has_photo': bool(row['photo_stored_name']),
        'photo_url': url_for('interview.resume_photo', candidate_id=row['candidate_id']) if row['photo_stored_name'] else '',
        'model': str(row['model'] or ''),
        'error_message': str(row['error_message'] or ''),
        'analyzed_at': str(row['analyzed_at'] or ''),
    }


def _candidate_dict(row, answers=None, attachments=(), panelists=(), analysis=None):
    scores = [int(item['score']) for item in panelists if item['score'] is not None]
    result = str(_row_get(row, 'result', '') or '')
    is_completed = str(_row_get(row, 'status', '') or '') == 'completed'
    has_answers = bool(row['questionnaire_submitted_at'])
    # 면접자가 사전질문지를 등록한 시점부터 '면접진행중'으로 본다.
    progress_state = 'completed' if is_completed else ('ongoing' if has_answers else 'scheduled')
    return {
        'id': row['id'],
        'status': str(_row_get(row, 'status', 'scheduled') or 'scheduled'),
        'is_completed': is_completed,
        'progress_state': progress_state,
        'progress_label': PROGRESS_LABELS[progress_state],
        'result': result,
        'result_label': RESULT_LABELS.get(result, ''),
        'completed_at': str(_row_get(row, 'completed_at', '') or ''),
        'completed_by_name': str(_row_get(row, 'completed_by_name', '') or ''),
        'completed_by_position': str(_row_get(row, 'completed_by_position', '') or ''),
        'completed_by_label': ' '.join(part for part in (
            str(_row_get(row, 'completed_by_name', '') or ''),
            str(_row_get(row, 'completed_by_position', '') or ''),
        ) if part),
        'name': row['name'],
        'target_position': row['target_position'],
        'target_school': row['target_school'],
        'interview_at': row['interview_at'],
        'memo': row['memo'],
        'questionnaire_url': url_for('interview.questionnaire', token=row['questionnaire_token']),
        'questionnaire_submitted_at': str(row['questionnaire_submitted_at'] or ''),
        'has_answers': has_answers,
        'typing_cpm': int((answers or {}).get('typing_cpm') or 0),
        'created_by': row['created_by'],
        'created_by_name': row['created_by_name'],
        'created_at': str(row['created_at'] or ''),
        'can_manage': _can_manage(row),
        'answers': answers,
        'attachments': [
            {
                'id': item['id'],
                'filename': item['filename'],
                'file_size': int(item['file_size'] or 0),
                'download_url': url_for('interview.download_attachment', attachment_id=item['id']),
            }
            for item in attachments
        ],
        'attachment_count': len(attachments),
        'attachment_total_size': sum(int(item['file_size'] or 0) for item in attachments),
        'resume_analysis': _analysis_dict(analysis),
        'panelists': [
            {
                'id': item['id'],
                'name': item['name'],
                'score': item['score'],
                'comment': item['comment'],
                'evaluated_at': str(item['evaluated_at'] or ''),
            }
            for item in panelists
        ],
        'panelist_count': len(panelists),
        'evaluated_count': len(scores),
        'average_score': round(sum(scores) / len(scores), 1) if scores else None,
    }


def _answers_dict(row):
    if not row:
        return None
    data = {key: row[key] for key in QUESTIONNAIRE_KEYS}
    for key in TYPING_KEYS:
        data[key] = int(_row_get(row, key, 0) or 0)
    data['submitted_at'] = str(row['submitted_at'] or '')
    data['updated_at'] = str(row['updated_at'] or '')
    return data


def _load_candidate(conn, candidate_id):
    return conn.execute(
        'SELECT * FROM interview_candidates WHERE id=?', (candidate_id,)
    ).fetchone()


def _load_related(conn, candidate_id):
    answers = conn.execute(
        'SELECT * FROM interview_answers WHERE candidate_id=?', (candidate_id,)
    ).fetchone()
    attachments = conn.execute(
        'SELECT * FROM interview_attachments WHERE candidate_id=? ORDER BY id',
        (candidate_id,),
    ).fetchall()
    panelists = conn.execute(
        'SELECT * FROM interview_panelists WHERE candidate_id=? ORDER BY sort_order, id',
        (candidate_id,),
    ).fetchall()
    analysis = conn.execute(
        'SELECT * FROM interview_resume_analyses WHERE candidate_id=?', (candidate_id,)
    ).fetchone()
    return _answers_dict(answers), attachments, panelists, analysis


def _store_attachments(conn, candidate_id, files):
    """첨부파일을 암호화 저장하고 저장된 경로 목록을 돌려준다."""
    existing_row = conn.execute(
        'SELECT COUNT(*) AS file_count, COALESCE(SUM(file_size), 0) AS total_size '
        'FROM interview_attachments WHERE candidate_id=?', (candidate_id,)
    ).fetchone()
    existing = int(existing_row['file_count'] or 0)
    total_size = int(existing_row['total_size'] or 0)
    if existing + len(files) > MAX_ATTACHMENTS:
        raise ValueError(f'첨부파일은 최대 {MAX_ATTACHMENTS}개까지 등록할 수 있습니다.')
    saved_paths = []
    try:
        for file in files:
            display_name = original_filename(file.filename)
            extension = os.path.splitext(display_name)[1].lower()
            if extension not in ALLOWED_ATTACHMENT_EXTENSIONS:
                raise ValueError(f'{display_name}: 등록할 수 없는 형식입니다.')
            stored_name = encrypted_storage_name(display_name)
            save_path = os.path.join(_upload_dir(), stored_name)
            size = encrypt_upload(file, save_path)
            saved_paths.append(save_path)
            total_size += size
            if total_size > MAX_ATTACHMENT_TOTAL_BYTES:
                raise ValueError('첨부파일 전체 용량은 30MB 이하만 등록할 수 있습니다.')
            conn.execute('''
                INSERT INTO interview_attachments (
                    candidate_id, filename, stored_name, file_size, uploaded_by
                ) VALUES (?, ?, ?, ?, ?)
            ''', (candidate_id, display_name, stored_name, size, _emp_no()))
    except Exception:
        for path in saved_paths:
            delete_file(path)
        raise
    return saved_paths


def _reset_resume_analysis(conn, candidate_id):
    """첨부가 바뀌면 이전 요약을 비워 새 이력서 결과만 남게 한다.

    이력서를 다른 파일로 교체했을 때 예전 요약과 사진이 남아 있어
    재분석 결과와 뒤섞이던 문제를 막는다.
    """
    conn.execute('''
        INSERT INTO interview_resume_analyses (candidate_id, status, error_message, updated_at)
        VALUES (?, 'pending', '', CURRENT_TIMESTAMP)
        ON CONFLICT(candidate_id) DO UPDATE SET
            status='pending', summary='', profile_json='{}', education_json='[]', qualifications_json='[]',
            career_json='[]', source_files_json='[]', photo_stored_name='', photo_mime='',
            error_message='', analyzed_at=NULL, updated_at=CURRENT_TIMESTAMP
    ''', (candidate_id,))


def _stored_photo_path(conn, candidate_id):
    row = conn.execute(
        'SELECT photo_stored_name FROM interview_resume_analyses WHERE candidate_id=?', (candidate_id,)
    ).fetchone()
    name = str(row['photo_stored_name'] or '') if row else ''
    return os.path.join(_upload_dir(), name) if name else ''


def _uploaded_files():
    return [item for item in request.files.getlist('files') if item and item.filename]


# ---------------------------------------------------------------- 화면

@interview_bp.route('/interview')
def interview_page():
    conn = get_db()
    try:
        ensure_interview_schema(conn)
        positions = [
            row['name'] for row in conn.execute(
                'SELECT name FROM hr_positions ORDER BY sort_order, id'
            ).fetchall()
        ]
    finally:
        conn.close()
    from . import openai_settings as ai_settings
    return render_template(
        'interview.html',
        positions=positions,
        ai_status=ai_settings.public_ai_settings(ai_settings.get_ai_settings()),
        max_panelists=MAX_PANELISTS,
        max_attachments=MAX_ATTACHMENTS,
        max_attachment_total_bytes=MAX_ATTACHMENT_TOTAL_BYTES,
        questionnaire_fields=QUESTIONNAIRE_FIELDS,
    )


@interview_bp.route('/interview/sheet/<int:candidate_id>')
def interview_sheet_page(candidate_id):
    """면접 진행표. 면접관이 크게 보도록 별도 브라우저 창으로 연다."""
    conn = get_db()
    try:
        ensure_interview_schema(conn)
        row = _load_candidate(conn, candidate_id)
    finally:
        conn.close()
    if not row:
        abort(404)
    return render_template(
        'interview_sheet.html',
        candidate_id=candidate_id,
        candidate_name=row['name'],
        csrf_token=_csrf_token(),
        max_panelists=MAX_PANELISTS,
        max_attachments=MAX_ATTACHMENTS,
        max_attachment_total_bytes=MAX_ATTACHMENT_TOTAL_BYTES,
        questionnaire_fields=QUESTIONNAIRE_FIELDS,
    )


# ---------------------------------------------------------------- 면접자 API

@interview_bp.route('/interview/api/candidates', methods=['GET'])
@_login_required
def list_candidates():
    conn = get_db()
    try:
        ensure_interview_schema(conn)
        rows = conn.execute('''
            SELECT * FROM interview_candidates
            ORDER BY CASE WHEN interview_at = '' THEN 1 ELSE 0 END, interview_at DESC, id DESC
        ''').fetchall()
        candidates = []
        for row in rows:
            answers, attachments, panelists, analysis = _load_related(conn, row['id'])
            candidates.append(_candidate_dict(row, answers, attachments, panelists, analysis))
        return _success(
            '면접 목록을 불러왔습니다.',
            csrf_token=_csrf_token(),
            candidates=candidates,
        )
    finally:
        conn.close()


@interview_bp.route('/interview/api/candidates', methods=['POST'])
@_mutating
def create_candidate():
    name = _text(request.form.get('name'), 60)
    if not name:
        return _error('면접자 이름을 입력해주세요.', 400, 'NAME_REQUIRED')
    files = _uploaded_files()
    conn = get_db()
    saved_paths = []
    try:
        ensure_interview_schema(conn)
        cursor = conn.execute('''
            INSERT INTO interview_candidates (
                name, target_position, target_school, interview_at, memo,
                questionnaire_token, created_by, created_by_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            name,
            _text(request.form.get('target_position'), 60),
            _text(request.form.get('target_school'), 120),
            _text(request.form.get('interview_at'), 40),
            _text(request.form.get('memo'), 2000),
            secrets.token_urlsafe(24),
            _emp_no(),
            _text(session.get('user_name'), 60),
        ))
        candidate_id = cursor.lastrowid
        saved_paths = _store_attachments(conn, candidate_id, files)
        conn.commit()
        row = _load_candidate(conn, candidate_id)
        answers, attachments, panelists, analysis = _load_related(conn, candidate_id)
        return _success('면접자를 등록했습니다.', candidate=_candidate_dict(row, answers, attachments, panelists, analysis))
    except ValueError as exc:
        conn.rollback()
        for path in saved_paths:
            delete_file(path)
        return _error(str(exc), 400, 'ATTACHMENT_INVALID')
    except Exception:
        conn.rollback()
        for path in saved_paths:
            delete_file(path)
        raise
    finally:
        conn.close()


@interview_bp.route('/interview/api/candidates/<int:candidate_id>', methods=['GET'])
@_login_required
def get_candidate(candidate_id):
    conn = get_db()
    try:
        ensure_interview_schema(conn)
        row = _load_candidate(conn, candidate_id)
        if not row:
            return _error('면접 기록을 찾을 수 없습니다.', 404, 'CANDIDATE_NOT_FOUND')
        answers, attachments, panelists, analysis = _load_related(conn, candidate_id)
        return _success(
            '면접 정보를 불러왔습니다.',
            csrf_token=_csrf_token(),
            candidate=_candidate_dict(row, answers, attachments, panelists, analysis),
        )
    finally:
        conn.close()


@interview_bp.route('/interview/api/candidates/<int:candidate_id>', methods=['PUT', 'DELETE'])
@_mutating
def modify_candidate(candidate_id):
    conn = get_db()
    try:
        ensure_interview_schema(conn)
        row = _load_candidate(conn, candidate_id)
        if not row:
            return _error('면접 기록을 찾을 수 없습니다.', 404, 'CANDIDATE_NOT_FOUND')
        if not _can_manage(row):
            return _error('이 면접 기록을 수정할 권한이 없습니다.', 403, 'FORBIDDEN')

        if request.method == 'DELETE':
            stored = conn.execute(
                'SELECT stored_name FROM interview_attachments WHERE candidate_id=?', (candidate_id,)
            ).fetchall()
            conn.execute('DELETE FROM interview_attachments WHERE candidate_id=?', (candidate_id,))
            analysis = conn.execute(
                'SELECT photo_stored_name FROM interview_resume_analyses WHERE candidate_id=?', (candidate_id,)
            ).fetchone()
            conn.execute('DELETE FROM interview_resume_analyses WHERE candidate_id=?', (candidate_id,))
            conn.execute('DELETE FROM interview_answers WHERE candidate_id=?', (candidate_id,))
            conn.execute('DELETE FROM interview_panelists WHERE candidate_id=?', (candidate_id,))
            conn.execute('DELETE FROM interview_candidates WHERE id=?', (candidate_id,))
            conn.commit()
            for item in stored:
                delete_file(os.path.join(_upload_dir(), item['stored_name']))
            if analysis and analysis['photo_stored_name']:
                delete_file(os.path.join(_upload_dir(), analysis['photo_stored_name']))
            return _success('면접 기록과 첨부파일을 삭제했습니다.', candidate_id=candidate_id)

        data = request.get_json(silent=True) or {}
        name = _text(data.get('name'), 60)
        if not name:
            return _error('면접자 이름을 입력해주세요.', 400, 'NAME_REQUIRED')
        conn.execute('''
            UPDATE interview_candidates
            SET name=?, target_position=?, target_school=?, interview_at=?, memo=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        ''', (
            name,
            _text(data.get('target_position'), 60),
            _text(data.get('target_school'), 120),
            _text(data.get('interview_at'), 40),
            _text(data.get('memo'), 2000),
            candidate_id,
        ))
        conn.commit()
        row = _load_candidate(conn, candidate_id)
        answers, attachments, panelists, analysis = _load_related(conn, candidate_id)
        return _success('면접자 정보를 수정했습니다.', candidate=_candidate_dict(row, answers, attachments, panelists, analysis))
    finally:
        conn.close()


@interview_bp.route('/interview/api/candidates/<int:candidate_id>/status', methods=['PUT'])
@_mutating
def update_candidate_status(candidate_id):
    """면접 진행완료 처리와 합격·불합격 판정을 저장한다."""
    data = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        ensure_interview_schema(conn)
        row = _load_candidate(conn, candidate_id)
        if not row:
            return _error('면접 기록을 찾을 수 없습니다.', 404, 'CANDIDATE_NOT_FOUND')
        if not _can_manage(row):
            return _error('이 면접 기록을 수정할 권한이 없습니다.', 403, 'FORBIDDEN')

        status = str(data.get('status') or '').strip()
        if status not in {'scheduled', 'completed'}:
            return _error('면접 진행상태 값이 올바르지 않습니다.', 400, 'STATUS_INVALID')
        result = str(data.get('result') or '').strip()
        if result and result not in RESULT_LABELS:
            return _error('합격 여부 값이 올바르지 않습니다.', 400, 'RESULT_INVALID')
        if status != 'completed':
            result = ''

        # 완료 시각과 담당자는 처음 완료 처리한 값을 함께 유지하고, 되돌리면 같이 지운다.
        if status != 'completed':
            conn.execute('''
                UPDATE interview_candidates
                SET status=?, result='', completed_at=NULL, completed_by='',
                    completed_by_name='', completed_by_position='', updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            ''', (status, candidate_id))
        elif _row_get(row, 'completed_at', ''):
            conn.execute('''
                UPDATE interview_candidates
                SET status=?, result=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            ''', (status, result, candidate_id))
        else:
            conn.execute('''
                UPDATE interview_candidates
                SET status=?, result=?, completed_at=CURRENT_TIMESTAMP, completed_by=?,
                    completed_by_name=?, completed_by_position=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            ''', (
                status, result, _emp_no(),
                _text(session.get('user_name'), 60), _text(session.get('position'), 60),
                candidate_id,
            ))
        conn.commit()

        row = _load_candidate(conn, candidate_id)
        answers, attachments, panelists, analysis = _load_related(conn, candidate_id)
        message = '면접을 완료 처리했습니다.' if status == 'completed' else '면접을 진행중 상태로 되돌렸습니다.'
        if status == 'completed' and result:
            message = f'면접을 완료 처리하고 {RESULT_LABELS[result]}으로 저장했습니다.'
        return _success(message, candidate=_candidate_dict(row, answers, attachments, panelists, analysis))
    finally:
        conn.close()


# ---------------------------------------------------------------- 첨부파일

@interview_bp.route('/interview/api/candidates/<int:candidate_id>/attachments', methods=['POST'])
@_mutating
def add_attachments(candidate_id):
    files = _uploaded_files()
    if not files:
        return _error('추가할 파일을 선택해주세요.', 400, 'FILE_REQUIRED')
    # replace=1 이면 기존 첨부와 사진을 지우고 새 이력서로 완전히 바꾼다.
    replace = str(request.form.get('replace') or '').strip().lower() in {'1', 'true', 'on', 'yes'}
    conn = get_db()
    saved_paths = []
    try:
        ensure_interview_schema(conn)
        row = _load_candidate(conn, candidate_id)
        if not row:
            return _error('면접 기록을 찾을 수 없습니다.', 404, 'CANDIDATE_NOT_FOUND')
        if not _can_manage(row):
            return _error('이 면접 기록을 수정할 권한이 없습니다.', 403, 'FORBIDDEN')
        removed_paths = []
        # 첨부를 추가만 해도 기존 요약은 초기화된다. 이전 사진 경로를 먼저
        # 보관해야 DB 연결이 끊긴 암호화 사진 파일이 저장소에 남지 않는다.
        photo_path = _stored_photo_path(conn, candidate_id)
        if photo_path:
            removed_paths.append(photo_path)
        if replace:
            removed_paths = [
                os.path.join(_upload_dir(), item['stored_name'])
                for item in conn.execute(
                    'SELECT stored_name FROM interview_attachments WHERE candidate_id=?', (candidate_id,)
                ).fetchall()
            ] + removed_paths
            conn.execute('DELETE FROM interview_attachments WHERE candidate_id=?', (candidate_id,))
        saved_paths = _store_attachments(conn, candidate_id, files)
        _reset_resume_analysis(conn, candidate_id)
        conn.commit()
        for path in removed_paths:
            delete_file(path)
        answers, attachments, panelists, analysis = _load_related(conn, candidate_id)
        return _success(
            '이력서를 교체했습니다.' if replace else '첨부파일을 등록했습니다.',
            candidate=_candidate_dict(row, answers, attachments, panelists, analysis),
        )
    except ValueError as exc:
        conn.rollback()
        for path in saved_paths:
            delete_file(path)
        return _error(str(exc), 400, 'ATTACHMENT_INVALID')
    except Exception:
        conn.rollback()
        for path in saved_paths:
            delete_file(path)
        raise
    finally:
        conn.close()


@interview_bp.route('/interview/api/attachments/<int:attachment_id>', methods=['DELETE'])
@_mutating
def delete_attachment(attachment_id):
    conn = get_db()
    try:
        ensure_interview_schema(conn)
        item = conn.execute(
            'SELECT * FROM interview_attachments WHERE id=?', (attachment_id,)
        ).fetchone()
        if not item:
            return _error('첨부파일을 찾을 수 없습니다.', 404, 'ATTACHMENT_NOT_FOUND')
        candidate = _load_candidate(conn, item['candidate_id'])
        if not _can_manage(candidate):
            return _error('이 첨부파일을 삭제할 권한이 없습니다.', 403, 'FORBIDDEN')
        conn.execute('DELETE FROM interview_attachments WHERE id=?', (attachment_id,))
        photo_path = _stored_photo_path(conn, item['candidate_id'])
        _reset_resume_analysis(conn, item['candidate_id'])
        conn.commit()
        delete_file(os.path.join(_upload_dir(), item['stored_name']))
        if photo_path:
            delete_file(photo_path)
        return _success('첨부파일을 삭제했습니다.', attachment_id=attachment_id)
    finally:
        conn.close()


@interview_bp.route('/interview/api/attachments/<int:attachment_id>/download', methods=['GET'])
@_login_required
def download_attachment(attachment_id):
    conn = get_db()
    try:
        ensure_interview_schema(conn)
        item = conn.execute(
            'SELECT * FROM interview_attachments WHERE id=?', (attachment_id,)
        ).fetchone()
    finally:
        conn.close()
    if not item:
        abort(404)
    path = os.path.join(_upload_dir(), item['stored_name'])
    if not os.path.exists(path):
        abort(404)
    # 면접 집중 모드에서는 이력서를 새 탭에서 바로 열어본다.
    inline = str(request.args.get('inline') or '').strip().lower() in {'1', 'true', 'on', 'yes'}
    return encrypted_response(path, item['filename'], as_attachment=not inline)


# ---------------------------------------------------------------- AI 이력서 분석

RESUME_ANALYSIS_EXTENSIONS = {'.pdf', '.hwp', '.hwpx', '.doc', '.docx', '.png', '.jpg', '.jpeg', '.webp', '.gif'}


def _resume_ai_message(exc):
    name = exc.__class__.__name__
    if name in {'AuthenticationError', 'PermissionDeniedError'}:
        return '등록된 OpenAI API 키 인증에 실패했습니다. 통합관리 > AI api설정을 확인해주세요.', 401, 'AI_AUTH_FAILED'
    if name == 'NotFoundError':
        return '선택한 AI 모델을 사용할 수 없습니다. 통합관리 > AI api설정을 확인해주세요.', 400, 'AI_MODEL_INVALID'
    if name in {'APIConnectionError', 'APITimeoutError'}:
        return 'AI 서버 연결에 실패했습니다. 잠시 후 다시 시도해주세요.', 503, 'AI_CONNECTION_FAILED'
    if name == 'RateLimitError':
        return 'OpenAI 사용 한도 또는 API 크레딧이 부족합니다.', 429, 'AI_RATE_LIMIT'
    if isinstance(exc, (ValueError, json.JSONDecodeError)):
        return 'AI가 이력서 요약 형식을 완성하지 못했습니다. 다시 분석해주세요.', 502, 'AI_OUTPUT_INVALID'
    return 'AI 이력서 분석 중 오류가 발생했습니다.', 502, 'AI_ANALYSIS_FAILED'


@interview_bp.route('/interview/api/candidates/<int:candidate_id>/resume-analysis', methods=['POST'])
@_mutating
def analyze_resume(candidate_id):
    conn = get_db()
    old_photo = ''
    new_photo_path = ''
    try:
        ensure_interview_schema(conn)
        candidate = _load_candidate(conn, candidate_id)
        if not candidate:
            return _error('면접 기록을 찾을 수 없습니다.', 404, 'CANDIDATE_NOT_FOUND')
        if not _can_manage(candidate):
            return _error('이 면접 기록을 분석할 권한이 없습니다.', 403, 'FORBIDDEN')
        attachment_rows = conn.execute(
            'SELECT * FROM interview_attachments WHERE candidate_id=? ORDER BY id', (candidate_id,)
        ).fetchall()
        supported = [
            row for row in attachment_rows
            if os.path.splitext(str(row['filename'] or ''))[1].lower() in RESUME_ANALYSIS_EXTENSIONS
        ]
        if not supported:
            return _error(
                '분석할 이력서를 첨부해주세요. PDF, HWP, HWPX, DOC, DOCX 또는 이미지 파일을 지원합니다.',
                400, 'RESUME_FILE_REQUIRED',
            )

        from . import openai_settings as ai_settings
        settings = ai_settings.get_ai_settings()
        if not settings.get('api_key'):
            return _error(
                '통합관리 > AI api설정에 API 키를 먼저 등록해주세요.',
                400, 'AI_NOT_CONFIGURED',
            )
        provider = str(settings.get('provider') or 'openai').strip().lower()
        if provider not in {'openai', 'claude'}:
            return _error('현재 AI 제공사는 이력서 분석을 지원하지 않습니다.', 400, 'AI_PROVIDER_NOT_SUPPORTED')

        conn.execute('''
            INSERT INTO interview_resume_analyses (candidate_id, status, error_message, updated_at)
            VALUES (?, 'analyzing', '', CURRENT_TIMESTAMP)
            ON CONFLICT(candidate_id) DO UPDATE SET
                status='analyzing', error_message='', updated_at=CURRENT_TIMESTAMP
        ''', (candidate_id,))
        conn.commit()

        stored_files = []
        for row in supported:
            path = os.path.join(_upload_dir(), row['stored_name'])
            if not os.path.exists(path):
                continue
            stored_files.append({
                'filename': row['filename'],
                'mime': mimetypes.guess_type(row['filename'])[0] or 'application/octet-stream',
                'data': read_decrypted(path, MAX_ATTACHMENT_TOTAL_BYTES + 1),
            })
        if not stored_files:
            raise ValueError('저장된 이력서 파일을 읽을 수 없습니다.')

        documents, warnings = prepare_documents(stored_files)
        analyzer = analyze_with_claude if provider == 'claude' else analyze_with_openai
        result, usage = analyzer(
            str(settings['api_key']), str(settings['model']), documents,
            safety_value=_emp_no() or str(candidate_id),
        )
        photo = extract_candidate_photo(documents)
        photo_stored_name = ''
        photo_mime = ''
        if photo:
            photo_data, photo_mime = photo
            photo_stored_name = encrypted_storage_name('candidate-photo.jpg')
            new_photo_path = os.path.join(_upload_dir(), photo_stored_name)
            encrypt_bytes(photo_data, new_photo_path)

        previous = conn.execute(
            'SELECT photo_stored_name FROM interview_resume_analyses WHERE candidate_id=?', (candidate_id,)
        ).fetchone()
        old_photo = str(previous['photo_stored_name'] or '') if previous else ''
        summary = result['summary']
        if warnings and not summary:
            summary = ' · '.join(warnings[:2])
        conn.execute('''
            INSERT INTO interview_resume_analyses (
                candidate_id, status, summary, profile_json, education_json, qualifications_json,
                career_json, source_files_json, photo_stored_name, photo_mime, model,
                error_message, input_tokens, output_tokens, total_tokens,
                analyzed_at, updated_at
            ) VALUES (?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(candidate_id) DO UPDATE SET
                status='ready', summary=excluded.summary, profile_json=excluded.profile_json,
                education_json=excluded.education_json,
                qualifications_json=excluded.qualifications_json, career_json=excluded.career_json,
                source_files_json=excluded.source_files_json,
                photo_stored_name=excluded.photo_stored_name, photo_mime=excluded.photo_mime,
                model=excluded.model, error_message='', input_tokens=excluded.input_tokens,
                output_tokens=excluded.output_tokens, total_tokens=excluded.total_tokens,
                analyzed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
        ''', (
            candidate_id, summary,
            json.dumps(result.get('profile') or {}, ensure_ascii=False),
            json.dumps(result['education'], ensure_ascii=False),
            json.dumps(result['qualifications'], ensure_ascii=False),
            json.dumps(result['career'], ensure_ascii=False),
            json.dumps([item['filename'] for item in documents], ensure_ascii=False),
            photo_stored_name, photo_mime, str(settings['model']),
            usage['input_tokens'], usage['output_tokens'], usage['total_tokens'],
        ))
        conn.execute('''
            INSERT INTO interview_resume_analysis_history (
                candidate_id, emp_no, model, input_tokens, output_tokens, total_tokens
            ) VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            candidate_id, _emp_no(), str(settings['model']), usage['input_tokens'],
            usage['output_tokens'], usage['total_tokens'],
        ))
        conn.commit()
        if old_photo and old_photo != photo_stored_name:
            delete_file(os.path.join(_upload_dir(), old_photo))
        row = _load_candidate(conn, candidate_id)
        answers, attachments, panelists, analysis = _load_related(conn, candidate_id)
        return _success(
            '이력서 AI 분석을 완료했습니다.',
            candidate=_candidate_dict(row, answers, attachments, panelists, analysis),
        )
    except Exception as exc:
        conn.rollback()
        if new_photo_path:
            delete_file(new_photo_path)
        message, status, code = _resume_ai_message(exc)
        current_app.logger.exception('면접 이력서 AI 분석 실패: candidate_id=%s', candidate_id)
        try:
            conn.execute('''
                INSERT INTO interview_resume_analyses (candidate_id, status, error_message, updated_at)
                VALUES (?, 'error', ?, CURRENT_TIMESTAMP)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    status='error', error_message=excluded.error_message, updated_at=CURRENT_TIMESTAMP
            ''', (candidate_id, message))
            conn.commit()
        except Exception:
            conn.rollback()
        return _error(message, status, code)
    finally:
        conn.close()


@interview_bp.route('/interview/api/candidates/<int:candidate_id>/resume-photo', methods=['GET'])
@_login_required
def resume_photo(candidate_id):
    conn = get_db()
    try:
        ensure_interview_schema(conn)
        row = conn.execute(
            'SELECT photo_stored_name FROM interview_resume_analyses WHERE candidate_id=?', (candidate_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row or not row['photo_stored_name']:
        abort(404)
    path = os.path.join(_upload_dir(), row['photo_stored_name'])
    if not os.path.exists(path):
        abort(404)
    return encrypted_response(path, '지원자사진.jpg', as_attachment=False)


# ---------------------------------------------------------------- 면접관 평가

@interview_bp.route('/interview/api/candidates/<int:candidate_id>/panelists', methods=['POST'])
@_mutating
def add_panelist(candidate_id):
    data = request.get_json(silent=True) or {}
    name = _text(data.get('name'), 60)
    if not name:
        return _error('면접관 이름을 입력해주세요.', 400, 'PANELIST_NAME_REQUIRED')
    conn = get_db()
    try:
        ensure_interview_schema(conn)
        row = _load_candidate(conn, candidate_id)
        if not row:
            return _error('면접 기록을 찾을 수 없습니다.', 404, 'CANDIDATE_NOT_FOUND')
        count = conn.execute(
            'SELECT COUNT(*) FROM interview_panelists WHERE candidate_id=?', (candidate_id,)
        ).fetchone()[0]
        if count >= MAX_PANELISTS:
            return _error(f'면접관은 최대 {MAX_PANELISTS}명까지 추가할 수 있습니다.', 400, 'PANELIST_LIMIT')
        conn.execute('''
            INSERT INTO interview_panelists (candidate_id, name, emp_no, sort_order)
            VALUES (?, ?, ?, ?)
        ''', (candidate_id, name, _text(data.get('emp_no'), 30), count + 1))
        conn.commit()
        answers, attachments, panelists, analysis = _load_related(conn, candidate_id)
        return _success('면접관을 추가했습니다.', candidate=_candidate_dict(row, answers, attachments, panelists, analysis))
    finally:
        conn.close()


@interview_bp.route('/interview/api/panelists/<int:panelist_id>', methods=['PUT', 'DELETE'])
@_mutating
def modify_panelist(panelist_id):
    conn = get_db()
    try:
        ensure_interview_schema(conn)
        item = conn.execute(
            'SELECT * FROM interview_panelists WHERE id=?', (panelist_id,)
        ).fetchone()
        if not item:
            return _error('면접관 정보를 찾을 수 없습니다.', 404, 'PANELIST_NOT_FOUND')
        candidate = _load_candidate(conn, item['candidate_id'])

        if request.method == 'DELETE':
            if not _can_manage(candidate):
                return _error('면접관을 삭제할 권한이 없습니다.', 403, 'FORBIDDEN')
            conn.execute('DELETE FROM interview_panelists WHERE id=?', (panelist_id,))
            conn.commit()
        else:
            data = request.get_json(silent=True) or {}
            score = data.get('score')
            if score in ('', None):
                score_value = None
            else:
                try:
                    score_value = int(score)
                except (TypeError, ValueError):
                    return _error('면접점수는 0~100 사이 숫자로 입력해주세요.', 400, 'SCORE_INVALID')
                if not 0 <= score_value <= 100:
                    return _error('면접점수는 0~100 사이 숫자로 입력해주세요.', 400, 'SCORE_INVALID')
            name = _text(data.get('name'), 60) or item['name']
            conn.execute('''
                UPDATE interview_panelists
                SET name=?, score=?, comment=?, evaluated_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            ''', (name, score_value, _text(data.get('comment'), 4000), panelist_id))
            conn.commit()

        answers, attachments, panelists, analysis = _load_related(conn, item['candidate_id'])
        return _success(
            '면접관 정보를 삭제했습니다.' if request.method == 'DELETE' else '면접 평가를 저장했습니다.',
            candidate=_candidate_dict(candidate, answers, attachments, panelists, analysis),
        )
    finally:
        conn.close()


# ---------------------------------------------------------------- 사전질문지(면접자 공개 화면)

def _questionnaire_page(status=200, **context):
    """사전질문지 화면은 브라우저가 절대 캐시하지 못하게 한다.

    면접자는 같은 주소를 여러 번 열고, 담당자도 같은 링크로 확인한다.
    캐시된 옛 화면이 남으면 지금 서버에 있는 것과 다른 폼(예: 예전 필수입력
    표시가 붙은 폼)이 그대로 떠서, 전송이 막히고 빈 칸으로 커서가 튀는
    현상이 생긴다. 항상 최신 화면을 받도록 no-store를 붙인다.
    """
    response = make_response(render_template(
        'interview_questionnaire.html', fields=QUESTIONNAIRE_FIELDS, **context
    ), status)
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@interview_bp.route('/interview/q/<token>', methods=['GET', 'POST'])
def questionnaire(token):
    """면접자가 로그인 없이 여는 사전질문지. 링크 토큰 자체가 열쇠다."""
    token = str(token or '').strip()
    conn = get_db()
    try:
        ensure_interview_schema(conn)
        candidate = conn.execute(
            'SELECT * FROM interview_candidates WHERE questionnaire_token=?', (token,)
        ).fetchone()
        if not candidate:
            return _questionnaire_page(
                404, candidate=None, answers={}, when={}, submitted=False, invalid=True,
            )

        if request.method == 'POST':
            values = {
                key: str(request.form.get(key) or '').strip()[:4000]
                for key in QUESTIONNAIRE_KEYS
            }
            values.update(_typing_stats(request.form))
            if not values['typing_cpm']:
                # 다시 들어와 조금만 고친 경우에는 먼저 잰 타자 기록을 그대로 둔다.
                previous = conn.execute(
                    'SELECT * FROM interview_answers WHERE candidate_id=?', (candidate['id'],)
                ).fetchone()
                if previous:
                    for key in TYPING_KEYS:
                        values[key] = int(_row_get(previous, key, 0) or 0)
            save_keys = QUESTIONNAIRE_KEYS + TYPING_KEYS
            columns = ', '.join(save_keys)
            placeholders = ', '.join('?' for _ in save_keys)
            updates = ', '.join(f'{key}=excluded.{key}' for key in save_keys)
            conn.execute(
                f'''
                INSERT INTO interview_answers (candidate_id, {columns})
                VALUES (?, {placeholders})
                ON CONFLICT(candidate_id) DO UPDATE SET {updates}, updated_at=CURRENT_TIMESTAMP
                ''',
                (candidate['id'], *[values[key] for key in save_keys]),
            )
            conn.execute(
                'UPDATE interview_candidates SET questionnaire_submitted_at=CURRENT_TIMESTAMP WHERE id=?',
                (candidate['id'],),
            )
            conn.commit()
            return _questionnaire_page(
                candidate=candidate,
                answers=values,
                when=_interview_when(candidate['interview_at']),
                submitted=True,
                invalid=False,
            )

        stored = conn.execute(
            'SELECT * FROM interview_answers WHERE candidate_id=?', (candidate['id'],)
        ).fetchone()
        answers = {key: (stored[key] if stored else '') for key in QUESTIONNAIRE_KEYS}
        return _questionnaire_page(
            candidate=candidate,
            answers=answers,
            when=_interview_when(candidate['interview_at']),
            submitted=False,
            invalid=False,
            # 이미 제출한 질문지를 다시 열면 전송 버튼 대신 '작성완료'를 보여준다.
            already_submitted=bool(candidate['questionnaire_submitted_at']),
        )
    finally:
        conn.close()
