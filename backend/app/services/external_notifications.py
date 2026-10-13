"""External delivery is reserved for the current, active admin's own events."""
from __future__ import annotations

from app.services.user_context import current_user


def allowed() -> bool:
    from app.services.auth import get_store
    owner = current_user(required=False)
    if owner is None:
        return False
    user = get_store(owner.data_dir).get_user(owner.id)
    return bool(user and user['is_active'] and user['role'] == 'admin')


def submit(executor, function, *args):
    """Recheck the persisted role when an asynchronous delivery actually executes."""
    from app.services.auth import get_store
    owner = current_user()
    def deliver():
        user = get_store(owner.data_dir).get_user(owner.id)
        if user and user['is_active'] and user['role'] == 'admin':
            return function(*args)
        return None
    return executor.submit(deliver)
