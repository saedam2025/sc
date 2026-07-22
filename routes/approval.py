from flask import Blueprint, render_template, request, jsonify, session
import os
import json
import time
from datetime import datetime, timedelta
from .database import get_db

approval_bp = Blueprint('approval', __name__)
UPLOAD_FOLDER = '/mnt/data/uploads'
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

    draft_rows = conn.execute("SELECT * FROM approvals WHERE drafter = ? ORDER BY created_at DESC", (current_user,)).fetchall()
    my_drafts = rows_to_dicts(draft_rows)

    completed_rows = conn.execute('''
        SELECT * FROM approvals
        WHERE status = '완료'
          AND (drafter = ? OR approver_1 = ? OR approver_2 = ? OR receivers LIKE ? OR cc_receivers LIKE ?)
        ORDER BY updated_at DESC
    ''', (current_user, current_user, current_user, f'%{current_user}%', f'%{current_user}%')).fetchall()
    completed_docs = rows_to_dicts(completed_rows)

    archive_rows = conn.execute('''
        SELECT * FROM approvals
        WHERE status = '완료'
        ORDER BY updated_at DESC
    ''').fetchall()
    archive_docs = rows_to_dicts(archive_rows)

    db_users = conn.execute("SELECT name, position, level, department FROM users WHERE status='승인' ORDER BY department ASC, level ASC, name ASC").fetchall()
    
    user_list = []
    for row in db_users:
        u = dict(row)
        if u.get('name') != current_user and u.get('name', '').lower() != 'admin':
            pos = u.get('position')
            if not pos: pos = '사원'
            dept = u.get('department')
            if not dept: dept = '소속 없음'
            
            user_list.append({
                'name': u.get('name'), 
                'role': pos,
                'level': u.get('level', 10),
                'dept': dept
            })
            
    user_list.sort(key=lambda x: (x['level'], x['name']))
    
    grouped_users = [
        {'group': '본부', 'users': []},
        {'group': '센터장', 'users': []},
        {'group': '강사', 'users': []}
    ]
    
    for user in user_list:
        pos = user['role']
        if '센터장' in pos:
            grouped_users[1]['users'].append(user)
        elif '강사' in pos:
            grouped_users[2]['users'].append(user)
        else:
            grouped_users[0]['users'].append(user)
            
    grouped_users = [g for g in grouped_users if len(g['users']) > 0]
    
    flat_user_list = []
    for g in grouped_users:
        flat_user_list.extend(g['users'])
        
    conn.close()

    return render_template('approval.html', 
                           current_user=current_user, 
                           pending_docs=pending_docs, 
                           my_drafts=my_drafts, 
                           completed_docs=completed_docs, 
                           archive_docs=archive_docs,
                           user_list=flat_user_list,
                           grouped_users=grouped_users,
                           next_id=next_id)

@approval_bp.route('/submit', methods=['POST'])
def submit_approval():
    ensure_schema()
    current_user = session.get('user_name', '익명')
    doc_type = request.form.get('doc_type')
    title = request.form.get('title')
    doc_data = request.form.get('doc_data', '{}')
    
    approver_1 = request.form.get('approver_1', '')
    approver_2 = request.form.get('approver_2', '')
    receivers = request.form.get('receivers', '')
    cc_receivers = request.form.get('cc_receivers', '')
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

    if doc_type in receiver_doc_types:
        if not receivers.strip():
            return jsonify({"status": "error", "message": "수신자를 최소 1명 이상 지정해주세요."}), 400
    else:
        if not approver_1.strip():
            return jsonify({"status": "error", "message": "1차 결재자는 필수입니다."}), 400
        if approver_2.strip() and approver_1.strip() == approver_2.strip():
            return jsonify({"status": "error", "message": "1차 결재자와 2차 결재자는 같은 사람으로 지정할 수 없습니다."}), 400

    if doc_type in receiver_doc_types:
        status = '완료'
    elif approver_1 == '전결':
        status = '1차승인'
    else:
        status = '대기'

    files = request.files.getlist('file')
    filenames, filepaths, filesizes = [], [], []
    for file in files:
        if file and file.filename:
            fname = file.filename
            safe_filename = f"{int(time.time())}_{fname.replace(' ', '_')}"
            fpath = os.path.join(UPLOAD_FOLDER, safe_filename)
            file.save(fpath)
            
            # 🚀 신규 업로드 시 파일 사이즈 계산 로직 추가
            try:
                size_mb = os.path.getsize(fpath) / (1024 * 1024)
                filesizes.append(f"{size_mb:.2f}MB")
            except Exception:
                filesizes.append("0.00MB")

            filenames.append(fname)
            filepaths.append(fpath)
            
    filename_str = ','.join(filenames)
    filepath_str = ','.join(filepaths)
    filesize_str = ','.join(filesizes)

    conn = get_db()
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
    conn = get_db()
    doc = conn.execute("SELECT * FROM approvals WHERE id=?", (doc_id,)).fetchone()
    conn.close()
    if not doc: return jsonify({"error": "Not found"}), 404
    
    doc_dict = dict(doc)
    
    # 🚀 과거 작성된 문서 호환: DB에 filesize 정보가 없는 경우 서버 디스크에서 실시간으로 계산해서 전송
    if doc_dict.get('filepath') and not doc_dict.get('filesize'):
        sizes = []
        for fpath in doc_dict['filepath'].split(','):
            fpath = fpath.strip()
            if os.path.exists(fpath):
                try:
                    size_bytes = os.path.getsize(fpath)
                    sizes.append(f"{size_bytes / (1024 * 1024):.2f}MB")
                except:
                    sizes.append("0.00MB")
            else:
                sizes.append("0.00MB")
        doc_dict['filesize'] = ','.join(sizes)
        
    return jsonify(doc_dict)
