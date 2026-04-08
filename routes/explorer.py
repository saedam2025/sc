from flask import Blueprint, render_template, session, abort, send_file
import os
import shutil
from datetime import datetime

# 'explorer'라는 이름의 독립된 Blueprint 생성
explorer_bp = Blueprint('explorer', __name__)

@explorer_bp.route('/', defaults={'req_path': ''}, strict_slashes=False)
@explorer_bp.route('/<path:req_path>')
def file_explorer(req_path):
    current_user = session.get('user_name')
    if not current_user:
        return "로그인이 필요합니다.", 401
    
    # [선택 보안] 특정 계정만 이 숨겨진 페이지를 보게 하려면 아래 주석을 풀고 설정하세요.
    # if current_user not in ['관리자계정명', 'admin']:
    #     return "시스템 관리자만 접근할 수 있는 페이지입니다.", 403

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

    # ==========================================
    # [신규 기능] 대시보드 통계 데이터 계산
    # ==========================================
    
    # 1. 서버 디스크 전체 용량 확인 (Render 마운트 디스크 기준)
    total, used, free = shutil.disk_usage(BASE_DIR)
    
    # 2. BASE_DIR 내부의 실제 파일 갯수 및 앱 사용 용량 정밀 계산
    total_files = 0
    app_used_bytes = 0
    for root, dirs, f_names in os.walk(BASE_DIR):
        total_files += len(f_names)
        for f in f_names:
            fp = os.path.join(root, f)
            # 심볼릭 링크 등 가짜 파일 제외 후 실제 크기 합산
            if os.path.exists(fp) and not os.path.islink(fp):
                app_used_bytes += os.path.getsize(fp)

    # 용량 단위 자동 변환 함수
    def format_size(bytes_size):
        if bytes_size >= 1024**3: return f"{bytes_size / (1024**3):.2f} GB"
        elif bytes_size >= 1024**2: return f"{bytes_size / (1024**2):.2f} MB"
        else: return f"{bytes_size / 1024:.2f} KB"

    # 프론트로 넘겨줄 통계 딕셔너리
    stats = {
        'total_disk_gb': round(total / (1024**3), 2),
        'used_disk_gb': round(used / (1024**3), 2),
        'free_disk_gb': round(free / (1024**3), 2),
        'usage_percent': round((used / total) * 100, 1) if total > 0 else 0,
        'app_used_str': format_size(app_used_bytes),
        'total_files': f"{total_files:,}" # 천 단위 콤마 추가
    }

    # ==========================================
    # 기존 파일 목록 조회 로직
    # ==========================================
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