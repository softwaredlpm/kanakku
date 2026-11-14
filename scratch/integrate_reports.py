"""Wire report snippets (scratch/report_ui/<group>.html) into BusyMobileWeb/index.html.

Idempotent: each group's views, script and tile card live between BEGIN/END markers and are replaced on every run.
Anchors in index.html: <!-- REPORTS:VIEWS -->, <!-- REPORTS:TILES -->, and </body> for scripts.
After running: node scratch/validate_syntax.js.
"""
import html
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, 'BusyMobileWeb', 'index.html')
UI_DIR = os.path.join(ROOT, 'scratch', 'report_ui')
GROUPS = [  # (group, card title)
    ('accounts', 'Account Books (BUSY Display)'),
    ('inventory', 'Inventory Display'),
    ('analysis', 'Analysis & Final Results'),
    ('gst', 'GST Reports'),
    ('batch', 'Batch / Bale Reports'),
    ('registers', 'Registers & Movement'),
]


def section(text, name):
    m = re.search(rf'<!-- {name} -->(.*?)<!-- /{name} -->', text, re.S)
    return m.group(1).strip() if m else ''


def put(doc, anchor, group, kind, block, before=True):
    begin, end = f'<!-- REPORTS:{group}:{kind}:BEGIN -->', f'<!-- REPORTS:{group}:{kind}:END -->'
    doc = re.sub(re.escape(begin) + r'.*?' + re.escape(end) + r'\n?', '', doc, flags=re.S)
    if not block:
        return doc
    wrapped = f'{begin}\n{block}\n{end}\n'
    if anchor not in doc:
        raise SystemExit(f'anchor {anchor!r} missing from index.html')
    return doc.replace(anchor, wrapped + anchor, 1) if before else doc.replace(anchor, anchor + wrapped, 1)


def tiles_card(title, tiles):
    cells = []
    for line in tiles.splitlines():
        parts = [p.strip() for p in line.split('|')]
        if len(parts) != 4 or not parts[0]:
            continue
        view_id, label, icon, loader = parts
        if not re.fullmatch(r'[A-Za-z0-9_-]+', view_id) or not re.fullmatch(r'[A-Za-z_$][\w$]*', loader):
            raise SystemExit(f'bad tile line: {line!r}')
        cells.append(f'''                        <div onclick="openExtraReport('{view_id}', '{loader}');" class="flex flex-col items-center gap-1 cursor-pointer active:scale-95 transition">
                            <i class="{html.escape(icon)} text-2xl text-slate-800"></i>
                            <span class="text-[11px] font-medium text-slate-700 leading-tight">{html.escape(label)}</span>
                        </div>''')
    if not cells:
        return ''
    return f'''                <div class="bg-white rounded-2xl p-4 shadow-xs border border-blue-50/50 space-y-3">
                    <div class="text-sm font-bold text-busy-navy">{html.escape(title)}</div>
                    <div class="grid grid-cols-4 gap-y-4 text-center">
{chr(10).join(cells)}
                    </div>
                </div>'''


doc = open(INDEX, encoding='utf-8').read()
only = set(sys.argv[1:])
for group, title in GROUPS:
    if only and group not in only:
        continue
    path = os.path.join(UI_DIR, f'{group}.html')
    if not os.path.exists(path):
        print(f'{group}: no snippet yet, skipped')
        continue
    snip = open(path, encoding='utf-8').read()
    views, script, tiles = section(snip, 'VIEWS'), section(snip, 'SCRIPT'), section(snip, 'TILES')
    view_ids = re.findall(r'<div[^>]*\bid="(view-[A-Za-z0-9_-]+)"', views)
    # register this group's views so switchTab/closeReportView hide them too
    register = f"<script>window.EXTRA_REPORT_VIEWS = (window.EXTRA_REPORT_VIEWS || []).concat({view_ids!r});</script>"
    doc = put(doc, '<!-- REPORTS:VIEWS -->', group, 'VIEWS', views)
    doc = put(doc, '<!-- REPORTS:TILES -->', group, 'TILES', tiles_card(title, tiles))
    doc = put(doc, '</body>', group, 'SCRIPT', register + '\n' + script)
    print(f'{group}: views {view_ids}, script {len(script)} chars, tiles {len(tiles.splitlines())}')
open(INDEX, 'w', encoding='utf-8', newline='').write(doc)
