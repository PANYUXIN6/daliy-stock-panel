"""小帅工作台后端。"""

import json
import sys
from contextlib import suppress
from pathlib import Path

# 版本号从 frontend/package.json 加载，与页面版本徽标同源。
# 缺少元数据时回退硬编码值，版本读取不得阻塞服务启动。
_FALLBACK_VERSION = "0.3.8"


def _load_version() -> str:
    pkg = Path(__file__).resolve().parents[2] / "frontend" / "package.json"
    try:
        return json.loads(pkg.read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError, TypeError):
        return _FALLBACK_VERSION


__version__ = _load_version()

# 源码与容器日志统一使用 UTF-8，避免 SDK 输出含 emoji 的名称时受终端编码影响。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        with suppress(Exception):
            _stream.reconfigure(encoding="utf-8", errors="replace")
