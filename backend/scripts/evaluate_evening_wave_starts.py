"""离线评价正式晚报的波次启动队列。"""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.market_time import cn_now
from app.services.evening_report_observation_study import compare_observations
from app.services.evening_report_start_study import run_study
from app.services.evening_report_store import EveningReportStore, atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--observation-baseline", action="store_true", help="同时评价晚报名单与5日涨幅基线")
    args = parser.parse_args()
    store = EveningReportStore(args.data_dir)
    result = run_study(store, args.as_of, evaluated_at=cn_now().isoformat())
    if args.observation_baseline:
        result["observation_baseline"] = compare_observations(store, args.as_of)
    output = store.root / "start_research.json"
    atomic_json(output, result)
    print(output)


if __name__ == "__main__":
    main()
