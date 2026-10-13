"""DOCX extractor: body paragraphs and tables in actual document order.

Paragraphs and table cells carry precise locators. Comments, revisions, text
boxes, headers and footers are NOT extracted and the report says so.
"""
from __future__ import annotations
from pathlib import Path
from . import make_block, normalize, version_of

def extract_docx(path, source_sha256):
    import docx
    document = docx.Document(str(path))
    version = version_of('python-docx')
    blocks = []
    body = document.element.body
    para_index, table_index = 0, 0
    for child in body.iterchildren():
        tag = child.tag.split('}')[-1]
        if tag == 'p':
            paragraph = docx.text.paragraph.Paragraph(child, document)
            text = normalize(paragraph.text)
            block = make_block(source_sha256, 'cch-docx', version,
                               {'kind': 'docx_paragraph', 'index': para_index}, text,
                               role='heading' if paragraph.style.name.startswith('Heading') else 'unknown')
            if block:
                blocks.append(block)
            para_index += 1
        elif tag == 'tbl':
            table = docx.table.Table(child, document)
            header = [normalize(c.text).strip() for c in table.rows[0].cells] if table.rows else []
            for r, row in enumerate(table.rows):
                for c, cell in enumerate(row.cells):
                    block = make_block(source_sha256, 'cch-docx', version,
                                       {'kind': 'docx_table_cell', 'table': table_index, 'row': r, 'column': c},
                                       normalize(cell.text),
                                       context={'table_header': header} if header and r > 0 else None)
                    if block:
                        blocks.append(block)
            table_index += 1
    report = {'media_type': 'application/docx', 'extractor': {'name': 'cch-docx', 'version': version},
              'blocks': len(blocks), 'paragraphs': para_index, 'tables': table_index,
              'covered': ['body paragraphs in document order', 'table cells with table/row/column locators'],
              'not_covered': ['comments', 'tracked revisions', 'text boxes', 'headers/footers', 'embedded objects']}
    return blocks, report
