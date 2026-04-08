from flask import Blueprint, render_template, request, session, jsonify
from datetime import datetime
import sqlite3

# app.py에서 사용하는 데이터베이스 연결 함수 가져오기
from routes.database import get_db

attendance_bp = Blueprint('attendance', __name__)

@attendance_bp.route('/attendance')
def attendance_list():
    # 1. 로그인 정보 및 권한 확인 (세션 기준)
    emp_no = session.get('emp_no')
    user_level = session.get('user_level', 4) # 기본값 4
    
    # 파라미터 받기 (기본값: 이번 달)
    target_month = request.args.get('month', datetime.now().strftime('%Y-%m'))
    search_emp_no = request.args.get('search_emp_no', '')

    conn = get_db()
    
    # 2 & 4. 권한에 따른 쿼리 조건 설정 (users 테이블과 JOIN하여 이름도 가져옴)
    # [수정됨] attendance 테이블에서 daily_attendance 테이블로 변경
    query = """
        SELECT a.id, a.emp_no, a.date, a.clock_in_time, a.clock_out_time, a.status, u.name as user_name
        FROM daily_attendance a
        JOIN users u ON a.emp_no = u.emp_no
        WHERE a.date LIKE ?
    """
    params = [f"{target_month}-%"]

    # 레벨 4 이하는 본인 것만 보기
    if user_level >= 4:
        query += " AND a.emp_no = ?"
        params.append(str(emp_no))
    # 관리자가 특정 회원을 검색한 경우
    elif search_emp_no:
        query += " AND a.emp_no = ?"
        params.append(str(search_emp_no))

    # 날짜 내림차순, 출근시간 오름차순 정렬
    query += " ORDER BY a.date DESC, a.clock_in_time ASC"

    raw_records = conn.execute(query, params).fetchall()

    # 2. 일별 출근 등수 계산 로직 (딕셔너리로 변환하여 처리)
    records = []
    daily_ranks = {}
    
    for row in raw_records:
        record = dict(row) # sqlite3.Row 객체를 수정 가능한 딕셔너리로 변환
        date_str = record['date']
        
        if date_str not in daily_ranks:
            daily_ranks[date_str] = 1
            
        record['daily_rank'] = daily_ranks[date_str]
        daily_ranks[date_str] += 1
        records.append(record)

    # 관리자용 회원 목록 (셀렉트 박스용)
    all_users = []
    if user_level <= 3:
        all_users = [dict(u) for u in conn.execute("SELECT emp_no, name FROM users ORDER BY name").fetchall()]

    conn.close()

    return render_template(
        'attendance.html', 
        records=records, 
        current_month=target_month,
        all_users=all_users,
        user_level=user_level,
        search_emp_no=search_emp_no,
        current_emp_no=emp_no
    )

@attendance_bp.route('/attendance/clock_out', methods=['POST'])
def clock_out():
    """3. 퇴근/조퇴 처리 API"""
    emp_no = session.get('emp_no')
    data = request.json
    record_id = data.get('record_id')
    action_type = data.get('type') # '퇴근' 또는 '조퇴'

    conn = get_db()
    # [수정됨] attendance 테이블에서 daily_attendance 테이블로 변경
    record = conn.execute("SELECT * FROM daily_attendance WHERE id = ?", (record_id,)).fetchone()
    
    # 본인의 기록이 맞는지 검증
    if not record or str(record['emp_no']) != str(emp_no):
        conn.close()
        return jsonify({"success": False, "message": "권한이 없거나 잘못된 요청입니다."}), 403
    
    # 이미 퇴근했는지 검증
    if record['clock_out_time']:
        conn.close()
        return jsonify({"success": False, "message": "이미 퇴근 처리가 완료되었습니다."}), 400

    current_time = datetime.now().strftime('%H:%M:%S')
    
    # DB 업데이트
    # [수정됨] attendance 테이블에서 daily_attendance 테이블로 변경
    conn.execute(
        "UPDATE daily_attendance SET clock_out_time = ?, status = ? WHERE id = ?",
        (current_time, action_type, record_id)
    )
    conn.commit()
    conn.close()
    
    return jsonify({"success": True, "message": f"{action_type} 처리가 완료되었습니다."})