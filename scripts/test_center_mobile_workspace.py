from pathlib import Path


template_path = Path(__file__).resolve().parents[1] / 'templates' / 'school_bp.html'
template = template_path.read_text(encoding='utf-8')

assert '/* 센터장 전용 업무공간 모바일 홈 */' in template
assert '.school-detail-page .navbar { height: 62px; padding: 0 10px; }' in template
assert '.school-detail-page .menu-item { width: 38px; height: 38px;' in template
assert '.school-detail-page .user-section { display: none; }' in template
assert '.school-detail-page .content-container { padding: 4.8px 10px 24px !important;' in template
assert '.school-left-rail, .school-main-rail, .school-side-rail { display: contents; }' in template
assert '.school-profile-card { order: 1; }' in template
assert '.board-section { order: 2; }' in template
assert '.school-calendar-card { order: 3; }' in template
assert '.school-link-card { order: 4; }' in template
assert '.school-chat-card { order: 5; }' in template
assert '.company-gallery-card { order: 6; }' in template
assert 'table.board-table { width: 100%; min-width: 0;' in template
assert 'table.board-table .board-col-number,' in template
assert 'class="board-col-title"' in template
assert 'class="board-mobile-meta">{{ p.author }} · {{ p.created_at[:10] }}</span>' in template
assert '#boardDropdownMenu > div { grid-template-columns: 1fr !important;' in template
assert '#postFormTitle { padding-right: 36px; font-size: 1.18rem !important;' in template
assert '.read-title { font-size:1.08rem; line-height:1.4; }' in template
assert '.read-meta { flex-wrap:wrap; row-gap:8px; white-space:normal; }' in template
assert '.comment-file-picker { flex:1 1 100%; }' in template
assert '@media (max-width: 420px)' in template

print('center mobile workspace checks passed')
