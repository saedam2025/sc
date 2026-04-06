from flask import Flask, session, redirect, url_for, request, render_template, jsonify
import os
import sys

# 배포 환경에서 모듈 임포트 에러 방지를 위해 현재 디렉토리를 시스템 경로에 추가
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# 상대 경로 임포트 에러 수정을 위해 절대 경로 사용
from routes.main import main_bp
from routes.document import document_bp  # 증명서 및 서류 관리
from routes.contract import contract_bp
from routes.user_mgmt import user_mgmt_bp
from routes.approval import approval_bp
from routes.board import board_bp
from routes.payroll import payroll_bp  # [신규] 급여 명세서 발송 시스템

# 엑셀 대신 SQLite DB를 사용하도록 설정된 데이터베이스 모듈 임포트
from routes.database import get_db

app = Flask(__name__)

# 세션 보안을 위한 키 설정 (환경 변수 권장)
app.secret_key = os.environ.get("SECRET_KEY", "saedam_2026_secure_key_1234")

# 구글 메일 발송을 위한 환경 변수 (시스템 설정에 맞춰 수정 필요)
# os.environ['MAIL_USERNAME'] = 'saedam2025@gmail.com'
# os.environ['MAIL_PASSWORD'] = 'your_app_password'

# 로그인 체크 제외 대상 (인트라넷 로그인 관련, 정적 파일 + 외부용 서비스 경로)
EXEMPT_ROUTES = [
    'login_page', 
    'login', 
    'logout', 
    'user_mgmt.register', 
    'user_mgmt.invite_page', 
    'static',
    # --- 강사 계약 시스템 예외 경로 (외부 강사 본인인증용) ---
    'contract.login',           # 계약자 로그인/본인인증 페이지
    'contract.contract_list',   # 계약 목록
    'contract.contract',        # 계약서 보기
    'contract.save_contract',   # 계약 완료 및 서명 저장
    # --- 증명서 신청 시스템 예외 경로 (외부 강사용) ---
    'document.apply',           # 강사 경력증명서 직접 신청 페이지 (로그인 없이 접근 가능)
    # --- 급여 명세서 관련은 내부 직원이 사용하므로 제외 목록에 넣지 않음 ---
]

@app.before_request
def check_login():
    # 1. 예외 경로이거나 정적 파일 요청이면 로그인 체크 통과
    if request.endpoint in EXEMPT_ROUTES or (request.path and request.path.startswith('/static')):
        return None
    
    # 2. 세션에 사번(emp_no)이 없으면 인트라넷 로그인 페이지로 리다이렉트
    if 'emp_no' not in session:
        return redirect(url_for('login_page'))

# --- 로그인/로그아웃 총괄 로직 ---

@app.route('/login_page')
def login_page():
    return render_template('login.html')

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    emp_no = data.get('emp_no')
    password = data.get('password')
    
    # SQLite DB에서 사용자 정보 조회
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE emp_no=? AND password=?", (str(emp_no), str(password))).fetchone()
    conn.close()
    
    if not user:
        return jsonify({"status": "error", "message": "사번 또는 비밀번호가 틀립니다."}), 401

    if user['status'] != '승인':
        return jsonify({"status": "error", "message": "승인이 대기 중인 계정입니다."}), 403
            
    # 세션 저장 (직급 및 레벨 정보 저장)
    session['emp_no'] = str(user['emp_no'])
    session['user_name'] = user['name']
    session['user_level'] = int(user['level'])
    session['role'] = str(user['position']) # 직급 데이터를 세션에 저장
    
    return jsonify({"status": "success"})

# --- 내 회원정보 조회 API (인트라넷 사용자용) ---
@app.route('/user/my_info')
def get_my_info():
    if 'emp_no' not in session:
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401
    
    try:
        # SQLite DB에서 현재 로그인한 사용자 정보 조회
        conn = get_db()
        user_row = conn.execute("SELECT * FROM users WHERE emp_no=?", (session['emp_no'],)).fetchone()
        conn.close()
        
        if not user_row:
            return jsonify({"status": "error", "message": "정보를 찾을 수 없습니다."}), 404
            
        # 모든 정보를 딕셔너리로 변환 (보안을 위해 암호는 제외)
        info_dict = dict(user_row)
        if 'password' in info_dict:
            del info_dict['password']
            
        return jsonify({"status": "success", "data": info_dict})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/logout')
def logout():
    session.clear() # 모든 세션 파기
    return redirect(url_for('login_page'))

# --- Blueprint 등록 ---
app.register_blueprint(main_bp)
# 증명서 관리/신청 시스템 연결 (/document/apply 등)
app.register_blueprint(document_bp, url_prefix='/document')
# 강사 계약 시스템 연결
app.register_blueprint(contract_bp, url_prefix='/contract')
app.register_blueprint(user_mgmt_bp, url_prefix='/user')
app.register_blueprint(approval_bp, url_prefix='/approval')
app.register_blueprint(board_bp, url_prefix='/board')
# [신규 추가] 급여 명세서 발송 시스템 연결
app.register_blueprint(payroll_bp, url_prefix='/payroll')

@app.errorhandler(404)
def page_not_found(e):
    return "페이지를 찾을 수 없습니다. 경로를 확인해주세요.", 404

if __name__ == '__main__':
    # 업로드 파일 저장을 위한 필수 폴더 생성
    os.makedirs('static', exist_ok=True)
    
    # Render 등 배포 환경의 포트 설정 대응
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)