"""One-time/occasional CLI to create or reset the administrator account.

Run from the repository root:

    python -m backend.scripts.create_admin --username admin --password ...

If --password is omitted, you'll be prompted for it (hidden input). This is
intentionally a CLI, not an API endpoint -- Phase 1 has no user-management UI,
and self-service account creation isn't something a single-admin deployment
needs yet.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from backend.app.auth import service as auth_service
from core.db import get_session


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", default=None, help="Omit to be prompted securely.")
    parser.add_argument("--role", default="admin")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="If the user already exists, update its password instead of failing.",
    )
    args = parser.parse_args()

    password = args.password or getpass.getpass("Password: ")

    with get_session() as session:
        existing = auth_service.get_user_by_username(args.username, session)
        if existing is not None and not args.reset:
            print(
                f"User '{args.username}' already exists. Pass --reset to change its password.",
                file=sys.stderr,
            )
            return 1

        if existing is not None:
            if len(password) < 8:
                print("Password must be at least 8 characters.", file=sys.stderr)
                return 1
            from app.auth.security import hash_password

            existing.password_hash = hash_password(password)
            existing.is_active = True
            print(f"Password reset for user '{existing.username}'.")
            return 0

        user = auth_service.create_user(args.username, password, session, role=args.role)
        print(f"Created user '{user.username}' (id={user.id}, role={user.role}).")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
