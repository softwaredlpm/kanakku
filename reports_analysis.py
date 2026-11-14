"""Analysis & Final Results report group: Party-wise Sales/Purchase, Item-wise Sales/Purchase,
Month-wise summary, Group Summary, Profit & Loss account.

Pure functions over the read-only SQLite mirror of BUSY data (standard library only, no import of server).
Sign conventions (see AI_DEVELOPER_GUIDE.md):
  busy_master1.OpBal  positive = Debit
  busy_tran2.Value1   (RecType 1) negative = Debit, positive = Credit
  Closing (Dr+) = OpBal - SUM(Value1)
Every handler: handler(conn, company, fy, params) -> dict
"""
import datetime

PARTY_TYPES = {'sales': (9, 'Sales'), 'purchase': (2, 'Purchase')}
# Item lines: RecType 2 = direct (stock-affecting) lines; RecType 7 = lines billed against a challan.
ITEM_TYPES = {'sales': (9, 'Sales'), 'purchase': (2, 'Purchase')}
MONTH_TYPES = [(9, 'sales', 'Sales'), (2, 'purchase', 'Purchase'), (14, 'receipt', 'Receipt'), (19, 'payment', 'Payment')]
MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

REVENUE_ROOT = 109
# Children of Revenue Accounts (109) and where they sit in the Trading / P&L account.
PL_SECTIONS = {
    122: ('trading', 'dr'),   # Purchase
    124: ('trading', 'dr'),   # Expenses (Direct/Mfg.)
    123: ('trading', 'cr'),   # Sale
    126: ('trading', 'cr'),   # Income (Direct/Opr.)
    125: ('pl', 'dr'),        # Expenses (Indirect/Admn.)
    127: ('pl', 'cr'),        # Income (Indirect)
}


# ---------------------------------------------------------------- helpers

def _p(params, key, default=''):
    v = (params or {}).get(key)
    if not v:
        return default
    v = v[0] if isinstance(v, (list, tuple)) else v
    return v if v not in (None, '') else default


def _fy_int(fy):
    try:
        return int(fy)
    except (TypeError, ValueError):
        return 2026


def _fy_bounds(fy):
    y = _fy_int(fy)
    return f'{y}-04-01', f'{y + 1}-03-31'


def _date(v, default):
    try:
        datetime.date.fromisoformat(str(v))
        return str(v)
    except (TypeError, ValueError):
        return default


def _range(params, fy):
    fy_from, fy_to = _fy_bounds(fy)
    d_from = _date(_p(params, 'from'), fy_from)
    d_to = _date(_p(params, 'to'), fy_to)
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    return d_from, d_to


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _r(x, n=2):
    return round(x or 0.0, n)


def _drcr(amount):
    """Signed Dr+ amount -> (abs amount, 'Dr'/'Cr')."""
    a = round(amount or 0.0, 2)
    return abs(a), ('Dr' if a >= 0 else 'Cr')


def _type_param(params, table):
    t = str(_p(params, 'type', 'sales')).lower()
    if t not in table:
        t = 'sales'
    return t, table[t][0], table[t][1]


# ---------------------------------------------------------------- 1. party-wise sales / purchase

def analysis_partywise(conn, company, fy, params):
    """Per party (Tran1.MasterCode1): invoice count, item qty, net amount (VchAmt), share %."""
    t, vtype, label = _type_param(params, PARTY_TYPES)
    d_from, d_to = _range(params, fy)
    c = conn.cursor()
    c.execute('''
        SELECT t1.MasterCode1 AS Code, COALESCE(m.Name, '(Unknown party)') AS Name,
               COUNT(*) AS Cnt, COALESCE(SUM(t1.VchAmt), 0) AS Amt
        FROM busy_tran1 t1
        LEFT JOIN busy_master1 m ON m.company_code = t1.company_code AND m.fy_year = t1.fy_year AND m.Code = t1.MasterCode1
        WHERE t1.company_code = ? AND t1.fy_year = ? AND t1.VchType = ? AND t1.Cancelled = 0
          AND t1.Date BETWEEN ? AND ?
        GROUP BY t1.MasterCode1
    ''', (company, fy, vtype, d_from, d_to))
    rows = {r['Code']: {'code': r['Code'], 'name': r['Name'], 'invoices': r['Cnt'],
                        'amount': r['Amt'] or 0.0, 'qty': 0.0} for r in c.fetchall()}
    c.execute('''
        SELECT t1.MasterCode1 AS Code, COALESCE(SUM(ABS(t2.Value1)), 0) AS Qty
        FROM busy_tran1 t1
        JOIN busy_tran2 t2 ON t2.company_code = t1.company_code AND t2.fy_year = t1.fy_year AND t2.VchCode = t1.VchCode
        WHERE t1.company_code = ? AND t1.fy_year = ? AND t1.VchType = ? AND t1.Cancelled = 0
          AND t1.Date BETWEEN ? AND ? AND t2.RecType IN (2, 7)
        GROUP BY t1.MasterCode1
    ''', (company, fy, vtype, d_from, d_to))
    for r in c.fetchall():
        if r['Code'] in rows:
            rows[r['Code']]['qty'] = r['Qty'] or 0.0
    total = sum(r['amount'] for r in rows.values())
    out = []
    for r in sorted(rows.values(), key=lambda x: -x['amount']):
        out.append({'code': r['code'], 'name': r['name'], 'invoices': r['invoices'],
                    'qty': _r(r['qty'], 3), 'amount': _r(r['amount']),
                    'share': _r(r['amount'] * 100.0 / total if total else 0.0)})
    return {'report': 'partywise', 'type': t, 'type_label': label, 'vch_type': vtype,
            'from': d_from, 'to': d_to, 'parties': len(out),
            'total_invoices': sum(r['invoices'] for r in out),
            'total_qty': _r(sum(r['qty'] for r in out), 3),
            'total_amount': _r(total), 'rows': out,
            'note': 'Amount = voucher net amount (Tran1.VchAmt); qty = item lines (direct + against challan) in each item\'s main unit (mixed units are summed as-is).'}


def analysis_party_invoices(conn, company, fy, params):
    """Drill-down: invoices of one party for the type and date range."""
    t, vtype, label = _type_param(params, PARTY_TYPES)
    d_from, d_to = _range(params, fy)
    code = _int(_p(params, 'code', '0'))
    c = conn.cursor()
    c.execute('SELECT Name FROM busy_master1 WHERE company_code = ? AND fy_year = ? AND Code = ?', (company, fy, code))
    nm = c.fetchone()
    c.execute('''
        SELECT t1.VchCode, t1.Date, t1.VchNo, t1.VchAmt,
               (SELECT COALESCE(SUM(ABS(t2.Value1)), 0) FROM busy_tran2 t2
                 WHERE t2.company_code = t1.company_code AND t2.fy_year = t1.fy_year
                   AND t2.VchCode = t1.VchCode AND t2.RecType IN (2, 7)) AS Qty
        FROM busy_tran1 t1
        WHERE t1.company_code = ? AND t1.fy_year = ? AND t1.VchType = ? AND t1.Cancelled = 0
          AND t1.MasterCode1 = ? AND t1.Date BETWEEN ? AND ?
        ORDER BY t1.Date, t1.VchCode
    ''', (company, fy, vtype, code, d_from, d_to))
    rows = [{'vch_code': r['VchCode'], 'date': r['Date'], 'vch_no': (r['VchNo'] or '').strip(),
             'qty': _r(r['Qty'], 3), 'amount': _r(r['VchAmt'])} for r in c.fetchall()]
    return {'report': 'party_invoices', 'type': t, 'type_label': label, 'code': code,
            'name': nm['Name'] if nm else '', 'from': d_from, 'to': d_to,
            'count': len(rows), 'total_amount': _r(sum(r['amount'] for r in rows)), 'rows': rows}


# ---------------------------------------------------------------- 2. item-wise sales / purchase

def analysis_itemwise(conn, company, fy, params):
    """Per item: qty in main unit, gross amount (busy_item_ext.GrossAmt, before bill sundries)
    and net amount (Tran2.Value3, after item-level discounts). Lines: RecType 2 (direct) + RecType 7
    (billed against challan)."""
    t, vtype, label = _type_param(params, ITEM_TYPES)
    d_from, d_to = _range(params, fy)
    c = conn.cursor()
    c.execute('''
        SELECT t2.MasterCode1 AS Code, COALESCE(m.Name, '(Unknown item)') AS Name,
               COALESCE(u.Name, '') AS Unit, t2.RecType AS RecType,
               COUNT(*) AS Lines,
               COALESCE(SUM(ABS(t2.Value1)), 0) AS Qty,
               COALESCE(SUM(CASE WHEN COALESCE(x.GrossAmt, 0) <> 0 THEN ABS(x.GrossAmt) ELSE ABS(t2.Value3) END), 0) AS Gross,
               COALESCE(SUM(ABS(t2.Value3)), 0) AS Net
        FROM busy_tran1 t1
        JOIN busy_tran2 t2 ON t2.company_code = t1.company_code AND t2.fy_year = t1.fy_year AND t2.VchCode = t1.VchCode
        LEFT JOIN busy_master1 m ON m.company_code = t2.company_code AND m.fy_year = t2.fy_year AND m.Code = t2.MasterCode1
        LEFT JOIN busy_master1 u ON u.company_code = m.company_code AND u.fy_year = m.fy_year AND u.Code = m.CM1
        LEFT JOIN busy_item_ext x ON x.company_code = t2.company_code AND x.fy_year = t2.fy_year
                                 AND x.VchCode = t2.VchCode AND x.SrNo = t2.SrNo AND x.RecType = t2.RecType
        WHERE t1.company_code = ? AND t1.fy_year = ? AND t1.VchType = ? AND t1.Cancelled = 0
          AND t1.Date BETWEEN ? AND ? AND t2.RecType IN (2, 7)
        GROUP BY t2.MasterCode1, t2.RecType
    ''', (company, fy, vtype, d_from, d_to))
    items = {}
    split = {2: {'lines': 0, 'gross': 0.0, 'net': 0.0}, 7: {'lines': 0, 'gross': 0.0, 'net': 0.0}}
    for r in c.fetchall():
        it = items.setdefault(r['Code'], {'code': r['Code'], 'name': r['Name'], 'unit': r['Unit'],
                                          'lines': 0, 'qty': 0.0, 'gross': 0.0, 'net': 0.0})
        it['lines'] += r['Lines']; it['qty'] += r['Qty']; it['gross'] += r['Gross']; it['net'] += r['Net']
        s = split[r['RecType']]
        s['lines'] += r['Lines']; s['gross'] += r['Gross']; s['net'] += r['Net']
    total_gross = sum(i['gross'] for i in items.values())
    rows = []
    for i in sorted(items.values(), key=lambda x: -x['gross']):
        rows.append({'code': i['code'], 'name': i['name'], 'unit': i['unit'], 'lines': i['lines'],
                     'qty': _r(i['qty'], 3), 'gross_amount': _r(i['gross']), 'net_amount': _r(i['net']),
                     'avg_rate': _r(i['gross'] / i['qty'] if i['qty'] else 0.0),
                     'share': _r(i['gross'] * 100.0 / total_gross if total_gross else 0.0)})
    units = {}
    for r in rows:
        units[r['unit'] or '-'] = round(units.get(r['unit'] or '-', 0.0) + r['qty'], 3)
    return {'report': 'itemwise', 'type': t, 'type_label': label, 'vch_type': vtype,
            'from': d_from, 'to': d_to, 'items': len(rows),
            'total_gross': _r(total_gross), 'total_net': _r(sum(i['net'] for i in items.values())),
            'qty_by_unit': units,
            'direct_lines': {k: _r(v) if k != 'lines' else v for k, v in split[2].items()},
            'against_challan_lines': {k: _r(v) if k != 'lines' else v for k, v in split[7].items()},
            'rows': rows,
            'labels': {'gross_amount': 'Gross amount (qty x rate, before bill sundries / discounts)',
                       'net_amount': 'Net amount (item line value after item discounts, before bill sundries)'},
            'note': 'Item lines = RecType 2 (direct) + RecType 7 (billed against challan). '
                    'Gross + signed bill sundries = invoice total (VchAmt).'}


# ---------------------------------------------------------------- 3. month-wise summary

def analysis_monthwise(conn, company, fy, params):
    """Count + amount (VchAmt) per month for Sales / Purchase / Receipt / Payment."""
    d_from, d_to = _range(params, fy)
    y = _fy_int(fy)
    months = []
    for i in range(12):
        mm = (3 + i) % 12 + 1
        yy = y if mm >= 4 else y + 1
        months.append(f'{yy}-{mm:02d}')
    c = conn.cursor()
    c.execute('''
        SELECT substr(Date, 1, 7) AS Ym, VchType, COUNT(*) AS Cnt, COALESCE(SUM(VchAmt), 0) AS Amt
        FROM busy_tran1
        WHERE company_code = ? AND fy_year = ? AND Cancelled = 0 AND VchType IN (9, 2, 14, 19)
          AND Date BETWEEN ? AND ?
        GROUP BY Ym, VchType
    ''', (company, fy, d_from, d_to))
    data = {}
    for r in c.fetchall():
        data[(r['Ym'], r['VchType'])] = (r['Cnt'], r['Amt'] or 0.0)
    extra = sorted({ym for ym, _ in data} - set(months))
    rows = []
    totals = {key: {'count': 0, 'amount': 0.0} for _, key, _ in MONTH_TYPES}
    for ym in months + extra:
        row = {'month': ym, 'label': f"{MONTH_NAMES[int(ym[5:7]) - 1]} {ym[:4]}"}
        for vt, key, _ in MONTH_TYPES:
            cnt, amt = data.get((ym, vt), (0, 0.0))
            row[key] = {'count': cnt, 'amount': _r(amt)}
            totals[key]['count'] += cnt
            totals[key]['amount'] += amt
        rows.append(row)
    for v in totals.values():
        v['amount'] = _r(v['amount'])
    return {'report': 'monthwise', 'from': d_from, 'to': d_to,
            'types': [{'key': k, 'label': l, 'vch_type': vt} for vt, k, l in MONTH_TYPES],
            'rows': rows, 'totals': totals}


# ---------------------------------------------------------------- 4. group summary

def _group_tree(c, company, fy):
    c.execute('''SELECT Code, Name, ParentGrp FROM busy_master1
                 WHERE company_code = ? AND fy_year = ? AND MasterType = 1''', (company, fy))
    groups = {r['Code']: {'code': r['Code'], 'name': r['Name'], 'parent': r['ParentGrp'] or 0} for r in c.fetchall()}
    return groups


def _account_balances(c, company, fy, d_from, d_to):
    """Per account: opening at d_from (Dr+), debit and credit in [d_from, d_to], closing (Dr+)."""
    c.execute('''SELECT Code, Name, ParentGrp, COALESCE(OpBal, 0) AS OpBal FROM busy_master1
                 WHERE company_code = ? AND fy_year = ? AND MasterType = 2''', (company, fy))
    accts = {r['Code']: {'code': r['Code'], 'name': r['Name'], 'parent': r['ParentGrp'] or 0,
                         'opening': r['OpBal'] or 0.0, 'debit': 0.0, 'credit': 0.0} for r in c.fetchall()}
    c.execute('''
        SELECT t2.MasterCode1 AS Code,
               COALESCE(SUM(CASE WHEN t1.Date < ? THEN t2.Value1 ELSE 0 END), 0) AS Before,
               COALESCE(SUM(CASE WHEN t1.Date BETWEEN ? AND ? AND t2.Value1 < 0 THEN -t2.Value1 ELSE 0 END), 0) AS Dr,
               COALESCE(SUM(CASE WHEN t1.Date BETWEEN ? AND ? AND t2.Value1 > 0 THEN t2.Value1 ELSE 0 END), 0) AS Cr
        FROM busy_tran2 t2
        JOIN busy_tran1 t1 ON t1.company_code = t2.company_code AND t1.fy_year = t2.fy_year AND t1.VchCode = t2.VchCode
        WHERE t2.company_code = ? AND t2.fy_year = ? AND t2.RecType = 1 AND t1.Cancelled = 0
        GROUP BY t2.MasterCode1
    ''', (d_from, d_from, d_to, d_from, d_to, company, fy))
    for r in c.fetchall():
        a = accts.get(r['Code'])
        if a is None:
            continue
        a['opening'] -= r['Before'] or 0.0
        a['debit'] += r['Dr'] or 0.0
        a['credit'] += r['Cr'] or 0.0
    for a in accts.values():
        a['closing'] = a['opening'] + a['debit'] - a['credit']
    return accts


def _rollup(groups, accts):
    """Totals per group including all sub-groups. Returns {group_code: {opening, debit, credit, closing, accounts}}."""
    tot = {g: {'opening': 0.0, 'debit': 0.0, 'credit': 0.0, 'closing': 0.0, 'accounts': 0} for g in groups}
    for a in accts.values():
        g = a['parent']
        seen = set()
        while g in groups and g not in seen:
            seen.add(g)
            t = tot[g]
            for k in ('opening', 'debit', 'credit', 'closing'):
                t[k] += a[k]
            t['accounts'] += 1
            g = groups[g]['parent']
    return tot


def _bal_row(kind, code, name, v, extra=None):
    op, op_s = _drcr(v['opening'])
    cl, cl_s = _drcr(v['closing'])
    row = {'kind': kind, 'code': code, 'name': name,
           'opening': op, 'opening_side': op_s, 'opening_signed': _r(v['opening']),
           'debit': _r(v['debit']), 'credit': _r(v['credit']),
           'closing': cl, 'closing_side': cl_s, 'closing_signed': _r(v['closing'])}
    if extra:
        row.update(extra)
    return row


def _group_summary_data(conn, company, fy, code, d_from, d_to):
    c = conn.cursor()
    groups = _group_tree(c, company, fy)
    accts = _account_balances(c, company, fy, d_from, d_to)
    tot = _rollup(groups, accts)
    return groups, accts, tot


def analysis_group_summary(conn, company, fy, params):
    """Child groups (with rolled-up sub-group totals) and direct accounts of a group.
    code = 0 (default) lists the primary groups (ParentGrp 0) and the Difference in Opening Balance."""
    d_from, d_to = _range(params, fy)
    code = _int(_p(params, 'code', '0'))
    groups, accts, tot = _group_summary_data(conn, company, fy, code, d_from, d_to)
    if code and code not in groups:
        code = 0
    rows = []
    for g in sorted((g for g in groups.values() if g['parent'] == code and g['code'] != code), key=lambda x: x['name'] or ''):
        has_children = any(x['parent'] == g['code'] for x in groups.values()) or tot[g['code']]['accounts'] > 0
        rows.append(_bal_row('group', g['code'], g['name'], tot[g['code']],
                             {'accounts': tot[g['code']]['accounts'], 'drill': has_children}))
    if code:
        for a in sorted((a for a in accts.values() if a['parent'] == code), key=lambda x: x['name'] or ''):
            rows.append(_bal_row('account', a['code'], a['name'], a))
    total = {k: sum(r[k + '_signed'] if k in ('opening', 'closing') else r[k] for r in rows)
             for k in ('opening', 'debit', 'credit', 'closing')}
    path = []
    g = code
    seen = set()
    while g in groups and g not in seen:
        seen.add(g)
        path.insert(0, {'code': g, 'name': groups[g]['name']})
        g = groups[g]['parent']
    out = {'report': 'group_summary', 'code': code, 'name': groups[code]['name'] if code else 'All primary groups',
           'parent': groups[code]['parent'] if code else None, 'path': path,
           'from': d_from, 'to': d_to, 'rows': rows,
           'total': _bal_row('total', code, 'Total', total)}
    if code == 0:
        # Accounts whose group is missing from the group master would be dropped from the primary totals.
        orphan = [a for a in accts.values() if a['parent'] not in groups]
        all_open = sum(a['opening'] for a in accts.values())
        all_close = sum(a['closing'] for a in accts.values())
        diff_amt, diff_side = _drcr(all_open)
        out['difference_in_opening'] = {
            'label': 'Difference in Opening Balance', 'amount': diff_amt,
            # The balancing line sits on the opposite side of the excess.
            'excess_side': diff_side, 'balancing_side': 'Cr' if diff_side == 'Dr' else 'Dr',
            'opening_signed_total_all_accounts': _r(all_open),
            'closing_signed_total_all_accounts': _r(all_close),
            'orphan_accounts': len(orphan),
            'note': 'Sum of all account opening balances (Dr - Cr). Transactions always balance, so the '
                    'primary groups\' closing Dr - Cr equals the same difference.'}
    return out


# ---------------------------------------------------------------- 5. profit & loss

def _item_stock(c, company, fy, mc_code=0):
    """Copy of server.py compute_item_stock() (do not diverge): closing qty x average inward rate."""
    mc_sql = "AND MasterCode2 = ?" if mc_code > 0 else ""
    mc_args = [mc_code] if mc_code > 0 else []
    out = {}

    def row(code):
        return out.setdefault(code, {'OpQty': 0.0, 'OpValue': 0.0, 'MovementQty': 0.0,
                                     '_rq': 0.0, '_rv': 0.0})

    c.execute(f'''SELECT MasterCode1, SUM(D1), SUM(D3) FROM busy_tran4
                  WHERE company_code = ? AND fy_year = ? {mc_sql} GROUP BY MasterCode1''',
              [company, fy] + mc_args)
    for code, q, v in c.fetchall():
        r = row(code); r['OpQty'] = q or 0.0; r['OpValue'] = v or 0.0

    c.execute(f'''SELECT MasterCode1, SUM(Value1) FROM busy_tran2
                  WHERE company_code = ? AND fy_year = ? AND RecType = 2 {mc_sql} GROUP BY MasterCode1''',
              [company, fy] + mc_args)
    for code, q in c.fetchall():
        row(code)['MovementQty'] = q or 0.0

    c.execute('''SELECT MasterCode1, SUM(D1), SUM(D3) FROM busy_tran4
                 WHERE company_code = ? AND fy_year = ? GROUP BY MasterCode1''', (company, fy))
    for code, q, v in c.fetchall():
        r = row(code); r['_rq'] += q or 0.0; r['_rv'] += v or 0.0
    c.execute('''SELECT t2.MasterCode1, SUM(t2.Value1), SUM(t2.Value3)
                 FROM busy_tran2 t2
                 JOIN busy_tran1 t1 ON t1.company_code = t2.company_code AND t1.fy_year = t2.fy_year AND t1.VchCode = t2.VchCode
                 WHERE t2.company_code = ? AND t2.fy_year = ? AND t2.RecType = 2 AND t2.Value1 > 0
                   AND t1.VchType <> 5 AND t1.Cancelled = 0
                 GROUP BY t2.MasterCode1''', (company, fy))
    for code, q, v in c.fetchall():
        r = row(code); r['_rq'] += q or 0.0; r['_rv'] += v or 0.0

    for r in out.values():
        rq, rv = r.pop('_rq'), r.pop('_rv')
        rate = rv / rq if rq > 0 else 0.0
        r['ClosingQty'] = round(r['OpQty'] + r['MovementQty'], 3)
        r['AvgRate'] = round(rate, 2)
        r['ClosingValue'] = round(r['ClosingQty'] * rate, 2)
    return out


def analysis_pl(conn, company, fy, params):
    """Trading + Profit & Loss account for the whole FY data (no date filter: closing stock is the
    app's current stock valuation, which has no date filter either)."""
    fy_from, fy_to = _fy_bounds(fy)
    c = conn.cursor()
    groups = _group_tree(c, company, fy)
    accts = _account_balances(c, company, fy, fy_from, fy_to)
    tot = _rollup(groups, accts)

    def section_lines(gcode):
        lines = []
        for g in sorted((g for g in groups.values() if g['parent'] == gcode), key=lambda x: x['name'] or ''):
            if tot[g['code']]['accounts'] or abs(tot[g['code']]['closing']) > 0.005:
                lines.append({'kind': 'group', 'code': g['code'], 'name': g['name'],
                              'amount_signed': _r(tot[g['code']]['closing'])})
        for a in sorted((a for a in accts.values() if a['parent'] == gcode), key=lambda x: x['name'] or ''):
            if abs(a['closing']) > 0.005:
                lines.append({'kind': 'account', 'code': a['code'], 'name': a['name'],
                              'amount_signed': _r(a['closing'])})
        return lines

    heads = {'trading': {'dr': [], 'cr': []}, 'pl': {'dr': [], 'cr': []}}
    for g in sorted((g for g in groups.values() if g['parent'] == REVENUE_ROOT), key=lambda x: x['code']):
        bal = tot[g['code']]['closing']  # Dr+
        sect, side = PL_SECTIONS.get(g['code'], ('pl', 'dr' if bal >= 0 else 'cr'))
        # Amount shown on its natural side; a balance opposite to that side is shown negative.
        amount = bal if side == 'dr' else -bal
        heads[sect][side].append({'code': g['code'], 'name': g['name'], 'amount': _r(amount),
                                  'balance_signed': _r(bal), 'lines': section_lines(g['code'])})
    # Accounts sitting directly under 109 (no sub-group)
    for a in accts.values():
        if a['parent'] == REVENUE_ROOT and abs(a['closing']) > 0.005:
            side = 'dr' if a['closing'] >= 0 else 'cr'
            heads['pl'][side].append({'code': a['code'], 'name': a['name'], 'amount': _r(abs(a['closing'])),
                                      'balance_signed': _r(a['closing']), 'lines': []})

    stock = _item_stock(c, company, fy)
    c.execute('SELECT COALESCE(SUM(D3), 0) FROM busy_tran4 WHERE company_code = ? AND fy_year = ?', (company, fy))
    opening_stock = c.fetchone()[0] or 0.0
    closing_stock = sum(r['ClosingValue'] for r in stock.values())

    def s(lst):
        return sum(x['amount'] for x in lst)

    tr_dr = s(heads['trading']['dr']) + opening_stock
    tr_cr = s(heads['trading']['cr']) + closing_stock
    gross_profit = tr_cr - tr_dr
    pl_dr = s(heads['pl']['dr'])
    pl_cr = s(heads['pl']['cr'])
    net_profit = gross_profit + pl_cr - pl_dr

    income_total = s(heads['trading']['cr']) + s(heads['pl']['cr'])
    expense_total = s(heads['trading']['dr']) + s(heads['pl']['dr'])
    rev = tot.get(REVENUE_ROOT, {'closing': 0.0})
    warnings = []
    if opening_stock < 0:
        warnings.append('Opening stock (SUM of busy_tran4.D3) is negative in the BUSY data for this company.')
    if closing_stock < 0:
        warnings.append('Closing stock valuation is negative (negative closing quantities).')
    return {
        'report': 'profit_loss', 'from': fy_from, 'to': fy_to,
        'trading': {
            'dr': [{'code': 'OPSTOCK', 'name': 'Opening Stock', 'amount': _r(opening_stock), 'lines': []}] + heads['trading']['dr'],
            'cr': heads['trading']['cr'] + [{'code': 'CLSTOCK', 'name': 'Closing Stock (app valuation)',
                                             'amount': _r(closing_stock), 'lines': [], 'provisional': True}],
            'total_dr': _r(tr_dr), 'total_cr': _r(tr_cr)},
        'gross_profit': _r(gross_profit),
        'gross_result': 'Gross Profit' if gross_profit >= 0 else 'Gross Loss',
        'pl': {'dr': heads['pl']['dr'], 'cr': heads['pl']['cr'],
               'total_dr': _r(pl_dr), 'total_cr': _r(pl_cr)},
        'net_profit': _r(net_profit),
        'net_result': 'Net Profit' if net_profit >= 0 else 'Net Loss',
        'opening_stock': _r(opening_stock),
        'closing_stock': _r(closing_stock),
        'closing_stock_items': len(stock),
        'closing_stock_provisional': True,
        'closing_stock_note': 'Closing stock is the app\'s valuation (closing qty x average inward rate, same as the '
                              'Stock Status report), pending confirmation against BUSY\'s own closing stock.',
        'income_total': _r(income_total),
        'expense_total': _r(expense_total),
        'revenue_group_closing_signed': _r(rev['closing']),
        'reconciliation': {
            'income_minus_expense': _r(income_total - expense_total),
            'minus_revenue_group_closing': _r(-rev['closing']),
            'net_profit_check': _r(-rev['closing'] + closing_stock - opening_stock),
        },
        'warnings': warnings,
    }


ROUTES = {
    '/api/reports/analysis_partywise': analysis_partywise,
    '/api/reports/analysis_party_invoices': analysis_party_invoices,
    '/api/reports/analysis_itemwise': analysis_itemwise,
    '/api/reports/analysis_monthwise': analysis_monthwise,
    '/api/reports/analysis_group_summary': analysis_group_summary,
    '/api/reports/analysis_pl': analysis_pl,
}
