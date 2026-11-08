import io
import stat
import zipfile

import pytest

from app.services import package_archive as archives


def zip_content(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


def test_roundtrip_and_no_extraction():
    files = {"manifest.json": b"{}", "folder/config.json": b'{"id":"one"}'}
    assert archives.read_archive(archives.build_archive(files)) == files


@pytest.mark.parametrize(
    "name",
    ["../outside.json", "/outside.json", "a/../b.json", "a\\b.json", "C:/a.json", "a//b.json"],
)
def test_rejects_unsafe_paths(name):
    with pytest.raises(archives.PackageArchiveError):
        archives.read_archive(zip_content([(name, b"{}")]))


def test_rejects_links_duplicates_and_expansion_limit(monkeypatch):
    link = zipfile.ZipInfo("link.json")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(archives.PackageArchiveError, match="链接"):
        archives.read_archive(zip_content([(link, b"../secret")]))
    with pytest.warns(UserWarning), pytest.raises(archives.PackageArchiveError, match="重复"):
        archives.read_archive(zip_content([("same.json", b"{}"), ("same.json", b"{}")]))
    monkeypatch.setattr(archives, "MAX_MEMBER_BYTES", 4)
    with pytest.raises(archives.PackageArchiveError, match="大小"):
        archives.read_archive(zip_content([("large.json", b"large payload")]))


@pytest.mark.parametrize("content", [b"", b"not a zip"])
def test_rejects_invalid_zip(content):
    with pytest.raises(archives.PackageArchiveError):
        archives.read_archive(content)
