"""GST report group (GSTR-1 / GSTR-3B / HSN summary) over BUSY's VchGSTSumItemWise mirror.

Source table: busy_gst_itemwise (one row per voucher item line, copied from BUSY VchGSTSumItemWise).
Field meanings established from the data (see scratch/test_reports_gst.py for the checks):
  * LocType 0 = intra-state (local): TaxRate/TaxAmt = CGST, TaxRate1/TaxAmt1 = SGST (equal halves).
    LocType 1 = inter-state (central): TaxRate/TaxAmt = IGST, TaxRate1/TaxAmt1 always 0.
  * ACessAmt / StateCessAmt = cess (always 0 in both companies).
  * ZeroTaxType 3 = taxable supply, 1 = nil / exempt / zero-rated line (rate 0), 0 = GST detail typed on a
    journal / debit note (VchType 16/17).
  * VchType 9 Sales, 3 Sales Return (credit note), 2 Purchase, 10 Purchase Return, 16 Journal with GST details,
    17 Debit Note. (No Sales Challan / VchType 11 rows: challans carry no GST.)
  * RecType 0 for invoices; on journals 61 = reverse-charge (RCM) inward supply (tax posted to the
    "... Output (RCM)" / "... Input Available (RCM)" ledgers), 63 = journal purchase/expense with GST details,
    54 = debit note.  RecType 63/54 vouchers in COMP0044 post NO tax to any GST ledger.
  * GSTIN = party GSTIN ('' = unregistered); TypeOfDealer 1 = regular, 2 = composition, 0 = unregistered/other.
  * PartyStateCode / POSStateCode = busy_master1 codes of MasterType 56 (state); TaxCatCode = MasterType 25.
  * Per voucher, the CGST/SGST/IGST in this table equals the RecType-1 lines posted to the CGST/SGST/IGST
    Output / Input ledgers exactly (checked for every sales/purchase/return voucher).
"""
import datetime
import re

OUT_TYPES = (9, 3)        # outward: sales (+), sales return / credit note (-)
IN_TYPES = (2, 10)        # inward: purchase (+), purchase return (-)
JRNL_TYPES = (16, 17, 18)  # journal / debit note / credit note carrying GST details
SIGN = {9: 1, 3: -1, 2: 1, 10: -1}
VCH_NAMES = {2: 'Purchase', 3: 'Sales Return', 9: 'Sales', 10: 'Purchase Return', 16: 'Journal',
             17: 'Debit Note', 18: 'Credit Note'}


def _p(params, key, default=''):
    v = params.get(key, [default]) if params else [default]
    return (v[0] if v else default) or default


def _period(fy, params):
    """Return (from, to) as YYYY-MM-DD. month=YYYY-MM wins over from/to; default = whole FY up to today."""
    try:
        fy = int(fy)
    except (TypeError, ValueError):
        fy = 2026
    fy_from, fy_to = '%d-04-01' % fy, '%d-03-31' % (fy + 1)
    month = _p(params, 'month')
    if re.fullmatch(r'\d{4}-\d{2}', month or ''):
        y, m = int(month[:4]), int(month[5:])
        if 1 <= m <= 12:
            nxt = datetime.date(y + (m == 12), m % 12 + 1, 1)
            return '%04d-%02d-01' % (y, m), (nxt - datetime.timedelta(days=1)).isoformat()
    d_from, d_to = _p(params, 'from'), _p(params, 'to')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', d_from or ''):
        d_from = fy_from
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', d_to or ''):
        d_to = min(datetime.date.today().isoformat(), fy_to)
    return d_from, d_to


def _r(x):
    return round(x or 0.0, 2)


def _rows(conn, company, fy, d_from, d_to, types):
    ph = ','.join('?' * len(types))
    sql = f"""
        SELECT g.VchCode, g.VchType, g.VchDate, g.ItemCode, g.PartyCode, g.HSNCode, g.TaxableAmt, g.TaxRate,
               g.TaxRate1, g.TaxAmt, g.TaxAmt1, g.ACessAmt, g.StateCessAmt, g.LocType, g.ZeroTaxType,
               g.ItemMainQty, g.RecType, g.TypeOfDealer, g.POSStateCode, g.GSTIN,
               h.VchNo, h.VchAmt, h.MasterCode1 AS HdrParty
        FROM busy_gst_itemwise g
        JOIN busy_tran1 h ON h.company_code = g.company_code AND h.fy_year = g.fy_year AND h.VchCode = g.VchCode
        WHERE g.company_code = ? AND g.fy_year = ? AND h.Cancelled = 0
          AND h.Date BETWEEN ? AND ? AND g.VchType IN ({ph})
        ORDER BY h.Date, g.VchCode, g.ItemSrNo"""
    out = []
    for r in conn.execute(sql, (company, fy, d_from, d_to) + tuple(types)):
        d = dict(r)
        loc = d['LocType'] or 0
        ta, ta1 = d['TaxAmt'] or 0.0, d['TaxAmt1'] or 0.0
        d['cgst'] = ta if loc == 0 else 0.0
        d['sgst'] = ta1 if loc == 0 else 0.0
        d['igst'] = ta + ta1 if loc == 1 else 0.0
        d['cess'] = (d['ACessAmt'] or 0.0) + (d['StateCessAmt'] or 0.0)
        d['rate'] = round((d['TaxRate'] or 0.0) + (d['TaxRate1'] or 0.0), 3)
        d['taxable'] = d['TaxableAmt'] or 0.0
        d['GSTIN'] = (d['GSTIN'] or '').strip()
        d['VchNo'] = (d['VchNo'] or '').strip()
        out.append(d)
    return out


def _names(conn, company, fy, codes):
    codes = sorted({c for c in codes if c})
    res = {}
    for i in range(0, len(codes), 500):
        chunk = codes[i:i + 500]
        sql = ('SELECT Code, Name FROM busy_master1 WHERE company_code = ? AND fy_year = ? AND Code IN (%s)'
               % ','.join('?' * len(chunk)))
        for r in conn.execute(sql, [company, fy] + chunk):
            res[r['Code']] = r['Name']
    return res


def _zero():
    return {'taxable': 0.0, 'cgst': 0.0, 'sgst': 0.0, 'igst': 0.0, 'cess': 0.0}


def _add(acc, d, s=1):
    for k in ('taxable', 'cgst', 'sgst', 'igst', 'cess'):
        acc[k] += s * d[k]


def _fin(acc):
    o = {k: _r(v) for k, v in acc.items() if isinstance(v, float)}
    o.update({k: v for k, v in acc.items() if not isinstance(v, float)})
    o['tax'] = _r(acc['cgst'] + acc['sgst'] + acc['igst'] + acc['cess'])
    o['total'] = _r(acc['taxable'] + acc['cgst'] + acc['sgst'] + acc['igst'] + acc['cess'])
    return o


def _vouchers(rows, names, sign_map=None):
    """Collapse item rows into one record per voucher."""
    vch = {}
    for d in rows:
        v = vch.get(d['VchCode'])
        if v is None:
            party = d['HdrParty'] or d['PartyCode']
            v = vch[d['VchCode']] = dict(_zero(), vch_code=d['VchCode'], vch_type=d['VchType'],
                                         vch_type_name=VCH_NAMES.get(d['VchType'], str(d['VchType'])),
                                         vch_no=d['VchNo'], date=d['VchDate'], party=names.get(party, ''),
                                         gstin=d['GSTIN'], pos=names.get(d['POSStateCode'], ''),
                                         inter_state=bool(d['LocType']), nil_value=0.0,
                                         vch_amt=_r(d['VchAmt']), rates=set())
        _add(v, d)
        if not v['gstin'] and d['GSTIN']:
            v['gstin'] = d['GSTIN']
        if d['rate'] == 0:
            v['nil_value'] += d['taxable']
        v['rates'].add(d['rate'])
    out = []
    for v in vch.values():
        v['rates'] = sorted(v['rates'])
        out.append(_fin(v))
    return out


def _sum(items, keys=('taxable', 'cgst', 'sgst', 'igst', 'cess')):
    acc = _zero()
    for it in items:
        for k in keys:
            acc[k] += it[k]
    return _fin(acc)


def _neg(v):
    o = dict(v)
    for k in ('taxable', 'cgst', 'sgst', 'igst', 'cess', 'tax', 'total', 'nil_value'):
        if k in o:
            o[k] = _r(-o[k])
    return o


# ---------------------------------------------------------------- GST ledger accounts
def _gst_accounts(conn, company, fy):
    """Find CGST/SGST/IGST/Cess Output/Input (incl. RCM) ledger accounts by name."""
    accs = []
    for r in conn.execute("SELECT Code, Name, OpBal FROM busy_master1 WHERE company_code = ? AND fy_year = ? "
                          "AND MasterType = 2", (company, fy)):
        n = (r['Name'] or '').strip()
        m = re.match(r'^(Add\. Cess|Cess|CGST|SGST|IGST)\b.*?\b(Output|Input)\b', n, re.I)
        if not m or re.search(r'Adjustable|Refundable|TDS|TCS', n, re.I):
            continue
        head = m.group(1).upper()
        head = 'CESS' if 'CESS' in head else head
        accs.append({'code': r['Code'], 'name': n, 'head': head.lower(), 'kind': m.group(2).lower(),
                     'rcm': 'RCM' in n.upper(), 'opbal': r['OpBal'] or 0.0})
    return accs


def _ledger_movement(conn, company, fy, d_from, d_to, accs):
    if not accs:
        return []
    codes = [a['code'] for a in accs]
    ph = ','.join('?' * len(codes))
    mv = {}
    for r in conn.execute(f"""
            SELECT t.MasterCode1 AS code, h.VchType AS vt,
                   SUM(CASE WHEN t.Value1 < 0 THEN -t.Value1 ELSE 0 END) AS dr,
                   SUM(CASE WHEN t.Value1 > 0 THEN t.Value1 ELSE 0 END) AS cr
            FROM busy_tran2 t JOIN busy_tran1 h
              ON h.company_code = t.company_code AND h.fy_year = t.fy_year AND h.VchCode = t.VchCode
            WHERE t.company_code = ? AND t.fy_year = ? AND t.RecType = 1 AND h.Cancelled = 0
              AND h.Date BETWEEN ? AND ? AND t.MasterCode1 IN ({ph})
            GROUP BY 1, 2""", [company, fy, d_from, d_to] + codes):
        mv.setdefault(r['code'], {})[r['vt']] = (r['dr'] or 0.0, r['cr'] or 0.0)
    out = []
    for a in accs:
        by = mv.get(a['code'], {})
        dr = sum(x[0] for x in by.values())
        cr = sum(x[1] for x in by.values())
        if not by and abs(a['opbal']) < 0.005:
            continue
        out.append({'code': a['code'], 'name': a['name'], 'head': a['head'], 'kind': a['kind'], 'rcm': a['rcm'],
                    'debit': _r(dr), 'credit': _r(cr),
                    'by_vch_type': [{'vch_type': vt, 'vch_type_name': VCH_NAMES.get(vt, str(vt)),
                                     'debit': _r(x[0]), 'credit': _r(x[1])} for vt, x in sorted(by.items())],
                    # movement from invoices / returns only (excludes set-off & payment journals)
                    'invoice_net_cr': _r(sum(x[1] - x[0] for vt, x in by.items() if vt in OUT_TYPES + IN_TYPES))})
    return out


# ---------------------------------------------------------------- 1. GSTR-1
def gst_gstr1(conn, company, fy, params):
    d_from, d_to = _period(fy, params)
    rows = _rows(conn, company, fy, d_from, d_to, OUT_TYPES)
    names = _names(conn, company, fy, [r['HdrParty'] for r in rows] + [r['PartyCode'] for r in rows]
                   + [r['POSStateCode'] for r in rows])
    sales = [r for r in rows if r['VchType'] == 9]
    rets = [r for r in rows if r['VchType'] == 3]

    b2b = [v for v in _vouchers([r for r in sales if r['GSTIN']], names)]
    b2b.sort(key=lambda v: (v['date'], v['vch_no']))

    b2c_map = {}
    for r in sales:
        if r['GSTIN']:
            continue
        key = (r['rate'], int(r['LocType'] or 0), names.get(r['POSStateCode'], ''))
        a = b2c_map.setdefault(key, dict(_zero(), vouchers=set()))
        _add(a, r)
        a['vouchers'].add(r['VchCode'])
    b2c = []
    for (rate, loc, pos), a in sorted(b2c_map.items()):
        o = _fin({k: v for k, v in a.items() if k != 'vouchers'})
        o.update(rate=rate, inter_state=bool(loc), pos=pos, vouchers=len(a['vouchers']))
        b2c.append(o)

    cdn = [_neg(v) for v in _vouchers(rets, names)]
    for v in cdn:
        v['note_type'] = 'C'
        v['section'] = 'CDNR' if v['gstin'] else 'CDNUR'
    cdn.sort(key=lambda v: (v['date'], v['vch_no']))

    nil = _zero()
    for r in sales:
        if r['rate'] == 0:
            _add(nil, r)
    t_b2b, t_b2c, t_cdn = _sum(b2b), _sum(b2c), _sum(cdn)
    t_all = _sum(sales)
    net = _sum([t_b2b, t_b2c, t_cdn])
    return {
        'report': 'GSTR-1 (outward supplies)', 'company': company, 'fy': fy, 'from': d_from, 'to': d_to,
        'b2b': b2b, 'b2c': b2c, 'cdn': cdn,
        'totals': {
            'b2b': dict(t_b2b, invoices=len(b2b)),
            'b2c': dict(t_b2c, invoices=len({r['VchCode'] for r in sales if not r['GSTIN']})),
            'outward': dict(t_all, invoices=len({r['VchCode'] for r in sales})),
            'credit_notes': dict(t_cdn, notes=len(cdn)),
            'net_outward': net,
            'nil_exempt_included': _fin(nil),
        },
        'notes': ['B2B = sales invoices whose party has a GSTIN; B2C = no GSTIN (grouped by rate / place of supply).',
                  'Credit notes = Sales Return vouchers (VchType 3), shown negative.',
                  'Rate-0 (nil / exempt) lines are included in B2B/B2C taxable value; nil_exempt_included shows them.'],
    }


# ---------------------------------------------------------------- 2. GSTR-3B
def gst_gstr3b(conn, company, fy, params):
    d_from, d_to = _period(fy, params)
    out_rows = _rows(conn, company, fy, d_from, d_to, OUT_TYPES)
    in_rows = _rows(conn, company, fy, d_from, d_to, IN_TYPES)
    jr_rows = _rows(conn, company, fy, d_from, d_to, JRNL_TYPES)

    def bucket(rows, pred):
        a = _zero()
        for r in rows:
            if pred(r):
                _add(a, r, SIGN.get(r['VchType'], 1))
        return _fin(a)

    outward_taxable = bucket(out_rows, lambda r: r['rate'] > 0)
    outward_nil = bucket(out_rows, lambda r: r['rate'] == 0)
    outward_sales = bucket([r for r in out_rows if r['VchType'] == 9], lambda r: True)
    outward_returns = bucket([r for r in out_rows if r['VchType'] == 3], lambda r: True)
    itc_purch = bucket([r for r in in_rows if r['VchType'] == 2], lambda r: True)
    itc_pret = bucket([r for r in in_rows if r['VchType'] == 10], lambda r: True)
    itc_net = bucket(in_rows, lambda r: r['rate'] > 0)
    inward_nil = bucket(in_rows, lambda r: r['rate'] == 0)
    rcm = bucket(jr_rows, lambda r: r['RecType'] == 61)

    # journals / notes that carry GST details: are their taxes posted to GST ledgers?
    accs = _gst_accounts(conn, company, fy)
    acc_codes = {a['code'] for a in accs}
    names = _names(conn, company, fy, [r['HdrParty'] for r in jr_rows] + [r['PartyCode'] for r in jr_rows])
    jv = _vouchers(jr_rows, names)
    posted = set()
    if jv and acc_codes:
        ph = ','.join('?' * len(acc_codes))
        vph = ','.join('?' * len(jv))
        for r in conn.execute(f"SELECT DISTINCT VchCode FROM busy_tran2 WHERE company_code = ? AND fy_year = ? "
                              f"AND RecType = 1 AND MasterCode1 IN ({ph}) AND VchCode IN ({vph})",
                              [company, fy] + sorted(acc_codes) + [v['vch_code'] for v in jv]):
            posted.add(r['VchCode'])
    rt = {}
    for r in jr_rows:
        rt.setdefault(r['VchCode'], set()).add(r['RecType'])
    for v in jv:
        v['posted_to_gst_ledgers'] = v['vch_code'] in posted
        v['gst_rec_types'] = sorted(rt.get(v['vch_code'], ()))
        v['is_rcm'] = 61 in v['gst_rec_types']
    jv.sort(key=lambda v: (v['date'], v['vch_no']))
    other_j = [v for v in jv if not v['is_rcm']]

    heads = ('cgst', 'sgst', 'igst', 'cess')
    output_tax = {h: _r(outward_taxable[h] + outward_nil[h]) for h in heads}
    itc_total = {h: _r(itc_net[h] + inward_nil[h] + rcm[h]) for h in heads}
    rcm_liab = {h: rcm[h] for h in heads}
    payable = {h: _r(output_tax[h] + rcm_liab[h] - itc_total[h]) for h in heads}
    payable['total'] = _r(sum(payable[h] for h in heads))

    return {
        'report': 'GSTR-3B summary', 'company': company, 'fy': fy, 'from': d_from, 'to': d_to,
        'outward': {'taxable': outward_taxable, 'nil_exempt': outward_nil,
                    'sales': outward_sales, 'sales_returns': outward_returns,
                    'net': _sum([outward_taxable, outward_nil])},
        'rcm_inward': rcm,
        'itc': {'purchases': itc_purch, 'purchase_returns': itc_pret, 'net_purchases': _sum([itc_net, inward_nil]),
                'rcm': rcm, 'total': dict(itc_total, tax=_r(sum(itc_total.values())))},
        'inward_nil_exempt': inward_nil,
        'output_tax': dict(output_tax, tax=_r(sum(output_tax.values()))),
        'rcm_liability': dict(rcm_liab, tax=_r(sum(rcm_liab.values()))),
        'net_payable': payable,
        'journals_with_gst': jv,
        'journals_not_in_itc': _sum(other_j) if other_j else _fin(_zero()),
        'ledgers': _ledger_movement(conn, company, fy, d_from, d_to, accs),
        'notes': ['Output = Sales - Sales Returns; ITC = Purchases - Purchase Returns + RCM journals (RecType 61).',
                  'Net payable per head = output tax + RCM liability - ITC (before set-off of opening credit).',
                  'Other journals / debit notes with GST details are listed but excluded from ITC; '
                  'posted_to_gst_ledgers tells whether BUSY actually booked their tax.'],
    }


# ---------------------------------------------------------------- 3. HSN summary
def gst_hsn(conn, company, fy, params):
    d_from, d_to = _period(fy, params)
    rows = _rows(conn, company, fy, d_from, d_to, OUT_TYPES + IN_TYPES)
    items = sorted({r['ItemCode'] for r in rows if r['ItemCode']})
    unit_of = {}
    for i in range(0, len(items), 500):
        chunk = items[i:i + 500]
        sql = ('SELECT i.Code, u.Name FROM busy_master1 i LEFT JOIN busy_master1 u ON u.company_code = i.company_code '
               'AND u.fy_year = i.fy_year AND u.Code = i.CM1 WHERE i.company_code = ? AND i.fy_year = ? '
               'AND i.Code IN (%s)' % ','.join('?' * len(chunk)))
        for r in conn.execute(sql, [company, fy] + chunk):
            unit_of[r['Code']] = (r['Name'] or '').strip()

    def build(sel):
        m = {}
        for r in rows:
            if r['VchType'] not in sel:
                continue
            s = SIGN[r['VchType']]
            key = ((r['HSNCode'] or '').strip(), r['rate'])
            a = m.setdefault(key, dict(_zero(), qty=0.0, units=set(), item_set=set()))
            _add(a, r, s)
            a['qty'] += s * (r['ItemMainQty'] or 0.0)
            if unit_of.get(r['ItemCode']):
                a['units'].add(unit_of[r['ItemCode']])
            a['item_set'].add(r['ItemCode'])
        out = []
        for (hsn, rate), a in sorted(m.items()):
            o = _fin({k: v for k, v in a.items() if k not in ('units', 'item_set')})
            o.update(hsn=hsn or '(blank)', rate=rate, qty=round(a['qty'], 3), uqc=', '.join(sorted(a['units'])),
                     items=len(a['item_set']))
            out.append(o)
        return out

    outward, inward = build(OUT_TYPES), build(IN_TYPES)
    return {'report': 'HSN-wise summary', 'company': company, 'fy': fy, 'from': d_from, 'to': d_to,
            'outward': outward, 'inward': inward,
            'totals': {'outward': dict(_sum(outward), qty=round(sum(o['qty'] for o in outward), 3)),
                       'inward': dict(_sum(inward), qty=round(sum(o['qty'] for o in inward), 3))},
            'notes': ['Outward = Sales - Sales Returns; Inward = Purchases - Purchase Returns. Grouped by HSN + rate '
                      '(rate = CGST+SGST or IGST %). qty in each item\'s main unit (uqc lists the units).']}


ROUTES = {
    '/api/reports/gst-gstr1': gst_gstr1,
    '/api/reports/gst-gstr3b': gst_gstr3b,
    '/api/reports/gst-hsn': gst_hsn,
}
