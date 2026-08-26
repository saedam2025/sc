(function () {
    'use strict';

    function getEditor(editorId) {
        return window.tinymce ? window.tinymce.get(editorId) : null;
    }

    function selectedCells(editor) {
        var cells = Array.from(editor.getDoc().querySelectorAll('td[data-mce-selected], th[data-mce-selected]'));
        if (cells.length) return cells;
        var node = editor.selection.getNode();
        var cell = node && node.closest ? node.closest('td, th') : null;
        return cell ? [cell] : [];
    }

    function selectedTable(editor) {
        var node = editor.selection.getNode();
        return node && node.closest ? node.closest('table') : null;
    }

    function normalizedSize(value) {
        if (value === null) return null;
        var trimmed = String(value).trim();
        if (!trimmed) return null;
        if (/^\d+(\.\d+)?$/.test(trimmed)) return trimmed + 'px';
        if (/^\d+(\.\d+)?(px|%|em|rem|cm|mm)$/i.test(trimmed)) return trimmed;
        window.alert('숫자 또는 px, %, cm 같은 단위를 입력해주세요. 예: 120, 30%, 3cm');
        return null;
    }

    function applyTablePadding(editor, padding) {
        var table = selectedTable(editor);
        if (!table) return window.alert('간격을 조정할 표 안에 커서를 놓고 다시 눌러주세요.');
        editor.undoManager.transact(function () {
            table.querySelectorAll('td, th').forEach(function (cell) {
                cell.style.padding = padding;
                cell.style.lineHeight = '1.35';
            });
            table.style.marginTop = '8px';
            table.style.marginBottom = '8px';
        });
        editor.nodeChanged();
    }

    function setCellSize(editor, property) {
        var cells = selectedCells(editor);
        if (!cells.length) return window.alert('표 셀 안에 커서를 놓고 다시 눌러주세요.');
        var first = cells[0];
        var current = property === 'height'
            ? (first.style.height || first.parentElement.style.height || '')
            : (first.style.width || '');
        var label = property === 'height' ? '높이' : '폭';
        var example = property === 'height' ? '40, 1.5cm' : '120, 30%, 3cm';
        var size = normalizedSize(window.prompt('선택한 셀/행의 ' + label + '을 입력하세요. 예: ' + example, current));
        if (!size) return;
        editor.undoManager.transact(function () {
            cells.forEach(function (cell) {
                cell.style[property] = size;
                if (property === 'height') cell.parentElement.style.height = size;
                if (property === 'width') {
                    var table = cell.closest('table');
                    if (table) table.style.tableLayout = 'fixed';
                }
            });
        });
        editor.nodeChanged();
    }

    function setTableSize(editor, property) {
        var table = selectedTable(editor);
        if (!table) return window.alert('크기를 조정할 표 안에 커서를 놓고 다시 눌러주세요.');
        var label = property === 'height' ? '높이' : '너비';
        var example = property === 'height' ? '240, 8cm' : '600, 80%, 16cm';
        var current = table.style[property] || (property === 'width' ? table.getAttribute('width') : '') || '';
        var size = normalizedSize(window.prompt('표 전체 ' + label + '를 입력하세요. 예: ' + example, current));
        if (!size) return;
        editor.undoManager.transact(function () {
            table.style[property] = size;
            if (property === 'width') {
                table.style.tableLayout = 'fixed';
                table.removeAttribute('width');
            }
        });
        editor.nodeChanged();
    }

    function init(editorId) {
        var textarea = document.getElementById(editorId);
        if (!textarea || !window.tinymce || getEditor(editorId)) return Promise.resolve(getEditor(editorId));
        var initialContent = textarea.value || '';
        return window.tinymce.init({
            selector: '#' + editorId,
            base_url: 'https://cdn.jsdelivr.net/npm/tinymce@8',
            suffix: '.min',
            license_key: 'gpl',
            height: window.innerWidth <= 720 ? 420 : 500,
            menubar: false,
            statusbar: false,
            branding: false,
            promotion: false,
            placeholder: '내용을 입력하세요...',
            plugins: 'advlist autolink lists link image noneditable table',
            toolbar_mode: 'sliding',
            toolbar_sticky: true,
            toolbar: 'undo redo | fontfamily fontsize lineheight | bold italic underline forecolor backcolor removeformat | alignleft aligncenter alignright bullist numlist outdent indent | link image | table tableprops tablerowprops tablecellprops tablecellbackgroundcolor tablecellbordercolor tablecellborderwidth tablecellborderstyle tableinsertrowafter tableinsertcolafter tabledeleterow tabledeletecol tablemergecells tablesplitcells tabledelete tabletuning',
            font_family_formats: '맑은 고딕=Malgun Gothic,맑은 고딕,sans-serif;굴림=Gulim,굴림,sans-serif;돋움=Dotum,돋움,sans-serif;바탕=Batang,바탕,serif;궁서=Gungsuh,궁서,serif;Arial=arial,helvetica,sans-serif;Arial Black=arial black,avant garde;Courier New=courier new,courier,monospace;Helvetica=helvetica,arial,sans-serif;Times New Roman=times new roman,times,serif;Verdana=verdana,geneva,sans-serif',
            font_size_formats: '10px 11px 12px 13px 14px 16px 18px 20px 24px 28px 36px',
            line_height_formats: '1 1.15 1.3 1.5 1.8 2',
            table_advtab: true,
            table_cell_advtab: true,
            table_row_advtab: true,
            table_appearance_options: true,
            table_style_by_css: true,
            table_resize_bars: true,
            table_column_resizing: 'resizetable',
            table_sizing_mode: 'fixed',
            object_resizing: 'img,table',
            table_default_attributes: {border: '1'},
            table_default_styles: {width: '100%', 'border-collapse': 'collapse'},
            contextmenu: 'table',
            table_toolbar: 'tableprops tablecellprops tablerowprops | tablecellbackgroundcolor tablecellbordercolor tablecellborderwidth tablecellborderstyle | tablemergecells tablesplitcells | tableinsertrowbefore tableinsertrowafter tabledeleterow | tableinsertcolbefore tableinsertcolafter tabledeletecol | tabledelete tabletuning',
            content_style: 'body{font-family:"Malgun Gothic","맑은 고딕",sans-serif;font-size:14px;line-height:1.5;color:#0f172a;padding:10px}p{margin:0 0 6px;line-height:1.5}img{max-width:100%;height:auto}table{border-collapse:collapse;width:100%;table-layout:fixed;margin:8px 0 15px}td,th{border:1px solid #cbd5e1;padding:6px 8px;vertical-align:top;word-break:break-all}',
            setup: function (editor) {
                editor.ui.registry.addMenuButton('tabletuning', {
                    text: '표 조정',
                    tooltip: '표 간격과 셀 크기 조정',
                    fetch: function (callback) {
                        callback([
                            {type: 'menuitem', text: '표 간격 좁게', onAction: function () { applyTablePadding(editor, '4px 6px'); }},
                            {type: 'menuitem', text: '표 간격 기본', onAction: function () { applyTablePadding(editor, '8px 10px'); }},
                            {type: 'separator'},
                            {type: 'menuitem', text: '표 전체 너비 지정', onAction: function () { setTableSize(editor, 'width'); }},
                            {type: 'menuitem', text: '표 전체 높이 지정', onAction: function () { setTableSize(editor, 'height'); }},
                            {type: 'separator'},
                            {type: 'menuitem', text: '선택 셀/열 폭 지정', onAction: function () { setCellSize(editor, 'width'); }},
                            {type: 'menuitem', text: '선택 셀/행 높이 지정', onAction: function () { setCellSize(editor, 'height'); }}
                        ]);
                    }
                });
                editor.on('init', function () {
                    editor.setContent(initialContent);
                });
            }
        });
    }

    function getContent(editorId) {
        var editor = getEditor(editorId);
        var textarea = document.getElementById(editorId);
        return editor && editor.initialized ? editor.getContent() : (textarea ? textarea.value : '');
    }

    function hasContent(html) {
        var container = document.createElement('div');
        container.innerHTML = String(html || '');
        return Boolean(container.textContent.trim() || container.querySelector('img, table, hr'));
    }

    window.GeneratedBoardEditor = {
        init: init,
        getContent: getContent,
        hasContent: hasContent
    };
}());
