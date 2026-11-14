"""Smoke-test the Registers <script> block in node with a stub DOM; fetch() is answered by the real handlers
(each request runs this script with --serve <url>, which calls reports_registers.ROUTES against the read-only DB)."""
import os, sys, json, sqlite3, subprocess, tempfile, re
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

if len(sys.argv) > 2 and sys.argv[1] == '--serve':
    from urllib.parse import urlparse, parse_qs
    import reports_registers as rr
    conn = sqlite3.connect('file:D:/Busy Mobile App/busy_local.db?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    u = urlparse(sys.argv[2])
    q = parse_qs(u.query)
    fn = rr.ROUTES[u.path]
    sys.stdout.write(json.dumps(fn(conn, q['company'][0], q['fy'][0], q)))
    sys.exit(0)

html = open(os.path.join(ROOT, 'scratch', 'report_ui', 'registers.html'), encoding='utf8').read()
script = re.search(r'<!-- SCRIPT -->\s*<script>([\s\S]*?)</script>\s*<!-- /SCRIPT -->', html).group(1)
ids = set(re.findall(r'id="([^"$]+)"', html.split('<!-- /VIEWS -->')[0]))
me = os.path.abspath(__file__)
fails = 0
for comp in ('COMP0044', 'COMP0027'):
    js = """
const { execFileSync } = require('child_process');
const ids = %s; const els = {};
for (const id of ids) els[id] = { id, innerHTML: '', innerText: '', value: '', classList: { add(){}, remove(){}, toggle(){} },
  remove(){}, insertAdjacentHTML(pos, h){ this.innerHTML += h; } };
els['registers-movement-days'].value = '90';
globalThis.document = { getElementById: id => els[id] || null };
globalThis.currentCompany = '%s'; globalThis.currentFY = '2026';
globalThis.fmtINR = v => new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', minimumFractionDigits: 2 }).format(Math.abs(v || 0));
const urls = [];
globalThis.fetch = async (url) => {
  urls.push(url);
  const out = execFileSync('py', [%s, '--serve', url], { maxBuffer: 1 << 28, encoding: 'utf8' });
  return { ok: true, json: async () => JSON.parse(out) };
};
let opened = 0; globalThis.openVoucherModal = () => { opened++; };
%s
(async () => {
  const out = {};
  await registersLoadVoucherRegister(); out.journal = els['registers-voucher-list'].innerHTML.length; out.jTitle = els['registers-voucher-title'].innerText;
  await registersSetVType('stock_transfer'); out.trf = els['registers-voucher-list'].innerHTML.length; out.trfMore = /registers-voucher-more/.test(els['registers-voucher-list'].innerHTML);
  registersVoucherMore(); out.trf2 = els['registers-voucher-list'].innerHTML.length;
  await registersSetVType('salary'); out.salary = els['registers-voucher-list'].innerHTML.length;
  await registersSetVType('receipt'); out.receipt = els['registers-voucher-summary'].innerHTML.length;
  registersOpenVch(5); out.opened = opened;
  await registersLoadChallanRegister(); out.dc = els['registers-challan-list'].innerHTML.length;
  await registersSetCType('so'); await registersSetCStatus('partial'); out.soPartial = (els['registers-challan-list'].innerHTML.match(/Partly cleared/g) || []).length;
  await registersSetCStatus('all');
  await registersLoadPartyItemSales(); out.pis = els['registers-partyitem-list'].innerHTML.length;
  const m = els['registers-partyitem-list'].innerHTML.match(/registersPickPi\\((\\d+)\\)/);
  if (m) { await registersPickPi(Number(m[1])); out.pisDrill = els['registers-partyitem-list'].innerHTML.length; out.pisHead = /All parties/.test(els['registers-partyitem-summary'].innerHTML); }
  await registersSetPiMode('item'); out.byItem = els['registers-partyitem-list'].innerHTML.length;
  await registersLoadPartyItemPurchase(); out.pip = els['registers-partyitem-title'].innerText;
  await registersLoadMovement(); out.mv = els['registers-movement-list'].innerHTML.length;
  const mi = els['registers-movement-list'].innerHTML.match(/registersPickMvItem\\((\\d+)\\)/);
  if (mi) { await registersPickMvItem(Number(mi[1])); out.mvDetail = els['registers-movement-detail'].innerHTML.length; }
  await registersLoadSlowMoving(); out.slow = els['registers-movement-list'].innerHTML.length; out.slowTitle = els['registers-movement-title'].innerText;
  out.errors = Object.values(els).filter(e => /Failed to load/.test(e.innerHTML)).map(e => e.id + ': ' + e.innerHTML.slice(0, 200));
  out.urls = urls.length;
  console.log(JSON.stringify(out));
})().catch(e => { console.log('ERROR', e.stack); process.exit(1); });
""" % (json.dumps(sorted(ids)), comp, json.dumps(me), script)
    f = tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf8')
    f.write(js); f.close()
    r = subprocess.run(['node', f.name], capture_output=True, text=True, encoding='utf8')
    os.unlink(f.name)
    print(comp, r.stdout.strip(), r.stderr.strip()[:500])
    try:
        res = json.loads(r.stdout.strip())
        ok = not res['errors'] and res['journal'] > 0 and res['mv'] > 0
    except Exception:
        ok = False
    print('  ', 'OK' if ok else 'FAIL')
    fails += 0 if ok else 1
print('RESULT:', 'ALL OK' if not fails else f'{fails} FAILED')
