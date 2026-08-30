from pathlib import Path


template_path = Path(__file__).resolve().parents[1] / 'templates' / 'school_bp.html'
template = template_path.read_text(encoding='utf-8')

assert "const isWeeklyReport = cat === 'weekly_report';" in template
assert "'(        )월   (       )주차 주간업무보고'" in template
assert 'function getWeeklyReportTemplate()' in template

for section in (
    '이번 주 업무',
    '일정 및 주요 이슈',
    '다음 주 계획',
    '본사 협의 및 요청사항',
    '기타 특이사항',
):
    assert f"'{section}'" in template

assert '<tr><td style="${cellStyle}">&nbsp;</td></tr>' in template
assert '? getWeeklyReportTemplate()' in template

print('weekly report write template checks passed')
