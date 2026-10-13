from __future__ import annotations

from app import desktop


def test_frozen_collector_entry_does_not_open_window(monkeypatch):
    monkeypatch.setattr(desktop.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(desktop.sys, 'executable', '/package/TSP')
    assert desktop._collector_command('--collector-service') == ['/package/TSP', '--collector-service']
    called = []
    monkeypatch.setattr(desktop.sys, 'argv', ['TSP', '--collector-service'])
    monkeypatch.setattr(desktop, '_guard_streams', lambda: None)
    monkeypatch.setattr(desktop, '_setup_logging', lambda: None)
    monkeypatch.setattr(desktop, '_ensure_data_dir_writable', lambda: None)
    monkeypatch.setattr(desktop, '_run_collector_service', lambda: called.append('collector') or 0)
    monkeypatch.setattr(desktop, '_acquire_single_instance', lambda: called.append('window') or True)
    assert desktop.main() == 0
    assert called == ['collector']


def test_desktop_reuses_running_collector_without_spawning(monkeypatch):
    monkeypatch.setattr(desktop, '_collector_healthy', lambda: True)
    monkeypatch.setattr(desktop, '_collector_runtime', lambda: (_ for _ in ()).throw(AssertionError('spawn')))
    desktop._ensure_collector()
