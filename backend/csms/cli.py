"""Command line admin: `csms serve`, `csms add-user`, `csms add-tag`, ..."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from . import auth
from .config import get_settings
from .db import Database


async def _with_db(fn, *args):
    db = Database(get_settings().db_path)
    await db.connect()
    try:
        return await fn(db, *args)
    finally:
        await db.close()


async def _add_user(db: Database, a) -> None:
    password = a.password or getpass.getpass(f"Password for {a.username}: ")
    display_name = a.display_name or a.username
    user_id = await auth.create_user(db, a.username, display_name, password, a.admin)
    tag = await db.app_tag_for_user(user_id)
    print(f"Created user {a.username} (id {user_id}), app id tag {tag}")


async def _set_password(db: Database, a) -> None:
    password = a.password or getpass.getpass(f"New password for {a.username}: ")
    await db.execute(
        "UPDATE users SET password_hash = ? WHERE username = ?",
        (auth.hash_password(password), a.username),
    )
    await db.execute(
        "DELETE FROM api_tokens WHERE user_id = (SELECT id FROM users WHERE username = ?)",
        (a.username,),
    )
    print("Password changed; existing logins were signed out.")


async def _add_tag(db: Database, a) -> None:
    user = await db.fetchone("SELECT id FROM users WHERE username = ?", (a.username,))
    if not user:
        sys.exit(f"No user {a.username}")
    await db.execute(
        "INSERT INTO id_tags (id_tag, user_id, label) VALUES (?, ?, ?) "
        "ON CONFLICT(id_tag) DO UPDATE SET user_id = excluded.user_id, label = excluded.label",
        (a.id_tag, user["id"], a.label),
    )
    await db.execute("DELETE FROM unknown_id_tags WHERE id_tag = ?", (a.id_tag,))
    print(f"Tag {a.id_tag} now belongs to {a.username}")


async def _list(db: Database, a) -> None:
    for u in await db.fetchall("SELECT id, username, display_name, is_admin FROM users"):
        print(
            f"{u['id']:>3} {u['username']:<16} {u['display_name']:<20} "
            f"{'admin' if u['is_admin'] else ''}"
        )
        for t in await db.fetchall(
            "SELECT id_tag, label, blocked FROM id_tags WHERE user_id = ?", (u["id"],)
        ):
            blocked = "BLOCKED" if t["blocked"] else ""
            print(f"      tag {t['id_tag']:<22} {t['label']:<12} {blocked}")
    unknown = await db.fetchall("SELECT * FROM unknown_id_tags ORDER BY last_seen DESC")
    if unknown:
        print("\nUnrecognised tags seen by the charger:")
        for t in unknown:
            print(f"      {t['id_tag']:<22} last seen {t['last_seen']} ({t['count']}x)")


def main() -> None:
    p = argparse.ArgumentParser(prog="csms")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the OCPP server and app API")

    s = sub.add_parser("add-user", help="create an app user")
    s.add_argument("username")
    s.add_argument("--display-name")
    s.add_argument("--password", help="omit to be prompted")
    s.add_argument("--admin", action="store_true")

    s = sub.add_parser("set-password")
    s.add_argument("username")
    s.add_argument("--password")

    s = sub.add_parser("add-tag", help="assign an RFID card id to a user")
    s.add_argument("username")
    s.add_argument("id_tag")
    s.add_argument("--label", default="RFID card")

    sub.add_parser("list", help="list users, their tags, and unrecognised tags")

    a = p.parse_args()
    if a.cmd == "serve":
        from .app import run

        run()
    else:
        fn = {
            "add-user": _add_user,
            "set-password": _set_password,
            "add-tag": _add_tag,
            "list": _list,
        }[a.cmd]
        asyncio.run(_with_db(fn, a))


if __name__ == "__main__":
    main()
