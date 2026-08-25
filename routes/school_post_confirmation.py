"""본부공지사항·자료실 게시물의 조직원 확인 기록 도우미."""

SHARED_BOARD_CATEGORIES = {'community', '본부공지사항', 'reference', '자료실'}


def is_shared_board(category):
    return str(category or '').strip() in SHARED_BOARD_CATEGORIES


def ensure_confirmation_schema(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS school_post_confirmations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            user_emp_no TEXT NOT NULL,
            user_name TEXT NOT NULL,
            school_name TEXT DEFAULT '',
            confirmed_at TEXT DEFAULT (datetime('now', 'localtime')),
            UNIQUE(post_id, user_emp_no),
            FOREIGN KEY(post_id) REFERENCES school_posts(id) ON DELETE CASCADE
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_school_post_confirmations_post
        ON school_post_confirmations(post_id, confirmed_at)
    ''')


def ensure_view_count_schema(conn):
    columns = {
        str(row['name'] if hasattr(row, 'keys') else row[1])
        for row in conn.execute('PRAGMA table_info(school_posts)').fetchall()
    }
    if 'view_count' not in columns:
        conn.execute(
            'ALTER TABLE school_posts ADD COLUMN view_count INTEGER NOT NULL DEFAULT 0'
        )


def increment_view_count(conn, post_id):
    ensure_view_count_schema(conn)
    conn.execute('''
        UPDATE school_posts
        SET view_count = COALESCE(view_count, 0) + 1
        WHERE id = ?
    ''', (post_id,))
    row = conn.execute(
        'SELECT COALESCE(view_count, 0) AS view_count FROM school_posts WHERE id = ?',
        (post_id,)
    ).fetchone()
    conn.commit()
    return int(row['view_count'] if row else 0)


def get_confirmation_map(conn, post_ids):
    normalized_ids = []
    for raw_id in post_ids:
        try:
            post_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if post_id > 0 and post_id not in normalized_ids:
            normalized_ids.append(post_id)

    result = {post_id: [] for post_id in normalized_ids}
    if not normalized_ids:
        return result

    placeholders = ','.join('?' for _ in normalized_ids)
    rows = conn.execute(f'''
        WITH assignment_rows AS (
            SELECT TRIM(center_director_id) AS emp_no, school_name
            FROM schools
            WHERE COALESCE(is_active, 1) = 1
              AND TRIM(COALESCE(center_director_id, '')) != ''
            UNION
            SELECT TRIM(center_director_id_2) AS emp_no, school_name
            FROM schools
            WHERE COALESCE(is_active, 1) = 1
              AND TRIM(COALESCE(center_director_id_2, '')) != ''
        ),
        assignments AS (
            SELECT emp_no, GROUP_CONCAT(school_name, ', ') AS assigned_school_names
            FROM assignment_rows
            GROUP BY emp_no
        ),
        user_positions AS (
            SELECT emp_no, MAX(position) AS user_position
            FROM users
            GROUP BY emp_no
        )
        SELECT c.post_id, c.user_emp_no, c.user_name, c.school_name,
               c.confirmed_at, a.assigned_school_names, u.user_position
        FROM school_post_confirmations c
        LEFT JOIN assignments a ON a.emp_no = c.user_emp_no
        LEFT JOIN user_positions u ON u.emp_no = c.user_emp_no
        WHERE c.post_id IN ({placeholders})
        ORDER BY c.confirmed_at ASC, c.user_name ASC
    ''', normalized_ids).fetchall()
    for row in rows:
        item = dict(row)
        assigned_school_names = str(item.pop('assigned_school_names', '') or '').strip()
        user_position = str(item.pop('user_position', '') or '').strip()
        stored_label = str(item.get('school_name') or '').strip()
        organization_label = assigned_school_names or user_position or stored_label
        user_name = str(item.get('user_name') or '').strip()
        item['organization_label'] = organization_label
        item['display_name'] = (
            f'{user_name} ({organization_label})' if organization_label else user_name
        )
        result.setdefault(int(item['post_id']), []).append(item)
    return result


def get_confirmation_summary(conn, post_id):
    confirmations = get_confirmation_map(conn, [post_id]).get(int(post_id), [])
    return {
        'confirmation_count': len(confirmations),
        'confirmations': confirmations,
        'confirmation_names': [item['display_name'] for item in confirmations],
    }
