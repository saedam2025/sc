from flask import Flask, session, redirect, url_for, request, render_template
import os
from routes.main import main_bp
from routes.document import document_bp
from routes.contract import contract_bp
from routes.user_mgmt import user_mgmt_bp
from routes.approval import approval_bp
from routes.board import board_bp
from routes.db_handler import init_files

app = Flask(__name__)
# 세션 암호화를 위한 키
app.secret_key = os.environ.get("SECRET_KEY", "saedam_2026_secure_key_7777")

# 서버 시작 시 엑셀 파일들 초기화
init_files()

# 로그인 체크 예외 대상 경로
EXEMPT_ROUTES = [
    'user_mgmt.login_page', 
    'user_mgmt.login', 
    'user_mgmt.register', 
    'user_mgmt.invite_page', 
    'static'
]

@app.before_request
def check_login():
    # 제외 대상 경로이거나 정적 파일인 경우 통과
    if request.endpoint in EXEMPT_ROUTES or (request.path and request.path.startswith('/static')):
        return None
    
    # 세션에 사번(emp_no) 정보가 없으면 로그인 페이지로 강제 이동
    if 'emp_no' not in session:
        return redirect(url_for('user_mgmt.login_page'))

# Blueprint 등록 (모든 메뉴 연결)
app.register_blueprint(main_bp)
app.register_blueprint(document_bp, url_prefix='/document')
app.register_blueprint(contract_bp, url_prefix='/contract')
app.register_blueprint(user_mgmt_bp, url_prefix='/user')
app.register_blueprint(approval_bp, url_prefix='/approval')
app.register_blueprint(board_bp, url_prefix='/board')

@app.errorhandler(404)
def page_not_found(e):
    return "페이지를 찾을 수 없습니다. 경로를 확인해주세요.", 404

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)