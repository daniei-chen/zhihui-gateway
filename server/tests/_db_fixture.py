"""Explicit connection ownership for tests whose HTTP middleware writes to DB."""
from pathlib import Path
import tempfile
import os
from server import config, db


def isolate_database(case):
    directory = tempfile.TemporaryDirectory()
    original_path, original_connection = config.DB_PATH, db._conn
    original_data = config.DATA_DIR
    original_spool = os.environ.get('WB_LEDGER_SPOOL_DIR')

    def cleanup():
        try:
            with db._lock:
                from server import budgetowner,tasklease
                budgetowner.close()
                tasklease.reset_identity()
                if db._conn is not None and db._conn is not original_connection:
                    db._conn.close()
                db._conn = original_connection
                config.DB_PATH = original_path
                config.DATA_DIR = original_data
                if original_spool is None:os.environ.pop('WB_LEDGER_SPOOL_DIR',None)
                else:os.environ['WB_LEDGER_SPOOL_DIR']=original_spool
        finally:
            directory.cleanup()

    case.addCleanup(cleanup)
    config.DB_PATH = Path(directory.name)/'fixture.db'
    config.DATA_DIR = Path(directory.name)
    os.environ['WB_LEDGER_SPOOL_DIR']=str(Path(directory.name)/'ledger-spool')
    db._conn = None
    db.connect()
    return Path(directory.name)
