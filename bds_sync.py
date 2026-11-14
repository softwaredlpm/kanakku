"""Multi-year BUSY -> busy_local.db sync.

For every company in bds_sync_config.json it finds every year file (db1YYYY.bds = FY YYYY-(YYYY+1)) and loads it
into the SQLite mirror with fy_year = YYYY. A year file is re-read only when its size or modified time changed,
so old years are copied once and the running year whenever BUSY writes to it.

Usage:
    py bds_sync.py                  # sync what changed
    py bds_sync.py --force          # re-read every year file
    py bds_sync.py --company COMP0044 --fy 2026 --db other.db
The server also runs sync_all() in a background thread (auto_sync_minutes in the config) and via /api/sync/run.

BUSY sign conventions are kept as-is (see AI_DEVELOPER_GUIDE.md); the only transformation is the account opening
balance: busy_master1.OpBal = -Folio1.D1 (positive = Debit).
Rows created inside the app (party / item / voucher codes above BUSY's highest code) are preserved.
"""
import csv
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'bds_sync_config.json')
DEFAULT_DB = os.path.join(BASE_DIR, 'busy_local.db')
EXPORTER = os.path.join(BASE_DIR, 'bds_sync_export.ps1')
YEAR_FILE = re.compile(r'^db1(\d{4})\.bds$', re.I)
csv.field_size_limit(2 ** 31 - 1)
_LOCK = threading.Lock()

SCHEMA = '''
CREATE TABLE IF NOT EXISTS busy_company (
    company_code TEXT PRIMARY KEY, bdep_id TEXT, name TEXT, print_name TEXT, gstin TEXT, mobile TEXT, email TEXT,
    beg_fy TEXT, cur_fy TEXT, last_synced_at DATETIME DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS busy_master1 (
    company_code TEXT NOT NULL, fy_year INTEGER NOT NULL, Code INTEGER NOT NULL, MasterType INTEGER, Name TEXT, Alias TEXT,
    PrintName TEXT, ParentGrp INTEGER, OpBal REAL DEFAULT 0, Stamp INTEGER, MasterSeriesGrp TEXT DEFAULT '',
    CM1 INTEGER DEFAULT 0, CM2 INTEGER DEFAULT 0, HSNCode TEXT DEFAULT '', PRIMARY KEY (company_code, fy_year, Code));
CREATE TABLE IF NOT EXISTS busy_masteraddressinfo (
    company_code TEXT NOT NULL, fy_year INTEGER NOT NULL, MasterCode INTEGER NOT NULL, GSTNo TEXT, Address1 TEXT,
    Address2 TEXT, Mobile TEXT, Email TEXT, StateCode TEXT, PinCode TEXT, PRIMARY KEY (company_code, fy_year, MasterCode));
CREATE TABLE IF NOT EXISTS busy_mastersupport (
    company_code TEXT NOT NULL, fy_year INTEGER NOT NULL, MasterCode INTEGER NOT NULL, MasterType INTEGER NOT NULL,
    CM1 INTEGER DEFAULT 0, CM2 INTEGER DEFAULT 0, PRIMARY KEY (company_code, fy_year, MasterCode));
CREATE TABLE IF NOT EXISTS busy_tran1 (
    company_code TEXT NOT NULL, fy_year INTEGER NOT NULL, VchCode INTEGER NOT NULL, VchType INTEGER, VchNo TEXT, Date TEXT,
    MasterCode1 INTEGER, MasterCode2 INTEGER, Cancelled INTEGER DEFAULT 0, VchAmt REAL DEFAULT 0, Stamp INTEGER,
    VchSeriesCode INTEGER DEFAULT 0, PRIMARY KEY (company_code, fy_year, VchCode));
CREATE TABLE IF NOT EXISTS busy_tran2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT, company_code TEXT NOT NULL, fy_year INTEGER NOT NULL, VchCode INTEGER NOT NULL,
    SrNo INTEGER NOT NULL, RecType INTEGER DEFAULT 1, MasterCode1 INTEGER, MasterCode2 INTEGER,
    Value1 REAL DEFAULT 0, Value2 REAL DEFAULT 0, Value3 REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS busy_tran3 (
    id INTEGER PRIMARY KEY AUTOINCREMENT, company_code TEXT NOT NULL, fy_year INTEGER NOT NULL, VchCode INTEGER NOT NULL,
    SrNo INTEGER NOT NULL, RecType INTEGER DEFAULT 1, MasterCode1 INTEGER, MasterCode2 INTEGER, RefCode INTEGER,
    Method INTEGER, BillNo TEXT, BillDate TEXT, DueDate TEXT, Value1 REAL DEFAULT 0, Narration TEXT);
CREATE TABLE IF NOT EXISTS busy_tran4 (
    company_code TEXT NOT NULL, fy_year INTEGER NOT NULL, SrNo INTEGER NOT NULL, MasterCode1 INTEGER, MasterCode2 INTEGER,
    D1 REAL DEFAULT 0, D2 REAL DEFAULT 0, D3 REAL DEFAULT 0, RecType INTEGER DEFAULT 0,
    PRIMARY KEY (company_code, fy_year, MasterCode1, MasterCode2, SrNo));
CREATE TABLE IF NOT EXISTS busy_billingdet (
    company_code TEXT, fy_year INTEGER, VchCode INTEGER, PartyName TEXT, Address1 TEXT, Address2 TEXT, Address3 TEXT,
    Address4 TEXT, MobileNo TEXT, Email TEXT, GSTNo TEXT, PRIMARY KEY (company_code, fy_year, VchCode));
CREATE TABLE IF NOT EXISTS busy_vchotherinfo (
    company_code TEXT, fy_year INTEGER, VchCode INTEGER, Transport TEXT, Station TEXT, GrNo TEXT, VehicleNo TEXT,
    TotalQty REAL, GrDate TEXT, PRIMARY KEY (company_code, fy_year, VchCode));
CREATE TABLE IF NOT EXISTS busy_item_ext (
    company_code TEXT NOT NULL, fy_year INTEGER NOT NULL, VchCode INTEGER NOT NULL, SrNo INTEGER NOT NULL,
    RecType INTEGER NOT NULL, Rate REAL DEFAULT 0, GrossAmt REAL DEFAULT 0, NetRate REAL DEFAULT 0,
    PRIMARY KEY (company_code, fy_year, VchCode, SrNo, RecType));
CREATE TABLE IF NOT EXISTS busy_sundry_info (
    company_code TEXT NOT NULL, fy_year INTEGER NOT NULL, Code INTEGER NOT NULL, Additive INTEGER DEFAULT 1,
    PRIMARY KEY (company_code, fy_year, Code));
CREATE TABLE IF NOT EXISTS busy_gst_itemwise (
    company_code TEXT NOT NULL, fy_year INTEGER NOT NULL, VchCode INTEGER, ItemCode INTEGER, ItemSrNo INTEGER,
    PartyCode INTEGER, VchType INTEGER, VchDate TEXT, HSNCode TEXT, TaxableAmt REAL, TaxRate REAL, TaxRate1 REAL,
    TaxAmt REAL, TaxAmt1 REAL, ACessAmt REAL, StateCessAmt REAL, LocType INTEGER, ZeroTaxType INTEGER, ItemMainQty REAL,
    RecType INTEGER, TypeOfDealer INTEGER, PartyStateCode INTEGER, POSStateCode INTEGER, GSTIN TEXT, TaxCatCode INTEGER);
CREATE TABLE IF NOT EXISTS busy_print_config (
    company_code TEXT PRIMARY KEY, title TEXT, company_name TEXT, address_line1 TEXT, address_line2 TEXT, declaration TEXT,
    terms_json TEXT, sign_for TEXT, sign_title TEXT, bank1 TEXT, bank2 TEXT);
CREATE TABLE IF NOT EXISTS busy_sync_state (
    company_code TEXT NOT NULL, fy_year INTEGER NOT NULL, file_path TEXT, file_size INTEGER, file_mtime REAL,
    synced_at TEXT, status TEXT, detail TEXT, PRIMARY KEY (company_code, fy_year));
CREATE INDEX IF NOT EXISTS idx_tran1_sales ON busy_tran1 (company_code, fy_year, VchType, Cancelled, Date);
CREATE INDEX IF NOT EXISTS idx_master1_grp ON busy_master1 (company_code, fy_year, ParentGrp);
CREATE INDEX IF NOT EXISTS idx_tran2_m1 ON busy_tran2 (company_code, fy_year, MasterCode1, RecType);
CREATE INDEX IF NOT EXISTS idx_tran2_vch ON busy_tran2 (company_code, fy_year, VchCode);
CREATE INDEX IF NOT EXISTS idx_tran3_comp_vch ON busy_tran3 (company_code, fy_year, VchCode);
CREATE INDEX IF NOT EXISTS idx_tran3_comp_rec_m1 ON busy_tran3 (company_code, fy_year, RecType, MasterCode1);
CREATE INDEX IF NOT EXISTS idx_tran3_comp_rec_m2 ON busy_tran3 (company_code, fy_year, RecType, MasterCode2);
CREATE INDEX IF NOT EXISTS idx_tran3_comp_bill ON busy_tran3 (company_code, fy_year, RecType, MasterCode1, BillNo);
CREATE INDEX IF NOT EXISTS idx_gst_vch ON busy_gst_itemwise (company_code, fy_year, VchCode);
'''

# name -> list of SQL variants (later variants are fallbacks for older BUSY versions missing a column)
FY_QUERIES = [
    ('master1', ["SELECT Code, MasterType, Name, Alias, PrintName, ParentGrp, MasterSeriesGrp, Stamp, CM1, CM2, HSNCode FROM Master1",
                 "SELECT Code, MasterType, Name, Alias, PrintName, ParentGrp, MasterSeriesGrp, Stamp, CM1, CM2 FROM Master1",
                 "SELECT Code, MasterType, Name, Alias, PrintName, ParentGrp, Stamp, CM1, CM2 FROM Master1"]),
    ('folio1', ["SELECT MasterCode, MasterType, D1 FROM Folio1"]),
    ('addr', ["SELECT MasterCode, GSTNo, Address1, Address2, Mobile, Email, StateCodeLong, PINCode FROM MasterAddressInfo",
              "SELECT MasterCode, Address1, Address2, Mobile, Email FROM MasterAddressInfo"]),
    ('tran1', ["SELECT VchCode, VchType, VchSeriesCode, VchNo, [Date], MasterCode1, MasterCode2, Cancelled, VchAmtBaseCur, Stamp FROM Tran1"]),
    ('tran2', ["SELECT VchCode, SrNo, RecType, MasterCode1, MasterCode2, Value1, Value2, Value3, D2, D5, D6 FROM Tran2"]),
    ('tran3', ["SELECT VchCode, SrNo, RecType, MasterCode1, MasterCode2, RefCode, Method, [No], [Date], DueDate, Value1, Narration FROM Tran3"]),
    ('tran4', ["SELECT SrNo, MasterCode1, MasterCode2, D1, D2, D3, RecType FROM Tran4"]),
    ('billing', ["SELECT VchCode, PartyName, Address1, Address2, Address3, Address4, MobileNo, Email, GSTNo FROM BillingDet"]),
    ('other', ["SELECT VchCode, Transport, Station, GrNo, VehicleNo, TotalQty, GrDate FROM VchOtherInfo"]),
    ('sundry', ["SELECT Code, I1 FROM Master1 WHERE MasterType = 9"]),
    ('gst', ["SELECT VchCode, ItemCode, ItemSrNo, PartyCode, VchType, VchDate, HSNCode, TaxableAmt, TaxRate, TaxRate1, TaxAmt, "
             "TaxAmt1, ACessAmt, StateCessAmt, LocType, ZeroTaxType, ItemMainQty, RecType, TypeOfDealer, PartyStateCode, "
             "POSStateCode, GSTIN, TaxCatCode FROM VchGSTSumItemWise"]),
]
REQUIRED = {'master1', 'folio1', 'tran1', 'tran2', 'tran3'}
COMPANY_QUERIES = [('company', ["SELECT TOP 1 Name, PrintName, GSTNo, BDEPID, BDEPMobile, BDEPEmailID, BegFY, CurFY FROM Company"])]


def load_config():
    with open(CONFIG_PATH, encoding='utf-8') as f:
        return json.load(f)


def connect(db_path):
    conn = sqlite3.connect(db_path, timeout=60)
    conn.execute('PRAGMA journal_mode=WAL')  # readers keep working while a year is being written
    return conn


def ensure_schema(conn):
    _migrate_add_fy(conn, 'busy_billingdet')
    _migrate_add_fy(conn, 'busy_vchotherinfo')
    conn.executescript(SCHEMA)


def _migrate_add_fy(conn, table):
    """Older DBs keyed busy_billingdet / busy_vchotherinfo by (company, VchCode) only; VchCode repeats across years."""
    cols = [r[1] for r in conn.execute(f'PRAGMA table_info({table})')]
    if not cols or 'fy_year' in cols:
        return
    data_cols = [c for c in cols if c != 'company_code']
    conn.execute(f'ALTER TABLE {table} RENAME TO {table}_old')
    conn.executescript(SCHEMA)  # creates the new table with fy_year
    conn.execute(f'''INSERT INTO {table} (company_code, fy_year, {", ".join(data_cols)})
                     SELECT o.company_code,
                            (SELECT t.fy_year FROM busy_tran1 t WHERE t.company_code = o.company_code AND t.VchCode = o.VchCode
                             ORDER BY t.fy_year DESC LIMIT 1),
                            {", ".join("o." + c for c in data_cols)}
                     FROM {table}_old o''')
    conn.execute(f'DROP TABLE {table}_old')
    conn.commit()


# ---------- TSV helpers ----------
def read_tsv(path):
    with open(path, encoding='utf-8', newline='') as f:
        reader = csv.reader(f, delimiter='\t', quoting=csv.QUOTE_NONE)
        header = next(reader, None) or []
        for row in reader:
            yield {h: (None if v == '\\N' else v) for h, v in zip(header, row)}


def s(v):
    return '' if v is None else v


def i(v):
    if v in (None, ''):
        return 0
    try:
        return int(float(v))
    except ValueError:
        return 0


def f(v):
    """Numbers; some BUSY columns (e.g. VchOtherInfo.TotalQty) are text like '   50,000.000'."""
    if v in (None, ''):
        return 0.0
    try:
        return float(v)
    except ValueError:
        try:
            return float(v.replace(',', '').strip() or 0)
        except ValueError:
            return 0.0


def iso_date(v):
    return '' if not v else v[:10]


def dmy_time(v):
    """'2026-10-05 00:00:00' -> '05-10-2026 00:00:00' (the format these columns already use)."""
    if not v:
        return ''
    try:
        return datetime.strptime(v[:19], '%Y-%m-%d %H:%M:%S').strftime('%d-%m-%Y %H:%M:%S')
    except ValueError:
        return v


def export(bds_path, queries, out_dir):
    qfile = os.path.join(out_dir, '_queries.json')
    with open(qfile, 'w', encoding='utf-8') as fh:
        json.dump([{'name': n, 'sql': sql} for n, sql in queries], fh)
    res = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', EXPORTER,
                          '-Bds', bds_path, '-QueryFile', qfile, '-OutDir', out_dir],
                         capture_output=True, text=True, timeout=3600)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout or 'export failed').strip()[-800:])
    errors = {}
    for name, _ in queries:
        err = os.path.join(out_dir, f'{name}.err')
        if os.path.exists(err):
            errors[name] = open(err, encoding='utf-8').read().strip()
    return errors


# ---------- loading ----------
def _clear(conn, table, comp, fy, key, keep_above):
    """Remove the rows this sync replaces; keep app-created rows (key above BUSY's highest code)."""
    if keep_above is None:
        conn.execute(f'DELETE FROM {table} WHERE company_code = ? AND fy_year = ?', (comp, fy))
    else:
        conn.execute(f'DELETE FROM {table} WHERE company_code = ? AND fy_year = ? AND {key} <= ?', (comp, fy, keep_above))


def load_year(conn, comp, fy, d):
    tsv = lambda n: os.path.join(d, f'{n}.tsv')
    has = lambda n: os.path.exists(tsv(n))
    counts = {}

    masters = list(read_tsv(tsv('master1')))
    max_code = max((i(r['Code']) for r in masters), default=0)
    tran1 = list(read_tsv(tsv('tran1')))
    max_vch = max((i(r['VchCode']) for r in tran1), default=0)
    opbal = {i(r['MasterCode']): -f(r['D1']) for r in read_tsv(tsv('folio1')) if i(r['MasterType']) == 2}

    conn.execute('BEGIN')
    try:
        _clear(conn, 'busy_master1', comp, fy, 'Code', max_code)
        conn.executemany('''INSERT OR REPLACE INTO busy_master1 (company_code, fy_year, Code, MasterType, Name, Alias, PrintName,
                            ParentGrp, OpBal, Stamp, MasterSeriesGrp, CM1, CM2, HSNCode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                         [(comp, fy, i(r['Code']), i(r['MasterType']), s(r['Name']), s(r['Alias']), s(r['PrintName']),
                           i(r['ParentGrp']), opbal.get(i(r['Code']), 0.0) if i(r['MasterType']) == 2 else 0.0, i(r['Stamp']),
                           s(r.get('MasterSeriesGrp')), i(r['CM1']), i(r['CM2']), s(r.get('HSNCode')).strip()) for r in masters])
        counts['master1'] = len(masters)

        if has('addr'):
            rows = list(read_tsv(tsv('addr')))
            _clear(conn, 'busy_masteraddressinfo', comp, fy, 'MasterCode', max_code)
            conn.executemany('''INSERT OR REPLACE INTO busy_masteraddressinfo (company_code, fy_year, MasterCode, GSTNo, Address1,
                                Address2, Mobile, Email, StateCode, PinCode) VALUES (?,?,?,?,?,?,?,?,?,?)''',
                             [(comp, fy, i(r['MasterCode']), s(r.get('GSTNo')), s(r['Address1']), s(r['Address2']),
                               s(r['Mobile']), s(r['Email']), s(r.get('StateCodeLong')), s(r.get('PINCode'))) for r in rows])
            counts['addr'] = len(rows)

        _clear(conn, 'busy_tran1', comp, fy, 'VchCode', max_vch)
        conn.executemany('''INSERT OR REPLACE INTO busy_tran1 (company_code, fy_year, VchCode, VchType, VchSeriesCode, VchNo, Date,
                            MasterCode1, MasterCode2, Cancelled, VchAmt, Stamp) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                         [(comp, fy, i(r['VchCode']), i(r['VchType']), i(r['VchSeriesCode']), s(r['VchNo']), iso_date(r['Date']),
                           i(r['MasterCode1']), i(r['MasterCode2']), 1 if i(r['Cancelled']) else 0, f(r['VchAmtBaseCur']),
                           i(r['Stamp'])) for r in tran1])
        counts['tran1'] = len(tran1)

        # Tran2 lines + item rate/gross extras (same rows)
        _clear(conn, 'busy_tran2', comp, fy, 'VchCode', max_vch)
        _clear(conn, 'busy_item_ext', comp, fy, 'VchCode', max_vch)
        t2, ext = [], []
        for r in read_tsv(tsv('tran2')):
            vc, sr, rt = i(r['VchCode']), i(r['SrNo']), i(r['RecType'])
            t2.append((comp, fy, vc, sr, rt, i(r['MasterCode1']), i(r['MasterCode2']), f(r['Value1']), f(r['Value2']), f(r['Value3'])))
            if rt in (2, 4, 7):
                ext.append((comp, fy, vc, sr, rt, f(r['D2']), f(r['D5']), f(r['D6'])))
        conn.executemany('''INSERT INTO busy_tran2 (company_code, fy_year, VchCode, SrNo, RecType, MasterCode1, MasterCode2,
                            Value1, Value2, Value3) VALUES (?,?,?,?,?,?,?,?,?,?)''', t2)
        conn.executemany('INSERT OR REPLACE INTO busy_item_ext VALUES (?,?,?,?,?,?,?,?)', ext)
        counts['tran2'], counts['item_ext'] = len(t2), len(ext)

        # Tran3: VchCode <= 0 are BUSY's opening / adjustment references, always replaced
        conn.execute('DELETE FROM busy_tran3 WHERE company_code = ? AND fy_year = ? AND VchCode <= ?', (comp, fy, max_vch))
        t3 = [(comp, fy, i(r['VchCode']), i(r['SrNo']), i(r['RecType']), i(r['MasterCode1']), i(r['MasterCode2']),
               i(r['RefCode']), i(r['Method']), s(r['No']).strip(), iso_date(r['Date']), iso_date(r['DueDate']),
               f(r['Value1']), s(r['Narration'])) for r in read_tsv(tsv('tran3'))]
        conn.executemany('''INSERT INTO busy_tran3 (company_code, fy_year, VchCode, SrNo, RecType, MasterCode1, MasterCode2,
                            RefCode, Method, BillNo, BillDate, DueDate, Value1, Narration) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', t3)
        counts['tran3'] = len(t3)

        conn.execute('DELETE FROM busy_tran4 WHERE company_code = ? AND fy_year = ?', (comp, fy))
        if has('tran4'):
            rows = [(comp, fy, i(r['SrNo']), i(r['MasterCode1']), i(r['MasterCode2']), f(r['D1']), f(r['D2']), f(r['D3']),
                     i(r['RecType'])) for r in read_tsv(tsv('tran4'))]
            conn.executemany('INSERT OR REPLACE INTO busy_tran4 (company_code, fy_year, SrNo, MasterCode1, MasterCode2, D1, D2, D3, '
                             'RecType) VALUES (?,?,?,?,?,?,?,?,?)', rows)
            counts['tran4'] = len(rows)

        if has('billing'):
            _clear(conn, 'busy_billingdet', comp, fy, 'VchCode', max_vch)
            rows = [(comp, fy, i(r['VchCode']), s(r['PartyName']), s(r['Address1']), s(r['Address2']), s(r['Address3']),
                     s(r['Address4']), s(r['MobileNo']), s(r['Email']), s(r['GSTNo'])) for r in read_tsv(tsv('billing'))]
            conn.executemany('INSERT OR REPLACE INTO busy_billingdet (company_code, fy_year, VchCode, PartyName, Address1, Address2, '
                             'Address3, Address4, MobileNo, Email, GSTNo) VALUES (?,?,?,?,?,?,?,?,?,?,?)', rows)
            counts['billing'] = len(rows)

        if has('other'):
            _clear(conn, 'busy_vchotherinfo', comp, fy, 'VchCode', max_vch)
            rows = [(comp, fy, i(r['VchCode']), s(r['Transport']), s(r['Station']), s(r['GrNo']), s(r['VehicleNo']),
                     f(r['TotalQty']), dmy_time(r['GrDate'])) for r in read_tsv(tsv('other'))]
            conn.executemany('INSERT OR REPLACE INTO busy_vchotherinfo (company_code, fy_year, VchCode, Transport, Station, GrNo, '
                             'VehicleNo, TotalQty, GrDate) VALUES (?,?,?,?,?,?,?,?,?)', rows)
            counts['other'] = len(rows)

        conn.execute('DELETE FROM busy_sundry_info WHERE company_code = ? AND fy_year = ?', (comp, fy))
        if has('sundry'):
            rows = [(comp, fy, i(r['Code']), 1 if i(r['I1']) == 1 else 0) for r in read_tsv(tsv('sundry'))]
            conn.executemany('INSERT OR REPLACE INTO busy_sundry_info VALUES (?,?,?,?)', rows)
            counts['sundry'] = len(rows)

        conn.execute('DELETE FROM busy_gst_itemwise WHERE company_code = ? AND fy_year = ?', (comp, fy))
        if has('gst'):
            rows = [(comp, fy, i(r['VchCode']), i(r['ItemCode']), i(r['ItemSrNo']), i(r['PartyCode']), i(r['VchType']),
                     iso_date(r['VchDate']), s(r['HSNCode']).strip(), f(r['TaxableAmt']), f(r['TaxRate']), f(r['TaxRate1']),
                     f(r['TaxAmt']), f(r['TaxAmt1']), f(r['ACessAmt']), f(r['StateCessAmt']), i(r['LocType']),
                     i(r['ZeroTaxType']), f(r['ItemMainQty']), i(r['RecType']), i(r['TypeOfDealer']), i(r['PartyStateCode']),
                     i(r['POSStateCode']), s(r['GSTIN']).strip(), i(r['TaxCatCode'])) for r in read_tsv(tsv('gst'))]
            conn.executemany(f'INSERT INTO busy_gst_itemwise VALUES ({",".join("?" * 25)})', rows)
            counts['gst'] = len(rows)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return counts


def sync_company_profile(conn, comp, comp_dir):
    main = next((os.path.join(comp_dir, n) for n in os.listdir(comp_dir) if n.lower() == 'db.bds'), None)
    if not main:
        return
    with tempfile.TemporaryDirectory(prefix='busy_sync_') as d:
        export(main, COMPANY_QUERIES, d)
        rows = list(read_tsv(os.path.join(d, 'company.tsv'))) if os.path.exists(os.path.join(d, 'company.tsv')) else []
    if not rows:
        return
    r = rows[0]
    conn.execute('''INSERT INTO busy_company (company_code, bdep_id, name, print_name, gstin, mobile, email, beg_fy, cur_fy, last_synced_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(company_code) DO UPDATE SET bdep_id = excluded.bdep_id, name = excluded.name,
                        print_name = excluded.print_name, gstin = excluded.gstin, mobile = excluded.mobile,
                        email = excluded.email, beg_fy = excluded.beg_fy, cur_fy = excluded.cur_fy,
                        last_synced_at = excluded.last_synced_at''',
                 (comp, s(r['BDEPID']), s(r['Name']), s(r['PrintName']), s(r['GSTNo']), s(r['BDEPMobile']),
                  s(r['BDEPEmailID']), dmy_time(r['BegFY']), dmy_time(r['CurFY']), datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    conn.commit()


def year_files(comp_dir):
    out = []
    for name in sorted(os.listdir(comp_dir)):
        m = YEAR_FILE.match(name)
        if m:
            out.append((int(m.group(1)), os.path.join(comp_dir, name)))
    return out


def sync_all(db_path=DEFAULT_DB, force=False, only_company=None, only_fy=None, log=print):
    """Sync every configured company and year that changed. Returns a list of per-year results."""
    if not _LOCK.acquire(blocking=False):
        log('sync already running')
        return [{'status': 'busy'}]
    results = []
    try:
        cfg = load_config()
        conn = connect(db_path)
        ensure_schema(conn)
        for comp in cfg.get('companies', []):
            if only_company and comp != only_company:
                continue
            comp_dir = os.path.join(cfg['bds_root'], comp)
            if not os.path.isdir(comp_dir):
                log(f'{comp}: folder not found {comp_dir}')
                results.append({'company': comp, 'status': 'missing folder'})
                continue
            try:
                sync_company_profile(conn, comp, comp_dir)
            except Exception as e:  # noqa: BLE001
                log(f'{comp}: company profile failed: {e}')
            for fy, path in year_files(comp_dir):
                if only_fy and fy != only_fy:
                    continue
                st = os.stat(path)
                prev = conn.execute('SELECT file_size, file_mtime, status FROM busy_sync_state WHERE company_code = ? AND fy_year = ?',
                                    (comp, fy)).fetchone()
                if not force and prev and prev[0] == st.st_size and abs((prev[1] or 0) - st.st_mtime) < 1 and prev[2] == 'ok':
                    results.append({'company': comp, 'fy': fy, 'status': 'unchanged'})
                    continue
                started = datetime.now()
                log(f'{comp} FY {fy}: reading {os.path.basename(path)} ({st.st_size // 1048576} MB)')
                status, detail = 'ok', ''
                try:
                    with tempfile.TemporaryDirectory(prefix='busy_sync_') as d:
                        errors = export(path, FY_QUERIES, d)
                        missing = REQUIRED & set(errors)
                        if missing:
                            raise RuntimeError(f'required tables failed: {", ".join(sorted(missing))}: {errors[sorted(missing)[0]]}')
                        counts = load_year(conn, comp, fy, d)
                    detail = json.dumps({'rows': counts, 'skipped': errors, 'seconds': round((datetime.now() - started).total_seconds(), 1)})
                    log(f'{comp} FY {fy}: ok {counts}' + (f' (skipped: {list(errors)})' if errors else ''))
                except Exception as e:  # noqa: BLE001
                    status, detail = 'error', str(e)[:1000]
                    log(f'{comp} FY {fy}: ERROR {e}')
                conn.execute('''INSERT OR REPLACE INTO busy_sync_state (company_code, fy_year, file_path, file_size, file_mtime,
                                synced_at, status, detail) VALUES (?,?,?,?,?,?,?,?)''',
                             (comp, fy, path, st.st_size, st.st_mtime, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), status, detail))
                conn.commit()
                results.append({'company': comp, 'fy': fy, 'status': status, 'detail': detail})
        conn.close()
    finally:
        _LOCK.release()
    return results


def copy_app_tables(src_db, dst_db):
    """Carry app-owned settings (print layout) into a freshly built DB."""
    src = sqlite3.connect(f'file:{src_db}?mode=ro', uri=True)
    rows = src.execute('SELECT * FROM busy_print_config').fetchall()
    src.close()
    dst = connect(dst_db)
    ensure_schema(dst)
    dst.executemany(f'INSERT OR REPLACE INTO busy_print_config VALUES ({",".join("?" * 11)})', rows)
    dst.commit()
    dst.close()


if __name__ == '__main__':
    args = sys.argv[1:]
    def opt(name):
        return args[args.index(name) + 1] if name in args else None
    db = opt('--db') or DEFAULT_DB
    if opt('--copy-app-tables-from'):
        copy_app_tables(opt('--copy-app-tables-from'), db)
    res = sync_all(db, force='--force' in args, only_company=opt('--company'),
                   only_fy=int(opt('--fy')) if opt('--fy') else None)
    bad = [r for r in res if r.get('status') == 'error']
    sys.exit(1 if bad else 0)
