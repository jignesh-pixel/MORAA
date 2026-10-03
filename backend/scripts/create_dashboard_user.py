#!/usr/bin/env python3
"""Create (or make administrator) a login for the WhatsApp chat dashboard.

Usage (from backend/):   python scripts/create_dashboard_user.py --email you@example.com [--username you]

Asks for the password on the keyboard (never on the command line, never printed). If the email already has an account it is
made an administrator and, only if you type one, its password is changed. Sign-in with Google needs no password: just list the
email in DASHBOARD_ALLOWED_EMAILS and set GOOGLE_CLIENT_ID.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--email", required=True)
    parser.add_argument("--username")
    args = parser.parse_args(argv)

    from app.database import SessionLocal
    from app.models.user import User
    from app.utils.security import hash_password

    email = args.email.strip().lower()
    password = getpass.getpass("Password (at least 8 characters, leave empty to keep the current one): ")
    if password and len(password) < 8:
        print("The password must have at least 8 characters.")
        return 2
    with SessionLocal() as db:
        user = db.query(User).filter(User.email == email).first()
        if user is None:
            if not password:
                print("A new account needs a password.")
                return 2
            user = User(email=email, username=(args.username or email), hashed_password=hash_password(password),
                        full_name=args.username or email, is_active=True)
            db.add(user)
            print("Account created.")
        elif password:
            user.hashed_password = hash_password(password)
            print("Password changed.")
        user.is_admin = True
        db.commit()
        print("This account can now use the dashboard.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
