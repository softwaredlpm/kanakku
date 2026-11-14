"""Verify reports_batch.py against independent SQL for both companies (read-only DB)."""
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import reports_batch as rb  # noqa: E402

DB = 'file:D:/Busy Mobile App/busy_local.db?mode=ro'
FY = 2026
FAILS = []


def check(cond, msg):
    print(('  OK   ' if cond else '  FAIL ') + msg)
    if not cond:
        FAILS.append(msg)


def close(a, b, tol=0.001):
    return abs((a or 0) - (b or 0)) <= tol


def P(**kw):
    return {k: [str(v)] for k, v in kw.items()}


conn = sqlite3.connect(DB, uri=True)
conn.row_factory = sqlite3.Row
c = conn.cursor()

for comp in ('COMP0044', 'COMP0027'):
    print(f'===== {comp} =====')
    t0 = time.time()
    # independent SQL truth per item + MC + batch
    c.execute('''SELECT MasterCode1, MasterCode2, TRIM(BillNo) b, SUM(Value1) q,
                        SUM(CASE WHEN VchCode = 0 THEN Value1 ELSE 0 END) op,
                        SUM(CASE WHEN VchCode <> 0 AND Value1 > 0 THEN Value1 ELSE 0 END) qin,
                        SUM(CASE WHEN VchCode <> 0 AND Value1 < 0 THEN -Value1 ELSE 0 END) qout
                 FROM busy_tran3 WHERE company_code = ? AND fy_year = ? AND RecType = 2
                 GROUP BY MasterCode1, MasterCode2, TRIM(BillNo)''', (comp, FY))
    truth = {(r[0], r[1], r[2]): (r[3], r[4], r[5], r[6]) for r in c.fetchall()}
    nz = {k: v for k, v in truth.items() if round(v[0], 3) != 0}
    sql_cnt, sql_qty = len(nz), sum(v[0] for v in nz.values())
    print(f'  SQL: {len(truth)} batches (item+MC+batch), {sql_cnt} with non-zero closing, closing qty {sql_qty:,.3f}')

    mcs = rb.batch_mcs(conn, comp, FY, {})['mcs']
    print(f'  batch-mcs: {len(mcs)} MCs with batch lines')

    s = rb.batch_mc_stock(conn, comp, FY, {})
    t = s['totals']
    print(f"  MC summary: {t['mc_count']} MCs, {t['batches_with_stock']} batches with stock, {t['items_with_stock']} items, closing {t['closing']:,.3f}")
    check(t['batches_with_stock'] == sql_cnt and close(t['closing'], sql_qty), 'MC summary totals == SQL non-zero batch count / closing')
    check(close(sum(m['closing'] for m in s['mcs']), t['closing']), 'sum of MC rows == summary total')
    check(close(t['opening'] + t['in'] - t['out'], t['closing']), 'opening + in - out == closing (summary)')
    if comp == 'COMP0044':
        check(sql_cnt == 1182 and close(sql_qty, 91262.7, 0.01), 'COMP0044 matches BUSY: 1182 batches / 91,262.7')
    s_all = rb.batch_mc_stock(conn, comp, FY, P(hide_zero=0))
    check(s_all['totals']['batch_count'] == len(truth) and close(s_all['totals']['closing'], sql_qty),
          f"hide_zero=0: {s_all['totals']['batch_count']} batches incl. zero == SQL {len(truth)}")

    # drill: every MC -> items -> batches
    bad_mc = bad_item = 0
    n_items = 0
    for m in s['mcs']:
        d = rb.batch_mc_stock(conn, comp, FY, P(mc=m['Code']))
        if not (close(d['totals']['closing'], m['closing']) and d['totals']['batches_with_stock'] == m['batches_with_stock']
                and close(sum(i['closing'] for i in d['items']), m['closing'])):
            bad_mc += 1
        for it in d['items']:
            n_items += 1
            b = rb.batch_mc_stock(conn, comp, FY, P(mc=m['Code'], item=it['Code']))
            exp = [(k, v) for k, v in nz.items() if k[0] == it['Code'] and k[1] == m['Code']]
            ok = (len(b['batches']) == len(exp) and close(b['totals']['closing'], it['closing'])
                  and close(sum(x['Closing'] for x in b['batches']), sum(v[0] for _k, v in exp))
                  and all(close(x['Opening'] + x['In'] - x['Out'], x['Closing']) for x in b['batches'])
                  and all(close(x['Closing'], truth[(it['Code'], m['Code'], x['BatchNo'])][0])
                          and close(x['In'], truth[(it['Code'], m['Code'], x['BatchNo'])][2]) for x in b['batches']))
            if not ok:
                bad_item += 1
    check(bad_mc == 0, f'MC -> items drill adds up for all {len(s["mcs"])} MCs')
    check(bad_item == 0, f'MC -> item -> batches drill adds up for all {n_items} MC items (per-batch closing / in == SQL)')
    for m in s['mcs'][:3]:
        print(f"    {m['Name']}: {m['batches_with_stock']} batches, {m['items_with_stock']} items, {m['closing']:,.3f}")

    # search
    if nz:
        k = next(iter(nz))
        sr = rb.batch_mc_stock(conn, comp, FY, P(search=k[2]))
        check(sr['totals']['batch_count'] >= 1, f'search "{k[2]}" finds {sr["totals"]["batch_count"]} batch(es)')
        c.execute('''SELECT Narration FROM busy_tran3 WHERE company_code = ? AND fy_year = ? AND RecType = 2
                     AND Narration <> '' LIMIT 1''', (comp, FY))
        nar = c.fetchone()[0]
        sr2 = rb.batch_mc_stock(conn, comp, FY, P(search=nar, hide_zero=0))
        check(sr2['totals']['batch_count'] >= 1, f'search by description "{nar}" finds {sr2["totals"]["batch_count"]} batch(es)')

    # batch ledger: every item+batch (all MCs) and a sample per MC
    c.execute('''SELECT MasterCode1, TRIM(BillNo), SUM(Value1), SUM(CASE WHEN VchCode = 0 THEN Value1 ELSE 0 END)
                 FROM busy_tran3 WHERE company_code = ? AND fy_year = ? AND RecType = 2
                 GROUP BY MasterCode1, TRIM(BillNo)''', (comp, FY))
    ib_all = c.fetchall()
    ib = ib_all[::max(1, len(ib_all) // 600)] if ib_all else []
    bad = 0
    for item, bno, q, op in ib:
        L = rb.batch_ledger(conn, comp, FY, P(batch=bno, item=item))
        if L.get('error') or not close(L['closing'], q) or not close(L['opening'], op) \
                or not close(L['opening'] + L['total_in'] - L['total_out'], L['closing']) \
                or (L['rows'] and not close(L['rows'][-1]['Balance'], q)) \
                or not close(sum(x['Closing'] for x in L['by_mc']), q):
            bad += 1
    check(bad == 0, f'batch ledger closing == SUM(Value1) for {len(ib)} of {len(ib_all)} item+batch (every k-th, all MCs)')
    bad = 0
    sample = list(truth.items())[::max(1, len(truth) // 300)] if truth else []
    for (item, mcc, bno), v in sample:
        L = rb.batch_ledger(conn, comp, FY, P(batch=bno, item=item, mc=mcc))
        if L.get('error') or not close(L['closing'], v[0]) or not close(L['total_in'], v[2]) or not close(L['total_out'], v[3]):
            bad += 1
    check(bad == 0, f'batch ledger per MC closing/in/out == SQL for {len(sample)} sampled item+MC+batch')
    c.execute('''SELECT TRIM(BillNo) FROM busy_tran3 WHERE company_code = ? AND fy_year = ? AND RecType = 2
                 GROUP BY TRIM(BillNo) HAVING COUNT(DISTINCT MasterCode1) > 1 LIMIT 1''', (comp, FY))
    multi = c.fetchone()
    if multi:
        L = rb.batch_ledger(conn, comp, FY, P(batch=multi[0]))
        check(L.get('need_item') and len(L['choices']) > 1, f'shared batch no "{multi[0]}" asks for item ({len(L["choices"])} choices)')
        srch = rb.batch_search(conn, comp, FY, P(search=multi[0]))['results']
        check(len({r['ItemCode'] for r in srch if r['BatchNo'] == multi[0]}) > 1, 'batch-search shows each item+MC for a shared batch no')
    L = rb.batch_ledger(conn, comp, FY, P(batch='NO-SUCH-BATCH-XYZ'))
    check(L.get('error') == 'Batch not found', 'unknown batch handled')
    if ib:
        item, bno, q, op = ib[0]
        L = rb.batch_ledger(conn, comp, FY, P(batch=bno, item=item))
        print(f"    ledger {L['item']['Name']} / {bno}: op {L['opening']} in {L['total_in']} out {L['total_out']} cl {L['closing']} ({len(L['rows'])} rows)")
        if L['rows']:
            print('    first row:', {k: L['rows'][0][k] for k in ('Date', 'VchTypeName', 'VchNo', 'Party', 'McName', 'In', 'Out', 'Balance')})

    # item-wise batch summary: every item with batches
    c.execute('''SELECT MasterCode1, SUM(Value1), SUM(CASE WHEN VchCode = 0 THEN Value1 ELSE 0 END),
                        COUNT(DISTINCT TRIM(BillNo))
                 FROM busy_tran3 WHERE company_code = ? AND fy_year = ? AND RecType = 2 GROUP BY MasterCode1''', (comp, FY))
    items = c.fetchall()
    bad = diffs = 0
    diff_list = []
    for item, q, op, nb in items:
        S = rb.batch_item_summary(conn, comp, FY, P(item=item))
        c.execute('SELECT COALESCE(SUM(D1),0) FROM busy_tran4 WHERE company_code=? AND fy_year=? AND MasterCode1=?', (comp, FY, item))
        t4 = c.fetchone()[0]
        c.execute('''SELECT COALESCE(SUM(Value1),0) FROM busy_tran2 WHERE company_code=? AND fy_year=? AND RecType=2
                     AND MasterCode1=?''', (comp, FY, item))
        t2 = c.fetchone()[0]
        T = S['totals']
        if S.get('error') or T['batch_count'] != nb or not close(T['closing'], q) or not close(T['opening'], op) \
                or not close(T['opening'] + T['in'] - T['out'], T['closing']) \
                or not close(S['stock']['stock_qty'], t4 + t2) \
                or not close(S['stock']['not_batch_tracked'], t4 + t2 - q):
            bad += 1
        if S['stock']['has_difference']:
            diffs += 1
            diff_list.append((S['item']['Name'], S['stock']['stock_qty'], S['stock']['batch_closing'], S['stock']['not_batch_tracked']))
    check(bad == 0, f'item batch summary totals / stock compare == SQL for all {len(items)} items')
    print(f'    items where stock qty <> batch closing ("not batch-tracked"): {diffs} {diff_list[:5]}')
    if s['mcs']:
        m = s['mcs'][0]
        d = rb.batch_mc_stock(conn, comp, FY, P(mc=m['Code']))
        it = d['items'][0]
        S = rb.batch_item_summary(conn, comp, FY, P(item=it['Code'], mc=m['Code'], hide_zero=1))
        check(close(S['totals']['closing'], it['closing']), f'item summary for one MC == MC drill ({it["Name"]} @ {m["Name"]}: {S["totals"]["closing"]})')
    S = rb.batch_item_summary(conn, comp, FY, P(item=0))
    check(S.get('error') == 'Item not found', 'missing item handled')
    print(f'  batch-items sample: {[i["Name"] for i in rb.batch_items(conn, comp, FY, P(search="a", limit=3))["items"]]}')
    print(f'  ({time.time() - t0:.1f}s)')

# timing of the heaviest call
t0 = time.time()
rb.batch_mc_stock(conn, 'COMP0044', FY, {})
print(f'batch-mc-stock summary COMP0044: {time.time() - t0:.2f}s')
print('\nALL OK' if not FAILS else f'\n{len(FAILS)} FAILED: {FAILS}')
