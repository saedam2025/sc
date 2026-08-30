"""스마트 공문발송 기본 화면의 라우트·로그인·렌더링 회귀 테스트."""

import os
import io
import re
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import app
from routes import smart_document


def main():
    client = app.test_client()

    anonymous = client.get('/smart-document')
    assert anonymous.status_code == 302
    assert anonymous.headers.get('Location', '').endswith('/login_page')

    with client.session_transaction() as login_session:
        login_session['emp_no'] = 'admin'
        login_session['user_name'] = 'admin'
        login_session['user_level'] = 1

    page = client.get('/smart-document')
    assert page.status_code == 200
    page_html = page.get_data(as_text=True)
    assert '스마트 공문발송'.encode() in page.data
    assert 'AI를 이용하여 공문을 쉽고 빠르게 작성할 수 있습니다.'.encode() in page.data
    assert 'AI 참고자료'.encode() in page.data
    assert '메일 붙임파일'.encode() in page.data
    assert '스마트명세서 발송계정 메뉴'.encode() in page.data
    assert b'<select id="openAiModel">' in page.data
    for model_name in (b'gpt-5.6-luna', b'gpt-5.6-terra', b'gpt-5.6-sol'):
        assert model_name in page.data
    assert b'id="pdfDocumentButton"' in page.data
    assert b'/smart-document' in page.data
    approval_menu = re.search(
        r'class="menu-item\s*([^\"]*)"[^>]*>\s*<i[^>]*fa-file-signature[^>]*></i>\s*사내결재',
        page_html,
    )
    assert approval_menu and 'active' not in approval_menu.group(1).split()
    assert client.get('/static/css/smart_document.css').status_code == 200
    smart_document_js = client.get('/static/js/smart_document.js')
    assert smart_document_js.status_code == 200
    smart_document_js_text = smart_document_js.get_data(as_text=True)
    assert 'buildEmailDocumentSubject(currentDocument)' in smart_document_js_text
    assert "replace(/요청\\s*$/, '공문')" in smart_document_js_text
    assert "`[${currentDocument.sender_company" not in smart_document_js_text

    numbered_html, numbered_text = smart_document._document_email_content({
        'body': ['1. 첫 번째 내용', '2. 두 번째 내용', '가. 두 번째 항목의 세부 내용', '번호 없는 내용'],
    })
    assert '<b>1.</b> 1.' not in numbered_html
    assert '<b>1.</b> 첫 번째 내용' in numbered_html
    assert 'margin-left:30px;' in numbered_html
    assert '  가. 두 번째 항목의 세부 내용' in numbered_text
    assert '3. 번호 없는 내용' in numbered_text
    table_html, _ = smart_document._document_email_content({
        'body': ['| 구분 | 내용 |\n|---|---|\n| 프로그램 | 방과후 영어교육 |'],
        'tables': [{'title': '강사 경력', 'headers': ['기관', '기간'], 'rows': [['새담', '2025년']]}],
    })
    assert '<table' in table_html
    assert '| 프로그램 |' not in table_html
    assert '강사 경력' in table_html
    assert '발송일' in table_html
    assert '귀교의 발전을 기원합니다.' not in smart_document._document_email_content({
        'greeting': '귀교의 발전을 기원합니다.', 'body': ['1. 안내드립니다.'],
    })[0]
    normalized = smart_document._normalize_document_content({
        'sender_company': '주식회사 새담',
        'body': ['1. 안내드립니다.', '끝.', '주식회사 새담 대표자 홍길동 사업자번호 123 주소: 서울 발송일 2026-08-29'],
        'tables': [
            {'title': '파견 세부내용', 'headers': ['구분', '내용'], 'rows': [
                ['파견 분야', '영어'], ['파견 장소', '확인 필요'], ['파견일', '2026-09-01'],
                ['목적', '수업 공백 방지'], ['대상', '초등학생'], ['기간', '1개월'],
            ]},
            {'title': '강사 세부사항', 'headers': ['이름', '경력'], 'rows': [['김아영', '영어교육 경력']]},
        ],
        'closing': '끝.',
    })
    assert normalized['body'] == ['1. 안내드립니다.']
    assert [table['title'] for table in normalized['tables']] == ['강사 세부사항', '파견 세부내용']
    assert len(normalized['tables'][1]['rows']) == 4
    assert all('확인 필요' not in ' '.join(row) for table in normalized['tables'] for row in table['rows'])
    normalized_markup = smart_document._official_document_markup({
        **normalized, 'document_number': 'TEST-1', 'dispatch_date': '2026-08-29',
        'recipient': '초록초등학교장', 'representative': '홍길동', 'subject': '강사 파견',
        'company_address': '서울시 테스트로 1', 'contact': '02-000-0000',
    })
    assert normalized_markup.count('끝.') == 1
    assert normalized_markup.index('강사 세부사항') < normalized_markup.index('파견 세부내용') < normalized_markup.index('끝.')
    assert normalized_markup.count('서울시 테스트로 1') == 1

    from routes import payroll
    original_payroll_login = payroll._smtp_login_for_sender
    original_verify_sender = payroll._verify_smtp_sender
    sent_messages = []

    class FakeSmtp:
        def send_message(self, message, **kwargs):
            sent_messages.append((message, kwargs))

        def quit(self):
            pass

        def close(self):
            pass

    try:
        payroll._smtp_login_for_sender = lambda sender: FakeSmtp()
        payroll._verify_smtp_sender = lambda smtp, sender: None
        smart_document._send_document_email(
            {
                'label': '스마트명세서 계정', 'email': 'sender@saedam.org',
                'provider': 'zeptomail', 'encrypted_app_password': 'encrypted',
            },
            'school@example.com', '공문 발송', {'body': ['안내드립니다.']},
            attachments=[{
                'filename': '붙임.pdf', 'mime': 'application/pdf', 'data': b'%PDF-1.4 test',
            }],
        )
        assert len(sent_messages) == 1
        sent_message = sent_messages[0][0]
        assert sent_message['From'] == '스마트명세서 계정 <sender@saedam.org>'
        assert any(part.get_filename() == '붙임.pdf' for part in sent_message.walk())
    finally:
        payroll._smtp_login_for_sender = original_payroll_login
        payroll._verify_smtp_sender = original_verify_sender

    original_get_db = smart_document.get_db
    original_secret_loader = smart_document.load_credential_secret
    original_test_connection = smart_document._test_connection
    original_create_document = smart_document._create_openai_document
    original_send_document_email = smart_document._send_document_email
    original_render_document_pdf = smart_document._render_document_pdf
    original_environment_key = os.environ.get('OPENAI_API_KEY')
    original_environment_model = os.environ.get('OPENAI_MODEL')
    tested = {}
    with tempfile.TemporaryDirectory(prefix='smart-document-settings-') as temp_dir:
        database_path = Path(temp_dir) / 'settings.db'

        def test_db():
            connection = sqlite3.connect(database_path)
            connection.row_factory = sqlite3.Row
            return connection

        def fake_connection(api_key, model):
            tested['api_key'] = api_key
            tested['model'] = model
            return model

        def fake_document(api_key, model, user_prompt, attachments, company, template, recipient):
            tested['generation_api_key'] = api_key
            tested['generation_model'] = model
            tested['generation_prompt'] = user_prompt
            tested['generation_attachments'] = attachments
            tested['generation_company'] = company['name']
            tested['generation_template'] = template['name']
            return ({
                'title': '공 문',
                'document_number': '새담-2026-001',
                'date': '2026년 08월 29일',
                'recipient': '초록초등학교장',
                'sender': '새담',
                'subject': '강사 파견 안내',
                'greeting': '귀교의 발전을 기원합니다.',
                'body': ['강사 파견 내용을 안내드립니다.'],
                'attachments': [],
                'contact': '확인 필요',
                'closing': '끝.',
                'assignment_start': '2026-09-01',
                'assignment_end': '2026-09-30',
                'source_facts': ['이력서에서 강사 경력을 확인함'],
                'attachment_references': [],
            }, {'input_tokens': 10, 'output_tokens': 20, 'total_tokens': 30})

        def fake_send_document_email(sender, recipient_email, subject, document, seal_data=None, seal_mime='', attachments=None):
            tested['email_sender'] = sender['email']
            tested['email_recipient'] = recipient_email
            tested['email_subject'] = subject
            tested['email_document_number'] = document['document_number']
            tested['email_attachments'] = attachments or []

        try:
            smart_document.get_db = test_db
            smart_document.load_credential_secret = lambda: 'smart-document-test-secret'
            smart_document._test_connection = fake_connection
            smart_document._create_openai_document = fake_document
            smart_document._send_document_email = fake_send_document_email
            smart_document._render_document_pdf = lambda markup: b'%PDF-1.4 smart-document-test'
            os.environ['OPENAI_API_KEY'] = 'sk-env-' + ('e' * 32)
            os.environ['OPENAI_MODEL'] = 'gpt-5.6-terra'

            bootstrap = client.get('/smart-document/api/settings')
            assert bootstrap.status_code == 200
            bootstrap_data = bootstrap.get_json()
            csrf_token = bootstrap_data['csrf_token']
            assert bootstrap_data['settings']['source'] == 'environment'
            assert bootstrap_data['settings']['model'] == 'gpt-5.6-terra'

            missing_csrf = client.put('/smart-document/api/settings', json={
                'api_key': 'sk-menu-' + ('m' * 32),
                'model': 'gpt-5.6-sol',
            })
            assert missing_csrf.status_code == 403

            menu_key = 'sk-menu-' + ('m' * 32)
            saved = client.put(
                '/smart-document/api/settings',
                headers={'X-CSRF-Token': csrf_token},
                json={'api_key': menu_key, 'model': 'gpt-5.6-sol'},
            )
            assert saved.status_code == 200
            saved_settings = saved.get_json()['settings']
            assert saved_settings['source'] == 'menu'
            assert saved_settings['model'] == 'gpt-5.6-sol'

            connection = test_db()
            stored = connection.execute(
                'SELECT api_key_encrypted FROM smart_document_ai_settings WHERE owner_emp_no=?',
                ('admin',),
            ).fetchone()['api_key_encrypted']
            connection.close()
            assert menu_key not in stored

            tested_response = client.post(
                '/smart-document/api/settings/test',
                headers={'X-CSRF-Token': csrf_token},
                json={'model': 'gpt-5.6-sol'},
            )
            assert tested_response.status_code == 200
            assert tested['api_key'] == menu_key
            assert tested['model'] == 'gpt-5.6-sol'

            company_response = client.post(
                '/smart-document/api/companies',
                headers={'X-CSRF-Token': csrf_token},
                json={
                    'name': '주식회사 새담',
                    'representative': '홍길동',
                    'document_prefix': '새담',
                    'is_default': True,
                },
            )
            assert company_response.status_code == 200
            company_id = company_response.get_json()['company']['id']
            workspace = client.get('/smart-document/api/workspace').get_json()
            template_id = workspace['templates'][0]['id']

            document_buffer = io.BytesIO()
            with zipfile.ZipFile(document_buffer, 'w') as archive:
                archive.writestr(
                    'word/document.xml',
                    '<w:document xmlns:w="urn:test"><w:body><w:p><w:r><w:t>'
                    '김강사 경력 10년, 파견기간 2026-09-01부터 2026-09-30까지'
                    '</w:t></w:r></w:p></w:body></w:document>',
                )
            document_buffer.seek(0)

            generated = client.post(
                '/smart-document/api/generate',
                headers={'X-CSRF-Token': csrf_token},
                data={
                    'prompt': '오늘 날짜로 초록초등학교 강사파견 공문 만들어줘.',
                    'company_id': str(company_id),
                    'template_id': str(template_id),
                    'reference_files': (document_buffer, '강사이력서.docx'),
                    'delivery_files': (io.BytesIO(b'%PDF-1.4 attached official document'), '강사경력증명서.pdf'),
                },
                content_type='multipart/form-data',
            )
            assert generated.status_code == 200
            generated_data = generated.get_json()
            assert generated_data['document']['subject'] == '강사 파견 안내'
            assert generated_data['usage']['total_tokens'] == 30
            assert tested['generation_api_key'] == menu_key
            assert tested['generation_model'] == 'gpt-5.6-sol'
            assert tested['generation_company'] == '주식회사 새담'
            assert '김강사 경력 10년' in tested['generation_attachments'][0]['text']
            assert generated_data['document']['document_number'].startswith('새담-')
            assert generated_data['document']['sender_company'] == '주식회사 새담'
            assert generated_data['document']['representative'] == '홍길동'
            assert generated_data['document']['attachments'] == ['강사경력증명서.pdf']
            assert generated_data['document']['reference_files'] == ['강사이력서.docx']

            history_id = generated_data['history_id']
            updated = client.patch(
                f'/smart-document/api/history/{history_id}',
                headers={'X-CSRF-Token': csrf_token},
                json={
                    'recipient': '초록초등학교장',
                    'subject': '수정된 강사 파견 안내',
                    'assignment_start': '2026-09-02',
                    'assignment_end': '2026-09-30',
                    'greeting': '귀교의 발전을 기원합니다.',
                    'body': ['수정된 강사 파견 내용을 안내드립니다.'],
                    'closing': '끝.',
                    'contact': '031-000-0000',
                },
            )
            assert updated.status_code == 200
            assert updated.get_json()['document']['subject'] == '수정된 강사 파견 안내'
            assert '귀교의 발전을 기원합니다.' not in updated.get_json()['rendered_html']

            connection = test_db()
            connection.execute('''
                CREATE TABLE ai_mail_senders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_emp_no TEXT NOT NULL,
                    label TEXT NOT NULL,
                    email TEXT NOT NULL,
                    encrypted_app_password TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    last_test_status TEXT,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            sender_id = connection.execute('''
                INSERT INTO ai_mail_senders (owner_emp_no, label, email, encrypted_app_password)
                VALUES ('admin', '테스트 발송자', 'sender@example.com', 'encrypted')
            ''').lastrowid
            connection.commit()
            connection.close()
            emailed = client.post(
                f'/smart-document/api/history/{history_id}/send-email',
                headers={'X-CSRF-Token': csrf_token},
                json={
                    'sender_id': sender_id,
                    'recipient_email': 'school@example.com',
                    'subject': '[주식회사 새담] 수정된 강사 파견 안내',
                },
            )
            assert emailed.status_code == 200
            assert emailed.get_json()['attachment_count'] == 1
            assert tested['email_sender'] == 'sender@example.com'
            assert tested['email_recipient'] == 'school@example.com'
            assert tested['email_document_number'].startswith('새담-')
            assert tested['email_attachments'][0]['filename'] == '강사경력증명서.pdf'

            sent_history = client.get(f'/smart-document/api/history/{history_id}?sent=1')
            assert sent_history.status_code == 200
            sent_payload = sent_history.get_json()['history']
            assert sent_payload['view_mode'] == 'sent'
            assert sent_payload['delivery_id'] == emailed.get_json()['delivery_id']
            assert '수정된 강사 파견 내용을 안내드립니다.' in sent_payload['rendered_html']
            assert '귀교의 발전을 기원합니다.' not in sent_payload['rendered_html']

            history_workspace = client.get('/smart-document/api/workspace').get_json()
            history_summary = next(item for item in history_workspace['history'] if item['id'] == history_id)
            assert history_summary['sent_count'] == 1

            pdf_response = client.get(f'/smart-document/api/history/{history_id}/pdf?sent=1')
            assert pdf_response.status_code == 200
            assert pdf_response.mimetype == 'application/pdf'
            assert pdf_response.data.startswith(b'%PDF-1.4')

            cleared = client.put(
                '/smart-document/api/settings',
                headers={'X-CSRF-Token': csrf_token},
                json={'clear_key': True, 'model': 'gpt-5.6-sol'},
            )
            assert cleared.status_code == 200
            assert cleared.get_json()['settings']['source'] == 'environment'

            os.environ.pop('OPENAI_API_KEY', None)
            missing_key = client.post(
                '/smart-document/api/generate',
                headers={'X-CSRF-Token': csrf_token},
                data={'prompt': '공문을 작성해줘.'},
            )
            assert missing_key.status_code == 400
            assert missing_key.get_json()['code'] == 'OPENAI_NOT_CONFIGURED'

            deleted = client.delete(
                f'/smart-document/api/history/{history_id}',
                headers={'X-CSRF-Token': csrf_token},
            )
            assert deleted.status_code == 200
            assert client.get(f'/smart-document/api/history/{history_id}').status_code == 404
            connection = test_db()
            assert connection.execute('SELECT COUNT(*) FROM smart_document_deliveries WHERE history_id=?', (history_id,)).fetchone()[0] == 0
            assert connection.execute('SELECT COUNT(*) FROM smart_document_attachments WHERE history_id=?', (history_id,)).fetchone()[0] == 0
            connection.close()
        finally:
            smart_document.get_db = original_get_db
            smart_document.load_credential_secret = original_secret_loader
            smart_document._test_connection = original_test_connection
            smart_document._create_openai_document = original_create_document
            smart_document._send_document_email = original_send_document_email
            smart_document._render_document_pdf = original_render_document_pdf
            if original_environment_key is None:
                os.environ.pop('OPENAI_API_KEY', None)
            else:
                os.environ['OPENAI_API_KEY'] = original_environment_key
            if original_environment_model is None:
                os.environ.pop('OPENAI_MODEL', None)
            else:
                os.environ['OPENAI_MODEL'] = original_environment_model
    print('smart document page regression: ok')


if __name__ == '__main__':
    main()
