# Kanakku (கணக்கு)

**See your BUSY accounting data on your phone — reports, ledgers, outstanding, stock, GST — from your own computer or your own cloud.**

Kanakku is an open-source, read-only companion for businesses that use **BUSY** accounting software on Windows.
A small sync tool copies your company data from BUSY's data files into a local database, and a mobile-friendly web app
shows dashboards and BUSY-style display reports on top of it.

> **Not affiliated with BUSY Infotech.** "BUSY" is a trademark of its owner. Kanakku does not contain, modify or
> redistribute any BUSY software. It only reads the data files of a BUSY installation you already own, using the
> data password you provide. Your data stays on your computer (or in a cloud database you create and control).

## Features
- **Dashboard:** sales, purchase, receipts, payments, receivables, payables, cash & bank, top customers/items.
- **Accounts:** party ledgers, outstanding with bill-by-bill details and ageing slabs, cash book, bank book, day book,
  daily / monthly summaries, trial balance, group summary, profit & loss.
- **Vouchers:** every voucher type with BUSY-accurate item rates, discounts, bill sundries and taxes; printable
  invoices, challans, receipts and statements (signed share links).
- **Inventory:** stock status, item group / godown-wise stock, item stock ledger, batch (bale) stock and ledger,
  pending orders and challans, stock movement and slow-moving items.
- **GST:** GSTR-1 and GSTR-3B summaries, HSN summary.
- **All financial years:** every year file BUSY creates is synced automatically; pick the year in the app.
- **Read-only by design** for BUSY data: Kanakku never writes to BUSY's files.

## How it works
```
BUSY data files (C:\BusyWin\DATA\COMPxxxx\db1YYYY.bds)
        │  bds_sync.py  (read-only ODBC, only changed year files, every few minutes)
        ▼
busy_local.db (SQLite)  ──►  server.py (Python, no dependencies)  ──►  web app (BusyMobileWeb/index.html)
```
Every figure is checked against BUSY's own data; see `AI_DEVELOPER_GUIDE.md` for the data rules.

## Requirements
- Windows with BUSY installed and its data folder (the sync reads BUSY's `.bds` files).
- Microsoft Access Database Engine (64-bit ODBC driver "Microsoft Access Driver (*.mdb, *.accdb)").
- Python 3.10+ (standard library only).

## Quick start (local)
1. Edit `bds_sync_config.json`:
   ```json
   { "bds_root": "C:\\BusyWin\\DATA", "companies": ["COMP0001"], "auto_sync_minutes": 30 }
   ```
   `companies` are the folder names of your BUSY companies.
2. Store your BUSY data password once (encrypted with Windows DPAPI for your Windows user only):
   ```powershell
   powershell -ExecutionPolicy Bypass -File set_bds_password.ps1
   ```
3. First sync, then start the app:
   ```powershell
   py bds_sync.py
   py server.py
   ```
4. Open http://localhost:3000 on this computer, or http://<this-computer's-IP>:3000 from a phone on the same network.

The server keeps syncing in the background (`auto_sync_minutes`), and a new financial year appears in the app
as soon as BUSY creates it.

## Security notes
- Secrets never live in the code: the link-signing secret comes from the `APP_SECRET` environment variable or a
  generated, git-ignored `.app_secret` file; the BUSY password is DPAPI-encrypted per Windows user.
- Do not expose the server to the internet yet: user login is on the roadmap below.

## Roadmap
- [ ] User login (admin setup on first run, users, per-company access)
- [ ] Cloud mode: sync to your own **Turso** database and host the app on your own **Vercel** account / domain, so
      reports work even when the office PC is off
- [ ] Windows sync agent with a setup wizard and installer
- [ ] Mobile app wrapper (PWA / Android)

## Development
- `AI_DEVELOPER_GUIDE.md`: data model, BUSY sign conventions, verified rules, and how to test.
- Reports live in `reports_*.py` with UI snippets in `scratch/report_ui/`, wired in by `py scratch/integrate_reports.py`.
- Checks: `node scratch/validate_syntax.js`, `node scratch/test_all_home_buttons.js`, `py scratch/test_reports_<group>.py`.

## License
MIT (see `LICENSE`).
