import base64
import hashlib
import hmac
import http.server
import json
import os
import sys
import sqlite3
import urllib.parse
from datetime import datetime
import importlib
import auth
import voucher_renderers

# BUSY "Display" report modules (reports_*.py). Each exports ROUTES = {path: handler(conn, company, fy, params) -> dict}.
# A missing or broken module must never take the whole server down, so each is loaded on its own.
REPORT_ROUTES = {}
for _mod_name in ('reports_accounts', 'reports_inventory', 'reports_analysis', 'reports_gst', 'reports_batch', 'reports_registers'):
    try:
        REPORT_ROUTES.update(getattr(importlib.import_module(_mod_name), 'ROUTES', {}))
    except Exception as _e:  # noqa: BLE001
        print(f"[reports] {_mod_name} not loaded: {_e}")

# Transaction-list reports open on the last 7 days of data (user's choice); GST returns on the last month.
# Balance / full-year reports (group summary, P&L, month-wise, monthly summary, stock status) keep the whole FY.
WEEK_DEFAULT_REPORTS = {
    '/api/reports/accounts_cash_book', '/api/reports/accounts_bank_book', '/api/reports/accounts_daily_summary',
    '/api/reports/accounts_register', '/api/reports/registers_voucher_register', '/api/reports/registers_challan_register',
    '/api/reports/registers_party_item', '/api/reports/registers_stock_movement', '/api/reports/analysis_partywise',
    '/api/reports/analysis_itemwise', '/api/reports/inventory-stock-ledger',
}
MONTH_DEFAULT_REPORTS = {'/api/reports/gst-gstr1', '/api/reports/gst-gstr3b', '/api/reports/gst-hsn'}


# ---------- automatic multi-year BUSY sync (bds_sync.py) ----------
SYNC_STATE = {"running": False, "last_run": None, "last_result": None}


def start_background_sync(force=False):
    """Run bds_sync.sync_all() in a background thread; returns False if one is already running."""
    import threading
    if SYNC_STATE["running"]:
        return False

    def _run():
        SYNC_STATE["running"] = True
        try:
            import bds_sync
            res = bds_sync.sync_all(DB_PATH, force=force, log=lambda m: print(f"[sync] {m}"))
            SYNC_STATE["last_result"] = [r for r in res if r.get('status') != 'unchanged'] or "no changes"
        except Exception as e:  # noqa: BLE001
            SYNC_STATE["last_result"] = f"error: {e}"
            print(f"[sync] error: {e}")
        finally:
            SYNC_STATE["running"] = False
            SYNC_STATE["last_run"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    threading.Thread(target=_run, daemon=True).start()
    return True


def auto_sync_loop():
    """Sync on start-up, then every auto_sync_minutes (bds_sync_config.json; 0 disables)."""
    import threading
    import time
    try:
        import bds_sync
        minutes = int(bds_sync.load_config().get('auto_sync_minutes', 30))
    except Exception as e:  # noqa: BLE001
        print(f"[sync] auto-sync disabled: {e}")
        return
    if minutes <= 0:
        return

    def _loop():
        while True:
            start_background_sync()
            time.sleep(minutes * 60)
    threading.Thread(target=_loop, daemon=True).start()


def fy_label(fy):
    """2026 -> '2026-27' (BUSY year file db12026 = 1 Apr 2026 .. 31 Mar 2027)."""
    fy = int(fy)
    return f"{fy}-{str(fy + 1)[2:]}"


def available_fys(conn, company):
    rows = conn.execute("SELECT fy_year, COUNT(*), MIN(Date), MAX(Date) FROM busy_tran1 WHERE company_code = ? "
                        "GROUP BY fy_year ORDER BY fy_year DESC", (company,)).fetchall()
    return [{"fy": r[0], "label": fy_label(r[0]), "vouchers": r[1], "first_date": r[2], "last_date": r[3]} for r in rows]


def latest_fy(conn, company):
    row = conn.execute("SELECT MAX(fy_year) FROM busy_tran1 WHERE company_code = ?", (company,)).fetchone()
    return int(row[0]) if row and row[0] else datetime.now().year - (1 if datetime.now().month < 4 else 0)


def latest_data_date(conn, company, fy):
    row = conn.execute("SELECT MAX(Date) FROM busy_tran1 WHERE company_code = ? AND fy_year = ? AND Cancelled = 0 "
                       "AND VchType NOT IN (12, 13, 26, 27)", (company, fy)).fetchone()
    return row[0] if row and row[0] else f"{int(fy)}-04-01"


def report_default_week(conn, company, fy):
    """(from, to): the 7 days ending on the latest real transaction date of this company/FY.
    Anchored on the data rather than today, so the view is not empty when the last BUSY sync is a few days old.
    Orders and quotations (12, 13, 26, 27) are ignored: they are often dated ahead (delivery dates)."""
    from datetime import timedelta
    row = conn.execute("""SELECT MAX(Date) FROM busy_tran1
                          WHERE company_code = ? AND fy_year = ? AND Cancelled = 0
                            AND VchType NOT IN (12, 13, 26, 27) AND Date <= ?""",
                       (company, fy, datetime.now().strftime('%Y-%m-%d'))).fetchone()
    last = row[0] if row and row[0] else datetime.now().strftime('%Y-%m-%d')
    end = datetime.strptime(last[:10], '%Y-%m-%d')
    return (end - timedelta(days=6)).strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')

PORT = 3000
BASE_DIR = os.path.dirname(os.path.abspath(__file__))           # code + bundled files (web UI, exporter)
DATA_DIR = os.environ.get('KANAKKU_HOME') or BASE_DIR            # database, config, secret (Kanakku.exe: %LOCALAPPDATA%\Kanakku)
DB_PATH = os.path.join(DATA_DIR, "busy_local.db")
WEB_DIR = os.path.join(BASE_DIR, "BusyMobileWeb")

def _load_app_secret():
    """HMAC secret for signed PDF links / sessions. APP_SECRET env var (set it in Vercel), else a random
    secret generated once into the git-ignored .app_secret file. Never commit a secret into the code."""
    env = os.environ.get('APP_SECRET', '').strip()
    if env:
        return env.encode('utf-8')
    path = os.path.join(DATA_DIR, '.app_secret')
    try:
        with open(path, encoding='utf-8') as fh:
            val = fh.read().strip()
        if val:
            return val.encode('utf-8')
    except FileNotFoundError:
        pass
    import secrets
    val = secrets.token_hex(32)
    try:
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(val)
    except OSError:
        pass  # read-only filesystem (serverless): the secret lives for this process only; set APP_SECRET instead
    return val.encode('utf-8')


SECURITY_SALT = _load_app_secret()

def compute_hmac_sig(payload: str) -> str:
    return hmac.new(SECURITY_SALT, payload.encode('utf-8'), hashlib.sha256).hexdigest()[:16]

def generate_secure_token(entity_type: str, company: str, fy, identifier: str) -> str:
    raw = f"{entity_type}:{company}:{fy}:{identifier}"
    sig = compute_hmac_sig(raw)
    full = f"{raw}:{sig}"
    return base64.urlsafe_b64encode(full.encode('utf-8')).decode('utf-8').rstrip('=')

def verify_secure_token(token: str, expected_type: str = None):
    if not token:
        return None
    try:
        token_str = str(token).strip()
        pad = len(token_str) % 4
        if pad:
            token_str += '=' * (4 - pad)
        raw = base64.urlsafe_b64decode(token_str.encode('utf-8')).decode('utf-8')
        parts = raw.split(':')
        if len(parts) != 5:
            return None
        etype, comp, fy_str, ident, sig = parts
        expected_raw = f"{etype}:{comp}:{fy_str}:{ident}"
        expected_sig = compute_hmac_sig(expected_raw)
        if not hmac.compare_digest(sig, expected_sig):
            return None
        if expected_type and etype != expected_type:
            return None
        return {
            "type": etype,
            "company": comp,
            "fy": int(fy_str),
            "id": ident
        }
    except Exception:
        return None


def amount_to_words(num):
    try:
        num = round(float(num), 2)
    except Exception:
        return "Rupees Zero Only"
    if num == 0:
        return "Rupees Zero Only"
    
    ones = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
            "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
            "Seventeen", "Eighteen", "Nineteen"]
    tens = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]
    
    def two_digits(n):
        if n < 20:
            return ones[n]
        return tens[n // 10] + (" " + ones[n % 10] if n % 10 != 0 else "")
    
    def three_digits(n):
        if n < 100:
            return two_digits(n)
        h = ones[n // 100] + " Hundred"
        rem = n % 100
        if rem > 0:
            return h + " " + two_digits(rem)
        return h

    is_negative = num < 0
    num = abs(num)
    rupees = int(num)
    paise = int(round((num - rupees) * 100))
    
    parts = []
    cr = rupees // 10000000
    rupees %= 10000000
    if cr > 0:
        parts.append(two_digits(cr) + " Crore")
    
    lakh = rupees // 100000
    rupees %= 100000
    if lakh > 0:
        parts.append(two_digits(lakh) + " Lakh")
    
    th = rupees // 1000
    rupees %= 1000
    if th > 0:
        parts.append(two_digits(th) + " Thousand")
    
    if rupees > 0:
        parts.append(three_digits(rupees))
    
    res = " ".join(parts).strip() if parts else "Zero"
    res = "Rupees " + res
    if paise > 0:
        res += " and " + two_digits(paise) + " Paise"
    res += " Only"
    if is_negative:
        res = "Minus " + res
    return res

VCH_MAP = {
    1: 'Opening Balance', 2: 'Purchase', 3: 'Sales Return', 4: 'Purchase Challan', 5: 'Material Issue',
    6: 'Material Receipt', 7: 'Stock Journal', 8: 'Stock Transfer', 9: 'Sales', 10: 'Purchase Return',
    11: 'Sales Challan', 12: 'Sales Order', 13: 'Purchase Order', 14: 'Receipt', 15: 'Contra',
    16: 'Journal', 17: 'Debit Note', 18: 'Credit Note', 19: 'Payment', 20: 'Production',
    21: 'Unassemble', 22: 'Forms Issued', 23: 'Forms Received', 24: 'Sales', 25: 'PDC Voucher',
    26: 'Sales Quotation', 27: 'Purchase Quotation', 28: 'Salary', 29: 'Purchase Indent',
    30: 'Call Receipt', 31: 'Call Allocation', 32: 'Call Report'
}

def compute_item_stock(c, company, fy, mc_code=0):
    """Item-wise stock from BUSY data.
    Qty:   Tran4.D1 (opening, per material centre) + SUM(Tran2.Value1) of item lines (RecType 2, +in / -out).
           This matches BUSY's own DailySum stock ledger per item and material centre.
    Value: closing qty x average inward rate, where the rate is
           (opening value Tran4.D3 + inward value Tran2.Value3) / (opening qty + inward qty),
           taken across all material centres and excluding inter-godown transfers (VchType 5).
    Note: Tran2.Value2 is the alternate-unit qty, not a rate."""
    mc_sql = "AND MasterCode2 = ?" if mc_code > 0 else ""
    mc_args = [mc_code] if mc_code > 0 else []
    out = {}

    def row(code):
        return out.setdefault(code, {'OpQty': 0.0, 'OpValue': 0.0, 'MovementQty': 0.0,
                                     '_rq': 0.0, '_rv': 0.0})

    c.execute(f'''SELECT MasterCode1, SUM(D1), SUM(D3) FROM busy_tran4
                  WHERE company_code = ? AND fy_year = ? {mc_sql} GROUP BY MasterCode1''',
              [company, fy] + mc_args)
    for code, q, v in c.fetchall():
        r = row(code); r['OpQty'] = q or 0.0; r['OpValue'] = v or 0.0

    c.execute(f'''SELECT MasterCode1, SUM(Value1) FROM busy_tran2
                  WHERE company_code = ? AND fy_year = ? AND RecType = 2 {mc_sql} GROUP BY MasterCode1''',
              [company, fy] + mc_args)
    for code, q in c.fetchall():
        row(code)['MovementQty'] = q or 0.0

    # Average inward rate (all material centres)
    c.execute('''SELECT MasterCode1, SUM(D1), SUM(D3) FROM busy_tran4
                 WHERE company_code = ? AND fy_year = ? GROUP BY MasterCode1''', (company, fy))
    for code, q, v in c.fetchall():
        r = row(code); r['_rq'] += q or 0.0; r['_rv'] += v or 0.0
    c.execute('''SELECT t2.MasterCode1, SUM(t2.Value1), SUM(t2.Value3)
                 FROM busy_tran2 t2
                 JOIN busy_tran1 t1 ON t1.company_code = t2.company_code AND t1.fy_year = t2.fy_year AND t1.VchCode = t2.VchCode
                 WHERE t2.company_code = ? AND t2.fy_year = ? AND t2.RecType = 2 AND t2.Value1 > 0
                   AND t1.VchType <> 5 AND t1.Cancelled = 0
                 GROUP BY t2.MasterCode1''', (company, fy))
    for code, q, v in c.fetchall():
        r = row(code); r['_rq'] += q or 0.0; r['_rv'] += v or 0.0

    for r in out.values():
        rq, rv = r.pop('_rq'), r.pop('_rv')
        rate = rv / rq if rq > 0 else 0.0
        r['ClosingQty'] = round(r['OpQty'] + r['MovementQty'], 3)
        r['AvgRate'] = round(rate, 2)
        r['ClosingValue'] = round(r['ClosingQty'] * rate, 2)
    return out

_EXTRA_TABLES_READY = False

def ensure_extra_tables(conn):
    """Tables filled by bds_sync.py. Created empty if missing so queries never fail;
    when empty, item lines fall back to net amounts and sundries are treated as additive."""
    global _EXTRA_TABLES_READY
    if _EXTRA_TABLES_READY:
        return
    auth.ensure_schema(conn)
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS busy_item_ext (
            company_code TEXT NOT NULL, fy_year INTEGER NOT NULL,
            VchCode INTEGER NOT NULL, SrNo INTEGER NOT NULL, RecType INTEGER NOT NULL,
            Rate REAL DEFAULT 0, GrossAmt REAL DEFAULT 0, NetRate REAL DEFAULT 0,
            PRIMARY KEY (company_code, fy_year, VchCode, SrNo, RecType));
        CREATE TABLE IF NOT EXISTS busy_sundry_info (
            company_code TEXT NOT NULL, fy_year INTEGER NOT NULL, Code INTEGER NOT NULL,
            Additive INTEGER DEFAULT 1, PRIMARY KEY (company_code, fy_year, Code));
        CREATE INDEX IF NOT EXISTS idx_tran2_vch ON busy_tran2 (company_code, fy_year, VchCode);
        CREATE INDEX IF NOT EXISTS idx_tran3_vch ON busy_tran3 (company_code, fy_year, VchCode);
        CREATE INDEX IF NOT EXISTS idx_tran3_master ON busy_tran3 (company_code, fy_year, MasterCode1);
    ''')
    _EXTRA_TABLES_READY = True

# Item lines as BUSY prints them: rate per main unit (CM1) and gross amount before bill-sundry discounts.
# Gross amounts + signed bill sundries add up to Tran1.VchAmt for every voucher.
VOUCHER_ITEMS_SQL = '''
    SELECT t2.MasterCode1, m.Name as ItemName, COALESCE(u.Name, 'Pcs.') as UnitName,
           abs(t2.Value1) as Qty,
           CASE WHEN COALESCE(x.Rate, 0) <> 0 THEN abs(x.Rate)
                WHEN abs(t2.Value1) > 0 THEN abs(t2.Value3) / abs(t2.Value1) ELSE 0 END as Price,
           CASE WHEN COALESCE(x.GrossAmt, 0) <> 0 THEN abs(x.GrossAmt) ELSE abs(t2.Value3) END as Amount,
           abs(t2.Value3) as NetAmount,
           COALESCE(m.HSNCode, '') as HSNCode,
           t2.Value1 as RawQty
    FROM busy_tran2 t2
    LEFT JOIN busy_master1 m ON m.company_code = t2.company_code AND m.fy_year = t2.fy_year AND m.Code = t2.MasterCode1
    LEFT JOIN busy_master1 u ON u.company_code = m.company_code AND u.fy_year = m.fy_year AND u.Code = m.CM1
    LEFT JOIN busy_item_ext x ON x.company_code = t2.company_code AND x.fy_year = t2.fy_year
                             AND x.VchCode = t2.VchCode AND x.SrNo = t2.SrNo AND x.RecType = t2.RecType
    WHERE t2.company_code = ? AND t2.fy_year = ? AND t2.VchCode = ? AND t2.RecType IN (2, 4, 7)
    ORDER BY t2.SrNo
'''

# Bill sundries: Value1 = rate/percent, Value2 = base amount, Value3 = amount (always positive in BUSY);
# the sundry master's nature (busy_sundry_info.Additive) decides add or subtract.
VOUCHER_SUNDRIES_SQL = '''
    SELECT m.Name as SundryName, t2.Value1 as Rate, t2.Value2 as BaseVal,
           CASE WHEN COALESCE(si.Additive, 1) = 1 THEN abs(t2.Value3) ELSE -abs(t2.Value3) END as Amount,
           COALESCE(si.Additive, 1) as Additive
    FROM busy_tran2 t2
    LEFT JOIN busy_master1 m ON m.company_code = t2.company_code AND m.fy_year = t2.fy_year AND m.Code = t2.MasterCode1
    LEFT JOIN busy_sundry_info si ON si.company_code = t2.company_code AND si.fy_year = t2.fy_year AND si.Code = t2.MasterCode1
    WHERE t2.company_code = ? AND t2.fy_year = ? AND t2.VchCode = ? AND t2.RecType = 3
    ORDER BY t2.SrNo
'''

AGEING_SLABS = ['0-30', '31-60', '61-90', '91-180', '180+', 'On Account']

def compute_bill_ageing(c, company, fy, is_creditor=False, as_of=None):
    """Pending bill-by-bill amounts per party, bucketed by bill age (days since the bill date).
    Amounts are positive in the party's normal direction (Dr for debtors, Cr for creditors);
    advances and unadjusted new references land in 'On Account'."""
    as_of = as_of or datetime.now()
    c.execute('''
        SELECT MasterCode1, RefCode,
               COALESCE(MIN(CASE WHEN Method = 1 THEN NULLIF(BillDate, '') END), MIN(NULLIF(BillDate, ''))) as BillDate,
               SUM(Value1) as Pending
        FROM busy_tran3
        WHERE company_code = ? AND fy_year = ? AND RecType = 1
        GROUP BY MasterCode1, RefCode
        HAVING ABS(SUM(Value1)) > 0.5
    ''', (company, fy))
    out = {}
    for code, _ref, bdate, pending in c.fetchall():
        amt = pending if is_creditor else -pending
        if amt < 0:
            slab = 'On Account'
        else:
            try:
                days = (as_of - datetime.strptime((bdate or '')[:10], '%Y-%m-%d')).days
            except ValueError:
                days = None
            if days is None:
                slab = 'On Account'
            elif days <= 30:
                slab = '0-30'
            elif days <= 60:
                slab = '31-60'
            elif days <= 90:
                slab = '61-90'
            elif days <= 180:
                slab = '91-180'
            else:
                slab = '180+'
        p = out.setdefault(code, {})
        p[slab] = p.get(slab, 0.0) + amt
    return out

def group_tree_sql(alias='m'):
    """SQL condition: account <alias> sits under the given group or any of its sub-groups.
    Bind 3 params in order: root group code, company, fy."""
    return (f"{alias}.ParentGrp IN (WITH RECURSIVE gt(code) AS ("
            f"SELECT ? UNION SELECT x.Code FROM busy_master1 x JOIN gt ON x.ParentGrp = gt.code "
            f"WHERE x.company_code = ? AND x.fy_year = ? AND x.MasterType = 1) SELECT code FROM gt)")

def group_account_codes(c, company, fy, root):
    """Set of account codes (MasterType 2) under group `root`, including sub-groups."""
    c.execute(f"SELECT m.Code FROM busy_master1 m WHERE m.company_code = ? AND m.fy_year = ? AND m.MasterType = 2 AND {group_tree_sql()}",
              (company, fy, root, company, fy))
    return {r[0] for r in c.fetchall()}

EMPTY_STOCK = {'OpQty': 0.0, 'OpValue': 0.0, 'MovementQty': 0.0, 'ClosingQty': 0.0, 'AvgRate': 0.0, 'ClosingValue': 0.0}

def ledger_side(vch_type, amt):
    """Split a BUSY Tran2 accounting amount into (debit, credit, particulars).
    BUSY stores debits as negative Value1 and credits as positive Value1."""
    name = VCH_MAP.get(vch_type, 'Journal')
    if amt < 0:
        return abs(amt), 0.0, f"To {name}"
    return 0.0, abs(amt), f"By {name}"

class BusyApiHandler(http.server.SimpleHTTPRequestHandler):
    def send_security_block(self, message="பாதுகாப்பு கருதி நேரடியாக URL அளபுருக்களை (Parameters) மாற்றி இன்வாய்ஸ் அல்லது கணக்கு விபரங்களை அணுக முடியாது."):
        self.send_response(403)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        html = f"""<!DOCTYPE html>
<html lang="ta">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>403 Forbidden - Security Shield Active</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #0f172a;
            color: #f8fafc;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            margin: 0;
            padding: 20px;
            box-sizing: border-box;
        }}
        .card {{
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 16px;
            max-width: 540px;
            width: 100%;
            padding: 32px;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
            text-align: center;
        }}
        .icon {{ font-size: 56px; margin-bottom: 12px; }}
        .badge {{
            display: inline-block;
            padding: 4px 12px;
            background: rgba(239, 68, 68, 0.15);
            border: 1px solid rgba(239, 68, 68, 0.3);
            color: #f87171;
            border-radius: 9999px;
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 16px;
        }}
        h1 {{ font-size: 20px; font-weight: 700; color: #f87171; margin: 0 0 12px 0; }}
        p {{ font-size: 13px; line-height: 1.6; color: #cbd5e1; margin: 0 0 16px 0; }}
        .box {{
            background: #090d16;
            border: 1px dashed #475569;
            border-radius: 8px;
            padding: 12px;
            font-family: monospace;
            font-size: 11px;
            color: #fbbf24;
            margin-bottom: 20px;
            word-break: break-all;
            text-align: left;
        }}
        .btn {{
            display: inline-block;
            background: #2563eb;
            color: #fff;
            text-decoration: none;
            padding: 10px 24px;
            border-radius: 8px;
            font-weight: 600;
            font-size: 13px;
        }}
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">🛡️</div>
        <div class="badge">HMAC Security Shield Active</div>
        <h1>403 Forbidden: URL Manipulation Blocked</h1>
        <p>{message}</p>
        <div class="box">
            [SECURITY ALERT] Insecure Direct Object Reference (IDOR) Blocked.<br>
            Raw sequential parameter querying (?company=...&vchCode=...) is rejected.<br>
            Only cryptographically signed tokens (HMAC-SHA256) are permitted.
        </div>
        <p style="font-size: 11px; color: #94a3b8; margin-bottom: 24px;">Kanakku app அல்லது அதிகாரப்பூர்வ WhatsApp பகிர்வு மூலம் உருவாக்கப்படும் டோக்கன் இணைப்பு மட்டுமே செயல்படும்.</p>
        <a href="/" class="btn">&larr; Return to Mobile App</a>
    </div>
</body>
</html>"""
        self.wfile.write(html.encode('utf-8'))

    def end_headers(self):
        # Same-origin app: no CORS. Basic hardening headers.
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header('X-Frame-Options', 'SAMEORIGIN')
        for h in getattr(self, '_extra_headers', []):
            self.send_header(*h)
        self._extra_headers = []
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    # ---------- auth helpers ----------
    def read_body(self):
        if not hasattr(self, '_body_cache'):
            n = int(self.headers.get('Content-Length', 0) or 0)
            if n > 5_000_000:
                raise ValueError('Request too large')
            self._body_cache = self.rfile.read(n) if n > 0 else b''
        return self._body_cache

    def json_body(self):
        raw = self.read_body()
        try:
            data = json.loads(raw.decode('utf-8')) if raw else {}
        except (ValueError, UnicodeDecodeError):
            data = {}
        return data if isinstance(data, dict) else {}

    def is_https(self):
        return (self.headers.get('X-Forwarded-Proto') or '').lower() == 'https'

    def client_ip(self):
        fwd = self.headers.get('X-Forwarded-For') or ''
        return fwd.split(',')[0].strip() or self.client_address[0]

    def current_user(self):
        if not hasattr(self, '_user_cache'):
            token = ''
            for part in (self.headers.get('Cookie') or '').split(';'):
                k, _, v = part.strip().partition('=')
                if k == auth.COOKIE_NAME:
                    token = v
            conn = self.get_db()
            try:
                self._user_cache = auth.read_session(conn, SECURITY_SALT, token)
            finally:
                conn.close()
        return self._user_cache

    def allowed_companies(self, user):
        conn = self.get_db()
        try:
            all_codes = [r[0] for r in conn.execute('SELECT company_code FROM busy_company ORDER BY company_code')]
        finally:
            conn.close()
        return all_codes if user['role'] == 'admin' else [c for c in all_codes if c in (user.get('companies') or [])]

    def same_origin_ok(self):
        """Reject cross-site POSTs (in addition to the SameSite cookie)."""
        origin = self.headers.get('Origin')
        if not origin:
            return True
        return urllib.parse.urlparse(origin).netloc == (self.headers.get('Host') or '')

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if not self.same_origin_ok():
            self.send_json({"status": "error", "message": "Cross-site request blocked"}, code=403)
            return
        if parsed.path.startswith('/api/auth/') or parsed.path.startswith('/api/users/'):
            self.handle_auth_post(parsed.path)
            return
        user = self.current_user()
        if not user:
            self.send_json({"status": "error", "message": "Login required", "login_required": True}, code=401)
            return
        body_company = self.json_body().get('company') or ''
        if body_company and not auth.can_access_company(user, body_company):
            self.send_json({"status": "error", "message": "No access to this company"}, code=403)
            return
        if parsed.path == '/api/voucher/save':
            self.handle_save_voucher()
        elif parsed.path == '/api/party/save':
            self.handle_save_party()
        elif parsed.path == '/api/item/save':
            self.handle_save_item()
        else:
            self.send_json({"status": "error", "message": "Not found"}, code=404)

    def handle_auth_post(self, path):
        body = self.json_body()
        conn = self.get_db()
        try:
            if path == '/api/auth/setup':
                # first admin only, while no user exists
                if auth.user_count(conn) > 0:
                    raise auth.AuthError('Setup is already done. Please log in.', 403)
                u = auth.create_user(conn, (body.get('username') or '').strip(), body.get('password') or '',
                                     role='admin', display_name=(body.get('display_name') or '').strip())
                self._login_response(u)
                return
            if path == '/api/auth/login':
                username = (body.get('username') or '').strip()
                key = f'{self.client_ip()}|{username.lower()}'
                auth.throttle_check(key)
                try:
                    u = auth.authenticate(conn, username, body.get('password') or '')
                except auth.AuthError:
                    auth.throttle_fail(key)
                    raise
                auth.throttle_reset(key)
                self._login_response(u)
                return
            if path == '/api/auth/logout':
                self._extra_headers = [('Set-Cookie', auth.clear_cookie_header(self.is_https()))]
                self.send_json({"status": "success"})
                return
            user = self.current_user()
            if not user:
                raise auth.AuthError('Login required', 401)
            if path == '/api/auth/password':
                if not auth.verify_password(body.get('current_password') or '', user['pass_hash']):
                    raise auth.AuthError('Current password is wrong.', 403)
                u = auth.update_user(conn, user['id'], password=body.get('new_password') or '')
                self._login_response(u)  # other devices are logged out; this one gets a fresh session
                return
            if path == '/api/auth/logout-all':
                auth.revoke_sessions(conn, user['id'])
                self._extra_headers = [('Set-Cookie', auth.clear_cookie_header(self.is_https()))]
                self.send_json({"status": "success"})
                return
            # ---- admin only ----
            if user['role'] != 'admin':
                raise auth.AuthError('Admin only.', 403)
            if path == '/api/users/save':
                companies = body.get('companies')
                if companies is not None and not (isinstance(companies, list) and all(isinstance(c, str) for c in companies)):
                    raise auth.AuthError('companies must be a list of company codes.')
                if body.get('id'):
                    u = auth.update_user(conn, int(body['id']), role=body.get('role'), companies=companies,
                                         active=body.get('active'), display_name=body.get('display_name'),
                                         password=body.get('password') or None)
                else:
                    u = auth.create_user(conn, (body.get('username') or '').strip(), body.get('password') or '',
                                         role=body.get('role') or 'user', companies=companies or [],
                                         display_name=(body.get('display_name') or '').strip())
                self.send_json({"status": "success", "user": auth.public_user(u)})
                return
            if path == '/api/users/delete':
                if int(body.get('id') or 0) == user['id']:
                    raise auth.AuthError('You cannot delete your own account.')
                auth.delete_user(conn, int(body.get('id') or 0))
                self.send_json({"status": "success"})
                return
            raise auth.AuthError('Not found', 404)
        except auth.AuthError as e:
            self.send_json({"status": "error", "message": str(e)}, code=e.status)
        finally:
            conn.close()

    def _login_response(self, u):
        token = auth.make_session(SECURITY_SALT, u)
        self._extra_headers = [('Set-Cookie', auth.cookie_header(token, self.is_https()))]
        self.send_json({"status": "success", "user": auth.public_user(u)})

    def handle_save_party(self):
        try:
            raw_body = self.read_body()
            body = json.loads(raw_body.decode('utf-8'))

            company = body.get('company') or self.default_company()
            fy = self.resolve_fy(company, body.get('fy'))
            name = body.get('name', '').strip()
            if not name:
                self.send_json({"status": "error", "message": "Party name is required."})
                return

            group_code = int(body.get('groupCode', 116)) # 116 = Sundry Debtors, 117 = Sundry Creditors
            mobile = body.get('mobile', '').strip()
            gstin = body.get('gstin', '').strip()
            station = body.get('station', '').strip()
            address1 = body.get('address1', '').strip()

            conn = self.get_db()
            c = conn.cursor()
            c.execute("SELECT COALESCE(MAX(Code), 0) + 1 FROM busy_master1 WHERE company_code = ? AND fy_year = ?", (company, fy))
            new_code = c.fetchone()[0]

            c.execute('''
                INSERT INTO busy_master1 (company_code, fy_year, Code, MasterType, Name, Alias, PrintName, ParentGrp, OpBal, Stamp)
                VALUES (?, ?, ?, 2, ?, ?, ?, ?, 0.0, 1)
            ''', (company, fy, new_code, name, station, name, group_code))

            c.execute('''
                INSERT OR REPLACE INTO busy_masteraddressinfo (company_code, fy_year, MasterCode, GSTNo, Address1, Address2, Mobile, Email, StateCode, PinCode)
                VALUES (?, ?, ?, ?, ?, ?, ?, '', '', '')
            ''', (company, fy, new_code, gstin, address1, station, mobile))

            conn.commit()
            conn.close()

            self.send_json({"status": "success", "message": f"Party '{name}' created successfully!", "code": new_code})
        except Exception as e:
            self.send_json({"status": "error", "message": str(e)})

    def handle_save_item(self):
        try:
            raw_body = self.read_body()
            body = json.loads(raw_body.decode('utf-8'))

            company = body.get('company') or self.default_company()
            fy = self.resolve_fy(company, body.get('fy'))
            name = body.get('name', '').strip()
            if not name:
                self.send_json({"status": "error", "message": "Item name is required."})
                return

            hsn = body.get('hsn', '').strip()
            unit_code = int(body.get('unitCode', 1147)) # 1147 = Pcs., 1150 = Metre

            conn = self.get_db()
            c = conn.cursor()
            c.execute("SELECT COALESCE(MAX(Code), 0) + 1 FROM busy_master1 WHERE company_code = ? AND fy_year = ?", (company, fy))
            new_code = c.fetchone()[0]

            c.execute('''
                INSERT INTO busy_master1 (company_code, fy_year, Code, MasterType, Name, Alias, PrintName, ParentGrp, OpBal, Stamp, CM1, CM2, HSNCode)
                VALUES (?, ?, ?, 6, ?, '', ?, 1, 0.0, 1, 1147, ?, ?)
            ''', (company, fy, new_code, name, name, unit_code, hsn))

            conn.commit()
            conn.close()

            self.send_json({"status": "success", "message": f"Item '{name}' created successfully!", "code": new_code})
        except Exception as e:
            self.send_json({"status": "error", "message": str(e)})

    def handle_save_voucher(self):
        try:
            raw_body = self.read_body()
            body = json.loads(raw_body.decode('utf-8'))
            
            company = body.get('company') or self.default_company()
            fy = self.resolve_fy(company, body.get('fy'))
            vch_type = int(body.get('vchType', 9))
            series_code = int(body.get('seriesCode', 1))
            vch_no = body.get('vchNo', '').strip()
            date_str = body.get('date') or datetime.now().strftime('%Y-%m-%d')
            party_code = int(body.get('partyCode', 0))
            mc_code = int(body.get('mcCode', 201))
            vch_amt = float(body.get('vchAmt', 0.0))
            items = body.get('items', [])
            
            conn = self.get_db()
            c = conn.cursor()
            c.execute("SELECT COALESCE(MAX(VchCode), 0) + 1 FROM busy_tran1 WHERE company_code = ? AND fy_year = ?", (company, fy))
            new_vch_code = c.fetchone()[0]
            
            if not vch_no:
                prefix = "INV" if vch_type == 9 else ("PUR" if vch_type in (2, 10) else ("SRT" if vch_type == 3 else ("ORD" if vch_type == 12 else ("QTN" if vch_type in (26, 27) else ("RCP" if vch_type == 14 else "PMT")))))
                c.execute("SELECT COUNT(*) FROM busy_tran1 WHERE company_code = ? AND fy_year = ? AND VchType = ?", (company, fy, vch_type))
                count = c.fetchone()[0] + 1
                vch_no = f"{prefix}-{count:04d}"
                
            c.execute('''
                INSERT INTO busy_tran1 (company_code, fy_year, VchCode, VchType, VchSeriesCode, VchNo, Date, MasterCode1, MasterCode2, Cancelled, VchAmt, Stamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, datetime('now'))
            ''', (company, fy, new_vch_code, vch_type, series_code, vch_no, date_str, party_code, mc_code, vch_amt))
            
            sr_no = 1
            # 1. Item Entries (RecType = 2 for inventory, 4 for orders/quotations)
            item_rectype = 4 if vch_type in (12, 13, 26, 27) else 2
            for it in items:
                it_code = int(it.get('itemCode', 0))
                qty = float(it.get('qty', 1.0))
                price = float(it.get('price', 0.0))
                amt = float(it.get('amount', qty * price))
                c.execute('''
                    INSERT INTO busy_tran2 (company_code, fy_year, VchCode, SrNo, RecType, MasterCode1, MasterCode2, Value1, Value2, Value3)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (company, fy, new_vch_code, sr_no, item_rectype, it_code, mc_code, qty, price, amt))
                sr_no += 1
                
            # 2. Accounting Ledger Entries (RecType = 1)
            # Party and Sales/Purchase/Cash accounts
            if vch_type == 9: # Sales: Party Dr (-amt), Sales Cr (+amt)
                c.execute('''
                    INSERT INTO busy_tran2 (company_code, fy_year, VchCode, SrNo, RecType, MasterCode1, MasterCode2, Value1, Value2, Value3)
                    VALUES (?, ?, ?, ?, 1, ?, 0, ?, 0.0, 0.0)
                ''', (company, fy, new_vch_code, sr_no, party_code, -vch_amt))
                sr_no += 1
                c.execute('''
                    INSERT INTO busy_tran2 (company_code, fy_year, VchCode, SrNo, RecType, MasterCode1, MasterCode2, Value1, Value2, Value3)
                    VALUES (?, ?, ?, ?, 1, 4, 0, ?, 0.0, 0.0)
                ''', (company, fy, new_vch_code, sr_no, vch_amt))
            elif vch_type in (2, 10): # Purchase: Party Cr (+amt), Purchase Dr (-amt)
                c.execute('''
                    INSERT INTO busy_tran2 (company_code, fy_year, VchCode, SrNo, RecType, MasterCode1, MasterCode2, Value1, Value2, Value3)
                    VALUES (?, ?, ?, ?, 1, ?, 0, ?, 0.0, 0.0)
                ''', (company, fy, new_vch_code, sr_no, party_code, vch_amt))
                sr_no += 1
                c.execute('''
                    INSERT INTO busy_tran2 (company_code, fy_year, VchCode, SrNo, RecType, MasterCode1, MasterCode2, Value1, Value2, Value3)
                    VALUES (?, ?, ?, ?, 1, 5, 0, ?, 0.0, 0.0)
                ''', (company, fy, new_vch_code, sr_no, -vch_amt))
            elif vch_type == 3: # Sales Return: Party Cr (+amt), Sales Return Dr (-amt)
                c.execute('''
                    INSERT INTO busy_tran2 (company_code, fy_year, VchCode, SrNo, RecType, MasterCode1, MasterCode2, Value1, Value2, Value3)
                    VALUES (?, ?, ?, ?, 1, ?, 0, ?, 0.0, 0.0)
                ''', (company, fy, new_vch_code, sr_no, party_code, vch_amt))
                sr_no += 1
                c.execute('''
                    INSERT INTO busy_tran2 (company_code, fy_year, VchCode, SrNo, RecType, MasterCode1, MasterCode2, Value1, Value2, Value3)
                    VALUES (?, ?, ?, ?, 1, 4, 0, ?, 0.0, 0.0)
                ''', (company, fy, new_vch_code, sr_no, -vch_amt))
            elif vch_type == 14: # Receipt: Cash/Bank Dr (-amt), Party Cr (+amt)
                c.execute('''
                    INSERT INTO busy_tran2 (company_code, fy_year, VchCode, SrNo, RecType, MasterCode1, MasterCode2, Value1, Value2, Value3)
                    VALUES (?, ?, ?, ?, 1, ?, 0, ?, 0.0, 0.0)
                ''', (company, fy, new_vch_code, sr_no, party_code, vch_amt))
                sr_no += 1
                c.execute('''
                    INSERT INTO busy_tran2 (company_code, fy_year, VchCode, SrNo, RecType, MasterCode1, MasterCode2, Value1, Value2, Value3)
                    VALUES (?, ?, ?, ?, 1, 1, 0, ?, 0.0, 0.0)
                ''', (company, fy, new_vch_code, sr_no, -vch_amt))
            elif vch_type in (1, 19): # Payment: Party Dr (-amt), Cash/Bank Cr (+amt)
                c.execute('''
                    INSERT INTO busy_tran2 (company_code, fy_year, VchCode, SrNo, RecType, MasterCode1, MasterCode2, Value1, Value2, Value3)
                    VALUES (?, ?, ?, ?, 1, ?, 0, ?, 0.0, 0.0)
                ''', (company, fy, new_vch_code, sr_no, party_code, -vch_amt))
                sr_no += 1
                c.execute('''
                    INSERT INTO busy_tran2 (company_code, fy_year, VchCode, SrNo, RecType, MasterCode1, MasterCode2, Value1, Value2, Value3)
                    VALUES (?, ?, ?, ?, 1, 1, 0, ?, 0.0, 0.0)
                ''', (company, fy, new_vch_code, sr_no, vch_amt))
                
            conn.commit()
            conn.close()
            
            self.send_json({
                "status": "success",
                "message": f"Voucher {vch_no} saved successfully!",
                "vchCode": new_vch_code,
                "vchNo": vch_no
            })
        except Exception as e:
            self.send_json({
                "status": "error",
                "message": str(e)
            }, code=500)

    # Signed-token PDF links (shared on WhatsApp) open without a login: the HMAC token itself is the permission.
    TOKEN_PUBLIC_PATHS = ('/api/voucher/invoice-pdf', '/api/share/')

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path == '/api/auth/status':
            _c = self.get_db()
            try:
                setup_required = auth.user_count(_c) == 0
            finally:
                _c.close()
            u = None if setup_required else self.current_user()
            self.send_json({"setup_required": setup_required, "user": auth.public_user(u),
                            "companies": self.allowed_companies(u) if u else []})
            return

        user = None
        if parsed.path.startswith('/api/') and not parsed.path.startswith(self.TOKEN_PUBLIC_PATHS):
            user = self.current_user()
            if not user:
                self.send_json({"status": "error", "message": "Login required", "login_required": True}, code=401)
                return
            req_company = params.get('company', [''])[0]
            if req_company and not auth.can_access_company(user, req_company):
                self.send_json({"status": "error", "message": "No access to this company"}, code=403)
                return
            if parsed.path == '/api/users':
                if user['role'] != 'admin':
                    self.send_json({"status": "error", "message": "Admin only."}, code=403)
                    return
                _c = self.get_db()
                try:
                    self.send_json({"users": auth.list_users(_c), "companies": self.allowed_companies(user)})
                finally:
                    _c.close()
                return
            if parsed.path in ('/api/sync/run',) and user['role'] != 'admin':
                self.send_json({"status": "error", "message": "Admin only."}, code=403)
                return

        if user:
            allowed = self.allowed_companies(user)
            company = params.get('company', [''])[0] or (allowed[0] if allowed else '')
        else:
            company = params.get('company', [''])[0] or self.default_company()
        try:
            fy = int(params.get('fy', [''])[0])
        except ValueError:
            fy = 0
        if not fy:
            # no FY chosen: use this company's latest synced year
            _c = self.get_db()
            try:
                fy = latest_fy(_c, company)
            finally:
                _c.close()

        if parsed.path == '/api/companies':
            self.handle_companies(self.allowed_companies(user) if user else [])
        elif parsed.path == '/api/fys':
            # financial years synced for this company, newest first
            _c = self.get_db()
            try:
                self.send_json(available_fys(_c, company))
            finally:
                _c.close()
        elif parsed.path == '/api/sync/status':
            _c = self.get_db()
            try:
                rows = [dict(r) for r in _c.execute(
                    "SELECT company_code, fy_year, synced_at, status, detail FROM busy_sync_state ORDER BY company_code, fy_year DESC")]
            except sqlite3.OperationalError:
                rows = []
            finally:
                _c.close()
            self.send_json({"running": SYNC_STATE["running"], "last_run": SYNC_STATE["last_run"],
                            "last_result": SYNC_STATE["last_result"], "years": rows})
        elif parsed.path == '/api/sync/run':
            started = start_background_sync(force=params.get('force', ['0'])[0] == '1')
            self.send_json({"started": started, "running": SYNC_STATE["running"]})
        elif parsed.path == '/api/dashboard':
            from_date = params.get('fromDate', [''])[0]
            to_date = params.get('toDate', [''])[0]
            self.handle_dashboard(company, fy, from_date, to_date)
        elif parsed.path == '/api/ledger/parties':
            search = params.get('search', [''])[0]
            group = params.get('group', ['all'])[0]
            route = int(params.get('route', [0])[0])
            self.handle_parties(company, fy, search, group, route)
        elif parsed.path == '/api/ledger/statement':
            code = int(params.get('code', [0])[0])
            self.handle_statement(company, fy, code)
        elif parsed.path == '/api/outstanding':
            out_type = params.get('type', ['receivable'])[0]
            route = int(params.get('route', [0])[0])
            self.handle_outstanding(company, fy, out_type, route)
        elif parsed.path == '/api/outstanding/bills':
            party_code = int(params.get('partyCode', [0])[0])
            self.handle_party_bills(company, fy, party_code)
        elif parsed.path == '/api/daybook':
            date_str = params.get('date', [''])[0]
            self.handle_daybook(company, fy, date_str)
        elif parsed.path == '/api/stock':
            search = params.get('search', [''])[0]
            mc_code = int(params.get('mcCode', [0])[0])
            self.handle_stock(company, fy, search, mc_code)
        elif parsed.path == '/api/trialbalance':
            self.handle_trialbalance(company, fy)
        elif parsed.path == '/api/vouchers':
            vch_type = params.get('type', ['all'])[0]
            search = params.get('search', [''])[0]
            series_code = int(params.get('seriesCode', [0])[0])
            from_date = params.get('fromDate', [''])[0]
            to_date = params.get('toDate', [''])[0]
            self.handle_vouchers_list(company, fy, vch_type, search, series_code, from_date, to_date)
        elif parsed.path == '/api/voucher/details':
            vch_code = int(params.get('vchCode', [0])[0])
            self.handle_voucher_details(company, fy, vch_code)
        elif parsed.path == '/api/materialcentres':
            self.handle_materialcentres(company, fy)
        elif parsed.path == '/api/items':
            search = params.get('search', [''])[0]
            self.handle_items_list(company, fy, search)
        elif parsed.path == '/api/series':
            self.handle_series(company, fy)
        elif parsed.path == '/api/masterseries':
            self.handle_masterseries(company, fy)
        elif parsed.path == '/api/bcn/stock':
            search = params.get('search', [''])[0]
            self.handle_bcn_stock(company, fy, search)
        elif parsed.path == '/api/parameter/stock':
            search = params.get('search', [''])[0]
            mc_code = int(params.get('mcCode', [0])[0])
            self.handle_parameter_stock(company, fy, search, mc_code)
        elif parsed.path == '/api/batch/stock':
            search = params.get('search', [''])[0]
            mc_code = int(params.get('mcCode', [0])[0])
            self.handle_batch_stock(company, fy, search, mc_code)
        elif parsed.path == '/api/security/token':
            req_type = params.get('type', ['inv'])[0]
            req_id = params.get('id', [''])[0]
            token = generate_secure_token(req_type, company, fy, req_id)
            pdf_path_map = {
                'inv': '/api/voucher/invoice-pdf',
                'led': '/api/share/ledger-pdf',
                'out': '/api/share/outstanding-pdf',
                'day': '/api/share/daybook-pdf',
                'stk': '/api/share/stock-pdf',
                'bstk': '/api/share/batch-stock-pdf',
                'tb': '/api/share/trialbalance-pdf'
            }
            pdf_path = pdf_path_map.get(req_type, '/api/voucher/invoice-pdf')
            self.send_json({
                "token": token,
                "secure_url": f"{pdf_path}?token={token}"
            })
        elif parsed.path == '/api/voucher/invoice-pdf':
            token = params.get('token', [''])[0]
            vch_code_param = params.get('vchCode', [None])[0]
            sig_param = params.get('sig', [None])[0]
            if token:
                data = verify_secure_token(token, expected_type='inv')
                if not data:
                    self.send_security_block("தவறான அல்லது மாற்றப்பட்ட பாதுகாப்பு டோக்கன் (Invalid Token).")
                    return
                self.handle_invoice_pdf(data['company'], data['fy'], int(data['id']))
            elif vch_code_param and sig_param:
                expected_sig = compute_hmac_sig(f"inv:{company}:{fy}:{vch_code_param}")
                if not hmac.compare_digest(sig_param, expected_sig):
                    self.send_security_block("அங்கீகரிக்கப்படாத URL மாற்றம் கண்டறியப்பட்டது (Signature Mismatch).")
                    return
                self.handle_invoice_pdf(company, fy, int(vch_code_param))
            else:
                self.send_security_block("நேரடியாக URL எண்களை மாற்றி (Parameter Tampering) இன்வாய்ஸை அணுக அனுமதி இல்லை. IDOR தடுப்பு கவசம் செயல்படுத்தப்பட்டுள்ளது.")
                return
        elif parsed.path == '/api/share/ledger-pdf':
            token = params.get('token', [''])[0]
            code_param = params.get('code', [None])[0]
            sig_param = params.get('sig', [None])[0]
            if token:
                data = verify_secure_token(token, expected_type='led')
                if not data:
                    self.send_security_block("தவறான அல்லது மாற்றப்பட்ட லெட்ஜர் பாதுகாப்பு டோக்கன்.")
                    return
                self.handle_ledger_pdf(data['company'], data['fy'], int(data['id']))
            elif code_param and sig_param:
                expected_sig = compute_hmac_sig(f"led:{company}:{fy}:{code_param}")
                if not hmac.compare_digest(sig_param, expected_sig):
                    self.send_security_block("அங்கீகரிக்கப்படாத URL மாற்றம் கண்டறியப்பட்டது.")
                    return
                self.handle_ledger_pdf(company, fy, int(code_param))
            else:
                self.send_security_block("நேரடியாக URL எண்களை மாற்றி லெட்ஜர் கணக்கு விபரங்களை அணுக அனுமதி இல்லை.")
                return
        elif parsed.path == '/api/share/outstanding-pdf':
            token = params.get('token', [''])[0]
            out_type = params.get('type', ['receivable'])[0]
            route = int(params.get('route', [0])[0])
            sig_param = params.get('sig', [None])[0]
            if token:
                data = verify_secure_token(token, expected_type='out')
                if not data:
                    self.send_security_block("தவறான நிலுவைத் தொகை பாதுகாப்பு டோக்கன்.")
                    return
                parts = str(data['id']).split('__')
                o_type = parts[0] if parts[0] else 'receivable'
                o_route = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                o_bills = len(parts) > 2 and parts[2] == 'bills'  # bill-wise mode is part of the signed id
                self.handle_outstanding_pdf(data['company'], data['fy'], o_type, o_route, billwise=o_bills)
            elif sig_param:
                expected_sig = compute_hmac_sig(f"out:{company}:{fy}:{out_type}__{route}")
                if not hmac.compare_digest(sig_param, expected_sig):
                    self.send_security_block("அங்கீகரிக்கப்படாத URL மாற்றம் கண்டறியப்பட்டது.")
                    return
                self.handle_outstanding_pdf(company, fy, out_type, route)
            else:
                self.send_security_block("நேரடியாக URL-ஐ மாற்றி நிலுவைத் தொகை அறிக்கையை அணுக அனுமதி இல்லை.")
                return
        elif parsed.path == '/api/share/batch-stock-pdf':
            token = params.get('token', [''])[0]
            search = params.get('search', [''])[0]
            mc_code = int(params.get('mcCode', [0])[0])
            sig_param = params.get('sig', [None])[0]
            if token:
                data = verify_secure_token(token, expected_type='bstk')
                if not data:
                    self.send_security_block("தவறான Batch Stock பாதுகாப்பு டோக்கன்.")
                    return
                parts = str(data['id']).split('__')
                s_search = parts[0]
                s_mc = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                self.handle_batch_stock_pdf(data['company'], data['fy'], s_search, s_mc)
            elif sig_param:
                expected_sig = compute_hmac_sig(f"bstk:{company}:{fy}:{search}__{mc_code}")
                if not hmac.compare_digest(sig_param, expected_sig):
                    self.send_security_block("அங்கீகரிக்கப்படாத URL மாற்றம் கண்டறியப்பட்டது.")
                    return
                self.handle_batch_stock_pdf(company, fy, search, mc_code)
            else:
                self.send_security_block("நேரடியாக URL-ஐ மாற்றி Batch Stock அறிக்கையை அணுக அனுமதி இல்லை.")
                return
        elif parsed.path == '/api/share/daybook-pdf':
            token = params.get('token', [''])[0]
            date_str = params.get('date', [''])[0]
            sig_param = params.get('sig', [None])[0]
            if token:
                data = verify_secure_token(token, expected_type='day')
                if not data:
                    self.send_security_block("தவறான Daybook பாதுகாப்பு டோக்கன்.")
                    return
                self.handle_daybook_pdf(data['company'], data['fy'], str(data['id']))
            elif sig_param:
                expected_sig = compute_hmac_sig(f"day:{company}:{fy}:{date_str}")
                if not hmac.compare_digest(sig_param, expected_sig):
                    self.send_security_block("அங்கீகரிக்கப்படாத URL மாற்றம் கண்டறியப்பட்டது.")
                    return
                self.handle_daybook_pdf(company, fy, date_str)
            else:
                self.send_security_block("நேரடியாக URL-ஐ மாற்றி Daybook அறிக்கையை அணுக அனுமதி இல்லை.")
                return
        elif parsed.path == '/api/share/stock-pdf':
            token = params.get('token', [''])[0]
            search = params.get('search', [''])[0]
            mc_code = int(params.get('mcCode', [0])[0])
            sig_param = params.get('sig', [None])[0]
            if token:
                data = verify_secure_token(token, expected_type='stk')
                if not data:
                    self.send_security_block("தவறான Stock பாதுகாப்பு டோக்கன்.")
                    return
                parts = str(data['id']).split('__')
                s_search = parts[0]
                s_mc = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                self.handle_stock_pdf(data['company'], data['fy'], s_search, s_mc)
            elif sig_param:
                expected_sig = compute_hmac_sig(f"stk:{company}:{fy}:{search}__{mc_code}")
                if not hmac.compare_digest(sig_param, expected_sig):
                    self.send_security_block("அங்கீகரிக்கப்படாத URL மாற்றம் கண்டறியப்பட்டது.")
                    return
                self.handle_stock_pdf(company, fy, search, mc_code)
            else:
                self.send_security_block("நேரடியாக URL-ஐ மாற்றி இருப்பு அறிக்கையை (Stock) அணுக அனுமதி இல்லை.")
                return
        elif parsed.path == '/api/share/trialbalance-pdf':
            token = params.get('token', [''])[0]
            sig_param = params.get('sig', [None])[0]
            if token:
                data = verify_secure_token(token, expected_type='tb')
                if not data:
                    self.send_security_block("தவறான Trial Balance பாதுகாப்பு டோக்கன்.")
                    return
                self.handle_trialbalance_pdf(data['company'], data['fy'])
            elif sig_param:
                expected_sig = compute_hmac_sig(f"tb:{company}:{fy}:all")
                if not hmac.compare_digest(sig_param, expected_sig):
                    self.send_security_block("அங்கீகரிக்கப்படாத URL மாற்றம் கண்டறியப்பட்டது.")
                    return
                self.handle_trialbalance_pdf(company, fy)
            else:
                self.send_security_block("நேரடியாக URL-ஐ மாற்றி Trial Balance அறிக்கையை அணுக அனுமதி இல்லை.")
                return
        elif parsed.path == '/api/reports/default-range':
            conn = self.get_db()
            try:
                f, t = report_default_week(conn, company, fy)
                self.send_json({"from": f, "to": t, "month": t[:7]})
            finally:
                conn.close()
        elif parsed.path in REPORT_ROUTES:
            # BUSY "Display" reports from the reports_*.py modules
            conn = self.get_db()
            try:
                no_period = not any(k in params for k in ('from', 'to', 'month'))
                if no_period and parsed.path in WEEK_DEFAULT_REPORTS:
                    # transaction lists open on the last 7 days of data instead of the whole year
                    f, t = report_default_week(conn, company, fy)
                    params = dict(params, **{'from': [f], 'to': [t]})
                elif no_period and parsed.path in MONTH_DEFAULT_REPORTS:
                    _f, t = report_default_week(conn, company, fy)
                    params = dict(params, month=[t[:7]])
                self.send_json(REPORT_ROUTES[parsed.path](conn, company, fy, params))
            except Exception as e:
                self.send_json({"status": "error", "message": str(e)})
            finally:
                conn.close()
        else:
            # Serve index.html or static files
            file_path = os.path.join(WEB_DIR, parsed.path.lstrip('/'))
            if parsed.path == '/' or not os.path.exists(file_path):
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                with open(os.path.join(WEB_DIR, 'index.html'), 'rb') as f:
                    self.wfile.write(f.read())
            else:
                super().do_GET()

    def resolve_fy(self, company, fy):
        """Requested FY, or the company's latest synced FY when none / invalid was given."""
        try:
            fy = int(fy or 0)
        except (TypeError, ValueError):
            fy = 0
        if fy:
            return fy
        conn = sqlite3.connect(DB_PATH)
        try:
            return latest_fy(conn, company)
        finally:
            conn.close()

    def default_company(self):
        conn = sqlite3.connect(DB_PATH)
        try:
            row = conn.execute("SELECT company_code FROM busy_company ORDER BY company_code LIMIT 1").fetchone()
        finally:
            conn.close()
        return row[0] if row else ''

    def get_db(self):
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        ensure_extra_tables(conn)
        return conn


    def send_html(self, html_content):
        encoded = html_content.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_json(self, data, code=200):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False, default=str).encode('utf-8'))

    def handle_companies(self, allowed):
        conn = self.get_db()
        c = conn.cursor()
        c.execute('SELECT company_code, name, print_name, gstin, bdep_id, mobile, email, last_synced_at FROM busy_company ORDER BY company_code')
        rows = [dict(r) for r in c.fetchall() if r['company_code'] in allowed]
        conn.close()
        self.send_json(rows)

    def handle_dashboard(self, company, fy, from_date="", to_date=""):
        conn = self.get_db()
        c = conn.cursor()

        date_sql = ""
        date_args = []
        if from_date and to_date:
            date_sql = "AND Date BETWEEN ? AND ?"
            date_args = [from_date, to_date]
        elif from_date:
            date_sql = "AND Date >= ?"
            date_args = [from_date]
        elif to_date:
            date_sql = "AND Date <= ?"
            date_args = [to_date]

        # 1. Total Sales (VchType IN (9, 24))
        c.execute(f'''
            SELECT COUNT(*), COALESCE(SUM(VchAmt), 0)
            FROM busy_tran1
            WHERE company_code = ? AND fy_year = ? AND VchType IN (9, 24) AND Cancelled = 0
              {date_sql}
        ''', [company, fy] + date_args)
        sales_row = c.fetchone()

        # 2. Total Purchases (VchType = 2)
        c.execute(f'''
            SELECT COUNT(*), COALESCE(SUM(VchAmt), 0)
            FROM busy_tran1
            WHERE company_code = ? AND fy_year = ? AND VchType = 2 AND Cancelled = 0
              {date_sql}
        ''', [company, fy] + date_args)
        purc_row = c.fetchone()

        # 3. Total Receipts (VchType = 14)
        c.execute(f'''
            SELECT COUNT(*), COALESCE(SUM(VchAmt), 0)
            FROM busy_tran1
            WHERE company_code = ? AND fy_year = ? AND VchType = 14 AND Cancelled = 0
              {date_sql}
        ''', [company, fy] + date_args)
        rcpt_row = c.fetchone()

        # 4. Total Payments (VchType = 19)
        c.execute(f'''
            SELECT COUNT(*), COALESCE(SUM(VchAmt), 0)
            FROM busy_tran1
            WHERE company_code = ? AND fy_year = ? AND VchType = 19 AND Cancelled = 0
              {date_sql}
        ''', [company, fy] + date_args)
        pymt_row = c.fetchone()

        # 5. Receivables (Group 116 Sundry Debtors)
        c.execute(f'''
            SELECT COUNT(m.Code),
                   COALESCE(SUM(m.OpBal + COALESCE(s.TxnDr, 0) - COALESCE(s.TxnCr, 0)), 0)
            FROM busy_master1 m
            LEFT JOIN (
                SELECT MasterCode1,
                       SUM(CASE WHEN Value1 < 0 THEN ABS(Value1) ELSE 0 END) as TxnDr,
                       SUM(CASE WHEN Value1 > 0 THEN Value1 ELSE 0 END) as TxnCr
                FROM busy_tran2
                WHERE company_code = ? AND fy_year = ? AND RecType = 1
                GROUP BY MasterCode1
            ) s ON s.MasterCode1 = m.Code
            WHERE m.company_code = ? AND m.fy_year = ? AND m.MasterType = 2 AND {group_tree_sql()}
        ''', (company, fy, company, fy, 116, company, fy))
        rec_row = c.fetchone()

        # 6. Payables (Group 117 Sundry Creditors)
        c.execute(f'''
            SELECT COUNT(m.Code),
                   COALESCE(SUM(-m.OpBal + COALESCE(s.TxnCr, 0) - COALESCE(s.TxnDr, 0)), 0)
            FROM busy_master1 m
            LEFT JOIN (
                SELECT MasterCode1,
                       SUM(CASE WHEN Value1 < 0 THEN ABS(Value1) ELSE 0 END) as TxnDr,
                       SUM(CASE WHEN Value1 > 0 THEN Value1 ELSE 0 END) as TxnCr
                FROM busy_tran2
                WHERE company_code = ? AND fy_year = ? AND RecType = 1
                GROUP BY MasterCode1
            ) s ON s.MasterCode1 = m.Code
            WHERE m.company_code = ? AND m.fy_year = ? AND m.MasterType = 2 AND {group_tree_sql()}
        ''', (company, fy, company, fy, 117, company, fy))
        pay_row = c.fetchone()

        # 7. Cash & Bank Accounts (Groups 111, 112, 128)
        c.execute('''
            SELECT m.Code, m.ParentGrp, m.Name, m.OpBal, COALESCE(SUM(t2.Value1), 0) as TxnSum
            FROM busy_master1 m
            LEFT JOIN busy_tran2 t2 ON t2.company_code = m.company_code AND t2.fy_year = m.fy_year AND t2.MasterCode1 = m.Code AND t2.RecType = 1
            WHERE m.company_code = ? AND m.fy_year = ? AND m.MasterType = 2 AND m.ParentGrp IN (111, 112, 128)
            GROUP BY m.Code, m.ParentGrp, m.Name, m.OpBal
            ORDER BY m.ParentGrp, m.Name
        ''', (company, fy))
        cash_bank = []
        for r in c.fetchall():
            net = r['OpBal'] - r['TxnSum']  # OpBal: +Dr; Tran2.Value1: -Dr
            cash_bank.append({
                'name': r['Name'],
                'group': 'Cash' if r['ParentGrp'] == 111 else ('Bank' if r['ParentGrp'] == 112 else 'Bank OD'),
                'balance': abs(net),
                'bal_type': 'Dr' if net >= 0 else 'Cr'
            })

        # 8. Monthly Sales Trend
        c.execute('''
            SELECT SUBSTR(Date, 1, 7) as MonthStr, SUM(VchAmt) as SalesTotal
            FROM busy_tran1
            WHERE company_code = ? AND fy_year = ? AND VchType = 9 AND Cancelled = 0
            GROUP BY MonthStr
            ORDER BY MonthStr
        ''', (company, fy))
        sales_trend = [{'month': r[0], 'amount': r[1]} for r in c.fetchall()]

        # 9. Latest Activity Date
        c.execute('SELECT MAX(Date) FROM busy_tran1 WHERE company_code = ? AND fy_year = ?', (company, fy))
        latest_date = c.fetchone()[0] or f'{fy}-04-01'

        # 10. Top 5 Customers (Demo Widget)
        c.execute('''
            SELECT m.Code, m.Name, COUNT(t1.VchCode) as BillsCount, SUM(t1.VchAmt) as TotalAmt
            FROM busy_tran1 t1
            JOIN busy_master1 m ON m.company_code = t1.company_code AND m.fy_year = t1.fy_year AND m.Code = t1.MasterCode1
            WHERE t1.company_code = ? AND t1.fy_year = ? AND t1.VchType = 9 AND t1.Cancelled = 0
            GROUP BY m.Code, m.Name
            ORDER BY TotalAmt DESC
            LIMIT 5
        ''', (company, fy))
        top_customers = [dict(r) for r in c.fetchall()]

        # 11. Top 5 Items Sold (Demo Widget)
        c.execute('''
            SELECT m.Code, m.Name, ABS(SUM(t2.Value1)) as Qty, ABS(SUM(t2.Value3)) as TotalAmt
            FROM busy_tran2 t2
            JOIN busy_tran1 t1 ON t1.company_code = t2.company_code AND t1.fy_year = t2.fy_year AND t1.VchCode = t2.VchCode
            JOIN busy_master1 m ON m.company_code = t2.company_code AND m.fy_year = t2.fy_year AND m.Code = t2.MasterCode1
            WHERE t2.company_code = ? AND t2.fy_year = ? AND t1.VchType = 9 AND t1.Cancelled = 0
              AND t2.RecType IN (2, 7)  -- 7 = invoice lines billed against a delivery challan
            GROUP BY m.Code, m.Name
            ORDER BY TotalAmt DESC
            LIMIT 5
        ''', (company, fy))
        top_items = [dict(r) for r in c.fetchall()]

        conn.close()

        self.send_json({
            "sales": {"count": sales_row[0], "total": sales_row[1]},
            "purchases": {"count": purc_row[0], "total": purc_row[1]},
            "receipts": {"count": rcpt_row[0], "total": rcpt_row[1]},
            "payments": {"count": pymt_row[0], "total": pymt_row[1]},
            "receivables": {"count": rec_row[0], "total": abs(rec_row[1]), "type": "Dr" if rec_row[1] >= 0 else "Cr"},
            "payables": {"count": pay_row[0], "total": abs(pay_row[1]), "type": "Cr" if pay_row[1] >= 0 else "Dr"},
            "cash_bank": cash_bank,
            "sales_trend": sales_trend,
            "latest_date": latest_date,
            "top_customers": top_customers,
            "top_items": top_items
        })

    def handle_parties(self, company, fy, search, group, route=0):
        conn = self.get_db()
        c = conn.cursor()
        search_filter = f"%{search}%"

        group_filter = ""
        group_args = ()
        if group in ('debtors', 'creditors'):
            group_filter = f"AND {group_tree_sql()}"
            group_args = (116 if group == 'debtors' else 117, company, fy)
        debtor_codes = group_account_codes(c, company, fy, 116)
        creditor_codes = group_account_codes(c, company, fy, 117)

        route_filter = ""
        if route > 0:
            route_filter = f"AND (';' || m.MasterSeriesGrp || ';') LIKE '%;{route};%'"

        c.execute(f'''
            SELECT m.Code, m.Name, m.PrintName, m.ParentGrp, m.OpBal, m.MasterSeriesGrp,
                   COALESCE(a.GSTNo, '') as GSTNo,
                   COALESCE(a.Mobile, '') as Mobile,
                   COALESCE(a.Address1, '') as City,
                   COALESCE(s.TxnDr, 0) as TxnDr,
                   COALESCE(s.TxnCr, 0) as TxnCr
            FROM busy_master1 m
            LEFT JOIN busy_masteraddressinfo a ON a.company_code = m.company_code AND a.fy_year = m.fy_year AND a.MasterCode = m.Code
            LEFT JOIN (
                SELECT MasterCode1,
                       SUM(CASE WHEN Value1 < 0 THEN ABS(Value1) ELSE 0 END) as TxnDr,
                       SUM(CASE WHEN Value1 > 0 THEN Value1 ELSE 0 END) as TxnCr
                FROM busy_tran2
                WHERE company_code = ? AND fy_year = ? AND RecType = 1
                GROUP BY MasterCode1
            ) s ON s.MasterCode1 = m.Code
            WHERE m.company_code = ? AND m.fy_year = ? AND m.MasterType = 2
              AND (m.Name LIKE ? OR a.Mobile LIKE ? OR a.GSTNo LIKE ?)
              {group_filter}
              {route_filter}
            ORDER BY m.Name
            LIMIT 150
        ''', (company, fy, company, fy, search_filter, search_filter, search_filter) + group_args)

        parties = []
        for r in c.fetchall():
            d = dict(r)
            if d['Code'] in creditor_codes: # Creditor
                bal = -d['OpBal'] + d['TxnCr'] - d['TxnDr']
                d['BalType'] = 'Cr' if bal >= 0 else 'Dr'
            else: # Debtor / Asset / General
                bal = d['OpBal'] + d['TxnDr'] - d['TxnCr']
                d['BalType'] = 'Dr' if bal >= 0 else 'Cr'

            d['CurrentBalance'] = abs(bal)
            d['GroupName'] = 'Debtor' if d['Code'] in debtor_codes else ('Creditor' if d['Code'] in creditor_codes else 'Account')
            parties.append(d)

        conn.close()
        self.send_json(parties)

    def handle_statement(self, company, fy, code):
        conn = self.get_db()
        c = conn.cursor()

        # Party Details with Opening Balance
        c.execute('''
            SELECT m.Code, m.Name, m.PrintName, m.OpBal,
                   COALESCE(a.GSTNo, '') as GSTNo,
                   COALESCE(a.Mobile, '') as Mobile,
                   COALESCE(a.Address1, '') as Address
            FROM busy_master1 m
            LEFT JOIN busy_masteraddressinfo a ON a.company_code = m.company_code AND a.fy_year = m.fy_year AND a.MasterCode = m.Code
            WHERE m.company_code = ? AND m.fy_year = ? AND m.Code = ?
        ''', (company, fy, code))
        party = dict(c.fetchone() or {})
        op_bal = party.get('OpBal') or 0.0

        # Accounting entries (RecType = 1)
        c.execute('''
            SELECT t1.VchCode, t1.Date, t1.VchType, t1.VchNo, t2.Value1,
                   CASE WHEN t2.Value1 > 0 THEN t2.Value1 ELSE 0 END as Debit,
                   CASE WHEN t2.Value1 < 0 THEN ABS(t2.Value1) ELSE 0 END as Credit
            FROM busy_tran2 t2
            JOIN busy_tran1 t1 ON t1.company_code = t2.company_code AND t1.fy_year = t2.fy_year AND t1.VchCode = t2.VchCode
            WHERE t2.company_code = ? AND t2.fy_year = ? AND t2.MasterCode1 = ? AND t2.RecType = 1
            ORDER BY t1.Date, t1.VchCode, t2.SrNo
        ''', (company, fy, code))

        lines = []
        running = op_bal
        total_dr = 0.0
        total_cr = 0.0

        for r in c.fetchall():
            d = dict(r)
            vch_type = d['VchType']
            amt = d['Value1']
            dr, cr, part = ledger_side(vch_type, amt)

            total_dr += dr
            total_cr += cr
            running += (dr - cr)

            d['Debit'] = dr
            d['Credit'] = cr
            d['Particulars'] = part
            d['RunningBalance'] = abs(running)
            d['BalType'] = 'Dr' if running >= 0 else 'Cr'
            d['VchTypeName'] = VCH_MAP.get(vch_type, f"Type {vch_type}")
            lines.append(d)

        conn.close()

        sec_token = generate_secure_token('led', company, fy, code)
        self.send_json({
            "party": party,
            "transactions": lines,
            "opening_balance": abs(op_bal),
            "op_bal_type": 'Dr' if op_bal >= 0 else 'Cr',
            "total_debit": total_dr,
            "total_credit": total_cr,
            "closing_balance": abs(running),
            "closing_bal_type": 'Dr' if running >= 0 else 'Cr',
            "secure_token": sec_token,
            "secure_pdf_url": f"/api/share/ledger-pdf?token={sec_token}"
        })

    def handle_outstanding(self, company, fy, out_type, route=0):
        conn = self.get_db()
        c = conn.cursor()

        # Group 116 for Receivable (Debtors), Group 117 for Payable (Creditors)
        target_group = 116 if out_type == 'receivable' else 117

        route_filter = ""
        if route > 0:
            route_filter = f"AND (';' || m.MasterSeriesGrp || ';') LIKE '%;{route};%'"

        c.execute(f'''
            SELECT m.Code, m.Name, m.ParentGrp, m.OpBal, m.MasterSeriesGrp,
                   COALESCE(a.Mobile, '') as Mobile,
                   COALESCE(a.Address1, '') as City,
                   COALESCE(s.TxnDr, 0) as TxnDr,
                   COALESCE(s.TxnCr, 0) as TxnCr,
                   COALESCE(b.BillsCount, 0) as BillsCount
            FROM busy_master1 m
            LEFT JOIN busy_masteraddressinfo a ON a.company_code = m.company_code AND a.fy_year = m.fy_year AND a.MasterCode = m.Code
            LEFT JOIN (
                SELECT MasterCode1,
                       SUM(CASE WHEN Value1 < 0 THEN ABS(Value1) ELSE 0 END) as TxnDr,
                       SUM(CASE WHEN Value1 > 0 THEN Value1 ELSE 0 END) as TxnCr
                FROM busy_tran2
                WHERE company_code = ? AND fy_year = ? AND RecType = 1
                GROUP BY MasterCode1
            ) s ON s.MasterCode1 = m.Code
            LEFT JOIN (
                -- count only bills that are still pending
                SELECT MasterCode1, COUNT(*) as BillsCount
                FROM (
                    SELECT MasterCode1, RefCode
                    FROM busy_tran3
                    WHERE company_code = ? AND fy_year = ? AND RecType = 1
                    GROUP BY MasterCode1, RefCode
                    HAVING ABS(SUM(Value1)) > 0.5
                )
                GROUP BY MasterCode1
            ) b ON b.MasterCode1 = m.Code
            WHERE m.company_code = ? AND m.fy_year = ? AND m.MasterType = 2 AND {group_tree_sql()}
              {route_filter}
            ORDER BY m.Name
        ''', (company, fy, company, fy, company, fy, target_group, company, fy))

        party_rows = [dict(r) for r in c.fetchall()]
        bill_ageing = compute_bill_ageing(c, company, fy, is_creditor=(target_group == 117))

        parties = []
        total_pending = 0.0
        total_parties = 0
        total_ageing = {k: 0.0 for k in AGEING_SLABS}

        for d in party_rows:
            if target_group == 117: # Payable / Creditors
                bal = -d['OpBal'] + d['TxnCr'] - d['TxnDr']
                d['BalType'] = 'Cr' if bal >= 0 else 'Dr'
            else: # Receivable / Debtors
                bal = d['OpBal'] + d['TxnDr'] - d['TxnCr']
                d['BalType'] = 'Dr' if bal >= 0 else 'Cr'

            if abs(bal) > 1.0:
                d['PendingAmt'] = abs(bal)
                total_pending += bal  # net: advances (opposite balance) reduce the total, as in BUSY
                total_parties += 1
                # Ageing of pending bills by bill date; whatever the bills don't explain is "On Account"
                slabs = dict(bill_ageing.get(d['Code'], {}))
                billed = sum(slabs.values())
                slabs['On Account'] = slabs.get('On Account', 0.0) + (bal - billed)
                d['Ageing'] = {k: round(slabs.get(k, 0.0), 2) for k in AGEING_SLABS}
                for k in AGEING_SLABS:
                    total_ageing[k] += d['Ageing'][k]
                parties.append(d)

        # Sort by PendingAmt descending
        parties.sort(key=lambda x: x['PendingAmt'], reverse=True)

        conn.close()
        self.send_json({
            "type": out_type,
            "total_pending": total_pending,
            "total_parties": total_parties,
            "ageing": {k: round(v, 2) for k, v in total_ageing.items()},
            "ageing_as_of": datetime.now().strftime('%Y-%m-%d'),
            "parties": parties
        })

    def handle_party_bills(self, company, fy, party_code):
        conn = self.get_db()
        c = conn.cursor()

        # Bill-by-bill references: busy_tran3 RecType 1, one RefCode per bill (VchCode 0 = opening bills).
        # Method 1 = new reference, 2 = against reference. Value1 is signed like Tran2 (negative = Debit),
        # so a bill's pending amount is the signed sum over its RefCode.
        is_creditor = party_code in group_account_codes(c, company, fy, 117)
        c.execute('''
            SELECT t3.RefCode,
                   COALESCE(MAX(CASE WHEN t3.Method = 1 THEN NULLIF(t3.BillNo, '') END), MAX(NULLIF(t3.BillNo, '')), 'OB-' || t3.RefCode) as BillNo,
                   COALESCE(MIN(CASE WHEN t3.Method = 1 THEN NULLIF(t3.BillDate, '') END), MIN(NULLIF(t3.BillDate, '')), MIN(t1.Date), '') as BillDate,
                   COALESCE(MIN(CASE WHEN t3.Method = 1 THEN NULLIF(t3.DueDate, '') END), MIN(NULLIF(t3.DueDate, '')), '') as DueDate,
                   SUM(CASE WHEN t3.Method = 1 THEN t3.Value1 ELSE 0 END) as OriginalSigned,
                   SUM(t3.Value1) as PendingSigned
            FROM busy_tran3 t3
            LEFT JOIN busy_tran1 t1 ON t1.company_code = t3.company_code AND t1.fy_year = t3.fy_year AND t1.VchCode = t3.VchCode
            WHERE t3.company_code = ? AND t3.fy_year = ? AND t3.MasterCode1 = ? AND t3.RecType = 1
            GROUP BY t3.RefCode
            HAVING ABS(SUM(t3.Value1)) > 0.5
            ORDER BY BillDate DESC
            LIMIT 200
        ''', (company, fy, party_code))

        bills = []
        today = datetime.now()
        for r in c.fetchall():
            d = dict(r)
            pending = d.pop('PendingSigned')
            original = d.pop('OriginalSigned')
            # Debit-positive for debtors/others, credit-positive for creditors
            pending = pending if is_creditor else -pending
            d['OriginalAmt'] = abs(original)
            d['PendingAmt'] = abs(pending)
            normal = 'Cr' if is_creditor else 'Dr'
            other = 'Dr' if is_creditor else 'Cr'
            d['BalType'] = normal if pending > 0 else other
            d['IsAdvance'] = pending < 0
            try:
                ddate = datetime.strptime((d['DueDate'] or d['BillDate'])[:10], '%Y-%m-%d')
                days = (today - ddate).days
            except (ValueError, TypeError):
                days = 0
            d['DaysOverdue'] = 0 if d['IsAdvance'] else max(0, days)
            bills.append(d)

        conn.close()
        self.send_json(bills)

    def handle_daybook(self, company, fy, date_str):
        conn = self.get_db()
        c = conn.cursor()

        # If date not provided, get the most active or latest date
        if not date_str:
            c.execute('SELECT MAX(Date) FROM busy_tran1 WHERE company_code = ? AND fy_year = ?', (company, fy))
            date_str = c.fetchone()[0] or f'{fy}-04-01'

        c.execute('''
            SELECT t1.VchCode, t1.VchType, t1.VchNo, t1.Date, t1.VchAmt,
                   COALESCE(m.Name, 'General Entry') as PartyName
            FROM busy_tran1 t1
            LEFT JOIN busy_master1 m ON m.company_code = t1.company_code AND m.fy_year = t1.fy_year AND m.Code = t1.MasterCode1
            WHERE t1.company_code = ? AND t1.fy_year = ? AND t1.Date = ? AND t1.Cancelled = 0
            ORDER BY t1.VchCode
        ''', (company, fy, date_str))

        vouchers = []
        total_amt = 0.0

        for r in c.fetchall():
            d = dict(r)
            d['VchTypeName'] = VCH_MAP.get(d['VchType'], f"Type {d['VchType']}")
            total_amt += d['VchAmt']
            vouchers.append(d)

        conn.close()
        self.send_json({
            "date": date_str,
            "total_vouchers": len(vouchers),
            "total_amount": total_amt,
            "vouchers": vouchers
        })

    def handle_trialbalance(self, company, fy):
        conn = self.get_db()
        c = conn.cursor()

        c.execute('''
            SELECT g.Code, g.Name as GroupName,
                   SUM(CASE WHEN (m.OpBal - COALESCE(s.TxnSum, 0)) > 0 THEN (m.OpBal - COALESCE(s.TxnSum, 0)) ELSE 0 END) as TotalDebit,
                   SUM(CASE WHEN (m.OpBal - COALESCE(s.TxnSum, 0)) < 0 THEN ABS(m.OpBal - COALESCE(s.TxnSum, 0)) ELSE 0 END) as TotalCredit
            FROM busy_master1 g
            JOIN busy_master1 m ON m.company_code = g.company_code AND m.fy_year = g.fy_year AND m.ParentGrp = g.Code AND m.MasterType = 2
            LEFT JOIN (
                SELECT MasterCode1, SUM(Value1) as TxnSum
                FROM busy_tran2
                WHERE company_code = ? AND fy_year = ? AND RecType = 1
                GROUP BY MasterCode1
            ) s ON s.MasterCode1 = m.Code
            WHERE g.company_code = ? AND g.fy_year = ? AND g.MasterType = 1
            GROUP BY g.Code, g.Name
            HAVING (TotalDebit + TotalCredit) > 0
            ORDER BY g.Name
        ''', (company, fy, company, fy))

        groups = []
        grand_dr = 0.0
        grand_cr = 0.0

        for r in c.fetchall():
            d = dict(r)
            dr = d['TotalDebit']
            cr = d['TotalCredit']
            grand_dr += dr
            grand_cr += cr
            net = dr - cr
            d['NetBal'] = abs(net)
            d['BalType'] = 'Dr' if net >= 0 else 'Cr'
            groups.append(d)

        conn.close()
        self.send_json({
            "grand_debit": grand_dr,
            "grand_credit": grand_cr,
            "groups": groups
        })

    def handle_vouchers_list(self, company, fy, vch_type, search, series_code=0, from_date="", to_date=""):
        conn = self.get_db()
        c = conn.cursor()
        search_filter = f"%{search}%"

        type_filter = ""
        if vch_type == 'sales': type_filter = "AND t1.VchType IN (9, 24)"
        elif vch_type in ('sales_challan', 'delivery', 'challan'): type_filter = "AND t1.VchType = 11"
        elif vch_type == 'sales_order': type_filter = "AND t1.VchType = 12"
        elif vch_type == 'purchase_order': type_filter = "AND t1.VchType = 13"
        elif vch_type == 'quotation': type_filter = "AND t1.VchType IN (26, 27)"
        elif vch_type == 'production': type_filter = "AND (t1.VchType = 20 OR (t1.VchType = 5 AND (s.Name LIKE '%FG%' OR s.Name LIKE '%PROD%')))"
        elif vch_type in ('stock_transfer', 'mat_issue'): type_filter = "AND (t1.VchType = 8 OR (t1.VchType = 5 AND s.Name NOT LIKE '%FG%'))"
        elif vch_type == 'mat_receipt': type_filter = "AND t1.VchType = 6"
        elif vch_type == 'stock_journal': type_filter = "AND t1.VchType = 7"
        elif vch_type == 'unassemble': type_filter = "AND t1.VchType = 21"
        elif vch_type == 'purchase': type_filter = "AND t1.VchType = 2"
        elif vch_type == 'return': type_filter = "AND t1.VchType IN (3, 10)"
        elif vch_type == 'sales_return': type_filter = "AND t1.VchType = 3"
        elif vch_type == 'purchase_return': type_filter = "AND t1.VchType = 10"
        elif vch_type == 'purchase_challan': type_filter = "AND t1.VchType = 4"
        elif vch_type == 'debit_note': type_filter = "AND t1.VchType = 17"
        elif vch_type == 'credit_note': type_filter = "AND t1.VchType = 18"
        elif vch_type == 'salary': type_filter = "AND t1.VchType = 28"
        elif vch_type == 'receipt': type_filter = "AND t1.VchType = 14"
        elif vch_type == 'payment': type_filter = "AND t1.VchType = 19"
        elif vch_type == 'contra': type_filter = "AND t1.VchType = 15"
        elif vch_type == 'journal': type_filter = "AND t1.VchType = 16"
        elif vch_type == 'indent': type_filter = "AND t1.VchType = 29"
        elif vch_type.isdigit(): type_filter = f"AND t1.VchType = {int(vch_type)}"  

        series_filter = ""
        if series_code > 0:
            series_filter = f"AND t1.VchSeriesCode = {series_code}"

        date_sql = ""
        date_args = []
        if from_date and to_date:
            date_sql = "AND t1.Date BETWEEN ? AND ?"
            date_args = [from_date, to_date]
        elif from_date:
            date_sql = "AND t1.Date >= ?"
            date_args = [from_date]
        elif to_date:
            date_sql = "AND t1.Date <= ?"
            date_args = [to_date]

        c.execute(f'''
            SELECT t1.VchCode, t1.VchType, t1.VchSeriesCode, t1.VchNo, t1.Date, t1.VchAmt,
                   CASE 
                       WHEN t1.VchType = 5 THEN COALESCE(m2.Name, m.Name, 'General Entry')
                       ELSE COALESCE(m.Name, 'General Entry')
                   END as PartyName,
                   CASE 
                       WHEN SUBSTR(s.Name, 1, 2) GLOB '[0-9][0-9]' THEN SUBSTR(s.Name, 3)
                       ELSE COALESCE(s.Name, '')
                   END as SeriesName
            FROM busy_tran1 t1
            LEFT JOIN busy_master1 m ON m.company_code = t1.company_code AND m.fy_year = t1.fy_year AND m.Code = t1.MasterCode1
            LEFT JOIN busy_master1 m2 ON m2.company_code = t1.company_code AND m2.fy_year = t1.fy_year AND m2.Code = t1.MasterCode2
            LEFT JOIN busy_master1 s ON s.company_code = t1.company_code AND s.fy_year = t1.fy_year AND s.Code = t1.VchSeriesCode AND s.MasterType = 21
            WHERE t1.company_code = ? AND t1.fy_year = ? AND t1.Cancelled = 0
              {type_filter}
              {series_filter}
              {date_sql}
              AND (t1.VchNo LIKE ? OR m.Name LIKE ? OR m2.Name LIKE ? OR s.Name LIKE ?)
            ORDER BY t1.Date DESC, t1.VchCode DESC
            LIMIT 150
        ''', [company, fy] + date_args + [search_filter, search_filter, search_filter, search_filter])

        vouchers = []
        for r in c.fetchall():
            d = dict(r)
            s_name = d.get('SeriesName', '') or ''
            if 'FG-' in s_name or 'FG ' in s_name or s_name.startswith('FG') or d['VchType'] == 20:
                d['VchTypeName'] = "Production"
            elif d['VchType'] in (5, 8):
                d['VchTypeName'] = "Stock Transfer"
            else:
                d['VchTypeName'] = VCH_MAP.get(d['VchType'], f"Type {d['VchType']}")
            vouchers.append(d)

        conn.close()
        self.send_json(vouchers)

    def handle_stock(self, company, fy, search, mc_code=0):
        conn = self.get_db()
        c = conn.cursor()
        search_filter = f"%{search}%"
        stock = compute_item_stock(c, company, fy, mc_code)

        c.execute('''
            SELECT Code, Name, PrintName FROM busy_master1
            WHERE company_code = ? AND fy_year = ? AND MasterType = 6 AND Name LIKE ?
            ORDER BY Name
        ''', (company, fy, search_filter))

        items = []
        total_items = 0
        total_val = 0.0

        for r in c.fetchall():
            d = dict(r)
            s = stock.get(d['Code'], EMPTY_STOCK)
            if mc_code > 0 and s['OpQty'] == 0 and s['MovementQty'] == 0:
                continue  # item never touched this material centre
            d.update(s)
            total_items += 1
            total_val += d['ClosingValue']
            if len(items) < 1000:
                items.append(d)

        conn.close()
        self.send_json({
            "total_items": total_items,
            "total_stock_value": total_val,
            "mc_filter": mc_code,
            "items": items
        })

    def handle_materialcentres(self, company, fy):
        conn = self.get_db()
        c = conn.cursor()
        c.execute('''
            SELECT Code, Name, Alias
            FROM busy_master1
            WHERE company_code = ? AND fy_year = ? AND MasterType = 11
            ORDER BY Name
        ''', (company, fy))
        data = [dict(r) for r in c.fetchall()]
        conn.close()
        self.send_json(data)

    def handle_items_list(self, company, fy, search=''):
        conn = self.get_db()
        c = conn.cursor()
        search_filter = f"%{search}%"
        c.execute('''
            SELECT Code, Name, PrintName, Alias
            FROM busy_master1
            WHERE company_code = ? AND fy_year = ? AND MasterType = 6 AND (Name LIKE ? OR Alias LIKE ?)
            ORDER BY Name ASC
            LIMIT 500
        ''', (company, fy, search_filter, search_filter))
        data = [dict(r) for r in c.fetchall()]
        conn.close()
        self.send_json(data)

    def handle_series(self, company, fy):
        conn = self.get_db()
        c = conn.cursor()
        c.execute('''
            SELECT Code, Name, Alias,
                   CASE 
                       WHEN SUBSTR(Name, 1, 2) GLOB '[0-9][0-9]' THEN CAST(SUBSTR(Name, 1, 2) AS INTEGER)
                       ELSE 0
                   END as VchType,
                   CASE 
                       WHEN SUBSTR(Name, 1, 2) GLOB '[0-9][0-9]' THEN SUBSTR(Name, 3)
                       ELSE Name
                   END as CleanName
            FROM busy_master1
            WHERE company_code = ? AND fy_year = ? AND MasterType = 21
            ORDER BY Name
        ''', (company, fy))
        data = [dict(r) for r in c.fetchall()]
        conn.close()
        self.send_json(data)

    def handle_masterseries(self, company, fy):
        conn = self.get_db()
        c = conn.cursor()
        c.execute('''
            SELECT Code, Name, Alias
            FROM busy_master1
            WHERE company_code = ? AND fy_year = ? AND MasterType = 26
            ORDER BY Name
        ''', (company, fy))
        data = [dict(r) for r in c.fetchall()]
        conn.close()
        self.send_json(data)

    def handle_bcn_stock(self, company, fy, search):
        conn = self.get_db()
        c = conn.cursor()
        search_filter = f"%{search}%"
        c.execute('''
            SELECT m.Code, m.Name, m.PrintName,
                   COUNT(t1.VchCode) as VoucherCount,
                   COALESCE(SUM(t1.VchAmt), 0) as TotalValue,
                   COALESCE(SUM(p.Qty), 0) as ProducedQty
            FROM busy_master1 m
            LEFT JOIN busy_tran1 t1 ON t1.company_code = m.company_code AND t1.fy_year = m.fy_year AND t1.MasterCode2 = m.Code AND t1.VchType = 6 AND t1.Cancelled = 0
            LEFT JOIN (
                -- produced (inward) qty per production voucher, aggregated so voucher totals are not multiplied by line count
                SELECT company_code, fy_year, VchCode, SUM(Value1) as Qty
                FROM busy_tran2
                WHERE RecType = 2 AND Value1 > 0
                GROUP BY company_code, fy_year, VchCode
            ) p ON p.company_code = t1.company_code AND p.fy_year = t1.fy_year AND p.VchCode = t1.VchCode
            WHERE m.company_code = ? AND m.fy_year = ? AND m.MasterType = 15 AND m.Name LIKE ?
            GROUP BY m.Code, m.Name, m.PrintName
            HAVING VoucherCount > 0 OR ProducedQty > 0
            ORDER BY TotalValue DESC
            LIMIT 500
        ''', (company, fy, search_filter))
        items = [dict(r) for r in c.fetchall()]
        total_val = sum(it['TotalValue'] for it in items)
        total_qty = sum(it['ProducedQty'] for it in items)
        conn.close()
        self.send_json({
            "total_items": len(items),
            "total_stock_value": total_val,
            "total_produced_qty": total_qty,
            "items": items
        })

    def handle_parameter_stock(self, company, fy, search='', mc_code=0):
        conn = self.get_db()
        c = conn.cursor()
        search_filter = f"%{search}%"
        # MasterType 15 = Bill of Material variants (CM1 = finished item, CM2 = unit).
        # BUSY's parameterised-stock table (ItemParamDet) is empty for these companies,
        # so each variant shows the real stock of its finished item.
        stock = compute_item_stock(c, company, fy, mc_code)
        c.execute('''
            SELECT
                m.Code,
                m.Name as VariantName,
                m.CM1 as ParentCode,
                COALESCE(p.Name, m.Name) as ParentName,
                COALESCE(u.Name, 'Pcs.') as UnitName
            FROM busy_master1 m
            LEFT JOIN busy_master1 p ON p.company_code = m.company_code AND p.fy_year = m.fy_year AND p.Code = m.CM1
            LEFT JOIN busy_master1 u ON u.company_code = m.company_code AND u.fy_year = m.fy_year AND u.Code = m.CM2
            WHERE m.company_code = ? AND m.fy_year = ? AND m.MasterType = 15 AND (m.Name LIKE ? OR p.Name LIKE ?)
            ORDER BY m.Name
            LIMIT 1000
        ''', (company, fy, search_filter, search_filter))

        items = []
        tot_qty = 0
        tot_val = 0
        counted_parents = set()
        for r in c.fetchall():
            row = dict(r)
            s = stock.get(row['ParentCode'], EMPTY_STOCK)
            row['OpQty'] = s['OpQty']
            row['MovementQty'] = s['MovementQty']
            row['AvgRate'] = s['AvgRate']
            row['ClosingQty'] = s['ClosingQty']
            row['StockValue'] = s['ClosingValue']
            items.append(row)
            # Several BOM variants can share one finished item: count its stock once.
            if row['ParentCode'] not in counted_parents:
                counted_parents.add(row['ParentCode'])
                tot_qty += s['ClosingQty']
                tot_val += s['ClosingValue']

        conn.close()
        self.send_json({
            "total_items": len(items),
            "total_qty": round(tot_qty, 2),
            "total_stock_value": round(tot_val, 2),
            "items": items
        })

    def handle_batch_stock(self, company, fy, search='', mc_code=0):
        conn = self.get_db()
        c = conn.cursor()
        search_filter = f"%{search}%"

        mc_clause = ""
        params = [company, fy, search_filter, search_filter, search_filter, search_filter]
        if mc_code > 0:
            mc_clause = "AND t3.MasterCode2 = ?"
            params.append(mc_code)

        c.execute(f'''
            SELECT 
                m1.Code as ItemCode,
                m1.Name as ItemName,
                mc.Code as McCode,
                mc.Name as MaterialCenter,
                t3.BillNo as BatchNo,
                MAX(t3.Narration) as Parameter,
                MAX(t3.BillDate) as LastDate,
                SUM(CASE WHEN t3.Value1 > 0 THEN t3.Value1 ELSE 0 END) as QtyIn,
                SUM(CASE WHEN t3.Value1 < 0 THEN ABS(t3.Value1) ELSE 0 END) as QtyOut,
                SUM(t3.Value1) as ClosingQty
            FROM busy_tran3 t3
            JOIN busy_master1 m1 ON m1.company_code = t3.company_code AND m1.fy_year = t3.fy_year AND m1.Code = t3.MasterCode1
            JOIN busy_master1 mc ON mc.company_code = t3.company_code AND mc.fy_year = t3.fy_year AND mc.Code = t3.MasterCode2
            WHERE t3.company_code = ? AND t3.fy_year = ? AND t3.RecType = 2 AND t3.BillNo <> ''
              AND (m1.Name LIKE ? OR t3.BillNo LIKE ? OR mc.Name LIKE ? OR t3.Narration LIKE ?)
              {mc_clause}
            GROUP BY m1.Code, m1.Name, mc.Code, mc.Name, t3.BillNo
            HAVING ROUND(SUM(t3.Value1), 2) <> 0
            ORDER BY mc.Name ASC, m1.Name ASC, t3.BillNo ASC
        ''', tuple(params))

        batches = []
        total_qty = 0.0
        total_batches = 0
        for r in c.fetchall():
            d = dict(r)
            total_qty += d['ClosingQty']  # totals cover every batch, not only the rows sent
            total_batches += 1
            if len(batches) < 500:
                batches.append(d)

        conn.close()
        self.send_json({
            "status": "success",
            "total_batches": total_batches,
            "total_qty": total_qty,
            "batches": batches
        })

    def handle_batch_stock_pdf(self, company, fy, search='', mc_code=0):
        conn = self.get_db()
        c = conn.cursor()

        # Company Info
        c.execute('SELECT name, print_name, gstin, mobile, email FROM busy_company WHERE company_code = ?', (company,))
        comp_row = c.fetchone()
        comp = dict(comp_row) if comp_row else {'name': 'Company', 'print_name': 'Company', 'gstin': '', 'mobile': '', 'email': ''}

        search_filter = f"%{search}%"
        mc_clause = ""
        params = [company, fy, search_filter, search_filter, search_filter, search_filter]
        if mc_code > 0:
            mc_clause = "AND t3.MasterCode2 = ?"
            params.append(mc_code)

        c.execute(f'''
            SELECT 
                m1.Name as ItemName,
                mc.Name as MaterialCenter,
                t3.BillNo as BatchNo,
                MAX(t3.Narration) as Parameter,
                MAX(t3.BillDate) as LastDate,
                SUM(CASE WHEN t3.Value1 > 0 THEN t3.Value1 ELSE 0 END) as QtyIn,
                SUM(CASE WHEN t3.Value1 < 0 THEN ABS(t3.Value1) ELSE 0 END) as QtyOut,
                SUM(t3.Value1) as ClosingQty
            FROM busy_tran3 t3
            JOIN busy_master1 m1 ON m1.company_code = t3.company_code AND m1.fy_year = t3.fy_year AND m1.Code = t3.MasterCode1
            JOIN busy_master1 mc ON mc.company_code = t3.company_code AND mc.fy_year = t3.fy_year AND mc.Code = t3.MasterCode2
            WHERE t3.company_code = ? AND t3.fy_year = ? AND t3.RecType = 2 AND t3.BillNo <> ''
              AND (m1.Name LIKE ? OR t3.BillNo LIKE ? OR mc.Name LIKE ? OR t3.Narration LIKE ?)
              {mc_clause}
            GROUP BY m1.Code, m1.Name, mc.Code, mc.Name, t3.BillNo
            HAVING ROUND(SUM(t3.Value1), 2) <> 0
            ORDER BY mc.Name ASC, m1.Name ASC, t3.BillNo ASC
        ''', tuple(params))

        rows = c.fetchall()
        total_in = sum(r['QtyIn'] for r in rows)
        total_out = sum(r['QtyOut'] for r in rows)
        total_closing = sum(r['ClosingQty'] for r in rows)

        table_rows = ""
        for i, r in enumerate(rows, 1):
            param_badge = f"<span style='font-size:10px; color:#64748b;'> ({r['Parameter']})</span>" if r['Parameter'] else ""
            table_rows += f"""
            <tr style="border-bottom: 1px solid #e2e8f0; font-size: 11px;">
                <td style="padding: 6px 8px; text-align: center; color: #64748b;">{i}</td>
                <td style="padding: 6px 8px; font-weight: 600; color: #1e293b;">{r['ItemName']}{param_badge}</td>
                <td style="padding: 6px 8px; color: #475569;">{r['MaterialCenter']}</td>
                <td style="padding: 6px 8px; font-family: monospace; font-weight: bold; color: #0284c7;">{r['BatchNo']}</td>
                <td style="padding: 6px 8px; text-align: right; color: #059669;">{r['QtyIn']:,.2f}</td>
                <td style="padding: 6px 8px; text-align: right; color: #dc2626;">{r['QtyOut']:,.2f}</td>
                <td style="padding: 6px 8px; text-align: right; font-weight: bold; color: #0f172a;">{r['ClosingQty']:,.2f}</td>
            </tr>
            """

        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Batch Closing Stock - {comp['print_name'] or comp['name']}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 20px; color: #0f172a; background: #f8fafc; }}
        .paper {{ max-width: 900px; margin: 0 auto; background: white; padding: 30px; box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1); border-radius: 8px; }}
        .header {{ border-bottom: 2px solid #002b49; padding-bottom: 12px; margin-bottom: 15px; display: flex; justify-content: space-between; align-items: flex-start; }}
        .header h1 {{ margin: 0; font-size: 20px; color: #002b49; text-transform: uppercase; font-weight: 800; }}
        .header p {{ margin: 2px 0; font-size: 11px; color: #64748b; }}
        .badge {{ background: #002b49; color: white; padding: 4px 10px; border-radius: 4px; font-size: 11px; font-weight: bold; text-transform: uppercase; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
        th {{ background: #f1f5f9; color: #334155; font-size: 11px; text-transform: uppercase; padding: 8px; border-top: 1px solid #cbd5e1; border-bottom: 2px solid #cbd5e1; text-align: left; }}
        .actions {{ position: sticky; top: 10px; max-width: 900px; margin: 0 auto 15px auto; display: flex; justify-content: flex-end; gap: 10px; }}
        .btn {{ background: #002b49; color: white; border: none; padding: 8px 16px; border-radius: 6px; font-size: 12px; font-weight: 600; cursor: pointer; text-decoration: none; display: inline-flex; align-items: center; gap: 6px; }}
        .btn-green {{ background: #16a34a; }}
        @media print {{
            body {{ background: white; padding: 0; }}
            .paper {{ box-shadow: none; padding: 0; max-width: 100%; }}
            .actions {{ display: none !important; }}
        }}
    </style>
</head>
<body>
    <div class="actions">
        <button class="btn" onclick="window.print()">🖨️ Print / Save PDF</button>
        <a class="btn btn-green" href="https://api.whatsapp.com/send?text={comp['print_name'] or comp['name']}%20Batch%20Stock%20Summary:%20Total%20Batches:%20{len(rows)},%20Closing%20Qty:%20{total_closing:,.2f}" target="_blank">📱 WhatsApp</a>
    </div>
    <div class="paper">
        <div class="header">
            <div>
                <h1>{comp['print_name'] or comp['name']}</h1>
                <p>GSTIN: {comp['gstin'] or 'N/A'} | Mobile: {comp['mobile'] or 'N/A'}</p>
                <p style="font-weight: 600; color: #0284c7; margin-top: 4px;">BATCH CLOSING STOCK STATUS REPORT</p>
            </div>
            <div style="text-align: right;">
                <span class="badge">FY {fy_label(fy)}</span>
                <p style="margin-top: 8px;">Date: {datetime.now().strftime('%d-%m-%Y %H:%M')}</p>
                <p>Total Batches: <b>{len(rows)}</b></p>
            </div>
        </div>

        <table>
            <thead>
                <tr>
                    <th style="width: 30px; text-align: center;">#</th>
                    <th>Item Description</th>
                    <th style="width: 140px;">Material Center</th>
                    <th style="width: 110px;">Batch No</th>
                    <th style="width: 80px; text-align: right;">Total In</th>
                    <th style="width: 80px; text-align: right;">Total Out</th>
                    <th style="width: 90px; text-align: right;">Closing Qty</th>
                </tr>
            </thead>
            <tbody>
                {table_rows}
            </tbody>
            <tfoot>
                <tr style="background: #f8fafc; font-weight: bold; border-top: 2px solid #002b49; border-bottom: 2px solid #002b49;">
                    <td colspan="4" style="padding: 8px; text-align: right; text-transform: uppercase;">Total Active Stock:</td>
                    <td style="padding: 8px; text-align: right; color: #059669;">{total_in:,.2f}</td>
                    <td style="padding: 8px; text-align: right; color: #dc2626;">{total_out:,.2f}</td>
                    <td style="padding: 8px; text-align: right; color: #0f172a; font-size: 13px;">{total_closing:,.2f}</td>
                </tr>
            </tfoot>
        </table>
    </div>
</body>
</html>"""

        conn.close()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))

    def handle_daybook_pdf(self, company, fy, date_str):
        conn = self.get_db()
        c = conn.cursor()

        # Company Info
        c.execute('SELECT name, print_name, gstin, mobile, email FROM busy_company WHERE company_code = ?', (company,))
        comp_row = c.fetchone()
        comp = dict(comp_row) if comp_row else {'name': 'Company', 'print_name': 'Company', 'gstin': '', 'mobile': '', 'email': ''}

        if not date_str:
            c.execute('SELECT MAX(Date) FROM busy_tran1 WHERE company_code = ? AND fy_year = ?', (company, fy))
            date_str = c.fetchone()[0] or datetime.now().strftime('%Y-%m-%d')

        c.execute('''
            SELECT t1.VchCode, t1.VchType, t1.VchNo, t1.Date, t1.VchAmt,
                   COALESCE(m.Name, 'General Entry') as PartyName
            FROM busy_tran1 t1
            LEFT JOIN busy_master1 m ON m.company_code = t1.company_code AND m.fy_year = t1.fy_year AND m.Code = t1.MasterCode1
            WHERE t1.company_code = ? AND t1.fy_year = ? AND t1.Date = ? AND t1.Cancelled = 0
            ORDER BY t1.VchCode
        ''', (company, fy, date_str))

        vouchers = []
        total_amt = 0.0
        for r in c.fetchall():
            d = dict(r)
            d['VchTypeName'] = VCH_MAP.get(d['VchType'], f"Type {d['VchType']}")
            total_amt += d['VchAmt']
            vouchers.append(d)

        tbl_rows = ""
        for i, v in enumerate(vouchers, 1):
            tbl_rows += f"""
            <tr style="border-bottom: 1px solid #e2e8f0; font-size: 11px;">
                <td style="padding: 6px 8px; text-align: center; color: #64748b;">{i}</td>
                <td style="padding: 6px 8px; font-family: monospace; font-weight: bold; color: #0284c7;">{v['VchNo'].strip()}</td>
                <td style="padding: 6px 8px; font-weight: 600; color: #002b49;">{v['VchTypeName']}</td>
                <td style="padding: 6px 8px; font-weight: 600; color: #1e293b;">{v['PartyName']}</td>
                <td style="padding: 6px 8px; text-align: right; font-weight: bold; color: #0f172a;">₹{v['VchAmt']:,.2f}</td>
            </tr>
            """

        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Day Book ({date_str}) - {comp['print_name'] or comp['name']}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 20px; color: #0f172a; background: #f8fafc; }}
        .paper {{ max-width: 900px; margin: 0 auto; background: white; padding: 30px; box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1); border-radius: 8px; }}
        .header {{ border-bottom: 2px solid #002b49; padding-bottom: 12px; margin-bottom: 15px; display: flex; justify-content: space-between; align-items: flex-start; }}
        .header h1 {{ margin: 0; font-size: 20px; color: #002b49; text-transform: uppercase; font-weight: 800; }}
        .header p {{ margin: 2px 0; font-size: 11px; color: #64748b; }}
        .badge {{ background: #002b49; color: white; padding: 4px 10px; border-radius: 4px; font-size: 11px; font-weight: bold; text-transform: uppercase; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
        th {{ background: #f1f5f9; color: #334155; font-size: 11px; text-transform: uppercase; padding: 8px; border-top: 1px solid #cbd5e1; border-bottom: 2px solid #cbd5e1; text-align: left; }}
        .actions {{ position: sticky; top: 10px; max-width: 900px; margin: 0 auto 15px auto; display: flex; justify-content: flex-end; gap: 10px; }}
        .btn {{ background: #002b49; color: white; border: none; padding: 8px 16px; border-radius: 6px; font-size: 12px; font-weight: 600; cursor: pointer; text-decoration: none; }}
        @media print {{ body {{ background: white; padding: 0; }} .paper {{ box-shadow: none; padding: 0; max-width: 100%; }} .actions {{ display: none !important; }} }}
    </style>
</head>
<body>
    <div class="actions">
        <button class="btn" onclick="window.print()">🖨️ Print / Save PDF</button>
        <a class="btn" style="background:#16a34a;" href="https://api.whatsapp.com/send?text={comp['print_name'] or comp['name']}%20Day%20Book%20({date_str}):%20Total%20Vouchers:%20{len(vouchers)},%20Turnover:%20Rs.{total_amt:,.2f}" target="_blank">📱 WhatsApp</a>
    </div>
    <div class="paper">
        <div class="header">
            <div>
                <h1>{comp['print_name'] or comp['name']}</h1>
                <p>GSTIN: {comp['gstin'] or 'N/A'} | Mobile: {comp['mobile'] or 'N/A'}</p>
                <p style="font-weight: 600; color: #0284c7; margin-top: 4px;">DAY BOOK STATEMENT</p>
            </div>
            <div style="text-align: right;">
                <span class="badge">Date: {date_str}</span>
                <p style="margin-top: 8px;">Total Vouchers: <b>{len(vouchers)}</b></p>
            </div>
        </div>

        <table>
            <thead>
                <tr>
                    <th style="width: 30px; text-align: center;">#</th>
                    <th style="width: 120px;">Vch No</th>
                    <th style="width: 120px;">Type</th>
                    <th>Particulars / Account</th>
                    <th style="width: 130px; text-align: right;">Amount (₹)</th>
                </tr>
            </thead>
            <tbody>
                {tbl_rows}
            </tbody>
            <tfoot>
                <tr style="background: #f8fafc; font-weight: bold; border-top: 2px solid #002b49; border-bottom: 2px solid #002b49;">
                    <td colspan="4" style="padding: 8px; text-align: right; text-transform: uppercase;">Day Total Turnover:</td>
                    <td style="padding: 8px; text-align: right; color: #0f172a; font-size: 14px;">₹{total_amt:,.2f}</td>
                </tr>
            </tfoot>
        </table>
    </div>
</body>
</html>"""

        conn.close()
        self.send_html(html)

    def handle_stock_pdf(self, company, fy, search='', mc_code=0):
        conn = self.get_db()
        c = conn.cursor()

        # Company Info
        c.execute('SELECT name, print_name, gstin, mobile, email FROM busy_company WHERE company_code = ?', (company,))
        comp_row = c.fetchone()
        comp = dict(comp_row) if comp_row else {'name': 'Company', 'print_name': 'Company', 'gstin': '', 'mobile': '', 'email': ''}

        search_filter = f"%{search}%"
        stock = compute_item_stock(c, company, fy, mc_code)
        c.execute('''
            SELECT Code, Name, PrintName FROM busy_master1
            WHERE company_code = ? AND fy_year = ? AND MasterType = 6 AND Name LIKE ?
            ORDER BY Name
        ''', (company, fy, search_filter))

        items = []
        total_val = 0.0
        total_qty = 0.0
        for r in c.fetchall():
            row = dict(r)
            s = stock.get(row['Code'], EMPTY_STOCK)
            if s['OpQty'] == 0 and s['MovementQty'] == 0:
                continue
            row.update(s)
            total_qty += row['ClosingQty']
            total_val += row['ClosingValue']
            items.append(row)

        tbl_rows = ""
        for i, it in enumerate(items, 1):
            tbl_rows += f"""
            <tr style="border-bottom: 1px solid #e2e8f0; font-size: 11px;">
                <td style="padding: 6px 8px; text-align: center; color: #64748b;">{i}</td>
                <td style="padding: 6px 8px; font-weight: 600; color: #1e293b;">{it['Name']}</td>
                <td style="padding: 6px 8px; text-align: right; color: #475569;">{it['OpQty']:,.2f}</td>
                <td style="padding: 6px 8px; text-align: right; color: {'#059669' if it['MovementQty'] >= 0 else '#dc2626'};">{it['MovementQty']:+,.2f}</td>
                <td style="padding: 6px 8px; text-align: right; font-weight: bold; color: #0f172a;">{it['ClosingQty']:,.2f}</td>
                <td style="padding: 6px 8px; text-align: right; color: #475569;">₹{it['AvgRate']:,.2f}</td>
                <td style="padding: 6px 8px; text-align: right; font-weight: bold; color: #0284c7;">₹{it['ClosingValue']:,.2f}</td>
            </tr>
            """

        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Stock Status - {comp['print_name'] or comp['name']}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 20px; color: #0f172a; background: #f8fafc; }}
        .paper {{ max-width: 900px; margin: 0 auto; background: white; padding: 30px; box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1); border-radius: 8px; }}
        .header {{ border-bottom: 2px solid #002b49; padding-bottom: 12px; margin-bottom: 15px; display: flex; justify-content: space-between; align-items: flex-start; }}
        .header h1 {{ margin: 0; font-size: 20px; color: #002b49; text-transform: uppercase; font-weight: 800; }}
        .header p {{ margin: 2px 0; font-size: 11px; color: #64748b; }}
        .badge {{ background: #002b49; color: white; padding: 4px 10px; border-radius: 4px; font-size: 11px; font-weight: bold; text-transform: uppercase; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
        th {{ background: #f1f5f9; color: #334155; font-size: 11px; text-transform: uppercase; padding: 8px; border-top: 1px solid #cbd5e1; border-bottom: 2px solid #cbd5e1; text-align: left; }}
        .actions {{ position: sticky; top: 10px; max-width: 900px; margin: 0 auto 15px auto; display: flex; justify-content: flex-end; gap: 10px; }}
        .btn {{ background: #002b49; color: white; border: none; padding: 8px 16px; border-radius: 6px; font-size: 12px; font-weight: 600; cursor: pointer; text-decoration: none; }}
        @media print {{ body {{ background: white; padding: 0; }} .paper {{ box-shadow: none; padding: 0; max-width: 100%; }} .actions {{ display: none !important; }} }}
    </style>
</head>
<body>
    <div class="actions">
        <button class="btn" onclick="window.print()">🖨️ Print / Save PDF</button>
        <a class="btn" style="background:#16a34a;" href="https://api.whatsapp.com/send?text={comp['print_name'] or comp['name']}%20Stock%20Status:%20Total%20Items:%20{len(items)},%20Valuation:%20Rs.{total_val:,.2f}" target="_blank">📱 WhatsApp</a>
    </div>
    <div class="paper">
        <div class="header">
            <div>
                <h1>{comp['print_name'] or comp['name']}</h1>
                <p>GSTIN: {comp['gstin'] or 'N/A'} | Mobile: {comp['mobile'] or 'N/A'}</p>
                <p style="font-weight: 600; color: #0284c7; margin-top: 4px;">CLOSING STOCK STATUS REPORT</p>
            </div>
            <div style="text-align: right;">
                <span class="badge">FY {fy_label(fy)}</span>
                <p style="margin-top: 8px;">Total Items: <b>{len(items)}</b></p>
            </div>
        </div>

        <table>
            <thead>
                <tr>
                    <th style="width: 30px; text-align: center;">#</th>
                    <th>Item Description</th>
                    <th style="width: 80px; text-align: right;">Op Qty</th>
                    <th style="width: 80px; text-align: right;">Movement</th>
                    <th style="width: 80px; text-align: right;">Closing Qty</th>
                    <th style="width: 80px; text-align: right;">Rate (₹)</th>
                    <th style="width: 100px; text-align: right;">Valuation (₹)</th>
                </tr>
            </thead>
            <tbody>
                {tbl_rows}
            </tbody>
            <tfoot>
                <tr style="background: #f8fafc; font-weight: bold; border-top: 2px solid #002b49; border-bottom: 2px solid #002b49;">
                    <td colspan="4" style="padding: 8px; text-align: right; text-transform: uppercase;">Total Valuation:</td>
                    <td style="padding: 8px; text-align: right; color: #0f172a;">{total_qty:,.2f}</td>
                    <td></td>
                    <td style="padding: 8px; text-align: right; color: #0284c7; font-size: 13px;">₹{total_val:,.2f}</td>
                </tr>
            </tfoot>
        </table>
    </div>
</body>
</html>"""

        conn.close()
        self.send_html(html)

    def handle_trialbalance_pdf(self, company, fy):
        conn = self.get_db()
        c = conn.cursor()

        # Company Info
        c.execute('SELECT name, print_name, gstin, mobile, email FROM busy_company WHERE company_code = ?', (company,))
        comp_row = c.fetchone()
        comp = dict(comp_row) if comp_row else {'name': 'Company', 'print_name': 'Company', 'gstin': '', 'mobile': '', 'email': ''}

        c.execute('''
            SELECT g.Code, g.Name as GroupName,
                   SUM(CASE WHEN (m.OpBal - COALESCE(s.TxnSum, 0)) > 0 THEN (m.OpBal - COALESCE(s.TxnSum, 0)) ELSE 0 END) as TotalDebit,
                   SUM(CASE WHEN (m.OpBal - COALESCE(s.TxnSum, 0)) < 0 THEN ABS(m.OpBal - COALESCE(s.TxnSum, 0)) ELSE 0 END) as TotalCredit
            FROM busy_master1 g
            JOIN busy_master1 m ON m.company_code = g.company_code AND m.fy_year = g.fy_year AND m.ParentGrp = g.Code AND m.MasterType = 2
            LEFT JOIN (
                SELECT MasterCode1, SUM(Value1) as TxnSum
                FROM busy_tran2
                WHERE company_code = ? AND fy_year = ? AND RecType = 1
                GROUP BY MasterCode1
            ) s ON s.MasterCode1 = m.Code
            WHERE g.company_code = ? AND g.fy_year = ? AND g.MasterType = 1
            GROUP BY g.Code, g.Name
            HAVING (TotalDebit + TotalCredit) > 0
            ORDER BY g.Name
        ''', (company, fy, company, fy))

        groups = []
        grand_dr = 0.0
        grand_cr = 0.0

        for r in c.fetchall():
            d = dict(r)
            net = d['TotalDebit'] - d['TotalCredit']
            d['NetBal'] = abs(net)
            d['BalType'] = 'Dr' if net >= 0 else 'Cr'
            if net >= 0:
                grand_dr += net
            else:
                grand_cr += -net
            groups.append(d)

        tbl_rows = ""
        for i, g in enumerate(groups, 1):
            tbl_rows += f"""
            <tr style="border-bottom: 1px solid #e2e8f0; font-size: 11px;">
                <td style="padding: 6px 8px; text-align: center; color: #64748b;">{i}</td>
                <td style="padding: 6px 8px; font-weight: 600; color: #1e293b;">{g['GroupName']}</td>
                <td style="padding: 6px 8px; text-align: right; font-weight: bold; color: {'#0f172a' if g['BalType'] == 'Dr' else '#94a3b8'};">{'₹' + f"{g['NetBal']:,.2f}" if g['BalType'] == 'Dr' else '-'}</td>
                <td style="padding: 6px 8px; text-align: right; font-weight: bold; color: {'#dc2626' if g['BalType'] == 'Cr' else '#94a3b8'};">{'₹' + f"{g['NetBal']:,.2f}" if g['BalType'] == 'Cr' else '-'}</td>
            </tr>
            """

        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Trial Balance - {comp['print_name'] or comp['name']}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 20px; color: #0f172a; background: #f8fafc; }}
        .paper {{ max-width: 900px; margin: 0 auto; background: white; padding: 30px; box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1); border-radius: 8px; }}
        .header {{ border-bottom: 2px solid #002b49; padding-bottom: 12px; margin-bottom: 15px; display: flex; justify-content: space-between; align-items: flex-start; }}
        .header h1 {{ margin: 0; font-size: 20px; color: #002b49; text-transform: uppercase; font-weight: 800; }}
        .header p {{ margin: 2px 0; font-size: 11px; color: #64748b; }}
        .badge {{ background: #002b49; color: white; padding: 4px 10px; border-radius: 4px; font-size: 11px; font-weight: bold; text-transform: uppercase; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
        th {{ background: #f1f5f9; color: #334155; font-size: 11px; text-transform: uppercase; padding: 8px; border-top: 1px solid #cbd5e1; border-bottom: 2px solid #cbd5e1; text-align: left; }}
        .actions {{ position: sticky; top: 10px; max-width: 900px; margin: 0 auto 15px auto; display: flex; justify-content: flex-end; gap: 10px; }}
        .btn {{ background: #002b49; color: white; border: none; padding: 8px 16px; border-radius: 6px; font-size: 12px; font-weight: 600; cursor: pointer; text-decoration: none; }}
        @media print {{ body {{ background: white; padding: 0; }} .paper {{ box-shadow: none; padding: 0; max-width: 100%; }} .actions {{ display: none !important; }} }}
    </style>
</head>
<body>
    <div class="actions">
        <button class="btn" onclick="window.print()">🖨️ Print / Save PDF</button>
        <a class="btn" style="background:#16a34a;" href="https://api.whatsapp.com/send?text={comp['print_name'] or comp['name']}%20Trial%20Balance:%20Total%20Debit:%20Rs.{grand_dr:,.2f},%20Total%20Credit:%20Rs.{grand_cr:,.2f}" target="_blank">📱 WhatsApp</a>
    </div>
    <div class="paper">
        <div class="header">
            <div>
                <h1>{comp['print_name'] or comp['name']}</h1>
                <p>GSTIN: {comp['gstin'] or 'N/A'} | Mobile: {comp['mobile'] or 'N/A'}</p>
                <p style="font-weight: 600; color: #0284c7; margin-top: 4px;">TRIAL BALANCE SUMMARY REPORT</p>
            </div>
            <div style="text-align: right;">
                <span class="badge">FY {fy_label(fy)}</span>
                <p style="margin-top: 8px;">Date: {datetime.now().strftime('%d-%m-%Y')}</p>
            </div>
        </div>

        <table>
            <thead>
                <tr>
                    <th style="width: 30px; text-align: center;">#</th>
                    <th>Account Group Description</th>
                    <th style="width: 140px; text-align: right;">Debit Balance (₹)</th>
                    <th style="width: 140px; text-align: right;">Credit Balance (₹)</th>
                </tr>
            </thead>
            <tbody>
                {tbl_rows}
            </tbody>
            <tfoot>
                <tr style="background: #f8fafc; font-weight: bold; border-top: 2px solid #002b49; border-bottom: 2px solid #002b49;">
                    <td colspan="2" style="padding: 8px; text-align: right; text-transform: uppercase;">Total Trial Balance:</td>
                    <td style="padding: 8px; text-align: right; color: #0f172a; font-size: 13px;">₹{grand_dr:,.2f}</td>
                    <td style="padding: 8px; text-align: right; color: #dc2626; font-size: 13px;">₹{grand_cr:,.2f}</td>
                </tr>
            </tfoot>
        </table>
    </div>
</body>
</html>"""

        conn.close()
        self.send_html(html)

    def handle_voucher_details(self, company, fy, vch_code):
        conn = self.get_db()
        c = conn.cursor()

        # Header
        c.execute('''
            SELECT t1.VchCode, t1.Date, t1.VchType, t1.VchSeriesCode, t1.VchNo, t1.VchAmt, t1.MasterCode1, t1.MasterCode2,
                   CASE 
                       WHEN t1.VchType = 5 THEN COALESCE(m2.Name, m.Name, 'General Entry')
                       ELSE COALESCE(m.Name, 'General Entry')
                   END as PartyName,
                   CASE 
                       WHEN SUBSTR(s.Name, 1, 2) GLOB '[0-9][0-9]' THEN SUBSTR(s.Name, 3)
                       ELSE COALESCE(s.Name, '')
                   END as SeriesName
            FROM busy_tran1 t1
            LEFT JOIN busy_master1 m ON m.company_code = t1.company_code AND m.fy_year = t1.fy_year AND m.Code = t1.MasterCode1
            LEFT JOIN busy_master1 m2 ON m2.company_code = t1.company_code AND m2.fy_year = t1.fy_year AND m2.Code = t1.MasterCode2
            LEFT JOIN busy_master1 s ON s.company_code = t1.company_code AND s.fy_year = t1.fy_year AND s.Code = t1.VchSeriesCode AND s.MasterType = 21
            WHERE t1.company_code = ? AND t1.fy_year = ? AND t1.VchCode = ?
        ''', (company, fy, vch_code))
        h_row = c.fetchone()
        header = dict(h_row) if h_row else {}
        vt = header.get('VchType', 0)
        s_name = header.get('SeriesName', '') or ''
        if 'FG-' in s_name or 'FG ' in s_name or s_name.startswith('FG') or vt == 20:
            header['VchTypeName'] = "Production"
        elif vt == 5:
            header['VchTypeName'] = "Material Issue"
        elif vt == 6:
            header['VchTypeName'] = "Material Receipt"
        elif vt == 8:
            header['VchTypeName'] = "Stock Transfer"
        elif vt == 2:
            header['VchTypeName'] = "Purchase"
        elif vt == 11:
            header['VchTypeName'] = "Delivery Challan"
        elif vt == 12:
            header['VchTypeName'] = "Sales Order"
        elif vt == 13:
            header['VchTypeName'] = "Purchase Order"
        elif vt == 14:
            header['VchTypeName'] = "Receipt"
        elif vt in (1, 19):
            header['VchTypeName'] = "Payment"
        elif vt == 15:
            header['VchTypeName'] = "Contra"
        elif vt == 16:
            header['VchTypeName'] = "Journal"
        elif vt in (3, 18):
            header['VchTypeName'] = "Credit Note"
        elif vt == 4:
            header['VchTypeName'] = "Purchase Challan"
        elif vt in (10, 17):
            header['VchTypeName'] = "Debit Note"
        elif vt in (26, 27):
            header['VchTypeName'] = "Quotation"
        else:
            header['VchTypeName'] = VCH_MAP.get(vt, f"Type {vt}")
        header['AmountInWords'] = amount_to_words(header.get('VchAmt', 0))

        # Party Address & GST
        c.execute('''
            SELECT Address1, Address2, GSTNo, Mobile, Email
            FROM busy_masteraddressinfo
            WHERE company_code = ? AND fy_year = ? AND MasterCode = ?
        ''', (company, fy, header.get('MasterCode1', 0)))
        addr_row = c.fetchone()
        party_info = dict(addr_row) if addr_row else {}

        # Check BillingDet for overrides
        c.execute('''
            SELECT PartyName, Address1, Address2, Address3, Address4, MobileNo, Email, GSTNo
            FROM busy_billingdet
            WHERE company_code = ? AND fy_year = ? AND VchCode = ?
        ''', (company, fy, vch_code))
        bdet_row = c.fetchone()
        if bdet_row:
            bdet = dict(bdet_row)
            if bdet.get('PartyName'):
                header['PartyName'] = bdet['PartyName']
            if bdet.get('Address1'):
                party_info['Address1'] = bdet['Address1']
            if bdet.get('Address2'):
                party_info['Address2'] = bdet['Address2']
            if bdet.get('GSTNo'):
                party_info['GSTNo'] = bdet['GSTNo']
            if bdet.get('MobileNo'):
                party_info['Mobile'] = bdet['MobileNo']

        # Transport & Shipping Info
        c.execute('''
            SELECT Transport, Station, GrNo, VehicleNo, TotalQty, GrDate
            FROM busy_vchotherinfo
            WHERE company_code = ? AND fy_year = ? AND VchCode = ?
        ''', (company, fy, vch_code))
        t_row = c.fetchone()
        transport_info = dict(t_row) if t_row else {}

        # Company Info
        c.execute('''
            SELECT name, print_name, gstin, mobile, email, bdep_id
            FROM busy_company
            WHERE company_code = ?
        ''', (company,))
        comp_row = c.fetchone()
        company_info = dict(comp_row) if comp_row else {}

        # Items (RecType in (2, 4, 7) - includes Orders and Challan Billed Items)
        c.execute(VOUCHER_ITEMS_SQL, (company, fy, vch_code))
        raw_items = [dict(r) for r in c.fetchall()]
        has_pos = any(r.get('RawQty', 0) > 0 for r in raw_items)
        has_neg = any(r.get('RawQty', 0) < 0 for r in raw_items)
        if has_pos and has_neg:
            items = [r for r in raw_items if r.get('RawQty', 0) > 0]
        else:
            items = raw_items

        # Linked vouchers (bidirectional Challan <-> Sales Invoice)
        linked_sales = []
        linked_challans = []
        if header.get('VchType') == 11:
            c.execute('''
                SELECT t1.VchCode, t1.VchNo, t1.Date, t1.VchAmt
                FROM busy_tran3 t3_adj
                JOIN busy_tran1 t1 ON t1.VchCode = t3_adj.VchCode AND t1.company_code = t3_adj.company_code AND t1.fy_year = t3_adj.fy_year
                WHERE t3_adj.company_code = ? AND t3_adj.fy_year = ?
                  AND t3_adj.RefCode IN (SELECT RefCode FROM busy_tran3
                                         WHERE company_code = ? AND fy_year = ? AND VchCode = ? AND Method = 1 AND RecType = 3)
                  AND t3_adj.Method = 2 AND t1.VchType = 9
            ''', (company, fy, company, fy, vch_code))
            linked_sales = [dict(r) for r in c.fetchall()]
        elif header.get('VchType') == 9:
            c.execute('''
                SELECT DISTINCT t3.BillNo, t1.VchCode, t1.Date
                FROM busy_tran3 t3
                LEFT JOIN busy_tran3 t3_orig ON t3_orig.RefCode = t3.RefCode AND t3_orig.Method = 1 AND t3_orig.RecType = 3
                                            AND t3_orig.company_code = t3.company_code AND t3_orig.fy_year = t3.fy_year
                LEFT JOIN busy_tran1 t1 ON t1.VchCode = t3_orig.VchCode AND t1.company_code = t3.company_code AND t1.fy_year = t3.fy_year
                WHERE t3.company_code = ? AND t3.fy_year = ? AND t3.VchCode = ? AND t3.Method = 2 AND t3.RecType = 3
            ''', (company, fy, vch_code))
            linked_challans = [dict(r) for r in c.fetchall()]

        # Accounting entries (RecType = 1)
        c.execute('''
            SELECT m.Name as AccountName, t2.Value1 as Amount
            FROM busy_tran2 t2
            LEFT JOIN busy_master1 m ON m.company_code = t2.company_code AND m.fy_year = t2.fy_year AND m.Code = t2.MasterCode1
            WHERE t2.company_code = ? AND t2.fy_year = ? AND t2.VchCode = ? AND t2.RecType = 1
            ORDER BY t2.SrNo
        ''', (company, fy, vch_code))
        accounts = [dict(r) for r in c.fetchall()]

        # Sundries (RecType = 3), signed: negative = subtractive (discounts, Rounded Off (-))
        c.execute(VOUCHER_SUNDRIES_SQL, (company, fy, vch_code))
        sundries = [dict(r) for r in c.fetchall() if abs(r['Amount'] or 0) > 0.001]

        # Resolve From MC and To MC for Stock Transfer / Stock Journal / Material Issue
        from_mc = ""
        to_mc = ""
        try:
            c.execute('''
                SELECT t2.SrNo, t2.Value1 as Qty, t2.MasterCode2, m.Name as MC_Name
                FROM busy_tran2 t2
                LEFT JOIN busy_master1 m ON m.company_code = t2.company_code AND m.fy_year = t2.fy_year AND m.Code = t2.MasterCode2
                WHERE t2.company_code = ? AND t2.fy_year = ? AND t2.VchCode = ? AND t2.RecType IN (2, 7)
                ORDER BY t2.SrNo
            ''', (company, fy, vch_code))
            t2_mc_rows = [dict(r) for r in c.fetchall()]

            from_mcs = []
            to_mcs = []
            for r in t2_mc_rows:
                q = r.get('Qty', 0)
                mc_name = r.get('MC_Name', '').strip() if r.get('MC_Name') else ''
                if mc_name:
                    if q < 0 and mc_name not in from_mcs:
                        from_mcs.append(mc_name)
                    elif q > 0 and mc_name not in to_mcs:
                        to_mcs.append(mc_name)

            from_mc = ", ".join(from_mcs)
            to_mc = ", ".join(to_mcs)

            header_m1 = header.get('PartyName', '').strip() if header.get('PartyName') else ''
            c.execute('''
                SELECT m.Name FROM busy_tran1 t1
                LEFT JOIN busy_master1 m ON m.company_code = t1.company_code AND m.fy_year = t1.fy_year AND m.Code = t1.MasterCode2
                WHERE t1.company_code = ? AND t1.fy_year = ? AND t1.VchCode = ?
            ''', (company, fy, vch_code))
            m2_row = c.fetchone()
            header_m2 = m2_row['Name'].strip() if m2_row and m2_row['Name'] else ""

            c.execute('''
                SELECT m.Name FROM busy_tran1 t1
                LEFT JOIN busy_master1 m ON m.company_code = t1.company_code AND m.fy_year = t1.fy_year AND m.Code = t1.VchSeriesCode
                WHERE t1.company_code = ? AND t1.fy_year = ? AND t1.VchCode = ?
            ''', (company, fy, vch_code))
            s_row = c.fetchone()
            series_name = s_row['Name'].strip() if s_row and s_row['Name'] else ""
            header['SeriesName'] = series_name

            if not to_mc:
                to_mc = header_m1 or header_m2
            if not from_mc:
                if header_m1 and header_m1 != to_mc:
                    from_mc = header_m1
                elif header_m2 and header_m2 != to_mc:
                    from_mc = header_m2
                else:
                    from_mc = "MAIN STORE"

            if from_mc == to_mc and header_m1 and header_m1 != from_mc:
                to_mc = header_m1

            header['from_mc'] = from_mc
            header['to_mc'] = to_mc
        except Exception:
            header['from_mc'] = "MAIN STORE"
            header['to_mc'] = header.get('PartyName', '')

        conn.close()
        sec_token = generate_secure_token('inv', company, fy, vch_code)
        self.send_json({
            "header": header,
            "party": party_info,
            "transport": transport_info,
            "company": company_info,
            "items": items,
            "accounts": accounts,
            "sundries": sundries,
            "linked_sales": linked_sales,
            "linked_challans": linked_challans,
            "secure_token": sec_token,
            "secure_pdf_url": f"/api/voucher/invoice-pdf?token={sec_token}"
        })

    def handle_invoice_pdf(self, company, fy, vch_code):
        conn = self.get_db()
        c = conn.cursor()

        # Fetch voucher header details
        c.execute('''
            SELECT t1.VchCode, t1.Date, t1.VchType, t1.VchSeriesCode, t1.VchNo, t1.VchAmt, t1.MasterCode1, t1.MasterCode2,
                   CASE 
                       WHEN t1.VchType = 5 THEN COALESCE(m2.Name, m.Name, 'General Entry')
                       ELSE COALESCE(m.Name, 'General Entry')
                   END as PartyName,
                   CASE 
                       WHEN SUBSTR(s.Name, 1, 2) GLOB '[0-9][0-9]' THEN SUBSTR(s.Name, 3)
                       ELSE COALESCE(s.Name, '')
                   END as SeriesName
            FROM busy_tran1 t1
            LEFT JOIN busy_master1 m ON m.company_code = t1.company_code AND m.fy_year = t1.fy_year AND m.Code = t1.MasterCode1
            LEFT JOIN busy_master1 m2 ON m2.company_code = t1.company_code AND m2.fy_year = t1.fy_year AND m2.Code = t1.MasterCode2
            LEFT JOIN busy_master1 s ON s.company_code = t1.company_code AND s.fy_year = t1.fy_year AND s.Code = t1.VchSeriesCode AND s.MasterType = 21
            WHERE t1.company_code = ? AND t1.fy_year = ? AND t1.VchCode = ?
        ''', (company, fy, vch_code))
        h_row = c.fetchone()
        header = dict(h_row) if h_row else {}
        s_name = header.get('SeriesName', '') or ''
        if 'FG-' in s_name or 'FG ' in s_name or s_name.startswith('FG') or header.get('VchType', 0) == 20:
            header['VchTypeName'] = "Production"
        elif header.get('VchType', 0) in (5, 8):
            header['VchTypeName'] = "Stock Transfer"
        else:
            header['VchTypeName'] = VCH_MAP.get(header.get('VchType', 0), "Tax Invoice")
        header['AmountInWords'] = amount_to_words(header.get('VchAmt', 0))

        # Fetch Party Address info
        c.execute('''
            SELECT Address1, Address2, GSTNo, Mobile, Email
            FROM busy_masteraddressinfo
            WHERE company_code = ? AND fy_year = ? AND MasterCode = ?
        ''', (company, fy, header.get('MasterCode1', 0)))
        party = dict(c.fetchone() or {})

        # Fetch Billing details override
        c.execute('''
            SELECT PartyName, Address1, Address2, Address3, Address4, MobileNo, Email, GSTNo
            FROM busy_billingdet
            WHERE company_code = ? AND fy_year = ? AND VchCode = ?
        ''', (company, fy, vch_code))
        bdet = dict(c.fetchone() or {})
        if bdet.get('PartyName'): header['PartyName'] = bdet['PartyName']
        if bdet.get('Address1'): party['Address1'] = bdet['Address1']
        if bdet.get('Address2'): party['Address2'] = bdet['Address2']
        if bdet.get('GSTNo'): party['GSTNo'] = bdet['GSTNo']

        # Fetch Transport info
        c.execute('''
            SELECT Transport, Station, GrNo, VehicleNo, TotalQty, GrDate
            FROM busy_vchotherinfo
            WHERE company_code = ? AND fy_year = ? AND VchCode = ?
        ''', (company, fy, vch_code))
        transport = dict(c.fetchone() or {})

        # Fetch Company Profile
        c.execute('''
            SELECT name, print_name, gstin, mobile, email, bdep_id
            FROM busy_company
            WHERE company_code = ?
        ''', (company,))
        comp = dict(c.fetchone() or {})

        # Fetch Busy Decoded Print Config from BDS (Standard Format)
        c.execute('''
            SELECT * FROM busy_print_config WHERE company_code = ?
        ''', (company,))
        p_row = c.fetchone()
        p_cfg = dict(p_row) if p_row else {}

        # Fetch Items with Unit and HSN Code (RecType in (2, 4, 7) - includes Orders and Challans)
        c.execute(VOUCHER_ITEMS_SQL, (company, fy, vch_code))
        raw_items = [dict(r) for r in c.fetchall()]
        has_pos = any(r.get('RawQty', 0) > 0 for r in raw_items)
        has_neg = any(r.get('RawQty', 0) < 0 for r in raw_items)
        if has_pos and has_neg:
            items = [r for r in raw_items if r.get('RawQty', 0) > 0]
        else:
            items = raw_items

        # Fetch Batch / Bale breakdown from busy_tran3
        c.execute('''
            SELECT MasterCode1, BillNo as BatchNo, abs(Value1) as BatchQty, Narration as BatchDesc
            FROM busy_tran3
            WHERE company_code = ? AND fy_year = ? AND VchCode = ? AND RecType = 2
            ORDER BY SrNo
        ''', (company, fy, vch_code))
        batch_rows = c.fetchall()
        item_batches = {}
        for b in batch_rows:
            mcode = b['MasterCode1']
            if mcode not in item_batches:
                item_batches[mcode] = []
            item_batches[mcode].append(dict(b))

        # Linked vouchers (bidirectional Challan <-> Sales Invoice)
        linked_sales = []
        linked_challans = []
        if header.get('VchType') == 11:
            c.execute('''
                SELECT t1.VchCode, t1.VchNo, t1.Date, t1.VchAmt
                FROM busy_tran3 t3_adj
                JOIN busy_tran1 t1 ON t1.VchCode = t3_adj.VchCode AND t1.company_code = t3_adj.company_code AND t1.fy_year = t3_adj.fy_year
                WHERE t3_adj.company_code = ? AND t3_adj.fy_year = ?
                  AND t3_adj.RefCode IN (SELECT RefCode FROM busy_tran3
                                         WHERE company_code = ? AND fy_year = ? AND VchCode = ? AND Method = 1 AND RecType = 3)
                  AND t3_adj.Method = 2 AND t1.VchType = 9
            ''', (company, fy, company, fy, vch_code))
            linked_sales = [dict(r) for r in c.fetchall()]
        elif header.get('VchType') == 9:
            c.execute('''
                SELECT DISTINCT t3.BillNo, t1.VchCode, t1.Date
                FROM busy_tran3 t3
                LEFT JOIN busy_tran3 t3_orig ON t3_orig.RefCode = t3.RefCode AND t3_orig.Method = 1 AND t3_orig.RecType = 3 AND t3_orig.company_code = t3.company_code
                LEFT JOIN busy_tran1 t1 ON t1.VchCode = t3_orig.VchCode AND t1.company_code = t3_orig.company_code
                WHERE t3.company_code = ? AND t3.VchCode = ? AND t3.Method = 2 AND t3.RecType = 3
            ''', (company, vch_code))
            linked_challans = [dict(r) for r in c.fetchall()]

        # Accounting entries fallback (if no items)
        c.execute('''
            SELECT m.Name as AccountName, abs(t2.Value1) as Amount
            FROM busy_tran2 t2
            LEFT JOIN busy_master1 m ON m.company_code = t2.company_code AND m.fy_year = t2.fy_year AND m.Code = t2.MasterCode1
            WHERE t2.company_code = ? AND t2.fy_year = ? AND t2.VchCode = ? AND t2.RecType = 1
            ORDER BY t2.SrNo
        ''', (company, fy, vch_code))
        accounts = [dict(r) for r in c.fetchall()]

        # Sundry entries with real rupee amounts (Value3), signed by the sundry's nature
        c.execute(VOUCHER_SUNDRIES_SQL, (company, fy, vch_code))
        sundries = [dict(r) for r in c.fetchall()]

        # Signed accounting entries (preserving debit/credit sign for voucher display)
        c.execute('''
            SELECT m.Name as AccountName, t2.Value1 as Amount, t2.MasterCode1
            FROM busy_tran2 t2
            LEFT JOIN busy_master1 m ON m.company_code = t2.company_code AND m.fy_year = t2.fy_year AND m.Code = t2.MasterCode1
            WHERE t2.company_code = ? AND t2.fy_year = ? AND t2.VchCode = ? AND t2.RecType = 1
            ORDER BY t2.SrNo
        ''', (company, fy, vch_code))
        signed_accounts = [dict(r) for r in c.fetchall()]

        # Bill-by-bill adjustments from tran3 (for receipt/payment vouchers)
        # Method 1 = new reference (advance / on account), 2 = against an existing bill.
        # The bill's own date comes from its Method 1 row (VchCode 0 for opening bills).
        c.execute('''
            SELECT t3.BillNo, t3.Method,
                   CASE WHEN t3.Method = 2 THEN COALESCE(o.BillDate, t3.BillDate) ELSE t3.BillDate END as BillDate,
                   CASE WHEN t3.Method = 2 THEN COALESCE(o.DueDate, t3.DueDate) ELSE t3.DueDate END as DueDate,
                   abs(t3.Value1) as AdjAmount,
                   CASE t3.Method WHEN 2 THEN 'Agst Ref' WHEN 1 THEN 'New Ref' ELSE 'On Account' END as RefType,
                   t3.Narration
            FROM busy_tran3 t3
            LEFT JOIN busy_tran3 o ON o.company_code = t3.company_code AND o.fy_year = t3.fy_year
                                  AND o.RefCode = t3.RefCode AND o.RecType = 1 AND o.Method = 1
                                  AND o.MasterCode1 = t3.MasterCode1
            WHERE t3.company_code = ? AND t3.fy_year = ? AND t3.VchCode = ? AND t3.RecType = 1
            GROUP BY t3.id
            ORDER BY t3.SrNo
        ''', (company, fy, vch_code))
        bill_adjustments = [dict(r) for r in c.fetchall()]

        # Resolve From MC and To MC for Stock Transfer / Stock Journal / Material Issue
        from_mc = ""
        to_mc = ""
        try:
            c.execute('''
                SELECT t2.SrNo, t2.Value1 as Qty, t2.MasterCode2, m.Name as MC_Name
                FROM busy_tran2 t2
                LEFT JOIN busy_master1 m ON m.company_code = t2.company_code AND m.fy_year = t2.fy_year AND m.Code = t2.MasterCode2
                WHERE t2.company_code = ? AND t2.fy_year = ? AND t2.VchCode = ? AND t2.RecType IN (2, 7)
                ORDER BY t2.SrNo
            ''', (company, fy, vch_code))
            t2_mc_rows = [dict(r) for r in c.fetchall()]

            from_mcs = []
            to_mcs = []
            for r in t2_mc_rows:
                q = r.get('Qty', 0)
                mc_name = r.get('MC_Name', '').strip() if r.get('MC_Name') else ''
                if mc_name:
                    if q < 0 and mc_name not in from_mcs:
                        from_mcs.append(mc_name)
                    elif q > 0 and mc_name not in to_mcs:
                        to_mcs.append(mc_name)

            from_mc = ", ".join(from_mcs)
            to_mc = ", ".join(to_mcs)

            header_m1 = header.get('PartyName', '').strip() if header.get('PartyName') else ''
            c.execute('''
                SELECT m.Name FROM busy_tran1 t1
                LEFT JOIN busy_master1 m ON m.company_code = t1.company_code AND m.fy_year = t1.fy_year AND m.Code = t1.MasterCode2
                WHERE t1.company_code = ? AND t1.fy_year = ? AND t1.VchCode = ?
            ''', (company, fy, vch_code))
            m2_row = c.fetchone()
            header_m2 = m2_row['Name'].strip() if m2_row and m2_row['Name'] else ""

            c.execute('''
                SELECT m.Name FROM busy_tran1 t1
                LEFT JOIN busy_master1 m ON m.company_code = t1.company_code AND m.fy_year = t1.fy_year AND m.Code = t1.VchSeriesCode
                WHERE t1.company_code = ? AND t1.fy_year = ? AND t1.VchCode = ?
            ''', (company, fy, vch_code))
            s_row = c.fetchone()
            series_name = s_row['Name'].strip() if s_row and s_row['Name'] else ""
            header['SeriesName'] = series_name

            if not to_mc:
                to_mc = header_m1 or header_m2
            if not from_mc:
                if header_m1 and header_m1 != to_mc:
                    from_mc = header_m1
                elif header_m2 and header_m2 != to_mc:
                    from_mc = header_m2
                else:
                    from_mc = "MAIN STORE"

            if from_mc == to_mc and header_m1 and header_m1 != from_mc:
                to_mc = header_m1

            header['from_mc'] = from_mc
            header['to_mc'] = to_mc
        except Exception:
            from_mc = "MAIN STORE"
            to_mc = header.get('PartyName', '')
            header['from_mc'] = from_mc
            header['to_mc'] = to_mc

        conn.close()

        # Dispatch to dedicated voucher print format based on VchType
        vch_type_dispatch = header.get('VchType', 0)
        meta = self._voucher_meta(comp, p_cfg)
        css = self._voucher_common_css()

        if vch_type_dispatch == 14:
            self._render_receipt_html(header, party, comp, p_cfg, signed_accounts, bill_adjustments)
            return
        elif vch_type_dispatch in (1, 19):
            self._render_payment_html(header, party, comp, p_cfg, signed_accounts, bill_adjustments)
            return
        elif vch_type_dispatch in (15, 16):
            self._render_journal_html(header, party, comp, p_cfg, signed_accounts)
            return
        elif vch_type_dispatch == 5:
            html = voucher_renderers.render_material_issue_html(header, party, comp, p_cfg, from_mc, to_mc, items, transport, item_batches, meta, css)
            self.send_html(html)
            return
        elif vch_type_dispatch == 6:
            html = voucher_renderers.render_material_receipt_html(header, party, comp, p_cfg, from_mc, to_mc, items, transport, item_batches, meta, css)
            self.send_html(html)
            return
        elif vch_type_dispatch == 2:
            html = voucher_renderers.render_purchase_html(header, party, comp, p_cfg, items, sundries, transport, item_batches, meta, css)
            self.send_html(html)
            return
        elif vch_type_dispatch == 4:
            html = voucher_renderers.render_purchase_html(header, party, comp, p_cfg, items, sundries, transport, item_batches, meta, css,
                                                          doc_title="PURCHASE CHALLAN")
            self.send_html(html)
            return
        elif vch_type_dispatch == 11:
            html = voucher_renderers.render_delivery_challan_html(header, party, comp, p_cfg, items, transport, item_batches, linked_sales, meta, css)
            self.send_html(html)
            return
        elif vch_type_dispatch in (8, 7, 28):
            self._render_stock_transfer_html(header, comp, p_cfg, from_mc, to_mc, items, transport, item_batches)
            return
        elif vch_type_dispatch in (3, 18):
            html = voucher_renderers.render_note_html(header, party, comp, p_cfg, items, sundries, transport, item_batches, meta, css, is_credit=True)
            self.send_html(html)
            return
        elif vch_type_dispatch in (10, 17):
            html = voucher_renderers.render_note_html(header, party, comp, p_cfg, items, sundries, transport, item_batches, meta, css, is_credit=False)
            self.send_html(html)
            return
        elif vch_type_dispatch in (12, 13):
            html = voucher_renderers.render_order_html(header, party, comp, p_cfg, items, sundries, transport, item_batches, meta, css)
            self.send_html(html)
            return

        # Decoded Busy Metadata
        vch_type = header.get('VchType', 0)
        if vch_type == 11:
            invoice_title = "DELIVERY CHALLAN"
            copy_type = "ORIGINAL / TRANSPORTER COPY"
        elif vch_type == 9:
            invoice_title = p_cfg.get('title') or "TAX INVOICE"
            copy_type = "ORIGINAL FOR RECIPIENT"
        elif vch_type == 12:
            invoice_title = "SALES ORDER"
            copy_type = "ORDER CONFIRMATION"
        elif vch_type == 13:
            invoice_title = "PURCHASE ORDER"
            copy_type = "PROCUREMENT ORDER"
        elif vch_type in (26, 27):
            invoice_title = "QUOTATION"
            copy_type = "ESTIMATE COPY" 
        elif vch_type in (10, 2):
            invoice_title = "PURCHASE INVOICE"
            copy_type = "BUYER COPY"
        elif vch_type == 8:
            invoice_title = "STOCK TRANSFER"
            copy_type = "TRANSFER COPY"
        else:
            invoice_title = VCH_MAP.get(vch_type, "INVOICE").upper()
            copy_type = "ORIGINAL"

        company_name = p_cfg.get('company_name') or comp.get('print_name') or comp.get('name') or company
        addr1 = p_cfg.get('address_line1') or ""
        addr2 = p_cfg.get('address_line2') or ""
        gstin = comp.get('gstin') or ''
        mobile = comp.get('mobile') or ""
        email = comp.get('email') or ""
        declaration = p_cfg.get('declaration') or "E.& O.E."
        sign_for = p_cfg.get('sign_for') or f"For {company_name}"
        sign_title = p_cfg.get('sign_title') or "Authorised Signatory"

        # Terms & Conditions List from Busy BDS Standard Format
        terms_list = []
        if p_cfg.get('terms_json'):
            try:
                terms_list = json.loads(p_cfg['terms_json'])
            except Exception:
                pass
        if not terms_list:
            terms_list = [
                "1. Goods once sold will not be taken back.",
                "2. Interest @ 18% p.a. will be charged if the payment is not made with in the stipulated time.",
                "3. Subject to 'Tamilnadu' Jurisdiction only."
            ]
        terms_html = "".join([f"<div style='margin-bottom: 2px;'>{t}</div>" for t in terms_list])

        # Bank Details from Busy BDS Standard Format
        bank_lines = []
        if p_cfg.get('bank1'):
            b1 = p_cfg['bank1'].replace(';', ' | ').replace(',', ' ')
            bank_lines.append(f"<div>{b1}</div>")
        if p_cfg.get('bank2'):
            b2 = p_cfg['bank2'].replace(';', ' | ').replace(',', ' ')
            bank_lines.append(f"<div>{b2}</div>")
        bank_html = "".join(bank_lines) if bank_lines else "<div>Bank Details on Request</div>"

        # Linked voucher numbers for header row
        c_nos = ', '.join([c['BillNo'] for c in linked_challans if c.get('BillNo')]) if linked_challans else ''
        s_nos = ', '.join([s['VchNo'].strip() for s in linked_sales if s.get('VchNo')]) if linked_sales else ''

        # Items Rows (Standard 7-Column Format)
        item_rows = ""
        total_qty = 0.0
        total_item_amt = 0.0
        if items:
            for idx, it in enumerate(items, 1):
                q = abs(it.get('Qty', 0))
                p = abs(it.get('Price', 0))
                a = abs(it.get('Amount', 0))
                total_qty += q
                total_item_amt += a
                mcode = it.get('MasterCode1', 0)

                batches = item_batches.get(mcode, [])
                batch_sub = ""
                if batches:
                    batch_sub = '<div style="margin-top: 3px; font-size: 9.5px; color: #333; line-height: 1.35; padding-left: 4px;">'
                    for b in batches:
                        desc = f" [{b['BatchDesc']}]" if b.get('BatchDesc') else ""
                        batch_sub += f"<div>&bull; Bale {b['BatchNo']}: {b['BatchQty']:g} {it.get('UnitName', '')}{desc}</div>"
                    batch_sub += '</div>'

                item_rows += f'''
                <tr style="border-bottom: 1px solid #000; font-size: 11px;">
                    <td style="border-right: 1px solid #000; padding: 5px 4px; text-align: center; vertical-align: top;">{idx}</td>
                    <td style="border-right: 1px solid #000; padding: 5px 8px; vertical-align: top;">
                        <span style="font-weight: bold; font-size: 12px; color: #000;">{it.get('ItemName', '')}</span>
                        {batch_sub}
                    </td>
                    <td style="border-right: 1px solid #000; padding: 5px 4px; text-align: center; vertical-align: top;">{it.get('HSNCode', '')}</td>
                    <td style="border-right: 1px solid #000; padding: 5px 8px; text-align: right; vertical-align: top; font-weight: bold;">{q:,.2f}</td>
                    <td style="border-right: 1px solid #000; padding: 5px 4px; text-align: center; vertical-align: top;">{it.get('UnitName', '')}</td>
                    <td style="border-right: 1px solid #000; padding: 5px 8px; text-align: right; vertical-align: top;">{p:,.2f}</td>
                    <td style="padding: 5px 8px; text-align: right; vertical-align: top; font-weight: bold;">{a:,.2f}</td>
                </tr>
                '''
        elif accounts:
            for idx, acc in enumerate(accounts, 1):
                a = abs(acc.get('Amount', 0))
                total_item_amt += a
                item_rows += f'''
                <tr style="border-bottom: 1px solid #000; font-size: 11px;">
                    <td style="border-right: 1px solid #000; padding: 5px 4px; text-align: center;">{idx}</td>
                    <td style="border-right: 1px solid #000; padding: 5px 8px; font-weight: bold;">{acc.get('AccountName', '')}</td>
                    <td style="border-right: 1px solid #000; padding: 5px 4px; text-align: center;">-</td>
                    <td style="border-right: 1px solid #000; padding: 5px 8px; text-align: right; font-weight: bold;">1.00</td>
                    <td style="border-right: 1px solid #000; padding: 5px 4px; text-align: center;">Nos</td>
                    <td style="border-right: 1px solid #000; padding: 5px 8px; text-align: right;">{a:,.2f}</td>
                    <td style="padding: 5px 8px; text-align: right; font-weight: bold;">{a:,.2f}</td>
                </tr>
                '''

        # Empty spacer rows to ensure standard Busy invoice paper height
        needed_spacers = max(0, 4 - len(items or accounts))
        for _ in range(needed_spacers):
            item_rows += '''
            <tr style="height: 22px; border-bottom: 1px solid #eee;">
                <td style="border-right: 1px solid #000;">&nbsp;</td>
                <td style="border-right: 1px solid #000;">&nbsp;</td>
                <td style="border-right: 1px solid #000;">&nbsp;</td>
                <td style="border-right: 1px solid #000;">&nbsp;</td>
                <td style="border-right: 1px solid #000;">&nbsp;</td>
                <td style="border-right: 1px solid #000;">&nbsp;</td>
                <td>&nbsp;</td>
            </tr>
            '''

        # Filter and format non-zero sundries
        sundry_rows = ""
        for s in sundries:
            s_name = s.get('SundryName', '') or ''
            s_signed = s.get('Amount', 0) or 0
            s_amt = abs(s_signed)
            s_rate = s.get('Rate', 0) or 0
            s_base = s.get('BaseVal', 0) or 0
            if s_amt < 0.001:
                continue  # zero sundries do not change the bill

            rate_label = ""
            if s_rate > 0:
                if s_base and abs(s_base * s_rate / 100 - s_amt) <= 1:
                    rate_label = f" ({s_rate:g}%)"
                elif s_base and abs(s_base * s_rate - s_amt) <= 1:
                    rate_label = f" (@ &#x20B9;{s_rate:g})"

            prefix = "-" if s_signed < 0 else ""

            sundry_rows += f'''
            <tr>
                <td style="padding: 2.5px 4px; font-size: 11px; text-align: left;">{s_name}{rate_label}:</td>
                <td style="padding: 2.5px 4px; font-size: 11px; text-align: right; font-weight: bold;">{prefix}&#x20B9;{s_amt:,.2f}</td>
            </tr>
            '''

        html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{invoice_title} - {header.get('VchNo', '').strip()}</title>
    <style>
        @page {{
            size: A4 portrait;
            margin: 8mm;
        }}
        html, body {{
            margin: 0;
            padding: 0;
            background: #cbd5e1;
            font-family: Arial, Helvetica, sans-serif;
            color: #000;
            -webkit-print-color-adjust: exact;
        }}
        .invoice-box {{
            max-width: 820px;
            margin: 16px auto;
            background: #fff;
            border: 2px solid #000;
            box-sizing: border-box;
            box-shadow: 0 4px 14px rgba(0,0,0,0.18);
        }}
        .action-bar {{
            max-width: 820px;
            margin: 12px auto 0;
            display: flex;
            gap: 8px;
            justify-content: flex-end;
            align-items: center;
        }}
        .btn {{
            padding: 7px 14px;
            font-size: 12px;
            font-weight: bold;
            border-radius: 4px;
            cursor: pointer;
            border: 1px solid #000;
            text-decoration: none;
            display: inline-flex;
            align-items: center;
            gap: 5px;
        }}
        .btn-print {{ background: #1e3a8a; color: #fff; border-color: #1e3a8a; }}
        .btn-wa {{ background: #15803d; color: #fff; border-color: #15803d; }}
        .btn-back {{ background: #475569; color: #fff; border-color: #475569; }}

        .invoice-scroll-wrap {{
            width: 100%;
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
            padding-bottom: 24px;
        }}
        .mobile-hint {{ display: none; }}
        @media screen and (max-width: 860px) {{
            body {{
                background: #f1f5f9;
                padding: 6px 4px;
            }}
            .action-bar {{
                position: -webkit-sticky;
                position: sticky;
                top: 0;
                z-index: 100;
                background: #ffffff;
                padding: 8px 10px;
                margin: 0 0 8px 0;
                border-radius: 8px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                display: flex;
                gap: 6px;
                box-sizing: border-box;
                width: 100%;
            }}
            .action-bar .btn {{
                flex: 1;
                padding: 10px 4px;
                font-size: 12px;
                text-align: center;
                justify-content: center;
                border-radius: 6px;
            }}
            .invoice-box {{
                margin: 0 auto;
                min-width: 780px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            }}
            .mobile-hint {{
                display: block !important;
                text-align: center;
                font-size: 11px;
                color: #64748b;
                margin-bottom: 8px;
                font-weight: 500;
            }}
        }}
        @media print {{
            body {{ background: #fff !important; padding: 0 !important; }}
            .invoice-scroll-wrap {{ overflow: visible !important; width: 100% !important; }}
            .invoice-box {{
                min-width: 100% !important;
                max-width: 100% !important;
                margin: 0 !important;
                border: 1.5px solid #000 !important;
                box-shadow: none !important;
            }}
            .no-print, .mobile-hint {{ display: none !important; }}
        }}
    </style>
</head>
<body>
    <div class="action-bar no-print">
        <button onclick="window.history.back();" class="btn btn-back">&#x2190; Back</button>
        <button onclick="window.print();" class="btn btn-print">&#x1F5B6; Print / Save PDF</button>
        <button onclick="shareWhatsApp();" class="btn btn-wa">&#x1F4AC; WhatsApp Share</button>
    </div>

    <div class="mobile-hint no-print">&#x1F4F1; Swipe left/right to view full invoice &bull; Pinch to zoom</div>

    <div class="invoice-scroll-wrap">
        <div class="invoice-box" id="printableInvoice">
        <!-- TOP TITLE & COPY BAR -->
        <table style="width: 100%; border-collapse: collapse; border-bottom: 1.5px solid #000; font-size: 11px;">
            <tr>
                <td style="padding: 4px 8px; width: 33%; font-weight: bold;">GSTIN: {gstin}</td>
                <td style="padding: 4px 8px; width: 34%; text-align: center; font-size: 13px; font-weight: bold; letter-spacing: 1px;">{invoice_title}</td>
                <td style="padding: 4px 8px; width: 33%; text-align: right; font-weight: bold;">({copy_type})</td>
            </tr>
        </table>

        <!-- COMPANY HEADER -->
        <div style="text-align: center; padding: 8px 12px; border-bottom: 1.5px solid #000;">
            <div style="font-size: 24px; font-weight: bold; letter-spacing: 0.5px; color: #000;">{company_name}</div>
            {f'<div style="font-size: 12px; font-weight: bold; margin-top: 2px;">{addr1}</div>' if addr1 else ''}
            {f'<div style="font-size: 11px;">{addr2}</div>' if addr2 else ''}
            <div style="font-size: 11px; margin-top: 3px;">
                {f'Mobile: <strong>{mobile}</strong> ' if mobile else ''}
                {f'| Email: <strong>{email}</strong> ' if email else ''}
                | State: <strong>TAMIL NADU (Code: 33)</strong>
            </div>
        </div>

        <!-- BUYER & VOUCHER DETAILS -->
        <table style="width: 100%; border-collapse: collapse; border-bottom: 1.5px solid #000; font-size: 11px;">
            <tr>
                <!-- Left: Buyer Details -->
                <td style="width: 55%; border-right: 1.5px solid #000; padding: 6px 8px; vertical-align: top;">
                    <div style="font-size: 10px; font-weight: bold; text-transform: uppercase; color: #333; margin-bottom: 3px;">DETAILS OF RECEIVER (BILLED TO):</div>
                    <div style="font-size: 13px; font-weight: bold; color: #000; margin-bottom: 2px;">{header.get('PartyName', '')}</div>
                    <div style="line-height: 1.35; color: #222;">
                        {party.get('Address1', '')}<br>
                        {party.get('Address2', '')}
                    </div>
                    <div style="margin-top: 4px; line-height: 1.4;">
                        GSTIN / UIN: <strong>{party.get('GSTNo') or 'Unregistered'}</strong><br>
                        State: <strong>TAMIL NADU (Code: 33)</strong><br>
                        Mobile: <strong>{party.get('Mobile') or 'N/A'}</strong>
                    </div>
                </td>

                <!-- Right: Invoice / Dispatch Details -->
                <td style="width: 45%; padding: 6px 8px; vertical-align: top;">
                    <table style="width: 100%; border-collapse: collapse; font-size: 11px; line-height: 1.45;">
                        <tr>
                            <td style="width: 45%; font-weight: bold;">Invoice / Note No.:</td>
                            <td style="font-weight: bold; font-size: 12px;">{header.get('VchNo', '').strip()}</td>
                        </tr>
                        <tr>
                            <td style="font-weight: bold;">Dated:</td>
                            <td>{header.get('Date', '')}</td>
                        </tr>
                        {f'<tr><td style="font-weight: bold;">Challan No.:</td><td style="font-weight: bold;">{c_nos}</td></tr>' if c_nos else ''}
                        {f'<tr><td style="font-weight: bold;">Sales Inv No.:</td><td style="font-weight: bold;">{s_nos}</td></tr>' if s_nos else ''}
                        <tr>
                            <td style="font-weight: bold;">Transport:</td>
                            <td>{transport.get('Transport') or '-'}</td>
                        </tr>
                        <tr>
                            <td style="font-weight: bold;">Vehicle No.:</td>
                            <td>{transport.get('VehicleNo') or '-'}</td>
                        </tr>
                        <tr>
                            <td style="font-weight: bold;">Station / Dest.:</td>
                            <td>{transport.get('Station') or '-'}</td>
                        </tr>
                        <tr>
                            <td style="font-weight: bold;">GR / RR No.:</td>
                            <td>{transport.get('GrNo') or '-'}</td>
                        </tr>
                    </table>
                </td>
            </tr>
        </table>

        <!-- ITEMS GRID (Standard 7-Column Format) -->
        <table style="width: 100%; border-collapse: collapse; border-bottom: 1.5px solid #000;">
            <thead>
                <tr style="background: #f4f4f4; border-bottom: 1.5px solid #000; font-size: 11px; font-weight: bold;">
                    <th style="border-right: 1px solid #000; padding: 5px; width: 5%; text-align: center;">S.N.</th>
                    <th style="border-right: 1px solid #000; padding: 5px 8px; width: 45%; text-align: left;">Description of Goods</th>
                    <th style="border-right: 1px solid #000; padding: 5px; width: 10%; text-align: center;">HSN/SAC</th>
                    <th style="border-right: 1px solid #000; padding: 5px 8px; width: 12%; text-align: right;">Quantity</th>
                    <th style="border-right: 1px solid #000; padding: 5px; width: 8%; text-align: center;">Unit</th>
                    <th style="border-right: 1px solid #000; padding: 5px 8px; width: 10%; text-align: right;">Rate (&#x20B9;)</th>
                    <th style="padding: 5px 8px; width: 10%; text-align: right;">Amount (&#x20B9;)</th>
                </tr>
            </thead>
            <tbody>
                {item_rows}
            </tbody>
            <tfoot>
                <tr style="border-top: 1.5px solid #000; background: #fafafa; font-weight: bold; font-size: 11px;">
                    <td colspan="3" style="border-right: 1px solid #000; padding: 5px 8px; text-align: right;">TOTAL:</td>
                    <td style="border-right: 1px solid #000; padding: 5px 8px; text-align: right; font-size: 12px;">{total_qty:,.2f}</td>
                    <td style="border-right: 1px solid #000; padding: 5px; text-align: center;">Pcs.</td>
                    <td style="border-right: 1px solid #000; padding: 5px 8px; text-align: right;">-</td>
                    <td style="padding: 5px 8px; text-align: right; font-size: 12px;">{total_item_amt:,.2f}</td>
                </tr>
            </tfoot>
        </table>

        <!-- LOWER SECTION (WORDS + BANK + TERMS vs SUNDRIES + GRAND TOTAL) -->
        <table style="width: 100%; border-collapse: collapse; border-bottom: 1.5px solid #000; font-size: 11px;">
            <tr>
                <!-- Left: Words, Bank, Terms -->
                <td style="width: 56%; border-right: 1.5px solid #000; padding: 6px 8px; vertical-align: top;">
                    <div style="font-size: 10px; font-weight: bold; color: #444;">Amount Chargeable (in words):</div>
                    <div style="font-size: 11.5px; font-weight: bold; margin-bottom: 8px; color: #000;">{header.get('AmountInWords', '')}</div>

                    <!-- Bank Details Box -->
                    <div style="border-top: 1px solid #000; padding-top: 4px; margin-top: 4px;">
                        <div style="font-size: 10px; font-weight: bold; text-transform: uppercase; margin-bottom: 2px;">BANK DETAILS:</div>
                        <div style="font-size: 10px; line-height: 1.35; color: #222;">
                            {bank_html}
                        </div>
                    </div>

                    <!-- Terms & Conditions Box -->
                    <div style="border-top: 1px solid #000; padding-top: 4px; margin-top: 6px;">
                        <div style="font-size: 10px; font-weight: bold; text-transform: uppercase; margin-bottom: 2px;">TERMS & CONDITIONS:</div>
                        <div style="font-size: 9.5px; line-height: 1.35; color: #222;">
                            {terms_html}
                        </div>
                    </div>
                </td>

                <!-- Right: Bill Sundries & Grand Total -->
                <td style="width: 44%; padding: 6px 8px; vertical-align: top;">
                    <table style="width: 100%; border-collapse: collapse;">
                        <tr>
                            <td style="padding: 2.5px 4px; font-size: 11px;">Item Sub Total:</td>
                            <td style="padding: 2.5px 4px; font-size: 11px; text-align: right; font-weight: bold;">&#x20B9;{total_item_amt:,.2f}</td>
                        </tr>
                        {sundry_rows}
                    </table>

                    <div style="border-top: 1.5px solid #000; border-bottom: 3px double #000; padding: 6px 4px; margin-top: 8px; display: flex; justify-content: space-between; font-weight: bold; font-size: 15px; color: #000;">
                        <span>Grand Total:</span>
                        <span>&#x20B9;{header.get('VchAmt', 0):,.2f}</span>
                    </div>
                </td>
            </tr>
        </table>

        <!-- SIGNATURES FOOTER -->
        <table style="width: 100%; border-collapse: collapse; font-size: 11px;">
            <tr>
                <td style="width: 50%; padding: 8px 10px; vertical-align: top; border-right: 1.5px solid #000;">
                    <div style="font-size: 10px; font-weight: bold;">Declaration: {declaration}</div>
                    <div style="height: 48px;"></div>
                    <div style="font-size: 11px; font-weight: bold; border-top: 1px dashed #666; display: inline-block; padding-top: 3px;">
                        Receiver's Signature
                    </div>
                </td>
                <td style="width: 50%; padding: 8px 10px; vertical-align: top; text-align: right;">
                    <div style="font-size: 11px; font-weight: bold;">{sign_for}</div>
                    <div style="height: 48px;"></div>
                    <div style="font-size: 11px; font-weight: bold; border-top: 1px dashed #666; display: inline-block; padding-top: 3px;">
                        {sign_title}
                    </div>
                </td>
            </tr>
        </table>
    </div>
    </div>

    <script>
        function shareWhatsApp() {{
            const text = encodeURIComponent(
                "*{company_name} - {invoice_title}*\n" +
                "Invoice No: {header.get('VchNo', '').strip()}\n" +
                "Date: {header.get('Date', '')}\n" +
                "Party: {header.get('PartyName', '')}\n" +
                "Amount: Rs. {header.get('VchAmt', 0):,.2f}\n" +
                "View Invoice: " + window.location.href
            );
            window.open("https://api.whatsapp.com/send?text=" + text, "_blank");
        }}
    </script>
</body>
</html>'''
        self.send_html(html)

    # ============================================================
    # DEDICATED VOUCHER PRINT TEMPLATES (Busy Standard Formats)
    # ============================================================

    def _voucher_common_css(self):
        return '''
        @page { size: A4 portrait; margin: 8mm; }
        html, body { margin: 0; padding: 0; background: #cbd5e1; font-family: Arial, Helvetica, sans-serif; color: #000; -webkit-print-color-adjust: exact; }
        .voucher-box { max-width: 820px; margin: 16px auto; background: #fff; border: 2px solid #000; box-sizing: border-box; box-shadow: 0 4px 14px rgba(0,0,0,0.18); }
        .action-bar { max-width: 820px; margin: 12px auto 0; display: flex; gap: 8px; justify-content: flex-end; align-items: center; }
        .btn { padding: 7px 14px; font-size: 12px; font-weight: bold; border-radius: 4px; cursor: pointer; border: 1px solid #000; text-decoration: none; display: inline-flex; align-items: center; gap: 5px; }
        .btn-print { background: #1e3a8a; color: #fff; border-color: #1e3a8a; }
        .btn-wa { background: #15803d; color: #fff; border-color: #15803d; }
        .btn-back { background: #475569; color: #fff; border-color: #475569; }
        table { border-collapse: collapse; }
        .tbl-bordered td, .tbl-bordered th { border: 1px solid #000; padding: 5px 6px; }
        .voucher-scroll-wrap {
            width: 100%;
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
            padding-bottom: 24px;
        }
        .mobile-hint { display: none; }
        @media screen and (max-width: 860px) {
            body { background: #f1f5f9; padding: 6px 4px; }
            .action-bar {
                position: -webkit-sticky;
                position: sticky;
                top: 0;
                z-index: 100;
                background: #ffffff;
                padding: 8px 10px;
                margin: 0 0 8px 0;
                border-radius: 8px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                display: flex;
                gap: 6px;
                box-sizing: border-box;
                width: 100%;
            }
            .action-bar .btn {
                flex: 1;
                padding: 10px 4px;
                font-size: 12px;
                text-align: center;
                justify-content: center;
                border-radius: 6px;
            }
            .voucher-box {
                margin: 0 auto;
                min-width: 760px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            }
            .mobile-hint {
                display: block !important;
                text-align: center;
                font-size: 11px;
                color: #64748b;
                margin-bottom: 8px;
                font-weight: 500;
            }
        }
        @media print {
            body { background: #fff !important; padding: 0 !important; }
            .voucher-scroll-wrap { overflow: visible !important; width: 100% !important; }
            .voucher-box { max-width: 100% !important; min-width: 100% !important; margin: 0 !important; border: 1.5px solid #000 !important; box-shadow: none !important; }
            .no-print, .mobile-hint { display: none !important; }
        }
        '''

    def _voucher_meta(self, comp, p_cfg):
        return {
            'company_name': p_cfg.get('company_name') or comp.get('print_name') or comp.get('name') or '',
            'addr1': p_cfg.get('address_line1') or '',
            'addr2': p_cfg.get('address_line2') or '',
            'gstin': comp.get('gstin') or '',
            'mobile': comp.get('mobile') or '',
            'email': comp.get('email') or '',
            'sign_for': p_cfg.get('sign_for') or f"For {p_cfg.get('company_name') or comp.get('print_name') or comp.get('name') or ''}",
            'sign_title': p_cfg.get('sign_title') or 'Authorised Signatory',
        }

    def _build_account_rows_html(self, signed_accounts):
        rows = ""
        total_dr = 0.0
        total_cr = 0.0
        for idx, acc in enumerate(signed_accounts, 1):
            name = acc.get('AccountName', '')
            amt = acc.get('Amount', 0) or 0
            dr = abs(amt) if amt > 0 else 0
            cr = abs(amt) if amt < 0 else 0
            total_dr += dr
            total_cr += cr
            rows += f'''<tr style="font-size: 11px;">
                <td class="tbl-bordered" style="text-align: center;">{idx}</td>
                <td class="tbl-bordered" style="font-weight: bold;">{name}</td>
                <td class="tbl-bordered" style="text-align: right;">{f"{dr:,.2f}" if dr > 0 else ""}</td>
                <td class="tbl-bordered" style="text-align: right;">{f"{cr:,.2f}" if cr > 0 else ""}</td>
            </tr>'''
        return rows, total_dr, total_cr

    def _build_bill_adj_html(self, bill_adjustments):
        if not bill_adjustments:
            return ""
        rows = ""
        total_adj = 0.0
        for idx, b in enumerate(bill_adjustments, 1):
            adj = abs(b.get('AdjAmount', 0))
            total_adj += adj
            rows += f'''<tr style="font-size: 11px;">
                <td class="tbl-bordered" style="text-align: center;">{idx}</td>
                <td class="tbl-bordered" style="font-weight: bold;">{(b.get('BillNo') or '').strip() or '-'}</td>
                <td class="tbl-bordered" style="text-align: center;">{b.get('BillDate', '') or '-'}</td>
                <td class="tbl-bordered" style="text-align: right; font-weight: bold;">&#x20B9;{adj:,.2f}</td>
                <td class="tbl-bordered">{b.get('RefType', '')}{(' - ' + b['Narration']) if b.get('Narration') else ''}</td>
            </tr>'''
        return f'''
        <div style="padding: 6px 10px; border-bottom: 1.5px solid #000;">
            <div style="font-size: 10px; font-weight: bold; text-transform: uppercase; margin-bottom: 4px;">BILL ADJUSTMENT DETAILS:</div>
            <table class="tbl-bordered" style="width: 100%; font-size: 11px;">
                <thead>
                    <tr style="background: #f4f4f4; font-weight: bold; font-size: 10px;">
                        <th class="tbl-bordered" style="width: 6%;">S.N.</th>
                        <th class="tbl-bordered" style="text-align: left;">Bill / Ref No.</th>
                        <th class="tbl-bordered" style="width: 14%; text-align: center;">Bill Date</th>
                        <th class="tbl-bordered" style="width: 18%; text-align: right;">Adjusted Amt (&#x20B9;)</th>
                        <th class="tbl-bordered" style="text-align: left;">Type / Remarks</th>
                    </tr>
                </thead>
                <tbody>{rows}</tbody>
                <tfoot>
                    <tr style="font-weight: bold; background: #fafafa; font-size: 11px;">
                        <td class="tbl-bordered" colspan="3" style="text-align: right;">TOTAL:</td>
                        <td class="tbl-bordered" style="text-align: right;">&#x20B9;{total_adj:,.2f}</td>
                        <td class="tbl-bordered"></td>
                    </tr>
                </tfoot>
            </table>
        </div>
        '''

    def _render_receipt_html(self, header, party, comp, p_cfg, signed_accounts, bill_adjustments):
        m = self._voucher_meta(comp, p_cfg)
        css = self._voucher_common_css()
        vch_no = header.get('VchNo', '').strip()
        date = header.get('Date', '')
        vch_amt = header.get('VchAmt', 0)
        amt_words = header.get('AmountInWords', '')

        received_from_parts = [a.get('AccountName', '') for a in signed_accounts if (a.get('Amount', 0) or 0) > 0]
        bank_parts = [a.get('AccountName', '') for a in signed_accounts if (a.get('Amount', 0) or 0) < 0]
        received_from = received_from_parts[0] if received_from_parts else header.get('PartyName', 'N/A')
        bank_account = bank_parts[0] if bank_parts else 'Cash'

        party_addr = f"{party.get('Address1', '')} {party.get('Address2', '')}".strip()
        party_gstin = party.get('GSTNo') or 'Unregistered'

        account_rows, total_dr, total_cr = self._build_account_rows_html(signed_accounts)
        bill_adj_html = self._build_bill_adj_html(bill_adjustments)

        html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>RECEIPT VOUCHER - {vch_no}</title>
    <style>{css}</style>
</head>
<body>
    <div class="action-bar no-print">
        <button onclick="window.history.back();" class="btn btn-back">&#x2190; Back</button>
        <button onclick="window.print();" class="btn btn-print">&#x1F5B6; Print / Save PDF</button>
        <button onclick="shareWhatsApp();" class="btn btn-wa">&#x1F4AC; WhatsApp</button>
    </div>

    <div class="mobile-hint no-print">&#x1F4F1; Swipe left/right to view full voucher &bull; Pinch to zoom</div>
    <div class="voucher-scroll-wrap">
        <div class="voucher-box">
        <!-- TITLE BAR -->
        <div style="text-align: center; padding: 6px; border-bottom: 2px solid #000; font-size: 14px; font-weight: bold; letter-spacing: 2px; background: #f8f8f8;">
            RECEIPT VOUCHER
        </div>

        <!-- COMPANY HEADER -->
        <div style="text-align: center; padding: 8px 12px; border-bottom: 1.5px solid #000;">
            <div style="font-size: 22px; font-weight: bold; letter-spacing: 0.5px;">{m['company_name']}</div>
            {f'<div style="font-size: 11px; font-weight: bold; margin-top: 2px;">{m["addr1"]}</div>' if m['addr1'] else ''}
            {f'<div style="font-size: 11px;">{m["addr2"]}</div>' if m['addr2'] else ''}
            <div style="font-size: 11px; margin-top: 3px;">
                {f'GSTIN: <strong>{m["gstin"]}</strong>' if m['gstin'] else ''}
                {f' | Mobile: <strong>{m["mobile"]}</strong>' if m['mobile'] else ''}
            </div>
        </div>

        <!-- VOUCHER INFO ROW -->
        <table style="width: 100%; border-bottom: 1.5px solid #000; font-size: 11px;">
            <tr>
                <td style="padding: 6px 10px; width: 50%; border-right: 1px solid #000;">
                    <strong>Receipt No.:</strong> <span style="font-size: 13px; font-weight: bold;">{vch_no}</span>
                </td>
                <td style="padding: 6px 10px; width: 50%; text-align: right;">
                    <strong>Date:</strong> <span style="font-weight: bold;">{date}</span>
                </td>
            </tr>
        </table>

        <!-- RECEIVED FROM & PAYMENT MODE -->
        <table style="width: 100%; border-bottom: 1.5px solid #000; font-size: 11px;">
            <tr>
                <td style="width: 55%; border-right: 1px solid #000; padding: 8px 10px; vertical-align: top;">
                    <div style="font-size: 10px; font-weight: bold; text-transform: uppercase; color: #555; margin-bottom: 3px;">RECEIVED FROM:</div>
                    <div style="font-size: 14px; font-weight: bold; color: #000;">{received_from}</div>
                    {f'<div style="margin-top: 2px; color: #333;">{party_addr}</div>' if party_addr else ''}
                    <div style="margin-top: 2px;">GSTIN: <strong>{party_gstin}</strong></div>
                </td>
                <td style="width: 45%; padding: 8px 10px; vertical-align: top;">
                    <div style="font-size: 10px; font-weight: bold; text-transform: uppercase; color: #555; margin-bottom: 3px;">PAYMENT MODE / ACCOUNT:</div>
                    <div style="font-size: 13px; font-weight: bold; color: #000;">{bank_account}</div>
                </td>
            </tr>
        </table>

        <!-- AMOUNT BOX -->
        <div style="padding: 8px 10px; border-bottom: 1.5px solid #000;">
            <table style="width: 100%;">
                <tr>
                    <td style="font-size: 12px; font-weight: bold;">Amount Received:</td>
                    <td style="text-align: right; font-size: 18px; font-weight: bold; color: #000;">&#x20B9;{vch_amt:,.2f}</td>
                </tr>
            </table>
            <div style="font-size: 11px; margin-top: 4px; font-style: italic; color: #333;">({amt_words})</div>
        </div>

        <!-- ACCOUNTING PARTICULARS -->
        <div style="padding: 6px 10px; border-bottom: 1.5px solid #000;">
            <div style="font-size: 10px; font-weight: bold; text-transform: uppercase; margin-bottom: 4px;">ACCOUNTING PARTICULARS:</div>
            <table class="tbl-bordered" style="width: 100%; font-size: 11px;">
                <thead>
                    <tr style="background: #f4f4f4; font-weight: bold; font-size: 10px;">
                        <th class="tbl-bordered" style="width: 6%;">S.N.</th>
                        <th class="tbl-bordered" style="text-align: left;">Particulars / Account Head</th>
                        <th class="tbl-bordered" style="width: 18%; text-align: right;">Debit (&#x20B9;)</th>
                        <th class="tbl-bordered" style="width: 18%; text-align: right;">Credit (&#x20B9;)</th>
                    </tr>
                </thead>
                <tbody>{account_rows}</tbody>
                <tfoot>
                    <tr style="font-weight: bold; background: #fafafa; font-size: 11px;">
                        <td class="tbl-bordered" colspan="2" style="text-align: right;">TOTAL:</td>
                        <td class="tbl-bordered" style="text-align: right;">{total_dr:,.2f}</td>
                        <td class="tbl-bordered" style="text-align: right;">{total_cr:,.2f}</td>
                    </tr>
                </tfoot>
            </table>
        </div>

        <!-- BILL ADJUSTMENTS -->
        {bill_adj_html}

        <!-- SIGNATURES -->
        <table style="width: 100%; font-size: 11px;">
            <tr>
                <td style="width: 50%; padding: 10px; vertical-align: top; border-right: 1.5px solid #000;">
                    <div style="height: 50px;"></div>
                    <div style="border-top: 1px dashed #666; display: inline-block; padding-top: 3px; font-weight: bold;">
                        Receiver's Signature
                    </div>
                </td>
                <td style="width: 50%; padding: 10px; vertical-align: top; text-align: right;">
                    <div style="font-weight: bold;">{m['sign_for']}</div>
                    <div style="height: 50px;"></div>
                    <div style="border-top: 1px dashed #666; display: inline-block; padding-top: 3px; font-weight: bold;">
                        {m['sign_title']}
                    </div>
                </td>
            </tr>
        </table>
    </div>

    <script>
        function shareWhatsApp() {{
            const text = encodeURIComponent(
                "*{m['company_name']} - RECEIPT VOUCHER*\\n" +
                "Receipt No: {vch_no}\\n" +
                "Date: {date}\\n" +
                "Received From: {received_from}\\n" +
                "Amount: Rs. {vch_amt:,.2f}\\n" +
                "View: " + window.location.href
            );
            window.open("https://api.whatsapp.com/send?text=" + text, "_blank");
        }}
    </script>
</body>
</html>'''
        self.send_html(html)

    def _render_payment_html(self, header, party, comp, p_cfg, signed_accounts, bill_adjustments):
        m = self._voucher_meta(comp, p_cfg)
        css = self._voucher_common_css()
        vch_no = header.get('VchNo', '').strip()
        date = header.get('Date', '')
        vch_amt = header.get('VchAmt', 0)
        amt_words = header.get('AmountInWords', '')

        paid_to_parts = [a.get('AccountName', '') for a in signed_accounts if (a.get('Amount', 0) or 0) > 0]
        paid_from_parts = [a.get('AccountName', '') for a in signed_accounts if (a.get('Amount', 0) or 0) < 0]
        paid_to = paid_to_parts[0] if paid_to_parts else header.get('PartyName', 'N/A')
        paid_from = paid_from_parts[0] if paid_from_parts else 'Cash'

        party_addr = f"{party.get('Address1', '')} {party.get('Address2', '')}".strip()
        party_gstin = party.get('GSTNo') or 'Unregistered'

        account_rows, total_dr, total_cr = self._build_account_rows_html(signed_accounts)
        bill_adj_html = self._build_bill_adj_html(bill_adjustments)

        html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PAYMENT VOUCHER - {vch_no}</title>
    <style>{css}</style>
</head>
<body>
    <div class="action-bar no-print">
        <button onclick="window.history.back();" class="btn btn-back">&#x2190; Back</button>
        <button onclick="window.print();" class="btn btn-print">&#x1F5B6; Print / Save PDF</button>
        <button onclick="shareWhatsApp();" class="btn btn-wa">&#x1F4AC; WhatsApp</button>
    </div>

    <div class="mobile-hint no-print">&#x1F4F1; Swipe left/right to view full voucher &bull; Pinch to zoom</div>
    <div class="voucher-scroll-wrap">
        <div class="voucher-box">
        <!-- TITLE BAR -->
        <div style="text-align: center; padding: 6px; border-bottom: 2px solid #000; font-size: 14px; font-weight: bold; letter-spacing: 2px; background: #f8f8f8;">
            PAYMENT VOUCHER
        </div>

        <!-- COMPANY HEADER -->
        <div style="text-align: center; padding: 8px 12px; border-bottom: 1.5px solid #000;">
            <div style="font-size: 22px; font-weight: bold; letter-spacing: 0.5px;">{m['company_name']}</div>
            {f'<div style="font-size: 11px; font-weight: bold; margin-top: 2px;">{m["addr1"]}</div>' if m['addr1'] else ''}
            {f'<div style="font-size: 11px;">{m["addr2"]}</div>' if m['addr2'] else ''}
            <div style="font-size: 11px; margin-top: 3px;">
                {f'GSTIN: <strong>{m["gstin"]}</strong>' if m['gstin'] else ''}
                {f' | Mobile: <strong>{m["mobile"]}</strong>' if m['mobile'] else ''}
            </div>
        </div>

        <!-- VOUCHER INFO ROW -->
        <table style="width: 100%; border-bottom: 1.5px solid #000; font-size: 11px;">
            <tr>
                <td style="padding: 6px 10px; width: 50%; border-right: 1px solid #000;">
                    <strong>Payment No.:</strong> <span style="font-size: 13px; font-weight: bold;">{vch_no}</span>
                </td>
                <td style="padding: 6px 10px; width: 50%; text-align: right;">
                    <strong>Date:</strong> <span style="font-weight: bold;">{date}</span>
                </td>
            </tr>
        </table>

        <!-- PAID TO & PAID FROM -->
        <table style="width: 100%; border-bottom: 1.5px solid #000; font-size: 11px;">
            <tr>
                <td style="width: 55%; border-right: 1px solid #000; padding: 8px 10px; vertical-align: top;">
                    <div style="font-size: 10px; font-weight: bold; text-transform: uppercase; color: #555; margin-bottom: 3px;">PAID TO / BENEFICIARY:</div>
                    <div style="font-size: 14px; font-weight: bold; color: #000;">{paid_to}</div>
                    {f'<div style="margin-top: 2px; color: #333;">{party_addr}</div>' if party_addr else ''}
                    <div style="margin-top: 2px;">GSTIN: <strong>{party_gstin}</strong></div>
                </td>
                <td style="width: 45%; padding: 8px 10px; vertical-align: top;">
                    <div style="font-size: 10px; font-weight: bold; text-transform: uppercase; color: #555; margin-bottom: 3px;">PAID FROM / ACCOUNT:</div>
                    <div style="font-size: 13px; font-weight: bold; color: #000;">{paid_from}</div>
                </td>
            </tr>
        </table>

        <!-- AMOUNT BOX -->
        <div style="padding: 8px 10px; border-bottom: 1.5px solid #000;">
            <table style="width: 100%;">
                <tr>
                    <td style="font-size: 12px; font-weight: bold;">Amount Paid:</td>
                    <td style="text-align: right; font-size: 18px; font-weight: bold; color: #000;">&#x20B9;{vch_amt:,.2f}</td>
                </tr>
            </table>
            <div style="font-size: 11px; margin-top: 4px; font-style: italic; color: #333;">({amt_words})</div>
        </div>

        <!-- ACCOUNTING PARTICULARS -->
        <div style="padding: 6px 10px; border-bottom: 1.5px solid #000;">
            <div style="font-size: 10px; font-weight: bold; text-transform: uppercase; margin-bottom: 4px;">ACCOUNT DISTRIBUTION / PARTICULARS:</div>
            <table class="tbl-bordered" style="width: 100%; font-size: 11px;">
                <thead>
                    <tr style="background: #f4f4f4; font-weight: bold; font-size: 10px;">
                        <th class="tbl-bordered" style="width: 6%;">S.N.</th>
                        <th class="tbl-bordered" style="text-align: left;">Particulars / Account Head</th>
                        <th class="tbl-bordered" style="width: 18%; text-align: right;">Debit (&#x20B9;)</th>
                        <th class="tbl-bordered" style="width: 18%; text-align: right;">Credit (&#x20B9;)</th>
                    </tr>
                </thead>
                <tbody>{account_rows}</tbody>
                <tfoot>
                    <tr style="font-weight: bold; background: #fafafa; font-size: 11px;">
                        <td class="tbl-bordered" colspan="2" style="text-align: right;">TOTAL:</td>
                        <td class="tbl-bordered" style="text-align: right;">{total_dr:,.2f}</td>
                        <td class="tbl-bordered" style="text-align: right;">{total_cr:,.2f}</td>
                    </tr>
                </tfoot>
            </table>
        </div>

        <!-- BILL ADJUSTMENTS -->
        {bill_adj_html}

        <!-- SIGNATURES (4 signature blocks for Payment) -->
        <table style="width: 100%; font-size: 11px;">
            <tr>
                <td style="width: 25%; padding: 8px 6px; vertical-align: top; border-right: 1px solid #ccc; text-align: center;">
                    <div style="height: 45px;"></div>
                    <div style="border-top: 1px dashed #666; display: inline-block; padding-top: 3px; font-weight: bold; font-size: 10px;">
                        Prepared By
                    </div>
                </td>
                <td style="width: 25%; padding: 8px 6px; vertical-align: top; border-right: 1px solid #ccc; text-align: center;">
                    <div style="height: 45px;"></div>
                    <div style="border-top: 1px dashed #666; display: inline-block; padding-top: 3px; font-weight: bold; font-size: 10px;">
                        Passed By
                    </div>
                </td>
                <td style="width: 25%; padding: 8px 6px; vertical-align: top; border-right: 1px solid #ccc; text-align: center;">
                    <div style="height: 45px;"></div>
                    <div style="border-top: 1px dashed #666; display: inline-block; padding-top: 3px; font-weight: bold; font-size: 10px;">
                        Receiver's Signature
                    </div>
                </td>
                <td style="width: 25%; padding: 8px 6px; vertical-align: top; text-align: center;">
                    <div style="font-weight: bold; font-size: 10px;">{m['sign_for']}</div>
                    <div style="height: 45px;"></div>
                    <div style="border-top: 1px dashed #666; display: inline-block; padding-top: 3px; font-weight: bold; font-size: 10px;">
                        {m['sign_title']}
                    </div>
                </td>
            </tr>
        </table>
    </div>

    <script>
        function shareWhatsApp() {{
            const text = encodeURIComponent(
                "*{m['company_name']} - PAYMENT VOUCHER*\\n" +
                "Payment No: {vch_no}\\n" +
                "Date: {date}\\n" +
                "Paid To: {paid_to}\\n" +
                "Amount: Rs. {vch_amt:,.2f}\\n" +
                "View: " + window.location.href
            );
            window.open("https://api.whatsapp.com/send?text=" + text, "_blank");
        }}
    </script>
</body>
</html>'''
        self.send_html(html)

    def _render_journal_html(self, header, party, comp, p_cfg, signed_accounts):
        m = self._voucher_meta(comp, p_cfg)
        css = self._voucher_common_css()
        vch_type = header.get('VchType', 16)
        voucher_title = "CONTRA VOUCHER" if vch_type == 15 else "JOURNAL VOUCHER"
        vch_no = header.get('VchNo', '').strip()
        date = header.get('Date', '')
        vch_amt = header.get('VchAmt', 0)
        amt_words = header.get('AmountInWords', '')

        account_rows, total_dr, total_cr = self._build_account_rows_html(signed_accounts)

        spacer_count = max(0, 4 - len(signed_accounts))
        spacer_rows = ""
        for _ in range(spacer_count):
            spacer_rows += '''<tr style="height: 22px;">
                <td class="tbl-bordered">&nbsp;</td>
                <td class="tbl-bordered">&nbsp;</td>
                <td class="tbl-bordered">&nbsp;</td>
                <td class="tbl-bordered">&nbsp;</td>
            </tr>'''

        html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{voucher_title} - {vch_no}</title>
    <style>{css}</style>
</head>
<body>
    <div class="action-bar no-print">
        <button onclick="window.history.back();" class="btn btn-back">&#x2190; Back</button>
        <button onclick="window.print();" class="btn btn-print">&#x1F5B6; Print / Save PDF</button>
        <button onclick="shareWhatsApp();" class="btn btn-wa">&#x1F4AC; WhatsApp</button>
    </div>

    <div class="mobile-hint no-print">&#x1F4F1; Swipe left/right to view full voucher &bull; Pinch to zoom</div>
    <div class="voucher-scroll-wrap">
        <div class="voucher-box">
        <!-- TITLE BAR -->
        <div style="text-align: center; padding: 6px; border-bottom: 2px solid #000; font-size: 14px; font-weight: bold; letter-spacing: 2px; background: #f8f8f8;">
            {voucher_title}
        </div>

        <!-- COMPANY HEADER -->
        <div style="text-align: center; padding: 8px 12px; border-bottom: 1.5px solid #000;">
            <div style="font-size: 22px; font-weight: bold; letter-spacing: 0.5px;">{m['company_name']}</div>
            {f'<div style="font-size: 11px; font-weight: bold; margin-top: 2px;">{m["addr1"]}</div>' if m['addr1'] else ''}
            {f'<div style="font-size: 11px;">{m["addr2"]}</div>' if m['addr2'] else ''}
            <div style="font-size: 11px; margin-top: 3px;">
                {f'GSTIN: <strong>{m["gstin"]}</strong>' if m['gstin'] else ''}
                {f' | Mobile: <strong>{m["mobile"]}</strong>' if m['mobile'] else ''}
            </div>
        </div>

        <!-- VOUCHER INFO ROW -->
        <table style="width: 100%; border-bottom: 1.5px solid #000; font-size: 11px;">
            <tr>
                <td style="padding: 6px 10px; width: 50%; border-right: 1px solid #000;">
                    <strong>Voucher No.:</strong> <span style="font-size: 13px; font-weight: bold;">{vch_no}</span>
                </td>
                <td style="padding: 6px 10px; width: 50%; text-align: right;">
                    <strong>Date:</strong> <span style="font-weight: bold;">{date}</span>
                </td>
            </tr>
        </table>

        <!-- DOUBLE-ENTRY ACCOUNTING TABLE -->
        <div style="padding: 6px 10px; border-bottom: 1.5px solid #000;">
            <table class="tbl-bordered" style="width: 100%; font-size: 11px;">
                <thead>
                    <tr style="background: #f4f4f4; font-weight: bold; font-size: 10px;">
                        <th class="tbl-bordered" style="width: 6%;">S.N.</th>
                        <th class="tbl-bordered" style="text-align: left;">Account Head / Particulars</th>
                        <th class="tbl-bordered" style="width: 20%; text-align: right;">Debit Amount (&#x20B9;)</th>
                        <th class="tbl-bordered" style="width: 20%; text-align: right;">Credit Amount (&#x20B9;)</th>
                    </tr>
                </thead>
                <tbody>
                    {account_rows}
                    {spacer_rows}
                </tbody>
                <tfoot>
                    <tr style="font-weight: bold; background: #fafafa; font-size: 12px; border-top: 2px solid #000;">
                        <td class="tbl-bordered" colspan="2" style="text-align: right; padding: 6px;">TOTAL:</td>
                        <td class="tbl-bordered" style="text-align: right; padding: 6px;">{total_dr:,.2f}</td>
                        <td class="tbl-bordered" style="text-align: right; padding: 6px;">{total_cr:,.2f}</td>
                    </tr>
                </tfoot>
            </table>
        </div>

        <!-- AMOUNT IN WORDS -->
        <div style="padding: 8px 10px; border-bottom: 1.5px solid #000;">
            <div style="font-size: 10px; font-weight: bold; color: #555;">Amount (in words):</div>
            <div style="font-size: 12px; font-weight: bold; color: #000; margin-top: 2px;">{amt_words}</div>
            <div style="margin-top: 6px; border-top: 1.5px solid #000; border-bottom: 3px double #000; padding: 6px 0; display: flex; justify-content: space-between; font-weight: bold; font-size: 15px;">
                <span>Grand Total:</span>
                <span>&#x20B9;{vch_amt:,.2f}</span>
            </div>
        </div>

        <!-- SIGNATURES -->
        <table style="width: 100%; font-size: 11px;">
            <tr>
                <td style="width: 50%; padding: 10px; vertical-align: top; border-right: 1.5px solid #000;">
                    <div style="height: 50px;"></div>
                    <div style="border-top: 1px dashed #666; display: inline-block; padding-top: 3px; font-weight: bold;">
                        Checked By
                    </div>
                </td>
                <td style="width: 50%; padding: 10px; vertical-align: top; text-align: right;">
                    <div style="font-weight: bold;">{m['sign_for']}</div>
                    <div style="height: 50px;"></div>
                    <div style="border-top: 1px dashed #666; display: inline-block; padding-top: 3px; font-weight: bold;">
                        {m['sign_title']}
                    </div>
                </td>
            </tr>
        </table>
    </div>

    <script>
        function shareWhatsApp() {{
            const text = encodeURIComponent(
                "*{m['company_name']} - {voucher_title}*\\n" +
                "Voucher No: {vch_no}\\n" +
                "Date: {date}\\n" +
                "Amount: Rs. {vch_amt:,.2f}\\n" +
                "View: " + window.location.href
            );
            window.open("https://api.whatsapp.com/send?text=" + text, "_blank");
        }}
    </script>
</body>
</html>'''
        self.send_html(html)

    def _render_stock_transfer_html(self, header, comp, p_cfg, from_mc, to_mc, items, transport, item_batches):
        m = self._voucher_meta(comp, p_cfg)
        css = self._voucher_common_css()
        vch_type = header.get('VchType', 8)
        vch_no = header.get('VchNo', '').strip()
        date = header.get('Date', '')
        series_name = header.get('SeriesName', '')
        vch_amt = header.get('VchAmt', 0)
        amt_words = header.get('AmountInWords', '')

        # Title & Label Selection
        if vch_type == 5:
            voucher_title = "STOCK TRANSFER / CHALLAN"
            copy_type = "ISSUE COPY"
            section_title = "JOB WORK ISSUE DETAILS / சலான் விபரங்கள்"
            from_box_title = "ISSUED FROM (மூலக் கிடங்கு):"
            to_box_title = "ISSUED TO (JOB WORKER / நபர்):"
        elif vch_type == 6:
            voucher_title = "STOCK TRANSFER / RECEIPT"
            copy_type = "RECEIPT COPY"
            section_title = "JOB WORK RECEIPT DETAILS / பெறப்பட்ட விபரங்கள்"
            from_box_title = "RECEIVED FROM (JOB WORKER / நபர்):"
            to_box_title = "RECEIVED IN (இலக்குக் கிடங்கு):"
        elif vch_type == 28 or "JNL" in vch_no:
            voucher_title = "STOCK JOURNAL"
            copy_type = "TRANSFER COPY"
            section_title = "STOCK JOURNAL DETAILS / கிடங்கு விபரங்கள்"
            from_box_title = "FROM MC (மூலக் கிடங்கு):"
            to_box_title = "TO MC (இலக்குக் கிடங்கு):"
        else:
            voucher_title = "STOCK TRANSFER"
            copy_type = "TRANSFER COPY"
            section_title = "MATERIAL CENTRE DETAILS / கிடங்கு விபரங்கள்"
            from_box_title = "FROM MC (மூலக் கிடங்கு):"
            to_box_title = "TO MC (இலக்குக் கிடங்கு):"

        # Build Items Rows
        item_rows = ""
        total_qty = 0.0
        total_amt = 0.0
        if items:
            for idx, it in enumerate(items, 1):
                q = abs(it.get('Qty', 0))
                p = abs(it.get('Price', 0))
                a = abs(it.get('Amount', 0))
                total_qty += q
                total_amt += a
                mcode = it.get('MasterCode1', 0)

                batches = item_batches.get(mcode, [])
                batch_sub = ""
                if batches:
                    batch_sub = '<div style="margin-top: 3px; font-size: 9.5px; color: #444; line-height: 1.35; padding-left: 4px;">'
                    for b in batches:
                        desc = f" [{b['BatchDesc']}]" if b.get('BatchDesc') else ""
                        batch_sub += f"<div>&bull; Bale {b['BatchNo']}: {b['BatchQty']:g} {it.get('UnitName', '')}{desc}</div>"
                    batch_sub += '</div>'

                rate_str = f"{p:,.2f}" if p > 0 else "-"
                amt_str = f"{a:,.2f}" if a > 0 else "-"

                item_rows += f'''
                <tr style="border-bottom: 1px solid #000; font-size: 11px;">
                    <td class="tbl-bordered" style="padding: 5px 4px; text-align: center; vertical-align: top;">{idx}</td>
                    <td class="tbl-bordered" style="padding: 5px 8px; vertical-align: top;">
                        <span style="font-weight: bold; font-size: 12px; color: #000;">{it.get('ItemName', '')}</span>
                        {batch_sub}
                    </td>
                    <td class="tbl-bordered" style="padding: 5px 4px; text-align: center; vertical-align: top;">{it.get('HSNCode') or '-'}</td>
                    <td class="tbl-bordered" style="padding: 5px 8px; text-align: right; vertical-align: top; font-weight: bold;">{q:,.2f}</td>
                    <td class="tbl-bordered" style="padding: 5px 4px; text-align: center; vertical-align: top;">{it.get('UnitName', 'Pcs.')}</td>
                    <td class="tbl-bordered" style="padding: 5px 8px; text-align: right; vertical-align: top;">{rate_str}</td>
                    <td class="tbl-bordered" style="padding: 5px 8px; text-align: right; vertical-align: top; font-weight: bold;">{amt_str}</td>
                </tr>
                '''

        spacer_count = max(0, 5 - len(items))
        spacer_rows = ""
        for _ in range(spacer_count):
            spacer_rows += '''<tr style="height: 22px; border-bottom: 1px solid #eee;">
                <td class="tbl-bordered">&nbsp;</td>
                <td class="tbl-bordered">&nbsp;</td>
                <td class="tbl-bordered">&nbsp;</td>
                <td class="tbl-bordered">&nbsp;</td>
                <td class="tbl-bordered">&nbsp;</td>
                <td class="tbl-bordered">&nbsp;</td>
                <td class="tbl-bordered">&nbsp;</td>
            </tr>'''

        amount_box_html = ""
        if total_amt > 0:
            amount_box_html = f'''
            <div style="padding: 6px 10px; border-bottom: 1.5px solid #000; background: #fafafa; font-size: 11px;">
                <strong>Total Amount Chargeable (in words):</strong><br>
                <span style="font-size: 12px; font-weight: bold; color: #1e3a8a;">{amt_words}</span>
            </div>
            '''

        html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{voucher_title} - {vch_no}</title>
    <style>
        {css}
    </style>
</head>
<body>
    <div class="action-bar no-print">
        <button onclick="window.history.back();" class="btn btn-back">&#x2190; Back</button>
        <button onclick="window.print();" class="btn btn-print">&#x1F5B6; Print / Save PDF</button>
        <button onclick="shareWhatsApp();" class="btn btn-wa">&#x1F4AC; WhatsApp</button>
    </div>

    <div class="mobile-hint no-print">&#x1F4F1; Swipe left/right to view full voucher &bull; Pinch to zoom</div>
    <div class="voucher-scroll-wrap">
        <div class="voucher-box">
        <!-- TOP TITLE & COPY BAR -->
        <table style="width: 100%; border-collapse: collapse; border-bottom: 1.5px solid #000; font-size: 11px;">
            <tr>
                <td style="padding: 4px 8px; width: 33%; font-weight: bold;">{f'GSTIN: {m["gstin"]}' if m['gstin'] else ''}</td>
                <td style="padding: 4px 8px; width: 34%; text-align: center; font-size: 13px; font-weight: bold; letter-spacing: 1px;">{voucher_title}</td>
                <td style="padding: 4px 8px; width: 33%; text-align: right; font-weight: bold;">({copy_type})</td>
            </tr>
        </table>

        <!-- COMPANY HEADER -->
        <div style="text-align: center; padding: 8px 12px; border-bottom: 1.5px solid #000;">
            <div style="font-size: 22px; font-weight: bold; letter-spacing: 0.5px;">{m['company_name']}</div>
            {f'<div style="font-size: 11px; font-weight: bold; margin-top: 2px;">{m["addr1"]}</div>' if m['addr1'] else ''}
            {f'<div style="font-size: 11px;">{m["addr2"]}</div>' if m['addr2'] else ''}
            <div style="font-size: 11px; margin-top: 3px;">
                {f'Mobile: <strong>{m["mobile"]}</strong> | ' if m['mobile'] else ''}State: <strong>TAMIL NADU (Code: 33)</strong>
            </div>
        </div>

        <!-- MATERIAL CENTRES (FROM MC / TO MC) & VOUCHER DETAILS -->
        <table style="width: 100%; border-collapse: collapse; border-bottom: 1.5px solid #000; font-size: 11px;">
            <tr>
                <!-- Left: Material Centre Transfer Route -->
                <td style="width: 55%; border-right: 1.5px solid #000; padding: 8px 10px; vertical-align: top;">
                    <div style="font-size: 10px; font-weight: bold; text-transform: uppercase; color: #475569; margin-bottom: 6px; letter-spacing: 0.5px;">
                        {section_title}
                    </div>

                    <!-- FROM BOX -->
                    <div style="margin-bottom: 8px; padding: 6px 9px; background: #fffbeb; border: 1px solid #fde68a; border-radius: 5px;">
                        <div style="font-size: 9.5px; font-weight: bold; color: #92400e; text-transform: uppercase; display: flex; align-items: center; gap: 4px;">
                            <span>&#x25B6;</span> {from_box_title}
                        </div>
                        <div style="font-size: 13px; font-weight: bold; color: #1e293b; margin-top: 2px;">
                            {from_mc}
                        </div>
                    </div>

                    <!-- TO BOX -->
                    <div style="padding: 6px 9px; background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 5px;">
                        <div style="font-size: 9.5px; font-weight: bold; color: #1e40af; text-transform: uppercase; display: flex; align-items: center; gap: 4px;">
                            <span>&#x25BC;</span> {to_box_title}
                        </div>
                        <div style="font-size: 13px; font-weight: bold; color: #1e293b; margin-top: 2px;">
                            {to_mc}
                        </div>
                    </div>
                </td>

                <!-- Right: Voucher & Transport Details -->
                <td style="width: 45%; padding: 8px 10px; vertical-align: top;">
                    <table style="width: 100%; border-collapse: collapse; font-size: 11px; line-height: 1.55;">
                        <tr>
                            <td style="width: 45%; font-weight: bold;">Voucher No.:</td>
                            <td style="font-weight: bold; font-size: 12px; color: #000;">{vch_no}</td>
                        </tr>
                        <tr>
                            <td style="font-weight: bold;">Dated:</td>
                            <td style="font-weight: bold;">{date}</td>
                        </tr>
                        {f'<tr><td style="font-weight: bold;">Series:</td><td style="font-weight: bold;">{series_name}</td></tr>' if series_name else ''}
                        <tr>
                            <td style="font-weight: bold;">Transport:</td>
                            <td>{transport.get('Transport') or '-'}</td>
                        </tr>
                        <tr>
                            <td style="font-weight: bold;">Vehicle No.:</td>
                            <td>{transport.get('VehicleNo') or '-'}</td>
                        </tr>
                        <tr>
                            <td style="font-weight: bold;">Station / Dest.:</td>
                            <td>{transport.get('Station') or '-'}</td>
                        </tr>
                        <tr>
                            <td style="font-weight: bold;">GR / RR No.:</td>
                            <td>{transport.get('GrNo') or '-'}</td>
                        </tr>
                    </table>
                </td>
            </tr>
        </table>

        <!-- ITEMS GRID (Standard Material Transfer Grid) -->
        <table class="tbl-bordered" style="width: 100%; border-collapse: collapse; border-bottom: 1.5px solid #000;">
            <thead>
                <tr style="background: #f4f4f4; border-bottom: 1.5px solid #000; font-size: 11px; font-weight: bold;">
                    <th class="tbl-bordered" style="padding: 5px; width: 5%; text-align: center;">S.N.</th>
                    <th class="tbl-bordered" style="padding: 5px 8px; text-align: left;">Description of Goods / பொருள் விபரம்</th>
                    <th class="tbl-bordered" style="padding: 5px 4px; width: 12%; text-align: center;">HSN/SAC</th>
                    <th class="tbl-bordered" style="padding: 5px 8px; width: 14%; text-align: right;">Quantity</th>
                    <th class="tbl-bordered" style="padding: 5px 4px; width: 8%; text-align: center;">Unit</th>
                    <th class="tbl-bordered" style="padding: 5px 8px; width: 12%; text-align: right;">Rate (&#x20B9;)</th>
                    <th class="tbl-bordered" style="padding: 5px 8px; width: 15%; text-align: right;">Amount (&#x20B9;)</th>
                </tr>
            </thead>
            <tbody>
                {item_rows}
                {spacer_rows}
            </tbody>
            <tfoot>
                <tr style="font-weight: bold; background: #f8fafc; font-size: 11px; border-top: 1.5px solid #000;">
                    <td class="tbl-bordered" colspan="3" style="padding: 6px 8px; text-align: right;">TOTAL:</td>
                    <td class="tbl-bordered" style="padding: 6px 8px; text-align: right; font-weight: bold;">{total_qty:,.2f}</td>
                    <td class="tbl-bordered" style="padding: 6px 4px; text-align: center;">Pcs.</td>
                    <td class="tbl-bordered" style="padding: 6px 8px; text-align: right;">-</td>
                    <td class="tbl-bordered" style="padding: 6px 8px; text-align: right; font-weight: bold;">{f'&#x20B9;{total_amt:,.2f}' if total_amt > 0 else '-'}</td>
                </tr>
            </tfoot>
        </table>

        <!-- AMOUNT IN WORDS (IF APPLICABLE) -->
        {amount_box_html}

        <!-- DECLARATION & TERMS -->
        <div style="padding: 6px 10px; font-size: 10px; color: #555; border-bottom: 1.5px solid #000; line-height: 1.4;">
            <strong>Declaration:</strong> Goods transferred for processing / job work / internal storage. Not for commercial sale.
        </div>

        <!-- 3 SIGNATURE BLOCKS -->
        <table style="width: 100%; font-size: 11px;">
            <tr>
                <td style="width: 33.3%; padding: 10px 8px; vertical-align: top; border-right: 1px solid #ccc; text-align: center;">
                    <div style="font-size: 10px; color: #666; margin-bottom: 35px;">Goods Dispatched By</div>
                    <div style="border-top: 1px dashed #666; display: inline-block; padding-top: 3px; font-weight: bold; font-size: 10px;">
                        Transfer In-Charge
                    </div>
                </td>
                <td style="width: 33.3%; padding: 10px 8px; vertical-align: top; border-right: 1px solid #ccc; text-align: center;">
                    <div style="font-size: 10px; color: #666; margin-bottom: 35px;">Goods Received In Good Condition</div>
                    <div style="border-top: 1px dashed #666; display: inline-block; padding-top: 3px; font-weight: bold; font-size: 10px;">
                        Receiver's Signature
                    </div>
                </td>
                <td style="width: 33.4%; padding: 10px 8px; vertical-align: top; text-align: center;">
                    <div style="font-weight: bold; font-size: 10px; margin-bottom: 35px;">{m['sign_for']}</div>
                    <div style="border-top: 1px dashed #666; display: inline-block; padding-top: 3px; font-weight: bold; font-size: 10px;">
                        {m['sign_title']}
                    </div>
                </td>
            </tr>
        </table>
        </div>
    </div>

    <script>
        function shareWhatsApp() {{
            const text = encodeURIComponent(
                "*{m['company_name']} - {voucher_title}*\n" +
                "Voucher No: {vch_no}\n" +
                "Date: {date}\n" +
                "From MC: {from_mc}\n" +
                "To MC: {to_mc}\n" +
                "Total Qty: {total_qty:,.2f} Pcs.\n" +
                "View: " + window.location.href
            );
            window.open("https://api.whatsapp.com/send?text=" + text, "_blank");
        }}
    </script>
</body>
</html>'''
        self.send_html(html)

    def handle_ledger_pdf(self, company, fy, code):
        conn = self.get_db()
        c = conn.cursor()

        # Party
        c.execute('''
            SELECT m.Code, m.Name, m.PrintName, m.OpBal, m.ParentGrp,
                   COALESCE(a.Address1, '') as Address1,
                   COALESCE(a.Address2, '') as Address2,
                   COALESCE(a.GSTNo, 'Unregistered') as GSTNo,
                   COALESCE(a.Mobile, '') as Mobile,
                   COALESCE(a.Email, '') as Email,
                   COALESCE(a.PinCode, '') as PinCode
            FROM busy_master1 m
            LEFT JOIN busy_masteraddressinfo a ON a.company_code = m.company_code AND a.fy_year = m.fy_year AND a.MasterCode = m.Code
            WHERE m.company_code = ? AND m.fy_year = ? AND m.Code = ?
        ''', (company, fy, code))
        party = dict(c.fetchone() or {})

        # Company
        c.execute('''
            SELECT name, print_name, gstin, mobile, email
            FROM busy_company
            WHERE company_code = ?
        ''', (company,))
        comp = dict(c.fetchone() or {})

        c.execute('''
            SELECT * FROM busy_print_config WHERE company_code = ?
        ''', (company,))
        p_row = c.fetchone()
        p_cfg = dict(p_row) if p_row else {}

        # Transactions
        c.execute('''
            SELECT t1.Date, t1.VchType, t1.VchNo, t2.Value1 as Amount
            FROM busy_tran2 t2
            JOIN busy_tran1 t1 ON t1.company_code = t2.company_code AND t1.fy_year = t2.fy_year AND t1.VchCode = t2.VchCode
            WHERE t2.company_code = ? AND t2.fy_year = ? AND t2.MasterCode1 = ? AND t2.RecType = 1
            ORDER BY t1.Date, t1.VchCode, t2.SrNo
        ''', (company, fy, code))

        rows = []
        op_bal = party.get('OpBal', 0.0) or 0.0
        curr_bal = op_bal
        total_dr = 0.0
        total_cr = 0.0

        for r in c.fetchall():
            vch_type = r['VchType']
            vch_no = r['VchNo'].strip() if r['VchNo'] else ''
            amt = r['Amount']
            vch_name = VCH_MAP.get(vch_type, f"Type {vch_type}")
            dr, cr, part = ledger_side(vch_type, amt)

            total_dr += dr
            total_cr += cr
            curr_bal += (dr - cr)
            b_type = "Dr" if curr_bal >= 0 else "Cr"
            rows.append({
                "Date": r['Date'],
                "VchType": vch_name,
                "VchNo": vch_no,
                "Particulars": part,
                "Debit": dr,
                "Credit": cr,
                "Balance": abs(curr_bal),
                "BalType": b_type
            })
        conn.close()

        tbl_rows = f'''
        <tr style="background: #eff6ff; font-weight: bold; border-bottom: 2px solid #bfdbfe;">
            <td style="padding: 9px 10px; color: #1e40af;">Opening Balance</td>
            <td>-</td>
            <td>-</td>
            <td style="color: #475569;">Brought Forward (B/F)</td>
            <td style="text-align: right; font-family: monospace; font-size: 13px;">{f"₹{abs(op_bal):,.2f}" if op_bal >= 0 and op_bal != 0 else "-"}</td>
            <td style="text-align: right; font-family: monospace; font-size: 13px;">{f"₹{abs(op_bal):,.2f}" if op_bal < 0 else "-"}</td>
            <td style="text-align: right; font-family: monospace; font-weight: bold; font-size: 13px;">₹{abs(op_bal):,.2f}</td>
            <td style="text-align: center; font-weight: bold; color: {'#15803d' if op_bal >= 0 else '#b91c1c'};">{'Dr' if op_bal >= 0 else 'Cr'}</td>
        </tr>
        '''
        for i, r in enumerate(rows):
            bg = "#ffffff" if i % 2 == 0 else "#f8fafc"
            pill_color = "#2563eb" if "Sales" in r['VchType'] else ("#16a34a" if "Receipt" in r['VchType'] else "#7c3aed")
            pill_bg = "#eff6ff" if "Sales" in r['VchType'] else ("#f0fdf4" if "Receipt" in r['VchType'] else "#f5f3ff")
            tbl_rows += f'''
            <tr style="background: {bg}; border-bottom: 1px solid #e2e8f0;">
                <td style="padding: 7px 10px; font-weight: 500; font-size: 12px; color: #334155;">{r['Date']}</td>
                <td style="padding: 7px 8px;">
                    <span style="background: {pill_bg}; color: {pill_color}; font-size: 10px; font-weight: bold; padding: 2px 7px; border-radius: 4px; border: 1px solid {pill_color}33; display: inline-block;">
                        {r['VchType']}
                    </span>
                </td>
                <td style="padding: 7px 8px; font-family: monospace; font-weight: 600; font-size: 11px; color: #475569;">{r['VchNo']}</td>
                <td style="padding: 7px 10px; color: #1e293b; font-weight: 500; font-size: 12px;">{r['Particulars']}</td>
                <td style="text-align: right; padding: 7px 10px; font-family: monospace; font-size: 12px; font-weight: 600; color: {'#0f172a' if r['Debit'] > 0 else '#94a3b8'};">
                    {f"₹{r['Debit']:,.2f}" if r['Debit'] > 0 else "-"}
                </td>
                <td style="text-align: right; padding: 7px 10px; font-family: monospace; font-size: 12px; font-weight: 600; color: {'#0f172a' if r['Credit'] > 0 else '#94a3b8'};">
                    {f"₹{r['Credit']:,.2f}" if r['Credit'] > 0 else "-"}
                </td>
                <td style="text-align: right; padding: 7px 10px; font-family: monospace; font-weight: bold; font-size: 12px; color: #0f172a;">
                    ₹{r['Balance']:,.2f}
                </td>
                <td style="text-align: center; padding: 7px 6px; font-weight: bold; font-size: 11px; color: {'#15803d' if r['BalType'] == 'Dr' else '#b91c1c'};">
                    {r['BalType']}
                </td>
            </tr>
            '''

        close_type = "Dr" if curr_bal >= 0 else "Cr"
        close_badge_bg = "#f0fdf4" if close_type == "Dr" else "#fef2f2"
        close_badge_color = "#166534" if close_type == "Dr" else "#991b1b"
        close_badge_border = "#bbf7d0" if close_type == "Dr" else "#fecaca"

        closing_words = amount_to_words(abs(curr_bal))
        stmt_date = datetime.now().strftime("%d-%b-%Y, %I:%M %p")

        comp_name = p_cfg.get('company_name') or comp.get('print_name') or comp.get('name') or company
        comp_addr1 = p_cfg.get('address_line1') or ""
        comp_addr2 = p_cfg.get('address_line2') or ""
        comp_gstin = comp.get('gstin') or ''
        comp_mobile = comp.get('mobile') or ""
        comp_email = comp.get('email') or ""

        bank_lines = []
        if p_cfg.get('bank1'):
            bank_lines.append(p_cfg['bank1'].replace(';', ' | ').replace(',', ' '))
        if p_cfg.get('bank2'):
            bank_lines.append(p_cfg['bank2'].replace(';', ' | ').replace(',', ' '))
        comp_bank_str = " &nbsp;|&nbsp; ".join(bank_lines) if bank_lines else "Bank details available on request"

        party_name = party.get('PrintName') or party.get('Name') or "Customer Account"
        party_address = f"{party.get('Address1', '')} {party.get('Address2', '')}".strip() or "Tamil Nadu, India"
        party_gstin = party.get('GSTNo') or "Unregistered"
        party_mobile = party.get('Mobile') or "N/A"

        wa_text = urllib.parse.quote(
            f"*{comp_name} - ACCOUNT STATEMENT*\n"
            f"Party: {party_name}\n"
            f"Period: FY {fy_label(fy)}\n"
            f"Total Bills (Debit): Rs. {total_dr:,.2f}\n"
            f"Total Paid (Credit): Rs. {total_cr:,.2f}\n"
            f"*Closing Balance: Rs. {abs(curr_bal):,.2f} {close_type}*\n"
            f"Please verify this ledger statement. Thank you!"
        )

        html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Account Ledger - {party_name} - {comp_name}</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
    <style>
        * {{ box-sizing: border-box; }}
        body {{
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            margin: 0;
            padding: 16px;
            background: #f1f5f9;
            color: #0f172a;
            font-size: 13px;
            line-height: 1.4;
        }}
        .action-bar {{
            max-width: 960px;
            margin: 0 auto 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: #ffffff;
            padding: 12px 16px;
            border-radius: 12px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.08);
            border: 1px solid #e2e8f0;
        }}
        .action-bar-left {{
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        .action-bar-right {{
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .btn {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 8px 14px;
            font-size: 12px;
            font-weight: 700;
            border-radius: 8px;
            text-decoration: none;
            cursor: pointer;
            border: none;
            transition: all 0.15s ease;
        }}
        .btn:active {{ transform: scale(0.97); }}
        .btn-back {{ background: #f1f5f9; color: #475569; border: 1px solid #cbd5e1; }}
        .btn-print {{ background: #005DA4; color: #ffffff; }}
        .btn-whatsapp {{ background: #16a34a; color: #ffffff; }}
        
        .statement-card {{
            max-width: 960px;
            margin: 0 auto;
            background: #ffffff;
            padding: 32px 36px;
            border-radius: 12px;
            box-shadow: 0 4px 16px rgba(0,0,0,0.06);
            border: 1px solid #e2e8f0;
            border-top: 6px solid #005DA4;
        }}

        /* Header */
        .company-header {{
            text-align: center;
            border-bottom: 2px solid #e2e8f0;
            padding-bottom: 14px;
            margin-bottom: 16px;
        }}
        .company-name {{
            font-size: 24px;
            font-weight: 800;
            color: #032C67;
            letter-spacing: 0.5px;
            margin-bottom: 2px;
        }}
        .company-sub {{
            font-size: 12px;
            color: #64748b;
            font-weight: 500;
        }}
        .statement-title-banner {{
            display: inline-block;
            background: #032C67;
            color: #ffffff;
            font-size: 13px;
            font-weight: 800;
            letter-spacing: 1px;
            padding: 4px 18px;
            border-radius: 20px;
            margin-top: 8px;
            text-transform: uppercase;
        }}
        .period-text {{
            font-size: 11px;
            font-weight: 600;
            color: #005DA4;
            margin-top: 4px;
        }}

        /* Party Details Grid */
        .meta-grid {{
            display: grid;
            grid-template-columns: 3fr 2fr;
            gap: 16px;
            background: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 10px;
            padding: 14px 16px;
            margin-bottom: 16px;
        }}
        .party-name-title {{
            font-size: 16px;
            font-weight: 800;
            color: #0f172a;
            margin-bottom: 4px;
        }}
        .meta-line {{
            font-size: 11.5px;
            color: #475569;
            margin-bottom: 2px;
        }}
        .closing-badge-box {{
            background: {close_badge_bg};
            border: 1px solid {close_badge_border};
            border-radius: 8px;
            padding: 8px 12px;
            text-align: right;
            display: flex;
            flex-direction: column;
            justify-content: center;
        }}
        .closing-badge-label {{
            font-size: 10px;
            font-weight: 700;
            text-transform: uppercase;
            color: #64748b;
        }}
        .closing-badge-val {{
            font-size: 18px;
            font-weight: 800;
            color: {close_badge_color};
            font-family: 'JetBrains Mono', monospace;
        }}

        /* Table */
        table.ledger-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 12px;
            margin-top: 8px;
        }}
        table.ledger-table th {{
            background: #032C67;
            color: #ffffff;
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            padding: 9px 8px;
            text-align: left;
            border: none;
        }}
        table.ledger-table td {{
            padding: 7px 8px;
            border-bottom: 1px solid #e2e8f0;
        }}
        table.ledger-table tfoot td {{
            font-weight: 800;
            background: #f1f5f9;
            border-top: 2px solid #0f172a;
            border-bottom: 2px solid #0f172a;
            padding: 9px 8px;
        }}

        /* Summary Cards */
        .summary-strip {{
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 10px;
            margin-top: 16px;
            background: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 10px;
            padding: 12px;
            text-align: center;
        }}
        .summary-col {{
            border-right: 1px solid #e2e8f0;
            padding: 0 8px;
        }}
        .summary-col:last-child {{ border-right: none; }}
        .summary-label {{
            font-size: 10px;
            font-weight: 700;
            text-transform: uppercase;
            color: #64748b;
            margin-bottom: 2px;
        }}
        .summary-val {{
            font-size: 14px;
            font-weight: 800;
            color: #0f172a;
            font-family: 'JetBrains Mono', monospace;
        }}

        /* In Words & Bank */
        .amount-words-box {{
            background: #fafafa;
            border-left: 4px solid #005DA4;
            padding: 8px 12px;
            margin-top: 14px;
            font-size: 11.5px;
            color: #334155;
            border-radius: 0 6px 6px 0;
        }}
        .bank-details-box {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-top: 1px dashed #cbd5e1;
            margin-top: 16px;
            padding-top: 12px;
            font-size: 11px;
            color: #64748b;
        }}

        /* Signatures */
        .signature-block {{
            display: flex;
            justify-content: space-between;
            align-items: flex-end;
            margin-top: 36px;
            padding-top: 12px;
        }}
        .sign-left {{
            font-size: 10.5px;
            color: #64748b;
            max-width: 380px;
            line-height: 1.35;
        }}
        .sign-right {{
            text-align: center;
            font-size: 12px;
            font-weight: 700;
            color: #0f172a;
        }}
        .sign-line {{
            margin-top: 48px;
            border-top: 1px solid #0f172a;
            padding-top: 4px;
            font-weight: 600;
        }}

        /* Print Media Rules */
        @media print {{
            body {{
                background: #ffffff;
                padding: 0;
                margin: 0;
                color: #000000;
                font-size: 11px;
            }}
            .action-bar {{ display: none !important; }}
            .statement-card {{
                box-shadow: none;
                border: none;
                border-top: none;
                padding: 0;
                max-width: 100%;
            }}
            table.ledger-table th {{
                background: #f1f5f9 !important;
                color: #000000 !important;
                border-bottom: 2px solid #000000 !important;
            }}
            table.ledger-table td {{
                border-bottom: 1px solid #cbd5e1 !important;
            }}
            .statement-title-banner {{
                background: #000000 !important;
                color: #ffffff !important;
                -webkit-print-color-adjust: exact;
                print-color-adjust: exact;
            }}
            tr {{ page-break-inside: avoid; }}
        }}
    </style>
</head>
<body>

    <!-- Top Action Bar -->
    <div class="action-bar">
        <div class="action-bar-left">
            <a href="javascript:history.back()" class="btn btn-back">⬅ Back to App</a>
            <span style="font-size: 12px; font-weight: 600; color: #64748b;">Ledger Statement Preview</span>
        </div>
        <div class="action-bar-right">
            <a href="https://api.whatsapp.com/send?text={wa_text}" target="_blank" class="btn btn-whatsapp">
                💬 WhatsApp Share
            </a>
            <button onclick="window.print()" class="btn btn-print">
                🖨️ Print / Save PDF
            </button>
        </div>
    </div>

    <!-- Main Statement Sheet -->
    <div class="statement-card">
        
        <!-- Company Header -->
        <div class="company-header">
            <div class="company-name">{comp_name}</div>
            <div class="company-sub">{comp_addr1} {comp_addr2}</div>
            <div class="company-sub" style="margin-top: 2px;">
                GSTIN: <strong>{comp_gstin}</strong> | Mobile: <strong>{comp_mobile}</strong> | Email: <strong>{comp_email}</strong>
            </div>
            <div>
                <div class="statement-title-banner">STATEMENT OF ACCOUNT</div>
            </div>
            <div class="period-text">
                Fiscal Year: {fy_label(fy)} | Statement Period: 01-Apr-{fy} to 31-Mar-{int(fy) + 1}
            </div>
        </div>

        <!-- Party Information Box -->
        <div class="meta-grid">
            <div>
                <div style="font-size: 10px; font-weight: 700; color: #64748b; text-transform: uppercase;">Account Details</div>
                <div class="party-name-title">M/s. {party_name}</div>
                <div class="meta-line">📍 {party_address}</div>
                <div class="meta-line">🏷️ GSTIN / UIN: <strong>{party_gstin}</strong> | 📞 Mobile: <strong>{party_mobile}</strong></div>
                <div class="meta-line" style="color: #005DA4; font-weight: 600;">Account Category: Sundry Debtors (வாடிக்கையாளர்)</div>
            </div>
            <div class="closing-badge-box">
                <div class="closing-badge-label">Net Outstanding Balance</div>
                <div class="closing-badge-val">₹{abs(curr_bal):,.2f} {close_type}</div>
                <div style="font-size: 10px; color: #64748b; margin-top: 2px;">Generated on {stmt_date}</div>
            </div>
        </div>

        <!-- Main Transactions Table -->
        <table class="ledger-table">
            <thead>
                <tr>
                    <th style="width: 10%;">Date</th>
                    <th style="width: 11%;">Vch Type</th>
                    <th style="width: 13%;">Vch No</th>
                    <th>Particulars / Account</th>
                    <th style="text-align: right; width: 14%;">Debit (₹ Dr)</th>
                    <th style="text-align: right; width: 14%;">Credit (₹ Cr)</th>
                    <th style="text-align: right; width: 14%;">Balance (₹)</th>
                    <th style="text-align: center; width: 6%;">Dr/Cr</th>
                </tr>
            </thead>
            <tbody>
                {tbl_rows}
            </tbody>
            <tfoot>
                <tr>
                    <td colspan="4" style="text-align: right; text-transform: uppercase;">Totals & Closing Balance:</td>
                    <td style="text-align: right; font-family: monospace; font-size: 13px; color: #0f172a;">₹{total_dr:,.2f}</td>
                    <td style="text-align: right; font-family: monospace; font-size: 13px; color: #0f172a;">₹{total_cr:,.2f}</td>
                    <td style="text-align: right; font-family: monospace; font-size: 13px; color: {close_badge_color};">₹{abs(curr_bal):,.2f}</td>
                    <td style="text-align: center; color: {close_badge_color};">{close_type}</td>
                </tr>
            </tfoot>
        </table>

        <!-- Summary Reconciliation Strip -->
        <div class="summary-strip">
            <div class="summary-col">
                <div class="summary-label">Opening Balance</div>
                <div class="summary-val">₹{abs(op_bal):,.2f} {('Dr' if op_bal >= 0 else 'Cr')}</div>
            </div>
            <div class="summary-col">
                <div class="summary-label">+ Total Debits (Bills)</div>
                <div class="summary-val" style="color: #0f172a;">₹{total_dr:,.2f}</div>
            </div>
            <div class="summary-col">
                <div class="summary-label">- Total Credits (Paid)</div>
                <div class="summary-val" style="color: #0f172a;">₹{total_cr:,.2f}</div>
            </div>
            <div class="summary-col">
                <div class="summary-label">= Net Closing Balance</div>
                <div class="summary-val" style="color: {close_badge_color};">₹{abs(curr_bal):,.2f} {close_type}</div>
            </div>
        </div>

        <!-- Amount In Words -->
        <div class="amount-words-box">
            <strong>Closing Balance in Words:</strong> {closing_words}
        </div>

        <!-- Bank Details for Remittance -->
        <div class="bank-details-box">
            <div>
                🏦 <strong>Bank Remittance Details:</strong> {comp_bank_str}
            </div>
            <div>
                UPI ID: <strong>{comp.get('bdep_id') or 'ttex'}@sbi</strong>
            </div>
        </div>

        <!-- Official Signatures -->
        <div class="signature-block">
            <div class="sign-left">
                <strong>Important Notice:</strong> Kindly verify this ledger statement immediately. Any discrepancies must be notified in writing within 7 days of receipt, failing which the balance shown above will be deemed final and confirmed. This is a computer-generated ledger extract (Kanakku).
            </div>
            <div class="sign-right">
                <div>For <strong>{comp_name}</strong></div>
                <div class="sign-line">Authorised Signatory</div>
            </div>
        </div>

    </div>

</body>
</html>'''
        self.send_html(html)

    def handle_outstanding_pdf(self, company, fy, out_type, route, billwise=False):
        conn = self.get_db()
        c = conn.cursor()
        if billwise:
            self._render_billwise_outstanding_pdf(c, company, fy, out_type, route)
            conn.close()
            return

        # Company
        c.execute('''
            SELECT name, print_name, gstin, mobile, email
            FROM busy_company
            WHERE company_code = ?
        ''', (company,))
        comp = dict(c.fetchone() or {})

        c.execute('''
            SELECT * FROM busy_print_config WHERE company_code = ?
        ''', (company,))
        p_row = c.fetchone()
        p_cfg = dict(p_row) if p_row else {}

        target_group = 116 if out_type == 'receivable' else 117
        title = "BILLS RECEIVABLE (DEBTORS OUTSTANDING)" if out_type == 'receivable' else "BILLS PAYABLE (CREDITORS OUTSTANDING)"
        route_sql = f"AND (';' || m.MasterSeriesGrp || ';') LIKE '%;{route};%'" if route > 0 else ""

        query = f'''
            SELECT
                m.Code,
                m.Name as PartyName,
                m.OpBal,
                COALESCE(addr.Address1, '') as City,
                COALESCE(addr.Mobile, '') as Mobile,
                COALESCE(s.TxnDr, 0) as TxnDr,
                COALESCE(s.TxnCr, 0) as TxnCr
            FROM busy_master1 m
            LEFT JOIN busy_masteraddressinfo addr ON addr.company_code = m.company_code AND addr.fy_year = m.fy_year AND addr.MasterCode = m.Code
            LEFT JOIN (
                SELECT MasterCode1,
                       SUM(CASE WHEN Value1 < 0 THEN ABS(Value1) ELSE 0 END) as TxnDr,
                       SUM(CASE WHEN Value1 > 0 THEN Value1 ELSE 0 END) as TxnCr
                FROM busy_tran2
                WHERE company_code = ? AND fy_year = ? AND RecType = 1
                GROUP BY MasterCode1
            ) s ON s.MasterCode1 = m.Code
            WHERE m.company_code = ? AND m.fy_year = ? AND m.MasterType = 2 AND {group_tree_sql()}
              {route_sql}
            ORDER BY m.Name
        '''
        c.execute(query, (company, fy, company, fy, target_group, company, fy))

        items = []
        total_out = 0.0
        for r in c.fetchall():
            if target_group == 117:
                bal = -r['OpBal'] + r['TxnCr'] - r['TxnDr']
                btype = 'Cr' if bal >= 0 else 'Dr'
            else:
                bal = r['OpBal'] + r['TxnDr'] - r['TxnCr']
                btype = 'Dr' if bal >= 0 else 'Cr'

            if abs(bal) > 1.0:
                items.append({
                    "PartyName": r['PartyName'],
                    "City": r['City'],
                    "Mobile": r['Mobile'],
                    "Amount": abs(bal),
                    "BalType": btype
                })
                total_out += bal  # net of advances, same as the outstanding API

        items.sort(key=lambda x: x['Amount'], reverse=True)
        conn.close()

        tbl_rows = ""
        for idx, it in enumerate(items, 1):
            tbl_rows += f"""
            <tr style="border-bottom: 1px solid #e2e8f0; font-size: 11px;">
                <td style="padding: 6px 8px; text-align: center; color: #64748b;">{idx}</td>
                <td style="padding: 6px 8px; font-weight: 600; color: #1e293b;">{it['PartyName']}</td>
                <td style="padding: 6px 8px; color: #475569;">{it['City']}</td>
                <td style="padding: 6px 8px; color: #475569;">{it['Mobile']}</td>
                <td style="padding: 6px 8px; text-align: right; font-weight: bold; color: {'#0f172a' if it['BalType'] == 'Dr' else '#dc2626'};">₹{it['Amount']:,.2f} <span style="font-size: 9px; padding: 1px 4px; border-radius: 3px; background: {'#f1f5f9' if it['BalType'] == 'Dr' else '#fef2f2'};">{it['BalType']}</span></td>
            </tr>
            """

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>{title} - {comp.get('name', 'Company')}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 20px; background: #f8fafc; font-size: 13px; color: #0f172a; }}
        .card {{ max-width: 900px; margin: 0 auto; background: #fff; padding: 24px; border-radius: 8px; border: 1px solid #e2e8f0; box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1); }}
        .action-bar {{ max-width: 900px; margin: 0 auto 16px; display: flex; gap: 12px; justify-content: flex-end; }}
        .btn {{ padding: 8px 16px; border-radius: 6px; cursor: pointer; border: none; font-weight: 600; text-decoration: none; font-size: 12px; display: inline-flex; align-items: center; gap: 6px; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
        th, td {{ padding: 6px 8px; border-bottom: 1px solid #e2e8f0; text-align: left; }}
        th {{ background: #f1f5f9; font-size: 11px; text-transform: uppercase; color: #475569; border-top: 1px solid #cbd5e1; border-bottom: 2px solid #cbd5e1; }}
        @media print {{ body {{ background: #fff; padding: 0; }} .action-bar {{ display: none; }} .card {{ border: none; padding: 0; max-width: 100%; box-shadow: none; }} }}
    </style>
</head>
<body>
    <div class="action-bar">
        <a href="javascript:history.back()" class="btn" style="background:#64748b; color:#fff;">⬅ Back</a>
        <button onclick="window.print()" class="btn" style="background:#002b49; color:#fff;">🖨️ Print / Save PDF</button>
        <a class="btn" style="background:#16a34a; color:#fff;" href="https://api.whatsapp.com/send?text={comp.get('name')}%20{title}%20Total:%20Rs.{total_out:,.2f}" target="_blank">📱 WhatsApp</a>
    </div>
    <div class="card">
        <div style="display: flex; justify-content: space-between; align-items: flex-start; border-bottom: 2px solid #002b49; padding-bottom: 12px; margin-bottom: 12px;">
            <div>
                <div style="font-size: 20px; font-weight: 800; color: #002b49; text-transform: uppercase;">{comp.get('print_name') or comp.get('name')}</div>
                <div style="color: #64748b; font-size: 11px;">GSTIN: {comp.get('gstin', 'N/A')} | Mobile: {comp.get('mobile', 'N/A')}</div>
                <div style="font-weight: bold; color: #0284c7; margin-top: 4px; text-transform: uppercase;">{title}</div>
            </div>
            <div style="text-align: right; font-size: 11px; color: #64748b;">
                <span style="background: #002b49; color: white; padding: 3px 8px; border-radius: 4px; font-weight: bold;">FY {fy_label(fy)}</span>
                <div style="margin-top: 6px;">Date: {datetime.now().strftime('%d-%m-%Y')}</div>
                <div>Total Parties: <b>{len(items)}</b></div>
            </div>
        </div>

        <table>
            <thead>
                <tr>
                    <th style="width: 30px; text-align: center;">#</th>
                    <th>Party Name</th>
                    <th style="width: 130px;">City / Station</th>
                    <th style="width: 110px;">Mobile</th>
                    <th style="width: 130px; text-align: right;">Pending Dues</th>
                </tr>
            </thead>
            <tbody>
                {tbl_rows}
            </tbody>
            <tfoot>
                <tr style="background: #f8fafc; font-weight: bold; border-top: 2px solid #002b49; border-bottom: 2px solid #002b49;">
                    <td colspan="4" style="padding: 8px; text-align: right; text-transform: uppercase;">Total Outstanding Dues:</td>
                    <td style="padding: 8px; text-align: right; color: #0f172a; font-size: 14px;">₹{total_out:,.2f}</td>
                </tr>
            </tfoot>
        </table>
    </div>
</body>
</html>"""
        self.send_html(html)

    def _render_billwise_outstanding_pdf(self, c, company, fy, out_type, route):
        """Bill-by-bill outstanding: every party's pending bills with age, plus an ageing summary.
        Amounts follow the party's normal side (Dr for debtors, Cr for creditors); advances show as the opposite side."""
        is_creditor = out_type != 'receivable'
        target_group = 117 if is_creditor else 116
        normal, other = ('Cr', 'Dr') if is_creditor else ('Dr', 'Cr')
        title = "BILL-WISE PAYABLES (CREDITORS)" if is_creditor else "BILL-WISE RECEIVABLES (DEBTORS)"
        as_of = datetime.now()

        c.execute('SELECT name, print_name, gstin, mobile FROM busy_company WHERE company_code = ?', (company,))
        comp = dict(c.fetchone() or {})

        route_sql = "AND (';' || m.MasterSeriesGrp || ';') LIKE ?" if route > 0 else ""
        route_args = (f'%;{route};%',) if route > 0 else ()
        c.execute(f'''
            SELECT m.Code, m.Name, m.OpBal, COALESCE(a.Address1, '') as City, COALESCE(a.Mobile, '') as Mobile,
                   COALESCE(s.TxnSum, 0) as TxnSum
            FROM busy_master1 m
            LEFT JOIN busy_masteraddressinfo a ON a.company_code = m.company_code AND a.fy_year = m.fy_year AND a.MasterCode = m.Code
            LEFT JOIN (SELECT MasterCode1, SUM(Value1) as TxnSum FROM busy_tran2
                       WHERE company_code = ? AND fy_year = ? AND RecType = 1 GROUP BY MasterCode1) s ON s.MasterCode1 = m.Code
            WHERE m.company_code = ? AND m.fy_year = ? AND m.MasterType = 2 AND {group_tree_sql()} {route_sql}
            ORDER BY m.Name
        ''', (company, fy, company, fy, target_group, company, fy) + route_args)
        parties = []
        for r in c.fetchall():
            dr_bal = r['OpBal'] - r['TxnSum']           # Debit-positive closing balance
            bal = -dr_bal if is_creditor else dr_bal    # positive = normal side
            if abs(bal) > 1.0:
                parties.append({'Code': r['Code'], 'Name': r['Name'], 'City': r['City'], 'Mobile': r['Mobile'], 'Bal': bal, 'Bills': []})
        by_code = {p['Code']: p for p in parties}

        c.execute('''
            SELECT MasterCode1, RefCode,
                   COALESCE(MAX(CASE WHEN Method = 1 THEN NULLIF(BillNo, '') END), MAX(NULLIF(BillNo, '')), 'Ref-' || RefCode) as BillNo,
                   COALESCE(MIN(CASE WHEN Method = 1 THEN NULLIF(BillDate, '') END), MIN(NULLIF(BillDate, '')), '') as BillDate,
                   COALESCE(MIN(CASE WHEN Method = 1 THEN NULLIF(DueDate, '') END), MIN(NULLIF(DueDate, '')), '') as DueDate,
                   SUM(Value1) as Pending
            FROM busy_tran3
            WHERE company_code = ? AND fy_year = ? AND RecType = 1
            GROUP BY MasterCode1, RefCode
            HAVING ABS(SUM(Value1)) > 0.5
            ORDER BY BillDate
        ''', (company, fy))
        for r in c.fetchall():
            p = by_code.get(r['MasterCode1'])
            if not p:
                continue
            amt = r['Pending'] if is_creditor else -r['Pending']
            try:
                days = (as_of - datetime.strptime(r['BillDate'][:10], '%Y-%m-%d')).days
            except (ValueError, TypeError):
                days = None
            p['Bills'].append({'BillNo': (r['BillNo'] or '').strip(), 'BillDate': r['BillDate'], 'DueDate': r['DueDate'],
                               'Days': days, 'Amt': amt})

        def slab_of(amt, days):
            if amt < 0 or days is None:
                return 'On Account'
            for limit, name in ((30, '0-30'), (60, '31-60'), (90, '61-90'), (180, '91-180')):
                if days <= limit:
                    return name
            return '180+'

        def fmt(v):
            side = normal if v >= 0 else other
            return f"&#x20B9;{abs(v):,.2f} {side}"

        ageing = {k: 0.0 for k in AGEING_SLABS}
        total = 0.0
        body = ""
        parties.sort(key=lambda p: p['Bal'], reverse=True)
        for idx, p in enumerate(parties, 1):
            total += p['Bal']
            billed = 0.0
            rows = ""
            for b in p['Bills']:
                billed += b['Amt']
                ageing[slab_of(b['Amt'], b['Days'])] += b['Amt']
                advance = b['Amt'] < 0
                rows += f'''<tr class="bill">
                    <td></td><td>{b['BillNo']}{' <i>(Advance)</i>' if advance else ''}</td><td>{b['BillDate'] or '-'}</td><td>{b['DueDate'] or '-'}</td>
                    <td style="text-align:right;">{'' if advance or b['Days'] is None else b['Days']}</td>
                    <td style="text-align:right;">{fmt(b['Amt'])}</td></tr>'''
            on_acc = p['Bal'] - billed
            ageing['On Account'] += on_acc  # always counted so the slabs add up to the total
            if abs(on_acc) > 0.5:
                rows +=f'''<tr class="bill"><td></td><td colspan="4"><i>On Account (not adjusted against bills)</i></td>
                    <td style="text-align:right;">{fmt(on_acc)}</td></tr>'''
            body += f'''<tr class="party"><td style="text-align:center;">{idx}</td>
                <td colspan="4">{p['Name']} <span class="sub">{p['City']} {p['Mobile']}</span></td>
                <td style="text-align:right;">{fmt(p['Bal'])}</td></tr>{rows}'''

        slab_cells = "".join(f'<td><div class="sub">{k}{"" if k == "On Account" else " days"}</div><b>{fmt(v)}</b></td>' for k, v in ageing.items())
        html = f'''<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} - {comp.get('name', '')}</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 16px; background: #f8fafc; color: #0f172a; font-size: 12px; }}
  .card {{ max-width: 960px; margin: 0 auto; background: #fff; padding: 20px; border: 1px solid #e2e8f0; border-radius: 8px; }}
  .bar {{ max-width: 960px; margin: 0 auto 12px; display: flex; gap: 10px; justify-content: flex-end; }}
  .btn {{ padding: 8px 14px; border-radius: 6px; border: none; font-weight: 600; font-size: 12px; cursor: pointer; text-decoration: none; color: #fff; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ background: #f1f5f9; font-size: 10px; text-transform: uppercase; color: #475569; padding: 6px; text-align: left; border-bottom: 2px solid #cbd5e1; }}
  td {{ padding: 4px 6px; }}
  tr.party td {{ font-weight: bold; background: #f8fafc; border-top: 1px solid #cbd5e1; }}
  tr.bill td {{ font-size: 11px; color: #334155; border-bottom: 1px dotted #e2e8f0; }}
  .sub {{ font-weight: normal; color: #64748b; font-size: 10px; }}
  .slabs td {{ border: 1px solid #e2e8f0; text-align: center; padding: 6px; }}
  @media print {{ body {{ background: #fff; padding: 0; }} .bar {{ display: none; }} .card {{ border: none; padding: 0; max-width: 100%; }} }}
</style></head><body>
<div class="bar">
  <a href="javascript:history.back()" class="btn" style="background:#64748b;">&#11013; Back</a>
  <button onclick="window.print()" class="btn" style="background:#002b49;">&#128424; Print / Save PDF</button>
</div>
<div class="card">
  <div style="display:flex; justify-content:space-between; border-bottom:2px solid #002b49; padding-bottom:10px; margin-bottom:10px;">
    <div>
      <div style="font-size:18px; font-weight:800; color:#002b49; text-transform:uppercase;">{comp.get('print_name') or comp.get('name', '')}</div>
      <div class="sub">GSTIN: {comp.get('gstin') or 'N/A'} | Mobile: {comp.get('mobile') or 'N/A'}</div>
      <div style="font-weight:bold; color:#0284c7; margin-top:4px;">{title}</div>
    </div>
    <div style="text-align:right;" class="sub">
      Ageing as on: <b>{as_of.strftime('%d-%m-%Y')}</b> (by bill date)<br>
      Parties: <b>{len(parties)}</b><br>
      Total: <b style="font-size:14px; color:#0f172a;">{fmt(total)}</b>
    </div>
  </div>
  <table class="slabs"><tr>{slab_cells}</tr></table>
  <table style="margin-top:12px;">
    <thead><tr><th style="width:30px;">#</th><th>Party / Bill No.</th><th style="width:90px;">Bill Date</th><th style="width:90px;">Due Date</th>
      <th style="width:50px; text-align:right;">Days</th><th style="width:140px; text-align:right;">Pending</th></tr></thead>
    <tbody>{body}</tbody>
    <tfoot><tr style="font-weight:bold; border-top:2px solid #002b49;"><td colspan="5" style="text-align:right; padding:8px;">TOTAL OUTSTANDING:</td>
      <td style="text-align:right; padding:8px; font-size:13px;">{fmt(total)}</td></tr></tfoot>
  </table>
</div></body></html>'''
        self.send_html(html)

class KanakkuHTTPServer(http.server.ThreadingHTTPServer):
    # The stdlib default (SO_REUSEADDR) lets a second process bind the same port on Windows and silently share
    # requests with the first. Bind exclusively so "port already in use" is detected.
    allow_reuse_address = False

    def server_bind(self):
        import socket
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(port, host='0.0.0.0'):
    """Bound HTTP server for the app (raises OSError if the port is busy). Used by __main__ and Kanakku.exe."""
    import functools
    handler = functools.partial(BusyApiHandler, directory=WEB_DIR)
    return KanakkuHTTPServer((host, port), handler)


if __name__ == '__main__':
    os.chdir(WEB_DIR)
    port = int(os.environ.get('PORT', '3000'))
    try:
        srv = make_server(port)
    except OSError as e:
        sys.exit(f"Error on port {port}: {e}")
    # Keep busy_local.db in step with BUSY (all year files of the configured companies)
    auto_sync_loop()
    # Browser / phone on the same network: http://<this-computer>:PORT (default 3000)
    print(f"Kanakku server listening at http://0.0.0.0:{port}")
    srv.serve_forever()
