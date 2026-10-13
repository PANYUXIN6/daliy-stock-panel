"""Server-enforced removal of executable-code and external-token web entry points."""
from fastapi.responses import JSONResponse

_CODE_PATHS = frozenset({
    '/api/strategies/build', '/api/strategies/build/stream',
    '/api/strategies/ai/generate', '/api/strategies/ai/iterate',
    '/api/strategies/code/validate', '/api/strategies/code/save',
    '/api/strategies/ai/save',
})


async def business_middleware(request, call_next):
    path = request.url.path
    if (path.startswith('/api/api-tokens') or path.startswith('/api/tokens')
            or path in {'/api/events', '/api/events/ticket'}):
        return JSONResponse(status_code=404, content={'detail': '外部 API Token 入口已关闭'})
    if path in _CODE_PATHS or (path.startswith('/api/strategies/') and path.endswith('/publish')):
        return JSONResponse(status_code=403, content={'detail': '网页不支持保存或执行 Python 策略代码'})
    return await call_next(request)
