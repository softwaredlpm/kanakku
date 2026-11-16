"""Manage Kanakku users from the command line (works on the computer that holds busy_local.db).

  py manage_users.py list
  py manage_users.py add <username> [--admin] [--companies COMP0001,COMP0002]
  py manage_users.py password <username>          # reset a forgotten password (also logs that user out everywhere)
  py manage_users.py companies <username> COMP0001,COMP0002
  py manage_users.py disable <username> | enable <username> | delete <username>

Passwords are asked interactively and never shown or stored in plain text.
"""
import getpass
import os
import sqlite3
import sys

import auth

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'busy_local.db')


def ask_password():
    p1 = getpass.getpass('New password: ')
    p2 = getpass.getpass('Repeat password: ')
    if p1 != p2:
        sys.exit('Passwords do not match.')
    return p1


def need_user(conn, username):
    u = auth.get_user(conn, username=username)
    if not u:
        sys.exit(f'No such user: {username}')
    return u


def main(argv):
    if not argv:
        sys.exit(__doc__)
    conn = sqlite3.connect(DB)
    auth.ensure_schema(conn)
    cmd = argv[0]
    try:
        if cmd == 'list':
            for u in auth.list_users(conn):
                print(f"{u['username']:20} {u['role']:6} {'active' if u['active'] else 'disabled':8} "
                      f"companies={'ALL' if u['role'] == 'admin' else ','.join(u['companies']) or '-'} "
                      f"last_login={u['last_login_at'] or '-'}")
        elif cmd == 'add':
            username = argv[1]
            role = 'admin' if '--admin' in argv else 'user'
            comps = argv[argv.index('--companies') + 1].split(',') if '--companies' in argv else []
            auth.create_user(conn, username, ask_password(), role=role, companies=comps)
            print(f'Created {role} {username}')
        elif cmd == 'password':
            u = need_user(conn, argv[1])
            auth.update_user(conn, u['id'], password=ask_password())
            print(f"Password changed for {u['username']}; old sessions are logged out.")
        elif cmd == 'companies':
            u = need_user(conn, argv[1])
            auth.update_user(conn, u['id'], companies=[c for c in argv[2].split(',') if c])
            print('Updated.')
        elif cmd in ('disable', 'enable'):
            u = need_user(conn, argv[1])
            auth.update_user(conn, u['id'], active=(cmd == 'enable'))
            print(f'{cmd}d {u["username"]}')
        elif cmd == 'delete':
            u = need_user(conn, argv[1])
            auth.delete_user(conn, u['id'])
            print(f'Deleted {u["username"]}')
        else:
            sys.exit(__doc__)
    except auth.AuthError as e:
        sys.exit(f'Error: {e}')
    except IndexError:
        sys.exit(__doc__)
    finally:
        conn.close()


if __name__ == '__main__':
    main(sys.argv[1:])
