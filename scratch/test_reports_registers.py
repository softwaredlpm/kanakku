"""Verification for reports_registers.py against the real busy_local.db (read-only), both companies, FY 2026."""
import os, sys, sqlite3, json, re, subprocess, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import reports_registers as rr
import reports_inventory as ri

conn = sqlite3.connect('file:D:/Busy Mobile App/busy_local.db?mode=ro', uri=True)
conn.row_factory = sqlite3.Row
FY = '2026'
FAIL = []


def check(label, a, b, tol=0.05):
    ok = abs((a or 0) - (b or 0)) <= tol
    print(f"  [{'OK' if ok else 'FAIL'}] {label}: report={a:,.3f} sql={b:,.3f}")
    if not ok:
        FAIL.append(label)


def truth(label, cond, info=''):
    print(f"  [{'OK' if cond else 'FAIL'}] {label} {info}")
    if not cond:
        FAIL.append(label)


def sql_type(comp, vt, frm='0000', to='9999'):
    return conn.execute("SELECT COUNT(*), COALESCE(SUM(VchAmt),0) FROM busy_tran1 WHERE company_code=? AND fy_year=? "
                        "AND VchType=? AND Cancelled=0 AND Date BETWEEN ? AND ?", (comp, FY, vt, frm, to)).fetchone()


def compute_item_stock(c, company, fy):
    """Verbatim copy of server.compute_item_stock (mc_code=0) for cross-checking."""
    out = {}
    def row(code):
        return out.setdefault(code, {'OpQty': 0.0, 'OpValue': 0.0, 'MovementQty': 0.0, '_rq': 0.0, '_rv': 0.0})
    for code, q, v in c.execute('SELECT MasterCode1, SUM(D1), SUM(D3) FROM busy_tran4 WHERE company_code=? AND fy_year=? GROUP BY MasterCode1', (company, fy)):
        r = row(code); r['OpQty'] = q or 0.0; r['OpValue'] = v or 0.0
    for code, q in c.execute('SELECT MasterCode1, SUM(Value1) FROM busy_tran2 WHERE company_code=? AND fy_year=? AND RecType=2 GROUP BY MasterCode1', (company, fy)):
        row(code)['MovementQty'] = q or 0.0
    for code, q, v in c.execute('SELECT MasterCode1, SUM(D1), SUM(D3) FROM busy_tran4 WHERE company_code=? AND fy_year=? GROUP BY MasterCode1', (company, fy)):
        r = row(code); r['_rq'] += q or 0.0; r['_rv'] += v or 0.0
    for code, q, v in c.execute('''SELECT t2.MasterCode1, SUM(t2.Value1), SUM(t2.Value3) FROM busy_tran2 t2
            JOIN busy_tran1 t1 ON t1.company_code=t2.company_code AND t1.fy_year=t2.fy_year AND t1.VchCode=t2.VchCode
            WHERE t2.company_code=? AND t2.fy_year=? AND t2.RecType=2 AND t2.Value1>0 AND t1.VchType<>5 AND t1.Cancelled=0
            GROUP BY t2.MasterCode1''', (company, fy)):
        r = row(code); r['_rq'] += q or 0.0; r['_rv'] += v or 0.0
    for r in out.values():
        rq, rv = r.pop('_rq'), r.pop('_rv')
        rate = rv / rq if rq > 0 else 0.0
        r['ClosingQty'] = round(r['OpQty'] + r['MovementQty'], 3)
        r['ClosingValue'] = round(r['ClosingQty'] * rate, 2)
    return out


t0 = time.time()
for comp in ('COMP0044', 'COMP0027'):
    print(f"\n===== {comp} =====")
    # ---------- 1. voucher registers
    print("-- Voucher registers")
    for key in rr.VOUCHER_TYPE_ORDER:
        vt = rr.VOUCHER_TYPES[key][0]
        d = rr.registers_voucher_register(conn, comp, FY, {'type': [key]})
        n, s = sql_type(comp, vt)
        g = d['grand']
        print(f" {d['title']}: {g['count']} vouchers, {g['amount']:,.2f}, {len(d['months'])} months")
        check(f"{comp} {key} grand == SUM(VchAmt)", g['amount'], s)
        check(f"{comp} {key} count", g['count'], n, 0)
        check(f"{comp} {key} months sum", sum(m['amount'] for m in d['months']), s)
        if d['kind'] == 'acc' and d['rows']:
            bad = [r for r in d['rows'] if abs(sum(x['amount'] for x in r['dr']) - sum(x['amount'] for x in r['cr'])) > 0.01]
            truth(f"{comp} {key} every voucher Dr == Cr", not bad, f"({len(bad)} unbalanced)")
            nolines = [r for r in d['rows'] if not r['dr'] and not r['cr']]
            truth(f"{comp} {key} every voucher has lines", not nolines, f"({len(nolines)} without lines, "
                  f"{sum(1 for r in d['rows'] if r['pdc'])} from RecType 5)")
        if d['kind'] == 'salary' and d['rows']:
            g_ok = sum(1 for r in d['rows'] if abs(r['gross_pay'] - r['amount']) <= 0.01)
            n_ok = sum(1 for r in d['rows'] if abs(r['net_pay'] - r['amount']) <= 0.01)
            print(f"   salary: VchAmt == employee gross (RecType 51 Value1) in {g_ok}/{len(d['rows'])}, "
                  f"== employee net (Value3) in {n_ok}/{len(d['rows'])} vouchers (info only, not a check)")
        if d['kind'] == 'stock' and d['rows']:
            print(f"   total qty {d['grand']['qty']:,.3f}; no-MC rows {sum(1 for r in d['rows'] if not r['from_mc'] and not r['to_mc'])}")
            if vt == 5:
                hdr = {r[0]: (r[1], r[2]) for r in conn.execute(
                    "SELECT VchCode, MasterCode1, MasterCode2 FROM busy_tran1 WHERE company_code=? AND fy_year=? AND VchType=5", (comp, FY))}
                nm = {r[0]: r[1] for r in conn.execute("SELECT Code, Name FROM busy_master1 WHERE company_code=? AND fy_year=?", (comp, FY))}
                mism = sum(1 for r in d['rows'] if r['from_mc'] != [nm.get(hdr[r['vch_code']][0], '')] or r['to_mc'] != [nm.get(hdr[r['vch_code']][1], '')])
                truth(f"{comp} stock transfer from/to MC (line signs) == header MC1/MC2", mism == 0, f"({mism} differ)")
        if d['rows']:
            print("   sample:", json.dumps({k: v for k, v in d['rows'][0].items() if k not in ('dr', 'cr', 'employees')}, ensure_ascii=False)[:300])
    d = rr.registers_voucher_register(conn, comp, FY, {'type': ['payment'], 'from': ['2026-05-01'], 'to': ['2026-05-31']})
    check(f"{comp} payment May grand", d['grand']['amount'], sql_type(comp, 19, '2026-05-01', '2026-05-31')[1])

    # ---------- 2. challan / order registers
    print("-- Challan / order registers")
    for key in rr.CHALLAN_TYPE_ORDER:
        vt, pkind, _lr = rr.CHALLAN_TYPES[key]
        d = rr.registers_challan_register(conn, comp, FY, {'type': [key]})
        n, s = sql_type(comp, vt)
        st = d['status_totals']
        print(f" {d['title']}: {d['grand']['count']} vouchers, {d['grand']['amount']:,.2f}; " +
              ', '.join(f"{v['label']} {v['count']} ({v['amount']:,.2f})" for v in st.values()))
        check(f"{comp} {key} grand == SUM(VchAmt)", d['grand']['amount'], s)
        check(f"{comp} {key} count", d['grand']['count'], n, 0)
        check(f"{comp} {key} status amounts sum", sum(v['amount'] for v in st.values()), s)
        inv = ri.inventory_pending(conn, comp, FY, {'type': [pkind]})
        pend_vchs = {r['VchCode'] for r in inv['rows'] if r['VchCode'] > 0}
        status = {r['vch_code']: r['status'] for r in d['rows']}
        bad = [v for v in pend_vchs if status.get(v) in ('cleared', 'none')]
        missing = [v for v in pend_vchs if v not in status]
        truth(f"{comp} {key} inventory-pending vouchers are not Cleared", not bad and not missing,
              f"({len(pend_vchs)} pending vouchers in reports_inventory, {len(bad)} cleared here, {len(missing)} missing)")
        mine = {r['vch_code'] for r in d['rows'] if r['status'] in ('pending', 'partial')}
        truth(f"{comp} {key} every Pending/Partly voucher is in inventory-pending", mine <= pend_vchs,
              f"({len(mine - pend_vchs)} extra)")
        inv_pq = sum(r['PendingQty'] for r in inv['rows'] if r['VchCode'] > 0)
        check(f"{comp} {key} pending qty == inventory-pending qty (non-opening)",
              sum(r['pending_qty'] for r in d['rows']), inv_pq, 0.01)
        for sf in ('pending', 'partial', 'cleared', 'none'):
            ds = rr.registers_challan_register(conn, comp, FY, {'type': [key], 'status': [sf]})
            truth(f"{comp} {key} filter {sf}", all(r['status'] == sf for r in ds['rows']) and ds['grand']['count'] == st[sf]['count'], '')
        if d['rows']:
            r0 = next((r for r in d['rows'] if r['status'] == 'partial'), d['rows'][0])
            print("   sample:", json.dumps(r0, ensure_ascii=False)[:400])

    # ---------- 3. party x item
    print("-- Party x Item")
    for t, vt in (('sales', 9), ('purchase', 2)):
        by_party = rr.registers_party_item(conn, comp, FY, {'type': [t], 'mode': ['party']})
        by_item = rr.registers_party_item(conn, comp, FY, {'type': [t], 'mode': ['item']})
        sql_amt, sql_qty = conn.execute('''SELECT COALESCE(SUM(CASE WHEN COALESCE(x.GrossAmt,0)<>0 THEN abs(x.GrossAmt) ELSE abs(t2.Value3) END),0),
                COALESCE(SUM(abs(t2.Value1)),0) FROM busy_tran2 t2 JOIN busy_tran1 t1 USING(company_code, fy_year, VchCode)
                LEFT JOIN busy_item_ext x ON x.company_code=t2.company_code AND x.fy_year=t2.fy_year AND x.VchCode=t2.VchCode AND x.SrNo=t2.SrNo AND x.RecType=t2.RecType
                WHERE t2.company_code=? AND t2.fy_year=? AND t2.RecType IN (2,7) AND t1.VchType=? AND t1.Cancelled=0''', (comp, FY, vt)).fetchone()
        print(f" {t}: {by_party['totals']['count']} parties, {by_item['totals']['count']} items, amount {by_item['totals']['amount']:,.2f}; {by_item['amount_basis']}")
        check(f"{comp} {t} party-wise total == item-wise total", by_party['totals']['amount'], by_item['totals']['amount'])
        check(f"{comp} {t} item-wise total == SQL gross", by_item['totals']['amount'], sql_amt)
        check(f"{comp} {t} item-wise qty == SQL qty", by_item['totals']['qty'], sql_qty, 0.01)
        if by_party['rows']:
            p = by_party['rows'][0]
            dp = rr.registers_party_item(conn, comp, FY, {'type': [t], 'mode': ['party'], 'code': [str(p['code'])]})
            check(f"{comp} {t} party {p['name']} item drill == party total", dp['totals']['amount'], p['amount'])
            print("   sample item line:", json.dumps(dp['rows'][0], ensure_ascii=False))
            it = by_item['rows'][0]
            di = rr.registers_party_item(conn, comp, FY, {'type': [t], 'mode': ['item'], 'code': [str(it['code'])]})
            check(f"{comp} {t} item {it['name']} party drill == item total", di['totals']['amount'], it['amount'])
            check(f"{comp} {t} item {it['name']} party drill qty", di['totals']['qty'], it['qty'], 0.001)
            # sum of all party drills == grand (sampled: first 15 parties)
            tot = 0.0
            for pr in by_party['rows'][:15]:
                tot += rr.registers_party_item(conn, comp, FY, {'type': [t], 'code': [str(pr['code'])]})['totals']['amount']
            check(f"{comp} {t} first 15 party drills == their totals", tot, sum(pr['amount'] for pr in by_party['rows'][:15]))
    if comp == 'COMP0044':
        s = rr.registers_party_item(conn, comp, FY, {'type': ['sales'], 'mode': ['item']})
        check("COMP0044 sales gross == 39,186,115.80", s['totals']['amount'], 39186115.80)

    # ---------- 4. stock movement
    print("-- Stock movement")
    sm = rr.registers_stock_movement(conn, comp, FY, {'limit': ['0']})
    ref = compute_item_stock(conn.cursor(), comp, FY)
    tran4 = {r[0]: r[1] or 0.0 for r in conn.execute("SELECT MasterCode1, SUM(D1) FROM busy_tran4 WHERE company_code=? AND fy_year=? GROUP BY 1", (comp, FY))}
    bad_id = [r for r in sm['rows'] if abs(r['opening'] + r['inward'] - r['outward'] - r['closing']) > 0.0015]
    bad_op = [r for r in sm['rows'] if abs(r['opening'] - tran4.get(r['code'], 0.0)) > 0.0015]
    bad_cl = [r for r in sm['rows'] if abs(r['closing'] - ref.get(r['code'], {}).get('ClosingQty', 0.0)) > 0.0015]
    bad_tr = [r for r in sm['rows'] if abs(r['transfer_net']) > 0.0005]
    T = sm['totals']
    print(f" {T['items']} items: opening {T['opening']:,.3f} in {T['inward']:,.3f} out {T['outward']:,.3f} "
          f"transfers {T['transfer_in']:,.3f} closing {T['closing']:,.3f} value {T['closing_value']:,.2f}")
    truth(f"{comp} per item opening(Tran4.D1) + in - out == closing", not bad_id, f"({len(bad_id)} bad)")
    truth(f"{comp} per item opening == Tran4.D1", not bad_op, f"({len(bad_op)} bad)")
    truth(f"{comp} per item closing == compute_item_stock", not bad_cl, f"({len(bad_cl)} bad)")
    truth(f"{comp} VchType 5 transfers net to zero per item", not bad_tr, f"({len(bad_tr)} non-zero)")
    check(f"{comp} total closing qty == compute_item_stock", T['closing'], sum(r['ClosingQty'] for r in ref.values()), 0.002)
    check(f"{comp} closing value == compute_item_stock", T['closing_value'], sum(r['ClosingValue'] for r in ref.values()), 1)
    check(f"{comp} month in sum == total in", sum(m['inward'] for m in sm['months']), T['inward'], 0.01)
    check(f"{comp} month out sum == total out", sum(m['outward'] for m in sm['months']), T['outward'], 0.01)
    if comp == 'COMP0044':
        check("COMP0044 total closing qty == 161,352.122", T['closing'], 161352.122, 0.0005)
    if sm['rows']:
        busiest = max(sm['rows'], key=lambda r: r['outward'])
        di = rr.registers_stock_movement(conn, comp, FY, {'item': [str(busiest['code'])], 'limit': ['1']})['item']
        check(f"{comp} item {busiest['name']} month-wise final closing", di['months'][-1]['closing'], busiest['closing'], 0.001)
        print("   item detail:", json.dumps({k: di[k] for k in ('name', 'opening', 'inward', 'outward', 'closing')}, ensure_ascii=False),
              [(m['label'], m['inward'], m['outward']) for m in di['months'] if m['inward'] or m['outward']][:4])
    # sub-range: Jun..Jul opening = Tran4 + movement before Jun
    sub = rr.registers_stock_movement(conn, comp, FY, {'from': ['2026-06-01'], 'to': ['2026-07-31'], 'limit': ['0']})
    pre = conn.execute("SELECT COALESCE(SUM(t2.Value1),0) FROM busy_tran2 t2 JOIN busy_tran1 t1 USING(company_code,fy_year,VchCode) WHERE t2.company_code=? AND t2.fy_year=? AND t2.RecType=2 AND t1.Date < '2026-06-01'", (comp, FY)).fetchone()[0]
    upto = conn.execute("SELECT COALESCE(SUM(t2.Value1),0) FROM busy_tran2 t2 JOIN busy_tran1 t1 USING(company_code,fy_year,VchCode) WHERE t2.company_code=? AND t2.fy_year=? AND t2.RecType=2 AND t1.Date <= '2026-07-31'", (comp, FY)).fetchone()[0]
    check(f"{comp} Jun-Jul opening total", sub['totals']['opening'], sum(tran4.values()) + pre, 0.01)
    check(f"{comp} Jun-Jul closing total", sub['totals']['closing'], sum(tran4.values()) + upto, 0.01)

    sl = rr.registers_slow_moving(conn, comp, FY, {})
    print(f" Slow/non-moving (>{sl['days']} days before {sl['as_of']}, cutoff {sl['cutoff']}): {sl['totals']['count']} of "
          f"{sl['totals']['items_in_stock']} items in stock, {sl['totals']['never_out']} never out, qty {sl['totals']['closing_qty']:,.3f}, "
          f"value {sl['totals']['closing_value']:,.2f}")
    lastout = {r[0]: r[1] for r in conn.execute('''SELECT t2.MasterCode1, MAX(t1.Date) FROM busy_tran2 t2 JOIN busy_tran1 t1 USING(company_code,fy_year,VchCode)
        WHERE t2.company_code=? AND t2.fy_year=? AND t2.RecType=2 AND t2.Value1<0 AND t1.VchType<>5 AND t1.Cancelled=0 GROUP BY 1''', (comp, FY))}
    expect = {code for code, r in ref.items() if r['ClosingQty'] > 0.0005 and (not lastout.get(code) or lastout[code] < sl['cutoff'])}
    got = {r['code'] for r in sl['rows']}
    truth(f"{comp} slow-moving set == independent SQL", got == expect, f"({len(got)} vs {len(expect)})")
    check(f"{comp} slow-moving value == compute_item_stock value of that set", sl['totals']['closing_value'],
          sum(ref[c]['ClosingValue'] for c in expect), 0.5)
    sl30 = rr.registers_slow_moving(conn, comp, FY, {'days': ['30'], 'as_of': ['2026-10-20']})
    print(f" Slow (30 days to 2026-10-20): {sl30['totals']['count']} items, value {sl30['totals']['closing_value']:,.2f}")
    if sl['rows']:
        print("   sample:", json.dumps(sl['rows'][0], ensure_ascii=False))

    srch = rr.registers_search(conn, comp, FY, {'kind': ['item'], 'q': ['a'], 'limit': ['5']})
    truth(f"{comp} search items", len(srch['rows']) > 0, str([r['name'] for r in srch['rows'][:3]]))
    json.dumps(sm); json.dumps(sl)

# ---------- route table + JS syntax
truth("ROUTES has 6 endpoints", len(rr.ROUTES) == 6 and all(k.startswith('/api/reports/registers_') for k in rr.ROUTES))
html = open(os.path.join(ROOT, 'scratch', 'report_ui', 'registers.html'), encoding='utf-8').read()
m = re.search(r'<!-- SCRIPT -->\s*<script>(.*?)</script>\s*<!-- /SCRIPT -->', html, re.S)
truth("SCRIPT block found", bool(m))
if m:
    tmp = os.path.join(os.environ.get('TEMP', '.'), 'registers_script_check.js')
    open(tmp, 'w', encoding='utf-8').write(m.group(1))
    p = subprocess.run(['node', '--check', tmp], capture_output=True, text=True)
    truth("node --check SCRIPT block", p.returncode == 0, p.stderr.strip()[:300])
    for fn in re.findall(r'^\S+\|[^|]+\|[^|]+\|(\w+)\s*$', html.split('<!-- TILES -->')[1], re.M):
        truth(f"tile loader {fn} defined", re.search(r'function\s+' + fn + r'\s*\(', m.group(1)) is not None)
    for vid in re.findall(r'^([\w-]+)\|', html.split('<!-- TILES -->')[1], re.M):
        truth(f"tile view view-{vid} exists", f'id="view-{vid}"' in html)
    ids = re.findall(r'getElementById\(\s*[\'"]([\w-]+)[\'"]', m.group(1))
    truth("all getElementById ids prefixed registers-", all(i.startswith('registers-') for i in ids), str([i for i in ids if not i.startswith('registers-')]))

print(f"\nElapsed {time.time() - t0:.1f}s")
print("\nRESULT:", "ALL OK" if not FAIL else f"{len(FAIL)} FAILED: {FAIL}")
