from flask import Blueprint, render_template, session
import sqlite3
import os
from datetime import datetime, timedelta

# database.py에서 설정한 DB 경로 사용
DB_FILE = '/mnt/data/saedam.db'

noti_bp = Blueprint('noti_bp', __name__, url_prefix='/widget')

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

@noti_bp.route('/notifications')
def get_notification_widget():
    # 1. 로그인 세션 확인
    user_name = session.get('user_name')
    user_id = session.get('user_id') # emp_no 등
    
    if not user_name:
        return ""

    conn = get_db_connection()
    c = conn.cursor()
    
    noti_data = {}

    try:
        # 2. 데이터 쿼리 수행
        
        # [결재] doc_type별 대기 현황 (결재자가 본인인 경우)
        # approvals 테이블: approver_1 또는 approver_2가 본인이면서 상태가 '대기'인 건수
        c.execute("""
            SELECT 
                COUNT(CASE WHEN doc_type = '연차신청서' THEN 1 END) as vac,
                COUNT(CASE WHEN doc_type = '기안서' THEN 1 END) as draft
            FROM approvals 
            WHERE (approver_1 = ? OR approver_2 = ?) AND status = '대기'
        """, (user_name, user_name))
        app_row = c.fetchone()
        noti_data['approval_vacation'] = app_row['vac']
        noti_data['approval_draft'] = app_row['draft']

        # [증명서] 발급 대기 현황 (전체 관리용 - 보통 레벨이 높거나 관리자일 때만 의미가 있음)
        # 만약 별도의 테이블이 없다면 approvals 내의 특정 타입을 쿼리하거나 처리
        # 여기서는 approvals 테이블 내 '증명서' 타입의 대기 건수를 예시로 함
        c.execute("SELECT COUNT(*) FROM approvals WHERE doc_type LIKE '%증명서%' AND status = '대기'")
        noti_data['cert_wait'] = c.fetchone()[0]

        # [전자계약] 본인이 작성자(drafter)인 문서 중 미결재(대기) 상태인 건수
        c.execute("SELECT COUNT(*) FROM approvals WHERE doc_type = '전자계약' AND drafter = ? AND status = '대기'", (user_name,))
        noti_data['contract_miss'] = c.fetchone()[0]

        # [게시판] 최근 24시간 이내의 신규 게시글 수
        yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
        c.execute("SELECT COUNT(*) FROM board WHERE created_at >= ?", (yesterday,))
        noti_data['board_new'] = c.fetchone()[0]

        # [쪽지] 수신자가 본인이면서 읽지 않은(is_read=0) 쪽지 수
        c.execute("SELECT COUNT(*) FROM messages WHERE receiver = ? AND is_read = 0", (user_name,))
        noti_data['message_new'] = c.fetchone()[0]

    except sqlite3.Error as e:
        print(f"DB 알림 조회 오류: {e}")
        # 오류 발생 시 모든 수치를 0으로 초기화하여 렌더링 에러 방지
        noti_data = {k: 0 for k in ['approval_vacation', 'approval_draft', 'cert_wait', 'contract_miss', 'board_new', 'message_new']}
    
    finally:
        conn.close()

    # 3. 렌더링 후 반환
    return render_template('widgets/noti_widget.html', data=noti_data)