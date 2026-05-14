from flask import Blueprint, render_template, request, jsonify, session
from routes.database import get_db

school_task_bp = Blueprint('school_task', __name__)

def get_mapped_category(raw_cat):
    """
    DB에 저장된 다양한 형태의 카테고리 ID(공백, 언더바, 과거이름 등)를 
    화면에 출력할 정확한 9개의 한글명으로 강력하게 매핑해주는 스마트 함수
    """
    if not raw_cat:
        return "미분류"
    
    # 소문자로 변환하고 띄어쓰기, 언더바(_), 하이픈(-)을 모두 제거하여 일치 확률을 100%로 끌어올림
    raw_lower = str(raw_cat).strip().lower().replace(" ", "").replace("_", "").replace("-", "")
    
    if raw_lower in ['notice', '수강안내문', '수강안내']: 
        return '수강안내문'
    elif raw_lower in ['weeklyreport', 'report', 'weekly', '주간업무보고', '주간업무']: 
        return '주간업무보고'
    elif raw_lower in ['openclass', 'class', 'open', '공개수업']: 
        return '공개수업'
    elif raw_lower in ['expense', '지출결의서', '지출결의']: 
        return '지출결의서'
    elif raw_lower in ['itemrequest', 'item', 'request', '물품요청', '물품']: 
        return '물품요청'
    elif raw_lower in ['workschedule', 'schedule', 'work', '근무표', '근무']: 
        return '근무표'
    elif raw_lower in ['billing', '청구관련', '청구']: 
        return '청구관련'
    elif raw_lower in ['survey', '만족도조사', '만족도']: 
        return '만족도조사'
    elif raw_lower in ['reference', 'archive', '자료실', '자료']: 
        return '자료실'
    
    # 만약 위 목록에 없는 완전히 엉뚱한 값이면 원본을 보여주어 데이터가 숨겨지지 않도록 방어
    return str(raw_cat).strip()

@school_task_bp.route('/')
def task_list():
    """
    1. 게시물 목록 및 9개 사이드바 렌더링
    """
    categories = [
        {'id': 'notice', 'name': '수강안내문', 'icon': 'fa-circle-info'},
        {'id': 'weekly_report', 'name': '주간업무보고', 'icon': 'fa-list-check'},
        {'id': 'open_class', 'name': '공개수업', 'icon': 'fa-chalkboard-user'},
        {'id': 'expense', 'name': '지출결의서', 'icon': 'fa-file-invoice-dollar'},
        {'id': 'item_request', 'name': '물품요청', 'icon': 'fa-box'},
        {'id': 'work_schedule', 'name': '근무표', 'icon': 'fa-calendar-days'},
        {'id': 'billing', 'name': '청구관련', 'icon': 'fa-receipt'},
        {'id': 'survey', 'name': '만족도조사', 'icon': 'fa-chart-simple'},
        {'id': 'reference', 'name': '자료실', 'icon': 'fa-file-zipper'}
    ]

    conn = get_db()
    tasks = []
    
    try:
        rows = conn.execute('''
            SELECT p.*, s.school_name 
            FROM school_posts p
            LEFT JOIN schools s ON p.school_id = s.id
            ORDER BY p.created_at DESC
        ''').fetchall()
        
        for row in rows:
            r_dict = dict(row)
            raw_cat = r_dict.get('category', '')
            
            # 강력한 스마트 함수를 통과시키면 9개 메뉴 이름 중 하나로 무조건 맞춰집니다.
            cat_name = get_mapped_category(raw_cat) 
            
            tasks.append({
                'id': r_dict.get('id'),
                'school_name': r_dict.get('school_name') or '알 수 없음',
                'cat_name': cat_name, # 화면의 카테고리 이름과 100% 일치하게 됨
                'title': r_dict.get('title', ''),
                'author': r_dict.get('author', ''),
                'date': str(r_dict.get('created_at', ''))[:10] if r_dict.get('created_at') else '',
                'status': r_dict.get('status') or '접수',
                'processor': r_dict.get('processor') or '-'
            })
    except Exception as e:
        print(f"전체 업무 목록 불러오기 실패: {e}")
    finally:
        conn.close()

    return render_template('school_task.html', categories=categories, tasks=tasks)


@school_task_bp.route('/api/update_status', methods=['POST'])
def update_status():
    """
    2. 체크박스 선택 후 상태 변경
    """
    try:
        data = request.get_json()
        post_ids = data.get('post_ids', [])
        new_status = data.get('status')
        current_user = session.get('user_name', '관리자')

        if not post_ids or not new_status:
            return jsonify({'status': 'fail', 'message': '잘못된 요청입니다.'}), 400

        conn = get_db()
        for pid in post_ids:
            conn.execute('''
                UPDATE school_posts 
                SET status = ?, processor = ? 
                WHERE id = ?
            ''', (new_status, current_user, pid))
        conn.commit()
        conn.close()

        return jsonify({'status': 'success'})
    except Exception as e:
        print(f"상태 업데이트 중 에러 발생: {e}")
        return jsonify({'status': 'error', 'message': '상태 변경 중 오류가 발생했습니다.'}), 500


@school_task_bp.route('/api/detail/<int:post_id>', methods=['GET'])
def task_detail(post_id):
    """
    3. 테이블 클릭 시 상세 모달 데이터
    """
    try:
        conn = get_db()
        row = conn.execute('''
            SELECT p.*, s.school_name 
            FROM school_posts p
            LEFT JOIN schools s ON p.school_id = s.id
            WHERE p.id = ?
        ''', (post_id,)).fetchone()
        conn.close()

        if not row:
            return jsonify({'error': True, 'message': '게시물을 찾을 수 없습니다.'}), 404

        r_dict = dict(row)
        raw_cat = r_dict.get('category', '')
        
        # 상세 모달창에서도 에러가 나지 않도록 스마트 함수 적용
        cat_name = get_mapped_category(raw_cat)

        post = {
            'id': r_dict.get('id'),
            'category': cat_name,
            'cat_name': cat_name,
            'title': r_dict.get('title', ''),
            'author': r_dict.get('author', ''),
            'created_at': str(r_dict.get('created_at', ''))[:16] if r_dict.get('created_at') else '',
            'school_name': r_dict.get('school_name') or '알 수 없음',
            'processor': r_dict.get('processor') or '미지정',
            'status': r_dict.get('status') or '접수',
            'content': r_dict.get('content', ''),
            'filename': r_dict.get('filename', ''),
            'filepath': r_dict.get('filepath', '')
        }

        return jsonify(post)
    except Exception as e:
        print(f"상세조회 중 에러 발생: {e}")
        return jsonify({'error': True, 'message': '서버 처리 중 오류가 발생했습니다.'}), 500