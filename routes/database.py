import sqlite3
import os
import platform

# =====================================================================
# [경로 설정] 환경에 따라 자동으로 경로를 전환합니다.
# =====================================================================
if platform.system() == 'Windows':
    # 윈도우 환경: 현재 폴더 기준
    BASE_DIR = os.getcwd() 
else:
    # 렌더 서버 환경: 마운트된 영구 저장소 경로 사용
    BASE_DIR = '/mnt/data' if os.path.exists('/mnt/data') else os.getcwd()

# 실제 파일 위치들
DB_FILE = os.path.join(BASE_DIR, 'saedam.db')
GALLERY_ROOT = os.path.join(BASE_DIR, 'gallery')
GALLERY_UPLOADS = os.path.join(GALLERY_ROOT, 'uploads')
GALLERY_THUMBS = os.path.join(GALLERY_ROOT, 'thumbnails')
PROFILE_ROOT = os.path.join(BASE_DIR, 'id')

# [신규 추가] 학교 업무 공간 첨부파일 경로
SCHOOL_UPLOADS = os.path.join(BASE_DIR, 'school_uploads')

# =====================================================================

def get_db():
    """데이터베이스 연결 객체 생성"""
    if platform.system() == 'Windows':
        print(f"DEBUG: 현재 연결된 DB 파일 위치 -> {DB_FILE}")
        
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """테이블 초기화 및 필수 폴더 생성"""
    
    # 필수 폴더 생성
    os.makedirs(GALLERY_UPLOADS, exist_ok=True)
    os.makedirs(GALLERY_THUMBS, exist_ok=True)
    os.makedirs(PROFILE_ROOT, exist_ok=True)
    os.makedirs(SCHOOL_UPLOADS, exist_ok=True) # 학교 게시판 파일 폴더
    
    conn = get_db()
    c = conn.cursor()
    
    # 1. 일정(Tasks) 테이블
    c.execute('''CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        year TEXT, date TEXT, owner TEXT,
        cat_meeting_title TEXT, cat_meeting_time TEXT,
        cat_interview_title TEXT, cat_interview_time TEXT,
        cat_miting_title TEXT, cat_miting_time TEXT,
        cat_out_title TEXT, cat_out_time TEXT,
        cat_etc_title TEXT, cat_etc_time TEXT,
        note TEXT
    )''')
    
    # 2. 근태(Attendance) 테이블
    c.execute('''CREATE TABLE IF NOT EXISTS attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner TEXT, type TEXT, start_date TEXT, end_date TEXT, status TEXT
    )''')

    # 2-1. 일일 출퇴근 테이블
    c.execute('''CREATE TABLE IF NOT EXISTS daily_attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_no TEXT NOT NULL,
        date TEXT NOT NULL,
        clock_in_time TEXT NOT NULL,
        clock_out_time TEXT,
        status TEXT NOT NULL,
        reason TEXT,
        position TEXT
    )''')
    
    # 3. 사내 게시판 테이블
    c.execute('''CREATE TABLE IF NOT EXISTS board (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT, content TEXT, author TEXT, 
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        filename TEXT, filepath TEXT
    )''')
    
    # 4. 메시지(쪽지) 테이블
    c.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender TEXT, receiver TEXT, content TEXT, 
        sent_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        is_read INTEGER DEFAULT 0,
        filename TEXT, filepath TEXT
    )''')

    # 5. 회원 관리(Users) 테이블
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_no TEXT, name TEXT, password TEXT, position TEXT, level INTEGER,
        rrn TEXT, email TEXT, phone TEXT,
        address TEXT, bank_account TEXT, department TEXT, profile_path TEXT,
        profile_icon TEXT DEFAULT '👤',
        join_date TEXT, retire_date TEXT, status TEXT DEFAULT '대기'
    )''')

    # 6. 전자결재(Approvals) 테이블
    c.execute('''CREATE TABLE IF NOT EXISTS approvals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        doc_type TEXT, title TEXT, drafter TEXT,
        approver_1 TEXT, approver_2 TEXT, status TEXT DEFAULT '대기',
        doc_data TEXT, filename TEXT, filepath TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')

    # 7. 개인 갤러리 테이블
    c.execute('''CREATE TABLE IF NOT EXISTS gallery (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        filename TEXT NOT NULL,
        thumb_name TEXT NOT NULL,
        file_type TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        tab_id INTEGER DEFAULT 1
    )''')

    # 8. 갤러리 탭 테이블
    c.execute('''CREATE TABLE IF NOT EXISTS gallery_tabs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL
    )''')
    
    # ---------------------------------------------------------
    # [신규 추가] 9. 학교 정보 등록 테이블 (학교업무공간)
    # ---------------------------------------------------------
    c.execute('''CREATE TABLE IF NOT EXISTS schools (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        year TEXT NOT NULL,                -- 연도
        school_name TEXT NOT NULL,         -- 학교명
        office_phone TEXT,                 -- 지원실 전화
        office_location TEXT,              -- 지원실 위치
        neulbom_assistant TEXT,            -- 늘봄실무사
        neulbom_manager TEXT,              -- 늘봄실장
        center_director_id TEXT,           -- 센터장 (users 테이블의 emp_no와 연동)
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')

    # ---------------------------------------------------------
    # [신규 추가] 10. 학교별 게시판 테이블 (업무보고, 게시판, 자료실, 맞춤형 등)
    # ---------------------------------------------------------
    c.execute('''CREATE TABLE IF NOT EXISTS school_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        school_id INTEGER NOT NULL,        -- 어느 학교의 게시물인지 (schools.id)
        category TEXT NOT NULL,            -- 구분 (report: 업무보고, board: 업무게시판, archive: 업무자료실, custom: 맞춤형)
        title TEXT NOT NULL,
        content TEXT,
        author TEXT,
        filename TEXT,                     -- 첨부파일명
        filepath TEXT,                     -- 첨부파일경로
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (school_id) REFERENCES schools (id) ON DELETE CASCADE
    )''')

    # ---------------------------------------------------------
    # [DB 자동 업데이트] 기존 테이블들에 신규 컬럼 자동 추가 (기존 코드 유지)
    # ---------------------------------------------------------
    # (이미 존재하는 경우를 대비한 예외 처리 포함)
    alter_queries = [
        "ALTER TABLE messages ADD COLUMN filename TEXT",
        "ALTER TABLE messages ADD COLUMN filepath TEXT",
        "ALTER TABLE daily_attendance ADD COLUMN reason TEXT",
        "ALTER TABLE daily_attendance ADD COLUMN position TEXT",
        "ALTER TABLE gallery ADD COLUMN tab_id INTEGER DEFAULT 1",
        "ALTER TABLE users ADD COLUMN profile_icon TEXT DEFAULT '👤'",
        "ALTER TABLE users ADD COLUMN address TEXT",
        "ALTER TABLE users ADD COLUMN bank_account TEXT",
        "ALTER TABLE users ADD COLUMN department TEXT",
        "ALTER TABLE users ADD COLUMN profile_path TEXT"
    ]
    
    for q in alter_queries:
        try:
            c.execute(q)
        except sqlite3.OperationalError:
            pass # 이미 컬럼이 존재하면 무시

    # 기본 탭 생성
    tabs_count = c.execute("SELECT count(*) FROM gallery_tabs").fetchone()[0]
    if tabs_count == 0:
        c.execute("INSERT INTO gallery_tabs (id, name) VALUES (1, '기본 갤러리')")

    conn.commit()
    conn.close()
    print("DATABASE INITIALIZED SUCCESSFULLY")

if __name__ == "__main__":
    init_db()