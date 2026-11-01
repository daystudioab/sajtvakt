"""Kommandon: python -m sajtvakt_server set-password"""
import getpass
import sys

from . import db
from .web import _hash_password


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] != "set-password":
        print("Användning: python -m sajtvakt_server set-password")
        sys.exit(1)
    db.init()
    pw = getpass.getpass("Nytt lösenord (minst 12 tecken): ")
    if len(pw) < 12 or pw != getpass.getpass("Igen: "):
        print("Lösenorden är för korta eller stämmer inte överens.")
        sys.exit(1)
    db.set_setting("admin_hash", _hash_password(pw))
    db.run("DELETE FROM sessions")
    print("Lösenordet är bytt. Alla inloggningar är utloggade.")


main()
