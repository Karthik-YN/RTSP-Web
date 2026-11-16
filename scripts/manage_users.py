#!/usr/bin/env python3
"""Manage dashboard users (stored hashed in <RTSPM_DATA_DIR>/users).

  manage_users.py add alice --role operator      # prompts for the password
  manage_users.py add bob --role viewer --password-stdin < pw.txt
  manage_users.py remove alice
  manage_users.py list

Run as the service account so the file keeps the right owner:
  sudo -u rtspmgr /opt/rtsp-manager/backend/.venv/bin/python /opt/rtsp-manager/scripts/manage_users.py list
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.config import Settings  # noqa: E402
from app.services.auth import ROLES, UserStore  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="add or update a user")
    a.add_argument("username")
    a.add_argument("--role", choices=ROLES, default="operator", help="admin, operator or viewer")
    a.add_argument("--password-stdin", action="store_true")
    r = sub.add_parser("remove", help="remove a user")
    r.add_argument("username")
    sub.add_parser("list", help="list users")
    args = p.parse_args()

    store = UserStore(Settings().users_path)
    if args.cmd == "add":
        if args.password_stdin:
            pw = sys.stdin.readline().rstrip("\n")
        else:
            pw = getpass.getpass(f"Password for {args.username}: ")
            if pw != getpass.getpass("Repeat: "):
                print("passwords do not match", file=sys.stderr)
                return 1
        try:
            store.set_user(args.username, args.role, pw)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"user '{args.username}' saved with role {args.role} in {store.path}")
    elif args.cmd == "remove":
        if not store.delete_user(args.username):
            print(f"no user '{args.username}'", file=sys.stderr)
            return 1
        print(f"removed '{args.username}'")
    else:
        for u in store.list_users():
            print(f"{u.username}\t{u.role}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
