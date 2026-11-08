"""小型配置包的有界ZIP读写；只返回内容，不解压路径或执行包内文件。"""

from __future__ import annotations

import io
import stat
import zipfile
from pathlib import PurePosixPath

MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_MEMBER_BYTES = 2 * 1024 * 1024
MAX_FILES = 128


class PackageArchiveError(ValueError):
    pass


def _name(value: str) -> str:
    if (
        not value
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or PurePosixPath(value).is_absolute()
    ):
        raise PackageArchiveError("配置包包含不安全的路径")
    return value


def read_archive(data: bytes) -> dict[str, bytes]:
    if not data or len(data) > MAX_ARCHIVE_BYTES:
        raise PackageArchiveError("配置包为空或超过8MB上限")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_FILES:
                raise PackageArchiveError("配置包条目数无效或超过128项")
            total = 0
            names: set[str] = set()
            result = {}
            for info in infos:
                name = _name(info.filename.removesuffix("/") if info.is_dir() else info.filename)
                mode = stat.S_IFMT(info.external_attr >> 16)
                if mode not in (0, stat.S_IFREG, stat.S_IFDIR) or info.flag_bits & 1:
                    raise PackageArchiveError("配置包不允许链接、特殊文件或加密条目")
                if name in names:
                    raise PackageArchiveError("配置包包含重复路径")
                names.add(name)
                if info.is_dir():
                    continue
                total += info.file_size
                if info.file_size > MAX_MEMBER_BYTES or total > MAX_TOTAL_BYTES:
                    raise PackageArchiveError("配置包解压内容超过大小上限")
                with archive.open(info) as member:
                    content = member.read(MAX_MEMBER_BYTES + 1)
                if len(content) != info.file_size or len(content) > MAX_MEMBER_BYTES:
                    raise PackageArchiveError("配置包条目长度无效")
                result[name] = content
            return result
    except PackageArchiveError:
        raise
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile, NotImplementedError) as exc:
        raise PackageArchiveError("不是有效的配置包ZIP文件") from exc


def build_archive(entries: dict[str, bytes]) -> bytes:
    if not entries or len(entries) > MAX_FILES:
        raise PackageArchiveError("配置包条目数超过上限")
    if sum(len(value) for value in entries.values()) > MAX_TOTAL_BYTES or any(
        len(value) > MAX_MEMBER_BYTES for value in entries.values()
    ):
        raise PackageArchiveError("配置包内容超过大小上限")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in sorted(entries.items()):
            archive.writestr(_name(name), value)
    result = output.getvalue()
    if len(result) > MAX_ARCHIVE_BYTES:
        raise PackageArchiveError("配置包超过8MB上限")
    return result
