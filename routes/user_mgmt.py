from flask import Blueprint, render_template, request, jsonify, url_for
from .db_handler import read_excel_db, write_excel_db, OWNER_FILE
import pandas as pd
import base64
import smtplib
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

user_mgmt_bp = Blueprint('user_mgmt', __name__)

# 직급별 권한 레벨 정의
LEVEL_MAP = {
    "대표이사": 1, "이사": 2, "실장": 3, "팀장": 4, "사원": 5,
    "센터장": 6, "전담코디": 7, "안전코디": 8, "계약직": 9, "임시회원": 10
}

# 실제 메일 발송 함수 (Render 환경변수 사용)
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
    <div style="font-family: sans-serif; max-width: 500px; margin: 0 auto; border: 1px solid #ddd; padding: 20px; border-radius: 10px;">
        <h2 style="color: #4a90e2; text-align: center;">새담 인트라넷 초대</h2>
        <p>새담 청소년 교육문화원 인트라넷 가입을 위한 전용 링크입니다.</p>
        <div style="text-align: center; margin: 20px 0;">
            <a href="{invite_link}" target="_blank" style="background: #4a90e2; color: white; padding: 12px 25px; text-decoration: none; border-radius: 5px; font-weight: bold;">가입 신청하기</a>
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
        return jsonify({"status": "error", "message": "메일 발송 실패"}), 500
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
                return jsonify({"status": "error", "message": "이미 가입된 정보입니다."}), 400

        new_user = pd.DataFrame([{
            '이름': data['name'], '암호': str(data['password']), '직급': data['position'],
            '레벨': 10, '주민번호': data.get('rrn', ''), '이메일': data.get('email', ''),
            '전화번호': data.get('phone', ''), '승인상태': '대기'
        }])
        df = pd.concat([df, new_user], ignore_index=True)
        write_excel_db(df, OWNER_FILE)
        return jsonify({"status": "success", "message": "가입 신청 완료!"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@user_mgmt_bp.route('/list')
def get_user_list():
    df = read_excel_db(OWNER_FILE)
    return jsonify(df.to_dict(orient='records') if not df.empty else [])

@user_mgmt_bp.route('/approve', methods=['POST'])
def approve():
    try:
        data = request.json
        df = read_excel_db(OWNER_FILE)
        idx = int(data['user_idx'])
        pos = data['approved_position']
        df.at[idx, '직급'] = pos
        df.at[idx, '레벨'] = LEVEL_MAP.get(pos, 10)
        df.at[idx, '승인상태'] = '승인'
        write_excel_db(df, OWNER_FILE)
        return jsonify({"status": "success", "message": "승인 완료"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@user_mgmt_bp.route('/update', methods=['POST'])
def update_user():
    try:
        data = request.json
        df = read_excel_db(OWNER_FILE)
        idx = int(data['user_idx'])
        
        # 수정사항 반영: 이름, 주민번호 제외 업데이트
        df.at[idx, '직급'] = data['position']
        df.at[idx, '레벨'] = int(data['level'])
        df.at[idx, '전화번호'] = data['phone']
        df.at[idx, '이메일'] = data['email']
        
        write_excel_db(df, OWNER_FILE)
        return jsonify({"status": "success", "message": "회원 정보가 성공적으로 수정되었습니다."})
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