from flask import Blueprint, render_template, request, jsonify, session, abort
import os
import json
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
        WHERE drafter = ? AND status != '완료'
        ORDER BY created_at DESC
    ''', (current_user,)).fetchall()
    my_drafts = rows_to_dicts(draft_rows)

    completed_rows = conn.execute('''
        SELECT * FROM approvals
        WHERE status = '완료'
          AND drafter = ?
        ORDER BY updated_at DESC
    ''', (current_user,)).fetchall()
    completed_docs = rows_to_dicts(completed_rows)

    reference_rows = conn.execute('''
        SELECT * FROM approvals
        WHERE status = '완료'
        ORDER BY updated_at DESC
    ''').fetchall()
    reference_docs = [
        dict(row) for row in reference_rows
        if current_user in _approval_names(row['cc_receivers'])
    ]

    archive_rows = conn.execute('''
        SELECT * FROM approvals
        WHERE status = '완료'
        ORDER BY updated_at DESC
    ''').fetchall()
    archive_docs = [
        dict(row)
        for row in archive_rows
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
                           completed_docs=completed_docs, 
                           reference_docs=reference_docs,
                           archive_docs=archive_docs,
                           approver_users=approver_users,
                           receiver_grouped_users=receiver_grouped_users,
                           reference_users=reference_users,
                           next_id=next_id)

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
    receiver_doc_types = ['보고서', '업무일지', '회의록']

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

    doc_data = json.dumps(doc_data_dict, ensure_ascii=False)

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

    if doc_type in receiver_doc_types:
        status = '완료'
    elif approver_1 == '전결':
        status = '1차승인'
    else:
        status = '대기'

    files = request.files.getlist('file')
    filenames, filepaths, filesizes = [], [], []
    try:
        for file in files:
            if file and file.filename:
                fname = original_filename(file.filename)
                fpath = os.path.join(UPLOAD_FOLDER, encrypted_storage_name(fname))
                size_bytes = encrypt_upload(file, fpath)
                filenames.append(encode_filename_token(fname))
                filepaths.append(fpath)
                filesizes.append(f"{size_bytes / (1024 * 1024):.2f}MB")
    except Exception:
        for fpath in filepaths:
            delete_file(fpath)
        raise
            
    filename_str = ','.join(filenames)
    filepath_str = ','.join(filepaths)
    filesize_str = ','.join(filesizes)

    conn = get_db()
    try:
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
    except Exception:
        conn.rollback()
        for fpath in filepaths:
            delete_file(fpath)
        raise
    finally:
        conn.close()
    return jsonify({"status": "success", "message": "성공적으로 상신되었습니다."})

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

    if action == 'reject':
        new_status = '반려'
        msg_content = f"결재가 반려되었습니다: [{doc['doc_type']}] {doc['title']} (반려자: {current_user})"
        msg_receivers.append(doc['drafter'])
        if current_user == doc['approver_2'] and doc['approver_1'] != '전결':
            msg_receivers.append(doc['approver_1'])
            
    elif action == 'approve':
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
