import sqlite3
import os

DB_FILE = '/mnt/data/saedam.db'

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row # 결과를 딕셔너리처럼 접근 가능하게 함
    return conn

def init_db():
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
    
    # 2. 근태/휴가 테이블 (기존 attendance.xlsx 대체)
    c.execute('''CREATE TABLE IF NOT EXISTS attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner TEXT, type TEXT, start_date TEXT, end_date TEXT, status TEXT
    )''')
    
    # 3. 사내 게시판 테이블
    c.execute('''CREATE TABLE IF NOT EXISTS board (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT, content TEXT, author TEXT, 
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        filename TEXT, filepath TEXT
    )''')
    
    # 4. 메시지 테이블
    c.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender TEXT, receiver TEXT, content TEXT, 
        sent_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        is_read INTEGER DEFAULT 0
    )''')
    
    conn.commit()
    conn.close()

# 서버 시작 시 DB가 없으면 자동 생성
init_db()