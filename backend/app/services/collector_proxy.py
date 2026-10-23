"""网页到唯一执行服务的薄 HTTP 转发；查询批量 POST 保持本地执行。"""
from __future__ import annotations

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

# 这些 POST 是纯查询，不因 HTTP 方法而变成采集命令。
_LOCAL_POSTS = {
    '/api/kline/instruments/names', '/api/kline/daily-batch', '/api/kline/minute-batch',
    '/api/events/ticket',
}
# 执行/持有进程内状态的模块：其 GET 也必须由实际所有者回答。
_OWNER_PREFIXES = (
    '/api/pipeline', '/api/backtest', '/api/strategies', '/api/monitor-rules',
    '/api/settings', '/api/capabilities', '/api/custom/assistant',
    '/api/factors', '/api/custom-signals',
    '/api/kline/minute/fetch',
    '/api/ai/credits',
    '/api/screener/run-status',
)
_OWNER_READS = {'/api/data/status', '/api/financials/execution-status', '/api/financials/collection-options', '/api/screener/cached',
                '/api/screener/cached-summary', '/api/screener/strategies'}
_WRITE_PREFIXES = (
    '/api/kline', '/api/index', '/api/intraday', '/api/data', '/api/watchlist',
    '/api/screener', '/api/ext-data', '/api/financials', '/api/stock-analysis',
    '/api/market-recap', '/api/factors', '/api/custom-signals', '/api/lots',
    '/api/alerts', '/api/paper', '/api/analysis-menus', '/api/regime', '/api/rps',
)
_HOP_HEADERS = {'host', 'connection', 'keep-alive', 'transfer-encoding', 'content-length',
                'proxy-authenticate', 'proxy-authorization', 'te', 'trailer', 'upgrade'}


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + '/')


def collector_owns(method: str, path: str) -> bool:
    if method == 'POST' and path in _LOCAL_POSTS:
        return False
    if any(_under(path, prefix) for prefix in _OWNER_PREFIXES):
        return True
    if path in _OWNER_READS or path.startswith('/api/screener/cached-result/'):
        return True
    return method in {'POST', 'PUT', 'PATCH', 'DELETE'} and any(
        _under(path, prefix) for prefix in _WRITE_PREFIXES
    )


async def forward(request: Request):
    client = request.app.state.collector_client
    url = request.app.state.collector_url + request.url.path
    if request.url.query:
        url += '?' + request.url.query
    headers = {
        key: value for key, value in request.headers.items()
        if key.lower() not in _HOP_HEADERS
        and key.lower() != 'forwarded'
        and not key.lower().startswith('x-forwarded-')
    }
    # The browser origin was validated by the outer web authentication middleware.
    # Forwarding headers belong to that outer connection; retaining its HTTPS
    # scheme would disagree with the collector transport origin below.
    if 'origin' in headers:
        target = httpx.URL(request.app.state.collector_url)
        headers['origin'] = str(target.copy_with(path='/', query=None)).rstrip('/')
    headers.pop('referer', None)
    outgoing = client.build_request(request.method, url, headers=headers, content=await request.body())
    try:
        response = await client.send(outgoing, stream=True)
    except httpx.HTTPError:
        return JSONResponse(status_code=503, content={'detail': '采集服务不可用，请检查采集服务健康状态'})
    async def body():
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        finally:
            await response.aclose()

    return StreamingResponse(
        body(), status_code=response.status_code,
        headers={key: value for key, value in response.headers.items() if key.lower() not in _HOP_HEADERS},
    )


async def proxy_middleware(request: Request, call_next):
    if request.app.state.service_role == 'web' and request.url.path.startswith('/internal/'):
        return JSONResponse(status_code=404, content={'detail': 'Not found'})
    if request.app.state.service_role == 'web' and collector_owns(request.method, request.url.path):
        return await forward(request)
    response = await call_next(request)
    if (request.app.state.service_role == 'collector'
            and request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}
            and response.status_code < 400
            and any(_under(request.url.path, prefix) for prefix in (
                '/api/settings', '/api/capabilities', '/api/strategies',
                '/api/monitor-rules', '/api/factors', '/api/watchlist',
            ))):
        import asyncio

        from app.services.shared_runtime import publish_data_update, publish_quotes
        await asyncio.to_thread(publish_quotes, request.app.state.quote_service)
        publish_data_update(request.app.state.repo.store.data_dir, ['config'])
    return response
