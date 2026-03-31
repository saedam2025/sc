from flask import Blueprint, render_template, jsonify
from .db_handler import read_excel_db, EXCEL_FILE, ATTEND_FILE
from datetime import datetime

main_bp = Blueprint('main', __name__)

@main_bp.route('/')
def index():
    # 1. 업무 스케줄(tasks.xlsx) 읽기
    df_tasks = read_excel_db(EXCEL_FILE)
    events = []
    
    if not df_tasks.empty:
        for idx, row in df_tasks.iterrows():
            # FullCalendar 형식에 맞춰 데이터 구성
            events.append({
                "id": str(idx),
                "title": f"[{row['담당자']}] {row['내근업무'] or row['외근업무']}",
                "start": str(row['날짜']),
                "color": "#4a90e2", # 기본 파란색
                "extendedProps": {
                    "owner": str(row['담당자']),
                    "inside": str(row['내근업무']),
                    "outside": str(row['외근업무']),
                    "meeting": str(row['회의']),
                    "note": str(row['비고'])
                }
            })

    # 2. 근태/휴가(attendance.xlsx) 중 승인된 것만 달력에 추가
    df_attend = read_excel_db(ATTEND_FILE)
    if not df_attend.empty:
        approved_vacation = df_attend[df_attend['승인상태'] == '승인']
        for idx, row in approved_vacation.iterrows():
            events.append({
                "title": f"[{row['담당자']}] {row['구분']}",
                "start": str(row['시작일']),
                "end": str(row['종료일']),
                "color": "#ff6b6b", # 휴가는 빨간색 계열
                "allDay": True
            })

    today_str = datetime.now().strftime('%Y-%m-%d')
    # 오늘 날짜에 해당하는 업무만 필터링해서 리스트로 전달
    today_events = [e for e in events if e.get('start') == today_str]

    return render_template('main.html', 
                           events=events, 
                           today_events=today_events,
                           today_str=datetime.now().strftime('%Y년 %m월 %d일'))