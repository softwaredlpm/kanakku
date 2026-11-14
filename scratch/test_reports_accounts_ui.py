"""Smoke-test the Account Books <script> block in node with a stub DOM and fetch() backed by the real handlers."""
import os, sys, json, sqlite3, subprocess, tempfile, re
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import reports_accounts as ra
from urllib.parse import urlparse, parse_qs

conn = sqlite3.connect('file:D:/Busy Mobile App/busy_local.db?mode=ro', uri=True)
conn.row_factory = sqlite3.Row
html = open(os.path.join(ROOT, 'scratch', 'report_ui', 'accounts.html'), encoding='utf8').read()
script = re.search(r'<!-- SCRIPT -->\s*<script>([\s\S]*?)</script>\s*<!-- /SCRIPT -->', html).group(1)
ids = set(re.findall(r'id="([^"$]+)"', html.split('<!-- /VIEWS -->')[0]))

# pre-compute responses for the URLs the UI will request (default params)
calls = [('accounts_cash_book', {'code': ['0']}), ('accounts_cash_book', {'code': ['1']}),
         ('accounts_bank_book', {'code': ['0']}), ('accounts_daily_summary', {}),
         ('accounts_search', {'q': [''], 'limit': ['50']}), ('accounts_monthly_summary', {'code': ['1']}),
         ('accounts_register', {'type': ['sales']}), ('accounts_register', {'type': ['purchase']})]
fails = 0
for comp in ('COMP0044', 'COMP0027'):
    responses = {}
    for name, p in calls:
        key = name + '|' + json.dumps(p, sort_keys=True)
        responses[key] = ra.ROUTES['/api/reports/' + name](conn, comp, '2026', p)
    js = """
const ids = %s; const responses = %s; const els = {};
for (const id of ids) els[id] = { id, innerHTML: '', innerText: '', value: '', classList: { add(){}, remove(){} },
  remove(){}, insertAdjacentHTML(pos, h){ this.innerHTML += h; } };
globalThis.document = { getElementById: id => els[id] || null };
globalThis.currentCompany = '%s'; globalThis.currentFY = '2026';
globalThis.fmtINR = v => new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', minimumFractionDigits: 2 }).format(Math.abs(v || 0));
globalThis.fetch = async (url) => {
  const u = new URL(url, 'http://x'); const name = u.pathname.split('/').pop(); const p = {};
  for (const [k, v] of u.searchParams) if (!['company','fy','from','to'].includes(k)) p[k] = [v];
  const keys = Object.keys(p).sort(); const sp = {}; keys.forEach(k => sp[k] = p[k]);
  const key = name + '|' + JSON.stringify(sp).replace(/,/g, ', ').replace(/:/g, ': ');
  if (!(key in responses)) throw new Error('unexpected ' + key);
  return { ok: true, json: async () => responses[key] };
};
%s
(async () => {
  const out = {};
  await accountsLoadCashBook(); out.cash = els['accounts-cashbook-list'].innerHTML.length; out.cashSel = els['accounts-cashbook-code'].innerHTML.length;
  els['accounts-cashbook-code'].value = '1'; await accountsLoadCashBook(); out.cash1 = els['accounts-cashbook-list'].innerHTML.length;
  await accountsLoadBankBook(); out.bank = els['accounts-bankbook-list'].innerHTML.length;
  await accountsLoadDailySummary(); out.daily = els['accounts-daily-list'].innerHTML.length;
  accountsLoadMonthly(); await new Promise(r => setTimeout(r, 50)); out.picks = els['accounts-monthly-picks'].innerHTML.length;
  accountsState.monthlyCode = 1; await accountsLoadMonthlyData(); out.monthly = els['accounts-monthly-list'].innerHTML.length;
  await accountsLoadSalesRegister(); out.sales = els['accounts-register-list'].innerHTML.length; out.salesTitle = els['accounts-register-title'].innerText;
  await accountsLoadPurchaseRegister(); out.purch = els['accounts-register-months'].innerHTML.length; out.purchTitle = els['accounts-register-title'].innerText;
  const errs = Object.values(els).filter(e => /Failed to load/.test(e.innerHTML)).map(e => e.id + ': ' + e.innerHTML);
  out.errors = errs;
  console.log(JSON.stringify(out));
})().catch(e => { console.log('ERROR', e.stack); process.exit(1); });
""" % (json.dumps(sorted(ids)), json.dumps(responses), comp, script)
    f = tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf8')
    f.write(js); f.close()
    r = subprocess.run(['node', f.name], capture_output=True, text=True, encoding='utf8')
    os.unlink(f.name)
    print(comp, r.stdout.strip(), r.stderr.strip())
    if r.returncode or '"errors":[]' not in r.stdout:
        fails += 1
print('UI SMOKE:', 'PASSED' if not fails else 'FAILED')
