




import pathlib
import subprocess
import sys
import time

_DB   = pathlib.Path("data/output/processed_index.db")
_CKPT = pathlib.Path("data/output/checkpoint.json")

_UNLINK_RETRIES = 10
_UNLINK_DELAY_S = 0.25


def safe_remove(path, retries=_UNLINK_RETRIES, delay=_UNLINK_DELAY_S):
    """Unlink path with retries for Windows transient file locks (WinError 32)."""
    for _ in range(retries):
        if not path.exists():
            return True
        try:
            path.unlink()
            return True
        except PermissionError:
            time.sleep(delay)
        except Exception:
            break
    return not path.exists()


def safe_start_spawn(spawn_fn):
    """Clean up DB/checkpoint with retries, assert absence, then call spawn_fn().

    Raises RuntimeError with tasklist/pgrep diagnostics if files remain locked.
    Import this from tests and use it wherever _spawn() is called on a clean slate.
    """
    db = _DB
    ck = _CKPT
    if not safe_remove(db) or not safe_remove(ck):
        for _ in range(3):
            time.sleep(0.5)
            if safe_remove(db) and safe_remove(ck):
                break
    if db.exists() or ck.exists():
        try:
            if sys.platform == "win32":
                out = subprocess.check_output(
                    ["tasklist", "/FI", "IMAGENAME eq python.exe"],
                    text=True, timeout=5,
                )
            else:
                out = subprocess.check_output(
                    ["pgrep", "-la", "python"], text=True, timeout=5,
                )
        except Exception as exc:
            out = "(process list unavailable: " + str(exc) + ")"
        msg = (
            "Safe start failed: DB or CK still present after retries.\n"
            "Process list:\n" + out
        )
        raise RuntimeError(msg)
    time.sleep(0.25)
    return spawn_fn()





