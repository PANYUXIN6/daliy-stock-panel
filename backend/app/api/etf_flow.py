"""ETF资金看板HTTP边界。GET只读；同步由collector共享任务槽执行。"""
from datetime import date

from fastapi import APIRouter, HTTPException, Query, Request

from app.market_time import cn_today
from app.services import etf_flow, etf_flow_sync

router = APIRouter(prefix="/api/etf-flow", tags=["etf-flow"])


@router.get("")
def get_board(request: Request, trade_date: date | None = None, window: int = Query(60, ge=0, le=120)):
    if 0 < window < 5:
        raise HTTPException(status_code=422, detail="历史窗口应为5至120个交易日，或0表示全部历史")
    if trade_date is not None and trade_date > cn_today():
        raise HTTPException(status_code=422, detail="不能查询未来交易日")
    return etf_flow.read_board(request.app.state.repo, trade_date, window)


@router.post("/sync")
async def sync_board(request: Request, days: int = Query(22, ge=22, le=120), full_history: bool = False):
    return await etf_flow_sync.submit(request.app.state.repo, request.app.state.capabilities,
                                      getattr(request.app.state, "quote_service", None), days=days, full_history=full_history)
