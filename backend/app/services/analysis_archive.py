"""流式分析在执行服务完成归档后才发送 done，浏览器无需再保存结果。"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path


async def archive_analysis(stream: AsyncIterator[str], save: Callable[[dict], dict],
                           data_dir: Path, dataset: str, fields: dict) -> AsyncIterator[str]:
    from app.services.shared_runtime import publish_data_update

    meta = {}
    content = []
    failed = False
    async for chunk in stream:
        event = json.loads(chunk)
        if event.get("type") == "meta":
            meta = {key: value for key, value in event.items() if key != "type"}
        elif event.get("type") == "delta":
            content.append(event.get("content", ""))
        elif event.get("type") == "error":
            failed = True
        elif event.get("type") == "done" and not failed and content:
            report = save({**meta, **fields, "content": "".join(content)})
            publish_data_update(data_dir, [dataset])
            chunk = json.dumps({**event, "archived": True, "report_id": report["id"]}, ensure_ascii=False)
        yield chunk
