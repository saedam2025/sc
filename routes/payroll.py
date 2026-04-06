import os
import time
import threading
import pandas as pd
from flask import Blueprint, render_template, request, jsonify, session
from datetime import datetime
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

payroll_bp = Blueprint('payroll', __name__)

# 발송 상태 실시간 추적용 전역 변수
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
    
    msg = MIMEMultipart('related')
    target_name = row.get('직원명', row.get('강사명', '성함없음'))
    msg['Subject'] = f"[{send_date}] {target_name}님 급여(수수료) 명세서 안내"
    msg['From'] = f"새담 인트라넷 <{SENDER_EMAIL}>"
    msg['To'] = row.get('이메일', '')

    # 템플릿 경로 설정 (templates/payroll/ 폴더 기준)
    template_map = {
        '강사': 'payroll/teacher.html',
        '직원근로자': 'payroll/employee_worker.html',
        '직원사업자': 'payroll/employee_business.html',
        '퇴직자': 'payroll/retired.html'
    }
    tpl_path = template_map.get(user_type, 'payroll/teacher.html')
    
    # HTML 렌더링
    html_content = render_template(tpl_path, row=row, send_date=send_date)
    msg.attach(MIMEText(html_content, 'html'))

    # 로고 및 광고 이미지 삽입 (CID 방식)
    img_targets = [
        ('logo_image', 'static/logo01.jpg'), 
        ('ad1_image', ad1_path), 
        ('ad2_image', ad2_path)
    ]
    
    for cid, path in img_targets:
        if path and os.path.exists(path):
            with open(path, 'rb') as f:
                img = MIMEImage(f.read())
                img.add_header('Content-ID', f'<{cid}>')
                msg.attach(img)

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        mail_status['errors'].append(f"{target_name}: {str(e)}")
        return False

def payroll_worker(df, send_date, interval, ad1, ad2):
    """속도 조절(Interval)을 통한 스팸 방지 발송 로직"""
    global mail_status
    mail_status['is_running'] = True
    mail_status['total_count'] = len(df)
    mail_status['sent_count'] = 0
    mail_status['sent_names'] = []

    for _, row in df.iterrows():
        u_type = str(row.get('직원구분', '강사')).strip()
        
        if send_payroll_mail(row, u_type, send_date, ad1, ad2):
            mail_status['sent_count'] += 1
            mail_status['sent_names'].append(row.get('직원명', row.get('강사명', 'Unknown')))
        
        # 구글 스팸 방지를 위한 대기 시간 적용
        time.sleep(float(interval))

    mail_status['is_running'] = False

@payroll_bp.route('/')
def index():
    return render_template('payroll_form.html')

@payroll_bp.route('/send', methods=['POST'])
def start_send():
    """발송 시작 및 광고 이미지 업로드 처리"""
    if 'excel' not in request.files: 
        return jsonify({"status": "error", "message": "엑셀 파일을 선택해주세요."})
    
    file = request.files['excel']
    df = pd.read_excel(file).fillna("")
    send_date = request.form.get('send_date')
    interval = request.form.get('interval', 2)
    
    ad1 = request.files.get('ad1')
    ad2 = request.files.get('ad2')
    ad1_path = "static/ad1.jpg" if ad1 else None
    ad2_path = "static/ad2.jpg" if ad2 else None
    
    if ad1: ad1.save(ad1_path)
    if ad2: ad2.save(ad2_path)

    threading.Thread(target=payroll_worker, args=(df, send_date, interval, ad1_path, ad2_path)).start()
    return jsonify({"status": "success", "message": "발송 프로세스가 시작되었습니다."})

@payroll_bp.route('/status')
def get_status():
    """실시간 발송 현황 반환 API"""
    return jsonify(mail_status)