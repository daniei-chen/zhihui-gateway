"""Explicit operator approval of a user-wallet cap, not a supplier-price cap.

The platform absorbs supplier costs above the request's frozen user balance.
Approval is persisted during controlled deployment; environment switches and
creating a key cannot silently approve this financial policy.
"""
POLICY = 'wallet-cap-platform-absorbs-v1'
MESSAGE = '计费调用尚未获准开放；用户积分保留，请联系管理员'
CODE = 'portal_billing_not_approved'


def dispatch_ready(conn=None):
    from . import portalstore as store
    try:
        if conn is None:
            if not (store.config.DATA_DIR / 'user-portal.sqlite3').is_file():
                return False
            with store.transaction(read_only=True) as opened:
                return dispatch_ready(opened)
        row = conn.execute("SELECT value FROM portal_settings WHERE key='billing_policy'").fetchone()
        return bool(row and row[0] == POLICY)
    except Exception:
        return False


def require_approved_policy(conn=None):
    from .budget import BudgetRejected
    if not dispatch_ready(conn):
        raise BudgetRejected(MESSAGE, CODE, status=503)
