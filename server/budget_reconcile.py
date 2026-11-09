"""Finite scheduled reconciliation through the guarded immutable release entry."""
import json
import time

from . import budget, budgetowner, db


def run(now=None):
    now = int(time.time()) if now is None else int(now)
    settled = budget.reconcile_evidenced_unknowns(now)
    summary = budget.unresolved_summary(now=now)['summary']
    return {'settled_confirmed_zero': settled, 'unknown_remaining': summary['unknown_total'],
            'needs_human': summary['needs_human'], 'archived_pending': summary['archived_pending']}


def main():
    try:
        db.connect()
        budget.initialize()
        print(json.dumps(run(), sort_keys=True))
        return 0
    except Exception:
        # Failure must reach the scheduler without logging request/user data.
        print(json.dumps({'status': 'reconciliation_failed', 'state_preserved': True}))
        return 1
    finally:
        budgetowner.close()
        with db._lock:
            db._close_connection_locked()


if __name__ == '__main__':
    raise SystemExit(main())
