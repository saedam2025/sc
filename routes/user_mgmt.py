from flask import Blueprint, render_template, request, jsonify, url_for, session, redirect, send_from_directory
from werkzeug.utils import secure_filename
from routes.db_handler import read_excel_db, write_excel_db, OWNER_FILE
import pandas as pd
import base64
import smtplib
import os
import platform
import re
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from .database import get_db 

user_mgmt_bp = Blueprint('user_mgmt', __name__)

# =====================================================================
# [사진 저장 경로 설정 복구]
# 윈도우는 현재폴더/id, 렌더 서버는 /mnt/data/id 에 영구 저장합니다.
# =====================================================================
if platform.system() == 'Windows':
    BASE_DIR = os.getcwd() 
else:
    BASE_DIR = '/mnt/data' if os.path.exists('/mnt/data') else os.getcwd()

PROFILE_ROOT = os.path.join(BASE_DIR, 'id')
# =====================================================================

LEVEL_MAP = {
    "최고관리자": 0, "대표이사": 1, "이사": 2, "실장": 3, "팀장": 4, "사원": 5,
    "계약직": 6, "센터장(팀장)": 7, "센터장": 8, "전담코디": 9, "보조코디": 10, "안전코디": 11,
    "방과후강사": 12, "맞춤형강사": 13, "임시회원": 14
}

GROUP_CODE_MAP = {
    "최고관리자": 0, "대표이사": 1, "이사": 2, "실장": 3, "팀장": 4, "사원": 5,
    "계약직": 6, "센터장(팀장)": 7, "센터장": 8, "전담코디": 9, "보조코디": 10, "안전코디": 11,
    "방과후강사": 12, "맞춤형강사": 13, "임시회원": 14
}

def generate_sd_emp_no(conn, position):
    # 정수로 변경된 GROUP_CODE_MAP에 맞춰서 두 자리 문자열로 자동 변환 (예: 5 -> "05")
    # 등록되지 않은 직급일 경우 기본값을 14(임시회원)로 처리합니다.
    group_code = GROUP_CODE_MAP.get(position, 14)
    prefix = f"sd{int(group_code):02d}"
    
    row = conn.execute("SELECT emp_no FROM users WHERE emp_no LIKE ? ORDER BY emp_no DESC LIMIT 1", (f"{prefix}%",)).fetchone()
    if not row or not row['emp_no']: return f"{prefix}001"
    last_no_str = row['emp_no'][-3:]
    next_no = int(last_no_str) + 1
    return f"{prefix}{next_no:03d}"

def send_real_email(target_email, invite_link):
    SMTP_SERVER = "smtp.gmail.com"
    SMTP_PORT = 587
    SENDER_EMAIL = os.environ.get('MAIL_USERNAME') or "lunch9797@gmail.com"
    SENDER_PASSWORD = os.environ.get('MAIL_PASSWORD') or "txnbofpijgysjpfq"

    if not SENDER_EMAIL or not SENDER_PASSWORD: return False

    msg = MIMEMultipart()
    msg['From'] = f"새담 인트라넷 <{SENDER_EMAIL}>"
    msg['To'] = target_email
    msg['Subject'] = "[새담 인트라넷] 회원 가입 초대장"

    # [수정됨] 이메일 클라이언트(아웃룩, 지메일 등)에서 호환성이 높은 테이블을 사용한 가로 배열 디자인
    body = f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="font-family: sans-serif; max-width: 700px; margin: 0 auto; border: 1px solid #ddd; border-radius: 12px; background-color: #ffffff; border-collapse: separate; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.05);">
        <tr>
            <td style="padding: 30px; vertical-align: middle;">
                <h2 style="color: #4a90e2; margin: 0 0 10px 0; font-size: 22px;">새담 인트라넷 초대</h2>
                <p style="margin: 0; color: #555; font-size: 15px; line-height: 1.5;">안녕하세요. 새담 청소년 교육문화원입니다.<br>가입을 위한 보안 링크를 보내드립니다. 우측 버튼을 클릭하여 진행해주세요.</p>
            </td>
            <td style="padding: 30px; text-align: right; vertical-align: middle; width: 160px; background-color: #f8fbff; border-left: 1px solid #eee;">
                <a href="{invite_link}" target="_blank" style="display: inline-block; background: #4a90e2; color: white; padding: 14px 24px; text-decoration: none; border-radius: 8px; font-weight: bold; white-space: nowrap; font-size: 15px; box-shadow: 0 2px 4px rgba(74, 144, 226, 0.3);">가입 신청하기</a>
            </td>
        </tr>
    </table>
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
    try:
        conn = get_db()
        # 1. 사번 admin인 계정이 있는지 확인
        admin = conn.execute("SELECT id FROM users WHERE emp_no = 'admin'").fetchone()
        conn.close()
        
        # 관리자 계정이 없다면 최초 설정 모드로 렌더링
        if not admin:
            return render_template('user_list.html', mode='admin_setup')
            
    except Exception as e:
        print(f"Admin 자동 생성 오류: {e}")

    return render_template('user_list.html')

# 🚀 신규 추가: 인트라넷 최초 구동 시 관리자 비밀번호 입력 라우트
@user_mgmt_bp.route('/setup_admin', methods=['POST'])
def setup_admin():
    try:
        data = request.json
        password = data.get('password')
        if not password:
            return jsonify({"status": "error", "message": "비밀번호를 입력해주세요."}), 400
            
        conn = get_db()
        today = datetime.now().strftime('%Y-%m-%d')
        conn.execute('''
            INSERT INTO users (emp_no, name, password, position, level, rrn, email, status, join_date, profile_icon, department)
            VALUES ('admin', 'admin', ?, '최고관리자', 1, '-', 'admin@admin.com', '승인', ?, '👑', '본부')
        ''', (password, today))
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": "최고관리자 계정이 성공적으로 설정되었습니다."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

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
        data = request.form
        profile_file = request.files.get('profile_image')
        
        password = data.get('password')
        password_confirm = data.get('password_confirm')
        if password and password_confirm and password != password_confirm:
            return jsonify({"status": "error", "message": "비밀번호가 일치하지 않습니다."}), 400

        conn = get_db()
        # 주민번호(RRN)는 민감 정보 보호 원칙에 따라 digits를 출력하지 않고 generic placeholder를 사용하거나 처리를 우회합니다.
        dup = conn.execute("SELECT id FROM users WHERE name=? AND rrn=?", (data.get('name'), data.get('rrn', ''))).fetchone()
        if dup:
            conn.close()
            return jsonify({"status": "error", "message": "이미 가입된 사용자입니다."}), 400

        profile_path = None
        if profile_file and profile_file.filename != '':
            os.makedirs(PROFILE_ROOT, exist_ok=True)
            ext = os.path.splitext(profile_file.filename)[1]
            # 안전한 파일명 생성
            raw_filename = f"id_{data.get('name')}_{datetime.now().strftime('%Y%m%d%H%M%S')}{ext}"
            safe_filename = secure_filename(raw_filename)
            upload_path = os.path.join(PROFILE_ROOT, safe_filename)
            profile_file.save(upload_path)
            
            # HTML에서 이미지를 불러올 라우트 주소
            profile_path = f"/user/profile_img/{safe_filename}"

        icon = data.get('profile_icon', '👤')
        conn.execute('''
            INSERT INTO users (name, password, position, level, rrn, email, phone, 
                               address, department, bank_account, profile_path, status, profile_icon)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '대기', ?)
        ''', (data.get('name'), str(password), data.get('position'), 10, data.get('rrn', ''), 
              data.get('email', ''), data.get('phone', ''), data.get('address', ''), 
              data.get('department', ''), data.get('bank_account', ''), profile_path, icon))
        
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
        data = request.form
        user_id = int(data.get('user_idx', 0))
        profile_file = request.files.get('profile_image')
        
        conn = get_db()
        
        if profile_file and profile_file.filename != '':
            os.makedirs(PROFILE_ROOT, exist_ok=True)
            ext = os.path.splitext(profile_file.filename)[1]
            raw_filename = f"id_update_{user_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}{ext}"
            safe_filename = secure_filename(raw_filename)
            upload_path = os.path.join(PROFILE_ROOT, safe_filename)
            profile_file.save(upload_path)
            profile_path = f"/user/profile_img/{safe_filename}"
            conn.execute("UPDATE users SET profile_path=? WHERE id=?", (profile_path, user_id))

        # 새로 입력받은 비밀번호 (앞뒤 공백 제거)
        new_password = data.get('password', '').strip()
        
        if new_password:
            # 1. 새 비밀번호가 입력된 경우 (비밀번호 포함 전체 업데이트)
            conn.execute("""
                UPDATE users 
                SET password=?, position=?, level=?, phone=?, email=?, 
                    address=?, department=?, bank_account=?, profile_icon=?
                WHERE id=?
            """, (
                new_password, data.get('position'), int(data.get('level', 10)), 
                data.get('phone', ''), data.get('email', ''), data.get('address', ''), 
                data.get('department', ''), data.get('bank_account', ''), data.get('profile_icon', '👤'), user_id
            ))
        else:
            # 2. 비밀번호 칸을 비워둔 경우 (기존 비밀번호는 유지하고 나머지만 업데이트)
            conn.execute("""
                UPDATE users 
                SET position=?, level=?, phone=?, email=?, 
                    address=?, department=?, bank_account=?, profile_icon=?
                WHERE id=?
            """, (
                data.get('position'), int(data.get('level', 10)), 
                data.get('phone', ''), data.get('email', ''), data.get('address', ''), 
                data.get('department', ''), data.get('bank_account', ''), data.get('profile_icon', '👤'), user_id
            ))
        
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
    users = conn.execute("SELECT * FROM users ORDER BY level ASC, id ASC").fetchall()
    conn.close()
    
    # 🚀 수정 포인트: 세션을 통해 현재 로그인한 사용자가 '최고관리자'인지 판별
    current_emp_no = session.get('emp_no', '') 
    is_admin_logged_in = (current_emp_no == 'admin')
    
    result = []
    for u in users:
        # 🚀 수정 포인트: Admin 계정은 최고관리자로 로그인했을 때만 명단에 포함
        if u['emp_no'] == 'admin' and not is_admin_logged_in:
            continue
            
        icon = u['profile_icon'] if 'profile_icon' in u.keys() and u['profile_icon'] else '👤'
        profile_path = u['profile_path'] if 'profile_path' in u.keys() else None
        result.append({
            "id": u['id'], "사번": u['emp_no'] or '', "이름": u['name'] or '',
            "직급": u['position'] or '', "레벨": u['level'] or 10, "주민번호": u['rrn'] or '',
            "비밀번호": u['password'] or '', 
            "이메일": u['email'] or '', "전화번호": u['phone'] or '', 
            "주소": u['address'] if 'address' in u.keys() else '',
            "소속": u['department'] if 'department' in u.keys() else '',
            "계좌": u['bank_account'] if 'bank_account' in u.keys() else '',
            "입사일": u['join_date'] or '', "퇴사일": u['retire_date'] or '', 
            "승인상태": u['status'] or '', "아이콘": icon, "profile_path": profile_path
        })
    return jsonify(result)

# ==============================================================================
# [필수 라우트 복구] 외부 폴더(/mnt/data/id/)에 저장된 이미지를 불러옵니다.
# ==============================================================================
@user_mgmt_bp.route('/profile_img/<filename>')
def serve_profile_image(filename):
    return send_from_directory(PROFILE_ROOT, filename)