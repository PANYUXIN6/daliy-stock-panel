"""持久化点数台账与模型用量统计；默认只有点数影响使用。

SQLite 的短写事务协调 web/collector 与并发请求。任务保存规则快照，调价、
升级、跨月和失败退款都不改写已有任务的账期与价格。
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from app.services.ai_credit_policy import FEATURES, CreditPolicy, cny, token_cost
from app.services.auth import AuthError, get_store
from app.services.user_context import UserContext

_BEIJING = ZoneInfo("Asia/Shanghai")
_current_task: ContextVar[CreditTask | None] = ContextVar("ai_credit_task", default=None)


class CreditError(RuntimeError):
    def __init__(self, detail: str, status: int = 429, code: str = "AI_COST_LIMIT"):
        super().__init__(detail)
        self.detail, self.status, self.code = detail, status, code


def _month(now: float | None = None) -> tuple[str, str]:
    local = datetime.fromtimestamp(time.time() if now is None else now, _BEIJING)
    year, month = local.year + (local.month == 12), local.month % 12 + 1
    return local.strftime("%Y-%m"), datetime(year, month, 1, tzinfo=_BEIJING).isoformat()


def _actor(owner: UserContext, now: float | None = None) -> dict:
    session = get_store(owner.data_dir).session(owner.session_token, now=now)
    if (not session or session["user"]["id"] != owner.id
            or session["user"]["must_change_password"]):
        raise AuthError("未登录、会话已过期或需要修改密码", 401, "SESSION_INVALID")
    return session["user"]


@dataclass(frozen=True)
class CallReservation:
    id: str
    max_tokens: int | None
    reserved_micros: int


@dataclass(frozen=True)
class CreditTask:
    store: CreditStore
    id: str
    user_id: str
    period: str
    feature: str
    policy: CreditPolicy

    @property
    def started(self) -> bool:
        with self.store._connection() as db:
            return db.execute("SELECT 1 FROM credit_calls WHERE task_id=? LIMIT 1", (self.id,)).fetchone() is not None

    def reserve_call(self, input_tokens_upper: int | None, requested_max_tokens: int | None) -> CallReservation:
        if input_tokens_upper is not None and (type(input_tokens_upper) is not int or input_tokens_upper < 0):
            raise CreditError("无法核验 AI 输入预算", code="AI_METERING_UNAVAILABLE")
        policy = self.policy
        if not policy.enforce_cost_limits:
            with self.store._connection(True) as db:
                task = db.execute("SELECT state FROM credit_tasks WHERE id=?", (self.id,)).fetchone()
                if task is None or task["state"] != "pending":
                    raise CreditError("本次 AI 任务已结束，请重新发起", 409, "AI_TASK_FINISHED")
                call = CallReservation(uuid.uuid4().hex, requested_max_tokens, 0)
                db.execute(
                    "INSERT INTO credit_calls(id,task_id,input_upper,output_limit,reserved_micros,usage_state) VALUES(?,?,?,?,0,'pending')",
                    (call.id, self.id, input_tokens_upper or 0, requested_max_tokens or 0),
                )
                return call
        if input_tokens_upper is None:
            raise CreditError("无法核验 AI 输入预算", code="AI_METERING_UNAVAILABLE")
        assistant = self.feature == "assistant"
        output_cap = policy.assistant_max_output_tokens if assistant else policy.report_max_output_tokens
        if requested_max_tokens is not None:
            if type(requested_max_tokens) is not int or requested_max_tokens <= 0:
                raise CreditError("无效的 AI 输出预算", code="AI_METERING_UNAVAILABLE")
            output_cap = min(output_cap, requested_max_tokens)
        max_calls = policy.assistant_max_calls if assistant else policy.report_max_calls
        with self.store._connection(True) as db:
            task = db.execute("SELECT * FROM credit_tasks WHERE id=?", (self.id,)).fetchone()
            incident = db.execute("SELECT reason FROM credit_incidents WHERE user_id=? AND period=?", (self.user_id, self.period)).fetchone()
            if incident:
                raise CreditError(incident[0], 503, "AI_METERING_UNAVAILABLE")
            if task["state"] != "pending":
                raise CreditError("本次 AI 任务已结束，请重新发起", 409, "AI_TASK_FINISHED")
            row = db.execute(
                "SELECT COUNT(*) AS calls, COALESCE(SUM(COALESCE(cost_micros,reserved_micros)),0) AS cost FROM credit_calls WHERE task_id=?",
                (self.id,),
            ).fetchone()
            remaining = task["budget_micros"] - row["cost"]
            input_cost = token_cost(input_tokens_upper, policy.input_micros_per_million)
            affordable = max(0, remaining - input_cost) * 1_000_000 // policy.output_micros_per_million
            output_cap = min(output_cap, affordable)
            if row["calls"] >= max_calls or output_cap < min(policy.min_output_tokens, requested_max_tokens or policy.min_output_tokens):
                raise CreditError("本次任务已达到费用或调用轮次上限，请缩小问题范围后重试")
            call = CallReservation(uuid.uuid4().hex, output_cap,
                                   input_cost + token_cost(output_cap, policy.output_micros_per_million))
            db.execute(
                "INSERT INTO credit_calls(id,task_id,input_upper,output_limit,reserved_micros,usage_state) VALUES(?,?,?,?,?,'pending')",
                (call.id, self.id, input_tokens_upper, output_cap, call.reserved_micros),
            )
            return call

    def settle_call(self, call_id: str, actual_micros: int | None, *,
                    prompt_tokens: int | None = None, completion_tokens: int | None = None,
                    usage_reported: bool | None = None) -> None:
        with self.store._connection(True) as db:
            row = db.execute("SELECT * FROM credit_calls WHERE id=? AND task_id=?", (call_id, self.id)).fetchone()
            if row is None or row["settled"]:
                return
            valid_tokens = all(type(value) is int and value >= 0 for value in (prompt_tokens, completion_tokens))
            reported = valid_tokens and usage_reported is not False
            if not reported:
                prompt_tokens = completion_tokens = None
            usage_state = ("reported" if actual_micros is not None else "unpriced") if reported else "missing"
            if actual_micros is None and self.policy.enforce_cost_limits:
                actual_micros = row["reserved_micros"]
            if actual_micros is not None and (type(actual_micros) is not int or actual_micros < 0):
                raise CreditError("无效的模型用量", code="AI_METERING_UNAVAILABLE")
            db.execute(
                "UPDATE credit_calls SET cost_micros=?,prompt_tokens=?,completion_tokens=?,usage_state=?,settled=1 WHERE id=?",
                (actual_micros, prompt_tokens, completion_tokens, usage_state, call_id),
            )
            # 不截掉真实账单。若上游违反预留契约，停止该账号继续调用，留待核对。
            if (self.policy.enforce_cost_limits and actual_micros is not None
                    and actual_micros > row["reserved_micros"]):
                db.execute("INSERT OR IGNORE INTO credit_incidents(user_id,period,reason) VALUES(?,?,?)",
                           (self.user_id, self.period, "模型返回用量超出预留上限，已暂停生成，请联系管理员"))

    def finish(self, outcome: Literal["success", "failed", "cancelled"]) -> None:
        if outcome not in {"success", "failed", "cancelled"}:
            raise ValueError("未知任务终态")
        with self.store._connection(True) as db:
            row = db.execute("SELECT state FROM credit_tasks WHERE id=?", (self.id,)).fetchone()
            if row is None or row["state"] != "pending":
                return
            calls = db.execute("SELECT COUNT(*) FROM credit_calls WHERE task_id=?", (self.id,)).fetchone()[0]
            if outcome == "cancelled" and not calls:
                outcome = "failed"
            if self.policy.enforce_cost_limits:
                db.execute(
                    "UPDATE credit_calls SET cost_micros=reserved_micros,settled=1,usage_state='missing' WHERE task_id=? AND settled=0",
                    (self.id,),
                )
            else:
                db.execute(
                    "UPDATE credit_calls SET cost_micros=NULL,settled=1,usage_state='missing' WHERE task_id=? AND settled=0",
                    (self.id,),
                )
            db.execute("UPDATE credit_tasks SET state=?,finished_at=? WHERE id=?", (outcome, time.time(), self.id))


class CreditStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "auth" / "ai_credits.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS credit_policy (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    version INTEGER NOT NULL, rules TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS credit_tasks (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    period TEXT NOT NULL, feature TEXT NOT NULL, points INTEGER NOT NULL,
                    policy_version INTEGER NOT NULL, policy_json TEXT NOT NULL,
                    budget_micros INTEGER NOT NULL, state TEXT NOT NULL,
                    created_at REAL NOT NULL, finished_at REAL,
                    UNIQUE(user_id,request_id)
                );
                CREATE INDEX IF NOT EXISTS credit_month ON credit_tasks(user_id,period,state);
                CREATE TABLE IF NOT EXISTS credit_calls (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES credit_tasks(id),
                    input_upper INTEGER NOT NULL, output_limit INTEGER NOT NULL,
                    reserved_micros INTEGER NOT NULL, cost_micros INTEGER,
                    prompt_tokens INTEGER, completion_tokens INTEGER,
                    settled INTEGER NOT NULL DEFAULT 0,
                    usage_state TEXT NOT NULL DEFAULT 'legacy'
                );
                CREATE INDEX IF NOT EXISTS credit_call_task ON credit_calls(task_id);
                CREATE TABLE IF NOT EXISTS credit_incidents (
                    user_id TEXT NOT NULL, period TEXT NOT NULL, reason TEXT NOT NULL,
                    PRIMARY KEY(user_id,period)
                );
            """)
            # 加法迁移保留既有账本。历史金额可能是预算预留，不冒充实际上报费用。
            db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(credit_calls)")}
            if "usage_state" not in columns:
                db.execute("ALTER TABLE credit_calls ADD COLUMN usage_state TEXT NOT NULL DEFAULT 'legacy'")
            db.execute("INSERT OR IGNORE INTO credit_policy VALUES(1,1,?)", (CreditPolicy().model_dump_json(),))
        if os.name != "nt":
            self.path.chmod(0o600)

    @contextmanager
    def _connection(self, write: bool = False):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _policy(db) -> tuple[int, CreditPolicy]:
        row = db.execute("SELECT version,rules FROM credit_policy WHERE singleton=1").fetchone()
        try:
            return row["version"], CreditPolicy.model_validate_json(row["rules"])
        except (ValueError, TypeError, KeyError) as exc:
            raise CreditError("AI 点数规则不可用，请联系管理员", 503, "AI_POLICY_UNAVAILABLE") from exc

    def get_policy(self) -> dict:
        with self._connection() as db:
            version, policy = self._policy(db)
            return {"policy_version": version, "policy": policy.model_dump(mode="json")}

    def update_policy(self, owner: UserContext, policy: CreditPolicy, expected_version: int) -> dict:
        if _actor(owner)["role"] != "admin":
            raise AuthError("仅管理员可修改 AI 点数规则", 403, "ADMIN_REQUIRED")
        with self._connection(True) as db:
            version, _old = self._policy(db)
            if version != expected_version:
                raise CreditError("AI 规则已更新，请刷新后重试", 409, "AI_POLICY_CHANGED")
            db.execute("UPDATE credit_policy SET version=?,rules=? WHERE singleton=1", (version + 1, policy.model_dump_json()))
        return self.get_policy()

    @staticmethod
    def _totals(db, user_id: str, period: str) -> dict:
        rows = db.execute("""
            SELECT t.state,t.points,t.budget_micros,
                   COALESCE(SUM(COALESCE(c.cost_micros,c.reserved_micros)),0) AS cost,
                   COALESCE(SUM(CASE WHEN c.usage_state='reported' AND c.prompt_tokens IS NOT NULL
                       AND c.completion_tokens IS NOT NULL THEN c.cost_micros ELSE 0 END),0) AS reported_cost
            FROM credit_tasks t LEFT JOIN credit_calls c ON c.task_id=t.id
            WHERE t.user_id=? AND t.period=? GROUP BY t.id
        """, (user_id, period)).fetchall()
        return {
            "used": sum(r["points"] for r in rows if r["state"] in {"success", "cancelled"}),
            "reserved": sum(r["points"] for r in rows if r["state"] == "pending"),
            "normal_cost": sum(r["cost"] for r in rows if r["state"] in {"success", "cancelled"}),
            "failed_cost": sum(r["cost"] for r in rows if r["state"] == "failed"),
            "pending_budget": sum(r["budget_micros"] for r in rows if r["state"] == "pending"),
            "cost": sum(r["cost"] for r in rows),
            "reported_cost": sum(r["reported_cost"] for r in rows),
        }

    def usage_for_users(self, users: list[dict], *, now: float | None = None) -> dict[str, dict]:
        """管理页按本页账号批量读取，不使用目标用户会话或遍历个人文件。"""
        if not users:
            return {}
        period, _resets = _month(now)
        ids = [user["id"] for user in users]
        placeholders = ",".join("?" for _ in ids)
        with self._connection() as db:
            _version, policy = self._policy(db)
            # 两个按用户分组的查询，不把任务点数按模型调用数量重复累加。
            points = {row["user_id"]: row for row in db.execute(f"""
                SELECT user_id,
                    SUM(CASE WHEN state IN ('success','cancelled') THEN points ELSE 0 END) AS used,
                    SUM(CASE WHEN state='pending' THEN points ELSE 0 END) AS reserved
                FROM credit_tasks WHERE period=? AND user_id IN ({placeholders}) GROUP BY user_id
            """, [period, *ids])}
            calls = {row["user_id"]: row for row in db.execute(f"""
                SELECT t.user_id,COUNT(*) AS total_calls,
                    COALESCE(SUM(c.prompt_tokens),0) AS prompt_tokens,
                    COALESCE(SUM(c.completion_tokens),0) AS completion_tokens,
                    SUM(CASE WHEN c.prompt_tokens IS NULL OR c.completion_tokens IS NULL THEN 1 ELSE 0 END) AS unreported_calls,
                    SUM(CASE WHEN c.prompt_tokens IS NOT NULL AND c.completion_tokens IS NOT NULL
                        AND (c.cost_micros IS NULL OR c.usage_state!='reported') THEN 1 ELSE 0 END) AS unpriced_calls,
                    COALESCE(SUM(CASE WHEN c.usage_state='reported' AND c.prompt_tokens IS NOT NULL
                        AND c.completion_tokens IS NOT NULL THEN c.cost_micros ELSE 0 END),0) AS cost
                FROM credit_tasks t JOIN credit_calls c ON c.task_id=t.id
                WHERE t.period=? AND t.user_id IN ({placeholders}) GROUP BY t.user_id
            """, [period, *ids])}
        summaries = {}
        for user in users:
            uid, role = user["id"], user["role"]
            point = points.get(uid)
            call = calls.get(uid)
            used = point["used"] if point else 0
            reserved = point["reserved"] if point else 0
            prompt = call["prompt_tokens"] if call else 0
            completion = call["completion_tokens"] if call else 0
            unlimited = role == "admin"
            summaries[uid] = {
                "period": period, "monthly_points": None if unlimited else policy.monthly_points[role],
                "used_points": used, "reserved_points": reserved, "unlimited": unlimited,
                "remaining_points": None if unlimited else max(0, policy.monthly_points[role] - used - reserved),
                "prompt_tokens": prompt, "completion_tokens": completion,
                "total_tokens": prompt + completion,
                "estimated_cost_cny": cny(call["cost"] if call else 0),
                "unreported_calls": call["unreported_calls"] if call else 0,
                "unpriced_calls": call["unpriced_calls"] if call else 0,
                "total_calls": call["total_calls"] if call else 0,
            }
        return summaries

    @staticmethod
    def _budget_reason(db, user_id: str, period: str, policy: CreditPolicy,
                       role: str, totals: dict, points: int) -> tuple[str, int, str] | None:
        if totals["used"] + totals["reserved"] + points > policy.monthly_points[role]:
            return "本月 AI 点数不足，请等待下月额度刷新", 402, "AI_POINTS_EXHAUSTED"
        if not policy.enforce_cost_limits:
            return None
        incident = db.execute("SELECT reason FROM credit_incidents WHERE user_id=? AND period=?", (user_id, period)).fetchone()
        if incident:
            return incident[0], 503, "AI_METERING_UNAVAILABLE"
        budget = points * policy.cost_per_point_micros
        if (totals["normal_cost"] + totals["pending_budget"] + budget
                > policy.monthly_points[role] * policy.cost_per_point_micros):
            return "本月 AI 费用预算已用尽，请等待下月刷新", 429, "AI_COST_LIMIT"
        if (totals["failed_cost"] + totals["pending_budget"] + budget
                > policy.failure_budget_micros[role]):
            return "AI 故障补偿预算不足或有任务处理中，请稍后重试或联系管理员", 429, "AI_COST_LIMIT"
        return None

    def status(self, owner: UserContext, *, now: float | None = None) -> dict:
        user = _actor(owner, now)
        role = user["role"]
        period, resets = _month(now)
        with self._connection() as db:
            version, policy = self._policy(db)
            totals = self._totals(db, owner.id, period)
            unlimited = role == "admin"
            features = {}
            for feature in FEATURES:
                reason = None if unlimited else self._budget_reason(
                    db, owner.id, period, policy, role, totals, policy.feature_points[feature],
                )
                text = reason[0] if reason else None
                features[feature] = {"points": policy.feature_points[feature], "available": text is None, "reason": text}
            return {
                "policy_version": version, "period": period, "resets_at": resets,
                "role": role, "unlimited": unlimited,
                "monthly_points": None if unlimited else policy.monthly_points[role],
                "used_points": totals["used"], "reserved_points": totals["reserved"],
                "remaining_points": None if unlimited else max(0, policy.monthly_points[role] - totals["used"] - totals["reserved"]),
                "cost_limits_enabled": policy.enforce_cost_limits,
                "cost_limit_cny": None if unlimited or not policy.enforce_cost_limits else cny(policy.monthly_points[role] * policy.cost_per_point_micros + policy.failure_budget_micros[role]),
                "cost_used_cny": cny(totals["reported_cost"]),
                "features": features,
            }

    def begin_task(self, owner: UserContext, feature: str, request_id: str | None = None,
                   expected_policy_version: int | None = None, *, now: float | None = None) -> CreditTask:
        user = _actor(owner, now)
        if feature not in FEATURES:
            raise CreditError("未知 AI 计费入口", 400, "AI_FEATURE_INVALID")
        request_id = request_id or uuid.uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", request_id):
            raise CreditError("无效 AI 请求标识", 400, "AI_REQUEST_INVALID")
        period, _reset = _month(now)
        with self._connection(True) as db:
            version, policy = self._policy(db)
            if expected_policy_version is not None and expected_policy_version != version:
                raise CreditError("AI 点数规则已更新，请刷新额度后重试", 409, "AI_POLICY_CHANGED")
            duplicate = db.execute("SELECT 1 FROM credit_tasks WHERE user_id=? AND request_id=?", (owner.id, request_id)).fetchone()
            if duplicate:
                raise CreditError("本次 AI 请求已处理或正在处理，请勿重复发送", 409, "AI_REQUEST_ALREADY_USED")
            totals = self._totals(db, owner.id, period)
            points = 0 if user["role"] == "admin" else policy.feature_points[feature]
            reason = None if user["role"] == "admin" else self._budget_reason(db, owner.id, period, policy, user["role"], totals, points)
            if reason:
                raise CreditError(*reason)
            task_id = uuid.uuid4().hex
            task_policy = policy if points else policy.model_copy(update={"enforce_cost_limits": False})
            db.execute("""
                INSERT INTO credit_tasks(id,user_id,request_id,period,feature,points,policy_version,
                    policy_json,budget_micros,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,'pending',?)
            """, (task_id, owner.id, request_id, period, feature, points, version,
                  task_policy.model_dump_json(), points * policy.cost_per_point_micros if task_policy.enforce_cost_limits else 0,
                  time.time() if now is None else now))
            return CreditTask(self, task_id, owner.id, period, feature, task_policy)

    def recover_interrupted(self) -> int:
        """仅持有执行者锁的 collector 在启动时退款；未上报用量仍为未知。"""
        with self._connection(True) as db:
            db.execute("""UPDATE credit_calls SET cost_micros=CASE WHEN reserved_micros>0 THEN reserved_micros ELSE NULL END,
                          settled=1,usage_state='missing'
                          WHERE settled=0 AND task_id IN (SELECT id FROM credit_tasks WHERE state='pending')""")
            cursor = db.execute("UPDATE credit_tasks SET state='failed',finished_at=? WHERE state='pending'", (time.time(),))
            return cursor.rowcount


@lru_cache(maxsize=8)
def get_credit_store(data_dir: Path) -> CreditStore:
    return CreditStore(Path(data_dir))


def current_credit_task() -> CreditTask | None:
    return _current_task.get()


@contextmanager
def credit_task_scope(task: CreditTask | None):
    token = _current_task.set(task)
    try:
        yield task
    finally:
        _current_task.reset(token)
