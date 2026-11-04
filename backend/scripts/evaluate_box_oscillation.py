"""箱体边界核对离线工具；只读取显式 JSON 文件，输出文件须尚不存在。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.box_evaluation import (
    EvaluationDataset,
    evaluate_dataset,
    verify_regression_baseline,
)

BASELINE = Path(__file__).resolve().parents[1] / "tests/fixtures/box_oscillation/synthetic_baseline_v1.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="箱体识别离线评估，不改变线上指标")
    commands = parser.add_subparsers(dest="command", required=True)
    baseline = commands.add_parser("baseline", help="逐字段核对冻结的合成回归基线")
    baseline.add_argument("--baseline", type=Path, default=BASELINE)
    baseline.add_argument("--output", type=Path, required=True)
    evaluate = commands.add_parser("evaluate", help="评估独立标注的完整本地 JSON 快照")
    evaluate.add_argument("--dataset", type=Path, required=True)
    evaluate.add_argument("--output", type=Path, required=True)
    schema = commands.add_parser("schema", help="导出数据与标注格式 JSON Schema")
    schema.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise ValueError("输出文件已存在，请使用新的报告路径")
        if args.command == "baseline":
            report = verify_regression_baseline(args.baseline)
        elif args.command == "evaluate":
            dataset = EvaluationDataset.model_validate_json(args.dataset.read_text(encoding="utf-8"))
            report = evaluate_dataset(dataset)
        else:
            report = EvaluationDataset.model_json_schema()
        # 独占新文件，避免评估报告或输入快照被静默覆盖。
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
    except (ValueError, OSError) as exc:
        parser.exit(2, f"评估失败：{exc}\n")
    if args.command == "baseline":
        print(f"合成回归基线：{'通过' if report['passed'] else '未通过'}；{len(report['cases'])} 个样本。人工校准待完成。")
        return 0 if report["passed"] else 1
    if args.command == "evaluate":
        print(f"已生成 {len(report['case_reports'])} 份分组评估；校准状态：{report['calibration_status']}。")
    else:
        print("标注格式已导出。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
