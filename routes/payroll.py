import os
import time
import threading
import pandas as pd
from flask import Blueprint, render_template, request, jsonify, current_app, url_for
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

payroll_bp = Blueprint('payroll', __name__)

mail_status = {
    'is_running': False,
    'total_count': 0,
    'sent_count': 0,
    'processed_count': 0, 
    'sent_names': [],
    'errors': []
}

def safe_amount(*args):
    try:
        value = 0
        if len(args) == 2:
            if hasattr(args[0], 'get') and isinstance(args[1], str):
                value = args[0].get(args[1], 0)
            else:
                value = args[0]
        elif len(args) == 1:
            value = args[0]

        if pd.isna(value) or str(value).strip() == "" or value is None:
            return "0"
        return f"{int(float(value)):,}"
    except Exception:
        return "0"

def get_template_path(user_type):
    u_type = str(user_type).replace(' ', '')
    if '근로' in u_type: return 'payroll/employee_worker.html'
    if '사업' in u_type: return 'payroll/employee_business.html'
    if '퇴직' in u_type: return 'payroll/retired.html'
    return 'payroll/teacher.html' 

def send_payroll_mail(row, user_type, send_date, base_url):
    SENDER_EMAIL = os.environ.get('MAIL_USERNAME')
    SENDER_PASSWORD = os.environ.get('MAIL_PASSWORD')
    
    if not SENDER_EMAIL or not SENDER_PASSWORD:
        return False, "메일 서버(구글 앱 비밀번호) 설정이 되어있지 않습니다."

    msg = MIMEMultipart('alternative')
    target_name = str(row.get('직원명', row.get('강사명', '성함없음'))).strip()
    msg['Subject'] = f"[{send_date}] {target_name}님 급여(수수료) 명세서 안내"
    msg['From'] = f"새담 인트라넷 <{SENDER_EMAIL}>"
    msg['To'] = str(row.get('이메일', '')).strip()

    if "@" not in msg['To']:
        return False, f"{target_name}님의 이메일 주소가 올바르지 않습니다."

    tpl_path = get_template_path(user_type)
    
    # URL 링크 방식으로 이미지 주소 생성 (Render 서버 주소 기반)
    # url_for(_external=True)를 사용하여 http://도메인/static/... 형태의 절대 경로를 만듭니다.
    logo_url = base_url + "/static/logo01.jpg"
    ad1_url = base_url + "/static/payroll_ad1.jpg"
    ad2_url = base_url + "/static/payroll_ad2.jpg"
    
    try:
        # 템플릿에 이미지 URL들을 변수로 전달
        html_content = render_template(
            tpl_path, 
            row=row, 
            send_date=send_date, 
            safe_amount=safe_amount,
            logo_url=logo_url,
            ad1_url=ad1_url,
            ad2_url=ad2_url
        )
        msg.attach(MIMEText(html_content, 'html'))

        # 기존의 무거운 MIMEImage 첨부 코드는 모두 삭제되었습니다 (URL 방식으로 대체)

        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(msg)
        return True, "Success"
    except Exception as e:
        return False, str(e)

def payroll_worker(app, df, send_date, interval, base_url):
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
            # base_url을 전달하여 스레드 내에서도 절대 경로를 생성할 수 있게 함
            success, err_msg = send_payroll_mail(row, u_type, send_date, base_url)
            
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
    return render_template('payroll/payroll_form.html', user_icons={})

# [추가됨] 광고 이미지 단독 업로드 API
@payroll_bp.route('/upload_ad', methods=['POST'])
def upload_ad():
    if 'image' not in request.files:
        return jsonify({"status": "error", "message": "이미지 파일이 없습니다."})
        
    file = request.files['image']
    ad_type = request.form.get('type') # 'ad1' 또는 'ad2'
    
    if ad_type not in ['ad1', 'ad2']:
        return jsonify({"status": "error", "message": "잘못된 요청입니다."})
        
    if file and file.filename:
        static_folder = os.path.join(current_app.root_path, 'static')
        os.makedirs(static_folder, exist_ok=True)
        # 덮어쓰기 저장
        filepath = os.path.join(static_folder, f'payroll_{ad_type}.jpg')
        file.save(filepath)
        return jsonify({"status": "success"})
        
    return jsonify({"status": "error", "message": "업로드 실패"})

@payroll_bp.route('/send', methods=['POST'])
def start_send():
    global mail_status
    if mail_status.get('is_running'):
        return jsonify({"status": "error", "message": "이미 다른 발송 작업이 진행 중입니다."})

    if 'excel' not in request.files: 
        return jsonify({"status": "error", "message": "엑셀 파일을 업로드해주세요."})
    
    try:
        file = request.files['excel']
        xls = pd.read_excel(file, sheet_name=None, header=2)
        df = None
        for sheet_name, sheet_df in xls.items():
            if '이메일' in sheet_df.columns:
                df = sheet_df
                if '직원구분' not in df.columns:
                    df['직원구분'] = sheet_name 
                break
                
        if df is None:
            return jsonify({"status": "error", "message": "어떤 시트에서도 3번째 줄에 '이메일' 항목을 찾을 수 없습니다."})

        df = df.dropna(how='all').fillna("")
        df = df[df['이메일'].astype(str).str.contains('@')]
            
        if len(df) == 0:
            return jsonify({"status": "error", "message": "유효한 이메일 주소(@포함)가 작성된 대상자가 없습니다."})
        
        send_date = request.form.get('send_date')
        interval = request.form.get('interval', 2)
        
        # 폼 데이터에서 광고 이미지 처리 로직 제거 (이제 별도 API로 업로드됨)

        mail_status.update({'sent_count': 0, 'processed_count': 0, 'sent_names': [], 'errors': [], 'total_count': len(df)})

        app = current_app._get_current_object()
        # 발송 시 사용할 절대 도메인 주소 획득 (예: https://saedam-intranet.onrender.com)
        base_url = request.host_url.rstrip('/')
        
        threading.Thread(target=payroll_worker, args=(app, df, send_date, interval, base_url)).start()
        
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": f"엑셀 분석 중 오류: {str(e)}"})

@payroll_bp.route('/status')
def get_status():
    return jsonify(mail_status)