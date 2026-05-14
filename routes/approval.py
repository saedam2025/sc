from flask import Blueprint, render_template, request, jsonify, session
import os
import json
import time
from datetime import datetime
from .database import get_db

approval_bp = Blueprint('approval', __name__)
UPLOAD_FOLDER = '/mnt/data/uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def send_system_message(conn, receiver, content):
    conn.execute("INSERT INTO messages (sender, receiver, content) VALUES (?, ?, ?)", 
                 ('🔔시스템알림', receiver.strip(), content))

# 데이터베이스 자동 마이그레이션 (수신자 컬럼 추가)
def ensure_schema():
    conn = get_db()
    try:
        conn.execute("ALTER TABLE approvals ADD COLUMN receivers TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        pass # 이미 컬럼이 존재하면 무시
    finally:
        conn.close()

# 에러 해결: Row 객체를 JSON 직렬화 가능한 dict로 변환하는 헬퍼 함수
def rows_to_dicts(rows):
    return [dict(row) for row in rows]

@approval_bp.route('/')
def index():
    ensure_schema() # DB 스키마 패치
    current_user = session.get('user_name', '배서현')
    conn = get_db()

    # 다음 부여될 문서 번호(ID) 계산 (현재 최대 ID + 1)
    max_id_row = conn.execute("SELECT MAX(id) as max_id FROM approvals").fetchone()
    next_id = (max_id_row['max_id'] or 0) + 1

    # 1. 내 결재함 (수신) - 결재해야 할 문서
    pending_rows = conn.execute('''
        SELECT * FROM approvals
        WHERE (approver_1 = ? AND status = '대기')
           OR (approver_2 = ? AND status = '1차승인')
        ORDER BY created_at DESC
    ''', (current_user, current_user)).fetchall()
    pending_docs = rows_to_dicts(pending_rows)

    # 2. 기안 진행함 (발신) - 내가 쓴 문서
    draft_rows = conn.execute("SELECT * FROM approvals WHERE drafter = ? ORDER BY created_at DESC", (current_user,)).fetchall()
    my_drafts = rows_to_dicts(draft_rows)

    # 3. 결재 완료함 (보관) - 결재/수신 완료된 내역
    completed_rows = conn.execute('''
        SELECT * FROM approvals
        WHERE (status = '완료' OR status = '반려')
          AND (drafter = ? OR approver_1 = ? OR approver_2 = ? OR receivers LIKE ?)
        ORDER BY updated_at DESC
    ''', (current_user, current_user, current_user, f'%{current_user}%')).fetchall()
    completed_docs = rows_to_dicts(completed_rows)

    db_users = conn.execute("SELECT name FROM users WHERE status='승인'").fetchall()
    user_list = [u['name'] for u in db_users if u['name'] != current_user]
    conn.close()

    return render_template('approval.html', 
                           current_user=current_user, 
                           pending_docs=pending_docs, 
                           my_drafts=my_drafts, 
                           completed_docs=completed_docs, 
                           user_list=user_list,
                           next_id=next_id) # 템플릿으로 다음 번호 전달

@approval_bp.route('/submit', methods=['POST'])
def submit_approval():
    current_user = session.get('user_name', '익명')
    doc_type = request.form.get('doc_type')
    title = request.form.get('title')
    doc_data = request.form.get('doc_data', '{}')
    
    approver_1 = request.form.get('approver_1', '')
    approver_2 = request.form.get('approver_2', '')
    receivers = request.form.get('receivers', '')

    if doc_type in ['보고서', '업무일지', '회의록']:
        status = '완료'
    elif approver_1 == '전결':
        status = '1차승인'
    else:
        status = '대기'

    file = request.files.get('file')
    filename, filepath = '', ''
    if file and file.filename:
        filename = file.filename
        safe_filename = f"{int(time.time())}_{filename.replace(' ', '_')}"
        filepath = os.path.join(UPLOAD_FOLDER, safe_filename)
        file.save(filepath)

    conn = get_db()
    conn.execute('''
        INSERT INTO approvals (doc_type, title, drafter, approver_1, approver_2, receivers, status, doc_data, filename, filepath)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (doc_type, title, current_user, approver_1, approver_2, receivers, status, doc_data, filename, filepath))

    if status == '대기' and approver_1:
        send_system_message(conn, approver_1, f"새 결재를 검토해주세요: [{doc_type}] {title}")
    elif status == '1차승인' and approver_2:
        send_system_message(conn, approver_2, f"새 결재를 검토해주세요 (전결 상신): [{doc_type}] {title}")
    elif status == '완료' and receivers:
        for rec in receivers.split(','):
            if rec.strip(): send_system_message(conn, rec.strip(), f"새 수신 문서가 도착했습니다: [{doc_type}] {title}")
    
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "message": "성공적으로 상신되었습니다."})

@approval_bp.route('/action/<int:doc_id>', methods=['POST'])
def approval_action(doc_id):
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

    doc_data_json = json.dumps(doc_data_dict, ensure_ascii=False)
    conn.execute("UPDATE approvals SET status=?, updated_at=CURRENT_TIMESTAMP, doc_data=? WHERE id=?", 
                 (new_status, doc_data_json, doc_id))
    
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
    return jsonify(dict(doc))