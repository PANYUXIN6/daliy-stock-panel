"""当前账号的声明式策略包；兼容策略包HTTP形状，不接受可执行源码或整站备份。"""

from __future__ import annotations

import json
import math
import re
import tempfile
from collections import Counter
from dataclasses import replace
from datetime import datetime
from pathlib import Path, PurePosixPath

from app.factors import registry, store
from app.factors.dsl import BASE_COLUMNS
from app.market_time import CN_TZ
from app.services.auth import AuthError, require_current_session
from app.services.package_archive import PackageArchiveError, build_archive, read_archive
from app.services.path_locks import path_lock
from app.services.private_storage import identifier
from app.services.user_context import current_user, user_data_dir
from app.strategy import custom_signals
from app.strategy.engine import StrategyEngine

BUNDLE_FORMAT = "tick-strategy-bundle"
BUNDLE_VERSION = 1
CONTRACT = "personal-declarations-v1"
_PATHS = {
    "strategies/composite": "strategies",
    "user_data/strategy_overrides": "strategy_overrides",
    "user_data/custom_signals": "custom_signals",
    "user_data/custom_factors": "custom_factors",
}
_FACTOR_REF = re.compile(r"\b(?:uf_|cf_)[a-z0-9_]+\b")


class StrategyBundleError(ValueError):
    pass


class StrategyBundlePermissionError(StrategyBundleError):
    pass


def _json(content: bytes) -> dict:
    def reject(value):
        raise ValueError("JSON不允许非有限数值")

    result = json.loads(content, parse_constant=reject)
    if not isinstance(result, dict):
        raise ValueError("包内JSON必须为对象")
    return result


def _bytes(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode()


def _target(data_dir: Path, path: str) -> Path:
    root = user_data_dir(data_dir)
    target = root.joinpath(*PurePosixPath(path).parts)
    if not target.resolve().is_relative_to(root):
        raise StrategyBundleError("策略包路径超出当前账号目录")
    for candidate in (target, *target.parents):
        if candidate == root:
            break
        if candidate.is_symlink():
            raise StrategyBundleError("策略包不允许读取或写入符号链接")
    return target


def _definitions(data_dir: Path, directory: str) -> dict[str, dict]:
    result = {}
    folder = _target(data_dir, directory)
    for path in sorted(folder.glob("*.json")):
        target = _target(data_dir, f"{directory}/{path.name}")
        result[path.stem] = _json(target.read_bytes())
    return result


def _fields(document: dict) -> set[str]:
    refs = set(document.get("scoring") or {})
    refs.update(document.get("required_features") or [])
    refs.update(document.get("entry_signals") or [])
    refs.update(document.get("exit_signals") or [])
    for condition in [*(document.get("conditions") or []), *(document.get("overlay_filter") or [])]:
        if isinstance(condition, dict):
            refs.add(condition.get("left", ""))
            right = condition.get("right")
            if isinstance(right, str) and right.startswith("field:"):
                refs.add(right[6:])
    return {ref for ref in refs if isinstance(ref, str) and ref}


def _factor_refs(definition: dict) -> set[str]:
    if definition.get("kind", "custom") == "composite":
        return set(definition.get("members") or {})
    return set(_FACTOR_REF.findall(str(definition.get("formula", ""))))


def _signal_id(ref: str) -> str | None:
    if ref.startswith("csgi_"):
        return ref[5:]
    if ref.startswith("csg_"):
        return ref[4:]
    return None


def _signal_access() -> None:
    owner = current_user()
    role = require_current_session()["role"] if owner.session_token else owner.role
    if role != "admin":
        raise StrategyBundlePermissionError("自定义信号包仅管理员可导入或导出")


def build_bundle(data_dir: Path, engine, strategy_id: str):
    """闭包仅取当前账号的声明、调参、信号和因子；其他策略保留稳定ID引用。"""
    try:
        identifier(strategy_id)
        entries = {}
        references: set[str] = set()
        warnings: set[str] = set()
        factors = _definitions(data_dir, "user_data/custom_factors")
        signals = _definitions(data_dir, "user_data/custom_signals")
        visiting: set[str] = set()
        seen: set[str] = set()

        def factor(fid):
            key = f"factor:{fid}"
            if key in visiting:
                raise StrategyBundleError("因子存在循环依赖")
            if key in seen:
                return
            if fid not in factors:
                if registry.get_factor(fid) is None and fid not in BASE_COLUMNS:
                    warnings.add(f"缺少依赖: {fid}")
                return
            visiting.add(key)
            for ref in _factor_refs(factors[fid]):
                factor(ref)
            visiting.remove(key)
            seen.add(key)
            entries[f"user_data/custom_factors/{identifier(fid)}.json"] = _bytes(factors[fid])

        def fields(document):
            for ref in _fields(document):
                sid = _signal_id(ref)
                if sid is not None:
                    signal(sid)
                elif ref.startswith(("uf_", "cf_")):
                    factor(ref)

        def signal(sid):
            _signal_access()
            key = f"signal:{sid}"
            if key in seen:
                return
            if sid not in signals:
                warnings.add(f"缺少依赖: csg_{sid}")
                return
            seen.add(key)
            entries[f"user_data/custom_signals/{identifier(sid)}.json"] = _bytes(signals[sid])
            fields(signals[sid])

        def strategy(sid):
            key = f"strategy:{sid}"
            if key in visiting:
                raise StrategyBundleError("策略存在循环依赖")
            if key in seen:
                return
            definition = engine.get(sid)
            if definition.meta.get("research_only"):
                raise StrategyBundleError("研究草稿不可打包")
            visiting.add(key)
            private = _target(data_dir, f"strategies/composite/{identifier(sid)}.py")
            if (
                definition.file_path is not None
                and definition.file_path.resolve() == private.resolve()
            ):
                declaration = StrategyEngine._load_composite_declaration(private)
                entries[f"strategies/composite/{sid}.json"] = _bytes(
                    {
                        "id": sid,
                        "execution_backend": "composite",
                        "meta": declaration.META,
                    }
                )
            else:
                references.add(sid)
            override_path = _target(data_dir, f"user_data/strategy_overrides/{sid}.json")
            override = _json(override_path.read_bytes()) if override_path.exists() else {}
            if override:
                entries[f"user_data/strategy_overrides/{sid}.json"] = _bytes(override)
            fields(definition.meta)
            fields(override)
            if definition.composite is not None:
                children = override.get("children") or definition.meta["children"]
                for child in children:
                    strategy(child["strategy_id"])
            visiting.remove(key)
            seen.add(key)

        strategy(strategy_id)
        counts = dict(Counter(_PATHS[str(PurePosixPath(name).parent)] for name in entries))
        manifest = {
            "format": BUNDLE_FORMAT,
            "version": BUNDLE_VERSION,
            "contract": CONTRACT,
            "strategy_id": strategy_id,
            "label": engine.get(strategy_id).meta.get("name", strategy_id),
            "created_at": datetime.now(CN_TZ).isoformat(),
            "references": sorted(references),
            "counts": counts,
            "warnings": sorted(warnings),
            "entries": [
                {"path": name, "kind": _PATHS[str(PurePosixPath(name).parent)]}
                for name in sorted(entries)
            ],
        }
        entries["manifest.json"] = _bytes(manifest)
        return build_archive(entries), f"tsp-strategy-{strategy_id}.zip", manifest
    except (StrategyBundleError, AuthError):
        raise
    except (ValueError, TypeError, KeyError, OSError, RecursionError) as exc:
        raise StrategyBundleError(f"策略包导出失败: {exc}") from exc


def _parse(data: bytes, data_dir: Path):
    entries = read_archive(data)
    if any(name.endswith(".py") for name in entries):
        raise StrategyBundleError("本地仅支持声明式JSON策略包，不支持Python源码包")
    manifest = _json(entries.pop("manifest.json", b"{}"))
    if (
        manifest.get("format") != BUNDLE_FORMAT
        or manifest.get("version") != BUNDLE_VERSION
        or manifest.get("contract") != CONTRACT
    ):
        raise StrategyBundleError("不是受支持的个人声明式策略包；源码包需由维护者部署")
    identifier(manifest["strategy_id"])
    if (
        not isinstance(manifest.get("label", ""), str)
        or not isinstance(manifest.get("warnings", []), list)
        or any(not isinstance(warning, str) for warning in manifest.get("warnings", []))
    ):
        raise StrategyBundleError("策略包说明格式无效")
    references = manifest.get("references", [])
    if not isinstance(references, list) or len(references) > 128:
        raise StrategyBundleError("策略引用列表无效")
    for sid in references:
        identifier(sid)
    listed = manifest.get("entries")
    if (
        not isinstance(listed, list)
        or any(not isinstance(item, dict) for item in listed)
        or len(listed) != len(entries)
        or {item.get("path") for item in listed} != set(entries)
    ):
        raise StrategyBundleError("策略包清单与文件不一致")
    records = []
    for item in listed:
        path = item["path"]
        relative = PurePosixPath(path)
        category = _PATHS.get(str(relative.parent))
        if category is None or relative.suffix != ".json" or item.get("kind") != category:
            raise StrategyBundleError("策略包包含白名单以外的文件")
        sid = identifier(relative.stem)
        target_name = f"strategies/composite/{sid}.py" if category == "strategies" else path
        records.append(
            {
                "path": path,
                "kind": category,
                "id": sid,
                "doc": _json(entries[path]),
                "target": _target(data_dir, target_name),
                "bytes": len(entries[path]),
                "issues": [],
            }
        )
    return manifest, records


def _analyze(data: bytes, data_dir: Path, engine):
    manifest, records = _parse(data, data_dir)
    factors = _definitions(data_dir, "user_data/custom_factors")
    signals = _definitions(data_dir, "user_data/custom_signals")
    declared = {}
    issues = []
    for item in records:
        if item["kind"] == "custom_factors":
            factors[item["id"]] = item["doc"]
        elif item["kind"] == "custom_signals":
            _signal_access()
            signals[item["id"]] = item["doc"]
        elif item["kind"] == "strategies":
            declared[item["id"]] = item["doc"]
    with registry.isolated_registry() as snapshot, tempfile.TemporaryDirectory() as directory:
        pending: set[str] = set()
        checked: set[str] = set()

        def factor(fid):
            if fid in checked or fid not in factors:
                return
            if fid in pending:
                raise ValueError(f"因子存在循环依赖: {fid}")
            pending.add(fid)
            try:
                for dep in _factor_refs(factors[fid]):
                    factor(dep)
                spec = store.to_spec(factors[fid])
                if any(not math.isfinite(weight) for _, weight in spec.components):
                    raise ValueError("复合因子权重必须为有限数值")
                snapshot[fid] = spec
                checked.add(fid)
            finally:
                pending.remove(fid)

        for fid in factors:
            # 本地未引用的坏定义不阻断其他包；被引用时仍须通过校验。
            try:
                factor(fid)
            except (ValueError, TypeError, KeyError) as exc:
                if any(item["kind"] == "custom_factors" and item["id"] == fid for item in records):
                    issues.append(str(exc))
        definitions = {}
        for sid, document in declared.items():
            if (
                document.get("id") != sid
                or document.get("execution_backend") != "composite"
                or not sid.startswith("composite_")
            ):
                raise ValueError("只允许composite_开头的个人叠加声明")
            meta = document["meta"]
            if not isinstance(meta, dict) or meta.get("id") != sid:
                raise ValueError("声明ID与文件名不一致")
            folder = Path(directory) / "composite"
            folder.mkdir(exist_ok=True)
            path = folder / f"{sid}.py"
            path.write_text(f"META = {meta!r}\nEXECUTION_BACKEND = 'composite'\n", encoding="utf-8")
            definitions[sid] = StrategyEngine._load_file(path, declarative=True)
            if any(
                not math.isfinite(child.weight) for child in definitions[sid].composite.children
            ):
                raise ValueError("叠加策略权重必须为有限数值")

        def strategy(sid):
            if sid in definitions:
                return definitions[sid]
            result = engine.get(sid)
            if result.meta.get("research_only"):
                raise ValueError(f"依赖策略不可用: {sid}")
            return result

        for sid in set(manifest["references"]) | {manifest["strategy_id"]}:
            try:
                strategy(sid)
            except ValueError:
                issues.append(f"缺少子策略: {sid}")
        for sid, definition in definitions.items():
            available = {
                **{row["id"]: engine.get(row["id"]) for row in engine.list_strategies()},
                **definitions,
            }
            error = StrategyEngine._validate_composite_references(sid, definition, available)
            if error:
                issues.append(error)

        from app.api.strategy import _validate_overlay_config, _validate_scoring_config

        for item in records:
            kind, sid, doc, target = item["kind"], item["id"], item["doc"], item["target"]
            try:
                if kind == "custom_factors":
                    if doc.get("id") != sid or sid not in checked:
                        raise ValueError(f"因子定义无效: {sid}")
                elif kind == "custom_signals":
                    if doc.get("id") != sid:
                        raise ValueError("信号ID与文件名不一致")
                    custom_signals.validate(doc)
                elif kind == "strategy_overrides":
                    definition = strategy(sid)
                    _validate_overlay_config(doc, definition)
                    _validate_scoring_config(doc)
                    if doc.get("children"):
                        from app.strategy.engine import _parse_composite_children

                        if definition.composite is None:
                            raise ValueError("非叠加策略不允许children配置")
                        children = _parse_composite_children(doc["children"])
                        if any(not math.isfinite(child.weight) for child in children.children):
                            raise ValueError("叠加策略权重必须为有限数值")
                        for child in children.children:
                            if strategy(child.strategy_id).composite is not None:
                                raise ValueError("本地不支持嵌套叠加策略")
                        error = StrategyEngine._validate_composite_references(
                            sid,
                            replace(definition, composite=children),
                            {
                                child.strategy_id: strategy(child.strategy_id)
                                for child in children.children
                            },
                        )
                        if error:
                            raise ValueError(error)
                if kind == "strategies":
                    fields = _fields(doc["meta"])
                    item["payload"] = (
                        f"META = {doc['meta']!r}\nEXECUTION_BACKEND = 'composite'\n".encode()
                    )
                else:
                    fields = _fields(doc)
                    item["payload"] = _bytes(doc)
                for ref in fields:
                    signal_id = _signal_id(ref)
                    if (signal_id is not None and signal_id not in signals) or (
                        ref.startswith(("uf_", "cf_")) and ref not in checked
                    ):
                        issues.append(f"缺少依赖: {ref}")
                if kind == "strategies":
                    try:
                        existing = engine.get(sid)
                    except ValueError:
                        existing = None
                    if existing is not None and (
                        existing.file_path is None
                        or existing.file_path.resolve() != target.resolve()
                    ):
                        item["action"] = "conflict"
                    elif target.exists():
                        item["action"] = (
                            "reuse" if _json_composite(target) == doc["meta"] else "conflict"
                        )
                    else:
                        item["action"] = "create"
                else:
                    item["action"] = (
                        "reuse"
                        if target.exists() and _json(target.read_bytes()) == doc
                        else "conflict"
                        if target.exists()
                        else "create"
                    )
            except Exception as exc:  # noqa: BLE001 - 每个资源的错误只进入只读预览
                item["issues"].append(getattr(exc, "detail", None) or str(exc))
    issues.extend(error for item in records for error in item["issues"])
    preview = {
        "strategy_id": manifest["strategy_id"],
        "label": manifest.get("label", manifest["strategy_id"]),
        "created_at": manifest.get("created_at"),
        "counts": dict(Counter(item["kind"] for item in records)),
        "warnings": manifest.get("warnings", []),
        "missing_deps": sorted(set(issues)),
        "entries": [
            {key: item.get(key) for key in ("path", "kind", "id", "action", "bytes")}
            for item in records
        ],
    }
    preview["can_apply"] = not issues and not any(
        item.get("action") == "conflict" for item in records
    )
    return preview, records, {fid: snapshot[fid] for fid in checked}


def _json_composite(path):
    return StrategyEngine._load_composite_declaration(path).META


def inspect_bundle(data: bytes, data_dir: Path, engine):
    try:
        return _analyze(data, data_dir, engine)[0]
    except (StrategyBundleError, AuthError):
        raise
    except (PackageArchiveError, ValueError, KeyError, TypeError, OSError, RecursionError) as exc:
        raise StrategyBundleError(str(exc)) from exc


def apply_bundle(
    data: bytes,
    data_dir: Path,
    engine,
    *,
    allow_overwrite=False,
    refresh=None,
    authorize=None,
    validate_factor=None,
):
    if allow_overwrite:
        raise StrategyBundleError("本地策略包仅支持无覆盖导入")
    with path_lock(user_data_dir(data_dir) / ".runtime"):
        preview, records, specs = _analyze(data, data_dir, engine)
        if not preview["can_apply"]:
            raise StrategyBundleError("请先解决缺失依赖或同ID内容冲突")
        created = []
        owner = current_user()
        try:
            if authorize:
                authorize()
            if any(item["kind"] == "custom_signals" for item in records):
                _signal_access()
            # 再次确认请求身份/权限后，使用排他创建，绝不覆盖已存在的文件。
            for item in records:
                if item["action"] != "create":
                    continue
                target = item["target"]
                _target(data_dir, str(target.relative_to(user_data_dir(data_dir))))
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as handle:
                    created.append(target)
                    handle.write(item["payload"])
            registry.forget_user_cache(data_dir, owner.id)
            registry.all_factors()
            for fid, spec in specs.items():
                if registry.get_factor(fid) is None:
                    registry.register_factor(spec)
            for item in records:
                if item["kind"] == "custom_factors" and item["action"] == "create":
                    store.to_spec(item["doc"])
                    if validate_factor:
                        validate_factor(item["doc"])
            engine.reload()
            for item in records:
                if item["kind"] == "strategies":
                    loaded = engine.get(item["id"])
                    if loaded.file_path.resolve() != item["target"].resolve():
                        raise ValueError("策略未加载到当前账号的声明目录")
            if authorize:
                authorize()
            if any(item["kind"] == "custom_signals" for item in records):
                _signal_access()
            if refresh:
                refresh()
            custom_signals.invalidate_intraday_cache(data_dir)
        except Exception as exc:
            for target in reversed(created):
                target.unlink(missing_ok=True)
            registry.forget_user_cache(data_dir, owner.id)
            engine.reload()
            if isinstance(exc, (AuthError, StrategyBundlePermissionError)):
                raise
            raise StrategyBundleError(f"导入失败，已撤销本次新增资源: {exc}") from exc
        return {
            "imported": len(created),
            "skipped": len(records) - len(created),
            "failed": 0,
            "strategy_id": preview["strategy_id"],
            "counts": preview["counts"],
            "reload_hints": {"strategies": True},
            "warnings": [],
        }
