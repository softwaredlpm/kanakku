"""Registers report group: Voucher Registers, Challan / Order Registers, Party x Item Sales / Purchase,
Stock Movement Analysis (month-wise in/out + slow / non-moving items).

Pure functions over the read-only SQLite mirror of BUSY data (standard library only, no import of server).
Every handler: handler(conn, company, fy, params) -> dict.

Data notes (verified in busy_local.db):
  * Voucher series: busy_master1 MasterType 21, Code = busy_tran1.VchSeriesCode; Name starts with the 2-digit
    VchType ("09A-GST") which is stripped.
  * Accounting vouchers (14/15/16/17/18/19): busy_tran2 RecType 1, Value1 negative = Dr, positive = Cr.
    Some Receipts (post-dated cheques) only carry RecType 5 lines with the same sign convention; used as fallback.
  * Salary (28): RecType 51 = one line per employee (Value1 = net pay, sums to VchAmt), RecType 52 = pay heads.
  * Stock vouchers (5/6/8): RecType 2 lines, MasterCode2 = material centre, Value1 + in / - out.
  * Challans / orders: status from busy_tran3 references exactly like reports_inventory.inventory_pending
    (RecType 3 = challan refs, RecType 4 = order refs, Method 1 = created, pending = sign * SUM(Value1) per RefCode).
  * Party x Item: sales = busy_tran2 RecType 2 + 7 of VchType 9, purchase = RecType 2 + 7 of VchType 2;
    amount = abs(busy_item_ext.GrossAmt) when present and non-zero, else abs(Value3).
  * Stock maths (opening, closing qty, average inward rate, value) = reports_inventory._item_stock, which is a copy
    of server.compute_item_stock().
"""
from datetime import date, timedelta

from reports_inventory import (EPS, PENDING_KINDS, VCH_NAMES as _INV_VCH_NAMES, _avg_rates, _date, _fy_range, _int,
                               _item_stock, _p, _unit_names)

VCH_NAMES = dict(_INV_VCH_NAMES)
VCH_NAMES[11] = 'Delivery Challan'

MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

# Voucher register types: key -> (VchType, kind)
VOUCHER_TYPES = {
    'journal': (16, 'acc'), 'contra': (15, 'acc'), 'debit_note': (17, 'acc'), 'credit_note': (18, 'acc'),
    'stock_transfer': (5, 'stock'), 'production': (6, 'stock'), 'stock_journal': (8, 'stock'),
    'salary': (28, 'salary'), 'receipt': (14, 'acc'), 'payment': (19, 'acc'),
}
VOUCHER_TYPE_ORDER = ['journal', 'contra', 'debit_note', 'credit_note', 'stock_transfer', 'production',
                      'stock_journal', 'salary', 'receipt', 'payment']

# Challan / order register types: key -> (VchType, reports_inventory pending kind, line RecType)
CHALLAN_TYPES = {
    'dc': (11, 'dc', 2), 'pc': (4, 'pc', 2), 'so': (12, 'so', 4), 'po': (13, 'po', 4),
}
CHALLAN_TYPE_ORDER = ['dc', 'pc', 'so', 'po']
STATUS_LABELS = {'pending': 'Pending', 'partial': 'Partly cleared', 'cleared': 'Cleared', 'none': 'No own reference'}

PXI_TYPES = {'sales': 9, 'purchase': 2}


# ---------------------------------------------------------------- helpers
def _r2(x):
    return round(x or 0.0, 2)


def _r3(x):
    return round(x or 0.0, 3)


def _vno(v):
    return (v or '').strip()


def _month_label(ym):
    return f"{MONTH_NAMES[int(ym[5:7]) - 1]} {ym[:4]}"


def _fy_months(d_from, d_to):
    out = []
    y, m = int(d_from[:4]), int(d_from[5:7])
    while f'{y:04d}-{m:02d}' <= d_to[:7]:
        out.append(f'{y:04d}-{m:02d}')
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def _all_names(c, company, fy):
    c.execute('SELECT Code, Name FROM busy_master1 WHERE company_code = ? AND fy_year = ?', (company, fy))
    return {r[0]: r[1] for r in c.fetchall()}


def _series_names(c, company, fy):
    c.execute('SELECT Code, Name FROM busy_master1 WHERE company_code = ? AND fy_year = ? AND MasterType = 21',
              (company, fy))
    out = {}
    for code, name in c.fetchall():
        name = name or ''
        out[code] = name[2:] if len(name) > 2 and name[:2].isdigit() else name
    return out


def _vch_lines(c, company, fy, vchs, rectypes):
    """{VchCode: [line dict]} for the given vouchers and tran2 RecTypes."""
    out = {}
    vchs = list(vchs)
    rt = ','.join('?' * len(rectypes))
    for i in range(0, len(vchs), 500):
        chunk = vchs[i:i + 500]
        marks = ','.join('?' * len(chunk))
        c.execute(f'''SELECT VchCode, SrNo, RecType, MasterCode1, MasterCode2, Value1, Value2, Value3
                      FROM busy_tran2 WHERE company_code = ? AND fy_year = ? AND RecType IN ({rt})
                        AND VchCode IN ({marks}) ORDER BY VchCode, RecType, SrNo''',
                  [company, fy] + list(rectypes) + chunk)
        for r in c.fetchall():
            out.setdefault(r['VchCode'], []).append(dict(r))
    return out


def _agg_names(pairs, names):
    """[(code, amount)] -> [{'code','name','amount'}] summed per code, biggest first."""
    acc = {}
    order = []
    for code, amt in pairs:
        if code not in acc:
            acc[code] = 0.0
            order.append(code)
        acc[code] += amt
    out = [{'code': c, 'name': names.get(c, ''), 'amount': _r2(acc[c])} for c in order]
    out.sort(key=lambda x: -abs(x['amount']))
    return out


def _months_and_grand(rows, amount_key='amount', extra=()):
    months, grand = {}, {'count': 0, 'amount': 0.0}
    for k in extra:
        grand[k] = 0.0
    for r in rows:
        ym = r['date'][:7]
        m = months.setdefault(ym, dict({'month': ym, 'label': _month_label(ym), 'count': 0, 'amount': 0.0},
                                       **{k: 0.0 for k in extra}))
        for agg in (m, grand):
            agg['count'] += 1
            agg['amount'] += r[amount_key] or 0.0
            for k in extra:
                agg[k] += r.get(k) or 0.0
    rnd = lambda d: {k: (_r2(v) if isinstance(v, float) else v) for k, v in d.items()}
    return [rnd(m) for m in sorted(months.values(), key=lambda x: x['month'])], rnd(grand)


def _type_counts(c, company, fy, d_from, d_to, vtypes):
    marks = ','.join('?' * len(vtypes))
    c.execute(f'''SELECT VchType, COUNT(*), COALESCE(SUM(VchAmt), 0) FROM busy_tran1
                  WHERE company_code = ? AND fy_year = ? AND Cancelled = 0 AND VchType IN ({marks})
                    AND Date BETWEEN ? AND ? GROUP BY VchType''', [company, fy] + list(vtypes) + [d_from, d_to])
    return {r[0]: (r[1], r[2]) for r in c.fetchall()}


# ---------------------------------------------------------------- lookups
def registers_search(conn, company, fy, params):
    """Party / item picker. params: kind = party | item (default party), q (text), limit (default 30, max 200)."""
    c = conn.cursor()
    kind = str(_p(params, 'kind', 'party')).strip().lower()
    mt = 6 if kind == 'item' else 2
    q = str(_p(params, 'q', '')).strip()
    limit = max(1, min(_int(params, 'limit', 30), 200))
    like = f'%{q}%'
    c.execute('''SELECT Code, Name, Alias FROM busy_master1
                 WHERE company_code = ? AND fy_year = ? AND MasterType = ?
                   AND (? = '' OR Name LIKE ? OR COALESCE(Alias, '') LIKE ?)
                 ORDER BY Name LIMIT ?''', (company, fy, mt, q, like, like, limit))
    return {'kind': 'item' if mt == 6 else 'party',
            'rows': [{'code': r['Code'], 'name': r['Name'], 'alias': r['Alias'] or ''} for r in c.fetchall()]}


# ---------------------------------------------------------------- 1. voucher registers
def registers_voucher_register(conn, company, fy, params):
    """params: type = journal|contra|debit_note|credit_note|stock_transfer|production|stock_journal|salary|receipt|
    payment (or the VchType number; default journal), from, to (default FY start .. today), search (text)."""
    c = conn.cursor()
    t = str(_p(params, 'type', 'journal')).strip().lower()
    if t.isdigit():
        t = next((k for k, v in VOUCHER_TYPES.items() if v[0] == int(t)), 'journal')
    if t not in VOUCHER_TYPES:
        t = 'journal'
    vt, kind = VOUCHER_TYPES[t]
    d_from, d_to = _fy_range(fy, params)
    search = str(_p(params, 'search', '')).strip().lower()

    c.execute('''SELECT VchCode, Date, VchNo, MasterCode1, MasterCode2, COALESCE(VchAmt, 0) AS VchAmt, VchSeriesCode
                 FROM busy_tran1 WHERE company_code = ? AND fy_year = ? AND Cancelled = 0 AND VchType = ?
                   AND Date BETWEEN ? AND ? ORDER BY Date, VchCode''', (company, fy, vt, d_from, d_to))
    heads = [dict(r) for r in c.fetchall()]
    names = _all_names(c, company, fy)
    series = _series_names(c, company, fy)
    rectypes = {'acc': (1, 5), 'stock': (2,), 'salary': (51,)}[kind]
    lines = _vch_lines(c, company, fy, [h['VchCode'] for h in heads], rectypes)

    rows = []
    for h in heads:
        ls = lines.get(h['VchCode'], [])
        row = {'vch_code': h['VchCode'], 'date': h['Date'], 'vch_no': _vno(h['VchNo']),
               'series': series.get(h['VchSeriesCode'], ''), 'amount': _r2(h['VchAmt'])}
        if kind == 'acc':
            acc = [l for l in ls if l['RecType'] == 1] or [l for l in ls if l['RecType'] == 5]
            dr = _agg_names([(l['MasterCode1'], -l['Value1']) for l in acc if (l['Value1'] or 0) < 0], names)
            cr = _agg_names([(l['MasterCode1'], l['Value1']) for l in acc if (l['Value1'] or 0) > 0], names)
            main = h['MasterCode1'] if h['MasterCode1'] else 0
            if not main:
                side = cr if vt == 14 else dr
                main = side[0]['code'] if side else (dr or cr or [{'code': 0}])[0]['code']
            row.update({'party_code': main, 'party': names.get(main, ''), 'dr': dr, 'cr': cr,
                        'pdc': bool(acc) and acc[0]['RecType'] == 5,
                        'summary': 'Dr: ' + ', '.join(x['name'] for x in dr) + ' | Cr: ' + ', '.join(x['name'] for x in cr)})
        elif kind == 'salary':
            # RecType 51: Value1 = employee gross pay, Value3 = employee net pay (after deductions)
            emps = _agg_names([(l['MasterCode1'], l['Value1'] or 0.0) for l in ls], names)
            net = {}
            for l in ls:
                net[l['MasterCode1']] = net.get(l['MasterCode1'], 0.0) + (l['Value3'] or 0.0)
            for e in emps:
                e['net'] = _r2(net.get(e['code'], 0.0))
            row.update({'party_code': 0, 'party': f'{len(emps)} employee' + ('' if len(emps) == 1 else 's'),
                        'employees': emps, 'gross_pay': _r2(sum(e['amount'] for e in emps)),
                        'net_pay': _r2(sum(net.values())),
                        'summary': ', '.join(e['name'] for e in emps[:6]) + (' ...' if len(emps) > 6 else '')})
        else:
            frm_mc, to_mc, q_in, q_out = [], [], 0.0, 0.0
            for l in ls:
                v = l['Value1'] or 0.0
                if v < 0:
                    q_out += -v
                    if l['MasterCode2'] not in frm_mc:
                        frm_mc.append(l['MasterCode2'])
                elif v > 0:
                    q_in += v
                    if l['MasterCode2'] not in to_mc:
                        to_mc.append(l['MasterCode2'])
            frm_n = [names.get(m, '') for m in frm_mc]
            to_n = [names.get(m, '') for m in to_mc]
            other = h['MasterCode2'] if vt == 6 else 0  # production: header MasterCode2 = bill of material
            row.update({'party_code': other, 'party': names.get(other, '') if other else '',
                        'from_mc': frm_n, 'to_mc': to_n, 'qty_in': _r3(q_in), 'qty_out': _r3(q_out),
                        'qty': _r3(q_in if q_in > 0 else q_out),
                        'summary': ('From ' + ', '.join(frm_n) if frm_n else '') +
                                   (' -> ' if frm_n and to_n else '') + ('To ' + ', '.join(to_n) if to_n else '')})
        if search and not any(search in str(row.get(k, '')).lower() for k in ('vch_no', 'party', 'summary', 'series')):
            continue
        rows.append(row)

    months, grand = _months_and_grand(rows, extra=('qty',) if kind == 'stock' else ())
    counts = _type_counts(c, company, fy, d_from, d_to, [VOUCHER_TYPES[k][0] for k in VOUCHER_TYPE_ORDER])
    return {'title': f'{VCH_NAMES.get(vt, "Voucher")} Register', 'type': t, 'vch_type': vt, 'kind': kind,
            'from': d_from, 'to': d_to, 'rows': rows, 'months': months, 'grand': grand,
            'types': [{'key': k, 'vch_type': VOUCHER_TYPES[k][0], 'name': VCH_NAMES.get(VOUCHER_TYPES[k][0], k),
                       'count': counts.get(VOUCHER_TYPES[k][0], (0, 0))[0]} for k in VOUCHER_TYPE_ORDER]}


# ---------------------------------------------------------------- 2. challan / order registers
def _ref_status(c, company, fy, rectype, sign, as_of):
    """Reference book-keeping copied from reports_inventory.inventory_pending.
    Returns {creating VchCode: {'ordered', 'adjusted', 'pending', 'refs', 'cleared_refs', 'touched_refs', 'clearing'}}."""
    c.execute('''SELECT t3.VchCode, t3.SrNo, t3.MasterCode1 AS Item, t3.MasterCode2 AS Party, t3.RefCode,
                        t3.Method, t3.BillNo, t3.BillDate, t3.Value1,
                        t1.VchType, t1.VchNo, t1.Date
                 FROM busy_tran3 t3
                 LEFT JOIN busy_tran1 t1 ON t1.company_code = t3.company_code AND t1.fy_year = t3.fy_year
                                         AND t1.VchCode = t3.VchCode
                 WHERE t3.company_code = ? AND t3.fy_year = ? AND t3.RecType = ?
                   AND COALESCE(t1.Cancelled, 0) = 0
                 ORDER BY t3.RefCode, t3.Method, COALESCE(t1.Date, t3.BillDate), t3.VchCode''',
              (company, fy, rectype))
    refs = {}
    for r in c.fetchall():
        d = (r['Date'] or r['BillDate'] or '')[:10]
        if d and d > as_of:
            continue
        ref = refs.setdefault(r['RefCode'], {'m1': None, 'adj': []})
        if r['Method'] == 1 and ref['m1'] is None:
            ref['m1'] = dict(r, _date=d)
        else:
            ref['adj'].append(dict(r, _date=d))
    out = {}
    for code, ref in refs.items():
        m1 = ref['m1']
        if not m1 or not m1['Value1'] or (1 if m1['Value1'] > 0 else -1) != sign:
            continue
        total = (m1['Value1'] or 0.0) + sum(a['Value1'] or 0.0 for a in ref['adj'])
        pending = sign * total
        adjusted = -sign * sum(a['Value1'] or 0.0 for a in ref['adj'])
        v = out.setdefault(m1['VchCode'], {'ordered': 0.0, 'adjusted': 0.0, 'pending': 0.0, 'refs': 0,
                                           'cleared_refs': 0, 'touched_refs': 0, 'clearing': {}})
        v['refs'] += 1
        v['ordered'] += abs(m1['Value1'])
        v['adjusted'] += adjusted
        v['pending'] += pending
        if abs(pending) <= EPS:
            v['cleared_refs'] += 1
        if abs(adjusted) > EPS:
            v['touched_refs'] += 1
        for a in ref['adj']:
            if (a['VchCode'] or 0) > 0:
                key = a['VchCode']
                info = {'vch_code': a['VchCode'], 'vch_no': _vno(a['VchNo']), 'date': a['_date'],
                        'type_name': VCH_NAMES.get(a['VchType'], f"Type {a['VchType']}")}
            else:
                key = 'cleared'
                info = {'vch_code': 0, 'vch_no': 'Ref. Cleared', 'date': a['_date'], 'type_name': 'Ref. Cleared'}
            cl = v['clearing'].setdefault(key, dict(info, qty=0.0))
            cl['qty'] += -sign * (a['Value1'] or 0.0)
    return out


def registers_challan_register(conn, company, fy, params):
    """params: type = dc | pc | so | po (or 11/4/12/13; default dc), from, to (voucher date range, default FY start ..
    today), as_of (status date, default = reports_inventory default: min(today, FY end)),
    status = all | pending | partial | cleared | none (default all), search (text)."""
    c = conn.cursor()
    t = str(_p(params, 'type', 'dc')).strip().lower()
    if t.isdigit():
        t = next((k for k, v in CHALLAN_TYPES.items() if v[0] == int(t)), 'dc')
    if t not in CHALLAN_TYPES:
        t = 'dc'
    vt, pkind, line_rt = CHALLAN_TYPES[t]
    rectype, sign, plabel = PENDING_KINDS[pkind]
    d_from, d_to = _fy_range(fy, params)
    _f, as_default = _fy_range(fy, {})
    as_of = _date(params, 'as_of', as_default)
    status_f = str(_p(params, 'status', 'all')).strip().lower()
    if status_f not in STATUS_LABELS:
        status_f = 'all'
    search = str(_p(params, 'search', '')).strip().lower()

    c.execute('''SELECT VchCode, Date, VchNo, MasterCode1, COALESCE(VchAmt, 0) AS VchAmt, VchSeriesCode
                 FROM busy_tran1 WHERE company_code = ? AND fy_year = ? AND Cancelled = 0 AND VchType = ?
                   AND Date BETWEEN ? AND ? ORDER BY Date, VchCode''', (company, fy, vt, d_from, d_to))
    heads = [dict(r) for r in c.fetchall()]
    names = _all_names(c, company, fy)
    series = _series_names(c, company, fy)
    lines = _vch_lines(c, company, fy, [h['VchCode'] for h in heads], (line_rt,))
    st = _ref_status(c, company, fy, rectype, sign, as_of)

    all_rows = []
    for h in heads:
        ls = lines.get(h['VchCode'], [])
        qty = sum(abs(l['Value1'] or 0.0) for l in ls)
        s = st.get(h['VchCode'])
        if not s or h['Date'] > as_of:
            status = 'none'
            ordered = adjusted = pending = 0.0
            clearing = []
        else:
            ordered, adjusted, pending = s['ordered'], s['adjusted'], s['pending']
            if s['cleared_refs'] == s['refs']:
                status = 'cleared'
            elif s['touched_refs'] == 0:
                status = 'pending'
            else:
                status = 'partial'
            clearing = sorted(({**cl, 'qty': _r3(cl['qty'])} for cl in s['clearing'].values()),
                              key=lambda x: (x['date'] or '', x['vch_no']))
        row = {'vch_code': h['VchCode'], 'date': h['Date'], 'vch_no': _vno(h['VchNo']),
               'series': series.get(h['VchSeriesCode'], ''), 'party_code': h['MasterCode1'],
               'party': names.get(h['MasterCode1'], ''), 'qty': _r3(qty), 'items': len(ls),
               'amount': _r2(h['VchAmt']), 'status': status, 'status_label': STATUS_LABELS[status],
               'ref_qty': _r3(ordered), 'cleared_qty': _r3(adjusted), 'pending_qty': _r3(pending),
               'clearing': clearing}
        if search and not any(search in str(row[k]).lower() for k in ('vch_no', 'party', 'series')) and \
                not any(search in cl['vch_no'].lower() for cl in clearing):
            continue
        all_rows.append(row)

    status_totals = {}
    for k in STATUS_LABELS:
        rs = [r for r in all_rows if r['status'] == k]
        status_totals[k] = {'label': STATUS_LABELS[k], 'count': len(rs), 'amount': _r2(sum(r['amount'] for r in rs)),
                            'pending_qty': _r3(sum(r['pending_qty'] for r in rs))}
    rows = all_rows if status_f == 'all' else [r for r in all_rows if r['status'] == status_f]
    months, grand = _months_and_grand(rows, extra=('qty', 'pending_qty'))
    _m, grand_all = _months_and_grand(all_rows, extra=('qty',))
    counts = _type_counts(c, company, fy, d_from, d_to, [CHALLAN_TYPES[k][0] for k in CHALLAN_TYPE_ORDER])
    return {'title': f'{VCH_NAMES.get(vt)} Register', 'type': t, 'vch_type': vt, 'from': d_from, 'to': d_to,
            'as_of': as_of, 'status': status_f, 'rows': rows, 'months': months, 'grand': grand,
            'grand_all': grand_all, 'status_totals': status_totals,
            'note': {'none': 'Vouchers without their own references (e.g. MODEL-RC purchase challans that only '
                             'adjust delivery challans).'},
            'types': [{'key': k, 'vch_type': CHALLAN_TYPES[k][0], 'name': VCH_NAMES.get(CHALLAN_TYPES[k][0]),
                       'count': counts.get(CHALLAN_TYPES[k][0], (0, 0))[0]} for k in CHALLAN_TYPE_ORDER]}


# ---------------------------------------------------------------- 3. party x item sales / purchase
def registers_party_item(conn, company, fy, params):
    """params: type = sales | purchase (default sales), mode = party | item (default party),
    code (party or item code; 0 = list all parties / items with totals), from, to, search (text)."""
    c = conn.cursor()
    t = str(_p(params, 'type', 'sales')).strip().lower()
    if t not in PXI_TYPES:
        t = 'sales'
    vt = PXI_TYPES[t]
    mode = str(_p(params, 'mode', 'party')).strip().lower()
    if mode not in ('party', 'item'):
        mode = 'party'
    code = _int(params, 'code', 0)
    d_from, d_to = _fy_range(fy, params)
    search = str(_p(params, 'search', '')).strip().lower()

    sql = '''SELECT t1.MasterCode1 AS Party, t2.MasterCode1 AS Item,
                    COUNT(*) AS n, COUNT(DISTINCT t1.VchCode) AS vchs,
                    SUM(abs(t2.Value1)) AS qty,
                    SUM(CASE WHEN COALESCE(x.GrossAmt, 0) <> 0 THEN abs(x.GrossAmt) ELSE abs(t2.Value3) END) AS amount,
                    SUM(CASE WHEN COALESCE(x.GrossAmt, 0) <> 0 THEN 1 ELSE 0 END) AS gross_lines,
                    MAX(t1.Date) AS last_date
             FROM busy_tran2 t2
             JOIN busy_tran1 t1 ON t1.company_code = t2.company_code AND t1.fy_year = t2.fy_year AND t1.VchCode = t2.VchCode
             LEFT JOIN busy_item_ext x ON x.company_code = t2.company_code AND x.fy_year = t2.fy_year
                      AND x.VchCode = t2.VchCode AND x.SrNo = t2.SrNo AND x.RecType = t2.RecType
             WHERE t2.company_code = ? AND t2.fy_year = ? AND t2.RecType IN (2, 7) AND t1.VchType = ?
               AND t1.Cancelled = 0 AND t1.Date BETWEEN ? AND ?'''
    args = [company, fy, vt, d_from, d_to]
    if code:
        sql += ' AND ' + ('t1.MasterCode1' if mode == 'party' else 't2.MasterCode1') + ' = ?'
        args.append(code)
        group = 't2.MasterCode1' if mode == 'party' else 't1.MasterCode1'
    else:
        group = 't1.MasterCode1' if mode == 'party' else 't2.MasterCode1'
    c.execute(sql + f' GROUP BY {group}', args)
    agg = [dict(r) for r in c.fetchall()]
    names = _all_names(c, company, fy)
    units = _unit_names(c, company, fy)

    rows, total_lines, gross_lines = [], 0, 0
    for a in agg:
        key = a['Item'] if (code and mode == 'party') or (not code and mode == 'item') else a['Party']
        is_item = (code and mode == 'party') or (not code and mode == 'item')
        qty, amt = a['qty'] or 0.0, a['amount'] or 0.0
        total_lines += a['n']
        gross_lines += a['gross_lines']
        row = {'code': key, 'name': names.get(key, ''), 'unit': units.get(key, '') if is_item else '',
               'qty': _r3(qty), 'amount': _r2(amt), 'avg_rate': _r2(amt / qty) if qty else 0.0,
               'vouchers': a['vchs'], 'lines': a['n'], 'last_date': a['last_date']}
        if search and search not in row['name'].lower():
            continue
        rows.append(row)
    rows.sort(key=lambda r: -r['amount'])
    units_seen = {r['unit'] for r in rows if r['unit']}
    listing = 'item' if ((code and mode == 'party') or (not code and mode == 'item')) else 'party'
    return {'title': ('Party x Item ' if mode == 'party' else 'Item x Party ') + ('Sales' if vt == 9 else 'Purchase'),
            'type': t, 'vch_type': vt, 'mode': mode, 'code': code, 'code_name': names.get(code, '') if code else '',
            'listing': listing, 'from': d_from, 'to': d_to, 'rows': rows,
            'totals': {'count': len(rows), 'amount': _r2(sum(r['amount'] for r in rows)),
                       'qty': _r3(sum(r['qty'] for r in rows)) if (listing == 'item' or code) else None,
                       'mixed_units': len(units_seen) > 1, 'lines': total_lines, 'gross_lines': gross_lines},
            'amount_basis': ('Gross item value (before discounts, tax and other bill sundries)'
                             if gross_lines == total_lines else
                             f'Gross item value (before bill sundries) for {gross_lines} of {total_lines} lines, net value for the rest'),
            'line_basis': f'busy_tran2 RecType 2 + 7 of VchType {vt}'}


# ---------------------------------------------------------------- 4. stock movement analysis
def _movement(c, company, fy, d_from, d_to):
    """{item: {'pre': qty before d_from, 'months': {ym: [in, out]}, 'tin': transfer in, 'tout': transfer out,
    'post': qty after d_to}} from busy_tran2 RecType 2 (stock-affecting). VchType 5 (inter-godown transfers)
    kept apart."""
    c.execute('''SELECT t2.MasterCode1 AS Item, substr(t1.Date, 1, 7) AS ym, t1.Date < ? AS pre, t1.Date > ? AS post,
                        t1.VchType = 5 AS tr,
                        SUM(CASE WHEN t2.Value1 > 0 THEN t2.Value1 ELSE 0 END) AS qin,
                        SUM(CASE WHEN t2.Value1 < 0 THEN -t2.Value1 ELSE 0 END) AS qout
                 FROM busy_tran2 t2
                 JOIN busy_tran1 t1 ON t1.company_code = t2.company_code AND t1.fy_year = t2.fy_year AND t1.VchCode = t2.VchCode
                 WHERE t2.company_code = ? AND t2.fy_year = ? AND t2.RecType = 2
                 GROUP BY 1, 2, 3, 4, 5''', (d_from, d_to, company, fy))
    out = {}
    for r in c.fetchall():
        m = out.setdefault(r['Item'], {'pre': 0.0, 'post': 0.0, 'months': {}, 'tin': 0.0, 'tout': 0.0})
        qin, qout = r['qin'] or 0.0, r['qout'] or 0.0
        if r['pre']:
            m['pre'] += qin - qout
        elif r['post']:
            m['post'] += qin - qout
        elif r['tr']:
            m['tin'] += qin
            m['tout'] += qout
        else:
            mm = m['months'].setdefault(r['ym'], [0.0, 0.0])
            mm[0] += qin
            mm[1] += qout
    return out


def _item_master(c, company, fy):
    c.execute('''SELECT i.Code, i.Name, i.ParentGrp, COALESCE(u.Name, '') AS UnitName
                 FROM busy_master1 i
                 LEFT JOIN busy_master1 u ON u.company_code = i.company_code AND u.fy_year = i.fy_year AND u.Code = i.CM1
                 WHERE i.company_code = ? AND i.fy_year = ? AND i.MasterType = 6''', (company, fy))
    return {r['Code']: dict(r) for r in c.fetchall()}


def registers_stock_movement(conn, company, fy, params):
    """params: from, to (default FY start .. today), item (code: adds month-wise detail for that item),
    search (item name text), limit (rows returned, default 300, 0 = all; totals always cover every item).
    Opening = Tran4.D1 + movement before `from`; in/out = RecType 2 lines excluding VchType 5 transfers
    (transfers shown separately; they net to zero per item); closing = opening + in - out + transfer net."""
    c = conn.cursor()
    d_from, d_to = _fy_range(fy, params)
    item_f = _int(params, 'item', 0)
    search = str(_p(params, 'search', '')).strip().lower()
    limit = _int(params, 'limit', 300)
    months = _fy_months(d_from, d_to)
    stock = _item_stock(c, company, fy)
    rates = _avg_rates(c, company, fy)
    mov = _movement(c, company, fy, d_from, d_to)
    items = _item_master(c, company, fy)

    rows = []
    month_tot = {ym: [0.0, 0.0] for ym in months}
    tot = {'opening': 0.0, 'inward': 0.0, 'outward': 0.0, 'transfer_in': 0.0, 'transfer_out': 0.0, 'closing': 0.0,
           'closing_value': 0.0}
    detail = None
    for code in set(stock) | set(mov):
        s = stock.get(code, {})
        m = mov.get(code, {'pre': 0.0, 'post': 0.0, 'months': {}, 'tin': 0.0, 'tout': 0.0})
        op = (s.get('OpQty') or 0.0) + m['pre']
        qin = sum(v[0] for v in m['months'].values())
        qout = sum(v[1] for v in m['months'].values())
        tnet = m['tin'] - m['tout']
        closing = op + qin - qout + tnet
        if max(abs(op), abs(qin), abs(qout), abs(closing), m['tin']) <= EPS:
            continue
        it = items.get(code, {'Name': f'Item {code}', 'UnitName': ''})
        rate = rates.get(code, 0.0)  # unrounded average inward rate, as in compute_item_stock
        cval = round(round(closing, 3) * rate, 2)
        row = {'code': code, 'name': it['Name'], 'unit': it['UnitName'], 'opening': _r3(op), 'inward': _r3(qin),
               'outward': _r3(qout), 'transfer_in': _r3(m['tin']), 'transfer_out': _r3(m['tout']),
               'transfer_net': _r3(tnet), 'closing': _r3(closing), 'avg_rate': _r2(rate),
               'closing_value': _r2(cval)}
        for ym, (a, b) in m['months'].items():
            if ym in month_tot:
                month_tot[ym][0] += a
                month_tot[ym][1] += b
        for k, v in (('opening', op), ('inward', qin), ('outward', qout), ('transfer_in', m['tin']),
                     ('transfer_out', m['tout']), ('closing', closing), ('closing_value', cval)):
            tot[k] += v
        if code == item_f:
            bal = op
            det = []
            for ym in months:
                a, b = m['months'].get(ym, [0.0, 0.0])
                bal += a - b
                det.append({'month': ym, 'label': _month_label(ym), 'inward': _r3(a), 'outward': _r3(b),
                            'closing': _r3(bal)})
            detail = dict(row, months=det)
        if search and search not in it['Name'].lower():
            continue
        rows.append(row)
    rows.sort(key=lambda r: r['name'])
    tot = {k: (_r2(v) if k == 'closing_value' else _r3(v)) for k, v in tot.items()}
    tot['items'] = len(rows)
    shown = rows if limit <= 0 else rows[:limit]
    return {'title': 'Stock Movement Analysis', 'from': d_from, 'to': d_to, 'rows': shown, 'row_count': len(rows),
            'totals': tot, 'item': detail,
            'months': [{'month': ym, 'label': _month_label(ym), 'inward': _r3(a), 'outward': _r3(b)}
                       for ym, (a, b) in month_tot.items()],
            'basis': 'busy_tran2 RecType 2 (stock-affecting); in/out exclude VchType 5 inter-godown transfers'}


def registers_slow_moving(conn, company, fy, params):
    """params: days (default 90), as_of (default min(today, FY end)), search (item text).
    Items with closing qty > 0 whose last outward movement (RecType 2, Value1 < 0, excluding VchType 5) is older
    than `days` days before as_of, or that never moved out. Value = closing qty x average inward rate
    (server.compute_item_stock logic via reports_inventory._item_stock)."""
    c = conn.cursor()
    days = _int(params, 'days', 90)
    if days < 0:
        days = 90
    _f, as_default = _fy_range(fy, {})
    as_of = _date(params, 'as_of', as_default)
    cutoff = (date.fromisoformat(as_of) - timedelta(days=days)).isoformat()
    search = str(_p(params, 'search', '')).strip().lower()
    stock = _item_stock(c, company, fy)
    items = _item_master(c, company, fy)
    c.execute('''SELECT t2.MasterCode1 AS Item,
                        MAX(CASE WHEN t2.Value1 < 0 THEN t1.Date END) AS last_out,
                        MAX(CASE WHEN t2.Value1 > 0 THEN t1.Date END) AS last_in
                 FROM busy_tran2 t2
                 JOIN busy_tran1 t1 ON t1.company_code = t2.company_code AND t1.fy_year = t2.fy_year AND t1.VchCode = t2.VchCode
                 WHERE t2.company_code = ? AND t2.fy_year = ? AND t2.RecType = 2 AND t1.VchType <> 5
                   AND t1.Cancelled = 0
                 GROUP BY t2.MasterCode1''', (company, fy))
    last = {r['Item']: (r['last_in'], r['last_out']) for r in c.fetchall()}
    rows = []
    in_stock = 0
    for code, s in stock.items():
        if (s.get('ClosingQty') or 0.0) <= EPS:
            continue
        in_stock += 1
        li, lo = last.get(code, (None, None))
        if lo and lo >= cutoff:
            continue
        it = items.get(code, {'Name': f'Item {code}', 'UnitName': ''})
        if search and search not in it['Name'].lower():
            continue
        rows.append({'code': code, 'name': it['Name'], 'unit': it['UnitName'], 'last_in': li, 'last_out': lo,
                     'days_since_out': (date.fromisoformat(as_of) - date.fromisoformat(lo)).days if lo else None,
                     'never_out': not lo, 'closing_qty': s['ClosingQty'], 'avg_rate': s['AvgRate'],
                     'closing_value': s['ClosingValue']})
    rows.sort(key=lambda r: (not r['never_out'], r['last_out'] or '', -r['closing_value']))
    return {'title': 'Slow / Non-moving Items', 'days': days, 'as_of': as_of, 'cutoff': cutoff, 'rows': rows,
            'totals': {'count': len(rows), 'never_out': sum(1 for r in rows if r['never_out']),
                       'closing_qty': _r3(sum(r['closing_qty'] for r in rows)),
                       'closing_value': _r2(sum(r['closing_value'] for r in rows)), 'items_in_stock': in_stock}}


ROUTES = {
    '/api/reports/registers_search': registers_search,
    '/api/reports/registers_voucher_register': registers_voucher_register,
    '/api/reports/registers_challan_register': registers_challan_register,
    '/api/reports/registers_party_item': registers_party_item,
    '/api/reports/registers_stock_movement': registers_stock_movement,
    '/api/reports/registers_slow_moving': registers_slow_moving,
}
