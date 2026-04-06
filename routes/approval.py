from flask import Blueprint, render_template, request, jsonify, session, send_from_directory
from werkzeug.utils import secure_filename
import os
import json
from datetime import datetime
from .database import get_db

approval_bp = Blueprint('approval', __name__)
UPLOAD_FOLDER = '/mnt/data/uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# 결재 알림을 시스템 쪽지로 자동 발송하는 헬퍼 함수
def send_system_message(conn, receiver, content):
    conn.execute("INSERT INTO messages (sender, receiver, content) VALUES (?, ?, ?)", 
                 ('🔔시스템알림', receiver, content))

@approval_bp.route('/')
def index():
    current_user = session.get('user_name', '배서현')
    conn = get_db()

    # 1. 수신 결재 (내가 결재해야 할 문서)
    pending_docs = conn.execute('''
        SELECT * FROM approvals
        WHERE (approver_1 = ? AND status = '대기')
           OR (approver_2 = ? AND status = '1차승인')
        ORDER BY created_at DESC
    ''', (current_user, current_user)).fetchall()

    # 2. 기안 문서 (내가 상신한 진행중/완료 문서)
    my_drafts = conn.execute("SELECT * FROM approvals WHERE drafter = ? ORDER BY created_at DESC", (current_user,)).fetchall()

    # 3. 결재 완료/반려 보관함 (내가 관련된 모든 종료된 문서)
    completed_docs = conn.execute('''
        SELECT * FROM approvals
        WHERE (status = '완료' OR status = '반려')
          AND (drafter = ? OR approver_1 = ? OR approver_2 = ?)
        ORDER BY updated_at DESC
    ''', (current_user, current_user, current_user)).fetchall()

    # 결재자 지정을 위한 전체 회원 명단 (가입 승인된 사람만)
    db_users = conn.execute("SELECT name FROM users WHERE status='승인'").fetchall()
    user_list = [u['name'] for u in db_users if u['name'] != current_user]
    
    conn.close()

    return render_template('approval.html', 
                           current_user=current_user, 
                           pending_docs=pending_docs, 
                           my_drafts=my_drafts, 
                           completed_docs=completed_docs, 
                           user_list=user_list)

@approval_bp.route('/submit', methods=['POST'])
def submit_approval():
    current_user = session.get('user_name', '익명')
    doc_type = request.form.get('doc_type')
    title = request.form.get('title')
    approver_1 = request.form.get('approver_1')
    approver_2 = request.form.get('approver_2', '') # 없으면 1차 전결
    doc_data = request.form.get('doc_data', '{}') # JSON 텍스트

    file = request.files.get('file')
    filename, filepath = '', ''
    if file and file.filename:
        filename = secure_filename(file.filename)
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)

    conn = get_db()
    conn.execute('''
        INSERT INTO approvals (doc_type, title, drafter, approver_1, approver_2, status, doc_data, filename, filepath)
        VALUES (?, ?, ?, ?, ?, '대기', ?, ?, ?)
    ''', (doc_type, title, current_user, approver_1, approver_2, doc_data, filename, filepath))

    # 1차 결재자에게 쪽지 발송
    send_system_message(conn, approver_1, f"새 결재 문서를 검토해주세요: [{doc_type}] {title}")
    
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "message": "결재 상신이 완료되었습니다."})

@approval_bp.route('/action/<int:doc_id>', methods=['POST'])
def approval_action(doc_id):
    current_user = session.get('user_name')
    action = request.json.get('action') # 'approve' or 'reject'
    
    conn = get_db()
    doc = conn.execute("SELECT * FROM approvals WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        conn.close()
        return jsonify({"status": "error", "message": "문서를 찾을 수 없습니다."}), 404

    new_status = doc['status']
    msg_receivers = []
    msg_content = ""

    if action == 'reject':
        new_status = '반려'
        msg_content = f"결재가 반려되었습니다: [{doc['doc_type']}] {doc['title']} (반려자: {current_user})"
        msg_receivers.append(doc['drafter'])
        if current_user == doc['approver_2']: # 2차 반려시 1차에도 알림
            msg_receivers.append(doc['approver_1'])
            
    elif action == 'approve':
        if doc['status'] == '대기' and current_user == doc['approver_1']:
            if not doc['approver_2']: # 전결 (2차 결재자 없음)
                new_status = '완료'
                msg_content = f"결재가 최종 승인(전결) 되었습니다: [{doc['doc_type']}] {doc['title']}"
                msg_receivers.append(doc['drafter'])
            else: # 2차 결재 진행
                new_status = '1차승인'
                msg_content = f"1차 승인되었습니다. 최종 결재 바랍니다: [{doc['doc_type']}] {doc['title']}"
                msg_receivers.append(doc['approver_2'])
                
        elif doc['status'] == '1차승인' and current_user == doc['approver_2']:
            new_status = '완료'
            msg_content = f"결재가 최종 승인되었습니다: [{doc['doc_type']}] {doc['title']}"
            msg_receivers.extend([doc['drafter'], doc['approver_1']])

    conn.execute("UPDATE approvals SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (new_status, doc_id))
    
    # 쪽지 전파
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