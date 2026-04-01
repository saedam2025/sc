from flask import Blueprint, render_template, request, jsonify, url_for, session, redirect
from .db_handler import read_excel_db, write_excel_db, OWNER_FILE
import pandas as pd
import base64
import smtplib
import os
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

user_mgmt_bp = Blueprint('user_mgmt', __name__)

# 직급별 권한 레벨 정의
LEVEL_MAP = {
    "대표이사": 1, "이사": 2, "실장": 3, "팀장": 4, "사원": 5,
    "센터장": 6, "전담코디": 7, "안전코디": 8, "계약직": 9, "임시회원": 10
}

# 사번 그룹 코드 정의
GROUP_CODE_MAP = {
    "대표이사": "01",
    "이사": "02", "실장": "02",
    "팀장": "03", "사원": "03",
    "센터장": "04",
    "전담코디": "05", "안전코디": "05", "계약직": "05",
    "임시회원": "00"
}

# 사번 생성 함수 (sd + 그룹코드 + 3자리 순번)
def generate_sd_emp_no(df, position):
    group_code = GROUP_CODE_MAP.get(position, "05")
    prefix = f"sd{group_code}"
    
    if df.empty or '사번' not in df.columns:
        return f"{prefix}001"
    
    group_emps = df[df['사번'].astype(str).str.startswith(prefix)]
    
    if group_emps.empty:
        return f"{prefix}001"
    
    # 마지막 순번 추출 후 1 증가
    last_no_str = group_emps['사번'].astype(str).max()[-3:]
    next_no = int(last_no_str) + 1
    return f"{prefix}{next_no:03d}"

# 로그인 페이지 렌더링
@user_mgmt_bp.route('/login_page')
def login_page():
    return render_template('login.html')

# 로그인 처리
@user_mgmt_bp.route('/login', methods=['POST'])
def login():
    try:
        data = request.json
        emp_no = data.get('emp_no')
        password = data.get('password')
        
        df = read_excel_db(OWNER_FILE)
        if df.empty:
            return jsonify({"status": "error", "message": "사용자 정보가 존재하지 않습니다."}), 404

        # 사번과 비밀번호 일치 여부 확인
        user = df[(df['사번'].astype(str) == str(emp_no)) & (df['암호'].astype(str) == str(password))]
        
        if not user.empty:
            u_info = user.iloc[0]
            if u_info['승인상태'] != '승인':
                return jsonify({"status": "error", "message": "승인 대기 중인 계정입니다."}), 403
            
            # 세션에 로그인 정보 기록
            session['emp_no'] = str(u_info['사번'])
            session['user_name'] = u_info['이름']
            session['user_level'] = int(u_info['레벨'])
            return jsonify({"status": "success"})
        
        return jsonify({"status": "error", "message": "사번 또는 비밀번호가 올바르지 않습니다."}), 401
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# 로그아웃 처리
@user_mgmt_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('user_mgmt.login_page'))

def send_real_email(target_email, invite_link):
    SMTP_SERVER = "smtp.gmail.com"
    SMTP_PORT = 587
    SENDER_EMAIL = os.environ.get('MAIL_USERNAME')
    SENDER_PASSWORD = os.environ.get('MAIL_PASSWORD')

    if not SENDER_EMAIL or not SENDER_PASSWORD:
        return False

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
        return jsonify({"status": "error", "message": "발송 실패"}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@user_mgmt_bp.route('/register', methods=['POST'])
def register():
    try:
        data = request.json
        df = read_excel_db(OWNER_FILE)
        if not df.empty:
            dup = df[(df['이름'] == data['name']) & (df['주민번호'] == data.get('rrn', ''))]
            if not dup.empty:
                return jsonify({"status": "error", "message": "이미 가입된 사용자입니다."}), 400

        new_user = pd.DataFrame([{
            '사번': '',
            '이름': data['name'], '암호': str(data['password']), '직급': data['position'],
            '레벨': 10, '주민번호': data.get('rrn', ''), '이메일': data.get('email', ''),
            '전화번호': data.get('phone', ''), '입사일': '', '퇴사일': '', '승인상태': '대기'
        }])
        df = pd.concat([df, new_user], ignore_index=True)
        write_excel_db(df, OWNER_FILE)
        return jsonify({"status": "success", "message": "가입 신청이 완료되었습니다."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@user_mgmt_bp.route('/approve', methods=['POST'])
def approve():
    try:
        data = request.json
        df = read_excel_db(OWNER_FILE)
        idx = int(data['user_idx'])
        pos = data['approved_position']
        
        # 승인 시 사번 생성 및 부여
        df.at[idx, '사번'] = generate_sd_emp_no(df, pos)
        df.at[idx, '직급'] = pos
        df.at[idx, '레벨'] = LEVEL_MAP.get(pos, 10)
        df.at[idx, '승인상태'] = '승인'
        df.at[idx, '입사일'] = datetime.now().strftime('%Y-%m-%d')
        
        write_excel_db(df, OWNER_FILE)
        return jsonify({"status": "success", "message": f"승인 완료! (사번: {df.at[idx, '사번']})"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@user_mgmt_bp.route('/retire', methods=['POST'])
def retire_user():
    try:
        data = request.json
        df = read_excel_db(OWNER_FILE)
        idx = int(data['user_idx'])
        df.at[idx, '퇴사일'] = datetime.now().strftime('%Y-%m-%d')
        write_excel_db(df, OWNER_FILE)
        return jsonify({"status": "success", "message": "퇴사 처리가 완료되었습니다."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@user_mgmt_bp.route('/update', methods=['POST'])
def update_user():
    try:
        data = request.json
        df = read_excel_db(OWNER_FILE)
        idx = int(data['user_idx'])
        df.at[idx, '직급'] = data['position']
        df.at[idx, '레벨'] = int(data['level'])
        df.at[idx, '전화번호'] = data['phone']
        df.at[idx, '이메일'] = data['email']
        write_excel_db(df, OWNER_FILE)
        return jsonify({"status": "success", "message": "정보 수정 완료"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@user_mgmt_bp.route('/delete', methods=['POST'])
def delete_user():
    try:
        data = request.json
        df = read_excel_db(OWNER_FILE)
        idx = int(data['user_idx'])
        df = df.drop(df.index[idx]).reset_index(drop=True)
        write_excel_db(df, OWNER_FILE)
        return jsonify({"status": "success", "message": "삭제 완료"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@user_mgmt_bp.route('/list')
def get_user_list():
    df = read_excel_db(OWNER_FILE)
    return jsonify(df.to_dict(orient='records') if not df.empty else [])