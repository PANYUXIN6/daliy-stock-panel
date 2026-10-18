"""扶摇(同花顺金融数据 API) HTTP 客户端。

职责: 认证、统一信封解包、快照分页、单标的日K、市场 dump 下载。不知道 provider / services 层。
文档: https://fuyao.aicubes.cn/docs — REST + X-api-key, 响应信封 {code, message, data}。

时间字段口径: 所有 *ms 字段(含 start/end 入参与 date_ms/ex_date_ms 出参)均为
北京时间零点对应的 epoch ms(= UTC 前一日 16:00), 由 provider 层统一 +8h 换算。
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import CancelledError
from pathlib import Path
from typing import ClassVar

import httpx

from app.data_providers.base import ProviderRateLimitError

logger = logging.getLogger(__name__)

BASE_URL = "https://fuyao.aicubes.cn"

# 单页 6000 覆盖全市场(实测 ~5600 含北交所, 2026-08 服务端不截断 limit=6000),
# 一次请求拉完; 分页循环兜底未来标的扩容或服务端改为截断的场景。
_SNAPSHOT_PAGE_SIZE = 6000
_SNAPSHOT_MAX_PAGES = 50
_PAGE_INTERVAL_S = 0.15  # 页间隔, 降低触发限频 (code=4001) 的概率

# 官方仅说明动态限流，没有固定 QPS：沿用项目请求间隔，并让所有入口共用。
# 预算限于当前采集进程，不代表供应商的账户级额度。
# 全市场长批次曾在 0.12 秒间隔下触发动态限流，降低持续请求频率。
_REQUEST_INTERVAL_S = 0.5
_REQUEST_SLOTS = threading.BoundedSemaphore(2)
_request_lock = threading.Lock()
_next_request_at = 0.0
_FINANCIAL_REQUEST_INTERVAL_S = 2.0
_financial_request_lock = threading.Lock()
_next_financial_request_at = 0.0


def _wait_request_slot(cancel_event: threading.Event | None = None) -> None:
    global _next_request_at
    with _request_lock:
        delay = _next_request_at - time.monotonic()
        if delay > 0:
            if cancel_event is None:
                time.sleep(delay)
            elif cancel_event.wait(delay):
                raise CancelledError("扶摇取数已取消")
        _next_request_at = time.monotonic() + _REQUEST_INTERVAL_S


def _wait_financial_request_slot(cancel_event: threading.Event | None = None) -> None:
    """财务长批次独立降频；其他行情请求仍沿用原共享间距。"""
    global _next_financial_request_at
    with _financial_request_lock:
        delay = _next_financial_request_at - time.monotonic()
        if delay > 0:
            if cancel_event is None:
                time.sleep(delay)
            elif cancel_event.wait(delay):
                raise CancelledError("扶摇财务取数已取消")
        _next_financial_request_at = time.monotonic() + _FINANCIAL_REQUEST_INTERVAL_S


class FuyaoError(Exception):
    """扶摇接口错误(配置缺失 / 网络失败 / 信封 code != 0)。"""


class FuyaoRateLimitError(FuyaoError, ProviderRateLimitError):
    """HTTP 429 / code=4001：结束当前同步，不重试。"""


class FuyaoDataUnavailableError(FuyaoError):
    """供应商明确返回 code=5003、Data source unavailable。"""


class FuyaoClient:
    """扶摇 REST 客户端 (线程安全: httpx.Client 可并发复用)。"""

    def __init__(self, api_key: str, base_url: str = BASE_URL, timeout: float = 20.0) -> None:
        if not api_key:
            raise FuyaoError("未配置 FUYAO_API_KEY")
        self.last_server_ts = 0  # 最近一页响应里的服务端时间戳(ms), 供行情归属
        self._http = httpx.Client(
            base_url=base_url,
            headers={"X-api-key": api_key},
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    # ---- 内部 ----
    def _get(self, path: str, params: dict, *, cancel_event: threading.Event | None = None) -> dict:
        """GET + 信封解包。code != 0 时抛 FuyaoError(含 code 与 message)。"""
        with _REQUEST_SLOTS:
            # 全市场逐股采集会遇到偶发断流；只重试幂等 GET 的传输错误，
            # 每次仍经过共享限速。HTTP/业务限流及数据错误直接传播。
            attempt = 0
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise CancelledError("扶摇取数已取消")
                _wait_request_slot(cancel_event)
                if path.startswith("/api/a-share/financials/"):
                    _wait_financial_request_slot(cancel_event)
                if cancel_event is not None and cancel_event.is_set():
                    raise CancelledError("扶摇取数已取消")
                try:
                    return self._request(path, params)
                except FuyaoError as exc:
                    if (attempt >= 2 or not isinstance(exc.__cause__, (
                            httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError))):
                        raise
                    attempt += 1
                    logger.warning("扶摇传输中断，重试 %d/2: %s", attempt, path)
                    delay = 0.5 * attempt
                    if cancel_event is not None:
                        if cancel_event.wait(delay):
                            raise CancelledError("扶摇取数已取消") from exc
                    else:
                        time.sleep(delay)

    def _request(self, path: str, params: dict) -> dict:
        try:
            resp = self._http.get(path, params=params)
        except httpx.HTTPError as e:
            raise FuyaoError(f"网络请求失败: {e}") from e
        if resp.status_code == 429:
            raise FuyaoRateLimitError(f"扶摇限流 HTTP 429: {path}")
        if resp.status_code != 200:
            raise FuyaoError(f"HTTP {resp.status_code}: {path}")
        try:
            payload = resp.json()
        except ValueError as e:
            raise FuyaoError(f"响应不是 JSON: {path}") from e
        code = payload.get("code")
        if code in (4001, "4001"):
            raise FuyaoRateLimitError(f"扶摇限流 code={code}: {path}")
        if code in (5003, "5003") and payload.get("message") == "Data source unavailable":
            raise FuyaoDataUnavailableError(f"扶摇接口错误 code={code}: Data source unavailable ({path})")
        if code not in (0, "0", None):
            raise FuyaoError(f"扶摇接口错误 code={code}: {payload.get('message', '')} ({path})")
        return payload.get("data") or {}

    # ---- 快照 ----
    def snapshot_page(
        self, limit: int = _SNAPSHOT_PAGE_SIZE, offset: int = 0
    ) -> tuple[list[dict], int]:
        """拉取一页 A 股全市场快照。返回 (rows, total), total 为全市场总数。

        实测响应(2026-08): data={timestamp, total, item}; 官方文档示例为
        data={count, data}。两者都兼容, 以实测为准。
        """
        data = self._get("/api/a-share/prices/snapshot", {"limit": limit, "offset": offset})
        try:
            self.last_server_ts = int(data.get("timestamp") or 0)
        except (TypeError, ValueError):
            self.last_server_ts = 0
        rows = data.get("item")
        if not isinstance(rows, list):
            rows = data.get("data") if isinstance(data.get("data"), list) else []
        raw_total = data.get("total")
        if raw_total is None:
            raw_total = data.get("count") or 0
        try:
            total = int(raw_total or 0)
        except (TypeError, ValueError):
            total = 0
        return rows, total

    def snapshot_all(self) -> tuple[list[dict], int]:
        """分页拉取全市场快照。返回 (rows, 服务端时间戳ms)。

        服务端时间戳用于行情归属; 缺失时返回 0, 由调用方退回本地时间。
        空数据 / 中途失败时抛 FuyaoError。
        """
        out: list[dict] = []
        server_ts = 0
        offset = 0
        for page in range(_SNAPSHOT_MAX_PAGES):
            if page > 0:
                time.sleep(_PAGE_INTERVAL_S)
            rows, total = self.snapshot_page(offset=offset)
            if not rows:
                break
            out.extend(rows)
            if not server_ts:
                server_ts = self.last_server_ts
            if total and len(out) >= total:
                break
            offset += len(rows)
        if not out:
            raise FuyaoError("全市场快照为空")
        return out, server_ts

    # ---- 指数快照 ----
    def index_snapshot(self, thscodes: list[str]) -> tuple[list[dict], int]:
        """拉取指数行情快照 (/api/a-share-index/prices/snapshot)。返回 (rows, 服务端时间戳ms)。

        与 A 股快照不同: 必须显式传 thscodes (逗号分隔), 无全量枚举;
        单次批量上限实测 627 个代码 (~6.3KB 参数, 超出 HTTP 400);
        混入未知代码整批失败 (code=1002 连坐), 调用方需自行过滤。
        覆盖范围: 沪深交易所指数 + 同花顺板块指数, 无北交所 (官方文档明确)。
        """
        if not thscodes:
            return [], 0
        joined = ",".join(thscodes[:627])
        data = self._get("/api/a-share-index/prices/snapshot", {"thscodes": joined})
        try:
            server_ts = int(data.get("timestamp") or 0)
        except (TypeError, ValueError):
            server_ts = 0
        rows = data.get("item")
        if not isinstance(rows, list):
            rows = []
        return rows, server_ts

    # ---- 历史日K ----
    def historical_kline(
        self, thscode: str, start_ms: int, end_ms: int, adjust: str = "none"
    ) -> list[dict]:
        """单标的日K(interval=1d 固定)。单次窗口 ≤10 年, 超出由调用方分片。

        adjust 必须显式传 "none" 取原始价 — 服务端默认是 forward(前复权),
        官方前复权序列事件间存在逐日漂移, 项目内禁止使用。
        返回 data.item 原始行: {date_ms, open_price, high_price, low_price,
        close_price, volume(股), turnover(元)}。
        """
        data = self._get(
            "/api/a-share/prices/historical",
            {
                "thscode": thscode,
                "interval": "1d",
                "adjust": adjust,
                "start": int(start_ms),
                "end": int(end_ms),
            },
        )
        rows = data.get("item")
        return rows if isinstance(rows, list) else []

    # ---- 财务 ----
    # 端点均单标的(thscode 不接受逗号)。取数模式二选一: limit=最近N期 或 start/end 区间,
    # 这里只用 limit。period=quarterly 覆盖每个季度末(含年报期), 与项目"各报告期累积"口径一致。
    _STATEMENT_ENDPOINTS: ClassVar[dict[str, str]] = {
        "income": "income-statements",
        "balance_sheet": "balance-sheets",
        "cash_flow": "cash-flow-statements",
    }

    def financial_statements(
        self, stmt: str, thscode: str, limit: int = 1,
        *, cancel_event: threading.Event | None = None,
    ) -> list[dict]:
        """单标的财务报表多期序列。stmt: income | balance_sheet | cash_flow。

        返回 data.item 原始行: 共有元数据(thscode/period/fiscal_year/fiscal_period/
        report_date_ms/period_end_ms/currency) + 各表字段。行内 null 表示该期未披露。
        """
        endpoint = self._STATEMENT_ENDPOINTS.get(stmt)
        if endpoint is None:
            raise FuyaoError(f"未知财务报表类型: {stmt}")
        data = self._get(
            f"/api/a-share/financials/{endpoint}",
            {"thscode": thscode, "period": "quarterly", "limit": max(1, min(20, limit))},
            cancel_event=cancel_event,
        )
        rows = data.get("item")
        return rows if isinstance(rows, list) else []

    def financial_indicators(
        self, thscode: str, report: str, *, cancel_event: threading.Event | None = None,
    ) -> list[dict]:
        """单标的单报告期财务指标(report 格式 yyyy-N, N=1..4 对应一季报..年报)。

        返回 data.abilities 原始列表 [{ability, indicators: [{index_id, value}]}];
        value 为保留原始精度的数值字符串(百分制指标即百分点数), 缺失为 null。
        未披露报告期实测返回 code=5003(文档写 3002, 以实测为准) → 经 _get 抛 FuyaoError,
        由调用方按"该期无数据"处理。
        """
        data = self._get(
            "/api/a-share/financials/indicators",
            {"thscode": thscode, "report": report},
            cancel_event=cancel_event,
        )
        abilities = data.get("abilities")
        return abilities if isinstance(abilities, list) else []

    def valuations_snapshot(
        self, thscodes: list[str], *, cancel_event: threading.Event | None = None,
    ) -> list[dict]:
        """批量估值快照(pe_ttm/pe_mrq/pb_mrq/ps_ttm/pcf_ttm), 数值为最新口径。

        服务端单次上限 100 只(超出 code=1003), 分批由调用方负责。
        返回 data.item 原始行。
        """
        data = self._get(
            "/api/a-share/valuations/snapshot",
            {"thscodes": ",".join(thscodes[:100])},
            cancel_event=cancel_event,
        )
        rows = data.get("item")
        return rows if isinstance(rows, list) else []

    def price_snapshot_batch(
        self, thscodes: list[str], *, cancel_event: threading.Event | None = None,
    ) -> list[dict]:
        """按 thscodes 批量行情快照(最新价等), 用于估值推导的分母。

        与全市场分页快照同一端点; thscodes 显式传入时不分页。
        返回 data.item 原始行。
        """
        data = self._get(
            "/api/a-share/prices/snapshot",
            {"thscodes": ",".join(thscodes[:100])},
            cancel_event=cancel_event,
        )
        rows = data.get("item")
        return rows if isinstance(rows, list) else []

    def trading_days(self) -> list[dict]:
        """近一年 A 股交易日序列 (固定窗口 [今日-1年, 今日], 无入参)。

        返回 data.item 原始行: {date_ms(上海零点), date(yyyyMMdd)}。
        供交易日探针判定「今天在列表内 ⇔ 交易日」。
        """
        data = self._get("/api/a-share/calendar/trading-days", {})
        rows = data.get("item")
        return rows if isinstance(rows, list) else []

    def dragon_tiger_list(self, board_type: str = "all", date: str | None = None) -> dict:
        """龙虎榜榜单 (特色数据)。board_type: all | org | hot_money。

        返回 data 原始容器: {trade_date, count, stock_count, stock_items[],
        hot_money_items[]}。省略 date 时服务端自动取最近已发布交易日;
        显式传非交易日返回 code=1002 (由调用方做交易日回退)。
        实测字段(2026-08): stock_items 12 个基础字段, org 榜额外带 4 个机构字段;
        文档中的 limit_reason / amount 实际不返回。
        """
        params: dict = {"board_type": board_type}
        if date:
            params["date"] = date
        return self._get("/api/a-share/special-data/dragon-tiger-list", params)

    def short_term_benchmark(self, date: str | None = None) -> dict:
        """短线风向标竞价基准 (同花顺竞价筛选, 每日约 5~6 只)。

        返回 data 原始容器: {date, date_ms, item[]}。item 行:
        {thscode, ticker, name, auction_pct, tags[]}。支持一年内历史日期;
        显式传非交易日返回 code=1002 (由调用方做交易日回退)。
        """
        params: dict = {}
        if date:
            params["date"] = date
        return self._get("/api/a-share/auction/short-term-benchmark", params)

    # ---- 市场 dump ----
    def dump_download_url(self, dump_kind: str) -> dict:
        """获取 dump 预签名下载信息(约 300s 有效)。

        dump_kind: adjustment-factors | daily-k-10d | daily-k。
        返回 {presigned_url, presigned_url_expires_at, expires_in_seconds};
        release 版本号(如 20260828)嵌在 presigned_url 的 releases/<date>/ 路径中,
        供调用方做缓存版本管理。
        """
        return self._get(f"/api/dump/market-dumps/{dump_kind}/download-url", {})

    def download_dump(self, dump_kind: str, dest: Path) -> Path:
        """下载 dump 到 dest(先写 .part 临时文件, 成功后原子改名)。失败抛 FuyaoError。

        预签名 URL 指向对象存储, 请求不得携带 X-api-key 头 → 用独立裸请求,
        不经过持有认证头的 self._http。
        """
        url = str(self.dump_download_url(dump_kind).get("presigned_url") or "")
        if not url:
            raise FuyaoError(f"dump {dump_kind} 未返回预签名 URL")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        try:
            with httpx.stream("GET", url, timeout=120.0, follow_redirects=True) as resp:
                if resp.status_code != 200:
                    raise FuyaoError(f"dump {dump_kind} 下载失败 HTTP {resp.status_code}")
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_bytes(1 << 20):
                        fh.write(chunk)
            tmp.replace(dest)
        except httpx.HTTPError as e:
            raise FuyaoError(f"dump {dump_kind} 下载网络失败: {e}") from e
        finally:
            tmp.unlink(missing_ok=True)
        return dest
