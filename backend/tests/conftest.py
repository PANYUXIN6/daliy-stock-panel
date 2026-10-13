"""Explicit ownership fixtures for domain tests that access personal stores."""
import pytest


@pytest.fixture
def private_owner(tmp_path, monkeypatch):
    from app.config import settings
    from app.services.user_context import UserContext, user_scope
    monkeypatch.setattr(settings, 'data_dir', tmp_path)
    with user_scope(UserContext('a' * 32, 'admin', tmp_path)) as owner:
        yield owner


@pytest.fixture
def authenticated_actor(tmp_path, monkeypatch):
    """Explicit real account/session for tests of authenticated delayed work."""
    from app.config import settings
    from app.services.auth import AuthStore
    from app.services.user_context import UserContext, user_scope
    monkeypatch.setattr(settings, 'data_dir', tmp_path)
    store = AuthStore(tmp_path)
    admin = store.bootstrap('testadmin', 'test-initial-password')
    token, _ = store.login('testadmin', 'test-initial-password', 'test', '')
    with user_scope(UserContext(admin['id'], 'admin', tmp_path, session_token=token)) as owner:
        yield owner
