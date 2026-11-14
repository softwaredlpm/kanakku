"""Verification for reports_gst.py against the real busy_local.db (read-only). Run: py scratch/test_reports_gst.py"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import reports_gst as G  # noqa: E402

conn = sqlite3.connect('file:D:/Busy Mobile App/busy_local.db?mode=ro', uri=True)
conn.row_factory = sqlite3.Row
FY = 2026
P = {'from': ['2026-04-01'], 'to': ['2027-03-31']}
fails = []


def check(label, a, b, tol=0.05):
    ok = abs((a or 0) - (b or 0)) <= tol
    print('  [%s] %-58s %16.2f vs %16.2f' % ('OK ' if ok else 'BAD', label, a or 0, b or 0))
    if not ok:
        fails.append(label)


def one(sql, *a):
    r = conn.execute(sql, a).fetchone()
    return r[0] if r else None


for co in ('COMP0044', 'COMP0027'):
    print('=' * 100)
    print(co)
    r1 = G.gst_gstr1(conn, co, FY, P)
    r3 = G.gst_gstr3b(conn, co, FY, P)
    rh = G.gst_hsn(conn, co, FY, P)
    json.dumps([r1, r3, rh])  # serialisable
    t = r1['totals']
    print('GSTR-1: B2B %d inv, B2C %d inv, CDN %d' % (t['b2b']['invoices'], t['b2c']['invoices'], t['credit_notes']['notes']))
    for k in ('b2b', 'b2c', 'outward', 'credit_notes', 'net_outward', 'nil_exempt_included'):
        x = t[k]
        print('   %-20s taxable %14.2f cgst %12.2f sgst %12.2f igst %12.2f cess %6.2f total %14.2f'
              % (k, x['taxable'], x['cgst'], x['sgst'], x['igst'], x['cess'], x['total']))
    for h in ('taxable', 'cgst', 'sgst', 'igst', 'tax'):
        check('B2B + B2C == outward (%s)' % h, t['b2b'][h] + t['b2c'][h], t['outward'][h])
    base = ("FROM busy_gst_itemwise g JOIN busy_tran1 h ON h.company_code=g.company_code AND h.fy_year=g.fy_year "
            "AND h.VchCode=g.VchCode WHERE g.company_code=? AND g.fy_year=? AND h.Cancelled=0 AND g.VchType=?")
    check('outward taxable == SUM(TaxableAmt) VchType 9', t['outward']['taxable'], one('SELECT SUM(TaxableAmt) ' + base, co, FY, 9))
    check('credit notes taxable == -SUM(TaxableAmt) VchType 3', t['credit_notes']['taxable'], -(one('SELECT SUM(TaxableAmt) ' + base, co, FY, 3) or 0))
    check('B2B invoices + B2C invoices == outward invoices', t['b2b']['invoices'] + t['b2c']['invoices'], t['outward']['invoices'], 0)

    # field meaning: LocType 1 never has TaxAmt1, LocType 0 CGST == SGST
    print('  LocType1 rows with TaxAmt1<>0:', one("SELECT COUNT(*) FROM busy_gst_itemwise WHERE company_code=? AND LocType=1 AND TaxAmt1<>0", co),
          '| LocType0 rows CGST<>SGST:', one("SELECT COUNT(*) FROM busy_gst_itemwise WHERE company_code=? AND LocType=0 AND abs(TaxAmt-TaxAmt1)>0.011", co),
          '| rows with cess:', one("SELECT COUNT(*) FROM busy_gst_itemwise WHERE company_code=? AND (ACessAmt<>0 OR StateCessAmt<>0)", co))

    # ---- voucher amount vs taxable+tax
    print('Voucher check: VchAmt - (taxable + tax), explained by bill sundries not in GST table')
    rows = conn.execute("""
        WITH g AS (SELECT VchCode, SUM(TaxableAmt+TaxAmt+TaxAmt1+ACessAmt+StateCessAmt) t FROM busy_gst_itemwise
                   WHERE company_code=? AND fy_year=? GROUP BY 1),
             rnd AS (SELECT t.VchCode, SUM(CASE WHEN COALESCE(s.Additive,1)=1 THEN t.Value3 ELSE -t.Value3 END) r
                     FROM busy_tran2 t JOIN busy_master1 m ON m.company_code=t.company_code AND m.fy_year=t.fy_year AND m.Code=t.MasterCode1
                     LEFT JOIN busy_sundry_info s ON s.company_code=t.company_code AND s.fy_year=t.fy_year AND s.Code=t.MasterCode1
                     WHERE t.company_code=? AND t.fy_year=? AND t.RecType=3 AND m.Name LIKE 'Round%' GROUP BY 1)
        SELECT h.VchType, h.VchCode, h.VchNo, h.VchAmt, g.t, COALESCE(rnd.r,0) r FROM g
        JOIN busy_tran1 h ON h.company_code=? AND h.fy_year=? AND h.VchCode=g.VchCode AND h.Cancelled=0
        LEFT JOIN rnd ON rnd.VchCode=g.VchCode""", (co, FY, co, FY, co, FY)).fetchall()
    by = {}
    resid = []
    for r in rows:
        b = by.setdefault(r['VchType'], [0, 0, 0, 0.0, 0.0, 0.0])
        gap = r['VchAmt'] - r['t']
        b[0] += 1
        b[1] += abs(gap) < 0.011
        b[2] += abs(gap - r['r']) < 0.011
        b[3] += gap
        b[4] += r['r']
        if abs(gap - r['r']) >= 0.011:
            resid.append((r['VchType'], r['VchCode'], r['VchNo'].strip(), round(r['VchAmt'], 2), round(r['t'], 2), round(gap, 2), round(r['r'], 2)))
    print('   VchType  vchs  exact  exact-after-roundoff  sum(gap)  sum(roundoff)')
    for vt, b in sorted(by.items()):
        print('   %7d %5d %6d %21d %10.2f %12.2f' % (vt, b[0], b[1], b[2], b[3], b[4]))
    print('   unexplained after round-off (type, vch, no, VchAmt, gst total, gap, roundoff): %d' % len(resid))
    for x in resid[:12]:
        print('     ', x)
    # explain each unexplained invoice by its bill sundries
    for x in [y for y in resid if y[0] not in (16, 17, 18)][:5]:
        bs = conn.execute("""SELECT m.Name, t.Value3, COALESCE(s.Additive,1) a FROM busy_tran2 t JOIN busy_master1 m
              ON m.company_code=t.company_code AND m.fy_year=t.fy_year AND m.Code=t.MasterCode1
              LEFT JOIN busy_sundry_info s ON s.company_code=t.company_code AND s.fy_year=t.fy_year AND s.Code=t.MasterCode1
              WHERE t.company_code=? AND t.fy_year=? AND t.VchCode=? AND t.RecType=3""", (co, FY, x[1])).fetchall()
        gross = one("SELECT SUM(GrossAmt) FROM busy_item_ext WHERE company_code=? AND fy_year=? AND VchCode=?", co, FY, x[1])
        print('      vch %s sundries: %s ; item gross %s' % (x[2], [(b['Name'], b['Value3'], b['a']) for b in bs], gross))
    print('   sales vouchers missing from GST table:',
          [dict(r) for r in conn.execute("""SELECT VchCode, trim(VchNo) no, Date, VchAmt FROM busy_tran1 WHERE company_code=? AND fy_year=?
              AND VchType IN (9,3,2,10) AND Cancelled=0 AND VchCode NOT IN (SELECT VchCode FROM busy_gst_itemwise WHERE company_code=?)""", (co, FY, co))])

    # ---- GSTR-3B vs ledgers
    print('GSTR-3B:')
    for k in ('output_tax', 'rcm_liability', 'net_payable'):
        print('   %-14s %s' % (k, r3[k]))
    print('   itc total     %s' % r3['itc']['total'])
    print('   outward net taxable %.2f, nil/exempt %.2f; inward nil %.2f; RCM taxable %.2f'
          % (r3['outward']['taxable']['taxable'], r3['outward']['nil_exempt']['taxable'], r3['inward_nil_exempt']['taxable'], r3['rcm_inward']['taxable']))
    led = {(l['head'], l['kind'], l['rcm']): l for l in r3['ledgers']}
    for h in ('cgst', 'sgst', 'igst'):
        lo = led.get((h, 'output', False))
        li = led.get((h, 'input', False))
        if lo:
            check('%s Output ledger net Cr from Sales/SR == 3B output' % h.upper(), lo['invoice_net_cr'], r3['output_tax'][h])
        else:
            check('%s output (no ledger movement) == 0' % h.upper(), 0, r3['output_tax'][h])
        itc_inv = r3['itc']['purchases'][h] + r3['itc']['purchase_returns'][h]
        if li:
            check('%s Input ledger net Dr from Purch/PR == ITC on invoices' % h.upper(), -li['invoice_net_cr'], itc_inv)
        lr = led.get((h, 'output', True))
        if lr or r3['rcm_inward'][h]:
            jr = [x for x in (lr['by_vch_type'] if lr else []) if x['vch_type'] == 16]
            print('   %s Output (RCM) ledger journal Cr %.2f Dr %.2f ; RCM GST detail %.2f'
                  % (h.upper(), jr[0]['credit'] if jr else 0, jr[0]['debit'] if jr else 0, r3['rcm_inward'][h]))
    print('   GST ledgers (period movement):')
    for l in r3['ledgers']:
        print('     %-34s Dr %13.2f Cr %13.2f  by type %s' % (l['name'], l['debit'], l['credit'],
              [(x['vch_type'], x['debit'], x['credit']) for x in l['by_vch_type']]))
    print('   journals/notes with GST details: %d (rcm %d, posted to GST ledgers %d)' % (
        len(r3['journals_with_gst']), sum(v['is_rcm'] for v in r3['journals_with_gst']),
        sum(v['posted_to_gst_ledgers'] for v in r3['journals_with_gst'])))
    for v in r3['journals_with_gst']:
        print('     %s %-4s %-12s rec%s posted=%s taxable %10.2f cgst %8.2f igst %8.2f vchamt %10.2f %s' % (
            v['date'], v['vch_type'], v['vch_no'], v['gst_rec_types'], v['posted_to_gst_ledgers'], v['taxable'], v['cgst'], v['igst'], v['vch_amt'], v['party']))

    # ---- HSN
    ht = rh['totals']
    print('HSN: outward %d lines, inward %d lines' % (len(rh['outward']), len(rh['inward'])))
    print('   outward', ht['outward'])
    print('   inward ', ht['inward'])
    check('HSN outward taxable == 3B outward net taxable', ht['outward']['taxable'], r3['outward']['net']['taxable'])
    check('HSN outward tax == 3B output tax', ht['outward']['tax'], r3['output_tax']['tax'])
    check('HSN outward taxable == GSTR-1 net outward', ht['outward']['taxable'], r1['totals']['net_outward']['taxable'])
    check('HSN inward tax == ITC on invoices', ht['inward']['tax'], sum(r3['itc']['net_purchases'][h] for h in ('cgst', 'sgst', 'igst', 'cess')))
    qty = one("SELECT SUM(CASE WHEN g.VchType IN (9) THEN ItemMainQty ELSE -ItemMainQty END) " + base.replace('g.VchType=?', 'g.VchType IN (9,3)'), co, FY)
    check('HSN outward qty == SUM(ItemMainQty) sales - returns', ht['outward']['qty'], qty, 0.01)
    print('   blank HSN rows outward:', [o for o in rh['outward'] if o['hsn'] == '(blank)'][:2])

    # month param sums to FY
    months = ['2026-%02d' % m for m in range(4, 13)] + ['2027-%02d' % m for m in range(1, 4)]
    s = sum(G.gst_gstr1(conn, co, FY, {'month': [m]})['totals']['net_outward']['total'] for m in months)
    check('sum of monthly GSTR-1 net totals == FY', s, r1['totals']['net_outward']['total'])
    p0 = G.gst_gstr3b(conn, co, FY, {})
    print('   default period:', p0['from'], p0['to'])

print('=' * 100)
print('FAILURES:', fails or 'none')
