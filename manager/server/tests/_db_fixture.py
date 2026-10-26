"""Explicit connection ownership for tests whose HTTP middleware writes to DB."""
from pathlib import Path
import tempfile
from server import config, db


def isolate_database(case):
    directory = tempfile.TemporaryDirectory()
    original_path, original_connection = config.DB_PATH, db._conn

    def cleanup():
        try:
            with db._lock:
                if db._conn is not None and db._conn is not original_connection:
                    db._conn.close()
                db._conn = original_connection
                config.DB_PATH = original_path
        finally:
            directory.cleanup()

    case.addCleanup(cleanup)
    config.DB_PATH = Path(directory.name)/'fixture.db'
    db._conn = None
    db.connect()
    return Path(directory.name)
