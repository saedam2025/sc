from flask import Blueprint, render_template, jsonify, session
from .db_handler import read_excel_db, EXCEL_FILE, ATTEND_FILE
from datetime import datetime, timedelta
import holidays

main_bp = Blueprint('main', __name__)

@main_bp.route('/')
def index():
    # 1. 업무 스케줄(tasks.xlsx) 읽기
    df_tasks = read_excel_db(EXCEL_FILE)
    events = []
    
    if not df_tasks.empty:
        for idx, row in df_tasks.iterrows():
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

    # 3. 날짜 계산 (오늘 및 주간 일정 필터링용)
    today = datetime.now()
    today_date = today.date()
    tomorrow_date = today_date + timedelta(days=1)
    next_week_date = today_date + timedelta(days=7) # 내일부터 1주일

    today_events = []
    weekly_events = []

    for e in events:
        try:
            # 문자열 날짜를 datetime 객체로 변환하여 비교
            e_date_str = e['start'][:10]
            e_date = datetime.strptime(e_date_str, '%Y-%m-%d').date()
            
            if e_date == today_date:
                today_events.append(e)
            elif tomorrow_date <= e_date <= next_week_date:
                weekly_events.append(e)
        except ValueError:
            continue

    # 날짜순으로 주간 일정 정렬
    weekly_events.sort(key=lambda x: x['start'])

    # 4. 한국 공휴일 데이터 생성 (대체공휴일 이름 완전 축약 적용)
    kr_holidays = holidays.KR(years=[today_date.year, today_date.year + 1])
    holidays_dict = {}
    for date, name in kr_holidays.items():
        name_str = str(name)
        # 이름에 '대체'라는 단어가 포함되어 있으면 앞뒤 단어 무시하고 무조건 '대체공휴일'로 덮어쓰기
        if "대체" in name_str:
            holidays_dict[str(date)] = "대체공휴일"
        else:
            holidays_dict[str(date)] = name_str

    # 5. 로그인 세션 정보 가져오기 (현재 로그인한 유저 기본값 세팅)
    current_user = session.get('user_name', '배서현') 

    return render_template('main.html', 
                           events=events, 
                           today_events=today_events,
                           weekly_events=weekly_events,
                           today_str=today.strftime('%Y년 %m월 %d일'),
                           holidays_dict=holidays_dict,
                           current_user=current_user)