# Report module contract (for parallel report builders)

You build one group of BUSY-style "Display" reports for this project. Several builders work at the same time,
so you may ONLY create/edit the files named in your brief. Do NOT edit `server.py`, `BusyMobileWeb/index.html`,
`voucher_renderers.py`, or anything in `busy_local.db` (open it read-only:
`sqlite3.connect('file:D:/Busy Mobile App/busy_local.db?mode=ro', uri=True)`). Do NOT start, stop or kill any
server on ports 3000/80 and do NOT touch BUSY BDS files. The integrator wires your files into the app.

Read `AI_DEVELOPER_GUIDE.md` first (schema + sign conventions). Key facts, all verified against BUSY:

## Data rules
- Always filter `company_code = ? AND fy_year = ?` (companies `COMP0044`, `COMP0027`; fy `2026`) and, for vouchers,
  `busy_tran1.Cancelled = 0` (cancelled vouchers have no lines anyway).
- Accounts: `busy_master1` MasterType 2. Groups: MasterType 1, `ParentGrp` chain (groups have sub-groups; walk the
  tree with a recursive CTE). Key groups: 111 Cash-in-hand, 112 Bank Accounts, 128 Bank O/D, 116 Sundry Debtors,
  117 Sundry Creditors, 102 Current Assets, 103 Current Liabilities, 109 Revenue Accounts; primary groups have ParentGrp 0.
- `busy_master1.OpBal` = account opening balance, **positive = Debit**.
- `busy_tran2` RecType 1 = accounting lines; `Value1` **negative = Debit, positive = Credit**.
  Closing (Dr+) = `OpBal - SUM(Value1)`. Every voucher's RecType 1 lines sum to 0.
- Item lines: `busy_tran2` RecType 2 (stock-affecting: Value1 = qty in main unit, + in / - out; MasterCode2 = material
  centre; Value3 = net amount), RecType 7 = sales-invoice lines billed against a delivery challan (NOT stock-affecting),
  RecType 4 = order lines (VchType 12/13, not stock-affecting). `Value2` = alternate-unit qty, never a rate.
  `busy_item_ext` (VchCode, SrNo, RecType) gives `Rate` (per main unit) and `GrossAmt` (before bill-sundry discounts).
- Item master: MasterType 6; `CM1` = main unit code (unit name = busy_master1 row with that Code); `ParentGrp` = item
  group (MasterType 5). Material centres: MasterType 11.
- Opening stock: `busy_tran4` per item (MasterCode1) and material centre (MasterCode2): D1 qty, D3 value.
- Bill sundries: `busy_tran2` RecType 3, Value3 = amount (always positive); `busy_sundry_info.Additive` 1 = add, 0 = subtract.
- Bill-by-bill: `busy_tran3` RecType 1 (RefCode per bill, Method 1 = new ref, 2 = against ref, Value1 signed like Tran2).
  RecType 3 = item-level references (Method 1 = order/challan created, 2 = adjusted against it), RecType 4 also references.
- GST: `busy_gst_itemwise` (BUSY's own VchGSTSumItemWise): TaxableAmt, TaxRate/TaxAmt and TaxRate1/TaxAmt1, HSNCode, GSTIN, VchType...
- Voucher types (VchType): 2 Purchase, 3 Sales Return, 4 Purchase Challan, 5 Stock Transfer, 6 Production,
  8 Stock Journal, 9 Sales, 10 Purchase Return, 11 Delivery (Sales) Challan, 12 Sales Order, 13 Purchase Order,
  14 Receipt, 15 Contra, 16 Journal, 17 Debit Note, 18 Credit Note, 19 Payment, 28 Salary.
- Dates are TEXT `YYYY-MM-DD`. Tran1.VchAmt = voucher total. VchNo has leading spaces: `.strip()` it.

## Backend file: `reports_<group>.py` (project root)
- Pure functions, standard library only, no import of `server`.
- Export `ROUTES = {'/api/reports/<name>': handler, ...}`.
- `def handler(conn, company, fy, params) -> dict` where `conn` is a sqlite3 connection with `row_factory = sqlite3.Row`
  and `params` is the parsed query string (`dict[str, list[str]]`, e.g. `params.get('code', ['0'])[0]`).
  Return a JSON-serialisable dict. Never write to the DB. Use bound parameters, never string-format user input into SQL.
- Optional filters you support must have safe defaults (e.g. whole FY 2026-04-01..today).

## Frontend file: `scratch/report_ui/<group>.html`
Two marked sections the integrator pastes into `index.html`:
```
<!-- VIEWS -->
  ...one <div id="view-<id>" class="hidden space-y-3"> per report...
<!-- /VIEWS -->
<!-- SCRIPT -->
<script> ...functions... </script>
<!-- /SCRIPT -->
<!-- TILES -->
  ...tile list: one line per report: <id>|<label>|<font-awesome class>|<loader function name>
<!-- /TILES -->
```
- Copy the look of the existing views in `BusyMobileWeb/index.html` (e.g. `view-daybook`, `view-outstanding`):
  Tailwind classes, a header row with `<button onclick="closeReportView()">` Back, mobile-first (~390px wide).
- Use existing globals only: `currentCompany`, `currentFY`, `fmtINR(n)`. Fetch with
  `fetch(`/api/reports/<name>?company=${currentCompany}&fy=${currentFY}&...`)`.
- Rule 5 of the guide: null-guard every `document.getElementById(...)`. No recursion into `switchTab`.
- Prefix every function and id with your group name to avoid clashes with other builders.
- The loader function (from TILES) takes no arguments and renders the view (the integrator un-hides the view, then calls it).

## Verification (required, report the numbers)
Write a test script in `scratch/` that imports your module, runs every handler for BOTH companies against the real DB,
and cross-checks totals with independent SQL (e.g. a Cash Book's closing = OpBal - SUM(Value1) for that account;
a register's total = SUM(VchAmt) for that type; Dr == Cr where it must). Also run `node -e` syntax check of your
`<script>` block. Report: files created, endpoints + params, and the verification output.
