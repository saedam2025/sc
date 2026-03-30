from flask import Flask, render_template, request, jsonify, send_file
import pandas as pd
import os
from datetime import datetime
from collections import Counter

app = Flask(__name__)

# [설정] Render 유료 디스크 마운트 경로
STORAGE_DIR = '/mnt/data'
if not os.path.exists(STORAGE_DIR):
    # 로컬 테스트 시에는 현재 폴더 사용
    STORAGE_DIR = os.path.dirname(os.path.abspath(__file__))

EXCEL_FILE = os.path.join(STORAGE_DIR, 'tasks.xlsx')
OWNER_FILE = os.path.join(STORAGE_DIR, 'owners.xlsx')

# 관리자 암호 (신규 담당자 등록 시에만 필요)
ADMIN_PASSWORD = "1900"

def init_files():
    """서버 시작 시 파일이 없으면 자동 생성"""
    if not os.path.exists(EXCEL_FILE):
        df = pd.DataFrame(columns=['연도', '날짜', '담당자', '내근업무', '외근업무', '회의', '면접', '비고', '기타'])
        df.to_excel(EXCEL_FILE, index=False, engine='openpyxl')
    if not os.path.exists(OWNER_FILE):
        # [구조 변경] 담당자명과 암호를 함께 저장
        df = pd.DataFrame(columns=['이름', '암호'])
        df.to_excel(OWNER_FILE, index=False, engine='openpyxl')

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/get_owners')
def get_owners():
    if not os.path.exists(OWNER_FILE): init_files()
    try:
        df = pd.read_excel(OWNER_FILE, engine='openpyxl').fillna('')
        return jsonify(df['이름'].tolist())
    except: return jsonify([])

@app.route('/add_owner', methods=['POST'])
def add_owner():
    """관리자 암호 확인 후, 담당자명과 개인 암호 저장"""
    data = request.json
    name = data.get('name')
    # 담당자가 사용할 개인 암호
    owner_pass = data.get('owner_pass') 
    # 관리자 암호
    admin_pass = data.get('admin_pass') 
    
    if admin_pass != ADMIN_PASSWORD:
        return jsonify({"status": "error", "message": "관리자 암호가 틀렸습니다."}), 403
    
    if not name or not owner_pass:
        return jsonify({"status": "error", "message": "이름과 암호를 모두 입력하세요."})
    
    try:
        df = pd.read_excel(OWNER_FILE, engine='openpyxl')
        if name in df['이름'].values:
            return jsonify({"status": "error", "message": "이미 등록된 담당자입니다."})
            
        new_row = pd.DataFrame([{'이름': name, '암호': str(owner_pass)}])
        df = pd.concat([df, new_row], ignore_index=True)
        df.to_excel(OWNER_FILE, index=False, engine='openpyxl')
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/get_tasks')
def get_tasks():
    if not os.path.exists(EXCEL_FILE): init_files()
    try:
        df = pd.read_excel(EXCEL_FILE, engine='openpyxl').fillna('')
        tasks = []
        for index, row in df.iterrows():
            tasks.append({
                # FullCalendar가 고유 ID로 인식 (수정 시 사용)
                "id": str(index), 
                "start": str(row['날짜']),
                "extendedProps": {
                    "owner": row['담당자'], "inside": row['내근업무'], "outside": row['외근업무'],
                    "meeting": row['회의'], "interview": row['면접'], "note": row['비고'], "etc": row['기타']
                }
            })
        return jsonify(tasks)
    except: return jsonify([])

# --- [2번 기능] 시각화 통계 데이터 API ---
@app.route('/get_school_stats')
def get_school_stats():
    """학교별 방문 빈도 및 담당자별 방문 통계 데이터 생성"""
    if not os.path.exists(EXCEL_FILE): return jsonify({})
    try:
        df = pd.read_excel(EXCEL_FILE, engine='openpyxl').fillna('')
        school_keywords = ['초', '중', '고', '학교']
        
        # 1. 학교 언급이 있는 데이터만 필터링
        school_tasks = df[df['외근업무'].apply(lambda x: any(k in str(x) for k in school_keywords))]
        
        if school_tasks.empty: return jsonify({"status":"empty"})

        # 2. 학교명 축소 로직 (간단화: '초등학교' -> '초')
        def extract_school(text):
            text = str(text)
            for k in school_keywords:
                if k in text:
                    idx = text.find(k)
                    pre_text = text[:idx].strip().split()
                    if pre_text: return pre_text[-1] + k
            return "기타"

        school_tasks = school_tasks.copy()
        school_tasks['학교명'] = school_tasks['외근업무'].apply(extract_school)
        
        # 3. 학교별 방문 횟수 통계
        school_counts = school_tasks['학교명'].value_counts()
        pie_data = {
            "labels": school_counts.index.tolist(),
            "datasets": [{
                "data": school_counts.values.tolist(),
                "backgroundColor": ['#ff9f43', '#0abde3', '#ee5253', '#10ac84', '#5f27cd', '#c8d6e5']
            }]
        }

        # 4. 가장 많이 방문한 학교 TOP 5 (Bar Chart)
        top5 = school_counts.head(5)
        top5_data = {
            "labels": top5.index.tolist(),
            "datasets": [{
                "label": "방문 횟수",
                "data": top5.values.tolist(),
                "backgroundColor": '#198754'
            }]
        }
        
        return jsonify({
            "pie": pie_data, 
            "top5": top5_data, 
            "total_visits": int(school_counts.sum()),
            "unique_schools": int(len(school_counts))
        })
        
    except Exception as e:
        print(f"Stats Data Error: {e}")
        return jsonify({"status":"error", "message": str(e)})

@app.route('/save_task', methods=['POST'])
def save_task():
    """신규 업무 저장"""
    data = request.json
    if not os.path.exists(EXCEL_FILE): init_files()
    try:
        df = pd.read_excel(EXCEL_FILE, engine='openpyxl')
        date_obj = datetime.strptime(data['date'], '%Y-%m-%d')
        new_row = {
            '연도': date_obj.year, '날짜': data['date'], '담당자': data['owner'],
            '내근업무': data['inside'], '외근업무': data['outside'],
            '회의': data['meeting'], '면접': data['interview'],
            '비고': data['note'], '기타': data.get('etc', '')
        }
        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
        df.to_excel(EXCEL_FILE, index=False, engine='openpyxl')
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# --- [3, 4번 기능] 담당자 암호 확인 및 업무 수정 ---
@app.route('/update_task', methods=['POST'])
def update_task():
    """담당자 개인 암호 확인 후 엑셀 행 데이터 업데이트"""
    data = request.json
    task_id = int(data.get('id'))
    owner = data.get('owner')
    password = data.get('password')
    
    if not os.path.exists(EXCEL_FILE) or not os.path.exists(OWNER_FILE):
        return jsonify({"status": "error", "message": "시스템 오류"}), 500
        
    try:
        # 1. 담당자 개인 암호 검증
        owners_df = pd.read_excel(OWNER_FILE, engine='openpyxl')
        owner_data = owners_df[owners_df['이름'] == owner]
        
        if owner_data.empty or str(owner_data.iloc[0]['암호']) != str(password):
            return jsonify({"status": "error", "message": "담당자 암호가 일치하지 않습니다."}), 403
            
        # 2. 엑셀 데이터 수정
        tasks_df = pd.read_excel(EXCEL_FILE, engine='openpyxl')
        
        # 수정할 데이터 매핑
        tasks_df.at[task_id, '내근업무'] = data.get('inside', '')
        tasks_df.at[task_id, '외근업무'] = data.get('outside', '')
        tasks_df.at[task_id, '회의'] = data.get('meeting', '')
        tasks_df.at[task_id, '면접'] = data.get('interview', '')
        tasks_df.at[task_id, '비고'] = data.get('note', '')
        tasks_df.at[task_id, '기타'] = data.get('etc', '')
        
        # 파일 저장
        tasks_df.to_excel(EXCEL_FILE, index=False, engine='openpyxl')
        return jsonify({"status": "success"})
        
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/download')
def download_file():
    if os.path.exists(EXCEL_FILE): return send_file(EXCEL_FILE, as_attachment=True)
    return "파일 없음", 404

if __name__ == '__main__':
    init_files()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)