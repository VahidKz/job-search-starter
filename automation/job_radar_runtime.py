"""Local compatibility entry point for the pinned discovery-only engine."""
from __future__ import annotations

import sqlite3
from functools import wraps


def install_sqlite_compat(store) -> None:
    original = store.new_since_last_run
    if getattr(original, "_bounded_sqlite_lookup", False):
        return

    @wraps(original)
    def bounded_lookup(con, uids, run=None):
        # Upstream binds every retained UID at once; full catalogs exceed SQLite's cap.
        get_limit = getattr(con, "getlimit", None)
        limit = get_limit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) if get_limit else 999
        batch_size = min(900, limit - 1)
        if not uids:
            return original(con, uids, run=run)
        if batch_size < 1:
            raise RuntimeError("SQLite needs at least two bind parameters for discovery lookup")
        found = set()
        for start in range(0, len(uids), batch_size):
            found.update(original(con, uids[start:start + batch_size], run=run))
        return found

    bounded_lookup._bounded_sqlite_lookup = True
    store.new_since_last_run = bounded_lookup


def main() -> int:
    from jobradar import store
    from jobradar.cli import main as upstream_main

    install_sqlite_compat(store)
    return upstream_main()


if __name__ == "__main__":
    raise SystemExit(main())
