from flask import Blueprint, render_template, request, jsonify, session, abort
from markupsafe import escape
import os
import json
import re
from datetime import datetime, timedelta
from .database import get_db
from .organization import ORGANIZATION_GROUPS, classify_organization_group
from .security import is_admin_session
from .storage import UPLOADS_ROOT
from .secure_files import (
    decode_filename_token,
    delete_file,
    encode_filename_token,
    encrypted_response,
    encrypted_storage_name,
    encrypt_upload,
    original_filename,
    plaintext_size,
)

approval_bp = Blueprint('approval', __name__)
UPLOAD_FOLDER = str(UPLOADS_ROOT)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# 상신 전에 잠시 보관해 두는 문서의 상태값. 기안자 본인에게만 보이며
# 결재선에도 올라가지 않는다.
TEMP_STATUS = '임시저장'
RECEIVER_DOC_TYPES = ['보고서', '업무일지', '회의록']
MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_TOTAL_BYTES = 15 * 1024 * 1024

def send_system_message(conn, receiver, content):
    conn.execute("INSERT INTO messages (sender, receiver, content) VALUES (?, ?, ?)", 
                 ('🔔시스템알림', receiver.strip(), content))

def ensure_schema():
    conn = get_db()
    try:
        conn.execute("ALTER TABLE approvals ADD COLUMN receivers TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        pass 
    try:
        conn.execute("ALTER TABLE approvals ADD COLUMN cc_receivers TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        pass 
    try:
        # 🚀 파일 사이즈를 저장할 수 있도록 DB 스키마 자동 패치
        conn.execute("ALTER TABLE approvals ADD COLUMN filesize TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE attendance ADD COLUMN approval_id INTEGER")
        conn.commit()
    except Exception:
        pass
    try:
        conn.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_approval_id
            ON attendance(approval_id)
            WHERE approval_id IS NOT NULL
        ''')
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

def parse_iso_date(value):
    return datetime.strptime(str(value or '').strip(), '%Y-%m-%d').date()

def sync_completed_vacation(conn, doc, doc_data):
    if doc['doc_type'] != '휴가원':
        return False

    start_text = str(doc_data.get('vacation_start_date') or '').strip()
    end_text = str(doc_data.get('vacation_end_date') or '').strip()
    if not start_text or not end_text:
        return False

    try:
        start_date = parse_iso_date(start_text)
        end_date = parse_iso_date(end_text)
    except (TypeError, ValueError):
        return False

    if end_date < start_date:
        return False

    # FullCalendar의 종료일은 포함되지 않으므로 선택한 마지막 날의 다음 날로 저장합니다.
    calendar_end = (end_date + timedelta(days=1)).strftime('%Y-%m-%d')
    conn.execute('''
        INSERT OR IGNORE INTO attendance
        (owner, type, start_date, end_date, status, approval_id)
        VALUES (?, ?, ?, ?, '승인', ?)
    ''', (
        doc['drafter'],
        f"{doc['drafter']} 휴가",
        start_date.strftime('%Y-%m-%d'),
        calendar_end,
        doc['id']
    ))
    return True

def rows_to_dicts(rows):
    return [dict(row) for row in rows]


def _approval_names(value):
    return {
        item.strip()
        for item in str(value or '').split(',')
        if item.strip()
    }


def _normalized_approval_names(value):
    """쉼표 목록을 입력 순서대로 정리하고 중복 이름을 제거한다."""
    names = []
    seen = set()
    for item in str(value or '').split(','):
        name = item.strip()
        if name and name not in seen:
            names.append(name)
            seen.add(name)
    return names


def can_view_approval(doc, current_user):
    # 임시저장은 아직 상신하지 않은 개인 메모다. 관리자도 열어 보지 않는다.
    if str(doc['status'] or '').strip() == TEMP_STATUS:
        return bool(current_user and current_user == str(doc['drafter'] or '').strip())
    if is_admin_session():
        return True
    allowed = {
        str(doc['drafter'] or '').strip(),
        str(doc['approver_1'] or '').strip(),
        str(doc['approver_2'] or '').strip(),
    }
    allowed.update(_approval_names(doc['receivers']))
    allowed.update(_approval_names(doc['cc_receivers']))
    return bool(current_user and current_user in allowed)


def belongs_to_completed_box(doc, current_user):
    """완료 문서 중 기안·결재·수신으로 직접 참여한 문서인지 확인한다."""
    direct_participants = {
        str(doc['drafter'] or '').strip(),
        str(doc['approver_1'] or '').strip(),
        str(doc['approver_2'] or '').strip(),
    }
    direct_participants.update(_approval_names(doc['receivers']))
    return bool(current_user and current_user in direct_participants)


def split_stored_list(value):
    return [item.strip() for item in str(value or '').split(',') if item.strip()]


def attachment_columns(doc):
    """저장된 첨부파일을 (표시이름토큰, 경로, 크기) 묶음으로 돌려준다."""
    names = split_stored_list(doc['filename'] if doc else '')
    paths = split_stored_list(doc['filepath'] if doc else '')
    sizes = [item.strip() for item in str((doc['filesize'] if doc else '') or '').split(',')]
    return [
        {
            'name_token': names[index] if index < len(names) else '',
            'path': path,
            'size': sizes[index] if index < len(sizes) else '',
        }
        for index, path in enumerate(paths)
    ]


def store_uploaded_files(files, already_used_bytes=0, already_count=0):
    """업로드 파일을 암호화 저장하고 항목 목록을 돌려준다(실패 시 되돌린다)."""
    stored = []
    total_bytes = already_used_bytes
    try:
        for file in files:
            if not file or not file.filename:
                continue
            if already_count + len(stored) >= MAX_ATTACHMENTS:
                raise ValueError(f'첨부파일은 최대 {MAX_ATTACHMENTS}개까지 올릴 수 있습니다.')
            fname = original_filename(file.filename)
            fpath = os.path.join(UPLOAD_FOLDER, encrypted_storage_name(fname))
            size_bytes = encrypt_upload(file, fpath)
            total_bytes += size_bytes
            if total_bytes > MAX_ATTACHMENT_TOTAL_BYTES:
                delete_file(fpath)
                raise ValueError('첨부파일 총 용량은 15MB를 넘을 수 없습니다.')
            stored.append({
                'name_token': encode_filename_token(fname),
                'path': fpath,
                'size': f"{size_bytes / (1024 * 1024):.2f}MB",
            })
    except Exception:
        for item in stored:
            delete_file(item['path'])
        raise
    return stored


def merge_attachments(existing, keep_indexes, new_items):
    """이어 쓰던 문서의 첨부를 정리한다. (남길 목록, 지울 경로) 를 돌려준다."""
    kept, removed = [], []
    for index, item in enumerate(existing):
        if keep_indexes is None or index in keep_indexes:
            kept.append(item)
        else:
            removed.append(item['path'])
    return kept + list(new_items), removed


def attachment_columns_to_text(items):
    return (
        ','.join(item['name_token'] for item in items),
        ','.join(item['path'] for item in items),
        ','.join(item['size'] for item in items),
    )


def parse_keep_indexes(raw):
    """화면이 보낸 '남길 첨부 순번' 목록. 값이 없으면 전부 유지한다."""
    if raw is None:
        return None
    text = str(raw).strip()
    if text == '':
        return set()
    indexes = set()
    for item in text.split(','):
        item = item.strip()
        if item.isdigit():
            indexes.add(int(item))
    return indexes


def normalize_user_level(value, default=14):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_current_user_level(conn, current_user):
    current_emp_no = str(session.get('emp_no', '')).strip()
    row = None
    if current_emp_no:
        row = conn.execute(
            "SELECT level FROM users WHERE emp_no=? AND status='승인' LIMIT 1",
            (current_emp_no,)
        ).fetchone()
    if not row:
        row = conn.execute(
            "SELECT level FROM users WHERE name=? AND status='승인' ORDER BY id ASC LIMIT 1",
            (current_user,)
        ).fetchone()
    if row:
        return normalize_user_level(row['level'])
    return normalize_user_level(session.get('user_level', 14))


def get_approval_members(conn):
    rows = conn.execute('''
        SELECT emp_no, name, position, level, department
        FROM users
        WHERE status='승인'
          AND LOWER(COALESCE(emp_no, '')) != 'admin'
          AND LOWER(COALESCE(name, '')) != 'admin'
        ORDER BY level ASC, name ASC
    ''').fetchall()

    members = []
    for row in rows:
        user = dict(row)
        members.append({
            'name': user.get('name') or '',
            'role': user.get('position') or '사원',
            'level': normalize_user_level(user.get('level')),
            'dept': user.get('department') or '소속 없음'
        })
    members.sort(key=lambda user: (user['level'], user['name']))
    return members


def group_approval_members(members):
    grouped_users = [
        {'group': group, 'users': []}
        for group in ORGANIZATION_GROUPS
    ]
    users_by_group = {
        group['group']: group['users']
        for group in grouped_users
    }
    for user in members:
        group_name = classify_organization_group(user['dept'], user['role'])
        users_by_group[group_name].append(user)
    return [group for group in grouped_users if group['users']]


CC_ROW_PATTERN = re.compile(
    r'<tr>\s*<th[^>]*>\s*참조자\s*</th>\s*<td[^>]*>.*?</td>\s*</tr>',
    re.IGNORECASE | re.DOTALL,
)
DOC_TITLE_ROW_PATTERN = re.compile(
    r'(<tr>\s*<th[^>]*>\s*(?:문서번호|보고서번호)\s*</th>\s*<td[^>]*>.*?</td>\s*</tr>)',
    re.IGNORECASE | re.DOTALL,
)
COMMENT_BLOCK_PATTERN = re.compile(
    r'<table[^>]*data-approval-comments="1".*?</table>',
    re.IGNORECASE | re.DOTALL,
)


def build_cc_row(cc_names):
    """문서 머리글에 넣을 참조자 줄을 만든다."""
    if not cc_names:
        return ''
    names = escape(', '.join(cc_names))
    return (
        '<tr><th style="background: var(--widget-bg, #f8fafc); padding: 8px;">참조자</th>'
        f'<td style="padding: 8px;">{names}</td></tr>'
    )


def apply_cc_row(body_html, cc_names):
    """저장 직전에 문서 본문의 참조자 줄을 실제 선택값과 똑같이 맞춘다.

    참조자를 고른 뒤 결재선을 바꾸는 등으로 머리글이 오래된 값을 들고 있어도,
    문서에 남는 참조자 목록이 DB의 참조자와 어긋나지 않게 한다.
    """
    html = str(body_html or '')
    if not html:
        return html
    row = build_cc_row(cc_names)
    if CC_ROW_PATTERN.search(html):
        # 참조자를 모두 해제했으면 줄 자체를 지운다.
        return CC_ROW_PATTERN.sub(row, html, count=1)
    if not row:
        return html
    match = DOC_TITLE_ROW_PATTERN.search(html)
    if match:
        return html[:match.end()] + row + html[match.end():]
    return html


def append_comment_block(body_html, comments):
    """결재자가 남긴 참조글을 문서 맨 아래에 붙인다."""
    html = COMMENT_BLOCK_PATTERN.sub('', str(body_html or '')).rstrip()
    rows = []
    for item in comments or ():
        text = str(item.get('text') or '').strip()
        if not text:
            continue
        writer = escape(str(item.get('by') or ''))
        step = escape(str(item.get('step') or ''))
        at = escape(str(item.get('at') or ''))
        meta = ' · '.join(part for part in [writer, step, at] if part)
        body = escape(text).replace('\n', '<br>')
        rows.append(
            '<tr>'
            '<th style="background:var(--widget-bg, #f8fafc); padding:10px; width:18%;'
            ' text-align:center; vertical-align:top;">'
            f'{meta}</th>'
            f'<td style="padding:10px; white-space:pre-wrap; line-height:1.6;">{body}</td>'
            '</tr>'
        )
    if not rows:
        return html
    return html + (
        '<table data-approval-comments="1" border="1" style="width:100%;'
        ' border-collapse:collapse; margin-top:14px; font-size:0.85rem;">'
        '<tr><th colspan="2" style="background:var(--widget-bg, #f8fafc);'
        ' padding:10px; text-align:center;">결재 참조</th></tr>'
        + ''.join(rows) +
        '</table>'
    )


@approval_bp.route('/')
def index():
    ensure_schema() # DB 스키마 패치
    current_user = session.get('user_name', '배서현')
    conn = get_db()

    max_id_row = conn.execute("SELECT MAX(id) as max_id FROM approvals").fetchone()
    next_id = (max_id_row['max_id'] or 0) + 1

    pending_rows = conn.execute('''
        SELECT * FROM approvals
        WHERE (approver_1 = ? AND status = '대기')
           OR (approver_2 = ? AND status = '1차승인')
        ORDER BY created_at DESC
    ''', (current_user, current_user)).fetchall()
    pending_docs = rows_to_dicts(pending_rows)

    draft_rows = conn.execute('''
        SELECT * FROM approvals
        WHERE drafter = ? AND status NOT IN ('완료', '반려', ?)
        ORDER BY created_at DESC
    ''', (current_user, TEMP_STATUS)).fetchall()
    my_drafts = rows_to_dicts(draft_rows)

    # 임시저장함 : 아직 상신하지 않은 내 문서(본인에게만 보인다).
    temp_rows = conn.execute('''
        SELECT * FROM approvals
        WHERE drafter = ? AND status = ?
        ORDER BY updated_at DESC, id DESC
    ''', (current_user, TEMP_STATUS)).fetchall()
    temp_docs = []
    for row in temp_rows:
        item = dict(row)
        item['attachments'] = [
            {
                'name': decode_filename_token(entry['name_token']) or os.path.basename(entry['path']),
                'size': entry['size'],
            }
            for entry in attachment_columns(row)
        ]
        temp_docs.append(item)

    rejected_rows = conn.execute('''
        SELECT * FROM approvals
        WHERE drafter = ? AND status = '반려'
        ORDER BY updated_at DESC
    ''', (current_user,)).fetchall()
    rejected_docs = rows_to_dicts(rejected_rows)

    completed_rows = conn.execute('''
        SELECT * FROM approvals
        WHERE status = '완료'
        ORDER BY updated_at DESC
    ''').fetchall()
    completed_docs = [
        dict(row) for row in completed_rows
        if belongs_to_completed_box(row, current_user)
    ]
    reference_docs = [
        dict(row) for row in completed_rows
        if current_user in _approval_names(row['cc_receivers'])
    ]

    archive_docs = [
        dict(row)
        for row in completed_rows
        if can_view_approval(row, current_user)
    ]

    current_user_level = get_current_user_level(conn, current_user)
    all_members = get_approval_members(conn)
    approver_users = [
        user for user in all_members
        if user['level'] <= current_user_level
        and user['name'] != current_user
    ]
    receiver_users = [
        user for user in all_members
        if user['name'] != current_user
    ]
    receiver_grouped_users = group_approval_members(receiver_users)
    reference_users = [
        user for user in all_members
        if user['name'] != current_user
    ]
        
    conn.close()

    return render_template('approval.html', 
                           current_user=current_user, 
                           pending_docs=pending_docs,
                           my_drafts=my_drafts,
                           rejected_docs=rejected_docs,
                           completed_docs=completed_docs,
                           temp_docs=temp_docs,
                           reference_docs=reference_docs,
                           archive_docs=archive_docs,
                           approver_users=approver_users,
                           receiver_grouped_users=receiver_grouped_users,
                           reference_users=reference_users,
                           next_id=next_id)

def load_editable_temp_doc(conn, draft_id, current_user):
    """이어 쓰기 요청이 내 임시저장 문서를 가리키는지 확인한다."""
    if not draft_id:
        return None
    try:
        draft_id = int(draft_id)
    except (TypeError, ValueError):
        return None
    row = conn.execute("SELECT * FROM approvals WHERE id=?", (draft_id,)).fetchone()
    if not row:
        return None
    if str(row['status'] or '').strip() != TEMP_STATUS:
        return None
    if str(row['drafter'] or '').strip() != current_user:
        return None
    return row


@approval_bp.route('/submit', methods=['POST'])
def submit_approval():
    ensure_schema()
    current_user = session.get('user_name', '익명')
    doc_type = request.form.get('doc_type')
    title = request.form.get('title')
    doc_data = request.form.get('doc_data', '{}')

    approver_1 = request.form.get('approver_1', '').strip()
    approver_2 = request.form.get('approver_2', '').strip()
    receiver_names = _normalized_approval_names(request.form.get('receivers', ''))
    cc_names = _normalized_approval_names(request.form.get('cc_receivers', ''))
    receiver_doc_types = RECEIVER_DOC_TYPES

    try:
        doc_data_dict = json.loads(doc_data) if doc_data else {}
    except (TypeError, json.JSONDecodeError):
        return jsonify({"status": "error", "message": "문서 내용을 확인해주세요."}), 400
    if not isinstance(doc_data_dict, dict):
        return jsonify({"status": "error", "message": "문서 내용 형식이 올바르지 않습니다."}), 400

    if doc_type == '휴가원':
        vacation_start = request.form.get('vacation_start_date', '').strip()
        vacation_end = request.form.get('vacation_end_date', '').strip()
        try:
            start_date = parse_iso_date(vacation_start)
            end_date = parse_iso_date(vacation_end)
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "휴가 시작일과 종료일을 선택해주세요."}), 400
        if end_date < start_date:
            return jsonify({"status": "error", "message": "휴가 종료일은 시작일보다 빠를 수 없습니다."}), 400
        doc_data_dict['vacation_start_date'] = vacation_start
        doc_data_dict['vacation_end_date'] = vacation_end

    validation_conn = get_db()
    try:
        current_user_level = get_current_user_level(validation_conn, current_user)
        members = get_approval_members(validation_conn)
        member_names = {user['name'] for user in members}
        eligible_approver_names = {
            user['name'] for user in members
            if user['level'] <= current_user_level and user['name'] != current_user
        }
    finally:
        validation_conn.close()

    selected_names = set(receiver_names) | set(cc_names) | {approver_1, approver_2}
    selected_names.discard('')
    if current_user in selected_names:
        return jsonify({"status": "error", "message": "상신자 본인은 결재자, 수신자 또는 참조자로 지정할 수 없습니다."}), 400
    unknown_names = selected_names - member_names
    if unknown_names:
        return jsonify({"status": "error", "message": "승인된 회원만 결재자, 수신자 또는 참조자로 지정할 수 있습니다."}), 400

    if doc_type in receiver_doc_types:
        if not receiver_names:
            return jsonify({"status": "error", "message": "수신자를 최소 1명 이상 지정해주세요."}), 400
        overlap = set(receiver_names) & set(cc_names)
        if overlap:
            return jsonify({"status": "error", "message": "수신자와 참조자는 중복 지정할 수 없습니다."}), 400
        approver_1 = ''
        approver_2 = ''
    else:
        if not approver_1:
            return jsonify({"status": "error", "message": "1차 결재자는 필수입니다."}), 400
        if approver_2 and approver_1 == approver_2:
            return jsonify({"status": "error", "message": "1차 결재자와 2차 결재자는 같은 사람으로 지정할 수 없습니다."}), 400

        if approver_1 not in eligible_approver_names:
            return jsonify({
                "status": "error",
                "message": "1차 결재자는 본인과 동급이거나 상위 레벨인 회원만 지정할 수 있습니다."
            }), 400
        if approver_2 and approver_2 not in eligible_approver_names:
            return jsonify({
                "status": "error",
                "message": "2차 결재자는 본인과 동급이거나 상위 레벨인 회원만 지정할 수 있습니다."
            }), 400
        if ({approver_1, approver_2} - {''}) & set(cc_names):
            return jsonify({"status": "error", "message": "결재자와 참조자는 중복 지정할 수 없습니다."}), 400
        receiver_names = []

    receivers = ','.join(receiver_names)
    cc_receivers = ','.join(cc_names)

    # 참조자 목록을 문서 자체(본문·부가정보)에도 함께 남긴다. 화면에서 만든
    # 머리글이 오래된 값을 들고 있어도 실제 지정한 참조자와 어긋나지 않는다.
    doc_data_dict['cc_receivers'] = cc_names
    doc_data_dict['본문'] = apply_cc_row(doc_data_dict.get('본문'), cc_names)
    doc_data = json.dumps(doc_data_dict, ensure_ascii=False)

    if doc_type in receiver_doc_types:
        status = '완료'
    elif approver_1 == '전결':
        status = '1차승인'
    else:
        status = '대기'

    conn = get_db()
    stored_now = []
    try:
        temp_doc = load_editable_temp_doc(conn, request.form.get('draft_id'), current_user)
        existing = attachment_columns(temp_doc) if temp_doc else []
        keep_indexes = parse_keep_indexes(request.form.get('keep_files'))
        used_bytes = 0
        for entry in existing:
            if os.path.isfile(entry['path']):
                used_bytes += plaintext_size(entry['path'])
        stored_now = store_uploaded_files(
            request.files.getlist('file'), used_bytes, len(existing)
        )
        merged, removed_paths = merge_attachments(existing, keep_indexes, stored_now)
        filename_str, filepath_str, filesize_str = attachment_columns_to_text(merged)

        if temp_doc:
            conn.execute('''
                UPDATE approvals
                SET doc_type=?, title=?, approver_1=?, approver_2=?, receivers=?,
                    cc_receivers=?, status=?, doc_data=?, filename=?, filepath=?,
                    filesize=?, created_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND drafter=? AND status=?
            ''', (doc_type, title, approver_1, approver_2, receivers, cc_receivers,
                  status, doc_data, filename_str, filepath_str, filesize_str,
                  temp_doc['id'], current_user, TEMP_STATUS))
            approval_id = int(temp_doc['id'])
        else:
            cursor = conn.execute('''
                INSERT INTO approvals (doc_type, title, drafter, approver_1, approver_2, receivers, cc_receivers, status, doc_data, filename, filepath, filesize)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (doc_type, title, current_user, approver_1, approver_2, receivers, cc_receivers, status, doc_data, filename_str, filepath_str, filesize_str))
            approval_id = cursor.lastrowid

        if status == '대기' and approver_1:
            send_system_message(conn, approver_1, f"새 결재를 검토해주세요: [{doc_type}] {title}")
        elif status == '1차승인' and approver_2:
            send_system_message(conn, approver_2, f"새 결재를 검토해주세요 (전결 상신): [{doc_type}] {title}")
        elif status == '완료':
            if receivers:
                for rec in receivers.split(','):
                    if rec.strip(): send_system_message(conn, rec.strip(), f"새 수신 문서가 도착했습니다: [{doc_type}] {title}")
            if cc_receivers:
                for cc in cc_receivers.split(','):
                    if cc.strip(): send_system_message(conn, cc.strip(), f"참조 문서가 등록되었습니다: [{doc_type}] {title}")
        conn.commit()
    except ValueError as exc:
        conn.rollback()
        for item in stored_now:
            delete_file(item['path'])
        conn.close()
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception:
        conn.rollback()
        for item in stored_now:
            delete_file(item['path'])
        conn.close()
        raise
    else:
        conn.close()

    # 커밋이 끝난 뒤에 지운다. 저장에 실패하면 예전 파일이 그대로 남는다.
    for path in removed_paths:
        delete_file(path)
    return jsonify({
        "status": "success",
        "message": "성공적으로 상신되었습니다.",
        "id": approval_id,
    })


@approval_bp.route('/draft/save', methods=['POST'])
def save_temp_draft():
    """상신 전에 쓰던 내용을 그대로 보관한다(결재선은 아직 비어 있어도 된다)."""
    ensure_schema()
    current_user = session.get('user_name', '익명')
    doc_type = str(request.form.get('doc_type') or '기안서').strip()
    title = str(request.form.get('title') or '').strip()
    if not title:
        return jsonify({"status": "error", "message": "임시저장하려면 문서 제목을 먼저 입력해주세요."}), 400

    try:
        doc_data_dict = json.loads(request.form.get('doc_data') or '{}')
    except (TypeError, json.JSONDecodeError):
        doc_data_dict = {}
    if not isinstance(doc_data_dict, dict):
        doc_data_dict = {}

    approver_1 = str(request.form.get('approver_1') or '').strip()
    approver_2 = str(request.form.get('approver_2') or '').strip()
    receiver_names = _normalized_approval_names(request.form.get('receivers', ''))
    cc_names = _normalized_approval_names(request.form.get('cc_receivers', ''))

    # 임시저장은 검증을 최소로 한다. 아직 다 고르지 않은 상태로도 보관해야 한다.
    doc_data_dict['cc_receivers'] = cc_names
    doc_data_dict['본문'] = apply_cc_row(doc_data_dict.get('본문'), cc_names)
    if doc_type == '휴가원':
        doc_data_dict['vacation_start_date'] = str(request.form.get('vacation_start_date') or '').strip()
        doc_data_dict['vacation_end_date'] = str(request.form.get('vacation_end_date') or '').strip()
    doc_data = json.dumps(doc_data_dict, ensure_ascii=False)

    conn = get_db()
    stored_now = []
    removed_paths = []
    try:
        temp_doc = load_editable_temp_doc(conn, request.form.get('draft_id'), current_user)
        existing = attachment_columns(temp_doc) if temp_doc else []
        keep_indexes = parse_keep_indexes(request.form.get('keep_files'))
        used_bytes = 0
        for entry in existing:
            if os.path.isfile(entry['path']):
                used_bytes += plaintext_size(entry['path'])
        stored_now = store_uploaded_files(
            request.files.getlist('file'), used_bytes, len(existing)
        )
        merged, removed_paths = merge_attachments(existing, keep_indexes, stored_now)
        filename_str, filepath_str, filesize_str = attachment_columns_to_text(merged)

        if temp_doc:
            conn.execute('''
                UPDATE approvals
                SET doc_type=?, title=?, approver_1=?, approver_2=?, receivers=?,
                    cc_receivers=?, doc_data=?, filename=?, filepath=?, filesize=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND drafter=? AND status=?
            ''', (doc_type, title, approver_1, approver_2, ','.join(receiver_names),
                  ','.join(cc_names), doc_data, filename_str, filepath_str, filesize_str,
                  temp_doc['id'], current_user, TEMP_STATUS))
            draft_id = int(temp_doc['id'])
        else:
            cursor = conn.execute('''
                INSERT INTO approvals (doc_type, title, drafter, approver_1, approver_2,
                                       receivers, cc_receivers, status, doc_data,
                                       filename, filepath, filesize)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (doc_type, title, current_user, approver_1, approver_2,
                  ','.join(receiver_names), ','.join(cc_names), TEMP_STATUS, doc_data,
                  filename_str, filepath_str, filesize_str))
            draft_id = cursor.lastrowid
        conn.commit()
    except ValueError as exc:
        conn.rollback()
        for item in stored_now:
            delete_file(item['path'])
        conn.close()
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception:
        conn.rollback()
        for item in stored_now:
            delete_file(item['path'])
        conn.close()
        raise
    else:
        conn.close()

    for path in removed_paths:
        delete_file(path)
    return jsonify({
        "status": "success",
        "message": "임시저장했습니다. [임시저장함]에서 이어서 작성할 수 있습니다.",
        "id": draft_id,
    })


@approval_bp.route('/draft/<int:draft_id>')
def get_temp_draft(draft_id):
    """임시저장함에서 고른 문서를 기안 작성창에 그대로 되살린다."""
    current_user = session.get('user_name')
    conn = get_db()
    try:
        doc = conn.execute("SELECT * FROM approvals WHERE id=?", (draft_id,)).fetchone()
        if not doc:
            return jsonify({"status": "error", "message": "문서를 찾을 수 없습니다."}), 404
        if str(doc['status'] or '').strip() != TEMP_STATUS \
                or str(doc['drafter'] or '').strip() != current_user:
            return jsonify({"status": "error", "message": "본인이 임시저장한 문서만 열 수 있습니다."}), 403
        item = dict(doc)
    finally:
        conn.close()

    item['attachments'] = [
        {
            'name': decode_filename_token(entry['name_token']) or os.path.basename(entry['path']),
            'size': entry['size'],
        }
        for entry in attachment_columns(doc)
    ]
    return jsonify({"status": "success", "draft": item})


@approval_bp.route('/draft/<int:draft_id>/delete', methods=['POST'])
def delete_temp_draft(draft_id):
    """임시저장 문서를 첨부파일까지 깨끗이 지운다."""
    current_user = session.get('user_name')
    conn = get_db()
    try:
        doc = conn.execute("SELECT * FROM approvals WHERE id=?", (draft_id,)).fetchone()
        if not doc:
            return jsonify({"status": "error", "message": "문서를 찾을 수 없습니다."}), 404
        if str(doc['status'] or '').strip() != TEMP_STATUS \
                or str(doc['drafter'] or '').strip() != current_user:
            return jsonify({"status": "error", "message": "본인이 임시저장한 문서만 삭제할 수 있습니다."}), 403
        paths = [entry['path'] for entry in attachment_columns(doc)]
        conn.execute("DELETE FROM approvals WHERE id=? AND drafter=? AND status=?",
                     (draft_id, current_user, TEMP_STATUS))
        conn.commit()
    finally:
        conn.close()

    for path in paths:
        delete_file(path)
    return jsonify({"status": "success", "message": "임시저장 문서를 삭제했습니다."})

@approval_bp.route('/action/<int:doc_id>', methods=['POST'])
def approval_action(doc_id):
    ensure_schema()
    current_user = session.get('user_name')
    action = request.json.get('action')
    
    conn = get_db()
    doc = conn.execute("SELECT * FROM approvals WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        conn.close()
        return jsonify({"status": "error", "message": "문서를 찾을 수 없습니다."}), 404

    expected_approver = None
    if doc['status'] == '대기':
        expected_approver = doc['approver_1']
    elif doc['status'] == '1차승인':
        expected_approver = doc['approver_2']
    if action in {'approve', 'reject'} and current_user != expected_approver:
        conn.close()
        return jsonify({
            "status": "error",
            "message": "현재 단계의 지정 결재자만 승인 또는 반려할 수 있습니다."
        }), 403

    new_status = doc['status']
    msg_receivers = []
    msg_content = ""

    doc_data_dict = json.loads(doc['doc_data']) if doc['doc_data'] else {}
    today_str = datetime.now().strftime('%Y-%m-%d')

    # 결재자가 남긴 참조글. 완료된 문서 맨 아래에 함께 남는다.
    comments = doc_data_dict.get('approval_comments')
    if not isinstance(comments, list):
        comments = []
    comment_text = str(request.json.get('comment', '') or '').strip()[:2000]
    step_label = '1차 결재' if doc['status'] == '대기' else '최종 결재'

    if action == 'reject':
        reason = str(request.json.get('reason', '') or '').strip()
        if not reason:
            conn.close()
            return jsonify({"status": "error", "message": "반려 사유를 입력해주세요."}), 400
        new_status = '반려'
        doc_data_dict['reject_reason'] = reason
        doc_data_dict['rejected_by'] = current_user
        doc_data_dict['rejected_at'] = today_str
        msg_content = f"결재가 반려되었습니다: [{doc['doc_type']}] {doc['title']} (반려자: {current_user}) · 사유: {reason}"
        msg_receivers.append(doc['drafter'])
        if current_user == doc['approver_2'] and doc['approver_1'] != '전결':
            msg_receivers.append(doc['approver_1'])
            
    elif action == 'approve':
        if comment_text:
            comments.append({
                'by': current_user,
                'step': step_label,
                'at': today_str,
                'text': comment_text,
            })
            doc_data_dict['approval_comments'] = comments
        if doc['status'] == '대기' and current_user == doc['approver_1']:
            doc_data_dict['app1_date'] = today_str
            if not doc['approver_2']: 
                new_status = '완료'
                msg_content = f"결재가 최종 승인(전결) 되었습니다: [{doc['doc_type']}] {doc['title']}"
                msg_receivers.append(doc['drafter'])
            else:
                new_status = '1차승인'
                msg_content = f"1차 승인되었습니다. 최종 결재 바랍니다: [{doc['doc_type']}] {doc['title']}"
                msg_receivers.append(doc['approver_2'])
                
        elif doc['status'] == '1차승인' and current_user == doc['approver_2']:
            doc_data_dict['app2_date'] = today_str
            new_status = '완료'
            msg_content = f"결재가 최종 승인되었습니다: [{doc['doc_type']}] {doc['title']}"
            msg_receivers.append(doc['drafter'])
            if doc['approver_1'] != '전결':
                msg_receivers.append(doc['approver_1'])
                
        if new_status == '완료' and dict(doc).get('cc_receivers'):
            for cc in doc['cc_receivers'].split(','):
                if cc.strip(): msg_receivers.append(cc.strip())

    became_complete = doc['status'] != '완료' and new_status == '완료'
    if became_complete:
        # 결재가 끝나는 순간, 그동안 쌓인 참조글을 문서 맨 아래에 넣어 확정한다.
        doc_data_dict['본문'] = append_comment_block(
            doc_data_dict.get('본문'), doc_data_dict.get('approval_comments')
        )
    doc_data_json = json.dumps(doc_data_dict, ensure_ascii=False)
    conn.execute("UPDATE approvals SET status=?, updated_at=CURRENT_TIMESTAMP, doc_data=? WHERE id=?", 
                 (new_status, doc_data_json, doc_id))

    if became_complete:
        sync_completed_vacation(conn, doc, doc_data_dict)

    for rec in msg_receivers:
        send_system_message(conn, rec, msg_content)

    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@approval_bp.route('/detail/<int:doc_id>')
def get_detail(doc_id):
    current_user = session.get('user_name')
    conn = get_db()
    doc = conn.execute("SELECT * FROM approvals WHERE id=?", (doc_id,)).fetchone()
    conn.close()
    if not doc: return jsonify({"error": "Not found"}), 404
    if not can_view_approval(doc, current_user):
        return jsonify({"error": "Forbidden"}), 403
    
    doc_dict = dict(doc)
    # 예전 문서(참조자 줄이 본문에 없던 시절)도 화면에서 참조자를 볼 수 있게 한다.
    doc_dict['cc_list'] = _normalized_approval_names(doc_dict.get('cc_receivers'))
    filename_tokens = [item.strip() for item in str(doc_dict.get('filename') or '').split(',') if item.strip()]
    attachment_paths = [item.strip() for item in str(doc_dict.get('filepath') or '').split(',') if item.strip()]
    size_tokens = [item.strip() for item in str(doc_dict.get('filesize') or '').split(',')]
    doc_dict['attachments'] = [
        {
            'name': decode_filename_token(filename_tokens[index]) if index < len(filename_tokens) else os.path.basename(path),
            'url': f"/approval/attachment/{doc_id}/{index}",
            'size': size_tokens[index] if index < len(size_tokens) else '',
        }
        for index, path in enumerate(attachment_paths)
    ]
    
    # 🚀 과거 작성된 문서 호환: DB에 filesize 정보가 없는 경우 서버 디스크에서 실시간으로 계산해서 전송
    if doc_dict.get('filepath') and not doc_dict.get('filesize'):
        sizes = []
        for fpath in doc_dict['filepath'].split(','):
            fpath = fpath.strip()
            if os.path.exists(fpath):
                try:
                    size_bytes = plaintext_size(fpath)
                    sizes.append(f"{size_bytes / (1024 * 1024):.2f}MB")
                except:
                    sizes.append("0.00MB")
            else:
                sizes.append("0.00MB")
        doc_dict['filesize'] = ','.join(sizes)
        
    return jsonify(doc_dict)


@approval_bp.route('/attachment/<int:doc_id>/<int:file_index>')
def approval_attachment(doc_id, file_index):
    current_user = session.get('user_name')
    conn = get_db()
    doc = conn.execute("SELECT * FROM approvals WHERE id=?", (doc_id,)).fetchone()
    conn.close()
    if not doc:
        abort(404)
    if not can_view_approval(doc, current_user):
        abort(403)
    paths = [item.strip() for item in str(doc['filepath'] or '').split(',') if item.strip()]
    names = [item.strip() for item in str(doc['filename'] or '').split(',') if item.strip()]
    if file_index < 0 or file_index >= len(paths):
        abort(404)
    display_name = decode_filename_token(names[file_index]) if file_index < len(names) else os.path.basename(paths[file_index])
    return encrypted_response(paths[file_index], display_name, as_attachment=True)
