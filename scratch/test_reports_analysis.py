"""Verification for reports_analysis.py against the real busy_local.db (read-only), both companies."""
import os
import sqlite3
import sys
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import reports_analysis as ra  # noqa: E402

FY = 2026
EXPECT = {
    'COMP0044': {'sales_cnt': 1834, 'sales_amt': 39750967.00, 'purchase': 24998732.0, 'receipt': 36619449.0,
                 'payment': 35589508.24, 'diff_opening': 1147523.92},
    'COMP0027': {'diff_opening': 10068331.29},
}
fails = []


def check(name, a, b, tol=0.02):
    ok = abs((a or 0) - (b or 0)) <= tol
    print(f"  [{'OK' if ok else 'FAIL'}] {name}: {a:,.2f} vs {b:,.2f}")
    if not ok:
        fails.append(name)


conn = sqlite3.connect(f'file:{ROOT}/busy_local.db?mode=ro', uri=True)
conn.row_factory = sqlite3.Row
c = conn.cursor()

for co in ('COMP0044', 'COMP0027'):
    print(f'=== {co} FY {FY}')
    exp = EXPECT[co]

    # 1. party-wise
    for t, vt in (('sales', 9), ('purchase', 2)):
        r = ra.analysis_partywise(conn, co, FY, {'type': [t]})
        cnt, amt = c.execute('SELECT COUNT(*), COALESCE(SUM(VchAmt),0) FROM busy_tran1 WHERE company_code=? AND fy_year=? '
                             'AND VchType=? AND Cancelled=0', (co, FY, vt)).fetchone()
        print(f' party-wise {t}: {r["parties"]} parties')
        check(f'{co} party-wise {t} amount == SUM(VchAmt)', r['total_amount'], amt)
        check(f'{co} party-wise {t} invoices == COUNT', r['total_invoices'], cnt, 0)
        check(f'{co} party-wise {t} share sums to 100', sum(x['share'] for x in r['rows']), 100.0 if r['rows'] else 0, 0.2)
        if t == 'sales' and 'sales_cnt' in exp:
            check(f'{co} sales invoices == BUSY', r['total_invoices'], exp['sales_cnt'], 0)
            check(f'{co} sales amount == BUSY', r['total_amount'], exp['sales_amt'])
        if r['rows']:
            top = r['rows'][0]
            d = ra.analysis_party_invoices(conn, co, FY, {'type': [t], 'code': [str(top['code'])]})
            print(f'  top party: {top["name"]} {top["amount"]:,.2f} ({top["share"]}%), {top["invoices"]} invoices')
            check(f'{co} drill-down {t} top party total', d['total_amount'], top['amount'])
            check(f'{co} drill-down {t} top party count', d['count'], top['invoices'], 0)
    # date-range filter
    r = ra.analysis_partywise(conn, co, FY, {'type': ['sales'], 'from': ['2026-05-01'], 'to': ['2026-05-31']})
    amt = c.execute("SELECT COALESCE(SUM(VchAmt),0) FROM busy_tran1 WHERE company_code=? AND fy_year=? AND VchType=9 "
                    "AND Cancelled=0 AND Date BETWEEN '2026-05-01' AND '2026-05-31'", (co, FY)).fetchone()[0]
    check(f'{co} party-wise sales May', r['total_amount'], amt)

    # 2. item-wise: gross + signed sundries == VchAmt
    for t, vt in (('sales', 9), ('purchase', 2)):
        r = ra.analysis_itemwise(conn, co, FY, {'type': [t]})
        sund = c.execute('''SELECT COALESCE(SUM(CASE WHEN COALESCE(si.Additive,1)=1 THEN ABS(t2.Value3) ELSE -ABS(t2.Value3) END),0)
                            FROM busy_tran2 t2 JOIN busy_tran1 t1 USING(company_code, fy_year, VchCode)
                            LEFT JOIN busy_sundry_info si ON si.company_code=t2.company_code AND si.fy_year=t2.fy_year AND si.Code=t2.MasterCode1
                            WHERE t2.company_code=? AND t2.fy_year=? AND t1.VchType=? AND t1.Cancelled=0 AND t2.RecType=3''',
                         (co, FY, vt)).fetchone()[0]
        amt = c.execute('SELECT COALESCE(SUM(VchAmt),0) FROM busy_tran1 WHERE company_code=? AND fy_year=? AND VchType=? AND Cancelled=0',
                        (co, FY, vt)).fetchone()[0]
        net = c.execute('''SELECT COALESCE(SUM(ABS(t2.Value3)),0) FROM busy_tran2 t2 JOIN busy_tran1 t1 USING(company_code, fy_year, VchCode)
                           WHERE t2.company_code=? AND t2.fy_year=? AND t1.VchType=? AND t1.Cancelled=0 AND t2.RecType IN (2,7)''',
                        (co, FY, vt)).fetchone()[0]
        print(f' item-wise {t}: {r["items"]} items, gross {r["total_gross"]:,.2f}, net {r["total_net"]:,.2f}, '
              f'sundries {sund:,.2f}; qty by unit {r["qty_by_unit"]}')
        print(f'  direct (RecType 2): {r["direct_lines"]}  against challan (RecType 7): {r["against_challan_lines"]}')
        check(f'{co} item-wise {t} net == SUM(Value3)', r['total_net'], net)
        check(f'{co} item-wise {t} gross + sundries == SUM(VchAmt)', r['total_gross'] + sund, amt, 1.0)

    # 3. month-wise
    r = ra.analysis_monthwise(conn, co, FY, {})
    for vt, key, _ in ra.MONTH_TYPES:
        cnt, amt = c.execute('SELECT COUNT(*), COALESCE(SUM(VchAmt),0) FROM busy_tran1 WHERE company_code=? AND fy_year=? '
                             'AND VchType=? AND Cancelled=0', (co, FY, vt)).fetchone()
        print(f' month-wise {key}: {r["totals"][key]["count"]} vouchers')
        check(f'{co} month-wise {key} amount', r['totals'][key]['amount'], amt)
        check(f'{co} month-wise {key} count', r['totals'][key]['count'], cnt, 0)
        if key in exp:
            check(f'{co} month-wise {key} == dashboard', r['totals'][key]['amount'], exp[key])
    print('  ', [(x['label'], x['sales']['amount']) for x in r['rows'] if x['sales']['count']])

    # 4. group summary
    g = ra.analysis_group_summary(conn, co, FY, {})
    tot = g['total']
    d = g['difference_in_opening']
    print(f' group summary (primary): {len(g["rows"])} groups; opening {tot["opening_signed"]:,.2f}, '
          f'Dr {tot["debit"]:,.2f}, Cr {tot["credit"]:,.2f}, closing {tot["closing_signed"]:,.2f}; '
          f'{d["label"]} {d["amount"]:,.2f} (excess {d["excess_side"]}, shown on {d["balancing_side"]}), orphans {d["orphan_accounts"]}')
    check(f'{co} primary opening Dr-Cr == BUSY difference in opening', tot['opening_signed'], exp['diff_opening'])
    check(f'{co} primary closing Dr-Cr == difference in opening', tot['closing_signed'], exp['diff_opening'])
    check(f'{co} primary debit == credit', tot['debit'], tot['credit'])
    ind = c.execute('''SELECT COALESCE(SUM(m.OpBal),0) - COALESCE((SELECT SUM(Value1) FROM busy_tran2 WHERE company_code=? AND fy_year=? AND RecType=1),0)
                       FROM busy_master1 m WHERE m.company_code=? AND m.fy_year=? AND m.MasterType=2''', (co, FY, co, FY)).fetchone()[0]
    check(f'{co} primary closing == independent OpBal - SUM(Value1)', tot['closing_signed'], ind)
    # drill into each primary group: children totals == the group row
    for row in g['rows']:
        sub = ra.analysis_group_summary(conn, co, FY, {'code': [str(row['code'])]})
        if abs(sub['total']['closing_signed'] - row['closing_signed']) > 0.02 or abs(sub['total']['debit'] - row['debit']) > 0.02:
            fails.append(f'{co} drill {row["name"]}')
            print(f'  [FAIL] drill {row["name"]}')
    print('  drill-down into every primary group: children sum == group row checked')
    # independent check: Sundry Debtors tree closing
    sd = ra.analysis_group_summary(conn, co, FY, {'code': ['102']})
    sd_row = next((x for x in sd['rows'] if x['code'] == 116), None)
    if sd_row:
        ind = c.execute('''WITH RECURSIVE gt(code) AS (SELECT 116 UNION SELECT x.Code FROM busy_master1 x JOIN gt ON x.ParentGrp=gt.code
                           WHERE x.company_code=? AND x.fy_year=? AND x.MasterType=1)
                           SELECT COALESCE(SUM(m.OpBal),0) - COALESCE((SELECT SUM(t.Value1) FROM busy_tran2 t WHERE t.company_code=m.company_code
                             AND t.fy_year=m.fy_year AND t.RecType=1 AND t.MasterCode1 IN (SELECT Code FROM busy_master1 z WHERE z.company_code=?
                             AND z.fy_year=? AND z.MasterType=2 AND z.ParentGrp IN (SELECT code FROM gt))),0)
                           FROM busy_master1 m WHERE m.company_code=? AND m.fy_year=? AND m.MasterType=2 AND m.ParentGrp IN (SELECT code FROM gt)''',
                        (co, FY, co, FY, co, FY)).fetchone()[0]
        check(f'{co} Sundry Debtors (116 tree) closing', sd_row['closing_signed'], ind)

    # 5. P&L
    p = ra.analysis_pl(conn, co, FY, {})
    rev = ra.analysis_group_summary(conn, co, FY, {'code': ['109']})
    print(f' P&L: opening stock {p["opening_stock"]:,.2f}, closing stock (app) {p["closing_stock"]:,.2f}, '
          f'income {p["income_total"]:,.2f}, expense {p["expense_total"]:,.2f}, '
          f'{p["gross_result"]} {p["gross_profit"]:,.2f}, {p["net_result"]} {p["net_profit"]:,.2f}; warnings {p["warnings"]}')
    for side in ('dr', 'cr'):
        for h in p['trading'][side] + p['pl'][side]:
            print(f'    {side} {h["name"]}: {h["amount"]:,.2f}')
    check(f'{co} P&L income - expense == -(Revenue group summary closing)', p['income_total'] - p['expense_total'],
          -rev['total']['closing_signed'])
    check(f'{co} P&L net profit == -rev + closing - opening stock', p['net_profit'],
          -rev['total']['closing_signed'] + p['closing_stock'] - p['opening_stock'])
    ops = c.execute('SELECT COALESCE(SUM(D3),0) FROM busy_tran4 WHERE company_code=? AND fy_year=?', (co, FY)).fetchone()[0]
    check(f'{co} opening stock == SUM(D3)', p['opening_stock'], ops)
    json.dumps(p); json.dumps(g)

print()
print('ALL CHECKS PASSED' if not fails else f'FAILED: {fails}')
sys.exit(1 if fails else 0)
