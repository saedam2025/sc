"""센터장 팀별 게시물 조회와 팀장확인 상태를 공통 관리한다."""

TEAM_REVIEW_CATEGORY = 'team_review'
TEAM_REVIEW_STATUS = '팀장확인'
# 팀장 확인 절차가 필요한 게시판만 지정한다. 한글로 저장된 과거 게시물도
# 함께 처리해 기존 데이터에서 상태 표시가 누락되지 않도록 한다.
TEAM_REVIEW_REQUIRED_CATEGORIES = frozenset({
    'work_schedule', '근무표',
    'billing', '청구관련',
})
TEAM_REVIEW_EXCLUDED_CATEGORIES = frozenset({
    'community', '본부공지사항',
    'reference', '자료실',
    'expense', '지출결의서',
    TEAM_REVIEW_CATEGORY, '[팀장전용]',
})


def _table_columns(conn, table_name):
    return {
        row['name'] if hasattr(row, 'keys') else row[1]
        for row in conn.execute(f'PRAGMA table_info({table_name})').fetchall()
    }


def ensure_team_review_schema(conn):
    """기존 DB에도 팀 분류 및 확인자 기록 컬럼을 안전하게 추가한다."""
    changed = False
    user_columns = _table_columns(conn, 'users')
    if user_columns and 'custom_team' not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN custom_team TEXT DEFAULT ''")
        changed = True

    post_columns = _table_columns(conn, 'school_posts')
    additions = (
        ('author_emp_no', "TEXT DEFAULT ''"),
        ('team_reviewer', "TEXT DEFAULT ''"),
        ('team_reviewed_at', 'DATETIME'),
    )
    for column_name, definition in additions:
        if post_columns and column_name not in post_columns:
            conn.execute(
                f'ALTER TABLE school_posts ADD COLUMN {column_name} {definition}'
            )
            changed = True
    if post_columns:
        conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_school_posts_author_emp_no
            ON school_posts(author_emp_no, created_at)
        ''')
    if changed:
        conn.commit()


def category_requires_team_review(category):
    return str(category or '').strip() in TEAM_REVIEW_REQUIRED_CATEGORIES


def get_user_team(conn, emp_no):
    if not str(emp_no or '').strip():
        return ''
    row = conn.execute(
        "SELECT custom_team FROM users WHERE emp_no=? LIMIT 1",
        (str(emp_no).strip(),),
    ).fetchone()
    return str(row['custom_team'] or '').strip() if row else ''


def get_team_leader(conn, emp_no):
    """별도 팀 게시물을 검토할 수 있는 센터장(팀장) 정보를 반환한다."""
    if not str(emp_no or '').strip():
        return None
    user = conn.execute('''
        SELECT emp_no, name, position, level, custom_team
        FROM users
        WHERE emp_no=? AND status='승인'
        LIMIT 1
    ''', (str(emp_no).strip(),)).fetchone()
    if not user:
        return None
    try:
        is_level_seven = int(user['level']) == 7
    except (TypeError, ValueError):
        is_level_seven = False
    position = str(user['position'] or '').replace(' ', '')
    if not is_level_seven or position not in {'센터장(팀장)', '센터장팀장'}:
        return None
    return user


def get_post_author_user(conn, post):
    post_data = dict(post)
    author_emp_no = str(post_data.get('author_emp_no') or '').strip()
    if author_emp_no:
        return conn.execute('''
            SELECT emp_no, name, custom_team, level
            FROM users
            WHERE emp_no=?
            LIMIT 1
        ''', (author_emp_no,)).fetchone()

    author_name = str(post_data.get('author') or '').strip()
    if not author_name:
        return None
    return conn.execute('''
        SELECT emp_no, name, custom_team, level
        FROM users
        WHERE name=?
        ORDER BY CASE WHEN CAST(COALESCE(level, 99) AS INTEGER) IN (7, 8) THEN 0 ELSE 1 END,
                 id ASC
        LIMIT 1
    ''', (author_name,)).fetchone()


def post_requires_team_review(conn, post):
    post_data = dict(post)
    if not category_requires_team_review(post_data.get('category')):
        return False
    author = get_post_author_user(conn, post_data)
    if not author or not str(author['custom_team'] or '').strip():
        return False
    try:
        return int(author['level']) in {7, 8}
    except (TypeError, ValueError):
        return False


def post_matches_team(conn, post, team_name):
    """게시판 종류와 무관하게 같은 별도 팀의 센터장 작성글인지 확인한다."""
    team_name = str(team_name or '').strip()
    if not team_name:
        return False
    author = get_post_author_user(conn, post)
    if not author or str(author['custom_team'] or '').strip() != team_name:
        return False
    try:
        return int(author['level']) in {7, 8}
    except (TypeError, ValueError):
        return False


def build_team_review_post_queries(team_name, search_query=''):
    """같은 별도 팀의 센터장 게시물만 조회하는 목록/카운트 쿼리를 반환한다."""
    excluded = tuple(sorted(TEAM_REVIEW_EXCLUDED_CATEGORIES))
    excluded_placeholders = ','.join('?' for _ in excluded)
    where_clause = f'''
        p.category NOT IN ({excluded_placeholders})
        AND EXISTS (
            SELECT 1
            FROM users AS team_user
            WHERE TRIM(COALESCE(team_user.custom_team, '')) = ?
              AND CAST(COALESCE(team_user.level, 99) AS INTEGER) IN (7, 8)
              AND (
                    (TRIM(COALESCE(p.author_emp_no, '')) <> ''
                     AND team_user.emp_no = p.author_emp_no)
                 OR (TRIM(COALESCE(p.author_emp_no, '')) = ''
                     AND team_user.name = p.author)
              )
        )
    '''
    params = [*excluded, str(team_name or '').strip()]
    if search_query:
        where_clause += '''
            AND (
                p.title LIKE ? OR p.author LIKE ? OR p.content LIKE ?
                OR COALESCE(s.school_name, '') LIKE ?
            )
        '''
        search_value = f'%{search_query}%'
        params.extend([search_value] * 4)
    return (
        f'''SELECT COUNT(*)
            FROM school_posts AS p
            LEFT JOIN schools AS s ON s.id = p.school_id
            WHERE {where_clause}''',
        f'''SELECT p.*, COALESCE(s.school_name, '') AS school_name
            FROM school_posts AS p
            LEFT JOIN schools AS s ON s.id = p.school_id
            WHERE {where_clause}
            ORDER BY p.created_at DESC, p.id DESC
            LIMIT ? OFFSET ?''',
        params,
    )
