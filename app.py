from flask import Flask, render_template
import os

# 1. 기능별 블루프린트 임포트
from routes.main import main_bp
from routes.approval import approval_bp
from routes.document import document_bp
from routes.contract import contract_bp
from routes.user_mgmt import user_mgmt_bp
from routes.board import board_bp

app = Flask(__name__)

# 보안을 위한 시크릿 키 설정 (세션 및 로그인 기능 구현 시 필수)
app.config['SECRET_KEY'] = 'saedam_secure_key_2026'

# 2. 블루프린트 등록 (URL 경로와 연결)
# url_prefix를 설정하면 브라우저 주소창에 /approval, /contract 등으로 구분됩니다.
app.register_blueprint(main_bp)                          # 메인은 보통 / (루트)
app.register_blueprint(approval_bp, url_prefix='/approval')
app.register_blueprint(document_bp, url_prefix='/document')
app.register_blueprint(contract_bp, url_prefix='/contract')
app.register_blueprint(user_mgmt_bp, url_prefix='/user')
app.register_blueprint(board_bp, url_prefix='/board')

# 공통 에러 페이지 (404 Not Found 등)
@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404

if __name__ == '__main__':
    # 디버그 모드로 실행 (수정 시 자동 재시작)
    app.run(host='0.0.0.0', port=5000, debug=True)