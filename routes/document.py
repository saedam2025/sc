import os
import pandas as pd
import pdfkit
import smtplib
import shutil
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Blueprint, render_template, request, jsonify, send_from_directory, session, redirect, url_for, flash, abort
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from jinja2 import Template

# Blueprint 설정
document_bp = Blueprint('document', __name__)

# 한국 시간 설정 함수
def now_kst():
    return datetime.now(ZoneInfo("Asia/Seoul"))

# --- [경로 및 환경 설정] ---
BASE_DIR = "/mnt/data" if os.path.exists("/mnt/data") else os.getcwd()
DATA_PATH = os.path.join(BASE_DIR, "certificates.xlsx")  # 엑셀 DB 파일
PDF_FOLDER = os.path.join(BASE_DIR, "output_pdfs")       # 생성된 PDF 보관 폴더
SEAL_IMAGE = os.path.join(os.getcwd(), "static", "seal.gif") # 도장 이미지

TEMPLATE_PATH = os.path.join(os.getcwd(), "templates", "certificate", "certificate_template.html")

os.makedirs(PDF_FOLDER, exist_ok=True)

# 이메일 설정
SENDER_EMAIL = os.environ.get("EMAIL_ADDRESS_01")
SENDER_PW = os.environ.get("APP_PASSWORD_01")
ADMIN_NOTIFICATION_EMAIL = "edu197@naver.com"

# PDF 엔진 설정
WKHTMLTOPDF_PATH = shutil.which("wkhtmltopdf") or "/usr/bin/wkhtmltopdf"
PDF_CONFIG = pdfkit.configuration(wkhtmltopdf=WKHTMLTOPDF_PATH)

# --- [내부 데이터베이스 관리 함수] ---
def ensure_db_initialized():
    """certificates.xlsx 파일이 없으면 표준 컬럼으로 생성"""
    if not os.path.exists(DATA_PATH):
        columns = [
            "신청일", "증명서종류", "성명", "주민번호", "자택주소",
            "근무시작일", "근무종료일", "근무장소", "강의과목", "용도", "직책",
            "이메일주소", "상태", "발급일", "발급번호", "종료사유", "파일명"
        ]
        pd.DataFrame(columns=columns).to_excel(DATA_PATH, index=False)

def get_next_issue_number():
    """연도별 발급 번호 자동 생성"""
    year_prefix = now_kst().strftime('%y')
    num_file = os.path.join(BASE_DIR, f"last_cert_num_{year_prefix}.txt")
    
    last_num = 0
    if os.path.exists(num_file):
        with open(num_file, 'r') as f:
            try: last_num = int(f.read().strip())
            except: last_num = 0
    
    next_num = last_num + 1
    with open(num_file, 'w') as f:
        f.write(str(next_num))
    
    return f"제{year_prefix}-{next_num:04d}호"

# --- [외부 라우트: 강사 신청용] ---
@document_bp.route('/apply', methods=['GET', 'POST'])
def apply():
    ensure_db_initialized()
    if request.method == 'POST':
        try:
            df = pd.read_excel(DATA_PATH, dtype=str).fillna("")
            form_data = dict(request.form)
            
            # 근무 종료일 '현재까지' 처리 로직
            if form_data.get("종료일선택") == "현재까지":
                form_data["근무종료일"] = "현재까지"
            
            form_data["신청일"] = now_kst().strftime("%Y-%m-%d")
            form_data["상태"] = "대기"
            form_data["발급일"] = ""
            form_data["발급번호"] = ""
            form_data["파일명"] = ""
            
            # 임시 필드 제거
            form_data.pop("종료일선택", None)

            # 데이터 추가 및 저장
            new_row = pd.DataFrame([form_data])
            df = pd.concat([df, new_row], ignore_index=True)
            df.to_excel(DATA_PATH, index=False)

            # 관리자 알림
            send_admin_alert(form_data['성명'], form_data['증명서종류'])
            
            # 수정사항: success.html에 form_data 전체를 넘겨 정보 요약이 가능하게 함
            return render_template('certificate/success.html', data=form_data)
        except Exception as e:
            return f"신청 중 오류가 발생했습니다: {str(e)}", 500
            
    return render_template('certificate/form.html')

# --- [내부 라우트: 관리자용] ---
@document_bp.route('/admin')
def admin_list():
    """인트라넷 관리자용 신청 현황 목록"""
    if 'user_name' not in session:
        return redirect(url_for('login_page'))
    
    ensure_db_initialized()
    df = pd.read_excel(DATA_PATH, dtype=str).fillna("")
    submissions = df.iloc[::-1].reset_index().to_dict(orient='records')
    
    return render_template('certificate/admin.html', 
                           submissions=submissions,
                           total=len(df),
                           pending=len(df[df['상태'] == '대기']))

@document_bp.route('/generate/<int:idx>')
def generate_certificate(idx):
    """관리자가 발급 버튼을 눌렀을 때 실행"""
    if 'user_name' not in session: return abort(403)
    
    try:
        df = pd.read_excel(DATA_PATH, dtype=str).fillna("")
        actual_idx = len(df) - 1 - idx
        row = df.iloc[actual_idx]
        
        if row['상태'] == '발급완료':
            flash("이미 발급이 완료된 요청입니다.")
            return redirect(url_for('document.admin_list'))

        issue_no = get_next_issue_number()
        pdf_path = create_pdf_file(row, issue_no)
        
        send_email_to_instructor(row['이메일주소'], row['성명'], pdf_path, row['증명서종류'])
        
        df.at[actual_idx, '상태'] = '발급완료'
        df.at[actual_idx, '발급일'] = now_kst().strftime("%Y-%m-%d")
        df.at[actual_idx, '발급번호'] = issue_no
        df.at[actual_idx, '파일명'] = os.path.basename(pdf_path)
        df.to_excel(DATA_PATH, index=False)
        
        flash(f"{row['성명']} 님께 증명서 발송을 완료했습니다.")
    except Exception as e:
        flash(f"발급 중 오류 발생: {str(e)}")
        
    return redirect(url_for('document.admin_list'))

# --- [보조 기능 함수들] ---

def create_pdf_file(row, issue_no):
    """HTML 템플릿을 읽어 PDF 파일 생성"""
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template = Template(f.read())

    # 수정사항: 주민번호 중복 인자 오류 해결을 위해 딕셔너리로 통합 관리
    data = row.to_dict()
    
    # 주민번호 마스킹 처리
    ssn = str(data.get('주민번호', ''))
    masked_ssn = ssn[:8] + "******" if "-" in ssn else ssn
    data['주민번호'] = masked_ssn  # 딕셔너리 내부의 주민번호를 마스킹된 것으로 교체

    # 템플릿 렌더링 (data 딕셔너리 하나만 풀어서 전달)
    html_content = template.render(
        **data,
        발급번호=issue_no,
        발급일자=now_kst().strftime("%Y년 %m월 %d일")
    )

    # 도장 이미지 삽입
    seal_uri = f"file:///{os.path.abspath(SEAL_IMAGE)}"
    html_content = html_content.replace('src="seal.gif"', f'src="{seal_uri}"')

    file_name = f"{issue_no}_{row['성명']}.pdf"
    output_path = os.path.join(PDF_FOLDER, file_name)
    
    options = {
        'enable-local-file-access': None, 
        'encoding': 'UTF-8',
        'margin-top': '0', 'margin-bottom': '0', 'margin-left': '0', 'margin-right': '0'
    }
    
    pdfkit.from_string(html_content, output_path, configuration=PDF_CONFIG, options=options)
    return output_path

def send_email_to_instructor(to_email, name, pdf_path, cert_type):
    """생성된 PDF를 첨부하여 강사에게 메일 발송"""
    msg = MIMEMultipart()
    msg['From'] = SENDER_EMAIL
    msg['To'] = to_email
    msg['Subject'] = f"[(사)새담] 요청하신 {cert_type} 발송 안내 ({name} 강사님)"
    
    body = f"{name} 강사님, 안녕하세요.\n\n새담청소년교육문화원입니다.\n요청하신 {cert_type}를 첨부파일로 보내드립니다.\n\n감사합니다."
    msg.attach(MIMEText(body, 'plain'))
    
    with open(pdf_path, "rb") as f:
        part = MIMEApplication(f.read(), _subtype="pdf")
        part.add_header('Content-Disposition', 'attachment', filename=os.path.basename(pdf_path))
        msg.attach(part)
        
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SENDER_EMAIL, SENDER_PW)
        server.send_message(msg)

def send_admin_alert(name, cert_type):
    """신청 발생 시 관리자에게 간단 알림"""
    try:
        msg = MIMEText(f"새로운 증명서 신청이 들어왔습니다.\n\n신청자: {name}\n종류: {cert_type}\n인트라넷에서 확인 후 발급해 주세요.")
        msg['Subject'] = f"[신청접수] {name} 강사님 - {cert_type}"
        msg['From'] = SENDER_EMAIL
        msg['To'] = ADMIN_NOTIFICATION_EMAIL
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SENDER_EMAIL, SENDER_PW)
            server.send_message(msg)
    except:
        pass

@document_bp.route('/pdf/<filename>')
def serve_pdf(filename):
    """관리자 페이지에서 발급된 PDF 보기"""
    if 'user_name' not in session: return abort(403)
    return send_from_directory(PDF_FOLDER, filename)

@document_bp.route('/delete/<int:idx>')
def delete_record(idx):
    """신청 기록 및 파일 삭제"""
    if 'user_name' not in session: return abort(403)
    try:
        df = pd.read_excel(DATA_PATH, dtype=str).fillna("")
        actual_idx = len(df) - 1 - idx
        
        filename = df.at[actual_idx, '파일명']
        if filename:
            p = os.path.join(PDF_FOLDER, filename)
            if os.path.exists(p): os.remove(p)
            
        df = df.drop(index=actual_idx)
        df.to_excel(DATA_PATH, index=False)
        flash("기록이 성공적으로 삭제되었습니다.")
    except Exception as e:
        flash(f"삭제 중 오류: {str(e)}")
    return redirect(url_for('document.admin_list'))