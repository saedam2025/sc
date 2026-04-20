import os
import pandas as pd
import pdfkit
import yagmail
import smtplib
import shutil
import platform
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

# 템플릿 경로 설정
TEMPLATE_PATH = os.path.join(os.getcwd(), "templates", "certificate", "certificate_template.html")

os.makedirs(PDF_FOLDER, exist_ok=True)

ADMIN_NOTIFICATION_EMAIL = "edu197@naver.com"

# =====================================================================
# [수정된 부분: PDF 엔진 설정] - 윈도우 에러 방지 처리 추가
# =====================================================================
if platform.system() == 'Windows':
    # 윈도우 로컬 환경에서는 기본적으로 C드라이브 경로를 찾거나, 못 찾으면 None으로 처리
    WKHTMLTOPDF_PATH = shutil.which("wkhtmltopdf") or r"C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe"
else:
    # Render (리눅스) 환경
    WKHTMLTOPDF_PATH = shutil.which("wkhtmltopdf") or "/usr/bin/wkhtmltopdf"

try:
    PDF_CONFIG = pdfkit.configuration(wkhtmltopdf=WKHTMLTOPDF_PATH)
except OSError:
    PDF_CONFIG = None
    print("⚠️ [안내] wkhtmltopdf 실행 파일을 찾을 수 없어 PDF 변환 기능이 비활성화됩니다. (기본 앱 실행에는 문제 없음)")
# =====================================================================

# --- [환경변수 이름 contract.py와 동일하게 통일] ---
def get_email_credentials():
    email = os.environ.get("MAIL_USERNAME", "")
    pw = os.environ.get("MAIL_PASSWORD", "")
    return email.strip(), pw.strip()

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
    """인트라넷 관리자용 신청 현황 목록 (페이징 추가)"""
    if 'emp_no' not in session:
        return redirect(url_for('login_page'))
    
    ensure_db_initialized()
    df = pd.read_excel(DATA_PATH, dtype=str).fillna("")
    
    df_with_idx = df.copy()
    df_with_idx['index'] = df.index
    submissions_all = df_with_idx.iloc[::-1].to_dict(orient='records')
    
    # --- 페이징 처리 로직 ---
    page = request.args.get('page', 1, type=int)
    per_page = 10  # 한 페이지당 10개씩 노출
    total_count = len(submissions_all)
    total_pages = (total_count + per_page - 1) // per_page
    
    if page < 1: page = 1
    if page > total_pages and total_pages > 0: page = total_pages
    
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    paginated_submissions = submissions_all[start_idx:end_idx]
    
    # 페이지네이션 10개 단위 블록
    block_size = 10
    current_block = (page - 1) // block_size + 1
    start_page = (current_block - 1) * block_size + 1
    end_page = min(start_page + block_size - 1, total_pages)
    
    return render_template('certificate/admin.html', 
                           submissions=paginated_submissions,
                           total=total_count,
                           pending=len(df[df['상태'] == '대기']),
                           page=page,
                           total_pages=total_pages,
                           start_page=start_page,
                           end_page=end_page)

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
        
        # 메일 발송 전 엑셀 데이터 먼저 업데이트 (발급 자체는 성공하도록)
        df.at[idx, '상태'] = '발급완료'
        df.at[idx, '발급일'] = now_kst().strftime("%Y-%m-%d")
        df.at[idx, '발급번호'] = issue_no
        df.at[idx, '파일명'] = os.path.basename(pdf_path)
        df.to_excel(DATA_PATH, index=False)

        # 메일 발송 (성공 여부와 상세 에러 메시지 반환)
        mail_success, err_msg = send_email_to_instructor(row.get('이메일주소', ''), row['성명'], pdf_path, row['증명서종류'])
        
        if mail_success:
            flash(f"{row['성명']} 님께 증명서 발송을 완료했습니다.")
        else:
            flash(f"발급은 완료되었으나, 메일 전송이 실패했습니다.\n사유: {err_msg}")
            
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
    
    # 윈도우에서 PDF_CONFIG가 없어도(None) 에러가 나지 않도록 조건 처리
    if PDF_CONFIG:
        pdfkit.from_string(html_content, output_path, configuration=PDF_CONFIG, options=options)
    else:
        # PDF_CONFIG가 없다면 (로컬 개발 환경) 그냥 빈 파일 생성 또는 에러 우회
        with open(output_path, "w", encoding="utf-8") as text_file:
            text_file.write("PDF 생성 환경이 설정되지 않았습니다. (로컬 테스트용 텍스트 파일)")
        
    return output_path

# --- [이중 방어벽이 적용된 이메일 발송 함수들] ---
def send_email_to_instructor(to_email, name, pdf_path, cert_type):
    """생성된 PDF를 첨부하여 강사에게 메일 발송 (yagmail -> smtplib 587 이중 시도)"""
    if not to_email or str(to_email).strip() == "":
        return False, "수신자(강사님)의 이메일 주소가 비어있습니다."
        
    email_addr, email_pw = get_email_credentials()
    if not email_addr or not email_pw:
        return False, "구글 계정/앱비밀번호 환경변수가 누락되었습니다."
        
    subject = f"[(사)새담] 요청하신 {cert_type} 발송 안내 ({name} 강사님)"
    contents = f"{name} 강사님, 안녕하세요.\n\n새담청소년교육문화원입니다.\n요청하신 {cert_type}를 첨부파일로 보내드립니다.\n\n감사합니다."
    
    # [1차 시도] yagmail 사용
    try:
        yag = yagmail.SMTP(email_addr, email_pw)
        yag.send(to=to_email, subject=subject, contents=contents, attachments=[pdf_path])
        return True, ""
    except Exception as yag_err:
        # [2차 시도] yagmail 실패 시 smtplib 포트 587 (TLS) 직접 연결 방식으로 우회 발송
        try:
            msg = MIMEMultipart()
            msg['From'] = email_addr
            msg['To'] = to_email
            msg['Subject'] = subject
            msg.attach(MIMEText(contents, 'plain'))
            
            with open(pdf_path, "rb") as f:
                part = MIMEApplication(f.read(), _subtype="pdf")
                part.add_header('Content-Disposition', 'attachment', filename=os.path.basename(pdf_path))
                msg.attach(part)
                
            server = smtplib.SMTP("smtp.gmail.com", 587)
            server.starttls()  # 보안 연결
            server.login(email_addr, email_pw)
            server.send_message(msg)
            server.quit()
            return True, ""
        except Exception as smtp_err:
            return False, f"서버 거부 (yagmail: {str(yag_err)} / smtplib: {str(smtp_err)})"

def send_admin_alert(name, cert_type):
    """신청 발생 시 관리자에게 간단 알림"""
    email_addr, email_pw = get_email_credentials()
    if not email_addr or not email_pw: return
    
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
    """신청 기록 및 파일 단건 삭제"""
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

@document_bp.route('/delete_multiple', methods=['POST'])
def delete_multiple():
    """여러 건 동시 선택 삭제"""
    if 'emp_no' not in session: return abort(403)
    try:
        selected_idxs = request.form.getlist('chk_ids')
        if not selected_idxs:
            flash("삭제할 항목이 선택되지 않았습니다.")
            return redirect(url_for('document.admin_list'))
            
        df = pd.read_excel(DATA_PATH, dtype=str).fillna("")
        deleted_count = 0
        
        for idx_str in selected_idxs:
            idx = int(idx_str)
            if idx in df.index:
                filename = df.at[idx, '파일명']
                if filename:
                    p = os.path.join(PDF_FOLDER, filename)
                    if os.path.exists(p): os.remove(p)
                df = df.drop(index=idx)
                deleted_count += 1
                
        df.to_excel(DATA_PATH, index=False)
        flash(f"총 {deleted_count}건의 기록이 성공적으로 삭제되었습니다.")
    except Exception as e:
        flash(f"선택 삭제 중 오류: {str(e)}")
        
    return redirect(url_for('document.admin_list'))

# 안내 메일 전송 기능 (admin.html 모달 전송용)
@document_bp.route('/send_simple_email', methods=['POST'])
def send_simple_email():
    if 'emp_no' not in session: return abort(403)
    
    to_email = request.form.get('email', '').strip()
    subject = request.form.get('subject', '')
    body = request.form.get('body', '')
    
    if not to_email:
        flash("발송 실패: 수신자 이메일 주소를 확인해주세요.")
        return redirect(url_for('document.admin_list'))
        
    email_addr, email_pw = get_email_credentials()
    if not email_addr or not email_pw:
        flash("발송 실패: 서버 환경변수(구글 계정)가 설정되지 않았습니다.")
        return redirect(url_for('document.admin_list'))
        
    # [1차 시도] yagmail
    try:
        yag = yagmail.SMTP(email_addr, email_pw)
        yag.send(to=to_email, subject=subject, contents=body)
        flash("이메일이 성공적으로 발송되었습니다.")
    except Exception as yag_e:
        # [2차 시도] smtplib 587
        try:
            msg = MIMEText(body)
            msg['Subject'] = subject
            msg['From'] = email_addr
            msg['To'] = to_email
            
            server = smtplib.SMTP("smtp.gmail.com", 587)
            server.starttls()
            server.login(email_addr, email_pw)
            server.send_message(msg)
            server.quit()
            flash("이메일이 성공적으로 발송되었습니다. (보조 발송 라인 이용)")
        except Exception as smtp_e:
            flash(f"메일 발송 완전 실패. 상세 원인:\n{str(smtp_e)}")
            
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