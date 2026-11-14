"""Verification for reports_accounts.py against the real busy_local.db (read-only), both companies, FY 2026."""
import os, sys, sqlite3, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import reports_accounts as ra

conn = sqlite3.connect('file:D:/Busy Mobile App/busy_local.db?mode=ro', uri=True)
conn.row_factory = sqlite3.Row
FY = '2026'
FAIL = []


def check(label, a, b, tol=0.05):
    ok = abs((a or 0) - (b or 0)) <= tol
    print(f"  [{'OK' if ok else 'FAIL'}] {label}: report={a:,.2f} sql={b:,.2f}")
    if not ok:
        FAIL.append(label)


def sql_closing(comp, code):
    op = conn.execute("SELECT COALESCE(OpBal,0) FROM busy_master1 WHERE company_code=? AND fy_year=? AND MasterType=2 AND Code=?",
                      (comp, FY, code)).fetchone()[0]
    s = conn.execute("SELECT COALESCE(SUM(Value1),0) FROM busy_tran2 WHERE company_code=? AND fy_year=? AND RecType=1 AND MasterCode1=?",
                     (comp, FY, code)).fetchone()[0]
    return op - s


def sql_drcr(comp, code, frm='0000', to='9999'):
    return conn.execute("""SELECT COALESCE(SUM(CASE WHEN t.Value1<0 THEN -t.Value1 END),0), COALESCE(SUM(CASE WHEN t.Value1>0 THEN t.Value1 END),0)
        FROM busy_tran2 t JOIN busy_tran1 h ON h.company_code=t.company_code AND h.fy_year=t.fy_year AND h.VchCode=t.VchCode
        WHERE t.company_code=? AND t.fy_year=? AND t.RecType=1 AND t.MasterCode1=? AND h.Date BETWEEN ? AND ?""",
                        (comp, FY, code, frm, to)).fetchone()


for comp in ('COMP0044', 'COMP0027'):
    print(f"\n===== {comp} =====")
    for fn, label in ((ra.accounts_cash_book, 'Cash'), (ra.accounts_bank_book, 'Bank')):
        combined = fn(conn, comp, FY, {})
        print(f"{label} Book combined: {len(combined['accounts'])} accounts, {combined['count']} lines, "
              f"closing {combined['closing']:,.2f} {combined['closing_type']}")
        check(f"{comp} {label} combined closing", combined['closing_signed'],
              sum(sql_closing(comp, a['code']) for a in combined['accounts']))
        for a in combined['accounts']:
            r = fn(conn, comp, FY, {'code': [str(a['code'])]})
            print(f" {label} {a['code']} {a['name']}: open {r['opening']:,.2f} {r['opening_type']} "
                  f"Dr {r['total_debit']:,.2f} Cr {r['total_credit']:,.2f} close {r['closing']:,.2f} {r['closing_type']} ({r['count']} lines)")
            check(f"{comp} {label} {a['code']} closing == OpBal-SUM(Value1)", r['closing_signed'], sql_closing(comp, a['code']))
            dr, cr = sql_drcr(comp, a['code'])
            check(f"{comp} {label} {a['code']} debit", r['total_debit'], dr)
            check(f"{comp} {label} {a['code']} credit", r['total_credit'], cr)
            if r['rows']:
                check(f"{comp} {label} {a['code']} last running bal", r['rows'][-1]['balance'] * (1 if r['rows'][-1]['balance_type'] == 'Dr' else -1), r['closing_signed'])
            # mid-range: opening as of from
            sub = fn(conn, comp, FY, {'code': [str(a['code'])], 'from': ['2026-06-01'], 'to': ['2026-06-30']})
            dr0, cr0 = sql_drcr(comp, a['code'], '0000', '2026-05-31')
            op = conn.execute("SELECT COALESCE(OpBal,0) FROM busy_master1 WHERE company_code=? AND fy_year=? AND MasterType=2 AND Code=?", (comp, FY, a['code'])).fetchone()[0]
            check(f"{comp} {label} {a['code']} opening as of 2026-06-01", sub['opening_signed'], op + dr0 - cr0)
            dr1, cr1 = sql_drcr(comp, a['code'], '2026-06-01', '2026-06-30')
            check(f"{comp} {label} {a['code']} June closing", sub['closing_signed'], op + dr0 - cr0 + dr1 - cr1)
        if combined['rows']:
            print("  sample row:", json.dumps(combined['rows'][0], ensure_ascii=False))

    ds = ra.accounts_daily_summary(conn, comp, FY, {})
    s = conn.execute("SELECT COUNT(*), SUM(VchAmt) FROM busy_tran1 WHERE company_code=? AND fy_year=? AND Cancelled=0", (comp, FY)).fetchone()
    print(f"Daily Summary: {len(ds['days'])} days, {ds['grand_count']} vouchers, {ds['grand_amount']:,.2f}; cash net {ds['cash_net']:,.2f}, bank net {ds['bank_net']:,.2f}")
    check(f"{comp} daily grand amount == SUM(VchAmt)", ds['grand_amount'], s[1])
    check(f"{comp} daily grand count", ds['grand_count'], s[0], 0)
    check(f"{comp} daily sum of day amounts", sum(d['amount'] for d in ds['days']), s[1], 1)
    cash = ra.accounts_cash_book(conn, comp, FY, {}); bank = ra.accounts_bank_book(conn, comp, FY, {})
    check(f"{comp} daily cash net == cash book Dr-Cr", ds['cash_net'], cash['total_debit'] - cash['total_credit'])
    check(f"{comp} daily bank net == bank book Dr-Cr", ds['bank_net'], bank['total_debit'] - bank['total_credit'])
    ds2 = ra.accounts_daily_summary(conn, comp, FY, {'from': ['2026-05-01'], 'to': ['2026-05-31']})
    s2 = conn.execute("SELECT COALESCE(SUM(VchAmt),0) FROM busy_tran1 WHERE company_code=? AND fy_year=? AND Cancelled=0 AND Date BETWEEN '2026-05-01' AND '2026-05-31'", (comp, FY)).fetchone()[0]
    check(f"{comp} daily May total", ds2['grand_amount'], s2)

    # monthly summary for a sample of accounts with movement + the cash account
    accs = [r[0] for r in conn.execute("""SELECT MasterCode1 FROM busy_tran2 WHERE company_code=? AND fy_year=? AND RecType=1
                                         GROUP BY MasterCode1 ORDER BY COUNT(*) DESC LIMIT 8""", (comp, FY))]
    nfail_before = len(FAIL)
    for code in accs:
        ms = ra.accounts_monthly_summary(conn, comp, FY, {'code': [str(code)]})
        dr, cr = sql_drcr(comp, code)
        check(f"{comp} monthly {code} {ms['account_name'][:25]} sum debits", sum(m['debit'] for m in ms['months']), dr)
        check(f"{comp} monthly {code} sum credits", sum(m['credit'] for m in ms['months']), cr)
        check(f"{comp} monthly {code} closing", ms['closing_signed'], sql_closing(comp, code))
    # every account: closing check
    allacc = [r[0] for r in conn.execute("SELECT Code FROM busy_master1 WHERE company_code=? AND fy_year=? AND MasterType=2", (comp, FY))]
    bad = 0
    for code in allacc:
        ms = ra.accounts_monthly_summary(conn, comp, FY, {'code': [str(code)]})
        if abs(ms['closing_signed'] - sql_closing(comp, code)) > 0.05:
            bad += 1
    print(f"  monthly summary closing checked for all {len(allacc)} accounts: {bad} mismatches")
    if bad:
        FAIL.append(f'{comp} monthly all accounts')
    srch = ra.accounts_search(conn, comp, FY, {'q': ['cash']})
    print(f"  search 'cash': {len(srch['accounts'])} -> {[a['name'] for a in srch['accounts']][:5]}")
    print(f"  search '': {len(ra.accounts_search(conn, comp, FY, {})['accounts'])} (limit 50)")

    for t, vt in (('sales', 9), ('purchase', 2), ('sales_return', 3), ('purchase_return', 10)):
        reg = ra.accounts_register(conn, comp, FY, {'type': [t]})
        s = conn.execute("SELECT COUNT(*), COALESCE(SUM(VchAmt),0) FROM busy_tran1 WHERE company_code=? AND fy_year=? AND Cancelled=0 AND VchType=?", (comp, FY, vt)).fetchone()
        g = conn.execute("SELECT COALESCE(SUM(TaxableAmt),0), COALESCE(SUM(TaxAmt+TaxAmt1+ACessAmt+StateCessAmt),0) FROM busy_gst_itemwise WHERE company_code=? AND fy_year=? AND VchCode IN (SELECT VchCode FROM busy_tran1 WHERE company_code=? AND fy_year=? AND VchType=? AND Cancelled=0)", (comp, FY, comp, FY, vt)).fetchone()
        gr = reg['grand']
        print(f"{reg['title']}: {gr['count']} vchs, taxable {gr['taxable']:,.2f}, tax {gr['tax']:,.2f}, total {gr['total']:,.2f}, months {len(reg['months'])}, without GST rows {reg['without_gst']}")
        check(f"{comp} {t} grand total == SUM(VchAmt)", gr['total'], s[1])
        check(f"{comp} {t} count", gr['count'], s[0], 0)
        check(f"{comp} {t} taxable == SUM(gst TaxableAmt)", gr['taxable'], g[0])
        check(f"{comp} {t} tax == SUM(gst tax)", gr['tax'], g[1])
        check(f"{comp} {t} sum of month totals", sum(m['total'] for m in reg['months']), s[1])
        reg2 = ra.accounts_register(conn, comp, FY, {'type': [str(vt)], 'from': ['2026-05-01'], 'to': ['2026-05-31']})
        s2 = conn.execute("SELECT COALESCE(SUM(VchAmt),0) FROM busy_tran1 WHERE company_code=? AND fy_year=? AND Cancelled=0 AND VchType=? AND Date BETWEEN '2026-05-01' AND '2026-05-31'", (comp, FY, vt)).fetchone()[0]
        check(f"{comp} {t} May total", reg2['grand']['total'], s2)
        if reg['rows'] and t == 'sales':
            print("  sample:", json.dumps(reg['rows'][0], ensure_ascii=False))

# routes + JSON-serialisable
for path, h in ra.ROUTES.items():
    json.dumps(h(conn, 'COMP0044', FY, {'code': ['1']}))
print('\nAll routes JSON-serialisable:', list(ra.ROUTES))
print('\nRESULT:', 'ALL CHECKS PASSED' if not FAIL else f'{len(FAIL)} FAILED: {FAIL}')
