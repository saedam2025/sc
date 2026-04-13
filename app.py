from flask import Flask, session, redirect, url_for, request, render_template, jsonify
from datetime import datetime  # [신규] 출퇴근 시간 기록을 위한 모듈 추가
import os
import sys
import traceback  # [신규] 에러 상세 분석을 위한 모듈

# 배포 환경에서 모듈 임포트 에러 방지를 위해 현재 디렉토리를 시스템 경로에 추가
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# 상대 경로 임포트 에러 수정을 위해 절대 경로 사용
from routes.main import main_bp
from routes.document import document_bp  # 증명서 및 서류 관리
from routes.contract import contract_bp
from routes.user_mgmt import user_mgmt_bp
from routes.approval import approval_bp
from routes.board import board_bp
from routes.payroll import payroll_bp  # 급여 명세서 발송 시스템
from routes.memo import memo_bp        # [신규] 개인 화이트보드 메모장 시스템
from routes.attendance import attendance_bp # [신규] 근태관리 시스템
from routes.excel_generator import excel_bp # [신규] 입금용 엑셀 생성 시스템
from routes.explorer import explorer_bp     # [신규 추가] 서버 스토리지 관리자 (히든 메뉴)
from routes.notifications import noti_bp    # [신규 추가] 나의 업무 알림 위젯 시스템
from routes.gallery import gallery_bp       # [신규 추가] 개인 갤러리 시스템

# 엑셀 대신 SQLite DB를 사용하도록 설정된 데이터베이스 모듈 임포트
from routes.database import get_db, init_db # [수정] init_db 추가

app = Flask(__name__)

# =====================================================================
# [핵심 수정] 배포 환경(gunicorn 등)에서도 DB 초기화가 무조건 실행되도록 
# if __name__ == '__main__': 블록 밖으로 꺼냈습니다.
# =====================================================================
with app.app_context():
    try:
        init_db()
        print("데이터베이스 초기화 및 폴더 생성 완료.")
    except Exception as e:
        print(f"데이터베이스 초기화 실패: {e}")

    # 업로드 파일 저장을 위한 필수 폴더 생성 (Render 영구 저장소 외 기본 구조)
    os.makedirs('static', exist_ok=True)
# =====================================================================

# 세션 보안을 위한 키 설정 (환경 변수 권장)
app.secret_key = os.environ.get("SECRET_KEY", "saedam_2026_secure_key_1234")

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
    'document.apply',           # 강사 경력증명서 직접 신청 페이지
    # --- 갤러리 복호화 서빙 경로 (필요 시 추가, 여기서는 세션 체크를 위해 일단 제외) ---
    'gallery.serve_thumb',      # 썸네일은 로그인 없이도 볼 수 있게 하려면 추가
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
    
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE emp_no=? AND password=?", (str(emp_no), str(password))).fetchone()
    
    if not user:
        conn.close()
        return jsonify({"status": "error", "message": "사번 또는 비밀번호가 틀립니다."}), 401

    if user['status'] != '승인':
        conn.close()
        return jsonify({"status": "error", "message": "승인이 대기 중인 계정입니다."}), 403
            
    # 최초 로그인 시 출근 처리 로직 (현재 직급 포함 저장)
    today_date = datetime.now().strftime('%Y-%m-%d')
    current_time = datetime.now().strftime('%H:%M:%S')

    attendance = conn.execute("SELECT * FROM daily_attendance WHERE emp_no=? AND date=?", (str(emp_no), today_date)).fetchone()

    if not attendance:
        try:
            conn.execute("INSERT INTO daily_attendance (emp_no, date, clock_in_time, status, position) VALUES (?, ?, ?, ?, ?)",
                         (str(emp_no), today_date, current_time, '근무중', user['position']))
            conn.commit()
        except Exception as e:
            print(f"근태 기록 생성 실패: {e}")

    conn.close()

    # 세션 저장
    session['emp_no'] = str(user['emp_no'])
    session['user_name'] = user['name']
    session['user_level'] = int(user['level'])
    session['role'] = str(user['position'])
    
    return jsonify({"status": "success"})


# --- 내 회원정보 조회 API (인트라넷 사용자용) ---
@app.route('/user/my_info')
def get_my_info():
    if 'emp_no' not in session:
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401
    
    try:
        conn = get_db()
        user_row = conn.execute("SELECT * FROM users WHERE emp_no=?", (session['emp_no'],)).fetchone()
        conn.close()
        
        if not user_row:
            return jsonify({"status": "error", "message": "정보를 찾을 수 없습니다."}), 404
            
        info_dict = dict(user_row)
        if 'password' in info_dict:
            del info_dict['password']
            
        return jsonify({"status": "success", "data": info_dict})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# --- [신규 추가] 새 창 팝업 쪽지함 라우트 ---
@app.route('/chat_popup/<partner>')
def chat_popup(partner):
    # 로그인 시 세션에 저장된 user_name을 현재 사용자로 넘겨줍니다.
    current_user = session.get('user_name', '알수없음')
    return render_template('chat_popup.html', partner=partner, current_user=current_user)


@app.route('/logout')
def logout():
    session.clear() # 모든 세션 파기
    return redirect(url_for('login_page'))

# --- Blueprint 등록 ---
app.register_blueprint(main_bp)
app.register_blueprint(document_bp, url_prefix='/document')
app.register_blueprint(contract_bp, url_prefix='/contract')
app.register_blueprint(user_mgmt_bp, url_prefix='/user')
app.register_blueprint(approval_bp, url_prefix='/approval')
app.register_blueprint(board_bp, url_prefix='/board')
app.register_blueprint(payroll_bp, url_prefix='/payroll')
app.register_blueprint(memo_bp, url_prefix='/memo')  
app.register_blueprint(attendance_bp)  
app.register_blueprint(excel_bp)       
app.register_blueprint(explorer_bp, url_prefix='/explorer')
app.register_blueprint(noti_bp)
app.register_blueprint(gallery_bp) # [등록 완료]

@app.errorhandler(404)
def page_not_found(e):
    return "페이지를 찾을 수 없습니다. 경로를 확인해주세요.", 404

# --- [신규] 500 인터널 서버 에러 상세 분석 핸들러 ---
@app.errorhandler(500)
def internal_server_error(e):
    # 에러의 전체 추적 경로(Traceback)를 가져옵니다.
    error_details = traceback.format_exc()
    # 화면에 친절하게 원인을 표시합니다. (개발 단계에서 매우 유용)
    return f"""
    <div style="padding:20px; border: 5px solid red; background-color: #fff0f0; font-family: monospace;">
        <h1 style="color: red;">⚠️ 500 Internal Server Error 발생</h1>
        <p><strong>발생 위치 및 원인:</strong></p>
        <pre style="background: #eee; padding: 10px; overflow-x: auto;">{error_details}</pre>
        <hr>
        <p>💡 <b>도움말:</b> 위 내용 중 가장 아래쪽 'File...' 문장을 보면 어느 파일의 몇 번째 줄에서 에러가 났는지 알 수 있습니다.</p>
        <a href="/">메인으로 돌아가기</a>
    </div>
    """, 500

if __name__ == '__main__':
    # Render 등 배포 환경의 포트 설정 대응
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)