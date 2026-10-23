"""Owner-bound domain instances over one shared market repository."""
from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

from app.services.path_locks import path_lock
from app.services.user_context import UserContext, current_user, user_data_dir, user_scope


class UserRuntimeRegistry:
    def __init__(self, app_state):
        self.state = app_state
        self._lock = threading.RLock()
        self._runtimes: dict[str, SimpleNamespace] = {}

    def get(self, user: UserContext):
        root = self.state.repo.store.data_dir
        with path_lock(user_data_dir(root, user.id) / '.runtime'):
            with self._lock:
                existing = self._runtimes.get(user.id)
            if existing is not None:
                role_changed = existing.user.role != user.role
                existing.user = user
                if role_changed:
                    from app.strategy import monitor_rules
                    with user_scope(user):
                        existing.monitor_engine.set_rules(monitor_rules.load_all(self.state.repo.store.data_dir))
                        existing.monitor_engine.invalidate_strategy_state()
                        existing.strategy_monitor.stop_all()
                return existing
            from app.services.screener import ScreenerService
            from app.services.sector_monitor import SectorMonitorService
            from app.strategy import config, monitor_rules
            from app.strategy.engine import StrategyEngine
            from app.strategy.monitor import MonitorRuleEngine, StrategyMonitorService
            root = self.state.repo.store.data_dir
            with user_scope(user):
                engine = StrategyEngine(
                    strategy_dirs=strategy_directories(root),
                    declarative_dirs=[user_data_dir(root) / 'strategies' / 'composite'],
                    override_loader=lambda sid: config.load_override(root, sid),
                )
                runtime = SimpleNamespace(user=user)
                monitor = MonitorRuleEngine(rule_filter=lambda rule: monitor_rules.runtime_rule_allowed(rule, runtime.user.role))
                paper_monitor = MonitorRuleEngine()
                sector = SectorMonitorService(self.state.repo)
                for rule_engine in (monitor, paper_monitor):
                    rule_engine.set_strategy_engine(engine)
                    rule_engine.set_data_dir(root)
                    rule_engine.set_history_loader(ScreenerService(self.state.repo)._load_enriched_history)
                    rule_engine.set_history_loader_etf(ScreenerService(self.state.repo, asset_type='etf')._load_enriched_history)
                monitor.set_sector_monitor_service(sector)
                from app.services import preferences
                monitor_rules.migrate_strategy_monitors(
                    root,
                    preferences.get_strategy_monitor_ids() if preferences.get_strategy_monitor_enabled() else [],
                    {item['id']: item['name'] for item in engine.list_strategies()},
                )
                monitor.set_rules(monitor_rules.load_all(root))
                from app.services.mining_manager import MiningJobManager
                mining_manager = MiningJobManager(root)
                runtime.strategy_engine = engine
                runtime.monitor_engine = monitor
                runtime.paper_monitor_engine = paper_monitor
                runtime.mining_manager = mining_manager
                runtime.sector_monitor_service = sector
                runtime.strategy_monitor = StrategyMonitorService()
                with self._lock:
                    self._runtimes[user.id] = runtime
                return runtime

    def invalidate_shared_strategy_state(self):
        with self._lock:
            runtimes = list(self._runtimes.values())
        for runtime in runtimes:
            runtime.strategy_engine.clear_realtime_matrices()
            runtime.monitor_engine.invalidate_strategy_state()
            runtime.paper_monitor_engine.invalidate_strategy_state()

    def recover_interrupted(self):
        # Recovery needs persisted manifests, not an engine per registered account.
        from app.services.auth import get_store
        from app.services.mining_jobs import MiningRunStore
        root = self.state.repo.store.data_dir
        for row in get_store(root).list_active_users():
            with user_scope(UserContext(row['id'], row['role'], root)):
                MiningRunStore(root).recover_interrupted()

    def background_active(self):
        """Keep offline tasks running without constructing idle users' engines."""
        from app.services import preferences
        from app.services.auth import get_store
        from app.strategy import monitor_rules, paper
        root = self.state.repo.store.data_dir
        result = []
        for row in get_store(root).list_active_users():
            user = UserContext(row['id'], row['role'], root)
            with user_scope(user):
                with self._lock:
                    existing = self._runtimes.get(user.id)
                has_paper = any(paper.accounts_base(root).glob('*/account.json'))
                if existing is not None:
                    runtime = self.get(user)  # Reconcile role changes before evaluating rules.
                    if any(rule.get('enabled', True) for rule in runtime.monitor_engine.rules.values()) or has_paper:
                        result.append(runtime)
                elif has_paper or (user.role == 'admin' and (
                    any(rule.get('enabled', True) for rule in monitor_rules.load_all(root))
                    or (preferences.get_strategy_monitor_enabled() and preferences.get_strategy_monitor_ids())
                )):
                    result.append(self.get(user))
        return result

    def shutdown(self):
        with self._lock:
            runtimes = list(self._runtimes.values())
        for runtime in runtimes:
            with user_scope(runtime.user):
                runtime.mining_manager.shutdown()

    def active(self):
        from app.services.auth import get_store
        root = self.state.repo.store.data_dir
        users = get_store(root).list_active_users()
        return [self.get(UserContext(row['id'], row['role'], root)) for row in users]


def runtime_for(request):
    return request.app.state.user_runtimes.get(current_user())


def strategy_directories(root: Path) -> list[Path]:
    private = user_data_dir(root)
    return [Path(__file__).resolve().parents[1] / 'strategy' / 'builtin',
            root / 'strategies' / 'templates',
            private / 'strategies' / 'composite']


def settle_user_accounts(root: Path, day: str) -> dict:
    from app.services import alert_store
    from app.services.auth import get_store
    from app.strategy import paper
    settled = 0
    for user in get_store(root).list_active_users():
        with user_scope(UserContext(user['id'], user['role'], root)):
            for account in paper.list_account_ids(root):
                result = paper.settle_day(root, day, account_id=account)
                if result.get('filled'):
                    alert_store.append_many(root, paper.day_fill_events(root, day, account_id=account))
            settled += 1
    return {'settled_users': settled}


def active_watchlist_symbols(root: Path) -> set[str]:
    from app.services import watchlist
    from app.services.auth import get_store
    symbols = set()
    for user in get_store(root).list_active_users():
        with user_scope(UserContext(user['id'], user['role'], root)):
            symbols.update(row['symbol'] for row in watchlist.list_symbols())
    return symbols
