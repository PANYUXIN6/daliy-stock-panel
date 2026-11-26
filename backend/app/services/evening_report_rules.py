"""观察名单和波次共用的三态比较。缺少正式事实时返回 None。"""

import math

RULE_VERSION = "evening-v3"
SELECTION_POLICY_VERSION = "theme-observation-v2"
Tri = bool | None


def all_of(*values: Tri) -> Tri:
    return False if False in values else None if None in values else True


def any_of(*values: Tri) -> Tri:
    return True if True in values else None if None in values else False


def finite(value, *, positive: bool = False) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value) and (not positive or value > 0)


def compare(value, threshold: float, op: str = "ge") -> Tri:
    if not finite(value):
        return None
    if math.isclose(value, threshold, rel_tol=0, abs_tol=1e-12):
        return op in ("ge", "le")
    return {"ge": value >= threshold, "gt": value > threshold,
            "le": value <= threshold, "lt": value < threshold}[op]


def ratio(numerator, denominator):
    return numerator / denominator if finite(numerator) and finite(denominator, positive=True) else None


def judgement(parts: dict[str, Tri], *, mode: str = "and") -> dict:
    return {
        "value": (all_of if mode == "and" else any_of)(*parts.values()),
        "failed_reasons": [key for key, value in parts.items() if value is False],
        "unknown_reasons": [key for key, value in parts.items() if value is None],
        "inputs": parts,
    }
