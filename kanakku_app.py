"""Kanakku.exe entry point: first-run setup wizard, then the app server + background sync with a small control window.

  Kanakku.exe              open the control window (the setup wizard first, if not configured yet)
  Kanakku.exe --setup      open the setup wizard
  Kanakku.exe --headless   no window: run server + auto-sync (for a service / testing)

All data lives in %LOCALAPPDATA%\\Kanakku (database, config, secret); passwords are stored with Windows DPAPI.
"""
import json
import os
import socket
import subprocess
import sys
import threading
import webbrowser

# Data folder must be decided before server / bds_sync are imported (they read KANAKKU_HOME at import time).
if not os.environ.get('KANAKKU_HOME'):
    os.environ['KANAKKU_HOME'] = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'Kanakku')
HOME = os.environ['KANAKKU_HOME']
os.makedirs(HOME, exist_ok=True)

# The windowed exe has no console (sys.stdout / sys.stderr are None); send logs to a file instead.
if sys.stdout is None or sys.stderr is None:
    _log = open(os.path.join(HOME, 'kanakku.log'), 'a', encoding='utf-8', buffering=1)
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log

import bds_sync  # noqa: E402
import server  # noqa: E402

CONFIG = bds_sync.CONFIG_PATH
SET_PASSWORD = os.path.join(bds_sync.BASE_DIR, 'set_bds_password.ps1')
DEFAULT_BUSY_DATA = r'C:\BusyWin\DATA'
APP_TITLE = 'Kanakku'


# ---------------- config & secrets ----------------
def load_config():
    try:
        with open(CONFIG, encoding='utf-8-sig') as fh:  # -sig: accept a BOM (Notepad / PowerShell 5 write one)
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    with open(CONFIG, 'w', encoding='utf-8') as fh:
        json.dump(cfg, fh, indent=2)


def store_password(kind, password):
    """Encrypt with Windows DPAPI via set_bds_password.ps1; the password travels in the child's environment only."""
    env = dict(os.environ)
    env['KANAKKU_SQL_PWD' if kind == 'sql' else 'BUSY_BDS_PWD'] = password
    res = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', SET_PASSWORD, '-Kind', kind],
                         env=env, capture_output=True, text=True, creationflags=bds_sync.NO_WINDOW, timeout=60)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or 'Could not save the password')


def has_password(kind):
    name = 'sql_password.dat' if kind == 'sql' else 'bds_password.dat'
    return os.path.exists(os.path.join(os.environ.get('LOCALAPPDATA', ''), 'BusyMobileApp', name))


def busy_server_from_server_txt():
    """Server name only (never the password) from BUSY's own SQL setting, to pre-fill the wizard."""
    try:
        with open(r'C:\BusyWin\SYSTEM\Server.Txt', encoding='utf-8', errors='ignore') as fh:
            parts = fh.read().strip().split(';')
        if len(parts) >= 3 and parts[0].upper() == 'MSSQL':
            return parts[1].strip(), parts[2].strip()
    except OSError:
        pass
    return '', ''


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))  # no packet is sent; picks the LAN interface
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return '127.0.0.1'


def find_bds_companies(root):
    """[(folder, year_count)] for BUSY company folders under root (searched one level deeper too)."""
    out = []
    if not os.path.isdir(root):
        return out
    candidates = [root] + [os.path.join(root, d) for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]
    for base in candidates:
        for d in sorted(os.listdir(base)):
            p = os.path.join(base, d)
            if os.path.isdir(p) and any(n.lower() == 'db.bds' for n in os.listdir(p)):
                years = bds_sync.year_files(p)
                if years:
                    out.append((base, d, len(years)))
        if out:
            break
    return out


# ---------------- server ----------------
class Runtime:
    def __init__(self):
        self.srv = None
        self.port = None

    def start(self, port):
        os.chdir(server.WEB_DIR)
        self.srv = server.make_server(port)
        self.port = port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        server.auto_sync_loop()

    def url(self, local=True):
        return f"http://{'localhost' if local else lan_ip()}:{self.port}"


# ---------------- setup wizard ----------------
def run_wizard(parent=None):
    """Returns True when a configuration was saved."""
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    cfg = load_config()
    win = tk.Toplevel(parent) if parent else tk.Tk()
    win.title(f'{APP_TITLE} – Setup')
    win.geometry('560x620')
    saved = {'ok': False}

    frm = ttk.Frame(win, padding=14)
    frm.pack(fill='both', expand=True)
    ttk.Label(frm, text='Connect Kanakku to your BUSY data', font=('Segoe UI', 13, 'bold')).pack(anchor='w')
    ttk.Label(frm, text='Kanakku only reads BUSY data. Passwords are stored encrypted for this Windows user.',
              foreground='#555').pack(anchor='w', pady=(0, 10))

    kind = tk.StringVar(value='mssql' if cfg.get('mssql') and not cfg.get('companies') else 'bds')
    row = ttk.Frame(frm); row.pack(fill='x')
    ttk.Radiobutton(row, text='BUSY data files (.bds)', variable=kind, value='bds', command=lambda: show()).pack(side='left')
    ttk.Radiobutton(row, text='BUSY SQL Server', variable=kind, value='mssql', command=lambda: show()).pack(side='left', padx=16)

    # --- BDS panel ---
    bds = ttk.LabelFrame(frm, text='BUSY data folder', padding=8)
    bds_root = tk.StringVar(value=cfg.get('bds_root') or DEFAULT_BUSY_DATA)
    r1 = ttk.Frame(bds); r1.pack(fill='x')
    ttk.Entry(r1, textvariable=bds_root).pack(side='left', fill='x', expand=True)
    ttk.Button(r1, text='Browse…', command=lambda: (bds_root.set(filedialog.askdirectory(initialdir=bds_root.get()) or bds_root.get()), find_bds())).pack(side='left', padx=4)
    ttk.Label(bds, text='BUSY data password:').pack(anchor='w', pady=(8, 0))
    bds_pwd = ttk.Entry(bds, show='•'); bds_pwd.pack(fill='x')
    ttk.Label(bds, text='(leave empty to keep the saved password)' if has_password('bds') else '', foreground='#777').pack(anchor='w')

    # --- SQL panel ---
    sql = ttk.LabelFrame(frm, text='BUSY SQL Server', padding=8)
    mcfg = cfg.get('mssql') or {}
    txt_server, txt_user = busy_server_from_server_txt()
    sql_server = tk.StringVar(value=mcfg.get('server') or txt_server or r'localhost\BUSY')
    sql_auth = tk.StringVar(value=mcfg.get('auth') or ('sql' if txt_user else 'windows'))
    sql_user = tk.StringVar(value=mcfg.get('user') or txt_user)
    ttk.Label(sql, text='Server (e.g. 192.168.1.10,1433 or localhost\\BUSY):').pack(anchor='w')
    ttk.Entry(sql, textvariable=sql_server).pack(fill='x')
    r2 = ttk.Frame(sql); r2.pack(fill='x', pady=4)
    ttk.Radiobutton(r2, text='Windows login', variable=sql_auth, value='windows').pack(side='left')
    ttk.Radiobutton(r2, text='SQL login', variable=sql_auth, value='sql').pack(side='left', padx=12)
    ttk.Label(sql, text='SQL user / password (SQL login only):').pack(anchor='w')
    r3 = ttk.Frame(sql); r3.pack(fill='x')
    ttk.Entry(r3, textvariable=sql_user, width=18).pack(side='left')
    sql_pwd = ttk.Entry(r3, show='•'); sql_pwd.pack(side='left', fill='x', expand=True, padx=4)

    # --- companies ---
    comp_box = ttk.LabelFrame(frm, text='Companies to sync', padding=8)
    lst = tk.Listbox(comp_box, selectmode='multiple', height=9, exportselection=False)
    lst.pack(fill='both', expand=True)
    status = ttk.Label(frm, text='', foreground='#1d4ed8')
    found_codes = []

    def set_status(msg, error=False):
        status.config(text=msg, foreground='#b91c1c' if error else '#1d4ed8')
        win.update_idletasks()

    def fill(items, chosen):
        lst.delete(0, 'end'); found_codes.clear()
        for code, label in items:
            found_codes.append(code)
            lst.insert('end', label)
            if not chosen or code in chosen:
                lst.selection_set('end')

    def find_bds():
        items = find_bds_companies(bds_root.get())
        if items and items[0][0] != bds_root.get():
            bds_root.set(items[0][0])  # companies were one folder deeper
        fill([(d, f'{d}   ({n} year file{"s" if n != 1 else ""})') for _, d, n in items], cfg.get('companies') or [])
        set_status(f'{len(items)} BUSY companies found.' if items else 'No BUSY company folders (COMPxxxx with db.bds) here.', not items)

    def find_sql():
        try:
            if sql_auth.get() == 'sql' and sql_pwd.get():
                store_password('sql', sql_pwd.get())
            set_status('Connecting…')
            found = bds_sync.discover_sql({'server': sql_server.get().strip(), 'auth': sql_auth.get(), 'user': sql_user.get().strip()})
        except Exception as e:  # noqa: BLE001
            set_status(f'Could not connect: {str(e)[:160]}', True)
            return
        items = [(c, f"{c}   ({len(e['years'])} year database{'s' if len(e['years']) != 1 else ''})") for c, e in sorted(found.items())]
        fill(items, (cfg.get('mssql') or {}).get('companies') or [])
        set_status(f'{len(items)} BUSY SQL companies found.' if items else 'Connected, but no BusyCompNNNN_db databases found.', not items)

    find_btn = ttk.Button(frm, text='Find companies', command=lambda: find_bds() if kind.get() == 'bds' else find_sql())

    # --- port / sync ---
    opts = ttk.Frame(frm)
    port = tk.StringVar(value=str(cfg.get('port', 3000)))
    minutes = tk.StringVar(value=str(cfg.get('auto_sync_minutes', 10)))
    ttk.Label(opts, text='App port:').pack(side='left'); ttk.Entry(opts, textvariable=port, width=7).pack(side='left', padx=(2, 14))
    ttk.Label(opts, text='Sync every (minutes):').pack(side='left'); ttk.Entry(opts, textvariable=minutes, width=5).pack(side='left', padx=2)

    def show():
        for w in (bds, sql, find_btn, comp_box, opts, status, buttons):
            w.pack_forget()
        (bds if kind.get() == 'bds' else sql).pack(fill='x', pady=8)
        find_btn.pack(anchor='w')
        comp_box.pack(fill='both', expand=True, pady=8)
        opts.pack(fill='x')
        status.pack(anchor='w', pady=6)
        buttons.pack(fill='x', side='bottom')
        lst.delete(0, 'end'); found_codes.clear()

    def save():
        chosen = [found_codes[i] for i in lst.curselection()]
        if not chosen:
            set_status('Click "Find companies" and select at least one company.', True)
            return
        try:
            p, m = int(port.get()), int(minutes.get())
        except ValueError:
            set_status('Port and minutes must be numbers.', True)
            return
        new = {'port': p, 'auto_sync_minutes': m}
        try:
            if kind.get() == 'bds':
                if bds_pwd.get():
                    store_password('bds', bds_pwd.get())
                elif not has_password('bds'):
                    set_status('Enter the BUSY data password.', True)
                    return
                new.update({'bds_root': bds_root.get(), 'companies': chosen})
            else:
                if sql_auth.get() == 'sql':
                    if sql_pwd.get():
                        store_password('sql', sql_pwd.get())
                    elif not has_password('sql'):
                        set_status('Enter the SQL password.', True)
                        return
                mssql = {'server': sql_server.get().strip(), 'auth': sql_auth.get(), 'companies': chosen}
                if sql_auth.get() == 'sql':
                    mssql['user'] = sql_user.get().strip()
                new['mssql'] = mssql
        except Exception as e:  # noqa: BLE001
            set_status(f'Could not save the password: {e}', True)
            return
        save_config(new)
        saved['ok'] = True
        msg = 'Saved. Sync starts now; large companies can take a few minutes.'
        if parent and p != cfg.get('port', 3000):
            msg += f'\n\nThe new port {p} is used after Kanakku is restarted.'
        messagebox.showinfo(APP_TITLE, msg, parent=win)
        win.destroy()

    buttons = ttk.Frame(frm)
    ttk.Button(buttons, text='Save', command=save).pack(side='right')
    ttk.Button(buttons, text='Cancel', command=win.destroy).pack(side='right', padx=6)
    show()
    if kind.get() == 'bds':
        find_bds()
    elif mcfg.get('companies'):
        # Show the saved choice without connecting; "Find companies" lists the rest.
        fill([(c, f'{c}   (saved)') for c in mcfg['companies']], mcfg['companies'])
        set_status('Saved companies shown. Click "Find companies" to see all.')
    if parent:
        win.transient(parent); win.grab_set(); parent.wait_window(win)
    else:
        win.mainloop()
    return saved['ok']


# ---------------- control window ----------------
def run_control(rt):
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title(APP_TITLE)
    root.geometry('430x300')
    frm = ttk.Frame(root, padding=14); frm.pack(fill='both', expand=True)
    ttk.Label(frm, text='Kanakku is running', font=('Segoe UI', 13, 'bold')).pack(anchor='w')
    ttk.Label(frm, text=f'On this computer:  {rt.url()}').pack(anchor='w', pady=(6, 0))
    ttk.Label(frm, text=f'On phones (same Wi-Fi):  {rt.url(local=False)}').pack(anchor='w')
    sync_lbl = ttk.Label(frm, text='', foreground='#555', wraplength=400); sync_lbl.pack(anchor='w', pady=8)

    def refresh():
        st = server.SYNC_STATE
        msg = 'Syncing with BUSY…' if st['running'] else f"Last sync: {st['last_run'] or '-'}"
        if isinstance(st['last_result'], str) and st['last_result'].startswith('error'):
            msg += f"\n{st['last_result'][:200]}"
        elif st['last_result'] == 'no changes':
            msg += '  ·  BUSY data unchanged'
        elif isinstance(st['last_result'], list):
            bad = [r for r in st['last_result'] if r.get('status') == 'error']
            ok = [r for r in st['last_result'] if r.get('status') == 'ok']
            msg += f"  ·  {len(ok)} year(s) updated" + (f", {len(bad)} failed: {bad[0].get('detail', '')[:120]}" if bad else '')
        sync_lbl.config(text=msg)
        root.after(2000, refresh)

    btns = ttk.Frame(frm); btns.pack(fill='x', side='bottom')
    ttk.Button(btns, text='Open app', command=lambda: webbrowser.open(rt.url())).pack(side='left')
    ttk.Button(btns, text='Sync now', command=lambda: server.start_background_sync()).pack(side='left', padx=6)
    ttk.Button(btns, text='Settings', command=lambda: run_wizard(root) and server.start_background_sync()).pack(side='left')
    ttk.Button(btns, text='Quit', command=root.destroy).pack(side='right')
    ttk.Label(frm, text='Closing this window stops Kanakku.', foreground='#777').pack(side='bottom', anchor='w', pady=6)
    refresh()
    webbrowser.open(rt.url())
    root.mainloop()


def main(argv):
    headless = '--headless' in argv
    if '--setup' in argv or not os.path.exists(CONFIG):
        if headless:
            sys.exit(f'Not configured: create {CONFIG} first (or run Kanakku.exe without --headless).')
        if not run_wizard():
            return
    cfg = load_config()
    rt = Runtime()
    try:
        rt.start(int(cfg.get('port', 3000)))
    except OSError:
        msg = f"Port {cfg.get('port', 3000)} is already in use — Kanakku may already be running."
        if headless:
            sys.exit(msg)
        import tkinter as tk
        from tkinter import messagebox
        tk.Tk().withdraw()
        messagebox.showwarning(APP_TITLE, msg)
        webbrowser.open(f"http://localhost:{cfg.get('port', 3000)}")
        return
    if headless:
        print(f'Kanakku running at {rt.url()} (data: {HOME})', flush=True)
        threading.Event().wait()
    else:
        run_control(rt)


if __name__ == '__main__':
    main(sys.argv[1:])
