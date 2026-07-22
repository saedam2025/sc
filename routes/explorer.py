from flask import Blueprint, render_template, session, abort, send_file, request
import os
import shutil
from datetime import datetime
from werkzeug.security import generate_password_hash

# [중요] 실제 프로젝트의 DB 설정에 맞게 수정하세요.
# 예: from app import db; from models import User
try:
    from models import db, User 
except ImportError:
    # 파일이 없거나 경로가 다를 경우를 대비한 가이드 (실제 환경에 맞춰 수정 필요)
    db = None
    User = None

# 'explorer'라는 이름의 독립된 Blueprint 생성
explorer_bp = Blueprint('explorer', __name__)

# ==========================================
# [신규] Admin 비밀번호 변경 처리 라우트
# ==========================================
@explorer_bp.route('/change_admin_password', methods=['POST'])
def change_admin_password():
    current_user = session.get('user_name')
    if not current_user:
        return "로그인이 필요합니다.", 401
    
    # [보안] 관리자 권한 확인 (필요 시 주석 해제)
    # if current_user not in ['admin', '관리자']:
    #     return "권한이 없습니다.", 403

    new_password = request.form.get('new_password')
    if not new_password:
        return "<script>alert('새 비밀번호를 입력해주세요.'); history.back();</script>"
    
    if db is None or User is None:
        return "<script>alert('DB 모델(User, db)을 불러오지 못했습니다. explorer.py 상단의 import문을 확인하세요.'); history.back();</script>"

    try:
        # DB에서 admin 계정 찾기
        admin_user = User.query.filter_by(username='admin').first()
        
        if admin_user:
            # 신규 비밀번호 해싱 후 저장
            admin_user.password = generate_password_hash(new_password)
            db.session.commit()
            return f"""<script>alert('admin 계정의 비밀번호가 성공적으로 변경되었습니다.'); location.href='/explorer';</script>"""
        else:
            return f"""<script>alert('admin 계정을 데이터베이스에서 찾을 수 없습니다.'); location.href='/explorer';</script>"""
            
    except Exception as e:
        db.session.rollback()
        return f"""<script>alert('오류 발생: {str(e)}'); location.href='/explorer';</script>"""


@explorer_bp.route('/', defaults={'req_path': ''}, strict_slashes=False)
@explorer_bp.route('/<path:req_path>')
def file_explorer(req_path):
    current_user = session.get('user_name')
    if not current_user:
        return "로그인이 필요합니다.", 401
    
    # 탐색할 최상위 기본 경로 (Render 디스크 마운트 경로)
    BASE_DIR = '/mnt/data'
    abs_path = os.path.abspath(os.path.join(BASE_DIR, req_path))

    # [핵심 보안] 상위 폴더(../)로 넘어가려는 해킹 시도 차단
    if not abs_path.startswith(os.path.abspath(BASE_DIR)):
        abort(403)
    if not os.path.exists(abs_path):
        abort(404)
    if os.path.isfile(abs_path):
        return send_file(abs_path)

    # 대시보드 통계 데이터 계산
    total, used, free = shutil.disk_usage(BASE_DIR)
    
    total_files = 0
    app_used_bytes = 0
    for root, dirs, f_names in os.walk(BASE_DIR):
        total_files += len(f_names)
        for f in f_names:
            fp = os.path.join(root, f)
            if os.path.exists(fp) and not os.path.islink(fp):
                app_used_bytes += os.path.getsize(fp)

    def format_size(bytes_size):
        if bytes_size >= 1024**3: return f"{bytes_size / (1024**3):.2f} GB"
        elif bytes_size >= 1024**2: return f"{bytes_size / (1024**2):.2f} MB"
        else: return f"{bytes_size / 1024:.2f} KB"

    stats = {
        'total_disk_gb': round(total / (1024**3), 2),
        'used_disk_gb': round(used / (1024**3), 2),
        'free_disk_gb': round(free / (1024**3), 2),
        'usage_percent': round((used / total) * 100, 1) if total > 0 else 0,
        'app_used_str': format_size(app_used_bytes),
        'total_files': f"{total_files:,}"
    }

    files = []
    for filename in os.listdir(abs_path):
        filepath = os.path.join(abs_path, filename)
        stat = os.stat(filepath)
        is_dir = os.path.isdir(filepath)
        size = stat.st_size if not is_dir else "-"
        
        if size != "-": size = format_size(size)
        mtime = datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M')

        files.append({
            'name': filename, 'is_dir': is_dir, 'size': size,
            'mtime': mtime, 'rel_path': os.path.join(req_path, filename).replace('\\', '/')
        })
    
    files.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
    parent_path = os.path.dirname(req_path) if req_path else None

    return render_template('explorer.html', files=files, current_path=req_path, parent_path=parent_path, stats=stats)