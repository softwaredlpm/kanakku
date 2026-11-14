"""Verification for reports_inventory.py against the real busy_local.db (read-only), both companies, FY 2026.
Run:  py scratch/test_reports_inventory.py
"""
import os
import random
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import reports_inventory as R  # noqa: E402

FY = 2026
FAILS = []


def check(name, ok, detail=''):
    print(('  PASS ' if ok else '  FAIL ') + name + (f'  [{detail}]' if detail else ''))
    if not ok:
        FAILS.append(name)


def close(a, b, tol=0.001):
    return abs((a or 0) - (b or 0)) <= tol


def P(**kw):
    return {k: [str(v)] for k, v in kw.items()}


conn = sqlite3.connect('file:D:/Busy Mobile App/busy_local.db?mode=ro', uri=True)
conn.row_factory = sqlite3.Row
c = conn.cursor()


def indep_stock(company):
    """Independent copy of compute_item_stock (qty + value) as plain SQL."""
    qty = {}
    for code, q in c.execute('SELECT MasterCode1, SUM(D1) FROM busy_tran4 WHERE company_code=? AND fy_year=? GROUP BY 1', (company, FY)):
        qty[code] = qty.get(code, 0) + (q or 0)
    for code, q in c.execute('SELECT MasterCode1, SUM(Value1) FROM busy_tran2 WHERE company_code=? AND fy_year=? AND RecType=2 GROUP BY 1', (company, FY)):
        qty[code] = qty.get(code, 0) + (q or 0)
    rq, rv = {}, {}
    for code, q, v in c.execute('SELECT MasterCode1, SUM(D1), SUM(D3) FROM busy_tran4 WHERE company_code=? AND fy_year=? GROUP BY 1', (company, FY)):
        rq[code] = rq.get(code, 0) + (q or 0); rv[code] = rv.get(code, 0) + (v or 0)
    for code, q, v in c.execute('''SELECT t2.MasterCode1, SUM(t2.Value1), SUM(t2.Value3) FROM busy_tran2 t2
            JOIN busy_tran1 t1 ON t1.company_code=t2.company_code AND t1.fy_year=t2.fy_year AND t1.VchCode=t2.VchCode
            WHERE t2.company_code=? AND t2.fy_year=? AND t2.RecType=2 AND t2.Value1>0 AND t1.VchType<>5 AND t1.Cancelled=0
            GROUP BY 1''', (company, FY)):
        rq[code] = rq.get(code, 0) + (q or 0); rv[code] = rv.get(code, 0) + (v or 0)
    out = {}
    for code, q in qty.items():
        rate = rv.get(code, 0) / rq[code] if rq.get(code, 0) > 0 else 0
        cq = round(q, 3)
        out[code] = (cq, round(cq * rate, 2))
    return out


for company in ('COMP0044', 'COMP0027'):
    print(f'\n===== {company} FY {FY} =====')
    st = indep_stock(company)
    tot_q = round(sum(q for q, _ in st.values()), 3)
    tot_v = round(sum(v for _, v in st.values()), 2)
    print(f'  Independent total stock qty {tot_q}, value {tot_v}, items with stock rows {len(st)}')

    # lookups
    it = R.inventory_items(conn, company, FY, P(search='', limit=5))
    check('item search returns items', len(it['items']) > 0, f"{len(it['items'])} e.g. {it['items'][0]['Name'] if it['items'] else ''}")
    mcs = R.inventory_mcs(conn, company, FY, {})
    check('mc list', len(mcs['mcs']) > 0, f"{len(mcs['mcs'])} MCs")

    # ---- 1. stock ledger: every item, whole FY
    codes = [r[0] for r in c.execute('SELECT Code FROM busy_master1 WHERE company_code=? AND fy_year=? AND MasterType=6', (company, FY))]
    bad = 0
    for code in codes:
        L = R.inventory_stock_ledger(conn, company, FY, P(code=code, **{'from': '2026-04-01', 'to': '2027-03-31'}))
        exp = st.get(code, (0, 0))[0]
        rec = round(L['opening_qty'] + L['total_in'] - L['total_out'], 3)
        if not close(L['closing_qty'], exp) or not close(rec, L['closing_qty']):
            bad += 1
            if bad <= 3:
                print('    mismatch', code, L['closing_qty'], exp, rec)
    check(f'ledger closing == Tran4 + SUM(Value1) for all {len(codes)} items', bad == 0, f'{bad} mismatches')

    # mid-period ledger: opening(from) + movements == closing(to) == independent SQL as of `to`
    random.seed(7)
    sample = [code for code in codes if code in st]
    sample = random.sample(sample, min(60, len(sample)))
    bad = 0
    for code in sample:
        for mc in [0] + [r[0] for r in c.execute('SELECT DISTINCT MasterCode2 FROM busy_tran2 WHERE company_code=? AND fy_year=? AND RecType=2 AND MasterCode1=? LIMIT 2', (company, FY, code))]:
            L = R.inventory_stock_ledger(conn, company, FY, P(code=code, mc=mc, **{'from': '2026-06-01', 'to': '2026-08-31'}))
            mc_sql, a = ('AND MasterCode2=?', [mc]) if mc else ('', [])
            t4 = c.execute(f'SELECT COALESCE(SUM(D1),0) FROM busy_tran4 WHERE company_code=? AND fy_year=? AND MasterCode1=? {mc_sql}', [company, FY, code] + a).fetchone()[0]
            mov = c.execute(f'''SELECT COALESCE(SUM(t2.Value1),0) FROM busy_tran2 t2 JOIN busy_tran1 t1 ON t1.company_code=t2.company_code AND t1.fy_year=t2.fy_year AND t1.VchCode=t2.VchCode
                               WHERE t2.company_code=? AND t2.fy_year=? AND t2.RecType=2 AND t2.MasterCode1=? AND t1.Date<='2026-08-31' {mc_sql.replace('MasterCode2', 't2.MasterCode2')}''', [company, FY, code] + a).fetchone()[0]
            ok = close(L['closing_qty'], t4 + mov) and close(L['opening_qty'] + L['total_in'] - L['total_out'], L['closing_qty']) \
                and (not L['rows'] or close(L['rows'][-1]['Balance'], L['closing_qty']))
            if not ok:
                bad += 1
                print('    mid mismatch', code, mc, L['closing_qty'], t4 + mov)
    check(f'mid-period ledgers (Jun-Aug, all MC + per MC) for {len(sample)} items reconcile', bad == 0, f'{bad} mismatches')
    if sample:
        L = R.inventory_stock_ledger(conn, company, FY, P(code=sample[0], **{'from': '2026-04-01', 'to': '2026-10-20'}))
        print(f"    e.g. {L['item']['Name']} ({L['item']['UnitName']}): open {L['opening_qty']} in {L['total_in']} out {L['total_out']} close {L['closing_qty']}, {len(L['rows'])} rows; first row {L['rows'][0] if L['rows'] else None}")
    rated = c.execute('''SELECT COUNT(*), SUM(x.Rate IS NOT NULL AND x.Rate<>0) FROM busy_tran2 t2 LEFT JOIN busy_item_ext x ON x.company_code=t2.company_code AND x.fy_year=t2.fy_year AND x.VchCode=t2.VchCode AND x.SrNo=t2.SrNo AND x.RecType=t2.RecType
                         WHERE t2.company_code=? AND t2.fy_year=? AND t2.RecType=2''', (company, FY)).fetchone()
    print(f'    item_ext rate present on {rated[1]} of {rated[0]} RecType 2 lines')

    # ---- 2. group-wise stock
    G = R.inventory_group_stock(conn, company, FY, P(group=0))
    top_q = sum(g['closing_qty'] for g in G['sub_groups']) + sum(i['ClosingQty'] for i in G['items'])
    top_v = sum(g['closing_value'] for g in G['sub_groups']) + sum(i['ClosingValue'] for i in G['items'])
    check('sum over top-level groups == total stock qty', close(top_q, tot_q) and close(G['totals']['closing_qty'], tot_q),
          f"groups {round(top_q, 3)} / totals {G['totals']['closing_qty']} / independent {tot_q}")
    check('sum over top-level groups == total stock value', close(top_v, tot_v, 0.05), f'{round(top_v, 2)} vs {tot_v}')
    check('item_count over groups == all items', G['totals']['item_count'] == len(codes), f"{G['totals']['item_count']} vs {len(codes)}")
    # every group: totals == children + own items; item numbers == independent Stock Status
    bad = badit = 0
    gcodes = [r[0] for r in c.execute('SELECT Code FROM busy_master1 WHERE company_code=? AND fy_year=? AND MasterType=5', (company, FY))]
    for g in gcodes:
        D = R.inventory_group_stock(conn, company, FY, P(group=g))
        s = sum(x['closing_qty'] for x in D['sub_groups']) + sum(i['ClosingQty'] for i in D['items'])
        n = sum(x['item_count'] for x in D['sub_groups']) + len(D['items'])
        if not close(s, D['totals']['closing_qty']) or n != D['totals']['item_count']:
            bad += 1
        for i in D['items']:
            q, v = st.get(i['Code'], (0, 0))
            if not close(i['ClosingQty'], q) or not close(i['ClosingValue'], v, 0.01):
                badit += 1
    check(f'all {len(gcodes)} group drill-downs: totals == sub-groups + items', bad == 0, f'{bad} bad')
    check('group drill item numbers == Stock Status numbers', badit == 0, f'{badit} bad')
    for g in G['sub_groups'][:6]:
        print(f"    {g['Name']}: {g['item_count']} items ({g['in_stock_count']} in stock), qty {g['closing_qty']}, value {g['closing_value']}")

    # ---- 3. MC-wise stock
    M = R.inventory_mc_stock(conn, company, FY, P(mc=0))
    mq = sum(m['closing_qty'] for m in M['mcs'])
    check('sum over MCs == total stock qty', close(mq, tot_q), f'{round(mq, 3)} vs {tot_q}')
    check('sum over MCs value ~= total value (per-MC rounding)', close(M['totals']['closing_value'], tot_v, 1.0), f"{M['totals']['closing_value']} vs {tot_v}")
    bad = 0
    for m in M['mcs']:
        D = R.inventory_mc_stock(conn, company, FY, P(mc=m['Code']))
        if not close(D['totals']['closing_qty'], m['closing_qty']) or not close(D['totals']['closing_value'], m['closing_value'], 0.01) \
                or D['totals']['in_stock_count'] != m['in_stock_count'] or len(D['items']) != m['items_touched']:
            bad += 1
            print('    mc mismatch', m['Name'], D['totals'], m)
    check(f"all {len(M['mcs'])} MC drill-downs match their summary rows", bad == 0, f'{bad} bad')
    for m in sorted(M['mcs'], key=lambda x: -abs(x['closing_qty']))[:5]:
        print(f"    {m['Name']} [{m['GroupName']}]: {m['in_stock_count']} items in stock, qty {m['closing_qty']}, value {m['closing_value']}")

    # ---- 4. pending orders / challans
    for kind, (rt, sign, label) in R.PENDING_KINDS.items():
        X = R.inventory_pending(conn, company, FY, P(type=kind, to='2027-03-31'))
        cmp_ = '>' if sign > 0 else '<'
        exp = c.execute(f'''SELECT COUNT(*), COALESCE(SUM(p),0) FROM (
                SELECT RefCode, {sign} * SUM(Value1) p FROM busy_tran3 WHERE company_code=? AND fy_year=? AND RecType=?
                GROUP BY RefCode HAVING SUM(CASE WHEN Method=1 THEN Value1 END) {cmp_} 0 AND ABS(SUM(Value1)) > 0.0005)''',
                        (company, FY, rt)).fetchone()
        check(f'{label}: lines/pending qty == independent SQL', X['totals']['count'] == exp[0] and close(X['totals']['pending_qty'], exp[1]),
              f"{X['totals']['count']} lines, pending {X['totals']['pending_qty']} (SQL {exp[0]}, {round(exp[1], 3)}), ordered {X['totals']['ordered_qty']}, adjusted {X['totals']['adjusted_qty']}, value {X['totals']['pending_value']}")
        neg = [r for r in X['rows'] if r['PendingQty'] < 0]
        if neg:
            print(f'    note: {len(neg)} over-adjusted refs (pending < 0), e.g. {neg[0]["VchNo"]} {neg[0]["ItemName"]} {neg[0]["PendingQty"]}')
        ok_sum = all(close(r['OrderedQty'] - r['AdjustedQty'], r['PendingQty']) and close(sum(a['Qty'] for a in r['Adjustments']), r['AdjustedQty']) for r in X['rows'])
        check(f'{label}: ordered - adjusted == pending on every line', ok_sum)
        # rate line matching: matched tran2 line qty == ordered qty
        mism = norate = 0
        line_rt = 4 if rt == 4 else 2
        for r in X['rows']:
            if r['VchCode'] <= 0:
                continue
            if r['Rate'] is None:
                norate += 1
            q = c.execute('SELECT GROUP_CONCAT(ABS(Value1)) FROM busy_tran2 WHERE company_code=? AND fy_year=? AND VchCode=? AND MasterCode1=? AND RecType=?',
                          (company, FY, r['VchCode'], r['ItemCode'], line_rt)).fetchone()[0] or ''
            if not any(close(float(v), r['OrderedQty']) for v in q.split(',') if v):
                mism += 1
        check(f'{label}: ordered qty matches the voucher item line', mism == 0, f'{mism} mismatches, {norate} without rate')
        # spot checks by hand: show 2 vouchers with their full tran3 trail
        for r in X['rows'][:2]:
            trail = c.execute('''SELECT t3.Method, COALESCE(t1.VchNo, t3.BillNo), COALESCE(t1.Date, t3.BillDate), t1.VchType, t3.Value1 FROM busy_tran3 t3
                                 LEFT JOIN busy_tran1 t1 ON t1.company_code=t3.company_code AND t1.fy_year=t3.fy_year AND t1.VchCode=t3.VchCode
                                 WHERE t3.company_code=? AND t3.fy_year=? AND t3.RecType=? AND t3.RefCode=?''', (company, FY, rt, r['RefCode'])).fetchall()
            hand = sign * sum(t[4] for t in trail)
            check(f"  spot {r['VchNo']} {r['Date']} {r['PartyName']} / {r['ItemName']}: ord {r['OrderedQty']} adj {r['AdjustedQty']} pend {r['PendingQty']} rate {r['Rate']}",
                  close(hand, r['PendingQty']), 'trail ' + '; '.join(f'M{t[0]} {str(t[1]).strip()} {t[2]} vt{t[3]} {t[4]}' for t in trail))
    # as-of filter: pending as of an earlier date
    E = R.inventory_pending(conn, company, FY, P(type='so', to='2026-06-30'))
    print(f"    Pending SO as of 2026-06-30: {E['totals']['count']} lines, pending qty {E['totals']['pending_qty']}")

print('\nRESULT:', 'ALL PASS' if not FAILS else f'{len(FAILS)} FAIL(S): ' + ', '.join(FAILS))
