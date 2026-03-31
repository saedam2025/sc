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
            # [담당자] 업무내용 형식으로 타이틀 구성
            title = f"[{row['담당자']}] {row['내근업무'] or row['외근업무']}"
            events.append({
                "id": f"task_{idx}",
                "title": title,
                "start": str(row['날짜']),
                "color": "#4a90e2", # 일반 업무: 파란색
                "extendedProps": {
                    "owner": str(row['담당자']),
                    "inside": str(row['내근업무']),
                    "outside": str(row['외근업무']),
                    "note": str(row['비고'])
                }
            })

    # 2. 근태/휴가(attendance.xlsx) 중 승인된 항목 추가
    df_attend = read_excel_db(ATTEND_FILE)
    if not df_attend.empty:
        approved = df_attend[df_attend['승인상태'] == '승인']
        for idx, row in approved.iterrows():
            events.append({
                "title": f"[{row['담당자']}] {row['구분']}",
                "start": str(row['시작일']),
                "end": str(row['종료일']),
                "color": "#ff6b6b", # 휴가/외출: 빨간색
                "allDay": True
            })

    today_str = datetime.now().strftime('%Y-%m-%d')
    # 오늘 날짜 일정만 필터링 (우측 TODAY 리스트용)
    today_events = [e for e in events if e['start'] == today_str]

    return render_template('main.html', 
                           events=events, 
                           today_events=today_events,
                           today_str=datetime.now().strftime('%Y년 %m월 %d일'))