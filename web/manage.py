"""Manage logins from the command line.

    python -m web.manage add <username> [--admin]
    python -m web.manage passwd <username>
    python -m web.manage list
    python -m web.manage remove <username>

The password is prompted for, so it never lands in shell history. For an
unattended first boot, set ELBASH_ADMIN_USER and ELBASH_ADMIN_PASSWORD and run
"python -m web.manage bootstrap".
"""

import argparse
import getpass
import os
import sys

from . import auth


def _ask_password() -> str:
    first = getpass.getpass("סיסמה: ")
    if first != getpass.getpass("אימות סיסמה: "):
        raise SystemExit("הסיסמאות אינן תואמות.")
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="web.manage", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="create a user")
    add.add_argument("username")
    add.add_argument("--admin", action="store_true")

    passwd = sub.add_parser("passwd", help="change a password")
    passwd.add_argument("username")

    remove = sub.add_parser("remove", help="delete a user")
    remove.add_argument("username")

    sub.add_parser("list", help="list users")
    sub.add_parser("bootstrap", help="create the first user from the environment")

    args = parser.parse_args(argv)
    auth.init_db()

    try:
        if args.command == "add":
            auth.create_user(args.username, _ask_password(), args.admin)
            print(f"נוצר המשתמש {auth.normalise(args.username)}")

        elif args.command == "passwd":
            auth.set_password(args.username, _ask_password())
            print("הסיסמה עודכנה.")

        elif args.command == "remove":
            auth.delete_user(args.username)
            print("המשתמש נמחק.")

        elif args.command == "list":
            users = auth.list_users()
            if not users:
                print("אין משתמשים.")
            for user in users:
                print(f"  {user.username}{'  (admin)' if user.is_admin else ''}")

        elif args.command == "bootstrap":
            if auth.count_users():
                print("כבר קיימים משתמשים; לא נוצר משתמש חדש.")
                return 0
            username = os.environ.get("ELBASH_ADMIN_USER")
            password = os.environ.get("ELBASH_ADMIN_PASSWORD")
            if not username or not password:
                raise SystemExit(
                    "יש להגדיר ELBASH_ADMIN_USER ו-ELBASH_ADMIN_PASSWORD."
                )
            auth.create_user(username, password, is_admin=True)
            print(f"נוצר המשתמש {auth.normalise(username)}")

    except auth.AuthError as exc:
        print(f"שגיאה: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
