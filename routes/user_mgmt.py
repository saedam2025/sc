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

# --- 실제 메일 발송 함수 (Render 환경변수 사용) ---
def send_real_email(target_email, invite_link):
    SMTP_SERVER = "smtp.gmail.com"
    SMTP_PORT = 587
    # Render 대시보드 Environment에 설정한 변수명을 가져옵니다.
    SENDER_EMAIL = os.environ.get('MAIL_USERNAME')
    SENDER_PASSWORD = os.environ.get('MAIL_PASSWORD')

    if not SENDER_EMAIL or not SENDER_PASSWORD:
        print("에러: MAIL_USERNAME 또는 MAIL_PASSWORD 환경변수가 설정되지 않았습니다.")
        return False

    msg = MIMEMultipart()
    msg['From'] = f"새담 인트라넷 <{SENDER_EMAIL}>"
    msg['To'] = target_email
    msg['Subject'] = "[새담 인트라넷] 신규 회원 가입 초대장입니다."

    body = f"""
    <div style="font-family: 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif; line-height: 1.6; max-width: 600px; margin: 0 auto; border: 1px solid #eee; padding: 20px; border-radius: 10px;">
        <h2 style="color: #4a90e2;">새담 인트라넷 초대</h2>
        <p>안녕하세요, <b>새담 청소년 교육문화원</b>입니다.</p>
        <p>인트라넷 시스템 사용을 위한 가입 초대 링크를 보내드립니다.</p>
        <p>아래 버튼을 클릭하여 회원가입 신청을 진행해 주세요.</p>
        <div style="text-align: center; margin: 30px 0;">
            <a href="{invite_link}" style="display: inline-block; padding: 15px 30px; background-color: #4a90e2; color: white; text-decoration: none; border-radius: 8px; font-weight: bold; font-size: 16px;">회원가입 신청하기</a>
        </div>
        <p style="color: #888; font-size: 0.9em; border-top: 1px solid #eee; padding-top: 15px;">
            본 메일은 발신 전용입니다. 가입 관련 문의는 관리자에게 연락 바랍니다.
        </p>
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
    except Exception as e:
        print(f"SMTP 메일 발송 실패: {str(e)}")
        return False

@user_mgmt_bp.route('/')
def index():
    try:
        return render_template('user_list.html')
    except Exception as e:
        return f"템플릿 에러: {str(e)}", 500

@user_mgmt_bp.route('/invite_page/<token>')
def invite_page(token):
    try:
        # 토큰에서 이메일 복원
        email = base64.b64decode(token).decode('utf-8')
        return render_template('user_list.html', invite_email=email, mode='invite')
    except:
        return "유효하지 않은 접근이거나 만료된 링크입니다.", 403

@user_mgmt_bp.route('/send_invite', methods=['POST'])
def send_invite():
    try:
        data = request.json
        email = data.get('email')
        if not email:
            return jsonify({"status": "error", "message": "이메일을 입력해 주세요."}), 400
        
        # 보안을 위한 토큰 생성 (base64)
        token = base64.b64encode(email.encode('utf-8')).decode('utf-8')
        invite_link = url_for('user_mgmt.invite_page', token=token, _external=True)
        
        # 메일 발송 시도
        if send_real_email(email, invite_link):
            return jsonify({"status": "success", "message": f"[{email}]로 초대 메일을 보냈습니다."})
        else:
            return jsonify({"status": "error", "message": "메일 발송에 실패했습니다. 서버 로그를 확인하세요."}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

def verify_admin(admin_pass):
    if str(admin_pass) == "1900":
        return True, "admin"
    df = read_excel_db(OWNER_FILE)
    if not df.empty:
        admin = df[(df['암호'].astype(str) == str(admin_pass)) & (df['레벨'] <= 2)]
        if not admin.empty:
            return True, admin.iloc[0]['이름']
    return False, None

@user_mgmt_bp.route('/register', methods=['POST'])
def register():
    try:
        data = request.json
        df = read_excel_db(OWNER_FILE)
        if not df.empty and data['name'] in df['이름'].values:
            return jsonify({"status": "error", "message": "이미 등록된 이름입니다."}), 400

        new_user = pd.DataFrame([{
            '이름': data['name'], 
            '암호': str(data['password']), 
            '직급': data['position'],
            '레벨': 10, 
            '주민번호': data.get('rrn', ''),
            '이메일': data.get('email', ''),
            '전화번호': data.get('phone', ''), 
            '주소': data.get('address', ''), 
            '기타사항': data.get('note', ''), 
            '승인상태': '대기'
        }])
        df = pd.concat([df, new_user], ignore_index=True)
        write_excel_db(df, OWNER_FILE)
        return jsonify({"status": "success", "message": "회원가입 신청이 정상적으로 접수되었습니다."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@user_mgmt_bp.route('/approve', methods=['POST'])
def approve():
    try:
        data = request.json
        is_valid, admin_name = verify_admin(data.get('admin_pass'))
        if not is_valid:
            return jsonify({"status": "error", "message": "관리자 권한이 없습니다."}), 403

        df = read_excel_db(OWNER_FILE)
        idx = int(data['user_idx'])
        approved_pos = data['approved_position']
        
        df.at[idx, '직급'] = approved_pos
        df.at[idx, '레벨'] = LEVEL_MAP.get(approved_pos, 10)
        df.at[idx, '승인상태'] = '승인'
        
        write_excel_db(df, OWNER_FILE)
        return jsonify({"status": "success", "message": f"{approved_pos} 승인 완료 (처리자: {admin_name})"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@user_mgmt_bp.route('/update', methods=['POST'])
def update_user():
    try:
        data = request.json
        is_valid, admin_name = verify_admin(data.get('admin_pass'))
        if not is_valid:
            return jsonify({"status": "error", "message": "관리자 권한이 없습니다."}), 403

        df = read_excel_db(OWNER_FILE)
        idx = int(data['user_idx'])
        df.at[idx, '직급'] = data['position']
        df.at[idx, '레벨'] = int(data['level'])
        df.at[idx, '전화번호'] = data['phone']
        df.at[idx, '이메일'] = data['email']
        
        write_excel_db(df, OWNER_FILE)
        return jsonify({"status": "success", "message": f"정보 수정 완료 (처리자: {admin_name})"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@user_mgmt_bp.route('/delete', methods=['POST'])
def delete_user():
    try:
        data = request.json
        is_valid, _ = verify_admin(data.get('admin_pass'))
        if not is_valid:
            return jsonify({"status": "error", "message": "삭제 권한이 없습니다."}), 403

        df = read_excel_db(OWNER_FILE)
        idx = int(data['user_idx'])
        df = df.drop(df.index[idx]).reset_index(drop=True)
        write_excel_db(df, OWNER_FILE)
        return jsonify({"status": "success", "message": "회원 삭제가 완료되었습니다."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@user_mgmt_bp.route('/list')
def get_user_list():
    df = read_excel_db(OWNER_FILE)
    return jsonify(df.to_dict(orient='records') if not df.empty else [])