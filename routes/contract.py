from flask import Blueprint, render_template, request, jsonify, redirect, url_for, session, send_file, abort
import pandas as pd
import os
import zipfile
import io
import pdfkit
import yagmail
from datetime import datetime, timedelta, timezone
from hashids import Hashids

# Blueprint 생성 (인트라넷 통합용)
contract_bp = Blueprint('contract', __name__)

hashids = Hashids(salt="saedam_secret_salt", min_length=8)

# --- [저장 경로 설정: 인트라넷 구조에 맞춤] ---
# Render 배포 환경(/mnt/data) 및 로컬 환경 자동 대응
if os.path.exists('/mnt/data'):
    MOUNT_PATH = '/mnt/data'
else:
    MOUNT_PATH = os.getcwd()

# 엑셀 및 PDF 저장 폴더 설정
EXCEL_FILE = os.path.join(MOUNT_PATH, 'admin_list.xlsx')
CONTRACTS_DIR = os.path.join(MOUNT_PATH, 'contracts')
# 약관 폴더는 프로젝트 최상위의 terms 폴더를 참조
TERMS_DIR = os.path.join(os.getcwd(), 'terms') 

if not os.path.exists(CONTRACTS_DIR):
    os.makedirs(CONTRACTS_DIR)

# [설정] 리눅스 표준 wkhtmltopdf 경로
WKHTMLTOPDF_PATH = '/usr/bin/wkhtmltopdf'
PDF_CONFIG = pdfkit.configuration(wkhtmltopdf=WKHTMLTOPDF_PATH)

SENDER_EMAIL = os.environ.get('MAIL_USERNAME')
SENDER_PASSWORD = os.environ.get('MAIL_PASSWORD')
ADMIN_PASSWORD = 'school97$$'
KST = timezone(timedelta(hours=9))

def format_value(val):
    """소수점(0.85)은 퍼센트(85%)로, 큰 숫자는 콤마(,) 형식으로 변환"""
    if not val or pd.isna(val) or str(val).strip() == "":
        return ""
    val = str(val).strip()
    try:
        num = float(val)
        if 0 < num < 1:
            return f"{int(num * 100)}%"
        if num >= 100:
            return "{:,}".format(int(num))
    except ValueError:
        pass
    return val

def init_excel():
    """엑셀 초기화 (모든 읽기 작업에 dtype=str 적용)"""
    columns = [
        '계약구분', '수탁학교명', '부서명', '성명', '주민번호', '수수료', '보조금', '경력수당', '직책수당', '기타', '근무시간', '계약기간', 'email', '연락처', '거주지', '계약완료일시', '연도', '파일명', 'IP'
    ]
    if not os.path.exists(EXCEL_FILE):
        df = pd.DataFrame(columns=columns)
        df.to_excel(EXCEL_FILE, index=False)
    else:
        df = pd.read_excel(EXCEL_FILE, dtype=str)
        for col in columns:
            if col not in df.columns:
                df[col] = ""
        df = df.reindex(columns=columns)
        df.to_excel(EXCEL_FILE, index=False)

# 서버 시작 시 엑셀 체크
init_excel()

# --- [수정된 zip 다운로드 로직] ---

@contract_bp.route('/admin/download_selected')
def download_selected_contracts():
    id_param = request.args.get('ids', '')
    if not id_param:
        return "<script>alert('선택된 항목이 없습니다.'); history.back();</script>", 400
        
    try:
        target_indices = [int(i) for i in id_param.split(',')]
        df = pd.read_excel(EXCEL_FILE, dtype=str).fillna("")
        memory_file = io.BytesIO()
        
        with zipfile.ZipFile(memory_file, 'w') as zf:
            file_count = 0
            for idx in target_indices:
                if idx in df.index:
                    filename = df.at[idx, '파일명']
                    if filename:
                        file_path = os.path.join(CONTRACTS_DIR, filename)
                        if os.path.exists(file_path):
                            zf.write(file_path, arcname=filename)
                            file_count += 1
            
            if file_count == 0:
                return "<script>alert('선택한 항목 중 작성 완료된 PDF 파일이 없습니다.'); history.back();</script>"
        
        memory_file.seek(0)
        current_time = datetime.now(KST).strftime('%Y%m%d_%H%M%S')
        return send_file(
            memory_file,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f'selected_contracts_{current_time}.zip'
        )
    except Exception as e:
        return f"다운로드 중 오류 발생: {str(e)}", 500

# --- [강사 서비스 로직] ---

@contract_bp.route('/')
def home():
    return redirect(url_for('contract.login'))

@contract_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        # 폼 데이터와 JSON 데이터 모두 대응 가능하도록 수정
        if request.is_json:
            data = request.json
            name = data.get('name')
            ssn_raw = data.get('ssn', '')
            ssn_last4 = data.get('ssn_last4')
        else:
            name = request.form.get('name')
            ssn_raw = request.form.get('ssn', '')
            ssn_last4 = request.form.get('ssn_last4')

        ssn = ssn_raw.replace("-", "")
        
        try:
            df = pd.read_excel(EXCEL_FILE, dtype=str)
            # 주민번호 비교 시 하이픈 제거 후 비교
            user_rows = df[(df['성명'] == name) & (df['주민번호'].astype(str).str.replace("-", "") == ssn)]
            
            if not user_rows.empty and ssn[-4:] == ssn_last4:
                session['contract_user_name'] = name
                session['contract_user_ssn'] = ssn_raw 
                return redirect(url_for('contract.contract_list'))
            return "<script>alert('정보가 일치하지 않습니다.'); history.back();</script>"
        except Exception as e:
            return f"에러: {str(e)}"
    return render_template('contract/login.html')

@contract_bp.route('/list')
def contract_list():
    if 'contract_user_name' not in session: return redirect(url_for('contract.login'))
    df = pd.read_excel(EXCEL_FILE, dtype=str).fillna("")
    my_contracts_df = df[(df['성명'] == session['contract_user_name']) & (df['주민번호'].astype(str) == session['contract_user_ssn']) & (df['계약완료일시'] == "")]
    contracts = []
    for idx, row in my_contracts_df.iterrows():
        item = row.to_dict()
        item['safe_id'] = hashids.encode(idx) 
        contracts.append(item)
    return render_template('contract/list.html', contracts=contracts, name=session['contract_user_name'])

@contract_bp.route('/contract/<string:safe_id>')
def contract(safe_id):
    if 'contract_user_name' not in session or 'contract_user_ssn' not in session: return redirect(url_for('contract.login'))
    decoded = hashids.decode(safe_id)
    if not decoded: return abort(404)
    orig_idx = decoded[0]
    try:
        df = pd.read_excel(EXCEL_FILE, dtype=str)
        if orig_idx >= len(df): return abort(404)
        target_row = df.iloc[orig_idx]
        if target_row['성명'] != session.get('contract_user_name'):
            return "<script>alert('해당 계약서에 대한 접근 권한이 없습니다.'); location.href='/contract/list';</script>"
        user_data = target_row.to_dict()
        user_data['orig_idx'] = orig_idx
        contract_type = user_data.get('계약구분', '방과후강사')
        
        def load_and_replace(filename):
            path = os.path.join(TERMS_DIR, filename)
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    c = f.read()
                    for col in df.columns:
                        raw_val = user_data.get(col, '')
                        if col in ['수수료', '보조금', '경력수당', '직책수당', '기타']:
                            val = format_value(raw_val)
                        else:
                            val = str(raw_val) if pd.notna(raw_val) else ""
                        
                        c = c.replace(f"{{{{ data.{col} }}}}", val)
                        display_style = "display:none" if not val or val == '0' or str(val).strip() == '' else "display:table-row"
                        c = c.replace(f"{{{{ style.{col} }}}}", display_style)
                    return c.replace('\n', '<br>').replace('<br><table', '<table').replace('</table><br>', '</table>')
            return ""
            
        user_data['terms_content1'] = load_and_replace(f"{contract_type}.txt")
        user_data['terms_content2'] = load_and_replace(f"{contract_type}2.txt")
        return render_template('contract/contract.html', data=user_data)
    except Exception as e: return f"에러 발생: {str(e)}", 500

@contract_bp.route('/save_contract', methods=['POST'])
def save_contract():
    data = request.get_json(silent=True)

    if not data:
        data = request.form.to_dict()

    if not data:
        return jsonify({"status": "error", "message": "데이터 없음"}), 400

    idx = int(data['orig_idx'])

    now_dt = datetime.now(KST)
    try:
        df = pd.read_excel(EXCEL_FILE, dtype=str)
        if (str(df.at[idx, '성명']) != session.get('contract_user_name')):
             return jsonify({"status": "error", "message": "잘못된 접근입니다."}), 403
        
        contract_type = df.at[idx, '계약구분']
        
        config = {
            '방과후강사': ("새담청소년교육문화원 위탁교육계약서", "수탁학교 :", "담당부서 :"),
            '맞춤형강사': ("새담청소년교육문화원 위탁교육계약서", "수탁학교 :", "담당부서 :"),
            '코디근로자': ("새담청소년교육문화원 센터장 계약서", "수탁학교 :", "직책 :"),
            '코디사업자': ("새담청소년교육문화원 센터장 계약서", "수탁학교 :", "직책 :"),
            '원어민근로자': ("방과후 영어 원어민 강사 위탁 계약서", "School Name :", "Part :"),
            '원어민사업자': ("방과후 영어 원어민 강사 위탁 계약서", "School Name :", "Part :"),
            '안전코디': ("새담청소년교육문화원 위수탁계약서", "수탁학교 :", "직책 :"),
            '직원근로자': ("새담청소년교육문화원 근로계약서", "기관명 :", "직책 :"),
            '직원사업자': ("새담청소년교육문화원 위탁업무계약서", "기관명 :", "위탁업무 :")
        }
        doc_title, school_label, dept_label = config.get(contract_type, (f"새담청소년교육문화원 계약서 ({contract_type})", "수탁학교 :", "담당부서 :"))
        final_school_name = "새담청소년교육문화원" if contract_type in ['직원근로자', '직원사업자'] else data.get('school', '')
        
        stamp_path = os.path.abspath(os.path.join(os.getcwd(), 'static', 'stamp7.png'))
        stamp_uri = f"file://{stamp_path}"

        signature_section = f"""
        <div class="signature-area" style="margin-top: 40px; position: relative; min-height: 150px;">
            <p style="text-align: center; margin-bottom: 50px;">{now_dt.strftime('%Y년 %m월 %d일')}</p>
            <br>
            <div style="float: left; width: 50%; position: relative;">
                <p><b>[위탁자]</b></p>
                <p style="font-size: 20px; line-height: 1.6; position: relative; width: 280px;">
               (사)새담청소년교육문화원
             <span style="display: block; text-align: right; padding-right: 64px;">이사장</span>
            <img src="{stamp_uri}" style="position: absolute; right: -60; bottom: -10px; width: 90px;">
                </p>
            </div>
            <div style="float: right; width: 45%; text-align: left;">
                <p><b>[수탁자]</b></p>
                <p style="line-height: 40px;">
                    성명: {data['name']} <br>
                    서명: <img src="{data['signature']}" style="width: 200px; border-bottom: 1px solid #000; vertical-align: middle; margin-left: 10px;">
                </p>
            </div>
            <div style="clear: both;"></div>
        </div>
        """

        def get_cleaned_content(filename):
            path = os.path.join(TERMS_DIR, filename)
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    c = f.read()
                    for col in df.columns:
                        raw_val = str(final_school_name) if col == '수탁학교명' else (str(df.at[idx, col]) if pd.notna(df.at[idx, col]) else "")
                        if col in ['수수료', '보조금', '경력수당', '직책수당', '기타']:
                            val = format_value(raw_val)
                        else:
                            val = raw_val
                        c = c.replace(f"{{{{ data.{col} }}}}", val)
                        display_style = "display:none" if not val or val == '0' or str(val).strip() == '' else "display:table-row"
                        c = c.replace(f"{{{{ style.{col} }}}}", display_style)
                    return c.replace('\n', '<br>').replace('<br><table', '<table').replace('</table><br>', '</table>')
            return ""

        content1, content2 = get_cleaned_content(f"{contract_type}.txt"), get_cleaned_content(f"{contract_type}2.txt")
        
        html_content = f"""
        <html><head><meta charset="UTF-8">
        <link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;700;900&display=swap" rel="stylesheet">
        <style>
            @page {{ size: A4; margin: 25mm 20mm; }} 
            body {{ margin: 0; padding: 0; font-family: 'Noto Sans KR', sans-serif; background-color: #fff; color: #000; }} 
            .document-wrapper {{ position: relative; z-index: 1; }} 
            .title {{ text-align: center; font-size: 28px; font-weight: bold; margin-bottom: 35px; text-decoration: underline; }} 
            .info-table {{ width: 100%; border-collapse: collapse; margin-bottom: 30px; font-size: 15px; border: none; }} 
            .info-table th, .info-table td {{ border: none; padding: 8px 5px; text-align: left; }} 
            .info-table th {{ font-weight: bold; width: 15%; color: #333; }} 
            .info-table td {{ width: 35%; border-bottom: 1px solid #eee; }} 
            .terms-area {{ text-align: justify; line-height: 1.6; font-size: 14.5px; margin-top: 10px; word-break: keep-all; }} 
            .signature-area {{ margin-top: 50px; position: relative; font-size: 16px; }} 
        </style></head>
        <body><div class="document-wrapper"><div class="title"><h1 style="text-align:center; line-height:1.4; margin-bottom:30px;"><span style="display:block; font-family:'Noto Sans KR', sans-serif; font-weight:900; font-size:26px; letter-spacing:-0.03em; color:#222;">{doc_title}</span></h1></div><br><table class="info-table"><tr><th>{school_label}</th><td>{final_school_name}</td><th>{dept_label}</th><td>{data.get('dept', '')}</td></tr><tr><th>성명 :</th><td>{data.get('name', '')}</td><th>주민번호 :</th><td>{data.get('ssn', session.get('contract_user_ssn', ''))}</td></tr><tr><th>연락처 :</th><td>{data.get('phone', '')}</td><th>이메일 :</th><td>{data.get('email', '')}</td></tr><tr><th>거주지 :</th><td colspan="3">{data.get('address', '')}</td></tr></table><br><div class="terms-area">{content1}</div>{signature_section}{"<div style='page-break-before: always;'></div>" if content2.strip() else ""}{f"<div class='terms-area' style='margin-top:10mm;'>{content2}</div>{signature_section}" if content2.strip() else ""}</div></body></html>
        """
        
        safe_school, safe_name = str(final_school_name).replace(' ', ''), str(data['name']).replace(' ', '')
        display_contract_type = "센터장" if contract_type in ['코디사업자', '코디근로자'] else contract_type
        filename = f"{display_contract_type}_{safe_school}_{safe_name}_{now_dt.strftime('%Y%m%d_%H%M%S')}.pdf"
        pdf_path = os.path.join(CONTRACTS_DIR, filename)
        
        pdfkit.from_string(html_content, pdf_path, configuration=PDF_CONFIG, options={'page-size': 'A4', 'encoding': "UTF-8", 'javascript-delay': '1000', 'enable-local-file-access': None, 'margin-top': '25', 'margin-bottom': '25', 'margin-left': '20', 'margin-right': '20'})
        
        user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        if user_ip and ',' in user_ip: user_ip = user_ip.split(',')[0].strip()

        df.at[idx, '연도'] = str(now_dt.year)
        df.at[idx, '연락처'] = str(data.get('phone', ''))
        df.at[idx, 'email'] = str(data.get('email', ''))
        df.at[idx, '거주지'] = str(data.get('address', ''))
        df.at[idx, '계약완료일시'] = now_dt.strftime('%Y-%m-%d %H:%M:%S')
        df.at[idx, '파일명'] = filename
        df.at[idx, 'IP'] = user_ip
        
        df.to_excel(EXCEL_FILE, index=False)
        
        try:
            target_user_email = str(data.get('email', '')).strip()
            if target_user_email and "@" in target_user_email:
                yag = yagmail.SMTP(SENDER_EMAIL, SENDER_PASSWORD)
                yag.send(to=[target_user_email, SENDER_EMAIL], subject=f"[계약완료] {data['name']}님 {doc_title} ({final_school_name})", contents=[f" '{doc_title}' 계약이 완료되었습니다. \n\n첨부된 파일을 확인하세요."], attachments=pdf_path)
        except: pass
        return jsonify({"status": "success", "message": "계약이 정상적으로 완료되었으며 이메일로 발송되었습니다."})
    except Exception as e: return jsonify({"status": "error", "message": f"오류 발생: {str(e)}"}), 500


# --- [관리자 기능 로직] ---
@contract_bp.route('/admin', methods=['GET', 'POST'])
def admin_page():
    # 1. 인트라넷 통합 로그인 체크 (세션에 사용자 이름이 있는지 확인)
    if not session.get('user_name'):
        return """
        <script>
            alert('인트라넷 로그인이 필요한 페이지입니다.');
            location.href = '/login'; 
        </script>
        """

    # 2. 관리자 권한(직급) 체크
    # 세션에 저장된 role(직급)이 관리자급인지 확인합니다.
    admin_roles = ['대표이사', '이사', '실장', 'admin']
    user_role = session.get('role')

    if user_role not in admin_roles:
        return """
        <script>
            alert('접근 권한이 없습니다. 관리자만 이용 가능합니다.');
            location.href = '/';
        </script>
        """

    # --- 권한 통과 시 기존 관리자 페이지 로직 수행 ---
    page = request.args.get('page', 1, type=int)
    per_page = 20
    s_year, s_cat, s_school, s_dept, s_name = (
        request.args.get('year', ''), 
        request.args.get('category', ''), 
        request.args.get('school', ''), 
        request.args.get('dept', ''), 
        request.args.get('name', '')
    )

    try:
        full_df = pd.read_excel(EXCEL_FILE, dtype=str).fillna("")
        
        total_count = len(full_df)
        completed_count = len(full_df[full_df['계약완료일시'].str.strip() != ""])
        pending_count = total_count - completed_count
        completion_rate = round((completed_count / total_count * 100), 1) if total_count > 0 else 0

        df = full_df.copy().sort_index(ascending=False)
        
        if s_year: 
            df = df[df['연도'].astype(str).str.contains(s_year)]
        if s_cat:
            if s_cat == '미작성':
                df = df[df['계약완료일시'].astype(str).str.strip() == ""]
            else:
                df = df[df['계약구분'] == s_cat]
        if s_school: 
            df = df[df['수탁학교명'] == s_school]
        if s_dept: 
            df = df[df['부서명'] == s_dept]
        if s_name: 
            df = df[df['성명'].str.contains(s_name)]

        years = sorted([str(y) for y in full_df['연도'].unique() if y != ""], reverse=True)
        schools = sorted([s for s in full_df['수탁학교명'].unique() if s != ""])
        depts = sorted([d for d in full_df['부서명'].unique() if d != ""])

        total_pages = (len(df) // per_page) + (1 if len(df) % per_page > 0 else 0)
        filtered_count = len(df)
        items = df.iloc[(page-1)*per_page : page*per_page].to_dict('records')
        
        page_indices = df.index[(page-1)*per_page : page*per_page]
        for i, item in enumerate(items):
            item['orig_idx'] = page_indices[i]

        display_size = 20 
        start_page = max(1, ((page - 1) // display_size) * display_size + 1)
        end_page = min(total_pages, start_page + display_size - 1)

        return render_template('contract/c_admin_.html', 
                               items=items, total_pages=total_pages, current_page=page,
                               start_page=start_page, end_page=end_page,
                               total_count=total_count, completed_count=completed_count,
                               pending_count=pending_count, completion_rate=completion_rate,
                               filtered_count=filtered_count, years=years, 
                               schools=schools, depts=depts)
    except Exception as e: 
        return f"에러: {str(e)}"

@contract_bp.route('/admin/upload_excel', methods=['POST'])
def upload_excel():
    if 'excel_file' not in request.files: return jsonify({'status': 'error', 'message': '파일 없음'}), 400
    file = request.files['excel_file']
    try:
        new_df = pd.read_excel(file, dtype=str)
        target_cols = ['수수료', '보조금', '경력수당', '직책수당', '기타']
        for col in target_cols:
            if col in new_df.columns:
                new_df[col] = new_df[col].apply(format_value)
        if '연도' not in new_df.columns: new_df['연도'] = ""
        else: new_df['연도'] = new_df['연도'].fillna("")

        existing_df = pd.read_excel(EXCEL_FILE, dtype=str) if os.path.exists(EXCEL_FILE) else pd.DataFrame()
        combined_df = pd.concat([existing_df, new_df], ignore_index=True)
        combined_df.to_excel(EXCEL_FILE, index=False)
        return jsonify({'status': 'success', 'message': f'{len(new_df)}명의 계약정보가 추가 되었습니다.'})
    except Exception as e: return jsonify({'status': 'error', 'message': str(e)}), 500

@contract_bp.route('/admin/add', methods=['POST'])
def admin_add():
    try:
        new_data = request.json
        df = pd.read_excel(EXCEL_FILE, dtype=str)
        new_row = {
            '계약구분': new_data.get('계약구분', '방과후강사'), '수탁학교명': new_data.get('수탁학교명'),
            '부서명': new_data.get('부서명'), '성명': new_data.get('성명'), '주민번호': new_data.get('주민번호'),
            '수수료': format_value(new_data.get('수수료', '0')), '보조금': format_value(new_data.get('보조금', '0')),
            '경력수당': format_value(new_data.get('경력수당', '0')), '직책수당': format_value(new_data.get('직책수당', '0')),
            '기타': format_value(new_data.get('기타', '0')), '근무시간': new_data.get('근무시간', ''),
            '계약기간': new_data.get('계약기간', ''), '연도': "", '계약완료일시': "", '파일명': "", 'IP': ""
        }
        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
        df.to_excel(EXCEL_FILE, index=False)
        return jsonify({'status': 'success'})
    except Exception as e: return jsonify({'status': 'error', 'message': str(e)}), 500

@contract_bp.route('/admin/delete', methods=['POST'])
def delete_contracts():
    indices = request.json.get('indices', [])
    try:
        df = pd.read_excel(EXCEL_FILE, dtype=str)
        for idx in [int(i) for i in indices]:
            if idx in df.index:
                filename = df.at[idx, '파일명']
                if filename and not pd.isna(filename):
                    p = os.path.join(CONTRACTS_DIR, str(filename))
                    if os.path.exists(p): os.remove(p)
        df = df.drop([int(i) for i in indices])
        df.to_excel(EXCEL_FILE, index=False)
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error", "message": str(e)})

@contract_bp.route('/download_pdf/<int:idx>')
def download_pdf(idx):
    try:
        df = pd.read_excel(EXCEL_FILE, dtype=str)
        pdf_path = os.path.join(CONTRACTS_DIR, str(df.at[idx, '파일명']))
        return send_file(pdf_path, mimetype='application/pdf')
    except: return "파일 없음", 404

@contract_bp.route('/admin/logout')
def admin_logout():
    # 전자계약 관리자 전용 세션은 이제 없으므로, 인트라넷 로그아웃 페이지로 이동시킵니다.
    return redirect(url_for('main.logout'))