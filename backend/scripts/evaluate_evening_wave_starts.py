"""离线评价正式晚报的波次启动队列。"""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.market_time import cn_now
from app.services.evening_report_inputs import load_inputs, prepare_evaluation_inputs, require_ready
from app.services.evening_report_observation_study import compare_observations
from app.services.evening_report_start_study import DEFAULT_END_RETURN, DEFAULT_SESSIONS, run_study
from app.services.evening_report_store import EveningReportStore, atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--sessions", type=int, default=DEFAULT_SESSIONS)
    parser.add_argument("--end-return", type=float, default=DEFAULT_END_RETURN)
    parser.add_argument("--observation-baseline", action="store_true", help="同时评价晚报名单与5日涨幅基线")
    args = parser.parse_args()
    if args.sessions < 1 or not 0 < args.end_return < float("inf"):
        parser.error("窗口须为正交易日数，期末目标须为正的小数比例")
    evidence = require_ready(args.data_dir, args.as_of)
    inputs = load_inputs(args.data_dir, args.as_of, evidence)
    store = EveningReportStore(args.data_dir)
    result = run_study(store, inputs, evaluated_at=cn_now().isoformat(),
                       sessions=args.sessions, end_return=args.end_return)
    if args.observation_baseline:
        prepare_evaluation_inputs(inputs)
        result["observation_baseline"] = compare_observations(store, inputs)
    require_ready(args.data_dir, args.as_of, input_id=evidence["input_id"])
    output = store.root / "start_research.json"
    atomic_json(output, result)
    print(output)


if __name__ == "__main__":
    main()
