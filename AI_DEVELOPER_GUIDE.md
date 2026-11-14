# AI DEVELOPER GUIDE (For Claude, Codex, Antigravity, & Cursor)

> **CRITICAL NOTICE FOR AI AGENTS**:
> Read this document completely before modifying any code in this repository. It contains strict architectural rules, domain-specific BUSY accounting invariants, and known pitfalls.

---

## 1. Persona & Tone
- The user communicates in **Tamil** (தமிழ்) or Tanglish ("tamil la mattum pesu", "step by step ah pannu").
- Maintain polite, clear, and step-by-step explanations in Tamil.
- Never guess or approximate accounting values; always rely on SQL queries to `busy_local.db`.

---

## 2. Invariant Rules (DO NOT BREAK)

### Rule 1: Real BDS Data Only (Zero Placeholders)
- Never use placeholder numbers like `1000`, `999`, or fake names like `Test Item 1`.
- If an endpoint returns 0, verify the company code, financial year (`/api/fys`) and date range against `busy_local.db`.

### Rule 2: No third-party app code
- The project is "Kanakku". Never add, decompile or publish any BUSY / third-party app (APKs, patched builds, vendor login emulation). The only UI is `BusyMobileWeb/index.html`, served by `server.py` on port 3000 (or `$PORT`).

### Rule 3: AST & Syntax Check Before Answering
- Vanilla JavaScript in `index.html` must remain 100% syntactically valid.
- Run:
```powershell
node scratch/validate_syntax.js
```
- If a syntax error is introduced, the browser halts execution of the entire `<script>` block, causing all buttons to freeze.

### Rule 4: Navigation Safety (No Recursion)
- `switchTab(tabId)` must **NOT** call a function that recursively calls `switchTab()`.
- To hide report views when switching tabs, hide their containers directly:
```javascript
['view-ledger', 'view-outstanding', 'view-daybook', 'view-trialbalance', 'view-stock', 'view-bcn-stock', 'view-parameter-stock', 'view-batch-stock'].forEach(v => {
    const el = document.getElementById(v);
    if (el) el.classList.add('hidden');
});
```

### Rule 5: DOM Null Guards
- Never write: `document.getElementById('someId').innerText = ...;`
- Always write: `const el = document.getElementById('someId'); if (el) el.innerText = ...;` or `document.getElementById('someId')?.innerText`.

---

## 3. Database Schema Cheat Sheet (`busy_local.db`)

### `busy_master1` (Masters Directory)
- `Code` (INTEGER, Primary Key): Unique master ID.
- `MasterType` (verified against BDS): `1` = Account Group, `2` = Account / Party, `5` = Item Group, `6` = Item, `8` = Unit, `11` = Material Centre / Godown, `15` = Bill of Material (CM1 = finished item, CM2 = unit), `21` = Voucher Series (first 2 digits of Name = VchType).
- `ParentGrp`: Group code. `116` = Sundry Debtors, `117` = Sundry Creditors, `111` = Cash, `112` = Bank, `128` = Bank OD.
  **Groups have sub-groups** (e.g. `JANAKI(CUSTOMER)` under 116). Always use `group_tree_sql()` / `group_account_codes()` in `server.py`, never `ParentGrp = 116`.
- `Name`: Master name.
- `PrintName`: Name printed on vouchers.
- `HSNCode`: Real HSN code (extracted from BDS `CM1` / `CM2`).
- `OpBal`: Account opening balance, **positive = Debit**. Source is BDS `Folio1.D1` negated (BUSY stores Debit as negative). Loaded by `bds_sync.py`. `Master1.D1` is NOT the opening balance.

### Sign conventions
- `busy_tran2.Value1` for accounts (RecType 1): **negative = Debit, positive = Credit**. Closing (Dr+) = `OpBal - SUM(Value1)`.
- Item lines (RecType 2): `Value1` = qty in main unit (+ in / − out), `Value2` = qty in alternate unit (NOT a rate), `Value3` = amount. `MasterCode2` = Material Centre.
- Item opening stock: `busy_tran4` per item + material centre (`D1` qty, `D2` alt qty, `D3` value). Stock maths lives in `compute_item_stock()`.
- Batches: `busy_tran3` RecType 2 (`BillNo` = batch no, `Value1` qty, `MasterCode2` = MC). RecType 3/4 = order/challan references.
- Bill-by-bill: `busy_tran3` RecType 1, one `RefCode` per bill. Method 1 = new ref (the bill itself, or an advance), 2 = against ref. `Value1` signed like Tran2 (negative = Dr); a bill's pending = signed SUM over its RefCode. VchCode 0 = opening bills. Ageing: `compute_bill_ageing()`.
- Item lines as BUSY prints them: rate `busy_item_ext.Rate` (D2) and gross `GrossAmt` (D5); bill sundries are signed by `busy_sundry_info.Additive` (Master1.I1). Gross + signed sundries = Tran1.VchAmt for every voucher. Loaded by `bds_sync.py`.
- VchType 4 = Purchase Challan (PURC-RCVD / MODEL-RC / RETURN-S series), 10 = Purchase Return, 3 = Sales Return.
- BDS `ItemParamDet` is empty for these companies: there is no parameter-wise stock in BUSY; the "Parameter Stock" view shows BOM variants with their finished item's stock.

### `busy_tran1` (Voucher Headers)
- `VchCode` (INTEGER, Primary Key): Unique transaction ID.
- `VchType` (INTEGER): Voucher type (9=Sales, 2/10=Purchase, 14=Receipt, 19=Payment, etc.).
- `Date` (TEXT: `YYYY-MM-DD`): Voucher date.
- `VchNo` (TEXT): Voucher / Invoice number.
- `MasterCode1`: Primary party account code.
- `MasterCode2`: Secondary account or Material Centre.
- `VchAmt` (REAL): Net voucher amount.

### `busy_tran2` (Voucher Line Items)
- `VchCode`: Foreign key to `busy_tran1.VchCode`.
- `MasterCode1`: Item Code (MasterType=1).
- `Qty` (REAL): Quantity.
- `Unit` (TEXT): Measurement unit (`Pcs.`, `Mtr.`, `Kgs.`, etc.).
- `Rate` (REAL): Rate per unit.
- `Amount` (REAL): Line total (`Qty * Rate`).

---

## 3a. Sync & Financial Years (bds_sync.py)
- `bds_sync.py` is the only BUSY -> `busy_local.db` sync. It reads every year file `db1YYYY.bds` (= FY 1 Apr YYYY .. 31 Mar YYYY+1) of each company listed in `bds_sync_config.json` into `fy_year = YYYY`, via the read-only ODBC exporter `bds_sync_export.ps1`.
- A year is re-read only when its file size/mtime changed (`busy_sync_state`). `py bds_sync.py --force` re-reads everything; `--company`, `--fy`, `--db` narrow it.
- `server.py` runs it at start-up and every `auto_sync_minutes`; `/api/sync/run`, `/api/sync/status`.
- Password: `set_bds_password.ps1` (DPAPI, current Windows user) or `$env:BUSY_BDS_PWD`. Never store it in plain text.
- App-created masters/vouchers (codes above BUSY's highest) survive a re-sync; BUSY rows are replaced.
- Every per-year table has `fy_year`; **every query must filter `company_code` AND `fy_year`** (VchCode / Code repeat across years). `/api/fys` lists a company's years; a missing `fy` param means the latest year. Never hardcode a year or a company code.
- To add a company: add its folder name (e.g. `COMP0011`) to `bds_sync_config.json` and run a sync.

## 3b. BUSY "Display" Reports (reports_*.py)
- `reports_accounts.py`, `reports_inventory.py`, `reports_analysis.py`, `reports_gst.py` each export `ROUTES = {path: handler(conn, company, fy, params) -> dict}`; `server.py` loads them into `REPORT_ROUTES` (a broken module is skipped, never fatal).
- UI lives in `scratch/report_ui/<group>.html` (VIEWS / SCRIPT / TILES) and is wired into `index.html` by `py scratch/integrate_reports.py [group]` (idempotent, between `REPORTS:<group>` markers). Edit the snippet, re-run the script, then copy index.html to the APK. Tiles call `openExtraReport(viewId, loader)`.
- Contract and data rules: `scratch/report_modules_contract.md`. Each group has `scratch/test_reports_<group>.py`.
- GST data comes from `busy_gst_itemwise` (loaded by `bds_sync.py`).

## 4. How to Verify Any Bug Fix

Always run these automated checks after editing code:

```powershell
# 1. Check JavaScript syntax
node scratch/validate_syntax.js

# 2. Check all Home page buttons and navigation in real headless Chrome
node scratch/test_all_home_buttons.js

# 3. Check live backend API response
py scratch/print_dash_values.py
```
