import os
import pandas as pd
import pdfkit
import yagmail  # smtplib 대신 contract.py와 동일하게 yagmail 사용
import shutil
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Blueprint, render_template, request, jsonify, send_from_directory, session, redirect, url_for, flash, abort
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

# 템플릿 경로 설정 (실제 경로 templates/certificate/ 하위로 고정)
TEMPLATE_PATH = os.path.join(os.getcwd(), "templates", "certificate", "certificate_template.html")

os.makedirs(PDF_FOLDER, exist_ok=True)

ADMIN_NOTIFICATION_EMAIL = "edu197@naver.com"

# PDF 엔진 설정
WKHTMLTOPDF_PATH = shutil.which("wkhtmltopdf") or "/usr/bin/wkhtmltopdf"
PDF_CONFIG = pdfkit.configuration(wkhtmltopdf=WKHTMLTOPDF_PATH)

# --- [내부 데이터베이스 관리 함수] ---
def ensure_db_initialized():
    """certificates.xlsx 파일이 없으면 표준 컬럼으로 생성"""
    columns = [
        "신청일", "증명서종류", "성명", "주민번호", "자택주소",
        "근무시작일", "근무종료일", "근무장소", "강의과목", "용도", "직책",
        "이메일주소", "상태", "발급일", "발급번호", "종료사유", "파일명"
    ]
    if not os.path.exists(DATA_PATH):
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
            
            if form_data.get("종료일선택") == "현재까지":
                form_data["근무종료일"] = "현재까지"
            
            form_data["신청일"] = now_kst().strftime("%Y-%m-%d")
            form_data["상태"] = "대기"
            form_data["발급일"] = ""
            form_data["발급번호"] = ""
            form_data["파일명"] = ""
            form_data.pop("종료일선택", None)

            new_row = pd.DataFrame([form_data])
            df = pd.concat([df, new_row], ignore_index=True)
            df.to_excel(DATA_PATH, index=False)

            send_admin_alert(form_data['성명'], form_data['증명서종류'])
            return render_template('certificate/success.html', data=form_data)
        except Exception as e:
            return f"신청 중 오류가 발생했습니다: {str(e)}", 500
            
    return render_template('certificate/form.html')

# --- [내부 라우트: 관리자용] ---
@document_bp.route('/admin')
def admin_list():
    """인트라넷 관리자용 신청 현황 목록"""
    if 'emp_no' not in session:
        return redirect(url_for('login_page'))
    
    ensure_db_initialized()
    df = pd.read_excel(DATA_PATH, dtype=str).fillna("")
    
    df_with_idx = df.copy()
    df_with_idx['index'] = df.index
    submissions = df_with_idx.iloc[::-1].to_dict(orient='records')
    
    return render_template('certificate/admin.html', 
                           submissions=submissions,
                           total=len(df),
                           pending=len(df[df['상태'] == '대기']))

@document_bp.route('/generate/<int:idx>')
def generate_certificate(idx):
    """관리자가 발급 버튼을 눌렀을 때 실행"""
    if 'emp_no' not in session: return abort(403)
    
    try:
        df = pd.read_excel(DATA_PATH, dtype=str).fillna("")
        
        if idx not in df.index:
            flash("데이터를 찾을 수 없습니다.")
            return redirect(url_for('document.admin_list'))
            
        row = df.iloc[idx]
        
        if row['상태'] == '발급완료':
            flash("이미 발급이 완료된 요청입니다.")
            return redirect(url_for('document.admin_list'))

        issue_no = get_next_issue_number()
        pdf_path = create_pdf_file(row, issue_no)
        
        # 메일 발송 시도 전 발급 완료 상태로 먼저 업데이트 (안전장치)
        df.at[idx, '상태'] = '발급완료'
        df.at[idx, '발급일'] = now_kst().strftime("%Y-%m-%d")
        df.at[idx, '발급번호'] = issue_no
        df.at[idx, '파일명'] = os.path.basename(pdf_path)
        df.to_excel(DATA_PATH, index=False)

        # 메일 발송
        mail_success = send_email_to_instructor(row['이메일주소'], row['성명'], pdf_path, row['증명서종류'])
        
        if mail_success:
            flash(f"{row['성명']} 님께 증명서 발송을 완료했습니다.")
        else:
            flash(f"{row['성명']} 님 증명서가 발급되었으나, 메일 발송에는 실패했습니다. 서버 설정을 확인해주세요.")
            
    except Exception as e:
        flash(f"발급 중 오류 발생: {str(e)}")
        
    return redirect(url_for('document.admin_list'))

# --- [보조 기능 함수들] ---

def create_pdf_file(row, issue_no):
    """HTML 템플릿을 읽어 PDF 파일 생성"""
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template = Template(f.read())

    data = row.to_dict()
    
    ssn = str(data.get('주민번호', '')).replace("-", "")
    if len(ssn) >= 7:
        masked_ssn = f"{ssn[:6]}-{ssn[6]}******"
    else:
        masked_ssn = ssn
    data['주민번호'] = masked_ssn

    data['발급번호'] = issue_no
    data['발급일자'] = now_kst().strftime("%Y년 %m월 %d일")

    html_content = template.render(**data)

    seal_uri = f"file:///{os.path.abspath(SEAL_IMAGE).replace(os.sep, '/')}"
    html_content = html_content.replace('src="seal.gif"', f'src="{seal_uri}"')

    file_name = f"{issue_no}_{row['성명']}.pdf".replace("/", "_")
    output_path = os.path.join(PDF_FOLDER, file_name)
    
    options = {
        'enable-local-file-access': None, 
        'encoding': 'UTF-8',
        'margin-top': '0', 'margin-bottom': '0', 'margin-left': '0', 'margin-right': '0'
    }
    
    pdfkit.from_string(html_content, output_path, configuration=PDF_CONFIG, options=options)
    return output_path

# --- [이메일 발송 함수들 (yagmail 적용)] ---

def send_email_to_instructor(to_email, name, pdf_path, cert_type):
    """생성된 PDF를 첨부하여 강사에게 메일 발송"""
    # 동적으로 실행 시점에 환경변수를 가져옵니다 (오류 원인 해결)
    email_addr = os.environ.get("EMAIL_ADDRESS_01")
    email_pw = os.environ.get("APP_PASSWORD_01")
    
    if not email_addr or not email_pw:
        print("메일 환경변수가 누락되었습니다.")
        return False
        
    try:
        yag = yagmail.SMTP(email_addr, email_pw)
        subject = f"[(사)새담] 요청하신 {cert_type} 발송 안내 ({name} 강사님)"
        contents = f"{name} 강사님, 안녕하세요.\n\n새담청소년교육문화원입니다.\n요청하신 {cert_type}를 첨부파일로 보내드립니다.\n\n감사합니다."
        
        # yagmail은 파일 경로 리스트만 넘기면 자동으로 첨부됨
        yag.send(
            to=to_email,
            subject=subject,
            contents=contents,
            attachments=[pdf_path]
        )
        return True
    except Exception as e:
        print(f"메일 전송 에러 발생: {e}")
        return False

def send_admin_alert(name, cert_type):
    """신청 발생 시 관리자에게 간단 알림"""
    email_addr = os.environ.get("EMAIL_ADDRESS_01")
    email_pw = os.environ.get("APP_PASSWORD_01")
    
    if not email_addr or not email_pw: 
        return
        
    try:
        yag = yagmail.SMTP(email_addr, email_pw)
        subject = f"[신청접수] {name} 강사님 - {cert_type}"
        contents = f"새로운 증명서 신청이 들어왔습니다.\n\n신청자: {name}\n종류: {cert_type}\n인트라넷에서 확인 후 발급해 주세요."
        
        yag.send(to=ADMIN_NOTIFICATION_EMAIL, subject=subject, contents=contents)
    except:
        pass

@document_bp.route('/pdf/<filename>')
def serve_pdf(filename):
    """관리자 페이지에서 발급된 PDF 보기"""
    if 'emp_no' not in session: return abort(403)
    return send_from_directory(PDF_FOLDER, filename)

@document_bp.route('/delete/<int:idx>')
def delete_record(idx):
    """신청 기록 및 파일 삭제"""
    if 'emp_no' not in session: return abort(403)
    try:
        df = pd.read_excel(DATA_PATH, dtype=str).fillna("")
        if idx in df.index:
            filename = df.at[idx, '파일명']
            if filename:
                p = os.path.join(PDF_FOLDER, filename)
                if os.path.exists(p): os.remove(p)
                
            df = df.drop(index=idx)
            df.to_excel(DATA_PATH, index=False)
            flash("기록이 성공적으로 삭제되었습니다.")
    except Exception as e:
        flash(f"삭제 중 오류: {str(e)}")
    return redirect(url_for('document.admin_list'))

# 안내 메일 전송 기능 (admin.html 모달 전송용)
@document_bp.route('/send_simple_email', methods=['POST'])
def send_simple_email():
    if 'emp_no' not in session: return abort(403)
    email = request.form.get('email')
    subject = request.form.get('subject')
    body = request.form.get('body')
    
    email_addr = os.environ.get("EMAIL_ADDRESS_01")
    email_pw = os.environ.get("APP_PASSWORD_01")
    
    if not email_addr or not email_pw:
        flash("메일 발송 실패: 메일 서버 환경변수가 설정되지 않았습니다.")
        return redirect(url_for('document.admin_list'))
        
    try:
        yag = yagmail.SMTP(email_addr, email_pw)
        yag.send(to=email, subject=subject, contents=body)
        flash("이메일이 성공적으로 발송되었습니다.")
    except Exception as e:
        flash(f"이메일 발송 실패: {str(e)}")
        
    return redirect(url_for('document.admin_list'))

@document_bp.route('/edit', methods=['POST'])
def edit_record_post():
    """모달창에서 전송된 수정 데이터를 엑셀에 반영"""
    if 'emp_no' not in session: return abort(403)
    
    try:
        idx = int(request.form.get('idx'))
        df = pd.read_excel(DATA_PATH, dtype=str).fillna("")
        
        if idx in df.index:
            fields = ['증명서종류', '성명', '주민번호', '자택주소', '근무시작일', 
                      '근무종료일', '근무장소', '강의과목', '직책', '용도', '종료사유', '이메일주소']
            for field in fields:
                if field in request.form:
                    df.at[idx, field] = request.form.get(field)
                    
            df.to_excel(DATA_PATH, index=False)
            flash("신청 정보가 성공적으로 수정되었습니다.")
        else:
            flash("해당 데이터를 찾을 수 없습니다.")
    except Exception as e:
        flash(f"수정 중 오류 발생: {str(e)}")
        
    return redirect(url_for('document.admin_list'))