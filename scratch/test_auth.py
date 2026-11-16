"""Security tests for login / sessions / company access. Runs server.py on a COPY of busy_local.db (port 3061).
Usage: py scratch/test_auth.py
"""
import http.cookiejar
import http.server
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
tmp = tempfile.mkdtemp(prefix='kanakku_auth_')
db_copy = os.path.join(tmp, 'busy_local.db')
src = sqlite3.connect(os.path.join(ROOT, 'busy_local.db'))
dst = sqlite3.connect(db_copy)
src.backup(dst)
src.close()
dst.execute('DROP TABLE IF EXISTS app_users')  # start as a fresh install
dst.commit()
dst.close()

import server  # noqa: E402
server.DB_PATH = db_copy
os.chdir(server.WEB_DIR)
server.BusyApiHandler.log_message = lambda *a, **k: None
srv = http.server.ThreadingHTTPServer(('127.0.0.1', 3061), server.BusyApiHandler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = 'http://127.0.0.1:3061'
results = []


def check(name, cond, detail=''):
    results.append(bool(cond))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f'  ({detail})' if detail and not cond else ''))


class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def req(self, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        h = {'Content-Type': 'application/json'} if body is not None else {}
        h.update(headers or {})
        r = urllib.request.Request(BASE + path, data=data, headers=h, method='POST' if body is not None else 'GET')
        try:
            resp = self.op.open(r, timeout=120)
            code, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            code, raw = e.code, e.read()
        try:
            return code, json.loads(raw)
        except ValueError:
            return code, raw

    def cookie(self):
        return next((c.value for c in self.jar if c.name == 'kanakku_session'), None)


anon = Client()
print('== fresh install')
code, st = anon.req('/api/auth/status')
check('status says setup required', code == 200 and st.get('setup_required') is True)
check('API blocked without login (dashboard)', anon.req('/api/dashboard?company=COMP0044')[0] == 401)
check('API blocked without login (companies)', anon.req('/api/companies')[0] == 401)
check('API blocked without login (report)', anon.req('/api/reports/accounts_cash_book?company=COMP0044')[0] == 401)
check('token minting blocked without login', anon.req('/api/security/token?company=COMP0044&type=inv&id=125')[0] == 401)
tok = server.generate_secure_token('inv', 'COMP0044', 2026, '125')
code, _ = anon.req(f'/api/voucher/invoice-pdf?token={tok}')
check('signed PDF share link still opens without login', code == 200)
code, _ = anon.req('/api/voucher/invoice-pdf?token=' + tok[:-3] + 'AAA')
check('tampered PDF token rejected', code != 200 or b'Token' in (_ if isinstance(_, bytes) else b''), str(code))

print('== first-run setup')
check('weak password refused', anon.req('/api/auth/setup', {'username': 'owner', 'password': 'short'})[0] == 400)
admin = Client()
code, data = admin.req('/api/auth/setup', {'username': 'owner', 'password': 'Correct-Horse-1', 'display_name': 'Owner'})
check('admin created + signed in', code == 200 and data['user']['role'] == 'admin' and admin.cookie())
check('setup closed after first admin', anon.req('/api/auth/setup', {'username': 'x2', 'password': 'Another-Pass-1'})[0] == 403)
code, d = admin.req('/api/dashboard?company=COMP0044&fy=2026')
check('admin sees dashboard', code == 200 and d['sales']['count'] == 1834, str(code))
code, comps = admin.req('/api/companies')
check('admin sees all companies', code == 200 and len(comps) == 2)

print('== restricted user')
code, data = admin.req('/api/users/save', {'username': 'staff', 'password': 'Staff-Pass-22', 'role': 'user', 'companies': ['COMP0027']})
check('admin creates user', code == 200)
staff = Client()
check('staff wrong password refused', staff.req('/api/auth/login', {'username': 'staff', 'password': 'nope-nope'})[0] == 401)
code, _ = staff.req('/api/auth/login', {'username': 'staff', 'password': 'Staff-Pass-22'})
check('staff signs in', code == 200)
code, comps = staff.req('/api/companies')
check('staff sees only COMP0027', code == 200 and [c['company_code'] for c in comps] == ['COMP0027'], str(comps)[:80])
check('staff blocked from COMP0044 dashboard', staff.req('/api/dashboard?company=COMP0044')[0] == 403)
check('staff blocked from COMP0044 report', staff.req('/api/reports/analysis_pl?company=COMP0044')[0] == 403)
check('staff blocked from COMP0044 PDF token', staff.req('/api/security/token?company=COMP0044&type=inv&id=125')[0] == 403)
check('staff blocked from saving into COMP0044', staff.req('/api/party/save', {'company': 'COMP0044', 'name': 'X'})[0] == 403)
code, d = staff.req('/api/dashboard')
check('staff default company is their own', code == 200 and d['sales']['count'] == 111, str(code))
check('staff cannot list users', staff.req('/api/users')[0] == 403)
check('staff cannot create users', staff.req('/api/users/save', {'username': 'evil', 'password': 'Evil-Pass-123', 'role': 'admin'})[0] == 403)
check('staff cannot trigger sync', staff.req('/api/sync/run')[0] == 403)

print('== sessions')
forged = Client()
forged.jar.set_cookie(http.cookiejar.Cookie(0, 'kanakku_session', staff.cookie()[:-4] + 'abcd', None, False, '127.0.0.1', False, False,
                                            '/', True, False, None, False, None, None, {}))
check('tampered session cookie rejected', forged.req('/api/companies')[0] == 401)
old_cookie = staff.cookie()
code, _ = staff.req('/api/auth/password', {'current_password': 'wrong-one', 'new_password': 'New-Staff-Pass-9'})
check('password change needs current password', code == 403)
code, _ = staff.req('/api/auth/password', {'current_password': 'Staff-Pass-22', 'new_password': 'New-Staff-Pass-9'})
check('password changed', code == 200)
stale = Client()
stale.jar.set_cookie(http.cookiejar.Cookie(0, 'kanakku_session', old_cookie, None, False, '127.0.0.1', False, False,
                                           '/', True, False, None, False, None, None, {}))
check('old session dead after password change', stale.req('/api/companies')[0] == 401)
check('current device still signed in', staff.req('/api/companies')[0] == 200)
uid = next(u['id'] for u in admin.req('/api/users')[1]['users'] if u['username'] == 'staff')
admin.req('/api/users/save', {'id': uid, 'active': False})
check('disabled user is signed out', staff.req('/api/companies')[0] == 401)
check('disabled user cannot sign in', Client().req('/api/auth/login', {'username': 'staff', 'password': 'New-Staff-Pass-9'})[0] == 401)

print('== admin safety')
admin_id = next(u['id'] for u in admin.req('/api/users')[1]['users'] if u['username'] == 'owner')
check('cannot demote the last admin', admin.req('/api/users/save', {'id': admin_id, 'role': 'user'})[0] == 400)
check('cannot delete yourself', admin.req('/api/users/delete', {'id': admin_id})[0] == 400)
check('cross-site POST blocked', admin.req('/api/users/save', {'username': 'x9', 'password': 'Xxxxxxxx-9'}, {'Origin': 'https://evil.example'})[0] == 403)

print('== brute force')
bf = Client()
codes = [bf.req('/api/auth/login', {'username': 'owner', 'password': f'guess-{i}-xx'})[0] for i in range(10)]
check('login throttled after repeated failures', 429 in codes, str(codes))

print('== storage')
conn = sqlite3.connect(db_copy)
hashes = [r[0] for r in conn.execute('SELECT pass_hash FROM app_users')]
conn.close()
check('only PBKDF2 hashes stored, no plain passwords',
      all(h.startswith('pbkdf2_sha256$') and 'Correct-Horse-1' not in h and 'Staff' not in h for h in hashes))
code, raw = anon.req('/')
check('app page itself loads (login screen is in the page)', code == 200)

srv.shutdown()
shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{sum(results)}/{len(results)} passed" + ('' if all(results) else '  <-- FAILURES'))
sys.exit(0 if all(results) else 1)
