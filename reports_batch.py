"""Batch (bale) display reports over the busy_local.db mirror of BUSY data:
Batch Stock MC-wise (summary -> items -> batches), Batch Ledger, Item-wise Batch Summary.

Pure functions, standard library only. Every handler has the signature
    handler(conn, company, fy, params) -> dict
where `conn` is a read-only sqlite3 connection (row_factory = sqlite3.Row) and `params` is the parsed
query string (dict[str, list[str]]).

Batch lines (the business calls them bales) are busy_tran3 RecType 2:
  MasterCode1 = item (MasterType 6), MasterCode2 = material centre (MasterType 11), BillNo = batch / bale no,
  Value1 = qty in main unit (+ in / - out), VchCode 0 = opening batch stock carried from last year,
  Narration = batch description / parameter text, entered per line (can differ between lines of one batch).
  A batch is identified by item + material centre + batch no (the same batch no exists for several items).
Date columns, as found in the data (COMP0044):
  BillDate = the voucher date. For every line with VchCode > 0 it equals busy_tran1.Date (59,122 / 59,122).
             On opening lines (VchCode 0) it is the date the batch came into that MC last year (2023-09..2026-03-31).
  DueDate  = the batch's own date (manufacturing / first receipt date): constant for an item + batch no on every
             line and every MC (4,182 of 4,183), never later than the voucher date except 52 lines that are 1 day
             later; it equals the first inward voucher date for 3,396 of 4,183 batches. Shown as "Batch Date".
Stock closing qty = busy_tran4.D1 + SUM(busy_tran2.Value1 RecType 2); its difference from the batch closing is
shown as "not batch-tracked".
"""
VCH_NAMES = {
    0: 'Opening', 2: 'Purchase', 3: 'Sales Return', 4: 'Purchase Challan', 5: 'Stock Transfer',
    6: 'Production', 7: 'Material Receipt', 8: 'Stock Journal', 9: 'Sales', 10: 'Purchase Return',
    11: 'Sales Challan', 12: 'Sales Order', 13: 'Purchase Order', 14: 'Receipt', 15: 'Contra',
    16: 'Journal', 17: 'Debit Note', 18: 'Credit Note', 19: 'Payment', 28: 'Salary',
}

EPS = 0.0005


# ---------------------------------------------------------------- helpers
def _p(params, key, default=''):
    v = (params or {}).get(key)
    if not v:
        return default
    v = v[0] if isinstance(v, (list, tuple)) else v
    return v if v is not None else default


def _int(params, key, default=0):
    try:
        return int(str(_p(params, key, default)).strip() or default)
    except (TypeError, ValueError):
        return default


def _bool(params, key, default=True):
    s = str(_p(params, key, '')).strip().lower()
    if s == '':
        return default
    return s not in ('0', 'false', 'no', 'off')


def _vno(v):
    return (v or '').strip()


def _r3(x):
    return round(x or 0.0, 3) + 0.0  # + 0.0 turns -0.0 into 0.0


def _names(c, company, fy, master_types):
    marks = ','.join('?' * len(master_types))
    c.execute(f'SELECT Code, Name FROM busy_master1 WHERE company_code = ? AND fy_year = ? AND MasterType IN ({marks})',
              [company, fy] + list(master_types))
    return {r[0]: r[1] for r in c.fetchall()}


def _all_names(c, company, fy):
    c.execute('SELECT Code, Name FROM busy_master1 WHERE company_code = ? AND fy_year = ?', (company, fy))
    return {r[0]: r[1] for r in c.fetchall()}


def _unit_names(c, company, fy):
    c.execute('''SELECT i.Code, COALESCE(u.Name, '') FROM busy_master1 i
                 LEFT JOIN busy_master1 u ON u.company_code = i.company_code AND u.fy_year = i.fy_year AND u.Code = i.CM1
                 WHERE i.company_code = ? AND i.fy_year = ? AND i.MasterType = 6''', (company, fy))
    return {r[0]: r[1] for r in c.fetchall()}


_LINES_SQL = '''
    SELECT t3.VchCode, t3.SrNo, t3.MasterCode1 AS Item, t3.MasterCode2 AS MC, TRIM(t3.BillNo) AS BatchNo,
           t3.BillDate, t3.DueDate, t3.Value1 AS Qty, COALESCE(t3.Narration, '') AS Narration,
           t1.VchType, t1.VchNo, t1.Date AS VchDate, t1.MasterCode1 AS H1, t1.MasterCode2 AS H2
    FROM busy_tran3 t3
    LEFT JOIN busy_tran1 t1 ON t1.company_code = t3.company_code AND t1.fy_year = t3.fy_year AND t1.VchCode = t3.VchCode
    WHERE t3.company_code = ? AND t3.fy_year = ? AND t3.RecType = 2
      AND (t3.VchCode = 0 OR COALESCE(t1.Cancelled, 0) = 0) {extra}
    ORDER BY CASE WHEN t3.VchCode = 0 THEN 0 ELSE 1 END, COALESCE(t1.Date, t3.BillDate), t3.VchCode, t3.SrNo, t3.id
'''


def _lines(c, company, fy, extra='', args=()):
    c.execute(_LINES_SQL.format(extra=extra), [company, fy] + list(args))
    return c.fetchall()


def _line_date(r):
    return ((r['VchDate'] if r['VchCode'] else None) or r['BillDate'] or '')[:10]


def _new_agg():
    return {'opening': 0.0, 'in': 0.0, 'out': 0.0, 'closing': 0.0, 'first_in': '', 'last_date': '',
            'batch_date': '', 'descs': [], 'moves': 0}


def _add(a, r):
    q = r['Qty'] or 0.0
    d = _line_date(r)
    if r['VchCode'] == 0:
        a['opening'] += q
    elif q > 0:
        a['in'] += q
    else:
        a['out'] += -q
    a['closing'] += q
    a['moves'] += 1
    if q > 0 and d and (not a['first_in'] or d < a['first_in']):
        a['first_in'] = d
    if d and d > a['last_date']:
        a['last_date'] = d
    bd = (r['DueDate'] or '')[:10]
    if bd and (not a['batch_date'] or bd < a['batch_date']):
        a['batch_date'] = bd
    n = (r['Narration'] or '').strip()
    if n and n not in a['descs']:
        a['descs'].append(n)


def _batches(c, company, fy, extra='', args=()):
    """{(item, mc, batch_no): agg} over every batch line (lines are read in date order)."""
    out = {}
    for r in _lines(c, company, fy, extra, args):
        _add(out.setdefault((r['Item'], r['MC'], r['BatchNo'] or ''), _new_agg()), r)
    return out


def _fmt_agg(a):
    return {'Opening': _r3(a['opening']), 'In': _r3(a['in']), 'Out': _r3(a['out']), 'Closing': _r3(a['closing']),
            'FirstInDate': a['first_in'], 'LastDate': a['last_date'], 'BatchDate': a['batch_date'],
            'Description': a['descs'][0] if a['descs'] else '', 'Descriptions': a['descs'], 'Movements': a['moves']}


def _matches(search, item_name, mc_name, batch_no, a):
    if not search:
        return True
    hay = [item_name or '', mc_name or '', batch_no or ''] + a['descs']
    return any(search in h.lower() for h in hay)


def _item_stock_qty(c, company, fy, item, mc=0):
    """Tran4.D1 + SUM(Tran2.Value1 RecType 2) for the item (and MC), same as Stock Status."""
    mc_sql = 'AND MasterCode2 = ?' if mc > 0 else ''
    mc_args = [mc] if mc > 0 else []
    c.execute(f'''SELECT COALESCE(SUM(D1), 0) FROM busy_tran4
                  WHERE company_code = ? AND fy_year = ? AND MasterCode1 = ? {mc_sql}''', [company, fy, item] + mc_args)
    op = c.fetchone()[0] or 0.0
    c.execute(f'''SELECT COALESCE(SUM(t2.Value1), 0) FROM busy_tran2 t2
                  JOIN busy_tran1 t1 ON t1.company_code = t2.company_code AND t1.fy_year = t2.fy_year AND t1.VchCode = t2.VchCode
                  WHERE t2.company_code = ? AND t2.fy_year = ? AND t2.RecType = 2 AND t2.MasterCode1 = ?
                    AND t1.Cancelled = 0 {mc_sql.replace('MasterCode2', 't2.MasterCode2')}''',
              [company, fy, item] + mc_args)
    mov = c.fetchone()[0] or 0.0
    return op, mov


# ---------------------------------------------------------------- lookups
def batch_mcs(conn, company, fy, params):
    """Material centres that have batch lines (for the dropdowns)."""
    c = conn.cursor()
    c.execute('''SELECT m.Code, m.Name, COUNT(*) AS Lines FROM busy_tran3 t3
                 JOIN busy_master1 m ON m.company_code = t3.company_code AND m.fy_year = t3.fy_year AND m.Code = t3.MasterCode2
                 WHERE t3.company_code = ? AND t3.fy_year = ? AND t3.RecType = 2
                 GROUP BY m.Code, m.Name ORDER BY m.Name''', (company, fy))
    return {'mcs': [dict(r) for r in c.fetchall()]}


def batch_items(conn, company, fy, params):
    """Items that have batch lines. params: search (item name / alias), limit (default 30, max 200)."""
    c = conn.cursor()
    search = str(_p(params, 'search', '')).strip()
    limit = max(1, min(_int(params, 'limit', 30), 200))
    c.execute('''SELECT i.Code, i.Name, COALESCE(u.Name, '') AS UnitName, COALESCE(g.Name, '') AS GroupName,
                        COUNT(DISTINCT TRIM(t3.BillNo)) AS BatchNos, ROUND(SUM(t3.Value1), 3) AS BatchClosing
                 FROM busy_tran3 t3
                 JOIN busy_master1 i ON i.company_code = t3.company_code AND i.fy_year = t3.fy_year AND i.Code = t3.MasterCode1
                 LEFT JOIN busy_master1 u ON u.company_code = i.company_code AND u.fy_year = i.fy_year AND u.Code = i.CM1
                 LEFT JOIN busy_master1 g ON g.company_code = i.company_code AND g.fy_year = i.fy_year AND g.Code = i.ParentGrp
                 WHERE t3.company_code = ? AND t3.fy_year = ? AND t3.RecType = 2
                   AND (i.Name LIKE ? OR COALESCE(i.Alias, '') LIKE ? OR CAST(i.Code AS TEXT) = ?)
                 GROUP BY i.Code, i.Name, u.Name, g.Name
                 ORDER BY i.Name LIMIT ?''', (company, fy, f'%{search}%', f'%{search}%', search, limit))
    return {'items': [dict(r) for r in c.fetchall()]}


def batch_search(conn, company, fy, params):
    """Batch no search for the ledger picker. One row per item + MC + batch no.
    params: search (batch no, required, prefix/substring), item (0 = all), limit (default 40, max 200)."""
    c = conn.cursor()
    search = str(_p(params, 'search', '')).strip()
    item = _int(params, 'item', 0)
    limit = max(1, min(_int(params, 'limit', 40), 200))
    if not search and not item:
        return {'results': []}
    item_sql = 'AND t3.MasterCode1 = ?' if item else ''
    args = [company, fy, f'%{search}%'] + ([item] if item else []) + [search, f'{search}%', limit]
    c.execute(f'''SELECT TRIM(t3.BillNo) AS BatchNo, t3.MasterCode1 AS ItemCode, i.Name AS ItemName,
                         t3.MasterCode2 AS McCode, COALESCE(m.Name, '') AS McName,
                         ROUND(SUM(t3.Value1), 3) AS Closing, COUNT(*) AS Movements,
                         MAX(COALESCE(t3.Narration, '')) AS Description
                  FROM busy_tran3 t3
                  JOIN busy_master1 i ON i.company_code = t3.company_code AND i.fy_year = t3.fy_year AND i.Code = t3.MasterCode1
                  LEFT JOIN busy_master1 m ON m.company_code = t3.company_code AND m.fy_year = t3.fy_year AND m.Code = t3.MasterCode2
                  WHERE t3.company_code = ? AND t3.fy_year = ? AND t3.RecType = 2 AND t3.BillNo LIKE ? {item_sql}
                  GROUP BY TRIM(t3.BillNo), t3.MasterCode1, i.Name, t3.MasterCode2, m.Name
                  ORDER BY CASE WHEN TRIM(t3.BillNo) = ? THEN 0 WHEN TRIM(t3.BillNo) LIKE ? THEN 1 ELSE 2 END,
                           TRIM(t3.BillNo), i.Name, m.Name
                  LIMIT ?''', args)
    return {'results': [dict(r) for r in c.fetchall()]}


# ---------------------------------------------------------------- 1. batch stock MC-wise
def batch_mc_stock(conn, company, fy, params):
    """params: mc (0 = summary per material centre), item (0 = items in the MC; >0 = that item's batches),
    search (batch no / item / description / MC text), hide_zero (1 default = only batches with closing <> 0).
    mc=0 & item>0 lists the item's batches in every MC."""
    c = conn.cursor()
    mc = _int(params, 'mc', 0)
    item = _int(params, 'item', 0)
    search = str(_p(params, 'search', '')).strip().lower()
    hide_zero = _bool(params, 'hide_zero', True)
    names = _names(c, company, fy, (6, 11))
    units = _unit_names(c, company, fy)
    extra, args = '', []
    if mc > 0:
        extra += ' AND t3.MasterCode2 = ?'; args.append(mc)
    if item > 0:
        extra += ' AND t3.MasterCode1 = ?'; args.append(item)
    data = _batches(c, company, fy, extra, args)
    picked = []
    for (it, m, bno), a in data.items():
        if hide_zero and abs(a['closing']) <= EPS:
            continue
        if not _matches(search, names.get(it, ''), names.get(m, ''), bno, a):
            continue
        picked.append((it, m, bno, a))
    base = {'mc': ({'Code': mc, 'Name': names.get(mc, f'MC {mc}')} if mc > 0 else None),
            'item': ({'Code': item, 'Name': names.get(item, f'Item {item}'), 'UnitName': units.get(item, '')}
                     if item > 0 else None),
            'search': search, 'hide_zero': hide_zero}

    def totals(rows):
        return {'batch_count': len(rows),
                'batches_with_stock': sum(1 for r in rows if abs(r[3]['closing']) > EPS),
                'item_count': len({r[0] for r in rows}),
                'items_with_stock': len({r[0] for r in rows if abs(r[3]['closing']) > EPS}),
                'opening': _r3(sum(r[3]['opening'] for r in rows)),
                'in': _r3(sum(r[3]['in'] for r in rows)), 'out': _r3(sum(r[3]['out'] for r in rows)),
                'closing': _r3(sum(r[3]['closing'] for r in rows))}

    if item > 0:
        rows = []
        for it, m, bno, a in picked:
            row = {'BatchNo': bno, 'ItemCode': it, 'McCode': m, 'McName': names.get(m, '')}
            row.update(_fmt_agg(a))
            rows.append(row)
        rows.sort(key=lambda r: (r['McName'], r['FirstInDate'] or r['BatchDate'], r['BatchNo']))
        base.update({'level': 'batches', 'batches': rows, 'totals': totals(picked)})
        return base

    if mc > 0:
        agg = {}
        for p in picked:
            agg.setdefault(p[0], []).append(p)
        rows = []
        for it, lst in agg.items():
            t = totals(lst)
            rows.append({'Code': it, 'Name': names.get(it, f'Item {it}'), 'UnitName': units.get(it, ''),
                         'batch_count': t['batch_count'], 'batches_with_stock': t['batches_with_stock'],
                         'opening': t['opening'], 'in': t['in'], 'out': t['out'], 'closing': t['closing']})
        rows.sort(key=lambda r: r['Name'])
        base.update({'level': 'items', 'items': rows, 'totals': totals(picked)})
        return base

    agg = {}
    for p in picked:
        agg.setdefault(p[1], []).append(p)
    rows = []
    for m, lst in agg.items():
        t = totals(lst)
        t.update({'Code': m, 'Name': names.get(m, f'MC {m}')})
        rows.append(t)
    rows.sort(key=lambda r: r['Name'])
    t = totals(picked)
    t['mc_count'] = len(rows)
    base.update({'level': 'mcs', 'mcs': rows, 'totals': t})
    return base


# ---------------------------------------------------------------- 2. batch ledger
def batch_ledger(conn, company, fy, params):
    """params: batch (batch no, required), item (item code; required when the batch no exists for several items,
    otherwise picked automatically), mc (0 = all material centres)."""
    c = conn.cursor()
    bno = str(_p(params, 'batch', '')).strip()
    item = _int(params, 'item', 0)
    mc = _int(params, 'mc', 0)
    base = {'batch': bno, 'item': None, 'mc': None, 'choices': [], 'opening': 0.0, 'total_in': 0.0,
            'total_out': 0.0, 'closing': 0.0, 'rows': [], 'by_mc': []}
    if not bno:
        base['error'] = 'Batch no required'
        return base
    names = _all_names(c, company, fy)
    units = _unit_names(c, company, fy)
    c.execute('''SELECT t3.MasterCode1, ROUND(SUM(t3.Value1), 3), COUNT(*) FROM busy_tran3 t3
                 WHERE t3.company_code = ? AND t3.fy_year = ? AND t3.RecType = 2 AND TRIM(t3.BillNo) = ?
                 GROUP BY t3.MasterCode1''', (company, fy, bno))
    found = {r[0]: (r[1], r[2]) for r in c.fetchall()}
    base['choices'] = sorted(({'ItemCode': k, 'ItemName': names.get(k, f'Item {k}'), 'Closing': v[0] or 0.0,
                               'Movements': v[1]} for k, v in found.items()), key=lambda r: r['ItemName'])
    if not found:
        base['error'] = 'Batch not found'
        return base
    if not item:
        if len(found) > 1:
            base['error'] = 'This batch no exists for several items: pick one'
            base['need_item'] = True
            return base
        item = next(iter(found))
    if item not in found:
        base['error'] = 'Batch not found for this item'
        return base
    extra = ' AND TRIM(t3.BillNo) = ? AND t3.MasterCode1 = ?'
    args = [bno, item]
    if mc > 0:
        extra += ' AND t3.MasterCode2 = ?'; args.append(mc)
    lines = _lines(c, company, fy, extra, args)
    rows, opening, tin, tout, bal = [], 0.0, 0.0, 0.0, 0.0
    by_mc = {}
    descs = []
    batch_date = ''
    for r in lines:
        q = r['Qty'] or 0.0
        a = by_mc.setdefault(r['MC'], _new_agg())
        _add(a, r)
        bd = (r['DueDate'] or '')[:10]
        if bd and (not batch_date or bd < batch_date):
            batch_date = bd
        n = (r['Narration'] or '').strip()
        if n and n not in descs:
            descs.append(n)
        bal += q
        if r['VchCode'] == 0:
            opening += q
            rows.append({'Date': _line_date(r), 'VchCode': 0, 'VchType': 0, 'VchTypeName': 'Opening', 'VchNo': '',
                         'Party': 'Opening batch stock', 'McCode': r['MC'], 'McName': names.get(r['MC'], ''),
                         'Description': n, 'In': _r3(q) if q > 0 else 0.0, 'Out': _r3(-q) if q < 0 else 0.0,
                         'Balance': _r3(bal), 'IsOpening': True})
            continue
        if q > 0:
            tin += q
        else:
            tout += -q
        vt = r['VchType'] or 0
        party = names.get(r['H1'], '')
        if vt == 5:  # stock transfer: the other material centre
            other = r['H2'] if r['MC'] == r['H1'] else r['H1']
            party = ('To ' if q < 0 else 'From ') + names.get(other, '')
        rows.append({'Date': _line_date(r), 'VchCode': r['VchCode'], 'VchType': vt,
                     'VchTypeName': VCH_NAMES.get(vt, f'Type {vt}'), 'VchNo': _vno(r['VchNo']),
                     'Party': party, 'McCode': r['MC'], 'McName': names.get(r['MC'], ''), 'Description': n,
                     'In': _r3(q) if q > 0 else 0.0, 'Out': _r3(-q) if q < 0 else 0.0, 'Balance': _r3(bal),
                     'IsOpening': False})
    base.update({
        'item': {'Code': item, 'Name': names.get(item, f'Item {item}'), 'UnitName': units.get(item, '')},
        'mc': ({'Code': mc, 'Name': names.get(mc, f'MC {mc}')} if mc > 0 else None),
        'batch_date': batch_date, 'descriptions': descs,
        'opening': _r3(opening), 'total_in': _r3(tin), 'total_out': _r3(tout), 'closing': _r3(bal), 'rows': rows,
        'by_mc': sorted(({'McCode': m, 'McName': names.get(m, f'MC {m}'), 'Opening': _r3(a['opening']),
                          'In': _r3(a['in']), 'Out': _r3(a['out']), 'Closing': _r3(a['closing'])}
                         for m, a in by_mc.items()), key=lambda r: r['McName']),
    })
    return base


# ---------------------------------------------------------------- 3. item-wise batch summary
def batch_item_summary(conn, company, fy, params):
    """params: item (required), mc (0 = all MCs, batches combined across MCs), hide_zero (0 default = show all),
    search (batch no / description)."""
    c = conn.cursor()
    item = _int(params, 'item', 0)
    mc = _int(params, 'mc', 0)
    hide_zero = _bool(params, 'hide_zero', False)
    search = str(_p(params, 'search', '')).strip().lower()
    names = _names(c, company, fy, (6, 11))
    units = _unit_names(c, company, fy)
    zero = {'batch_count': 0, 'batches_with_stock': 0, 'opening': 0.0, 'in': 0.0, 'out': 0.0, 'closing': 0.0}
    base = {'item': None, 'mc': None, 'batches': [], 'totals': dict(zero), 'stock': None}
    if item <= 0 or item not in units:
        base['error'] = 'Item not found'
        return base
    extra, args = ' AND t3.MasterCode1 = ?', [item]
    if mc > 0:
        extra += ' AND t3.MasterCode2 = ?'; args.append(mc)
    by_batch, mcs_of = {}, {}
    for r in _lines(c, company, fy, extra, args):
        b = r['BatchNo'] or ''
        _add(by_batch.setdefault(b, _new_agg()), r)
        m = mcs_of.setdefault(b, {})
        m[r['MC']] = m.get(r['MC'], 0.0) + (r['Qty'] or 0.0)
    all_closing = sum(a['closing'] for a in by_batch.values())
    rows = []
    for b, a in by_batch.items():
        if hide_zero and abs(a['closing']) <= EPS:
            continue
        if search and not (search in b.lower() or any(search in d.lower() for d in a['descs'])):
            continue
        row = {'BatchNo': b}
        row.update(_fmt_agg(a))
        row['StockIn'] = [{'McCode': m, 'McName': names.get(m, f'MC {m}'), 'Closing': _r3(q)}
                          for m, q in sorted(mcs_of[b].items(), key=lambda kv: names.get(kv[0], ''))
                          if abs(q) > EPS]
        rows.append(row)
    rows.sort(key=lambda r: (r['FirstInDate'] or r['BatchDate'] or '9999', r['BatchNo']))
    op, mov = _item_stock_qty(c, company, fy, item, mc)
    stock_qty = op + mov
    base.update({
        'item': {'Code': item, 'Name': names.get(item), 'UnitName': units.get(item, '')},
        'mc': ({'Code': mc, 'Name': names.get(mc, f'MC {mc}')} if mc > 0 else None),
        'hide_zero': hide_zero, 'search': search,
        'batches': rows,
        'totals': {'batch_count': len(rows), 'batches_with_stock': sum(1 for r in rows if abs(r['Closing']) > EPS),
                   'opening': _r3(sum(r['Opening'] for r in rows)), 'in': _r3(sum(r['In'] for r in rows)),
                   'out': _r3(sum(r['Out'] for r in rows)), 'closing': _r3(sum(r['Closing'] for r in rows))},
        # Comparison always uses every batch of the item (not the search / hide-zero filter).
        'stock': {'opening_qty': _r3(op), 'movement_qty': _r3(mov), 'stock_qty': _r3(stock_qty),
                  'batch_closing': _r3(all_closing), 'not_batch_tracked': _r3(stock_qty - all_closing),
                  'has_difference': abs(stock_qty - all_closing) > EPS},
    })
    return base


ROUTES = {
    '/api/reports/batch-mcs': batch_mcs,
    '/api/reports/batch-items': batch_items,
    '/api/reports/batch-search': batch_search,
    '/api/reports/batch-mc-stock': batch_mc_stock,
    '/api/reports/batch-ledger': batch_ledger,
    '/api/reports/batch-item-summary': batch_item_summary,
}
