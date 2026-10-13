"""Local web/collector channel credential; never accepted as a user identity."""
import os
import secrets
import tempfile
from contextlib import suppress
from pathlib import Path


def credential(data_dir: Path) -> str:
    root = Path(data_dir) / 'runtime'
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'internal.key'
    if not path.exists():
        fd, temporary = tempfile.mkstemp(prefix='.internal-key-', dir=root)
        try:
            with os.fdopen(fd, 'w', encoding='ascii') as file:
                file.write(secrets.token_hex(32))
                file.flush()
                os.fsync(file.fileno())
            with suppress(FileExistsError):
                os.link(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    value = path.read_text(encoding='ascii').strip()
    if len(value) != 64:
        raise RuntimeError('内部服务凭据无效')
    return value
