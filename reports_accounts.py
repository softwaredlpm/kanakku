"""Account Books report group: Cash Book, Bank Book, Daily Summary, Account Monthly Summary,
Sales / Purchase Registers.

Pure functions over the read-only SQLite mirror of BUSY data (standard library only).
Sign conventions (see AI_DEVELOPER_GUIDE.md):
  busy_master1.OpBal  positive = Debit
  busy_tran2.Value1   (RecType 1) negative = Debit, positive = Credit
  Closing (Dr+) = OpBal - SUM(Value1)
Every handler: handler(conn, company, fy, params) -> dict
"""
import datetime

VCH_NAMES = {
    1: 'Opening Balance', 2: 'Purchase', 3: 'Sales Return', 4: 'Purchase Challan', 5: 'Stock Transfer',
    6: 'Production', 7: 'Material Receipt', 8: 'Stock Journal', 9: 'Sales', 10: 'Purchase Return',
    11: 'Delivery Challan', 12: 'Sales Order', 13: 'Purchase Order', 14: 'Receipt', 15: 'Contra',
    16: 'Journal', 17: 'Debit Note', 18: 'Credit Note', 19: 'Payment', 20: 'Production',
    21: 'Unassemble', 25: 'PDC Voucher', 26: 'Sales Quotation', 27: 'Purchase Quotation', 28: 'Salary',
    29: 'Purchase Indent',
}

CASH_GROUPS = (111,)
BANK_GROUPS = (112, 128)

REGISTER_TYPES = {
    'sales': 9, 'purchase': 2, 'sales_return': 3, 'purchase_return': 10,
}

MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


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
    fs, fe = _fy_bounds(fy)
    frm = _date(_p(params, 'from'), fs)
    to = _date(_p(params, 'to'), fe)
    if frm > to:
        frm, to = to, frm
    return frm, to


def _int(v, default=0):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _r2(x):
    return round(x or 0.0, 2)


def _drcr(bal):
    """Dr+ balance -> (abs amount, 'Dr'/'Cr')."""
    bal = _r2(bal)
    return abs(bal), ('Dr' if bal >= 0 else 'Cr')


def _vname(t):
    return VCH_NAMES.get(t, f'Type {t}')


def _group_accounts(conn, company, fy, roots):
    """Accounts (MasterType 2) under any of the root groups, including sub-groups."""
    out = []
    seen = set()
    for root in roots:
        rows = conn.execute(
            """WITH RECURSIVE gt(code) AS (
                   SELECT ? UNION
                   SELECT x.Code FROM busy_master1 x JOIN gt ON x.ParentGrp = gt.code
                   WHERE x.company_code = ? AND x.fy_year = ? AND x.MasterType = 1)
               SELECT m.Code, m.Name, COALESCE(m.OpBal, 0) AS OpBal, m.ParentGrp
               FROM busy_master1 m
               WHERE m.company_code = ? AND m.fy_year = ? AND m.MasterType = 2
                 AND m.ParentGrp IN (SELECT code FROM gt)
               ORDER BY m.Name""",
            (root, company, fy, company, fy)).fetchall()
        for r in rows:
            if r['Code'] not in seen:
                seen.add(r['Code'])
                out.append({'code': r['Code'], 'name': r['Name'], 'opbal': r['OpBal'], 'group': r['ParentGrp']})
    return out


def _account_names(conn, company, fy, codes):
    codes = list(set(codes))
    names = {}
    for i in range(0, len(codes), 500):
        chunk = codes[i:i + 500]
        q = ','.join('?' * len(chunk))
        for r in conn.execute(
                f"SELECT Code, Name FROM busy_master1 WHERE company_code = ? AND fy_year = ? "
                f"AND MasterType = 2 AND Code IN ({q})", [company, fy] + chunk):
            names[r['Code']] = r['Name']
    return names


def _in(codes):
    return ','.join('?' * len(codes))


# ---------------------------------------------------------------- cash / bank book

def _book(conn, company, fy, params, roots, title):
    accounts = _group_accounts(conn, company, fy, roots)
    frm, to = _range(params, fy)
    code = _int(_p(params, 'code', '0'))
    by_code = {a['code']: a for a in accounts}
    if code and code in by_code:
        selected = [by_code[code]]
    else:
        code = 0
        selected = accounts
    acc_list = [{'code': a['code'], 'name': a['name']} for a in accounts]
    base = {'title': title, 'company': company, 'fy': _fy_int(fy), 'from': frm, 'to': to,
            'code': code, 'account_name': selected[0]['name'] if code else 'All accounts (combined)',
            'accounts': acc_list}
    if not selected:
        base.update({'opening': 0, 'opening_type': 'Dr', 'rows': [], 'total_debit': 0, 'total_credit': 0,
                     'closing': 0, 'closing_type': 'Dr', 'per_account': []})
        return base

    sel_codes = [a['code'] for a in selected]
    q = _in(sel_codes)
    opbal = sum(a['opbal'] for a in selected)
    pre = {r[0]: (r[1] or 0.0) for r in conn.execute(
        f"""SELECT t.MasterCode1, SUM(t.Value1) FROM busy_tran2 t
            JOIN busy_tran1 h ON h.company_code = t.company_code AND h.fy_year = t.fy_year AND h.VchCode = t.VchCode
            WHERE t.company_code = ? AND t.fy_year = ? AND t.RecType = 1 AND h.Cancelled = 0
              AND h.Date < ? AND t.MasterCode1 IN ({q}) GROUP BY t.MasterCode1""",
        [company, fy, frm] + sel_codes)}
    opening = opbal - sum(pre.values())

    lines = conn.execute(
        f"""SELECT h.VchCode, h.Date, h.VchType, h.VchNo, h.MasterCode1 AS Party, t.MasterCode1 AS Acc,
                   SUM(CASE WHEN t.Value1 < 0 THEN -t.Value1 ELSE 0 END) AS Dr,
                   SUM(CASE WHEN t.Value1 > 0 THEN t.Value1 ELSE 0 END) AS Cr
            FROM busy_tran2 t
            JOIN busy_tran1 h ON h.company_code = t.company_code AND h.fy_year = t.fy_year AND h.VchCode = t.VchCode
            WHERE t.company_code = ? AND t.fy_year = ? AND t.RecType = 1 AND h.Cancelled = 0
              AND h.Date BETWEEN ? AND ? AND t.MasterCode1 IN ({q})
            GROUP BY h.VchCode, t.MasterCode1
            ORDER BY h.Date, h.VchType, h.VchCode, t.MasterCode1""",
        [company, fy, frm, to] + sel_codes).fetchall()

    # other accounts per voucher (particulars)
    vchs = sorted({r['VchCode'] for r in lines})
    others = {}
    for i in range(0, len(vchs), 500):
        chunk = vchs[i:i + 500]
        for r in conn.execute(
                f"""SELECT VchCode, MasterCode1, SUM(Value1) AS v FROM busy_tran2
                    WHERE company_code = ? AND fy_year = ? AND RecType = 1 AND VchCode IN ({_in(chunk)})
                    GROUP BY VchCode, MasterCode1 ORDER BY VchCode, MIN(SrNo)""",
                [company, fy] + chunk):
            others.setdefault(r['VchCode'], []).append((r['MasterCode1'], r['v'] or 0.0))
    name_codes = {c for lst in others.values() for c, _ in lst} | {r['Party'] for r in lines if r['Party']}
    names = _account_names(conn, company, fy, name_codes)

    rows = []
    bal = opening
    tdr = tcr = 0.0
    for r in lines:
        acc = r['Acc']
        other = [c for c, v in others.get(r['VchCode'], []) if c != acc and abs(v) > 0.004]
        if not other and r['Party'] and r['Party'] != acc:
            other = [r['Party']]
        pnames = [names.get(c, f'#{c}') for c in other]
        part = ', '.join(pnames[:2]) + (f' & {len(pnames) - 2} more' if len(pnames) > 2 else '')
        dr, cr = _r2(r['Dr']), _r2(r['Cr'])
        bal += r['Dr'] - r['Cr']
        tdr += r['Dr']; tcr += r['Cr']
        b, bt = _drcr(bal)
        rows.append({'date': r['Date'], 'vch_code': r['VchCode'], 'vch_type': r['VchType'],
                     'vch_type_name': _vname(r['VchType']), 'vch_no': (r['VchNo'] or '').strip(),
                     'account': acc, 'account_name': by_code.get(acc, {}).get('name', ''),
                     'particulars': part or '-', 'debit': dr, 'credit': cr, 'balance': b, 'balance_type': bt})

    # per-account summary (useful in combined mode)
    per = {}
    for a in selected:
        per[a['code']] = {'code': a['code'], 'name': a['name'], 'opening': a['opbal'] - pre.get(a['code'], 0.0),
                          'debit': 0.0, 'credit': 0.0}
    for r in lines:
        p = per[r['Acc']]; p['debit'] += r['Dr']; p['credit'] += r['Cr']
    per_list = []
    for p in per.values():
        cl = p['opening'] + p['debit'] - p['credit']
        o, ot = _drcr(p['opening']); c, ct = _drcr(cl)
        per_list.append({'code': p['code'], 'name': p['name'], 'opening': o, 'opening_type': ot,
                         'debit': _r2(p['debit']), 'credit': _r2(p['credit']), 'closing': c, 'closing_type': ct,
                         'closing_signed': _r2(cl)})

    o, ot = _drcr(opening)
    c, ct = _drcr(bal)
    base.update({'opening': o, 'opening_type': ot, 'opening_signed': _r2(opening), 'rows': rows,
                 'count': len(rows), 'total_debit': _r2(tdr), 'total_credit': _r2(tcr),
                 'closing': c, 'closing_type': ct, 'closing_signed': _r2(bal), 'per_account': per_list})
    return base


def accounts_cash_book(conn, company, fy, params):
    """params: code (0/omitted = all cash accounts combined), from, to."""
    return _book(conn, company, fy, params, CASH_GROUPS, 'Cash Book')


def accounts_bank_book(conn, company, fy, params):
    """params: code (0/omitted = all bank + bank OD accounts combined), from, to."""
    return _book(conn, company, fy, params, BANK_GROUPS, 'Bank Book')


# ---------------------------------------------------------------- daily summary

def accounts_daily_summary(conn, company, fy, params):
    """params: from, to (default whole FY). Per date: count + VchAmt per voucher type, cash/bank net (Dr+)."""
    frm, to = _range(params, fy)
    days = {}
    type_tot = {}
    for r in conn.execute(
            """SELECT Date, VchType, COUNT(*) AS n, SUM(VchAmt) AS amt FROM busy_tran1
               WHERE company_code = ? AND fy_year = ? AND Cancelled = 0 AND Date BETWEEN ? AND ?
               GROUP BY Date, VchType ORDER BY Date, VchType""", (company, fy, frm, to)):
        d = days.setdefault(r['Date'], {'date': r['Date'], 'types': [], 'count': 0, 'amount': 0.0,
                                         'cash_net': 0.0, 'bank_net': 0.0})
        amt = r['amt'] or 0.0
        d['types'].append({'vch_type': r['VchType'], 'name': _vname(r['VchType']), 'count': r['n'], 'amount': _r2(amt)})
        d['count'] += r['n']; d['amount'] += amt
        t = type_tot.setdefault(r['VchType'], {'vch_type': r['VchType'], 'name': _vname(r['VchType']),
                                               'count': 0, 'amount': 0.0})
        t['count'] += r['n']; t['amount'] += amt

    cash = [a['code'] for a in _group_accounts(conn, company, fy, CASH_GROUPS)]
    bank = [a['code'] for a in _group_accounts(conn, company, fy, BANK_GROUPS)]
    totals_net = {}
    for key, codes in (('cash_net', cash), ('bank_net', bank)):
        totals_net[key] = 0.0
        if not codes:
            continue
        for r in conn.execute(
                f"""SELECT h.Date, -SUM(t.Value1) AS net FROM busy_tran2 t
                    JOIN busy_tran1 h ON h.company_code = t.company_code AND h.fy_year = t.fy_year AND h.VchCode = t.VchCode
                    WHERE t.company_code = ? AND t.fy_year = ? AND t.RecType = 1 AND h.Cancelled = 0
                      AND h.Date BETWEEN ? AND ? AND t.MasterCode1 IN ({_in(codes)})
                    GROUP BY h.Date""", [company, fy, frm, to] + codes):
            d = days.get(r['Date'])
            if d is not None:
                d[key] += r['net'] or 0.0
            totals_net[key] += r['net'] or 0.0

    out_days = []
    for d in sorted(days.values(), key=lambda x: x['date']):
        d['amount'] = _r2(d['amount']); d['cash_net'] = _r2(d['cash_net']); d['bank_net'] = _r2(d['bank_net'])
        out_days.append(d)
    types = [dict(t, amount=_r2(t['amount'])) for t in sorted(type_tot.values(), key=lambda x: x['vch_type'])]
    return {'title': 'Daily Summary', 'company': company, 'fy': _fy_int(fy), 'from': frm, 'to': to,
            'days': out_days, 'type_totals': types,
            'grand_count': sum(t['count'] for t in types), 'grand_amount': _r2(sum(t['amount'] for t in types)),
            'cash_net': _r2(totals_net['cash_net']), 'bank_net': _r2(totals_net['bank_net'])}


# ---------------------------------------------------------------- account monthly summary

def accounts_monthly_summary(conn, company, fy, params):
    """params: code (account). Months Apr..Mar: opening, debit, credit, closing (Dr/Cr)."""
    y = _fy_int(fy)
    fs, fe = _fy_bounds(fy)
    code = _int(_p(params, 'code', '0'))
    acc = conn.execute("SELECT Code, Name, COALESCE(OpBal, 0) AS OpBal, ParentGrp FROM busy_master1 "
                       "WHERE company_code = ? AND fy_year = ? AND MasterType = 2 AND Code = ?",
                       (company, fy, code)).fetchone()
    if not acc:
        return {'title': 'Account Monthly Summary', 'company': company, 'fy': y, 'code': code,
                'error': 'Account not found', 'months': []}
    grp = conn.execute("SELECT Name FROM busy_master1 WHERE company_code = ? AND fy_year = ? AND Code = ?",
                       (company, fy, acc['ParentGrp'])).fetchone()
    pre = conn.execute(
        """SELECT SUM(t.Value1) FROM busy_tran2 t
           JOIN busy_tran1 h ON h.company_code = t.company_code AND h.fy_year = t.fy_year AND h.VchCode = t.VchCode
           WHERE t.company_code = ? AND t.fy_year = ? AND t.RecType = 1 AND h.Cancelled = 0
             AND t.MasterCode1 = ? AND h.Date < ?""", (company, fy, code, fs)).fetchone()[0] or 0.0
    mv = {r['ym']: (r['dr'] or 0.0, r['cr'] or 0.0) for r in conn.execute(
        """SELECT substr(h.Date, 1, 7) AS ym,
                  SUM(CASE WHEN t.Value1 < 0 THEN -t.Value1 ELSE 0 END) AS dr,
                  SUM(CASE WHEN t.Value1 > 0 THEN t.Value1 ELSE 0 END) AS cr
           FROM busy_tran2 t
           JOIN busy_tran1 h ON h.company_code = t.company_code AND h.fy_year = t.fy_year AND h.VchCode = t.VchCode
           WHERE t.company_code = ? AND t.fy_year = ? AND t.RecType = 1 AND h.Cancelled = 0
             AND t.MasterCode1 = ? AND h.Date BETWEEN ? AND ?
           GROUP BY ym""", (company, fy, code, fs, fe))}
    bal = (acc['OpBal'] or 0.0) - pre
    opening = bal
    months = []
    tdr = tcr = 0.0
    for i in range(12):
        m = (3 + i) % 12 + 1
        yy = y if m >= 4 else y + 1
        ym = f'{yy}-{m:02d}'
        dr, cr = mv.get(ym, (0.0, 0.0))
        o, ot = _drcr(bal)
        bal += dr - cr
        tdr += dr; tcr += cr
        c, ct = _drcr(bal)
        months.append({'month': ym, 'label': f'{MONTH_NAMES[m - 1]} {yy}', 'opening': o, 'opening_type': ot,
                       'debit': _r2(dr), 'credit': _r2(cr), 'closing': c, 'closing_type': ct})
    o, ot = _drcr(opening); c, ct = _drcr(bal)
    return {'title': 'Account Monthly Summary', 'company': company, 'fy': y, 'code': code,
            'account_name': acc['Name'], 'group_name': grp['Name'] if grp else '',
            'opening': o, 'opening_type': ot, 'opening_signed': _r2(opening), 'months': months,
            'total_debit': _r2(tdr), 'total_credit': _r2(tcr), 'closing': c, 'closing_type': ct,
            'closing_signed': _r2(bal)}


def accounts_search(conn, company, fy, params):
    """params: q (name LIKE, optional), limit (<= 50). Account picker."""
    q = str(_p(params, 'q', '')).strip()
    limit = max(1, min(50, _int(_p(params, 'limit', '50'), 50)))
    like = f'%{q}%'
    rows = conn.execute(
        """SELECT m.Code, m.Name, m.ParentGrp, g.Name AS GroupName FROM busy_master1 m
           LEFT JOIN busy_master1 g ON g.company_code = m.company_code AND g.fy_year = m.fy_year
                AND g.Code = m.ParentGrp AND g.MasterType = 1
           WHERE m.company_code = ? AND m.fy_year = ? AND m.MasterType = 2
             AND (m.Name LIKE ? OR COALESCE(m.Alias, '') LIKE ?)
           ORDER BY CASE WHEN m.Name LIKE ? THEN 0 ELSE 1 END, m.Name LIMIT ?""",
        (company, fy, like, like, f'{q}%', limit)).fetchall()
    return {'q': q, 'accounts': [{'code': r['Code'], 'name': r['Name'], 'group': r['ParentGrp'],
                                  'group_name': r['GroupName'] or ''} for r in rows]}


# ---------------------------------------------------------------- sales / purchase registers

def accounts_register(conn, company, fy, params):
    """params: type (sales|purchase|sales_return|purchase_return or 9/2/3/10), from, to."""
    t = str(_p(params, 'type', 'sales')).strip().lower()
    if t.isdigit() and int(t) in REGISTER_TYPES.values():
        vt = int(t)
    else:
        vt = REGISTER_TYPES.get(t, 9)
    frm, to = _range(params, fy)
    rows = conn.execute(
        """SELECT h.VchCode, h.Date, h.VchNo, h.MasterCode1 AS Party, COALESCE(h.VchAmt, 0) AS VchAmt,
                  g.taxable, g.tax, g.n AS gst_rows
           FROM busy_tran1 h
           LEFT JOIN (SELECT VchCode, COUNT(*) AS n, SUM(TaxableAmt) AS taxable,
                             SUM(COALESCE(TaxAmt, 0) + COALESCE(TaxAmt1, 0) + COALESCE(ACessAmt, 0)
                                 + COALESCE(StateCessAmt, 0)) AS tax
                      FROM busy_gst_itemwise WHERE company_code = ? AND fy_year = ? GROUP BY VchCode) g
                  ON g.VchCode = h.VchCode
           WHERE h.company_code = ? AND h.fy_year = ? AND h.Cancelled = 0 AND h.VchType = ?
             AND h.Date BETWEEN ? AND ?
           ORDER BY h.Date, h.VchCode""", (company, fy, company, fy, vt, frm, to)).fetchall()
    names = _account_names(conn, company, fy, [r['Party'] for r in rows if r['Party']])
    out, months = [], {}
    gt = {'count': 0, 'taxable': 0.0, 'tax': 0.0, 'total': 0.0}
    for r in rows:
        has = bool(r['gst_rows'])
        taxable = (r['taxable'] or 0.0) if has else None
        tax = (r['tax'] or 0.0) if has else None
        out.append({'vch_code': r['VchCode'], 'date': r['Date'], 'vch_no': (r['VchNo'] or '').strip(),
                    'party_code': r['Party'], 'party': names.get(r['Party'], ''),
                    'taxable': _r2(taxable) if has else None, 'tax': _r2(tax) if has else None,
                    'total': _r2(r['VchAmt'])})
        ym = r['Date'][:7]
        m = months.setdefault(ym, {'month': ym, 'label': f"{MONTH_NAMES[int(ym[5:7]) - 1]} {ym[:4]}",
                                   'count': 0, 'taxable': 0.0, 'tax': 0.0, 'total': 0.0})
        for agg in (m, gt):
            agg['count'] += 1
            agg['taxable'] += taxable or 0.0
            agg['tax'] += tax or 0.0
            agg['total'] += r['VchAmt']
    mlist = [{k: (_r2(v) if isinstance(v, float) else v) for k, v in m.items()} for m in
             sorted(months.values(), key=lambda x: x['month'])]
    return {'title': f'{_vname(vt)} Register', 'company': company, 'fy': _fy_int(fy), 'from': frm, 'to': to,
            'vch_type': vt, 'type_name': _vname(vt), 'rows': out, 'months': mlist,
            'grand': {k: (_r2(v) if isinstance(v, float) else v) for k, v in gt.items()},
            'without_gst': sum(1 for x in out if x['taxable'] is None)}


ROUTES = {
    '/api/reports/accounts_cash_book': accounts_cash_book,
    '/api/reports/accounts_bank_book': accounts_bank_book,
    '/api/reports/accounts_daily_summary': accounts_daily_summary,
    '/api/reports/accounts_monthly_summary': accounts_monthly_summary,
    '/api/reports/accounts_search': accounts_search,
    '/api/reports/accounts_register': accounts_register,
}
