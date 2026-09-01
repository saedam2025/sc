"""면접진행 - 면접자 사전등록, 사전질문지 수집, 면접관 평가 기록."""

from __future__ import annotations

import functools
import hmac
import os
import secrets
from datetime import datetime

from flask import (
    Blueprint,
    abort,
    jsonify,
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
    encrypt_upload,
    original_filename,
)
from .storage import INTERVIEW_UPLOADS

interview_bp = Blueprint('interview', __name__)

MAX_PANELISTS = 5
MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
ALLOWED_ATTACHMENT_EXTENSIONS = {
    '.pdf', '.hwp', '.hwpx', '.doc', '.docx', '.xls', '.xlsx',
    '.ppt', '.pptx', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.zip',
}

# 사전질문지 문항. (필드명, 화면 제목, 안내문, 입력줄수)
QUESTIONNAIRE_FIELDS = (
    ('motivation', '지원동기', '우리 기관에 지원하신 이유를 자유롭게 적어주세요.', 5),
    ('job_understanding', '지원 업무에 대한 지식 및 이해도', '지원하신 업무를 어떻게 이해하고 계신지 적어주세요.', 5),
    ('related_experience', '관련 경험', '지원 업무와 관련된 경험을 적어주세요.', 5),
    ('commute', '출퇴근 방법 및 예상 소요시간', '예) 자가용 30분 / 지하철 + 버스 50분', 3),
    ('availability', '근무 가능일시 여부', '근무 시작 가능일과 가능한 요일·시간을 적어주세요.', 3),
    ('other_notes', '기타 면접 전 확인사항', '면접 전에 알려주실 내용이 있으면 적어주세요.', 4),
)
QUESTIONNAIRE_KEYS = tuple(field[0] for field in QUESTIONNAIRE_FIELDS)


def ensure_interview_schema(conn=None):
    """면접진행 메뉴가 사용하는 표준 스키마를 보장한다."""
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
        }.items():
            if column not in candidate_columns:
                conn.execute(f'ALTER TABLE interview_candidates ADD COLUMN {column} {definition}')
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


def _candidate_dict(row, answers=None, attachments=(), panelists=()):
    scores = [int(item['score']) for item in panelists if item['score'] is not None]
    result = str(_row_get(row, 'result', '') or '')
    return {
        'id': row['id'],
        'status': str(_row_get(row, 'status', 'scheduled') or 'scheduled'),
        'is_completed': str(_row_get(row, 'status', '') or '') == 'completed',
        'result': result,
        'result_label': RESULT_LABELS.get(result, ''),
        'completed_at': str(_row_get(row, 'completed_at', '') or ''),
        'name': row['name'],
        'target_position': row['target_position'],
        'target_school': row['target_school'],
        'interview_at': row['interview_at'],
        'memo': row['memo'],
        'questionnaire_url': url_for('interview.questionnaire', token=row['questionnaire_token']),
        'questionnaire_submitted_at': str(row['questionnaire_submitted_at'] or ''),
        'has_answers': bool(row['questionnaire_submitted_at']),
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
    return _answers_dict(answers), attachments, panelists


def _store_attachments(conn, candidate_id, files):
    """첨부파일을 암호화 저장하고 저장된 경로 목록을 돌려준다."""
    existing = conn.execute(
        'SELECT COUNT(*) FROM interview_attachments WHERE candidate_id=?', (candidate_id,)
    ).fetchone()[0]
    saved_paths = []
    for file in files:
        if existing + len(saved_paths) >= MAX_ATTACHMENTS:
            raise ValueError(f'첨부파일은 최대 {MAX_ATTACHMENTS}개까지 등록할 수 있습니다.')
        display_name = original_filename(file.filename)
        extension = os.path.splitext(display_name)[1].lower()
        if extension not in ALLOWED_ATTACHMENT_EXTENSIONS:
            raise ValueError(f'{display_name}: 등록할 수 없는 형식입니다.')
        stored_name = encrypted_storage_name(display_name)
        save_path = os.path.join(_upload_dir(), stored_name)
        try:
            size = encrypt_upload(file, save_path)
        except Exception:
            delete_file(save_path)
            raise
        saved_paths.append(save_path)
        if size > MAX_ATTACHMENT_BYTES:
            raise ValueError(f'{display_name}: 파일당 20MB 이하만 등록할 수 있습니다.')
        conn.execute('''
            INSERT INTO interview_attachments (
                candidate_id, filename, stored_name, file_size, uploaded_by
            ) VALUES (?, ?, ?, ?, ?)
        ''', (candidate_id, display_name, stored_name, size, _emp_no()))
    return saved_paths


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
        schools = [
            row['school_name'] for row in conn.execute(
                '''SELECT DISTINCT school_name FROM schools
                   WHERE COALESCE(is_active, 1) = 1 AND TRIM(COALESCE(school_name, '')) <> ''
                   ORDER BY school_name'''
            ).fetchall()
        ]
    finally:
        conn.close()
    return render_template(
        'interview.html',
        positions=positions,
        schools=schools,
        max_panelists=MAX_PANELISTS,
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
            answers, attachments, panelists = _load_related(conn, row['id'])
            candidates.append(_candidate_dict(row, answers, attachments, panelists))
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
        answers, attachments, panelists = _load_related(conn, candidate_id)
        return _success('면접자를 등록했습니다.', candidate=_candidate_dict(row, answers, attachments, panelists))
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
        answers, attachments, panelists = _load_related(conn, candidate_id)
        return _success('면접 정보를 불러왔습니다.', candidate=_candidate_dict(row, answers, attachments, panelists))
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
            conn.execute('DELETE FROM interview_answers WHERE candidate_id=?', (candidate_id,))
            conn.execute('DELETE FROM interview_panelists WHERE candidate_id=?', (candidate_id,))
            conn.execute('DELETE FROM interview_candidates WHERE id=?', (candidate_id,))
            conn.commit()
            for item in stored:
                delete_file(os.path.join(_upload_dir(), item['stored_name']))
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
        answers, attachments, panelists = _load_related(conn, candidate_id)
        return _success('면접자 정보를 수정했습니다.', candidate=_candidate_dict(row, answers, attachments, panelists))
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

        conn.execute('''
            UPDATE interview_candidates
            SET status=?, result=?,
                completed_at=CASE WHEN ?='completed' THEN COALESCE(completed_at, CURRENT_TIMESTAMP) ELSE NULL END,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        ''', (status, result, status, candidate_id))
        conn.commit()

        row = _load_candidate(conn, candidate_id)
        answers, attachments, panelists = _load_related(conn, candidate_id)
        message = '면접을 완료 처리했습니다.' if status == 'completed' else '면접을 진행중 상태로 되돌렸습니다.'
        if status == 'completed' and result:
            message = f'면접을 완료 처리하고 {RESULT_LABELS[result]}으로 저장했습니다.'
        return _success(message, candidate=_candidate_dict(row, answers, attachments, panelists))
    finally:
        conn.close()


# ---------------------------------------------------------------- 첨부파일

@interview_bp.route('/interview/api/candidates/<int:candidate_id>/attachments', methods=['POST'])
@_mutating
def add_attachments(candidate_id):
    files = _uploaded_files()
    if not files:
        return _error('추가할 파일을 선택해주세요.', 400, 'FILE_REQUIRED')
    conn = get_db()
    saved_paths = []
    try:
        ensure_interview_schema(conn)
        row = _load_candidate(conn, candidate_id)
        if not row:
            return _error('면접 기록을 찾을 수 없습니다.', 404, 'CANDIDATE_NOT_FOUND')
        if not _can_manage(row):
            return _error('이 면접 기록을 수정할 권한이 없습니다.', 403, 'FORBIDDEN')
        saved_paths = _store_attachments(conn, candidate_id, files)
        conn.commit()
        answers, attachments, panelists = _load_related(conn, candidate_id)
        return _success('첨부파일을 등록했습니다.', candidate=_candidate_dict(row, answers, attachments, panelists))
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
        conn.commit()
        delete_file(os.path.join(_upload_dir(), item['stored_name']))
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
    return encrypted_response(path, item['filename'], as_attachment=True)


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
        answers, attachments, panelists = _load_related(conn, candidate_id)
        return _success('면접관을 추가했습니다.', candidate=_candidate_dict(row, answers, attachments, panelists))
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

        answers, attachments, panelists = _load_related(conn, item['candidate_id'])
        return _success(
            '면접관 정보를 삭제했습니다.' if request.method == 'DELETE' else '면접 평가를 저장했습니다.',
            candidate=_candidate_dict(candidate, answers, attachments, panelists),
        )
    finally:
        conn.close()


# ---------------------------------------------------------------- 사전질문지(면접자 공개 화면)

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
            return render_template(
                'interview_questionnaire.html',
                fields=QUESTIONNAIRE_FIELDS,
                candidate=None,
                answers={},
                submitted=False,
                invalid=True,
            ), 404

        if request.method == 'POST':
            values = {
                key: str(request.form.get(key) or '').strip()[:4000]
                for key in QUESTIONNAIRE_KEYS
            }
            columns = ', '.join(QUESTIONNAIRE_KEYS)
            placeholders = ', '.join('?' for _ in QUESTIONNAIRE_KEYS)
            updates = ', '.join(f'{key}=excluded.{key}' for key in QUESTIONNAIRE_KEYS)
            conn.execute(
                f'''
                INSERT INTO interview_answers (candidate_id, {columns})
                VALUES (?, {placeholders})
                ON CONFLICT(candidate_id) DO UPDATE SET {updates}, updated_at=CURRENT_TIMESTAMP
                ''',
                (candidate['id'], *[values[key] for key in QUESTIONNAIRE_KEYS]),
            )
            conn.execute(
                'UPDATE interview_candidates SET questionnaire_submitted_at=CURRENT_TIMESTAMP WHERE id=?',
                (candidate['id'],),
            )
            conn.commit()
            return render_template(
                'interview_questionnaire.html',
                fields=QUESTIONNAIRE_FIELDS,
                candidate=candidate,
                answers=values,
                submitted=True,
                invalid=False,
            )

        stored = conn.execute(
            'SELECT * FROM interview_answers WHERE candidate_id=?', (candidate['id'],)
        ).fetchone()
        answers = {key: (stored[key] if stored else '') for key in QUESTIONNAIRE_KEYS}
        return render_template(
            'interview_questionnaire.html',
            fields=QUESTIONNAIRE_FIELDS,
            candidate=candidate,
            answers=answers,
            submitted=False,
            invalid=False,
        )
    finally:
        conn.close()
