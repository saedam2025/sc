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

# 발송 상태 전역 변수
mail_status = {
    'is_running': False,
    'total_count': 0,
    'sent_count': 0,
    'sent_names': [],
    'errors': []
}

def send_payroll_mail(row, user_type, send_date, ad1_path, ad2_path):
    """지정된 templates/payroll/ 경로의 템플릿을 사용하여 메일 발송"""
    SENDER_EMAIL = os.environ.get('MAIL_USERNAME')
    SENDER_PASSWORD = os.environ.get('MAIL_PASSWORD')
    
    if not SENDER_EMAIL or not SENDER_PASSWORD:
        return False, "메일 서버 설정이 되어있지 않습니다."

    msg = MIMEMultipart('related')
    target_name = str(row.get('직원명', row.get('강사명', '성함없음'))).strip()
    msg['Subject'] = f"[{send_date}] {target_name}님 급여(수수료) 명세서 안내"
    msg['From'] = f"새담 인트라넷 <{SENDER_EMAIL}>"
    msg['To'] = str(row.get('이메일', '')).strip()

    if "@" not in msg['To']:
        return False, "유효하지 않은 이메일 주소입니다."

    # 템플릿 경로 매핑
    template_map = {
        '강사': 'payroll/teacher.html',
        '직원근로자': 'payroll/employee_worker.html',
        '직원사업자': 'payroll/employee_business.html',
        '퇴직자': 'payroll/retired.html'
    }
    tpl_path = template_map.get(user_type, 'payroll/teacher.html')
    
    try:
        # 렌더링 시 숫자 포맷터 등 필요한 헬퍼 전달 가능
        html_content = render_template(tpl_path, row=row, send_date=send_date)
        msg.attach(MIMEText(html_content, 'html'))

        # 이미지 첨부 (로고 및 광고)
        img_configs = [
            ('logo_image', 'static/logo01.jpg'), 
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
    """별도 쓰레드에서 순차 발송"""
    global mail_status
    
    with app.app_context():
        mail_status['is_running'] = True
        mail_status['total_count'] = len(df)
        mail_status['sent_count'] = 0
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
                mail_status['errors'].append(f"오류: {err_msg}")
                print(f"❌ 발송 실패: {err_msg}")
            
            time.sleep(float(interval))

        mail_status['is_running'] = False

@payroll_bp.route('/')
def index():
    return render_template('payroll_form.html', user_icons={})

@payroll_bp.route('/send', methods=['POST'])
def start_send():
    global mail_status
    if mail_status.get('is_running'):
        return jsonify({"status": "error", "message": "이미 발송이 진행 중입니다."})

    if 'excel' not in request.files: 
        return jsonify({"status": "error", "message": "엑셀 파일을 업로드해주세요."})
    
    try:
        file = request.files['excel']
        df = pd.read_excel(file, header=2).dropna(how='all').fillna("")
        
        # '이메일' 칼럼에 '@'가 포함된 유효한 데이터만 필터링
        if '이메일' in df.columns:
            df = df[df['이메일'].astype(str).str.contains('@')]
        else:
            return jsonify({"status": "error", "message": "엑셀 파일에 '이메일' 열이 필요합니다."})
            
        if len(df) == 0:
            return jsonify({"status": "error", "message": "유효한 이메일 주소가 있는 대상자가 없습니다."})
        
        send_date = request.form.get('send_date')
        interval = request.form.get('interval', 2)
        
        # 광고 이미지 처리
        ad1 = request.files.get('ad1')
        ad2 = request.files.get('ad2')
        ad1_path = "static/payroll_ad1.jpg" if ad1 and ad1.filename else None
        ad2_path = "static/payroll_ad2.jpg" if ad2 and ad2.filename else None
        
        if ad1_path: ad1.save(ad1_path)
        if ad2_path: ad2.save(ad2_path)

        # 쓰레드 시작 전 상태 초기화
        mail_status.update({'sent_count': 0, 'sent_names': [], 'errors': [], 'total_count': len(df)})

        # 현재 플라스크 앱 객체를 스레드로 전달
        app = current_app._get_current_object()
        threading.Thread(target=payroll_worker, args=(app, df, send_date, interval, ad1_path, ad2_path)).start()
        
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": f"파일 처리 중 오류: {str(e)}"})

@payroll_bp.route('/status')
def get_status():
    return jsonify(mail_status)