from flask import Blueprint, render_template, request, jsonify, session
from routes.database import get_db
from routes.school_post_confirmation import (
    ensure_confirmation_schema,
    ensure_view_count_schema,
    get_confirmation_map,
    get_confirmation_summary,
    increment_view_count,
    is_shared_board,
)

school_task_bp = Blueprint('school_task', __name__)
VALID_STATUSES = {'접수', '처리중', '완료'}
SCHOOL_TASK_CATEGORIES = (
    {'id': 'community', 'name': '본부공지사항', 'icon': 'fa-bullhorn'},
    {'id': 'notice', 'name': '수강안내문', 'icon': 'fa-circle-info'},
    {'id': 'weekly_report', 'name': '주간업무보고', 'icon': 'fa-list-check'},
    {'id': 'open_class', 'name': '강사정보현황', 'icon': 'fa-chalkboard-user'},
    {'id': 'expense', 'name': '지출결의서', 'icon': 'fa-file-invoice-dollar'},
    {'id': 'item_request', 'name': '물품요청', 'icon': 'fa-box'},
    {'id': 'work_schedule', 'name': '근무표', 'icon': 'fa-calendar-days'},
    {'id': 'billing', 'name': '청구관련', 'icon': 'fa-receipt'},
    {'id': 'survey', 'name': '공개수업&만족도조사', 'icon': 'fa-chart-simple'},
    {'id': 'reference', 'name': '자료실', 'icon': 'fa-file-zipper'},
)
CATEGORY_BY_ID = {item['id']: item for item in SCHOOL_TASK_CATEGORIES}


@school_task_bp.before_request
def headquarters_only():
    """전 학교 통합 업무 목록은 본사 계정만 이용한다."""
    try:
        user_level = int(session.get('user_level', 99))
    except (TypeError, ValueError):
        user_level = 99
    if (
        session.get('user_name') != 'admin'
        and (not session.get('emp_no') or not 1 <= user_level <= 7)
    ):
        return "전 학교 통합 업무 목록은 본사 담당자만 이용할 수 있습니다.", 403


def get_mapped_category(raw_cat):
    """
    DB에 저장된 다양한 형태의 카테고리 ID(공백, 언더바, 과거이름 등)를 
    화면에 출력할 정확한 게시판 한글명으로 매핑해주는 함수
    """
    if not raw_cat:
        return "미분류"
    
    # 소문자로 변환하고 띄어쓰기, 언더바(_), 하이픈(-)을 모두 제거하여 일치 확률을 100%로 끌어올림
    raw_lower = str(raw_cat).strip().lower().replace(" ", "").replace("_", "").replace("-", "")
    
    if raw_lower in ['community', '본부공지사항', '본부공지', '커뮤니티']:
        return '본부공지사항'
    elif raw_lower in ['notice', '수강안내문', '수강안내']: 
        return '수강안내문'
    elif raw_lower in ['weeklyreport', 'report', 'weekly', '주간업무보고', '주간업무']: 
        return '주간업무보고'
    elif raw_lower in ['openclass', 'class', 'open', '공개수업', '강사정보현황']:
        return '강사정보현황'
    elif raw_lower in ['expense', '지출결의서', '지출결의']: 
        return '지출결의서'
    elif raw_lower in ['itemrequest', 'item', 'request', '물품요청', '물품']: 
        return '물품요청'
    elif raw_lower in ['workschedule', 'schedule', 'work', '근무표', '근무']: 
        return '근무표'
    elif raw_lower in ['billing', '청구관련', '청구']: 
        return '청구관련'
    elif raw_lower in ['survey', '만족도조사', '만족도', '공개수업&만족도조사']:
        return '공개수업&만족도조사'
    elif raw_lower in ['reference', 'archive', '자료실', '자료']: 
        return '자료실'
    
    # 만약 위 목록에 없는 완전히 엉뚱한 값이면 원본을 보여주어 데이터가 숨겨지지 않도록 방어
    return str(raw_cat).strip()

@school_task_bp.route('/')
def task_list():
    """
    1. 게시물 목록 및 게시판 사이드바 렌더링
    """
    categories = list(SCHOOL_TASK_CATEGORIES)

    conn = get_db()
    tasks = []
    
    try:
        ensure_confirmation_schema(conn)
        rows = conn.execute('''
            SELECT p.*, s.school_name 
            FROM school_posts p
            LEFT JOIN schools s ON p.school_id = s.id
            ORDER BY p.created_at DESC
        ''').fetchall()
        
        row_dicts = [dict(row) for row in rows]
        shared_ids = [row['id'] for row in row_dicts if is_shared_board(row.get('category'))]
        confirmation_map = get_confirmation_map(conn, shared_ids)

        for r_dict in row_dicts:
            raw_cat = r_dict.get('category', '')
            
            # 과거 영문/한글 카테고리를 현재 메뉴 이름으로 맞춘다.
            cat_name = get_mapped_category(raw_cat) 
            
            shared = is_shared_board(raw_cat)
            confirmations = confirmation_map.get(r_dict.get('id'), []) if shared else []
            tasks.append({
                'id': r_dict.get('id'),
                'school_name': '전체 센터' if shared else (r_dict.get('school_name') or '알 수 없음'),
                'original_school_name': r_dict.get('school_name') or '',
                'cat_name': cat_name, # 화면의 카테고리 이름과 100% 일치하게 됨
                'title': r_dict.get('title', ''),
                'author': r_dict.get('author', ''),
                'date': str(r_dict.get('created_at', ''))[:10] if r_dict.get('created_at') else '',
                'status': r_dict.get('status') or '접수',
                'processor': r_dict.get('processor') or '-',
                'is_shared': shared,
                'confirmation_count': len(confirmations),
                'confirmations': confirmations,
                'confirmation_names': [item['display_name'] for item in confirmations],
            })
    except Exception as e:
        print(f"전체 업무 목록 불러오기 실패: {e}")
    finally:
        conn.close()

    return render_template('school_task.html', categories=categories, tasks=tasks)


def normalize_post_ids(raw_post_ids):
    if not isinstance(raw_post_ids, list):
        return []
    post_ids = []
    for raw_id in raw_post_ids:
        try:
            post_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if post_id > 0 and post_id not in post_ids:
            post_ids.append(post_id)
    return post_ids


@school_task_bp.route('/api/update_status', methods=['POST'])
def update_status():
    """
    2. 체크박스 선택 후 상태 변경
    """
    conn = None
    try:
        data = request.get_json(silent=True) or {}
        raw_post_ids = data.get('post_ids', [])
        new_status = str(data.get('status') or '').strip()
        current_user = str(
            session.get('user_name')
            or session.get('name')
            or session.get('emp_no')
            or '관리자'
        ).strip()

        if not isinstance(raw_post_ids, list) or new_status not in VALID_STATUSES:
            return jsonify({'status': 'fail', 'message': '유효하지 않은 상태 변경 요청입니다.'}), 400

        post_ids = normalize_post_ids(raw_post_ids)

        if not post_ids:
            return jsonify({'status': 'fail', 'message': '변경할 게시물을 선택해주세요.'}), 400

        conn = get_db()
        ensure_confirmation_schema(conn)
        placeholders = ','.join('?' for _ in post_ids)
        selected_posts = conn.execute(f'''
            SELECT id, category
            FROM school_posts
            WHERE id IN ({placeholders})
        ''', post_ids).fetchall()
        shared_ids = [row['id'] for row in selected_posts if is_shared_board(row['category'])]
        if shared_ids:
            conn.rollback()
            return jsonify({
                'status': 'fail',
                'message': '본부공지사항과 자료실은 처리 상태를 변경하지 않고 확인 현황으로 관리합니다.'
            }), 400
        cursor = conn.execute(f'''
            UPDATE school_posts
            SET status = ?, processor = ?
            WHERE id IN ({placeholders})
        ''', (new_status, current_user, *post_ids))

        if cursor.rowcount == 0:
            conn.rollback()
            return jsonify({'status': 'fail', 'message': '변경할 게시물을 찾을 수 없습니다.'}), 404

        conn.commit()

        return jsonify({
            'status': 'success',
            'processor': current_user,
            'updated_ids': post_ids,
        })
    except Exception as e:
        print(f"상태 업데이트 중 에러 발생: {e}")
        return jsonify({'status': 'error', 'message': '상태 변경 중 오류가 발생했습니다.'}), 500
    finally:
        if conn is not None:
            conn.close()


@school_task_bp.route('/api/move_posts', methods=['POST'])
def move_posts():
    """선택 게시물의 자료는 유지하고 게시판 분류만 대상 분류로 이동한다."""
    conn = None
    try:
        data = request.get_json(silent=True) or {}
        post_ids = normalize_post_ids(data.get('post_ids', []))
        target_category = str(data.get('target_category') or '').strip()
        target = CATEGORY_BY_ID.get(target_category)

        if not post_ids:
            return jsonify({'status': 'fail', 'message': '이동할 게시물을 선택해주세요.'}), 400
        if not target:
            return jsonify({'status': 'fail', 'message': '이동할 게시판을 올바르게 선택해주세요.'}), 400

        conn = get_db()
        ensure_confirmation_schema(conn)
        placeholders = ','.join('?' for _ in post_ids)
        selected_posts = conn.execute(f'''
            SELECT p.id, p.category, p.school_id, s.id AS valid_school_id
            FROM school_posts p
            LEFT JOIN schools s ON s.id = p.school_id
            WHERE p.id IN ({placeholders})
        ''', post_ids).fetchall()
        selected_by_id = {int(row['id']): row for row in selected_posts}
        missing_ids = [post_id for post_id in post_ids if post_id not in selected_by_id]
        if missing_ids:
            return jsonify({
                'status': 'fail',
                'message': '선택한 게시물 중 존재하지 않는 게시물이 있습니다.',
            }), 404

        target_is_shared = is_shared_board(target_category)
        if not target_is_shared:
            invalid_school_posts = [
                row['id'] for row in selected_posts if row['valid_school_id'] is None
            ]
            if invalid_school_posts:
                return jsonify({
                    'status': 'fail',
                    'message': '소속 학교 정보가 없는 게시물은 학교별 게시판으로 이동할 수 없습니다.',
                }), 400

        moved_ids = [
            int(row['id'])
            for row in selected_posts
            if get_mapped_category(row['category']) != target['name']
        ]
        if not moved_ids:
            return jsonify({
                'status': 'fail',
                'message': '선택한 게시물이 이미 대상 게시판에 있습니다.',
            }), 400

        confirmation_reset_ids = [
            int(row['id'])
            for row in selected_posts
            if int(row['id']) in moved_ids
            and is_shared_board(row['category']) != target_is_shared
        ]
        moved_placeholders = ','.join('?' for _ in moved_ids)
        conn.execute(f'''
            UPDATE school_posts
            SET category = ?
            WHERE id IN ({moved_placeholders})
        ''', (target_category, *moved_ids))

        if confirmation_reset_ids:
            confirmation_placeholders = ','.join('?' for _ in confirmation_reset_ids)
            conn.execute(f'''
                DELETE FROM school_post_confirmations
                WHERE post_id IN ({confirmation_placeholders})
            ''', confirmation_reset_ids)

        conn.commit()
        return jsonify({
            'status': 'success',
            'moved_ids': moved_ids,
            'target_category': target_category,
            'target_name': target['name'],
            'confirmation_reset_ids': confirmation_reset_ids,
        })
    except Exception as e:
        if conn is not None:
            conn.rollback()
        print(f"게시물 이동 중 에러 발생: {e}")
        return jsonify({'status': 'error', 'message': '게시물 이동 중 오류가 발생했습니다.'}), 500
    finally:
        if conn is not None:
            conn.close()


@school_task_bp.route('/api/detail/<int:post_id>', methods=['GET'])
def task_detail(post_id):
    """
    3. 테이블 클릭 시 상세 모달 데이터
    """
    try:
        conn = get_db()
        ensure_confirmation_schema(conn)
        ensure_view_count_schema(conn)
        row = conn.execute('''
            SELECT p.*, s.school_name 
            FROM school_posts p
            LEFT JOIN schools s ON p.school_id = s.id
            WHERE p.id = ?
        ''', (post_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({'error': True, 'message': '게시물을 찾을 수 없습니다.'}), 404

        r_dict = dict(row)
        r_dict['view_count'] = increment_view_count(conn, post_id)
        raw_cat = r_dict.get('category', '')
        
        # 상세 모달창에서도 에러가 나지 않도록 스마트 함수 적용
        cat_name = get_mapped_category(raw_cat)
        shared = is_shared_board(raw_cat)
        confirmation_summary = get_confirmation_summary(conn, post_id) if shared else {
            'confirmation_count': 0,
            'confirmations': [],
            'confirmation_names': [],
        }
        conn.close()

        post = {
            'id': r_dict.get('id'),
            'category': cat_name,
            'cat_name': cat_name,
            'title': r_dict.get('title', ''),
            'author': r_dict.get('author', ''),
            'created_at': str(r_dict.get('created_at', ''))[:16] if r_dict.get('created_at') else '',
            'school_name': '전체 센터' if shared else (r_dict.get('school_name') or '알 수 없음'),
            'processor': r_dict.get('processor') or '미지정',
            'status': r_dict.get('status') or '접수',
            'view_count': r_dict.get('view_count', 0),
            'content': r_dict.get('content', ''),
            'filename': r_dict.get('filename', ''),
            'filepath': r_dict.get('filepath', ''),
            'is_shared': shared,
            **confirmation_summary,
        }

        return jsonify(post)
    except Exception as e:
        print(f"상세조회 중 에러 발생: {e}")
        return jsonify({'error': True, 'message': '서버 처리 중 오류가 발생했습니다.'}), 500
