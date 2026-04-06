from flask import Blueprint, render_template, jsonify, session, request
from .db_handler import read_excel_db, write_excel_db, EXCEL_FILE, ATTEND_FILE
from datetime import datetime, timedelta
import pandas as pd
import holidays

main_bp = Blueprint('main', __name__)

@main_bp.route('/')
def index():
    # 카테고리 및 색상 정의
    cats = ['회의', '면접', '미팅', '외근', '기타', '근태/휴가']
    cat_colors = {
        '회의': '#9b59b6',     # 보라색
        '면접': '#f1c40f',     # 노란색
        '미팅': '#1abc9c',     # 청록색
        '외근': '#e67e22',     # 주황색
        '기타': '#7b8a9e',     # 회색
        '근태/휴가': '#e74c3c' # 빨간색
    }

    events = []
    
    # 1. 업무 스케줄(tasks.xlsx) 읽기
    df_tasks = read_excel_db(EXCEL_FILE)
    if not df_tasks.empty:
        df_tasks = df_tasks.fillna('')
        
        for idx, row in df_tasks.iterrows():
            owner = str(row.get('담당자', ''))
            date_str = str(row.get('날짜', ''))
            note = str(row.get('비고', ''))
            
            # 각 카테고리별로 데이터가 입력되어 있으면 별도의 이벤트로 분리하여 추가
            for cat in ['회의', '면접', '미팅', '외근', '기타']:
                title_key = f"{cat}_제목"
                time_key = f"{cat}_시간"
                
                title_val = str(row.get(title_key, '')).strip()
                time_val = str(row.get(time_key, '')).strip()
                
                if title_val:
                    # 달력에는 [회의] 형태만 표시, 오른쪽 판넬은 상세 유지
                    events.append({
                        "id": f"task_{idx}_{cat}",
                        "title": title_val, # 원본 제목 유지 (상세창용)
                        "start": date_str,
                        "color": cat_colors[cat],
                        "extendedProps": {
                            "owner": owner,
                            "category": cat,
                            "task_title": title_val,
                            "task_time": time_val,
                            "note": note
                        }
                    })

    # 2. 근태/휴가(attendance.xlsx) 중 승인된 항목 추가
    df_attend = read_excel_db(ATTEND_FILE)
    if not df_attend.empty:
        df_attend = df_attend.fillna('')
        approved = df_attend[df_attend['승인상태'] == '승인']
        for idx, row in approved.iterrows():
            events.append({
                "title": str(row['구분']),
                "start": str(row['시작일']),
                "end": str(row['종료일']),
                "color": cat_colors['근태/휴가'],
                "allDay": True,
                "extendedProps": {
                    "owner": str(row['담당자']),
                    "category": "근태/휴가",
                    "task_title": str(row['구분']),
                    "task_time": "",
                    "note": ""
                }
            })

    # 3. 날짜 계산 및 카테고리별 그룹핑 (오늘 및 주간 일정) - 오른쪽 판넬용
    today = datetime.now()
    today_date = today.date()
    tomorrow_date = today_date + timedelta(days=1)
    next_week_date = today_date + timedelta(days=7)

    today_grouped = {c: [] for c in cats}
    weekly_grouped = {c: [] for c in cats}

    for e in events:
        try:
            start_date = datetime.strptime(e['start'][:10], '%Y-%m-%d').date()
            end_date = start_date
            if 'end' in e and e['end']:
                # FullCalendar end is exclusive
                end_date_orig = datetime.strptime(e['end'][:10], '%Y-%m-%d').date()
                if e.get('allDay'):
                     end_date = end_date_orig - timedelta(days=1)
                else:
                     end_date = end_date_orig

            cat = e.get('extendedProps', {}).get('category', '기타')
            
            # 오른쪽 판넬용 상세 제목 구성
            owner = e.get('extendedProps', {}).get('owner', '')
            task_title = e.get('extendedProps', {}).get('task_title', '')
            task_time = e.get('extendedProps', {}).get('task_time', '')
            
            display_title_detailed = f"[{owner}] {task_title}"
            if task_time:
                display_title_detailed += f" ({task_time})"
            
            event_copy = e.copy()
            event_copy['display_title_detailed'] = display_title_detailed

            # 오늘 일정 포함 여부 확인
            if start_date <= today_date <= end_date:
                if cat in today_grouped:
                    today_grouped[cat].append(event_copy)
            
            # 주간 일정 포함 여부 확인
            if start_date <= next_week_date and end_date >= tomorrow_date:
                if cat in weekly_grouped:
                    weekly_grouped[cat].append(event_copy)
                    
        except ValueError:
            continue

    # 시간순 정렬
    for cat in cats:
        today_grouped[cat].sort(key=lambda x: x['start'])
        weekly_grouped[cat].sort(key=lambda x: x['start'])

    # 4. 한국 공휴일 데이터 생성
    kr_holidays = holidays.KR(years=[today_date.year, today_date.year + 1])
    holidays_dict = {}
    for date, name in kr_holidays.items():
        holidays_dict[str(date)] = str(name)

    current_user = session.get('user_name', '배서현') 

    return render_template('main.html', 
                           events=events, 
                           today_grouped=today_grouped,
                           weekly_grouped=weekly_grouped,
                           cats=cats,
                           today_str=today.strftime('%Y년 %m월 %d일'),
                           holidays_dict=holidays_dict,
                           current_user=current_user)

@main_bp.route('/save_task', methods=['POST'])
def save_task():
    try:
        data = request.get_json()
        if not data: return jsonify({"status": "error", "message": "데이터가 없습니다."}), 400

        date_str = data.get('date', '')
        owner = data.get('owner', '')
        year = date_str[:4] if date_str else ''
        df_tasks = read_excel_db(EXCEL_FILE)

        new_row = pd.DataFrame([{
            '연도': year, '날짜': date_str, '담당자': owner,
            '회의_제목': data.get('회의_제목', ''), '회의_시간': data.get('회의_시간', ''),
            '면접_제목': data.get('면접_제목', ''), '면접_시간': data.get('면접_시간', ''),
            '미팅_제목': data.get('미팅_제목', ''), '미팅_시간': data.get('미팅_시간', ''),
            '외근_제목': data.get('외근_제목', ''), '외근_시간': data.get('외근_시간', ''),
            '기타_제목': data.get('기타_제목', ''), '기타_시간': data.get('기타_시간', ''),
            '비고': data.get('note', '')
        }])

        df_tasks = pd.concat([df_tasks, new_row], ignore_index=True) if not df_tasks.empty else new_row
        write_excel_db(df_tasks, EXCEL_FILE)
        return jsonify({"status": "success", "message": "일정 등록 완료"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500