import sqlite3
import os
import platform

if platform.system() == 'Windows':
    BASE_DIR = os.getcwd() 
else:
    BASE_DIR = '/mnt/data' if os.path.exists('/mnt/data') else os.getcwd()

DB_FILE = os.path.join(BASE_DIR, 'saedam.db')
GALLERY_ROOT = os.path.join(BASE_DIR, 'gallery')
GALLERY_UPLOADS = os.path.join(GALLERY_ROOT, 'uploads')
GALLERY_THUMBS = os.path.join(GALLERY_ROOT, 'thumbnails')
PROFILE_ROOT = os.path.join(BASE_DIR, 'id')
SCHOOL_UPLOADS = os.path.join(BASE_DIR, 'school_uploads')
AI_MAIL_UPLOADS = os.path.join(BASE_DIR, 'ai_mail_uploads')

def get_db():
    if platform.system() == 'Windows':
        print(f"DEBUG: 현재 연결된 DB 파일 위치 -> {DB_FILE}")
        
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs(GALLERY_UPLOADS, exist_ok=True)
    os.makedirs(GALLERY_THUMBS, exist_ok=True)
    os.makedirs(PROFILE_ROOT, exist_ok=True)
    os.makedirs(SCHOOL_UPLOADS, exist_ok=True)
    os.makedirs(AI_MAIL_UPLOADS, exist_ok=True)
    
    conn = get_db()
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        year TEXT, date TEXT, owner TEXT,
        cat_meeting_title TEXT, cat_meeting_time TEXT,
        cat_interview_title TEXT, cat_interview_time TEXT,
        cat_miting_title TEXT, cat_miting_time TEXT,
        cat_out_title TEXT, cat_out_time TEXT,
        cat_etc_title TEXT, cat_etc_time TEXT,
        note TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner TEXT, type TEXT, start_date TEXT, end_date TEXT, status TEXT,
        approval_id INTEGER
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS daily_attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_no TEXT NOT NULL,
        date TEXT NOT NULL,
        clock_in_time TEXT NOT NULL,
        clock_out_time TEXT,
        status TEXT NOT NULL,
        reason TEXT,
        position TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS board (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT, content TEXT, author TEXT, 
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        filename TEXT, filepath TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_id TEXT,
        sender TEXT, receiver TEXT, content TEXT, 
        sent_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        is_read INTEGER DEFAULT 0,
        filename TEXT, filepath TEXT,
        message_uid TEXT,
        reply_to_uid TEXT,
        edited_at DATETIME,
        deleted_for_all INTEGER DEFAULT 0,
        deleted_at DATETIME
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS chat_rooms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,                 
        created_by TEXT,           
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS chat_members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_id INTEGER NOT NULL,
        emp_no TEXT NOT NULL,      
        joined_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (room_id) REFERENCES chat_rooms(id) ON DELETE CASCADE
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS message_reads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id INTEGER NOT NULL,
        emp_no TEXT NOT NULL,
        is_read INTEGER DEFAULT 0,
        read_at DATETIME,
        FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_no TEXT, name TEXT, password TEXT, position TEXT, level INTEGER,
        rrn TEXT, email TEXT, phone TEXT,
        address TEXT, bank_account TEXT, department TEXT, profile_path TEXT,
        profile_icon TEXT DEFAULT '👤',
        join_date TEXT, retire_date TEXT, status TEXT DEFAULT '대기'
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS contact_center_teams (
        emp_no TEXT PRIMARY KEY,
        team_no INTEGER NOT NULL,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS login_activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_no TEXT,
        user_name TEXT,
        action TEXT NOT NULL,
        ip_address TEXT,
        user_agent TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS usage_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_no TEXT,
        user_name TEXT,
        menu_name TEXT,
        endpoint TEXT,
        path TEXT,
        method TEXT,
        ip_address TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS admin_settings (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS custom_themes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        effect TEXT DEFAULT 'blobs',
        category TEXT DEFAULT 'custom',
        vars_json TEXT NOT NULL,
        enabled INTEGER DEFAULT 1,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS theme_catalog_preferences (
        owner_emp_no TEXT NOT NULL,
        theme_key TEXT NOT NULL,
        is_favorite INTEGER NOT NULL DEFAULT 0,
        is_hidden INTEGER NOT NULL DEFAULT 0,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (owner_emp_no, theme_key)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS approvals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        doc_type TEXT, title TEXT, drafter TEXT,
        approver_1 TEXT, approver_2 TEXT, status TEXT DEFAULT '대기',
        doc_data TEXT, filename TEXT, filepath TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS expense_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        approval_id INTEGER UNIQUE,
        title TEXT,
        drafter TEXT,
        approver_1 TEXT,
        approver_2 TEXT,
        doc_status TEXT DEFAULT '대기',
        payment_status TEXT DEFAULT '결재중',
        total_amount INTEGER DEFAULT 0,
        item_count INTEGER DEFAULT 0,
        report_year TEXT,
        report_month TEXT,
        submitted_at DATETIME,
        approved_at DATETIME,
        paid_at DATETIME,
        paid_by TEXT,
        source_filename TEXT,
        source_filepath TEXT,
        payment_account TEXT,
        memo TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (approval_id) REFERENCES approvals(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS expense_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id INTEGER,
        approval_id INTEGER,
        row_no INTEGER,
        expense_date TEXT,
        category TEXT,
        vendor TEXT,
        description TEXT,
        payment_method TEXT,
        amount INTEGER DEFAULT 0,
        note TEXT,
        raw_json TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (report_id) REFERENCES expense_reports(id),
        FOREIGN KEY (approval_id) REFERENCES approvals(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS ai_mail_workgroups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_emp_no TEXT NOT NULL,
        name TEXT NOT NULL,
        features_json TEXT NOT NULL DEFAULT '{}',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(owner_emp_no, name)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS ai_mail_recipients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        email TEXT NOT NULL COLLATE NOCASE,
        recipient_name TEXT NOT NULL,
        memo TEXT DEFAULT '',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(group_id, email),
        FOREIGN KEY (group_id) REFERENCES ai_mail_workgroups(id) ON DELETE CASCADE
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS ai_mail_senders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_emp_no TEXT NOT NULL,
        label TEXT NOT NULL,
        email TEXT NOT NULL COLLATE NOCASE,
        encrypted_app_password TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        last_tested_at DATETIME,
        last_test_status TEXT,
        last_test_error TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(owner_emp_no, email)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS ai_mail_templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_emp_no TEXT NOT NULL,
        name TEXT NOT NULL,
        subject TEXT NOT NULL,
        body_html TEXT NOT NULL,
        body_text TEXT DEFAULT '',
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(owner_emp_no, name)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS ai_mail_template_assets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        template_id INTEGER NOT NULL,
        original_name TEXT NOT NULL,
        stored_name TEXT NOT NULL,
        filepath TEXT NOT NULL,
        mime_type TEXT NOT NULL,
        content_id TEXT NOT NULL UNIQUE,
        size_bytes INTEGER NOT NULL DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (template_id) REFERENCES ai_mail_templates(id) ON DELETE CASCADE
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS ai_mail_campaigns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_emp_no TEXT NOT NULL,
        group_id INTEGER NOT NULL,
        sender_id INTEGER NOT NULL,
        template_id INTEGER,
        group_name TEXT NOT NULL DEFAULT '',
        sender_label TEXT NOT NULL DEFAULT '',
        sender_email TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL,
        subject TEXT NOT NULL,
        body_html TEXT NOT NULL,
        body_text TEXT DEFAULT '',
        attachment_mode TEXT NOT NULL DEFAULT 'none',
        status TEXT NOT NULL DEFAULT 'staged',
        total_count INTEGER NOT NULL DEFAULT 0,
        processed_count INTEGER NOT NULL DEFAULT 0,
        sent_count INTEGER NOT NULL DEFAULT 0,
        failed_count INTEGER NOT NULL DEFAULT 0,
        cancelled_count INTEGER NOT NULL DEFAULT 0,
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        cancel_requested_at DATETIME,
        cancel_requested_by TEXT,
        cancel_reason TEXT,
        allow_missing_attachment INTEGER NOT NULL DEFAULT 0,
        send_interval REAL NOT NULL DEFAULT 1.0,
        preflight_ok INTEGER NOT NULL DEFAULT 0,
        preflight_json TEXT DEFAULT '{}',
        error_code TEXT,
        error_message TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        queued_at DATETIME,
        started_at DATETIME,
        finished_at DATETIME,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (group_id) REFERENCES ai_mail_workgroups(id),
        FOREIGN KEY (sender_id) REFERENCES ai_mail_senders(id),
        FOREIGN KEY (template_id) REFERENCES ai_mail_templates(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS ai_mail_campaign_recipients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id INTEGER NOT NULL,
        source_recipient_id INTEGER,
        email TEXT NOT NULL COLLATE NOCASE,
        recipient_name TEXT NOT NULL,
        memo TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        attachment_count INTEGER NOT NULL DEFAULT 0,
        attachment_bytes INTEGER NOT NULL DEFAULT 0,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        message_id TEXT,
        error_code TEXT,
        error_message TEXT,
        smtp_response TEXT,
        started_at DATETIME,
        sent_at DATETIME,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(campaign_id, email),
        FOREIGN KEY (campaign_id) REFERENCES ai_mail_campaigns(id) ON DELETE CASCADE,
        FOREIGN KEY (source_recipient_id) REFERENCES ai_mail_recipients(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS ai_mail_campaign_attachments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id INTEGER NOT NULL,
        campaign_recipient_id INTEGER,
        kind TEXT NOT NULL,
        match_method TEXT NOT NULL DEFAULT 'auto',
        match_status TEXT NOT NULL DEFAULT 'pending',
        original_name TEXT NOT NULL,
        stored_name TEXT NOT NULL,
        filepath TEXT NOT NULL,
        mime_type TEXT,
        size_bytes INTEGER NOT NULL DEFAULT 0,
        sha256 TEXT,
        diagnostic TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (campaign_id) REFERENCES ai_mail_campaigns(id) ON DELETE CASCADE,
        FOREIGN KEY (campaign_recipient_id) REFERENCES ai_mail_campaign_recipients(id) ON DELETE SET NULL
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS ai_mail_campaign_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        level TEXT NOT NULL DEFAULT 'info',
        message TEXT NOT NULL,
        details_json TEXT DEFAULT '{}',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (campaign_id) REFERENCES ai_mail_campaigns(id) ON DELETE CASCADE
    )''')

    # 명세서 발송 작업공간: 화면의 임시 배열이 아니라 사용자별로 영구 저장한다.
    c.execute('''CREATE TABLE IF NOT EXISTS payroll_workgroups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_emp_no TEXT NOT NULL,
        name TEXT NOT NULL,
        form_type TEXT NOT NULL DEFAULT 'form_basic',
        subject TEXT NOT NULL,
        body_html TEXT NOT NULL DEFAULT '',
        banner1_data TEXT,
        banner2_data TEXT,
        banner1_asset_id INTEGER,
        banner2_asset_id INTEGER,
        logo_asset_id INTEGER,
        memo TEXT DEFAULT '',
        template_id INTEGER,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(owner_emp_no, name)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS payroll_image_assets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_emp_no TEXT NOT NULL,
        asset_kind TEXT NOT NULL,
        name TEXT NOT NULL,
        source_type TEXT NOT NULL,
        source_value TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(owner_emp_no, asset_kind, name)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS payroll_mail_templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_emp_no TEXT NOT NULL,
        template_key TEXT,
        name TEXT NOT NULL,
        subject TEXT NOT NULL,
        description TEXT DEFAULT '',
        source_filename TEXT,
        match_keywords TEXT,
        body_html TEXT NOT NULL,
        is_system INTEGER NOT NULL DEFAULT 0,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(owner_emp_no, name)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS payroll_campaigns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_emp_no TEXT NOT NULL,
        group_id INTEGER,
        group_name TEXT NOT NULL DEFAULT '',
        sender_id INTEGER,
        sender_email TEXT NOT NULL DEFAULT '',
        subject TEXT NOT NULL DEFAULT '',
        source_filename TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'queued',
        total_count INTEGER NOT NULL DEFAULT 0,
        processed_count INTEGER NOT NULL DEFAULT 0,
        sent_count INTEGER NOT NULL DEFAULT 0,
        failed_count INTEGER NOT NULL DEFAULT 0,
        errors_json TEXT NOT NULL DEFAULT '[]',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        started_at DATETIME,
        finished_at DATETIME,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS payroll_campaign_recipients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id INTEGER NOT NULL,
        owner_emp_no TEXT NOT NULL,
        sheet_name TEXT NOT NULL DEFAULT '',
        excel_row INTEGER,
        recipient_type TEXT NOT NULL DEFAULT '',
        school_name TEXT NOT NULL DEFAULT '',
        recipient_name TEXT NOT NULL DEFAULT '',
        email TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'queued',
        error_message TEXT NOT NULL DEFAULT '',
        started_at DATETIME,
        finished_at DATETIME,
        elapsed_seconds REAL NOT NULL DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (campaign_id) REFERENCES payroll_campaigns(id) ON DELETE CASCADE
    )''')

    c.execute('CREATE INDEX IF NOT EXISTS idx_ai_mail_groups_owner ON ai_mail_workgroups(owner_emp_no, updated_at)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_ai_mail_recipients_group ON ai_mail_recipients(group_id, recipient_name)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_ai_mail_senders_owner ON ai_mail_senders(owner_emp_no, is_active)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_ai_mail_templates_owner ON ai_mail_templates(owner_emp_no, updated_at)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_ai_mail_template_assets_template ON ai_mail_template_assets(template_id, id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_ai_mail_campaigns_owner ON ai_mail_campaigns(owner_emp_no, created_at)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_ai_mail_campaigns_status ON ai_mail_campaigns(owner_emp_no, status, updated_at)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_ai_mail_campaigns_refs ON ai_mail_campaigns(group_id, sender_id, template_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_ai_mail_campaign_recipients_campaign ON ai_mail_campaign_recipients(campaign_id, status)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_ai_mail_campaign_attachments_campaign ON ai_mail_campaign_attachments(campaign_id, kind)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_ai_mail_campaign_attachments_recipient ON ai_mail_campaign_attachments(campaign_recipient_id, match_status)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_ai_mail_campaign_events_campaign ON ai_mail_campaign_events(campaign_id, created_at)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_payroll_groups_owner ON payroll_workgroups(owner_emp_no, updated_at)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_payroll_assets_owner_kind ON payroll_image_assets(owner_emp_no, asset_kind, updated_at)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_payroll_templates_owner ON payroll_mail_templates(owner_emp_no, updated_at)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_payroll_campaigns_owner ON payroll_campaigns(owner_emp_no, created_at)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_payroll_campaign_recipients_campaign ON payroll_campaign_recipients(campaign_id, id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_payroll_campaign_recipients_owner ON payroll_campaign_recipients(owner_emp_no, campaign_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_theme_preferences_owner ON theme_catalog_preferences(owner_emp_no, is_hidden, is_favorite)')

    c.execute('''CREATE TABLE IF NOT EXISTS gallery (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        filename TEXT NOT NULL,
        thumb_name TEXT NOT NULL,
        file_type TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        tab_id INTEGER DEFAULT 1
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS gallery_tabs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS gall2_tabs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS gall2 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        filename TEXT NOT NULL,
        thumb_name TEXT NOT NULL,
        file_type TEXT,
        tab_id INTEGER NOT NULL DEFAULT 1,
        post_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (tab_id) REFERENCES gall2_tabs (id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS gall2_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        content TEXT,
        author TEXT,
        tab_id INTEGER NOT NULL DEFAULT 1,
        upload_token TEXT UNIQUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (tab_id) REFERENCES gall2_tabs (id)
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS schools (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        year TEXT NOT NULL,                
        school_name TEXT NOT NULL,         
        contract_subject TEXT,
        office_phone TEXT,                 
        office_location TEXT,              
        school_address TEXT,
        school_phone TEXT,
        school_email TEXT,
        neulbom_assistant TEXT,            
        neulbom_manager TEXT,              
        center_director_id TEXT,           
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS school_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        school_id INTEGER NOT NULL,        
        category TEXT NOT NULL,            
        title TEXT NOT NULL,
        content TEXT,
        author TEXT,
        filename TEXT,                     
        filepath TEXT,                     
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (school_id) REFERENCES schools (id) ON DELETE CASCADE
    )''')

    # 🚀 [수정] 데이터베이스의 하위 호환성을 완벽히 보장하여 기존 db 파일을 마이그레이션할 때 
    # `is_read` 컬럼이 누락되어 카운트가 비정상 차감되거나 작동하지 않는 현상을 완전히 차단하기 위해 alter 구문에 is_read를 강제 주입했습니다.
    alter_queries = [
        "ALTER TABLE messages ADD COLUMN filename TEXT",
        "ALTER TABLE messages ADD COLUMN filepath TEXT",
        "ALTER TABLE messages ADD COLUMN room_id TEXT", 
        "ALTER TABLE messages ADD COLUMN is_read INTEGER DEFAULT 0",
        "ALTER TABLE messages ADD COLUMN message_uid TEXT",
        "ALTER TABLE messages ADD COLUMN reply_to_uid TEXT",
        "ALTER TABLE messages ADD COLUMN edited_at DATETIME",
        "ALTER TABLE messages ADD COLUMN deleted_for_all INTEGER DEFAULT 0",
        "ALTER TABLE messages ADD COLUMN deleted_at DATETIME",
        "ALTER TABLE daily_attendance ADD COLUMN reason TEXT",
        "ALTER TABLE daily_attendance ADD COLUMN position TEXT",
        "ALTER TABLE gallery ADD COLUMN tab_id INTEGER DEFAULT 1",
        "ALTER TABLE users ADD COLUMN profile_icon TEXT DEFAULT '👤'",
        "ALTER TABLE users ADD COLUMN address TEXT",
        "ALTER TABLE users ADD COLUMN bank_account TEXT",
        "ALTER TABLE users ADD COLUMN department TEXT",
        "ALTER TABLE users ADD COLUMN profile_path TEXT",
        "ALTER TABLE approvals ADD COLUMN receivers TEXT DEFAULT ''",
        "ALTER TABLE approvals ADD COLUMN cc_receivers TEXT DEFAULT ''",
        "ALTER TABLE approvals ADD COLUMN filesize TEXT DEFAULT ''",
        "ALTER TABLE attendance ADD COLUMN approval_id INTEGER",
        "ALTER TABLE expense_reports ADD COLUMN memo TEXT",
        "ALTER TABLE expense_reports ADD COLUMN payment_account TEXT",
        "ALTER TABLE ai_mail_campaigns ADD COLUMN group_name TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE ai_mail_campaigns ADD COLUMN sender_label TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE ai_mail_campaigns ADD COLUMN sender_email TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE ai_mail_campaigns ADD COLUMN cancel_requested_at DATETIME",
        "ALTER TABLE ai_mail_campaigns ADD COLUMN cancel_requested_by TEXT",
        "ALTER TABLE ai_mail_campaigns ADD COLUMN cancel_reason TEXT",
        "ALTER TABLE ai_mail_campaigns ADD COLUMN allow_missing_attachment INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE payroll_mail_templates ADD COLUMN template_key TEXT",
        "ALTER TABLE payroll_mail_templates ADD COLUMN description TEXT DEFAULT ''",
        "ALTER TABLE payroll_mail_templates ADD COLUMN source_filename TEXT",
        "ALTER TABLE payroll_mail_templates ADD COLUMN is_system INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE payroll_mail_templates ADD COLUMN match_keywords TEXT",
        "ALTER TABLE payroll_campaign_recipients ADD COLUMN school_name TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE payroll_workgroups ADD COLUMN banner1_asset_id INTEGER",
        "ALTER TABLE payroll_workgroups ADD COLUMN banner2_asset_id INTEGER",
        "ALTER TABLE payroll_workgroups ADD COLUMN logo_asset_id INTEGER",
        "ALTER TABLE custom_themes ADD COLUMN category TEXT DEFAULT 'custom'",
        "ALTER TABLE custom_themes ADD COLUMN updated_at DATETIME DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE schools ADD COLUMN contract_subject TEXT",
        "ALTER TABLE schools ADD COLUMN school_address TEXT",
        "ALTER TABLE schools ADD COLUMN school_phone TEXT",
        "ALTER TABLE schools ADD COLUMN school_email TEXT",
        "ALTER TABLE gall2 ADD COLUMN post_id INTEGER"
    ]
    
    for q in alter_queries:
        try:
            c.execute(q)
        except sqlite3.OperationalError:
            pass 

    c.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_approval_id
        ON attendance(approval_id)
        WHERE approval_id IS NOT NULL
    ''')

    tabs_count = c.execute("SELECT count(*) FROM gallery_tabs").fetchone()[0]
    if tabs_count == 0:
        c.execute("INSERT INTO gallery_tabs (id, name) VALUES (1, '기본 갤러리')")

    gall2_tabs_count = c.execute("SELECT count(*) FROM gall2_tabs").fetchone()[0]
    if gall2_tabs_count == 0:
        c.execute("INSERT INTO gall2_tabs (id, name) VALUES (1, '기본 갤러리 2')")

    conn.commit()
    conn.close()
    print("DATABASE INITIALIZED SUCCESSFULLY")

if __name__ == "__main__":
    init_db()
