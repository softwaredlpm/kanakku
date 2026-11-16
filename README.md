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

## Easiest: Kanakku.exe (Windows)
1. Run `Kanakku.exe`. The first time, a setup window asks for:
   - **BUSY data files**: the BUSY data folder (default `C:\BusyWin\DATA`) and the BUSY data password, **or**
   - **BUSY SQL Server**: the server (pre-filled from BUSY's own setting), Windows or SQL login.
   Click **Find companies**, tick the companies, Save.
2. Kanakku syncs, opens the app in your browser and shows the address for phones on the same Wi-Fi.
   The first visit asks you to create the administrator account.
3. Data lives in `%LOCALAPPDATA%\Kanakku`; passwords are encrypted for your Windows user (DPAPI).
   `Kanakku.exe --setup` reopens setup; `Kanakku.exe --headless` runs without a window.

Build it yourself: `py -m pip install --user pyinstaller` then `powershell -File build_exe.ps1` → `dist\Kanakku.exe`.

## Quick start (local, from source)
1. Copy `bds_sync_config.example.json` to `bds_sync_config.json` and edit it:
   ```json
   { "bds_root": "C:\\BusyWin\\DATA", "companies": ["COMP0001"], "auto_sync_minutes": 30 }
   ```
   `companies` are the folder names of your BUSY companies.

   **BUSY SQL edition** (data in SQL Server, databases `BusyComp0001_db`, `BusyComp0001_db12025`, …): add
   ```json
   "mssql": { "server": "192.168.1.10,1433", "auth": "sql", "user": "kanakku_reader", "companies": [] }
   ```
   (`auth` is `windows` or `sql`; an empty `companies` list means every `BusyCompNNNN` found). For SQL logins store the
   password once with `powershell -ExecutionPolicy Bypass -File set_bds_password.ps1 -Kind sql`. Use a **read-only**
   SQL login; Kanakku only runs SELECTs. A year is re-read only when its data checksum changes.
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
   The first visit shows **Create the administrator account**; after that everyone signs in.
   Add staff from **More › Users** and choose which companies each one can see.

### Users from the command line
```powershell
py manage_users.py list
py manage_users.py add <username> [--admin] [--companies COMP0001,COMP0002]
py manage_users.py password <username>     # reset a forgotten password
py manage_users.py disable <username>
```

The server keeps syncing in the background (`auto_sync_minutes`), and a new financial year appears in the app
as soon as BUSY creates it.

## Security notes
- **Login required** for every page of data. Passwords are stored only as PBKDF2-SHA256 hashes; sessions are signed
  HttpOnly cookies (30 days) that stop working when the password is changed, the user is disabled, or the user
  chooses "Sign out everywhere". Repeated wrong passwords are throttled.
- **Per-company access:** admins see every company; users see only the companies an admin gives them.
- **Shared PDF links** (invoice, ledger, outstanding…) open without a login on purpose so you can send them to a
  customer; each link is HMAC-signed for one document and cannot be altered to open another.
- Secrets never live in the code: the signing secret comes from the `APP_SECRET` environment variable or a
  generated, git-ignored `.app_secret` file; the BUSY password is DPAPI-encrypted per Windows user.
- On the internet, always serve it over HTTPS (cookies are marked `Secure` behind an HTTPS proxy).

## Roadmap
- [x] User login (admin setup on first run, users, per-company access)
- [ ] Cloud mode: sync to your own **Turso** database and host the app on your own **Vercel** account / domain, so
      reports work even when the office PC is off
- [x] Windows app (`Kanakku.exe`) with a setup wizard for BUSY data files or BUSY SQL Server
- [ ] Installer and run-at-Windows-startup option
- [ ] Mobile app wrapper (PWA / Android)

## Development
- `AI_DEVELOPER_GUIDE.md`: data model, BUSY sign conventions, verified rules, and how to test.
- Reports live in `reports_*.py` with UI snippets in `scratch/report_ui/`, wired in by `py scratch/integrate_reports.py`.
- Checks: `node scratch/validate_syntax.js`, `py scratch/test_auth.py` (login & access rules, on a copy of the DB),
  `py scratch/test_reports_<group>.py`.

## License
MIT (see `LICENSE`).
