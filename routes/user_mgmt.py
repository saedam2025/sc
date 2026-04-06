from flask import Blueprint, render_template, request, jsonify, url_for, session, redirect
from routes.db_handler import read_excel_db, write_excel_db, OWNER_FILE
import pandas as pd
import base64
import smtplib
import os
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from .database import get_db 

user_mgmt_bp = Blueprint('user_mgmt', __name__)

LEVEL_MAP = {
    "대표이사": 1, "이사": 2, "실장": 3, "팀장": 4, "사원": 5,
    "센터장": 6, "전담코디": 7, "안전코디": 8, "계약직": 9, "임시회원": 10
}

GROUP_CODE_MAP = {
    "대표이사": "01", "이사": "02", "실장": "02", "팀장": "03", "사원": "03",
    "센터장": "04", "전담코디": "05", "안전코디": "05", "계약직": "05", "임시회원": "00"
}

def generate_sd_emp_no(conn, position):
    group_code = GROUP_CODE_MAP.get(position, "05")
    prefix = f"sd{group_code}"
    row = conn.execute("SELECT emp_no FROM users WHERE emp_no LIKE ? ORDER BY emp_no DESC LIMIT 1", (f"{prefix}%",)).fetchone()
    if not row or not row['emp_no']: return f"{prefix}001"
    last_no_str = row['emp_no'][-3:]
    next_no = int(last_no_str) + 1
    return f"{prefix}{next_no:03d}"

def send_real_email(target_email, invite_link):
    SMTP_SERVER = "smtp.gmail.com"
    SMTP_PORT = 587
    SENDER_EMAIL = os.environ.get('MAIL_USERNAME')
    SENDER_PASSWORD = os.environ.get('MAIL_PASSWORD')

    if not SENDER_EMAIL or not SENDER_PASSWORD: return False

    msg = MIMEMultipart()
    msg['From'] = f"새담 인트라넷 <{SENDER_EMAIL}>"
    msg['To'] = target_email
    msg['Subject'] = "[새담 인트라넷] 회원 가입 초대장"

    body = f"""
    <div style="font-family: sans-serif; max-width: 500px; margin: 0 auto; border: 1px solid #ddd; padding: 25px; border-radius: 15px;">
        <h2 style="color: #4a90e2; text-align: center;">새담 인트라넷 초대</h2>
        <p>안녕하세요. 새담 청소년 교육문화원입니다. 가입을 위한 보안 링크를 보내드립니다.</p>
        <div style="text-align: center; margin: 25px 0;">
            <a href="{invite_link}" target="_blank" style="background: #4a90e2; color: white; padding: 12px 25px; text-decoration: none; border-radius: 8px; font-weight: bold;">가입 신청하기</a>
        </div>
    </div>
    """
    msg.attach(MIMEText(body, 'html'))
    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, target_email, msg.as_string())
        server.quit()
        return True
    except:
        return False

@user_mgmt_bp.route('/')
def index():
    return render_template('user_list.html')

@user_mgmt_bp.route('/invite_page/<token>')
def invite_page(token):
    try:
        email = base64.b64decode(token).decode('utf-8')
        return render_template('user_list.html', invite_email=email, mode='invite')
    except:
        return "유효하지 않은 링크입니다.", 403

@user_mgmt_bp.route('/send_invite', methods=['POST'])
def send_invite():
    try:
        data = request.json
        email = data.get('email')
        token = base64.b64encode(email.encode('utf-8')).decode('utf-8')
        invite_link = url_for('user_mgmt.invite_page', token=token, _external=True)
        if send_real_email(email, invite_link):
            return jsonify({"status": "success", "message": "초대 메일이 발송되었습니다."})
        return jsonify({"status": "error", "message": "발송 실패 (서버 설정을 확인하세요)"}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@user_mgmt_bp.route('/register', methods=['POST'])
def register():
    try:
        data = request.json
        conn = get_db()
        dup = conn.execute("SELECT id FROM users WHERE name=? AND rrn=?", (data['name'], data.get('rrn', ''))).fetchone()
        if dup:
            conn.close()
            return jsonify({"status": "error", "message": "이미 가입된 사용자입니다."}), 400

        icon = data.get('profile_icon', '👤')
        conn.execute('''
            INSERT INTO users (name, password, position, level, rrn, email, phone, status, profile_icon)
            VALUES (?, ?, ?, ?, ?, ?, ?, '대기', ?)
        ''', (data['name'], str(data['password']), data['position'], 10, data.get('rrn', ''), data.get('email', ''), data.get('phone', ''), icon))
        
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": "가입 신청이 완료되었습니다."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@user_mgmt_bp.route('/approve', methods=['POST'])
def approve():
    try:
        data = request.json
        user_id = int(data['user_idx'])
        pos = data['approved_position']
        
        conn = get_db()
        emp_no = generate_sd_emp_no(conn, pos)
        join_date = datetime.now().strftime('%Y-%m-%d')
        level = LEVEL_MAP.get(pos, 10)
        
        conn.execute("UPDATE users SET emp_no=?, position=?, level=?, status='승인', join_date=? WHERE id=?", 
                     (emp_no, pos, level, join_date, user_id))
        conn.commit()
        conn.close()
        
        return jsonify({"status": "success", "message": f"승인 완료! (사번: {emp_no})"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@user_mgmt_bp.route('/retire', methods=['POST'])
def retire_user():
    try:
        data = request.json
        user_id = int(data['user_idx'])
        retire_date = datetime.now().strftime('%Y-%m-%d')
        
        conn = get_db()
        conn.execute("UPDATE users SET retire_date=? WHERE id=?", (retire_date, user_id))
        conn.commit()
        conn.close()
        
        return jsonify({"status": "success", "message": "퇴사 처리가 완료되었습니다."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@user_mgmt_bp.route('/update', methods=['POST'])
def update_user():
    try:
        data = request.json
        user_id = int(data['user_idx'])
        
        conn = get_db()
        conn.execute("UPDATE users SET position=?, level=?, phone=?, email=? WHERE id=?", 
                     (data['position'], int(data['level']), data['phone'], data['email'], user_id))
        conn.commit()
        conn.close()
        
        return jsonify({"status": "success", "message": "정보 수정 완료"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@user_mgmt_bp.route('/delete', methods=['POST'])
def delete_user():
    try:
        data = request.json
        user_id = int(data['user_idx'])
        
        conn = get_db()
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        conn.commit()
        conn.close()
        
        return jsonify({"status": "success", "message": "삭제 완료"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@user_mgmt_bp.route('/list')
def get_user_list():
    conn = get_db()
    users = conn.execute("SELECT * FROM users").fetchall()
    conn.close()
    
    result = []
    for u in users:
        icon = u['profile_icon'] if 'profile_icon' in u.keys() and u['profile_icon'] else '👤'
        result.append({
            "id": u['id'], "사번": u['emp_no'] or '', "이름": u['name'] or '',
            "직급": u['position'] or '', "레벨": u['level'] or 10, "주민번호": u['rrn'] or '',
            "이메일": u['email'] or '', "전화번호": u['phone'] or '', "입사일": u['join_date'] or '',
            "퇴사일": u['retire_date'] or '', "승인상태": u['status'] or '', "아이콘": icon
        })
    return jsonify(result)