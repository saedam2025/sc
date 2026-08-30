from pathlib import Path


template_path = Path(__file__).resolve().parents[1] / 'templates' / 'school_bp.html'
template = template_path.read_text(encoding='utf-8')

assert "const isItemRequest = cat === 'item_request';" in template
assert 'function getItemRequestTemplate()' in template
assert 'Array.from({ length: 5 }' in template
assert '>요청물품</th>' in template
assert '>사용처</th>' in template
assert '>비고</th>' in template
assert '<col style="width:28%;">' in template
assert '<col style="width:47%;">' in template
assert '<col style="width:25%;">' in template
assert template.count('${cellStyle}height:27px;') == 3
assert '${cellStyle}height:54px;' not in template
assert "(isItemRequest ? getItemRequestTemplate() : '')" in template

print('item request write template checks passed')
