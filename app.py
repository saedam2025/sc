from flask import Flask, session, redirect, url_for, request, render_template, jsonify
from datetime import datetime
import os
import sys
import traceback
from extensions import socketio

# 배포 환경에서 모듈 임포트 에러 방지를 위해 현재 디렉토리를 시스템 경로에 추가
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# 블루프린트 임포트
from routes.main import main_bp
from routes.document import document_bp
from routes.contract import contract_bp
from routes.user_mgmt import user_mgmt_bp
from routes.approval import approval_bp
from routes.expense import expense_bp
from routes.board import board_bp
from routes.payroll import payroll_bp
from routes.ai_mail import ai_mail_bp
from routes.memo import memo_bp
from routes.attendance import attendance_bp
from routes.excel_generator import excel_bp
from routes.explorer import explorer_bp
from routes.notifications import noti_bp
from routes.gallery import gallery_bp
from routes.school_bp import school_bp
from routes.school_task import school_task_bp
from routes.contacts import contacts_bp
from routes.admin_management import admin_bp, get_active_theme

# [수정] gall2.py가 routes 폴더 안에 있다면 아래와 같이 수정해야 합니다.
from routes.gall2 import gall2_bp

# 🚀 새로 분리한 사내 메신저 블루프린트 임포트
from routes.chat import chat_bp

# 데이터베이스 모듈 임포트
from routes.database import get_db, init_db

app = Flask(__name__)
socketio.init_app(app)

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

# 💡 [새담 게시판 연동 추가] 첨부파일 최대 용량을 1.5GB로 설정 
# (이 설정이 없으면 Flask 기본 제한에 걸려 대용량 파일 업로드 시 에러가 발생합니다)
app.config['MAX_CONTENT_LENGTH'] = 1.5 * 1024 * 1024 * 1024

# 로그인 체크 제외 대상 (정적 파일 및 외부 서비스 경로)
EXEMPT_ROUTES = [
    'login_page', 
    'login', 
    'logout', 
    'user_mgmt.register', 
    'user_mgmt.invite_page', 
    'static',
    'contract.login', 
    'contract.contract_list', 
    'contract.contract', 
    'contract.save_contract', 
    'document.apply',
    'document.apply2',
    'expense.submit_expense',
    'expense.expense_template',
]

# 💡 레벨 8(센터장) 전용 허용 경로 목록 정의
# 개인 프로필 관련 API 경로들을 모두 허용하도록 추가했습니다.
LEVEL_8_ALLOWED_PATHS = [
    '/school',
    '/school/calendar',
    '/school/tasks',
    '/contacts',
    '/logout',
    '/user/my_info',
    '/user/update_my_info', 
    '/user/profile',         # 💡 공통 메인메뉴 프로필 조회 허용
    '/user/edit',            # 💡 공통 메인메뉴 프로필 수정 허용
    '/user/password',        # 💡 공통 메인메뉴 비밀번호 변경 허용
    '/user/upload',          # 💡 공통 메인메뉴 프로필 사진 업로드 허용
    '/user/api',             # 💡 기타 유저 관련 API 허용
    '/chat_popup',
    '/chat/attachment',
    '/send_message',
    '/get_chat_history',
    '/delete_message',
    '/api/chat',
    '/api/unread_messages',
    '/api/message_',
    '/api/messages',
    '/api/leave_chat',
    '/api/toggle_pin',
    '/api/move_pin',
    '/socket.io',
    '/gall2',
    '/api/activity_feed',
]

@app.before_request
def check_login():
    # 1. 예외 경로이거나 정적 파일 요청이면 통과
    if request.endpoint in EXEMPT_ROUTES or (request.path and request.path.startswith('/static')):
        return None
    
    # 2. 세션에 사번(emp_no)이 없으면 로그인 페이지로 이동
    if 'emp_no' not in session:
        return redirect(url_for('login_page'))
    
    # 3. 레벨 8 (센터장) 권한 체크 로직
    user_level = session.get('user_level', 99)
    if user_level == 8:
        is_allowed = any(request.path.startswith(allowed) for allowed in LEVEL_8_ALLOWED_PATHS)
        if request.path == '/':
            return redirect(url_for('school.school_list'))
        if not is_allowed:
            return "접근 권한이 없습니다. (센터장 전용 메뉴만 이용 가능)", 403

    _record_usage_log()


def _classify_menu(path):
    menu_map = [
        ('/admin', '통합관리'),
        ('/user', '인사관리'),
        ('/board', '게시판'),
        ('/chat', '사내메신저'),
        ('/chat_popup', '사내메신저'),
        ('/school', '학교업무메뉴'),
        ('/document', '증명발급'),
        ('/contract', '계약시스템'),
        ('/gall2', '갤러리'),
        ('/gallery', '갤러리'),
        ('/approval', '사내결재'),
        ('/expense', '지출결의'),
        ('/ai-mail', 'AI메일전송'),
        ('/payroll', '급여/업무지원'),
        ('/attendance', '근태관리'),
        ('/contacts', '본사연락망'),
        ('/memo', '개인화이트보드'),
        ('/excel-generator', '입금용 엑셀 생성기'),
        ('/notifications', '알림'),
    ]
    if path == '/':
        return '메인메뉴'
    for prefix, label in menu_map:
        if path.startswith(prefix):
            return label
    return '기타'


def _record_usage_log():
    try:
        path = request.path or ''
        if request.method not in ('GET', 'POST') or path.startswith('/static'):
            return
        if path.startswith(('/check_messages', '/api/activity_feed', '/user/profile_img')):
            return
        conn = get_db()
        conn.execute('''
            INSERT INTO usage_logs (emp_no, user_name, menu_name, endpoint, path, method, ip_address)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            session.get('emp_no'),
            session.get('user_name'),
            _classify_menu(path),
            request.endpoint,
            path,
            request.method,
            request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"이용 로그 기록 오류: {e}")

# =====================================================================
# [전역 변수 설정]
# =====================================================================
@app.context_processor
def inject_user_data():
    return {
        'current_user': session.get('user_name'),
        'current_user_profile_path': session.get('profile_path'),
        'current_user_level': session.get('user_level', 99),
        'global_theme': get_active_theme()
    }

# =====================================================================
# 💡 [템플릿 필터 추가] 게시판 새 글(New) 표시를 위한 날짜 계산 필터
# =====================================================================
@app.template_filter('as_datetime')
def as_datetime_filter(value, format="%Y-%m-%d %H:%M:%S"):
    try:
        if not value:
            return None
        # SQLite에서 가져온 날짜에 밀리초나 불필요한 문자가 붙어있을 경우를 대비해
        # 앞의 19자리(YYYY-MM-DD HH:MM:SS)만 잘라서 안전하게 파싱합니다.
        return datetime.strptime(str(value)[:19], format)
    except:
        return None

# --- 로그인/로그아웃 로직 ---

@app.route('/login_page')
def login_page():
    hidden_theme_keys = []
    try:
        conn = get_db()
        admin = conn.execute("SELECT id FROM users WHERE emp_no = 'admin'").fetchone()
        hidden_rows = conn.execute('''
            SELECT DISTINCT theme_key
            FROM theme_catalog_preferences
            WHERE is_hidden=1
        ''').fetchall()
        hidden_theme_keys = [row['theme_key'] for row in hidden_rows]
        conn.close()
        
        if not admin:
            return render_template('user_list.html', mode='admin_setup')
    except Exception as e:
        print(f"로그인 페이지 관리자 체크 오류: {e}")

    return render_template('login.html', hidden_theme_keys=hidden_theme_keys)

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    
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

    emp_no = str(data.get('emp_no', '')).strip()
    password = str(data.get('password', '')).strip()
    
    conn = get_db()
    user_row = conn.execute("SELECT * FROM users WHERE emp_no=? AND password=?", (str(emp_no), str(password))).fetchone()
    
    if not user_row:
        conn.close()
        return jsonify({"status": "error", "message": "사번 또는 비밀번호가 틀립니다."}), 401

    user = dict(user_row)
    
    if int(user.get('level', 99)) == 9:
        conn.close()
        return jsonify({"status": "error", "message": "현재 승인 대기 중입니다. 본사로 문의해 주세요."}), 403

    if user.get('status') != '승인':
        conn.close()
        return jsonify({"status": "error", "message": "승인이 대기 중인 계정입니다."}), 403
    
    session['emp_no'] = str(user.get('emp_no', ''))
    session['user_name'] = user.get('name', '알수없음')
    session['user_level'] = int(user.get('level', 14))
    
    session['position'] = str(user.get('position', '미지정'))
    session['department'] = str(user.get('department', '소속미지정'))
    session['role'] = str(user.get('position', '미지정'))
    
    session['profile_path'] = user.get('profile_path', '')
    session['profile_icon'] = user.get('profile_icon') or user.get('아이콘') or '👤'

    try:
        conn.execute('''
            INSERT INTO login_activity (emp_no, user_name, action, ip_address, user_agent)
            VALUES (?, ?, 'login', ?, ?)
        ''', (
            session.get('emp_no'),
            session.get('user_name'),
            request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip(),
            request.headers.get('User-Agent', '')[:255]
        ))
        conn.commit()
    except Exception as e:
        print(f"로그인 기록 오류: {e}")

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

@app.route('/user/update_my_info', methods=['POST'])
def update_my_info():
    if 'emp_no' not in session:
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401

    data = request.get_json(silent=True) or {}

    new_password = data.get('password')
    new_email = data.get('email', '')
    new_phone = data.get('phone', '')
    new_address = data.get('address', '')
    new_profile_icon = data.get('profile_icon', '👤')

    conn = get_db()
    try:
        # 실제 users 테이블에 컬럼이 있는지 확인
        columns = [row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()]

        update_fields = []
        params = []

        if 'email' in columns:
            update_fields.append("email=?")
            params.append(new_email)

        if 'phone' in columns:
            update_fields.append("phone=?")
            params.append(new_phone)

        if 'address' in columns:
            update_fields.append("address=?")
            params.append(new_address)

        if 'profile_icon' in columns:
            update_fields.append("profile_icon=?")
            params.append(new_profile_icon)
            session['profile_icon'] = new_profile_icon

        if new_password and 'password' in columns:
            update_fields.append("password=?")
            params.append(new_password)

        if not update_fields:
            return jsonify({"status": "error", "message": "수정 가능한 항목이 없습니다."}), 400

        params.append(session['emp_no'])

        conn.execute(
            f"UPDATE users SET {', '.join(update_fields)} WHERE emp_no=?",
            params
        )
        conn.commit()

        return jsonify({"status": "success", "message": "정보가 성공적으로 수정되었습니다."})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

    finally:
        conn.close()

@app.route('/api/activity_feed')
def activity_feed():
    if 'emp_no' not in session:
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401

    conn = get_db()
    activities = []

    def table_exists(table_name):
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        ).fetchone()
        return row is not None

    def get_columns(table_name):
        if not table_exists(table_name):
            return []
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()]

    def add_activity(kind, icon, color_type, actor, text, created_at, url=None):
        if not created_at:
            created_at = ''
        activities.append({
            "kind": kind,
            "icon": icon,
            "color_type": color_type,
            "actor": actor or "시스템",
            "text": text or "",
            "created_at": created_at,
            "url": url or ""
        })

    try:
        # 1) 사내 게시판
        if table_exists('board_posts'):
            cols = get_columns('board_posts')
            if all(c in cols for c in ['title', 'author', 'created_at']):
                rows = conn.execute("""
                    SELECT id, title, author, created_at
                    FROM board_posts
                    ORDER BY created_at DESC
                    LIMIT 10
                """).fetchall()

                for r in rows:
                    add_activity(
                        kind="board",
                        icon="fa-bullhorn",
                        color_type="yellow",
                        actor=r['author'],
                        text=f"사내게시판에 새 글 「{r['title']}」을 등록했습니다.",
                        created_at=r['created_at'],
                        url="/"
                    )

        # 2) 받은 쪽지
        if table_exists('messages'):
            cols = get_columns('messages')
            if all(c in cols for c in ['sender', 'receiver', 'sent_at']):
                rows = conn.execute("""
                    SELECT sender, receiver, sent_at
                    FROM messages
                    WHERE receiver = ?
                    ORDER BY sent_at DESC
                    LIMIT 10
                """, (session.get('user_name'),)).fetchall()

                for r in rows:
                    add_activity(
                        kind="message",
                        icon="fa-envelope",
                        color_type="blue",
                        actor=r['sender'],
                        text="새 쪽지를 보냈습니다.",
                        created_at=r['sent_at'],
                        url=""
                    )

        # 3) 학교업무공간 게시글
        if table_exists('school_posts'):
            cols = get_columns('school_posts')
            if all(c in cols for c in ['title', 'author', 'created_at']):
                rows = conn.execute("""
                    SELECT id, school_id, title, author, created_at
                    FROM school_posts
                    ORDER BY created_at DESC
                    LIMIT 10
                """).fetchall()

                for r in rows:
                    add_activity(
                        kind="school_post",
                        icon="fa-school",
                        color_type="green",
                        actor=r['author'],
                        text=f"학교업무공간에 새 글 「{r['title']}」을 등록했습니다.",
                        created_at=r['created_at'],
                        url=f"/school/{r['school_id']}" if 'school_id' in r.keys() else "/school"
                    )

        # 4) 학교업무공간 댓글
        if table_exists('school_post_comments'):
            cols = get_columns('school_post_comments')
            if all(c in cols for c in ['author', 'content', 'created_at']):
                rows = conn.execute("""
                    SELECT id, post_id, author, content, created_at
                    FROM school_post_comments
                    ORDER BY created_at DESC
                    LIMIT 10
                """).fetchall()

                for r in rows:
                    content = (r['content'] or '').replace('\n', ' ')
                    if len(content) > 25:
                        content = content[:25] + '...'

                    add_activity(
                        kind="school_comment",
                        icon="fa-comment-dots",
                        color_type="purple",
                        actor=r['author'],
                        text=f"학교업무공간에 댓글을 남겼습니다. 「{content}」",
                        created_at=r['created_at'],
                        url=""
                    )

        # 5) 학교 전용 일정
        if table_exists('school_tasks'):
            cols = get_columns('school_tasks')
            if all(c in cols for c in ['title', 'owner', 'created_at']):
                rows = conn.execute("""
                    SELECT id, school_id, title, owner, created_at
                    FROM school_tasks
                    ORDER BY created_at DESC
                    LIMIT 10
                """).fetchall()

                for r in rows:
                    add_activity(
                        kind="school_task",
                        icon="fa-calendar-check",
                        color_type="green",
                        actor=r['owner'] or "시스템",
                        text=f"학교 일정 「{r['title']}」을 등록했습니다.",
                        created_at=r['created_at'],
                        url=f"/school/{r['school_id']}" if 'school_id' in r.keys() else "/school"
                    )

        # 최신순 정렬 후 20개만 반환
        activities.sort(key=lambda x: x.get('created_at') or '', reverse=True)

        conn.close()
        return jsonify({
            "status": "success",
            "activities": activities[:20]
        })

    except Exception as e:
        conn.close()
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

@app.route('/chat_popup/<partner>')
def chat_popup(partner):
    current_user = session.get('user_name', '알수없음')
    return render_template('chat_popup.html', partner=partner, current_user=current_user)

@app.route('/logout')
def logout():
    try:
        if session.get('emp_no'):
            conn = get_db()
            conn.execute('''
                INSERT INTO login_activity (emp_no, user_name, action, ip_address, user_agent)
                VALUES (?, ?, 'logout', ?, ?)
            ''', (
                session.get('emp_no'),
                session.get('user_name'),
                request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip(),
                request.headers.get('User-Agent', '')[:255]
            ))
            conn.commit()
            conn.close()
    except Exception as e:
        print(f"로그아웃 기록 오류: {e}")
    session.clear()
    return redirect(url_for('login_page'))

# =====================================================================
# [Blueprint 등록]
# =====================================================================
app.register_blueprint(chat_bp)
app.register_blueprint(main_bp)
app.register_blueprint(document_bp, url_prefix='/document')
app.register_blueprint(contract_bp, url_prefix='/contract')
app.register_blueprint(user_mgmt_bp, url_prefix='/user')
app.register_blueprint(approval_bp, url_prefix='/approval')
app.register_blueprint(expense_bp, url_prefix='/expense')
app.register_blueprint(board_bp, url_prefix='/board')
app.register_blueprint(payroll_bp, url_prefix='/payroll')
app.register_blueprint(ai_mail_bp, url_prefix='/ai-mail')
app.register_blueprint(memo_bp, url_prefix='/memo')  
app.register_blueprint(attendance_bp)  
app.register_blueprint(excel_bp)       
app.register_blueprint(explorer_bp, url_prefix='/explorer')
app.register_blueprint(noti_bp)  
app.register_blueprint(gallery_bp) 
app.register_blueprint(school_bp, url_prefix='/school')
app.register_blueprint(school_task_bp, url_prefix='/school/tasks')
app.register_blueprint(contacts_bp)
app.register_blueprint(gall2_bp)
app.register_blueprint(admin_bp, url_prefix='/admin')

# 🚀 새로 분리한 메신저 블루프린트 등록 추가

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
    socketio.run(app, host='0.0.0.0', port=port, debug=True, allow_unsafe_werkzeug=True)
