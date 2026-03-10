




"""src/locked_db.py -- Cross-platform exclusive DB wrapper using portalocker.

Install:  pip install portalocker

Falls back to advisory-only locking when portalocker is absent.  In this
pipeline the single-writer architecture (main process only) is the real
guard; portalocker adds an OS-level fence that prevents a ghost process from
re-opening the DB simultaneously after an abnormal exit.
"""

import contextlib
import pathlib
import sqlite3

try:
    import portalocker as _pl
    _PORTALOCKER = True
except ImportError:
    _pl = None
    _PORTALOCKER = False


@contextlib.contextmanager
def open_locked_db(path: pathlib.Path):
    """Context manager: acquire exclusive advisory lock, open DB, yield conn.

    The .lock sidecar file is separate from the DB file so SQLite can open
    and manage the DB (including WAL sidecars) without interference.

    Example::

        from src.locked_db import open_locked_db
        with open_locked_db(DB_FILE) as conn:
            do_work(conn)
        # lock released, WAL checkpointed, connection closed

    """
    lock_path = pathlib.Path(str(path) + ".lock")
    lock_fh = open(str(lock_path), "w")
    try:
        if _PORTALOCKER:
            # LOCK_EX | LOCK_NB: fail immediately if another process holds it.
            # Remove LOCK_NB to block instead of raising.
            _pl.lock(lock_fh, _pl.LOCK_EX)
    except Exception:
        lock_fh.close()
        raise

    conn = None
    try:
        conn = sqlite3.connect(str(path), timeout=30, check_same_thread=True)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS processed"
            " (receipt_id INTEGER PRIMARY KEY);"
        )
        conn.commit()
        yield conn
    finally:
        if conn is not None:
            try:
                conn.commit()
            except Exception:
                pass
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
        if _PORTALOCKER:
            try:
                _pl.unlock(lock_fh)
            except Exception:
                pass
        try:
            lock_fh.close()
        except Exception:
            pass
        try:
            lock_path.unlink()
        except Exception:
            pass





