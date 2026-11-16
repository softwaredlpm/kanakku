"""Kanakku user accounts and sessions (standard library only).

- Passwords: PBKDF2-HMAC-SHA256 with a random 16-byte salt; only the hash is stored.
- Sessions: stateless signed cookie "kanakku_session" = base64(user_id:token_version:expiry):HMAC(APP_SECRET).
  No server-side session table, so it works the same on a single PC and on serverless hosting.
  Changing a password, disabling a user or "log out everywhere" bumps token_version, which invalidates old cookies.
- Roles: 'admin' (manage users, all companies) and 'user' (only the companies listed in `companies`).
- First run: while no user exists, /api/auth/setup creates the first admin; afterwards it is closed.
"""
import base64
import hashlib
import hmac
import json
import re
import secrets
import time

COOKIE_NAME = 'kanakku_session'
SESSION_DAYS = 30
PBKDF2_ITERATIONS = 200_000
USERNAME_RE = re.compile(r'^[A-Za-z0-9_.@-]{3,40}$')
MIN_PASSWORD = 8

SCHEMA = '''
CREATE TABLE IF NOT EXISTS app_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT DEFAULT '',
    pass_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    companies TEXT NOT NULL DEFAULT '[]',
    active INTEGER NOT NULL DEFAULT 1,
    token_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_login_at TEXT
);
'''


class AuthError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def ensure_schema(conn):
    conn.executescript(SCHEMA)


# ---------- passwords ----------
def hash_password(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, PBKDF2_ITERATIONS)
    return f'pbkdf2_sha256${PBKDF2_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}'


def verify_password(password, stored):
    try:
        algo, iters, salt_b64, hash_b64 = stored.split('$')
        if algo != 'pbkdf2_sha256':
            return False
        dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), base64.b64decode(salt_b64), int(iters))
        return hmac.compare_digest(dk, base64.b64decode(hash_b64))
    except (ValueError, TypeError):
        return False


def check_password_rules(password):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise AuthError(f'Password must be at least {MIN_PASSWORD} characters.')
    if len(password) > 200:
        raise AuthError('Password is too long.')


def check_username(username):
    if not isinstance(username, str) or not USERNAME_RE.match(username):
        raise AuthError('Username: 3-40 letters, digits or . _ @ -')


# ---------- users ----------
def _row_to_user(row):
    if not row:
        return None
    u = dict(zip(['id', 'username', 'display_name', 'pass_hash', 'role', 'companies', 'active', 'token_version',
                  'created_at', 'last_login_at'], row))
    try:
        u['companies'] = json.loads(u['companies'] or '[]')
    except ValueError:
        u['companies'] = []
    return u


USER_COLS = 'id, username, display_name, pass_hash, role, companies, active, token_version, created_at, last_login_at'


def user_count(conn):
    return conn.execute('SELECT COUNT(*) FROM app_users').fetchone()[0]


def get_user(conn, user_id=None, username=None):
    if user_id is not None:
        row = conn.execute(f'SELECT {USER_COLS} FROM app_users WHERE id = ?', (user_id,)).fetchone()
    else:
        row = conn.execute(f'SELECT {USER_COLS} FROM app_users WHERE username = ?', (username,)).fetchone()
    return _row_to_user(row)


def public_user(u):
    if not u:
        return None
    return {k: u[k] for k in ('id', 'username', 'display_name', 'role', 'companies', 'active', 'created_at', 'last_login_at')}


def list_users(conn):
    return [public_user(_row_to_user(r)) for r in conn.execute(f'SELECT {USER_COLS} FROM app_users ORDER BY username')]


def create_user(conn, username, password, role='user', companies=None, display_name=''):
    check_username(username)
    check_password_rules(password)
    if role not in ('admin', 'user'):
        raise AuthError('Role must be admin or user.')
    if get_user(conn, username=username):
        raise AuthError('That username already exists.')
    conn.execute('INSERT INTO app_users (username, display_name, pass_hash, role, companies) VALUES (?, ?, ?, ?, ?)',
                 (username, display_name or username, hash_password(password), role, json.dumps(companies or [])))
    conn.commit()
    return get_user(conn, username=username)


def update_user(conn, user_id, role=None, companies=None, active=None, display_name=None, password=None):
    u = get_user(conn, user_id=user_id)
    if not u:
        raise AuthError('User not found.', 404)
    sets, args, bump = [], [], False
    if role is not None:
        if role not in ('admin', 'user'):
            raise AuthError('Role must be admin or user.')
        sets.append('role = ?'); args.append(role); bump = bump or role != u['role']
    if companies is not None:
        sets.append('companies = ?'); args.append(json.dumps(list(companies)))
    if active is not None:
        sets.append('active = ?'); args.append(1 if active else 0); bump = bump or not active
    if display_name is not None:
        sets.append('display_name = ?'); args.append(display_name)
    if password is not None:
        check_password_rules(password)
        sets.append('pass_hash = ?'); args.append(hash_password(password)); bump = True
    if bump:
        sets.append('token_version = token_version + 1')
    if not sets:
        return u
    # never leave the system without an active admin
    if (role == 'user' or active is False) and u['role'] == 'admin' and _active_admins(conn, exclude=user_id) == 0:
        raise AuthError('At least one active admin is required.')
    conn.execute(f'UPDATE app_users SET {", ".join(sets)} WHERE id = ?', args + [user_id])
    conn.commit()
    return get_user(conn, user_id=user_id)


def delete_user(conn, user_id):
    u = get_user(conn, user_id=user_id)
    if not u:
        raise AuthError('User not found.', 404)
    if u['role'] == 'admin' and _active_admins(conn, exclude=user_id) == 0:
        raise AuthError('At least one active admin is required.')
    conn.execute('DELETE FROM app_users WHERE id = ?', (user_id,))
    conn.commit()


def revoke_sessions(conn, user_id):
    conn.execute('UPDATE app_users SET token_version = token_version + 1 WHERE id = ?', (user_id,))
    conn.commit()


def _active_admins(conn, exclude=None):
    return conn.execute("SELECT COUNT(*) FROM app_users WHERE role = 'admin' AND active = 1 AND id <> ?",
                        (exclude or -1,)).fetchone()[0]


def authenticate(conn, username, password):
    u = get_user(conn, username=username or '')
    # same work whether or not the user exists, so timing does not reveal valid usernames
    ok = verify_password(password or '', u['pass_hash'] if u else hash_password('x' * 12))
    if not u or not ok or not u['active']:
        raise AuthError('Wrong username or password.', 401)
    conn.execute('UPDATE app_users SET last_login_at = CURRENT_TIMESTAMP WHERE id = ?', (u['id'],))
    conn.commit()
    return u


# ---------- session cookie ----------
def make_session(secret, user, days=SESSION_DAYS):
    exp = int(time.time()) + days * 86400
    payload = f"{user['id']}:{user['token_version']}:{exp}"
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f'{payload}:{sig}'.encode()).decode().rstrip('=')


def read_session(conn, secret, token):
    """Return the active user for a session token, or None."""
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token + '=' * (-len(token) % 4)).decode()
        uid, ver, exp, sig = raw.split(':')
        expect = hmac.new(secret, f'{uid}:{ver}:{exp}'.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expect) or int(exp) < time.time():
            return None
        u = get_user(conn, user_id=int(uid))
        if not u or not u['active'] or int(ver) != int(u['token_version']):
            return None
        return u
    except (ValueError, TypeError, UnicodeDecodeError):
        return None


def cookie_header(token, secure, max_age=SESSION_DAYS * 86400):
    attrs = [f'{COOKIE_NAME}={token}', 'Path=/', 'HttpOnly', 'SameSite=Lax', f'Max-Age={max_age}']
    if secure:
        attrs.append('Secure')
    return '; '.join(attrs)


def clear_cookie_header(secure):
    return cookie_header('', secure, max_age=0)


def can_access_company(user, company):
    return bool(user) and (user['role'] == 'admin' or company in (user.get('companies') or []))


# ---------- login throttling (per process; enough to slow down guessing) ----------
_FAILS = {}


def throttle_check(key, limit=8, window=600):
    now = time.time()
    fails = [t for t in _FAILS.get(key, []) if now - t < window]
    _FAILS[key] = fails
    if len(fails) >= limit:
        raise AuthError('Too many attempts. Try again in a few minutes.', 429)


def throttle_fail(key):
    _FAILS.setdefault(key, []).append(time.time())


def throttle_reset(key):
    _FAILS.pop(key, None)
