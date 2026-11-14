"""Compare a freshly synced DB with the current one, table by table, for the (company, fy) pairs both contain.
Usage: py scratch/compare_sync_db.py busy_local.db busy_local.new.db
"""
import sqlite3
import sys
from collections import Counter

OLD, NEW = sys.argv[1], sys.argv[2]
o = sqlite3.connect(f'file:{OLD}?mode=ro', uri=True)
n = sqlite3.connect(f'file:{NEW}?mode=ro', uri=True)


def r2(v):
    return round(v, 2) if isinstance(v, float) else (v.strip() if isinstance(v, str) else v)


def rows(conn, sql, args):
    return [tuple(r2(x) for x in row) for row in conn.execute(sql, args)]


def cols(conn, table):
    return [r[1] for r in conn.execute(f'PRAGMA table_info({table})')]


CHECKS = {
    # table: (key columns, compared columns, extra where)
    'busy_master1': (['Code'], ['MasterType', 'Name', 'Alias', 'PrintName', 'ParentGrp', 'Stamp', 'MasterSeriesGrp', 'CM1', 'CM2', 'HSNCode'], ''),
    'busy_master1 (accounts OpBal)': (['Code'], ['OpBal'], 'AND MasterType = 2'),
    'busy_masteraddressinfo': (['MasterCode'], ['GSTNo', 'Address1', 'Address2', 'Mobile', 'Email', 'StateCode', 'PinCode'], ''),
    'busy_tran1': (['VchCode'], ['VchType', 'VchNo', 'Date', 'MasterCode1', 'MasterCode2', 'Cancelled', 'VchAmt', 'Stamp', 'VchSeriesCode'], ''),
    'busy_tran2': (['VchCode', 'SrNo', 'RecType'], ['MasterCode1', 'MasterCode2', 'Value1', 'Value2', 'Value3'], ''),
    'busy_tran3': (['VchCode', 'SrNo', 'RecType', 'RefCode'], ['MasterCode1', 'MasterCode2', 'Method', 'BillNo', 'BillDate', 'DueDate', 'Value1', 'Narration'], ''),
    'busy_tran4': (['MasterCode1', 'MasterCode2', 'SrNo'], ['D1', 'D2', 'D3', 'RecType'], ''),
    'busy_billingdet': (['VchCode'], ['PartyName', 'Address1', 'Address2', 'Address3', 'Address4', 'MobileNo', 'Email', 'GSTNo'], ''),
    'busy_vchotherinfo': (['VchCode'], ['Transport', 'Station', 'GrNo', 'VehicleNo', 'TotalQty', 'GrDate'], ''),
    'busy_item_ext': (['VchCode', 'SrNo', 'RecType'], ['Rate', 'GrossAmt', 'NetRate'], ''),
    'busy_sundry_info': (['Code'], ['Additive'], ''),
    'busy_gst_itemwise': (['VchCode', 'ItemSrNo', 'ItemCode'], ['TaxableAmt', 'TaxAmt', 'TaxAmt1', 'HSNCode', 'GSTIN', 'VchDate'], ''),
}

pairs = sorted(set(o.execute('SELECT DISTINCT company_code, fy_year FROM busy_tran1')) &
               set(n.execute('SELECT DISTINCT company_code, fy_year FROM busy_tran1')))
print('common (company, fy):', pairs)
print('new DB years:', n.execute('SELECT company_code, fy_year, COUNT(*) FROM busy_tran1 GROUP BY 1, 2').fetchall())
total_bad = 0
for comp, fy in pairs:
    print(f'===== {comp} FY {fy}')
    for label, (keys, comp_cols, extra) in CHECKS.items():
        table = label.split(' ')[0]
        has_fy_old = 'fy_year' in cols(o, table)
        fy_sql = 'AND fy_year = ?' if has_fy_old else ''
        args_old = (comp, fy) if has_fy_old else (comp,)
        sel = ', '.join(keys + comp_cols)
        a = Counter(rows(o, f'SELECT {sel} FROM {table} WHERE company_code = ? {fy_sql} {extra}', args_old))
        b = Counter(rows(n, f'SELECT {sel} FROM {table} WHERE company_code = ? AND fy_year = ? {extra}', (comp, fy)))
        only_old, only_new = a - b, b - a
        status = 'OK' if not only_old and not only_new else 'DIFF'
        total_bad += status != 'OK'
        print(f'  {status:4} {label}: old={sum(a.values())} new={sum(b.values())} only_old={sum(only_old.values())} only_new={sum(only_new.values())}')
        if status != 'OK':
            # show which columns differ for matching keys
            ko = {r[:len(keys)]: r for r in only_old}
            kn = {r[:len(keys)]: r for r in only_new}
            diffcols = Counter()
            for k in set(ko) & set(kn):
                for idx, c in enumerate(comp_cols):
                    if ko[k][len(keys) + idx] != kn[k][len(keys) + idx]:
                        diffcols[c] += 1
            print(f'       differing columns on same key: {dict(diffcols)}')
            for k in list(set(ko) & set(kn))[:2]:
                print(f'       e.g. key={k} old={ko[k][len(keys):]} new={kn[k][len(keys):]}')
            for k in list(set(ko) - set(kn))[:2]:
                print(f'       only old: {ko[k]}')
            for k in list(set(kn) - set(ko))[:2]:
                print(f'       only new: {kn[k]}')
    print('  company row old:', o.execute('SELECT company_code, bdep_id, name, gstin, beg_fy FROM busy_company WHERE company_code = ?', (comp,)).fetchone())
    print('  company row new:', n.execute('SELECT company_code, bdep_id, name, gstin, beg_fy FROM busy_company WHERE company_code = ?', (comp,)).fetchone())
print('print_config rows old/new:', o.execute('SELECT COUNT(*) FROM busy_print_config').fetchone(), n.execute('SELECT COUNT(*) FROM busy_print_config').fetchone())
print('TOTAL DIFF TABLES:', total_bad)
