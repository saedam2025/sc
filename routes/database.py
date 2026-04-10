import sqlite3
import os

# 1. 경로 설정
DB_FILE = '/mnt/data/saedam.db' # 렌더 영구저장소 DB 경로
GALLERY_ROOT = '/mnt/data/gallery'
GALLERY_UPLOADS = os.path.join(GALLERY_ROOT, 'uploads')
GALLERY_THUMBS = os.path.join(GALLERY_ROOT, 'thumbnails')

def get_db():
    """데이터베이스 연결 객체 생성"""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """테이블 초기화 및 필수 폴더 생성"""
    
    # [추가] 갤러리 관련 필수 폴더 생성
    os.makedirs(GALLERY_UPLOADS, exist_ok=True)
    os.makedirs(GALLERY_THUMBS, exist_ok=True)
    
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
        is_read INTEGER DEFAULT 0
    )''')

    # 5. 회원 관리(Users) 테이블
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_no TEXT, name TEXT, password TEXT, position TEXT, level INTEGER,
        rrn TEXT, email TEXT, phone TEXT,
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

    # 7. [수정 확인] 개인 갤러리 테이블
    c.execute('''CREATE TABLE IF NOT EXISTS gallery (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        filename TEXT NOT NULL,
        thumb_name TEXT NOT NULL,
        file_type TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')

    # ---------------------------------------------------------
    # [DB 자동 업데이트] 기존 테이블들에 신규 컬럼 자동 추가
    # ---------------------------------------------------------
    
    try:
        c.execute("ALTER TABLE messages ADD COLUMN filename TEXT")
        c.execute("ALTER TABLE messages ADD COLUMN filepath TEXT")
    except sqlite3.OperationalError: pass 

    try:
        c.execute("ALTER TABLE users ADD COLUMN profile_icon TEXT DEFAULT '👤'")
    except sqlite3.OperationalError: pass

    try:
        c.execute("ALTER TABLE daily_attendance ADD COLUMN reason TEXT")
    except sqlite3.OperationalError: pass

    try:
        c.execute("ALTER TABLE daily_attendance ADD COLUMN position TEXT")
    except sqlite3.OperationalError: pass
    
    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()