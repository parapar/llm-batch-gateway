"""Admin CLI: `batchsvc-admin <command> ...`.

Talks directly to the database (not the HTTP API) so it works without a
running server -- useful for initial setup / cron jobs / scripting on the
box that hosts the service.
"""

from __future__ import annotations

import argparse
import sys

from batchsvc import ledger
from batchsvc.config import load_settings
from batchsvc.db import build_database
from batchsvc.models import ApiKey, Budget, User
from batchsvc.security import generate_api_key


def cmd_create_user(args: argparse.Namespace) -> None:
    db = build_database(load_settings())
    with db.session_scope() as session:
        existing = session.query(User).filter(User.username == args.username).one_or_none()
        if existing is not None:
            print(f"error: username '{args.username}' already exists", file=sys.stderr)
            raise SystemExit(1)
        user = User(username=args.username, full_name=args.full_name)
        session.add(user)
        session.flush()
        session.add(Budget(user_id=user.id))
        session.commit()
        print(f"created user {user.id}  username={user.username}")

        if args.with_key:
            raw_key, key_prefix, key_hash = generate_api_key()
            session.add(ApiKey(user_id=user.id, key_prefix=key_prefix, key_hash=key_hash, label="initial"))
            session.commit()
            print(f"api key (save this, shown once): {raw_key}")

        if args.grant:
            budget = ledger.grant(session, user, args.grant, note="initial grant (cli)")
            print(f"granted {args.grant} tokens; available={budget.available_tokens}")


def cmd_create_key(args: argparse.Namespace) -> None:
    db = build_database(load_settings())
    with db.session_scope() as session:
        user = session.query(User).filter(User.username == args.username).one_or_none()
        if user is None:
            print(f"error: no such user '{args.username}'", file=sys.stderr)
            raise SystemExit(1)
        raw_key, key_prefix, key_hash = generate_api_key()
        session.add(ApiKey(user_id=user.id, key_prefix=key_prefix, key_hash=key_hash, label=args.label))
        session.commit()
        print(f"api key (save this, shown once): {raw_key}")


def cmd_grant(args: argparse.Namespace) -> None:
    db = build_database(load_settings())
    with db.session_scope() as session:
        user = session.query(User).filter(User.username == args.username).one_or_none()
        if user is None:
            print(f"error: no such user '{args.username}'", file=sys.stderr)
            raise SystemExit(1)
        budget = ledger.grant(session, user, args.tokens, note=args.note)
        print(f"granted {args.tokens} tokens to {user.username}; available={budget.available_tokens}")


def cmd_list_users(args: argparse.Namespace) -> None:
    db = build_database(load_settings())
    with db.session_scope() as session:
        users = session.query(User).order_by(User.created_at).all()
        for u in users:
            b = u.budget
            avail = b.available_tokens if b else 0
            status = "active" if u.is_active else "disabled"
            print(f"{u.id}  {u.username:<24} {status:<9} available={avail}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="batchsvc-admin")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-user", help="Create a student account")
    p.add_argument("username")
    p.add_argument("--full-name", default=None)
    p.add_argument("--with-key", action="store_true", help="Also issue an initial API key")
    p.add_argument("--grant", type=int, default=0, help="Initial token grant")
    p.set_defaults(func=cmd_create_user)

    p = sub.add_parser("create-key", help="Issue a new API key for an existing user")
    p.add_argument("username")
    p.add_argument("--label", default=None)
    p.set_defaults(func=cmd_create_key)

    p = sub.add_parser("grant", help="Grant additional tokens to a user")
    p.add_argument("username")
    p.add_argument("tokens", type=int)
    p.add_argument("--note", default=None)
    p.set_defaults(func=cmd_grant)

    p = sub.add_parser("list-users", help="List users and their available budget")
    p.set_defaults(func=cmd_list_users)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
