"""Inventory display reports (Item Stock Ledger, Item Group-wise Stock, Material Centre-wise Stock,
Pending Orders / Challans) over the busy_local.db mirror of BUSY data.

Pure functions, standard library only. Every handler has the signature
    handler(conn, company, fy, params) -> dict
where `conn` is a read-only sqlite3 connection (row_factory = sqlite3.Row) and `params` is the parsed
query string (dict[str, list[str]]).

Stock maths is a copy of server.compute_item_stock():
  qty   = busy_tran4.D1 (opening per item + material centre) + SUM(busy_tran2.Value1) of RecType 2 lines
  value = closing qty x average inward rate, rate = (Tran4.D3 + inward Value3) / (Tran4.D1 + inward Value1)
          over all material centres, inward = Value1 > 0, excluding VchType 5 (inter-godown transfers).

Order / challan references (busy_tran3), as found in the data:
  RecType 4 = order references.  Method 1 rows are created by Sales Orders (VchType 12, qty > 0) and
              Purchase Orders (VchType 13, qty < 0); opening pending orders carry VchCode -1200 (sales, > 0)
              and -1100 (purchase, < 0). Method 2 rows clear them: Sales Challans (11) against sales orders,
              Purchases (2) / Purchase Challans (4) against purchase orders, and VchCode -1300 "Ref. Cleared"
              (orders closed by hand in BUSY).
  RecType 3 = challan references. Method 1 rows are created by Delivery/Sales Challans (VchType 11, qty < 0)
              and Purchase Challans (VchType 4, qty > 0; includes the RETURN-S "../SR" series); opening
              pending delivery challans carry VchCode -1400 (< 0). Method 2 rows clear them: Sales (9) and
              Sales Returns (3) against challans issued/received from customers, Purchases (2) and Purchase
              Returns (10) against purchase challans, and Purchase Challans (4, MODEL-RC) against delivery
              challans (models returned).
  Every RefCode has exactly one Method 1 row, one item (MasterCode1) and one party (MasterCode2).
  pending = sign(Method 1 qty) * SUM(Value1) over the RefCode.
"""
from datetime import date, datetime

VCH_NAMES = {
    0: 'Opening', 2: 'Purchase', 3: 'Sales Return', 4: 'Purchase Challan', 5: 'Stock Transfer',
    6: 'Production', 7: 'Material Receipt', 8: 'Stock Journal', 9: 'Sales', 10: 'Purchase Return',
    11: 'Sales Challan', 12: 'Sales Order', 13: 'Purchase Order', 14: 'Receipt', 15: 'Contra',
    16: 'Journal', 17: 'Debit Note', 18: 'Credit Note', 19: 'Payment', 28: 'Salary',
}

EPS = 0.0005

# Pending report kinds: (tran3 RecType, sign of the Method 1 qty, label)
PENDING_KINDS = {
    'so': (4, 1, 'Pending Sales Orders'),
    'po': (4, -1, 'Pending Purchase Orders'),
    'dc': (3, -1, 'Pending Delivery Challans'),
    'pc': (3, 1, 'Pending Purchase Challans'),
}


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


def _date(params, key, default):
    s = str(_p(params, key, '')).strip()[:10]
    try:
        datetime.strptime(s, '%Y-%m-%d')
        return s
    except ValueError:
        return default


def _fy_range(fy, params):
    start = f'{int(fy)}-04-01'
    end_default = min(date.today().isoformat(), f'{int(fy) + 1}-03-31')
    d_from = _date(params, 'from', start)
    d_to = _date(params, 'to', end_default)
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    return d_from, d_to


def _vno(v):
    return (v or '').strip()


def _names(c, company, fy, master_types):
    marks = ','.join('?' * len(master_types))
    c.execute(f'SELECT Code, Name FROM busy_master1 WHERE company_code = ? AND fy_year = ? AND MasterType IN ({marks})',
              [company, fy] + list(master_types))
    return {r[0]: r[1] for r in c.fetchall()}


def _unit_names(c, company, fy):
    """item code -> main unit name (busy_master1 row whose Code = item.CM1)."""
    c.execute('''SELECT i.Code, COALESCE(u.Name, '') FROM busy_master1 i
                 LEFT JOIN busy_master1 u ON u.company_code = i.company_code AND u.fy_year = i.fy_year AND u.Code = i.CM1
                 WHERE i.company_code = ? AND i.fy_year = ? AND i.MasterType = 6''', (company, fy))
    return {r[0]: r[1] for r in c.fetchall()}


def _avg_rates(c, company, fy):
    """item code -> average inward rate (copy of compute_item_stock)."""
    acc = {}
    c.execute('''SELECT MasterCode1, SUM(D1), SUM(D3) FROM busy_tran4
                 WHERE company_code = ? AND fy_year = ? GROUP BY MasterCode1''', (company, fy))
    for code, q, v in c.fetchall():
        a = acc.setdefault(code, [0.0, 0.0]); a[0] += q or 0.0; a[1] += v or 0.0
    c.execute('''SELECT t2.MasterCode1, SUM(t2.Value1), SUM(t2.Value3)
                 FROM busy_tran2 t2
                 JOIN busy_tran1 t1 ON t1.company_code = t2.company_code AND t1.fy_year = t2.fy_year AND t1.VchCode = t2.VchCode
                 WHERE t2.company_code = ? AND t2.fy_year = ? AND t2.RecType = 2 AND t2.Value1 > 0
                   AND t1.VchType <> 5 AND t1.Cancelled = 0
                 GROUP BY t2.MasterCode1''', (company, fy))
    for code, q, v in c.fetchall():
        a = acc.setdefault(code, [0.0, 0.0]); a[0] += q or 0.0; a[1] += v or 0.0
    return {code: (v / q if q > 0 else 0.0) for code, (q, v) in acc.items()}


def _stock_by_item_mc(c, company, fy):
    """{(item, mc): [opening qty, movement qty]} from Tran4 + Tran2 RecType 2."""
    out = {}
    c.execute('''SELECT MasterCode1, MasterCode2, SUM(D1) FROM busy_tran4
                 WHERE company_code = ? AND fy_year = ? GROUP BY MasterCode1, MasterCode2''', (company, fy))
    for item, mc, q in c.fetchall():
        out.setdefault((item, mc), [0.0, 0.0])[0] += q or 0.0
    c.execute('''SELECT MasterCode1, MasterCode2, SUM(Value1) FROM busy_tran2
                 WHERE company_code = ? AND fy_year = ? AND RecType = 2 GROUP BY MasterCode1, MasterCode2''', (company, fy))
    for item, mc, q in c.fetchall():
        out.setdefault((item, mc), [0.0, 0.0])[1] += q or 0.0
    return out


def _item_stock(c, company, fy, mc_code=0):
    """Same numbers as server.compute_item_stock(c, company, fy, mc_code): {item: {...}}."""
    rates = _avg_rates(c, company, fy)
    out = {}
    for (item, mc), (op, mov) in _stock_by_item_mc(c, company, fy).items():
        if mc_code > 0 and mc != mc_code:
            continue
        r = out.setdefault(item, {'OpQty': 0.0, 'MovementQty': 0.0})
        r['OpQty'] += op
        r['MovementQty'] += mov
    for item, r in out.items():
        rate = rates.get(item, 0.0)
        r['ClosingQty'] = round(r['OpQty'] + r['MovementQty'], 3)
        r['AvgRate'] = round(rate, 2)
        r['ClosingValue'] = round(r['ClosingQty'] * rate, 2)
    return out


def _items(c, company, fy):
    c.execute('''SELECT i.Code, i.Name, i.ParentGrp, COALESCE(u.Name, '') AS UnitName
                 FROM busy_master1 i
                 LEFT JOIN busy_master1 u ON u.company_code = i.company_code AND u.fy_year = i.fy_year AND u.Code = i.CM1
                 WHERE i.company_code = ? AND i.fy_year = ? AND i.MasterType = 6
                 ORDER BY i.Name''', (company, fy))
    return [dict(r) for r in c.fetchall()]


def _item_row(it, s):
    s = s or {}
    return {
        'Code': it['Code'], 'Name': it['Name'], 'UnitName': it['UnitName'],
        'OpQty': round(s.get('OpQty', 0.0), 3), 'MovementQty': round(s.get('MovementQty', 0.0), 3),
        'ClosingQty': s.get('ClosingQty', 0.0), 'AvgRate': s.get('AvgRate', 0.0),
        'ClosingValue': s.get('ClosingValue', 0.0),
    }


# ---------------------------------------------------------------- lookups
def inventory_items(conn, company, fy, params):
    """Item search for the stock-ledger picker. params: search, limit (default 30, max 200)."""
    c = conn.cursor()
    search = str(_p(params, 'search', '')).strip()
    limit = max(1, min(_int(params, 'limit', 30), 200))
    c.execute('''SELECT i.Code, i.Name, COALESCE(i.Alias, '') AS Alias, COALESCE(u.Name, '') AS UnitName,
                        COALESCE(g.Name, '') AS GroupName
                 FROM busy_master1 i
                 LEFT JOIN busy_master1 u ON u.company_code = i.company_code AND u.fy_year = i.fy_year AND u.Code = i.CM1
                 LEFT JOIN busy_master1 g ON g.company_code = i.company_code AND g.fy_year = i.fy_year AND g.Code = i.ParentGrp
                 WHERE i.company_code = ? AND i.fy_year = ? AND i.MasterType = 6
                   AND (i.Name LIKE ? OR COALESCE(i.Alias, '') LIKE ? OR CAST(i.Code AS TEXT) = ?)
                 ORDER BY i.Name LIMIT ?''', (company, fy, f'%{search}%', f'%{search}%', search, limit))
    return {'items': [dict(r) for r in c.fetchall()]}


def inventory_mcs(conn, company, fy, params):
    """Material centres (MasterType 11) for the dropdowns."""
    c = conn.cursor()
    c.execute('''SELECT m.Code, m.Name, COALESCE(g.Name, '') AS GroupName FROM busy_master1 m
                 LEFT JOIN busy_master1 g ON g.company_code = m.company_code AND g.fy_year = m.fy_year AND g.Code = m.ParentGrp
                 WHERE m.company_code = ? AND m.fy_year = ? AND m.MasterType = 11 ORDER BY m.Name''', (company, fy))
    return {'mcs': [dict(r) for r in c.fetchall()]}


# ---------------------------------------------------------------- 1. item stock ledger
def inventory_stock_ledger(conn, company, fy, params):
    """params: code (item, required), mc (material centre, 0 = all), from, to (YYYY-MM-DD)."""
    c = conn.cursor()
    code = _int(params, 'code', 0)
    mc = _int(params, 'mc', 0)
    d_from, d_to = _fy_range(fy, params)
    c.execute('''SELECT i.Code, i.Name, COALESCE(u.Name, '') AS UnitName, COALESCE(g.Name, '') AS GroupName
                 FROM busy_master1 i
                 LEFT JOIN busy_master1 u ON u.company_code = i.company_code AND u.fy_year = i.fy_year AND u.Code = i.CM1
                 LEFT JOIN busy_master1 g ON g.company_code = i.company_code AND g.fy_year = i.fy_year AND g.Code = i.ParentGrp
                 WHERE i.company_code = ? AND i.fy_year = ? AND i.MasterType = 6 AND i.Code = ?''', (company, fy, code))
    item = c.fetchone()
    base = {'from': d_from, 'to': d_to, 'mc': mc, 'item': None, 'opening_qty': 0.0, 'closing_qty': 0.0,
            'total_in': 0.0, 'total_out': 0.0, 'rows': []}
    if not item:
        base['error'] = 'Item not found'
        return base
    names = _names(c, company, fy, (1, 2, 6, 11))
    mc_sql = 'AND t2.MasterCode2 = ?' if mc > 0 else ''
    mc_args = [mc] if mc > 0 else []

    c.execute(f'''SELECT COALESCE(SUM(D1), 0) FROM busy_tran4 t2
                  WHERE company_code = ? AND fy_year = ? AND MasterCode1 = ? {mc_sql}''',
              [company, fy, code] + mc_args)
    tran4_qty = c.fetchone()[0] or 0.0
    c.execute(f'''SELECT COALESCE(SUM(t2.Value1), 0) FROM busy_tran2 t2
                  JOIN busy_tran1 t1 ON t1.company_code = t2.company_code AND t1.fy_year = t2.fy_year AND t1.VchCode = t2.VchCode
                  WHERE t2.company_code = ? AND t2.fy_year = ? AND t2.RecType = 2 AND t2.MasterCode1 = ?
                    AND t1.Cancelled = 0 AND t1.Date < ? {mc_sql}''', [company, fy, code, d_from] + mc_args)
    before = c.fetchone()[0] or 0.0
    opening = tran4_qty + before

    c.execute(f'''SELECT t1.VchCode, t1.VchType, t1.VchNo, t1.Date, t1.MasterCode1 AS H1, t1.MasterCode2 AS H2,
                         t2.SrNo, t2.MasterCode2 AS MC, t2.Value1 AS Qty, t2.Value3 AS Amt, x.Rate AS Rate
                  FROM busy_tran2 t2
                  JOIN busy_tran1 t1 ON t1.company_code = t2.company_code AND t1.fy_year = t2.fy_year AND t1.VchCode = t2.VchCode
                  LEFT JOIN busy_item_ext x ON x.company_code = t2.company_code AND x.fy_year = t2.fy_year
                           AND x.VchCode = t2.VchCode AND x.SrNo = t2.SrNo AND x.RecType = t2.RecType
                  WHERE t2.company_code = ? AND t2.fy_year = ? AND t2.RecType = 2 AND t2.MasterCode1 = ?
                    AND t1.Cancelled = 0 AND t1.Date >= ? AND t1.Date <= ? {mc_sql}
                  ORDER BY t1.Date, t1.VchCode, t2.SrNo''', [company, fy, code, d_from, d_to] + mc_args)
    rows, bal, tin, tout = [], opening, 0.0, 0.0
    for r in c.fetchall():
        q = r['Qty'] or 0.0
        vt = r['VchType']
        if vt == 5:  # stock transfer: show the other material centre
            other = r['H2'] if r['MC'] == r['H1'] else r['H1']
            particulars = ('To ' if q < 0 else 'From ') + names.get(other, '')
        else:
            particulars = names.get(r['H1'], '')
        bal += q
        if q > 0:
            tin += q
        else:
            tout += -q
        rate = r['Rate']
        rows.append({
            'Date': r['Date'], 'VchCode': r['VchCode'], 'VchType': vt,
            'VchTypeName': VCH_NAMES.get(vt, f'Type {vt}'), 'VchNo': _vno(r['VchNo']),
            'Particulars': particulars, 'MCName': names.get(r['MC'], ''),
            'InQty': round(q, 3) if q > 0 else 0.0, 'OutQty': round(-q, 3) if q < 0 else 0.0,
            'Balance': round(bal, 3), 'Rate': abs(rate) if rate not in (None, 0) else None,
            'Amount': round(abs(r['Amt'] or 0.0), 2),
        })
    base.update({
        'item': {'Code': item['Code'], 'Name': item['Name'], 'UnitName': item['UnitName'],
                 'GroupName': item['GroupName']},
        'mc_name': names.get(mc, '') if mc > 0 else 'All Material Centres',
        'opening_qty': round(opening, 3), 'total_in': round(tin, 3), 'total_out': round(tout, 3),
        'closing_qty': round(bal, 3), 'rows': rows,
    })
    return base


# ---------------------------------------------------------------- 2. item group-wise stock
def inventory_group_stock(conn, company, fy, params):
    """params: group (item group code, 0 = top level). Returns the group's direct sub-groups (with totals over
    their whole sub-tree) and its direct items (numbers identical to Stock Status)."""
    c = conn.cursor()
    group = _int(params, 'group', 0)
    stock = _item_stock(c, company, fy)
    c.execute('''SELECT Code, Name, ParentGrp FROM busy_master1
                 WHERE company_code = ? AND fy_year = ? AND MasterType = 5''', (company, fy))
    groups = {r['Code']: {'Code': r['Code'], 'Name': r['Name'], 'ParentGrp': r['ParentGrp'] or 0} for r in c.fetchall()}
    children = {}
    for g in groups.values():
        p = g['ParentGrp'] if g['ParentGrp'] in groups and g['ParentGrp'] != g['Code'] else 0
        children.setdefault(p, []).append(g['Code'])
    items_by_grp = {}
    for it in _items(c, company, fy):
        p = it['ParentGrp'] if it['ParentGrp'] in groups else 0
        items_by_grp.setdefault(p, []).append(it)

    memo = {}

    def agg(g, seen=()):
        if g in memo:
            return memo[g]
        t = {'item_count': 0, 'in_stock_count': 0, 'closing_qty': 0.0, 'closing_value': 0.0}
        for it in items_by_grp.get(g, []):
            s = stock.get(it['Code'], {})
            q = s.get('ClosingQty', 0.0)
            t['item_count'] += 1
            t['in_stock_count'] += 1 if abs(q) > EPS else 0
            t['closing_qty'] += q
            t['closing_value'] += s.get('ClosingValue', 0.0)
        for ch in children.get(g, []):
            if ch in seen:
                continue
            a = agg(ch, seen + (g,))
            for k in t:
                t[k] += a[k]
        memo[g] = t
        return t

    def fmt(t):
        return {'item_count': t['item_count'], 'in_stock_count': t['in_stock_count'],
                'closing_qty': round(t['closing_qty'], 3), 'closing_value': round(t['closing_value'], 2)}

    if group and group not in groups:
        return {'error': 'Item group not found', 'group': None, 'path': [], 'sub_groups': [], 'items': [],
                'totals': fmt({'item_count': 0, 'in_stock_count': 0, 'closing_qty': 0.0, 'closing_value': 0.0})}
    sub = []
    for ch in sorted(children.get(group, []), key=lambda x: groups[x]['Name']):
        row = {'Code': ch, 'Name': groups[ch]['Name'], 'sub_group_count': len(children.get(ch, []))}
        row.update(fmt(agg(ch)))
        sub.append(row)
    items = [_item_row(it, stock.get(it['Code'])) for it in items_by_grp.get(group, [])]
    path, g, guard = [], group, 0
    while g and g in groups and guard < 50:
        path.insert(0, {'Code': g, 'Name': groups[g]['Name']})
        g = groups[g]['ParentGrp'] if groups[g]['ParentGrp'] in groups else 0
        guard += 1
    return {'group': ({'Code': group, 'Name': groups[group]['Name']} if group else {'Code': 0, 'Name': 'All Item Groups'}),
            'path': path, 'sub_groups': sub, 'items': items, 'totals': fmt(agg(group))}


# ---------------------------------------------------------------- 3. material centre-wise stock
def inventory_mc_stock(conn, company, fy, params):
    """params: mc (0 = summary of all material centres; >0 = items in that MC, like Stock Status with mcCode)."""
    c = conn.cursor()
    mc = _int(params, 'mc', 0)
    rates = _avg_rates(c, company, fy)
    by = _stock_by_item_mc(c, company, fy)
    c.execute('''SELECT m.Code, m.Name, COALESCE(g.Name, '') AS GroupName FROM busy_master1 m
                 LEFT JOIN busy_master1 g ON g.company_code = m.company_code AND g.fy_year = m.fy_year AND g.Code = m.ParentGrp
                 WHERE m.company_code = ? AND m.fy_year = ? AND m.MasterType = 11 ORDER BY m.Name''', (company, fy))
    mcs = {r['Code']: dict(r) for r in c.fetchall()}

    if mc > 0:
        stock = _item_stock(c, company, fy, mc)
        items = []
        tq = tv = 0.0
        for it in _items(c, company, fy):
            s = stock.get(it['Code'])
            if not s or (s['OpQty'] == 0 and s['MovementQty'] == 0):
                continue  # item never touched this material centre
            row = _item_row(it, s)
            items.append(row)
            tq += row['ClosingQty']
            tv += row['ClosingValue']
        m = mcs.get(mc)
        return {'mc': ({'Code': mc, 'Name': m['Name'], 'GroupName': m['GroupName']} if m else None),
                'items': items,
                'totals': {'item_count': len(items),
                           'in_stock_count': sum(1 for r in items if abs(r['ClosingQty']) > EPS),
                           'closing_qty': round(tq, 3), 'closing_value': round(tv, 2)}}

    agg = {}
    for (item, m), (op, mov) in by.items():
        q = round(op + mov, 3)
        a = agg.setdefault(m, {'items_touched': 0, 'in_stock_count': 0, 'closing_qty': 0.0, 'closing_value': 0.0})
        if op == 0 and mov == 0:
            continue  # same rule as Stock Status: item never touched this material centre (net)
        a['items_touched'] += 1
        a['in_stock_count'] += 1 if abs(q) > EPS else 0
        a['closing_qty'] += q
        a['closing_value'] += round(q * rates.get(item, 0.0), 2)
    rows = []
    for code in list(mcs) + [k for k in agg if k not in mcs]:
        m = mcs.get(code, {'Name': f'MC {code}', 'GroupName': ''})
        a = agg.get(code, {'items_touched': 0, 'in_stock_count': 0, 'closing_qty': 0.0, 'closing_value': 0.0})
        rows.append({'Code': code, 'Name': m['Name'], 'GroupName': m['GroupName'],
                     'items_touched': a['items_touched'], 'in_stock_count': a['in_stock_count'],
                     'closing_qty': round(a['closing_qty'], 3), 'closing_value': round(a['closing_value'], 2)})
    rows.sort(key=lambda r: r['Name'])
    return {'mcs': rows,
            'totals': {'mc_count': len(rows),
                       'closing_qty': round(sum(r['closing_qty'] for r in rows), 3),
                       'closing_value': round(sum(r['closing_value'] for r in rows), 2)}}


# ---------------------------------------------------------------- 4. pending orders / challans
def inventory_pending(conn, company, fy, params):
    """params: type = so | po | dc | pc (default so), to (as-of date, default today), party (code), item (code),
    search (party / item / voucher no text)."""
    c = conn.cursor()
    kind = str(_p(params, 'type', 'so')).strip().lower()
    if kind not in PENDING_KINDS:
        kind = 'so'
    rectype, sign, label = PENDING_KINDS[kind]
    _f, d_to = _fy_range(fy, params)
    d_to = _date(params, 'to', d_to)
    party = _int(params, 'party', 0)
    item_f = _int(params, 'item', 0)
    search = str(_p(params, 'search', '')).strip().lower()

    c.execute('''SELECT t3.VchCode, t3.SrNo, t3.MasterCode1 AS Item, t3.MasterCode2 AS Party, t3.RefCode,
                        t3.Method, t3.BillNo, t3.BillDate, t3.Value1,
                        t1.VchType, t1.VchNo, t1.Date
                 FROM busy_tran3 t3
                 LEFT JOIN busy_tran1 t1 ON t1.company_code = t3.company_code AND t1.fy_year = t3.fy_year
                                         AND t1.VchCode = t3.VchCode
                 WHERE t3.company_code = ? AND t3.fy_year = ? AND t3.RecType = ?
                   AND COALESCE(t1.Cancelled, 0) = 0
                 ORDER BY t3.RefCode, t3.Method, COALESCE(t1.Date, t3.BillDate), t3.VchCode''', (company, fy, rectype))
    refs = {}
    for r in c.fetchall():
        d = (r['Date'] or r['BillDate'] or '')[:10]
        if d and d > d_to:
            continue
        ref = refs.setdefault(r['RefCode'], {'m1': None, 'adj': []})
        if r['Method'] == 1 and ref['m1'] is None:
            ref['m1'] = dict(r, _date=d)
        else:
            ref['adj'].append(dict(r, _date=d))

    picked = []
    for code, ref in refs.items():
        m1 = ref['m1']
        if not m1 or not m1['Value1'] or (1 if m1['Value1'] > 0 else -1) != sign:
            continue
        if party and m1['Party'] != party:
            continue
        if item_f and m1['Item'] != item_f:
            continue
        total = (m1['Value1'] or 0.0) + sum(a['Value1'] or 0.0 for a in ref['adj'])
        pending = sign * total
        if abs(pending) <= EPS:
            continue
        picked.append((code, ref, pending))

    # Rates: the k-th item line (tran3 SrNo k) of the same item on the creating voucher.
    line_rt = 4 if rectype == 4 else 2
    vchs = sorted({ref['m1']['VchCode'] for _c, ref, _p in picked if ref['m1']['VchCode'] > 0})
    lines = {}
    for i in range(0, len(vchs), 500):
        chunk = vchs[i:i + 500]
        marks = ','.join('?' * len(chunk))
        c.execute(f'''SELECT t2.VchCode, t2.MasterCode1, t2.SrNo, t2.Value1, t2.Value3, x.Rate
                      FROM busy_tran2 t2
                      LEFT JOIN busy_item_ext x ON x.company_code = t2.company_code AND x.fy_year = t2.fy_year
                               AND x.VchCode = t2.VchCode AND x.SrNo = t2.SrNo AND x.RecType = t2.RecType
                      WHERE t2.company_code = ? AND t2.fy_year = ? AND t2.RecType = ? AND t2.VchCode IN ({marks})
                      ORDER BY t2.VchCode, t2.SrNo''', [company, fy, line_rt] + chunk)
        for r in c.fetchall():
            lines.setdefault((r['VchCode'], r['MasterCode1']), []).append(dict(r))

    names = _names(c, company, fy, (2, 6))
    units = _unit_names(c, company, fy)
    out = []
    for code, ref, pending in picked:
        m1 = ref['m1']
        ordered = abs(m1['Value1'])
        adjusted = -sign * sum(a['Value1'] or 0.0 for a in ref['adj'])
        rate = None
        if m1['VchCode'] > 0:
            ls = lines.get((m1['VchCode'], m1['Item']), [])
            k = (m1['SrNo'] or 1) - 1
            ln = ls[k] if 0 <= k < len(ls) else (ls[0] if ls else None)
            if ln:
                if ln['Rate']:
                    rate = abs(ln['Rate'])
                elif ln['Value1']:
                    rate = abs((ln['Value3'] or 0.0) / ln['Value1'])
        vno = _vno(m1['VchNo']) if m1['VchCode'] > 0 else (m1['BillNo'] or '').strip()
        row = {
            'RefCode': code, 'VchCode': m1['VchCode'],
            'VchType': m1['VchType'] or 0,
            'VchTypeName': VCH_NAMES.get(m1['VchType'] or 0, 'Opening') if m1['VchCode'] > 0 else 'Opening',
            'Date': m1['_date'], 'VchNo': vno,
            'PartyCode': m1['Party'], 'PartyName': names.get(m1['Party'], ''),
            'ItemCode': m1['Item'], 'ItemName': names.get(m1['Item'], ''), 'UnitName': units.get(m1['Item'], ''),
            'OrderedQty': round(ordered, 3), 'AdjustedQty': round(adjusted, 3), 'PendingQty': round(pending, 3),
            'Rate': round(rate, 2) if rate is not None else None,
            'PendingValue': round(pending * rate, 2) if rate is not None else None,
            'Adjustments': [{
                'Date': a['_date'],
                'VchNo': _vno(a['VchNo']) if (a['VchCode'] or 0) > 0 else (a['BillNo'] or '').strip(),
                'VchTypeName': VCH_NAMES.get(a['VchType'], 'Ref. Cleared') if (a['VchCode'] or 0) > 0 else 'Ref. Cleared',
                'Qty': round(-sign * (a['Value1'] or 0.0), 3),
            } for a in ref['adj']],
        }
        if search and not any(search in str(row[k]).lower() for k in ('PartyName', 'ItemName', 'VchNo')):
            continue
        out.append(row)
    out.sort(key=lambda r: (r['Date'], r['VchNo'], r['ItemName']))
    return {
        'type': kind, 'title': label, 'as_of': d_to,
        'rows': out,
        'totals': {'count': len(out), 'vouchers': len({(r['VchCode'], r['VchNo']) for r in out}),
                   'ordered_qty': round(sum(r['OrderedQty'] for r in out), 3),
                   'adjusted_qty': round(sum(r['AdjustedQty'] for r in out), 3),
                   'pending_qty': round(sum(r['PendingQty'] for r in out), 3),
                   'pending_value': round(sum(r['PendingValue'] or 0.0 for r in out), 2)},
    }


ROUTES = {
    '/api/reports/inventory-items': inventory_items,
    '/api/reports/inventory-mcs': inventory_mcs,
    '/api/reports/inventory-stock-ledger': inventory_stock_ledger,
    '/api/reports/inventory-group-stock': inventory_group_stock,
    '/api/reports/inventory-mc-stock': inventory_mc_stock,
    '/api/reports/inventory-pending': inventory_pending,
}
