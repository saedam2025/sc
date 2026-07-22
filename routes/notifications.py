from flask import Blueprint, render_template, session
import os
import sqlite3
import pandas as pd  # 🚀 증명서 건수 조회를 위해 추가
from .database import get_db

noti_bp = Blueprint('noti', __name__)

@noti_bp.route('/widget/notifications')
def widget_notifications():
    current_user = session.get('user_name', '배호영') 
    conn = get_db()
    
    # 기본값 설정
    approval_pending_count = 0
    approval_draft_count = 0
    expense_wait_count = 0
    school_task_wait_count = 0
    cert_wait_count = 0      # 증명서 대기
    contract_miss_count = 0  # 전자계약 미계약
    
    # 1. 결재 및 쪽지, 학교업무 (SQLite DB 조회)
    try:
        # 수신대기
        pending = conn.execute("SELECT COUNT(*) FROM approvals WHERE (approver_1 = ? AND status = '대기') OR (approver_2 = ? AND status = '1차승인')", (current_user, current_user)).fetchone()
        approval_pending_count = pending[0] if pending else 0

        # 기안함 전체
        draft = conn.execute("SELECT COUNT(*) FROM approvals WHERE drafter = ?", (current_user,)).fetchone()
        approval_draft_count = draft[0] if draft else 0
        
        # 학교업무 접수
        task = conn.execute("SELECT COUNT(*) FROM school_posts WHERE status = '접수' OR status IS NULL OR status = ''").fetchone()
        school_task_wait_count = task[0] if task else 0
        
        # 지출결의 대기
        expense_wait = conn.execute("""
            SELECT COUNT(*)
            FROM expense_reports
            WHERE COALESCE(doc_status, '대기') NOT IN ('완료', '반려')
        """).fetchone()
        expense_wait_count = expense_wait[0] if expense_wait else 0
    except Exception as e:
        print("메인 DB 조회 오류:", e)
    finally:
        conn.close()

    # 2. 🚀 증명서 발급 대기 건수 (엑셀 파일 조회)
    try:
        # document.py에 정의된 경로 방식과 동일하게 설정
        BASE_DIR = "/mnt/data" if os.path.exists("/mnt/data") else os.getcwd()
        DATA_PATH = os.path.join(BASE_DIR, "certificates.xlsx")
        
        if os.path.exists(DATA_PATH):
            df = pd.read_excel(DATA_PATH, dtype=str)
            # '상태' 컬럼이 '대기'인 행의 개수
            cert_wait_count = len(df[df['상태'] == '대기'])
    except Exception as e:
        print("증명서 엑셀 조회 오류:", e)

    # 3. 전자계약 미계약 건수 (contracts.db)
    try:
        if os.path.exists('/mnt/data'): MOUNT_PATH = '/mnt/data'
        else: MOUNT_PATH = os.getcwd()
        CONTRACT_DB_FILE = os.path.join(MOUNT_PATH, 'contracts.db')
        
        if os.path.exists(CONTRACT_DB_FILE):
            c_conn = sqlite3.connect(CONTRACT_DB_FILE)
            c_row = c_conn.execute("SELECT COUNT(*) FROM contracts WHERE 계약완료일시 = '' OR 계약완료일시 IS NULL").fetchone()
            contract_miss_count = c_row[0] if c_row else 0
            c_conn.close()
    except Exception as e:
        print("전자계약 DB 조회 오류:", e)

    notification_data = {
        'approval_vacation': approval_pending_count,
        'approval_draft': approval_draft_count,       
        'board_new': school_task_wait_count,          
        'cert_wait': cert_wait_count,                 # 🚀 이제 정확하게 연동됩니다
        'expense_wait': expense_wait_count,
        'contract_miss': contract_miss_count
    }

    return render_template('notifications.html', data=notification_data)
