from flask import Flask, render_template, request, jsonify, send_file
import pandas as pd
import os
from datetime import datetime
from collections import Counter

app = Flask(__name__)

# [설정] Render 유료 디스크 마운트 경로
STORAGE_DIR = '/mnt/data'
if not os.path.exists(STORAGE_DIR):
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
    data = request.json
    name = data.get('name')
    owner_pass = data.get('owner_pass') 
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
                "id": str(index), 
                "start": str(row['날짜']),
                "extendedProps": {
                    "owner": row['담당자'], "inside": row['내근업무'], "outside": row['외근업무'],
                    "meeting": row['회의'], "interview": row['면접'], "note": row['비고'], "etc": row['기타']
                }
            })
        return jsonify(tasks)
    except: return jsonify([])

@app.route('/get_school_stats')
def get_school_stats():
    """학교 방문 빈도 및 담당자별 방문 통계 데이터 생성"""
    if not os.path.exists(EXCEL_FILE): return jsonify({})
    try:
        df = pd.read_excel(EXCEL_FILE, engine='openpyxl').fillna('')
        school_keywords = ['초', '중', '고', '학교']
        
        # 학교 언급이 있는 데이터만 필터링
        school_tasks = df[df['외근업무'].apply(lambda x: any(k in str(x) for k in school_keywords))]
        
        if school_tasks.empty: return jsonify({"status":"empty"})

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
        
        # 1. 학교별 방문 횟수 (도넛 차트용)
        school_counts = school_tasks['학교명'].value_counts()
        pie_data = {
            "labels": school_counts.index.tolist(),
            "datasets": [{
                "data": school_counts.values.tolist(),
                "backgroundColor": ['#ff9f43', '#0abde3', '#ee5253', '#10ac84', '#5f27cd', '#c8d6e5', '#576574']
            }]
        }

        # 2. TOP 5 학교 (바 차트용)
        top5 = school_counts.head(5)
        top5_data = {
            "labels": top5.index.tolist(),
            "datasets": [{
                "label": "방문 횟수",
                "data": top5.values.tolist(),
                "backgroundColor": '#1dd1a1'
            }]
        }
        
        # 3. [추가] 담당자별 학교 방문 횟수
        owner_counts = school_tasks['담당자'].value_counts()
        owner_data = {
            "labels": owner_counts.index.tolist(),
            "datasets": [{
                "label": "방문 건수",
                "data": owner_counts.values.tolist(),
                "backgroundColor": '#48dbfb'
            }]
        }
        
        return jsonify({
            "pie": pie_data, 
            "top5": top5_data, 
            "owner": owner_data,
            "total_visits": int(school_counts.sum()),
            "unique_schools": int(len(school_counts))
        })
    except Exception as e:
        return jsonify({"status":"error", "message": str(e)})

@app.route('/save_task', methods=['POST'])
def save_task():
    data = request.json
    if not os.path.exists(EXCEL_FILE): init_files()
    try:
        df = pd.read_excel(EXCEL_FILE, engine='openpyxl')
        date_obj = datetime.strptime(data['date'], '%Y-%m-%d')
        new_row = {
            '연도': date_obj.year, '날짜': data['date'], '담당자': data['owner'],
            '내근업무': data['inside'], '외근업무': data['outside'],
            '회의': data['meeting'], '면접': data.get('interview', ''),
            '비고': data['note'], '기타': data.get('etc', '')
        }
        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
        df.to_excel(EXCEL_FILE, index=False, engine='openpyxl')
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/update_task', methods=['POST'])
def update_task():
    data = request.json
    task_id = int(data.get('id'))
    owner = data.get('owner')
    password = data.get('password')
    
    try:
        owners_df = pd.read_excel(OWNER_FILE, engine='openpyxl')
        owner_data = owners_df[owners_df['이름'] == owner]
        
        if owner_data.empty or str(owner_data.iloc[0]['암호']) != str(password):
            return jsonify({"status": "error", "message": "담당자 암호가 일치하지 않습니다."}), 403
            
        tasks_df = pd.read_excel(EXCEL_FILE, engine='openpyxl')
        tasks_df = tasks_df.astype(object) # 오류 방지 핵심 로직
        
        tasks_df.at[task_id, '내근업무'] = data.get('inside', '')
        tasks_df.at[task_id, '외근업무'] = data.get('outside', '')
        tasks_df.at[task_id, '회의'] = data.get('meeting', '')
        tasks_df.at[task_id, '면접'] = data.get('interview', '')
        tasks_df.at[task_id, '비고'] = data.get('note', '')
        tasks_df.at[task_id, '기타'] = data.get('etc', '')
        
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