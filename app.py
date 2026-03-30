from flask import Flask, render_template, request, jsonify
import sqlite3

app = Flask(__name__)

# 데이터베이스 초기화
def init_db():
    with sqlite3.connect('tasks.db') as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS tasks 
                        (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                         title TEXT, start_date TEXT, owner TEXT)''')

@app.route('/')
def index():
    return render_template('index.html')

# 일정 목록 가져오기
@app.route('/get_tasks')
def get_tasks():
    with sqlite3.connect('tasks.db') as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, title, start_date, owner FROM tasks")
        tasks = [{"id": row[0], "title": f"[{row[3]}] {row[1]}", "start": row[2], "owner": row[3]} for row in cur.fetchall()]
    return jsonify(tasks)

# 일정 추가 및 수정
@app.route('/save_task', methods=['POST'])
def save_task():
    data = request.json
    with sqlite3.connect('tasks.db') as conn:
        cur = conn.cursor()
        if data.get('id'): # 수정
            cur.execute("UPDATE tasks SET title=?, owner=? WHERE id=?", (data['title'], data['owner'], data['id']))
        else: # 신규 저장
            cur.execute("INSERT INTO tasks (title, start_date, owner) VALUES (?, ?, ?)", 
                        (data['title'], data['date'], data['owner']))
    return jsonify({"status": "success"})

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000)