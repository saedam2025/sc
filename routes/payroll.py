import os
import time
import threading
import pandas as pd
from flask import Blueprint, render_template, request, jsonify, current_app
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

payroll_bp = Blueprint('payroll', __name__)

# 발송 상태 전역 변수 (processed_count 추가로 진행률 정확도 100% 보장)
mail_status = {
    'is_running': False,
    'total_count': 0,
    'sent_count': 0,
    'processed_count': 0, 
    'sent_names': [],
    'errors': []
}

def get_template_path(user_type):
    """직원 구분을 유연하게 자동 인식하여 템플릿을 매칭합니다."""
    u_type = str(user_type).replace(' ', '')
    if '근로' in u_type: return 'payroll/employee_worker.html'
    if '사업' in u_type: return 'payroll/employee_business.html'
    if '퇴직' in u_type: return 'payroll/retired.html'
    return 'payroll/teacher.html' # 일치하는 게 없으면 기본 강사 템플릿

def send_payroll_mail(row, user_type, send_date, ad1_path, ad2_path):
    SENDER_EMAIL = os.environ.get('MAIL_USERNAME')
    SENDER_PASSWORD = os.environ.get('MAIL_PASSWORD')
    
    if not SENDER_EMAIL or not SENDER_PASSWORD:
        return False, "메일 서버(구글 앱 비밀번호) 설정이 되어있지 않습니다."

    msg = MIMEMultipart('related')
    target_name = str(row.get('직원명', row.get('강사명', '성함없음'))).strip()
    msg['Subject'] = f"[{send_date}] {target_name}님 급여(수수료) 명세서 안내"
    msg['From'] = f"새담 인트라넷 <{SENDER_EMAIL}>"
    msg['To'] = str(row.get('이메일', '')).strip()

    if "@" not in msg['To']:
        return False, f"{target_name}님의 이메일 주소가 올바르지 않습니다."

    # 템플릿 자동 매칭
    tpl_path = get_template_path(user_type)
    
    try:
        html_content = render_template(tpl_path, row=row, send_date=send_date)
        msg.attach(MIMEText(html_content, 'html'))

        # 광고 이미지 및 로고 첨부 로직 (경로가 존재하는 경우에만 정확히 작동)
        img_configs = [
            ('logo_image', os.path.join(current_app.root_path, 'static', 'logo01.jpg')), 
            ('ad1_image', ad1_path), 
            ('ad2_image', ad2_path)
        ]
        
        for cid, path in img_configs:
            if path and os.path.exists(path):
                with open(path, 'rb') as f:
                    img = MIMEImage(f.read())
                    img.add_header('Content-ID', f'<{cid}>')
                    msg.attach(img)

        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(msg)
        return True, "Success"
    except Exception as e:
        return False, str(e)

def payroll_worker(app, df, send_date, interval, ad1, ad2):
    global mail_status
    
    with app.app_context():
        mail_status['is_running'] = True
        mail_status['total_count'] = len(df)
        mail_status['sent_count'] = 0
        mail_status['processed_count'] = 0
        mail_status['sent_names'] = []
        mail_status['errors'] = []

        for _, row in df.iterrows():
            u_type = str(row.get('직원구분', '강사')).strip()
            success, err_msg = send_payroll_mail(row, u_type, send_date, ad1, ad2)
            
            if success:
                mail_status['sent_count'] += 1
                name = str(row.get('직원명', row.get('강사명', 'Unknown')))
                mail_status['sent_names'].append(name)
            else:
                name = str(row.get('직원명', row.get('강사명', '알수없음')))
                mail_status['errors'].append(f"[{name}] {err_msg}")
            
            mail_status['processed_count'] += 1
            time.sleep(float(interval))

        mail_status['is_running'] = False

@payroll_bp.route('/')
def index():
    return render_template('payroll_form.html', user_icons={})

@payroll_bp.route('/send', methods=['POST'])
def start_send():
    global mail_status
    if mail_status.get('is_running'):
        return jsonify({"status": "error", "message": "이미 다른 발송 작업이 진행 중입니다."})

    if 'excel' not in request.files: 
        return jsonify({"status": "error", "message": "엑셀 파일을 업로드해주세요."})
    
    try:
        file = request.files['excel']
        
        # [핵심] 여러 탭 중 '이메일' 컬럼이 있는 시트를 자동 탐색합니다.
        xls = pd.read_excel(file, sheet_name=None, header=2)
        df = None
        for sheet_name, sheet_df in xls.items():
            if '이메일' in sheet_df.columns:
                df = sheet_df
                # 직원구분 컬럼이 없을 경우 시트 이름을 기준으로 임시 부여
                if '직원구분' not in df.columns:
                    df['직원구분'] = sheet_name 
                break
                
        if df is None:
            return jsonify({"status": "error", "message": "어떤 시트에서도 3번째 줄에 '이메일' 항목을 찾을 수 없습니다."})

        # 유효한 이메일 골라내기
        df = df.dropna(how='all').fillna("")
        df = df[df['이메일'].astype(str).str.contains('@')]
            
        if len(df) == 0:
            return jsonify({"status": "error", "message": "유효한 이메일 주소(@포함)가 작성된 대상자가 없습니다."})
        
        send_date = request.form.get('send_date')
        interval = request.form.get('interval', 2)
        
        # 안전한 광고 이미지 경로 설정
        static_folder = os.path.join(current_app.root_path, 'static')
        os.makedirs(static_folder, exist_ok=True)
        
        ad1 = request.files.get('ad1')
        ad2 = request.files.get('ad2')
        ad1_path = os.path.join(static_folder, 'payroll_ad1.jpg') if ad1 and ad1.filename else None
        ad2_path = os.path.join(static_folder, 'payroll_ad2.jpg') if ad2 and ad2.filename else None
        
        if ad1_path: ad1.save(ad1_path)
        if ad2_path: ad2.save(ad2_path)

        mail_status.update({'sent_count': 0, 'processed_count': 0, 'sent_names': [], 'errors': [], 'total_count': len(df)})

        app = current_app._get_current_object()
        threading.Thread(target=payroll_worker, args=(app, df, send_date, interval, ad1_path, ad2_path)).start()
        
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": f"엑셀 분석 중 오류: {str(e)}"})

@payroll_bp.route('/status')
def get_status():
    return jsonify(mail_status)