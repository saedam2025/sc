from flask import Flask, session, redirect, url_for, request, render_template, jsonify
from datetime import datetime
import os
import sys
import traceback

# 배포 환경에서 모듈 임포트 에러 방지를 위해 현재 디렉토리를 시스템 경로에 추가
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# 블루프린트 임포트
from routes.main import main_bp
from routes.document import document_bp
from routes.contract import contract_bp
from routes.user_mgmt import user_mgmt_bp
from routes.approval import approval_bp
from routes.board import board_bp
from routes.payroll import payroll_bp
from routes.memo import memo_bp
from routes.attendance import attendance_bp
from routes.excel_generator import excel_bp
from routes.explorer import explorer_bp
from routes.notifications import noti_bp  # 나의 업무 알림 위젯 시스템
from routes.gallery import gallery_bp
from routes.school_bp import school_bp            # [기존] 학교업무공간 블루프린트
from routes.school_task import school_task_bp   # [신규 추가] 학교 업무 관리 블루프린트

# 데이터베이스 모듈 임포트
from routes.database import get_db, init_db

app = Flask(__name__)

# =====================================================================
# [DB 초기화 로직] 배포 환경에서도 안전하게 실행
# =====================================================================
with app.app_context():
    try:
        init_db()
        print("✅ 데이터베이스 초기화 및 필수 폴더 생성 완료.")
    except Exception as e:
        print(f"❌ 데이터베이스 초기화 실패: {e}")

    # 필수 정적 폴더 확인
    os.makedirs('static', exist_ok=True)
# =====================================================================

# 세션 보안 설정
app.secret_key = os.environ.get("SECRET_KEY", "saedam_2026_secure_key_1234")

# 로그인 체크 제외 대상 (정적 파일 및 외부 서비스 경로)
EXEMPT_ROUTES = [
    'login_page', 
    'login', 
    'logout', 
    'user_mgmt.register', 
    'user_mgmt.invite_page', 
    'static',
    # 강사 계약 시스템 (외부 강사 본인인증)
    'contract.login', 
    'contract.contract_list', 
    'contract.contract', 
    'contract.save_contract', 
    # 증명서 신청 시스템 (외부 강사용)
    'document.apply',
]

@app.before_request
def check_login():
    # 1. 예외 경로이거나 정적 파일 요청이면 통과
    if request.endpoint in EXEMPT_ROUTES or (request.path and request.path.startswith('/static')):
        return None
    
    # 2. 세션에 사번(emp_no)이 없으면 로그인 페이지로 이동
    if 'emp_no' not in session:
        return redirect(url_for('login_page'))

# =====================================================================
# [전역 변수 설정] 모든 템플릿에서 로그인 사용자 정보와 사진 경로를 바로 사용 가능하게 함
# =====================================================================
@app.context_processor
def inject_user_data():
    return {
        'current_user': session.get('user_name'),
        'current_user_profile_path': session.get('profile_path') # 대시보드 사진 표시용
    }

# --- 로그인/로그아웃 로직 ---

@app.route('/login_page')
def login_page():
    # 🚀 [추가] 관리자 계정 존재 여부 확인 로직
    try:
        conn = get_db()
        admin = conn.execute("SELECT id FROM users WHERE emp_no = 'admin'").fetchone()
        conn.close()
        
        # 관리자가 없으면 로그인 페이지 대신 최초 설정 화면(user_list.html) 렌더링
        if not admin:
            return render_template('user_list.html', mode='admin_setup')
    except Exception as e:
        print(f"로그인 페이지 관리자 체크 오류: {e}")

    return render_template('login.html')

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    
    # 🚀 [추가] 최고관리자 최초 설정 처리 (POST 응답)
    if data.get('action') == 'setup_admin':
        try:
            password = data.get('password')
            if not password:
                return jsonify({"status": "error", "message": "비밀번호를 입력해주세요."}), 400
                
            conn = get_db()
            today = datetime.now().strftime('%Y-%m-%d')
            conn.execute('''
                INSERT INTO users (emp_no, name, password, position, level, rrn, email, status, join_date, profile_icon, department)
                VALUES ('admin', 'admin', ?, '최고관리자', 1, '-', 'admin@admin.com', '승인', ?, '👑', '본부')
            ''', (password, today))
            conn.commit()
            conn.close()
            return jsonify({"status": "success", "message": "최고관리자 설정 완료! 이제 로그인하세요."})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    # --- 기존 로그인 로직 ---
    emp_no = str(data.get('emp_no', '')).strip()
    password = str(data.get('password', '')).strip()
    
    conn = get_db()
    user_row = conn.execute("SELECT * FROM users WHERE emp_no=? AND password=?", (str(emp_no), str(password))).fetchone()
    
    if not user_row:
        conn.close()
        return jsonify({"status": "error", "message": "사번 또는 비밀번호가 틀립니다."}), 401

    # 🚀 [해결] sqlite3.Row 객체를 안전한 파이썬 딕셔너리로 변환하여 KeyError 및 AttributeError 원천 차단
    user = dict(user_row)

    if user.get('status') != '승인':
        conn.close()
        return jsonify({"status": "error", "message": "승인이 대기 중인 계정입니다."}), 403
    
    # 세션 정보 안전하게 저장
    session['emp_no'] = str(user.get('emp_no', ''))
    session['user_name'] = user.get('name', '알수없음')
    session['user_level'] = int(user.get('level', 14))
    
    # 메인 화면 소속/직급 표시용 데이터 추가
    session['position'] = str(user.get('position', '미지정'))
    session['department'] = str(user.get('department', '소속미지정'))
    session['role'] = str(user.get('position', '미지정'))
    
    session['profile_path'] = user.get('profile_path', '')
    session['profile_icon'] = user.get('profile_icon') or user.get('아이콘') or '👤'

    conn.close()
    
    return jsonify({"status": "success"})

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

@app.route('/chat_popup/<partner>')
def chat_popup(partner):
    current_user = session.get('user_name', '알수없음')
    return render_template('chat_popup.html', partner=partner, current_user=current_user)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login_page'))

# =====================================================================
# [Blueprint 등록]
# =====================================================================
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
app.register_blueprint(noti_bp)  # 나의 업무 알림
app.register_blueprint(gallery_bp) 

# 학교 업무 관련 블루프린트 설정
app.register_blueprint(school_bp, url_prefix='/school')

# [신규 연동] school_task.py 블루프린트를 /school/tasks 경로로 매핑
app.register_blueprint(school_task_bp, url_prefix='/school/tasks')

# 에러 핸들러
@app.errorhandler(404)
def page_not_found(e):
    return "페이지를 찾을 수 없습니다. 경로를 확인해주세요.", 404

@app.errorhandler(500)
def internal_server_error(e):
    error_details = traceback.format_exc()
    return f"""
    <div style="padding:20px; border: 5px solid red; background-color: #fff0f0; font-family: monospace;">
        <h1 style="color: red;">⚠️ 500 Internal Server Error 발생</h1>
        <p><strong>발생 위치 및 원인:</strong></p>
        <pre style="background: #eee; padding: 10px; overflow-x: auto;">{error_details}</pre>
        <hr>
        <p>💡 <b>도움말:</b> 어느 파일의 몇 번째 줄에서 에러가 났는지 확인해보세요.</p>
        <a href="/">메인으로 돌아가기</a>
    </div>
    """, 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)