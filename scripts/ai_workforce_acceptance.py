import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.workforce_acceptance import run_acceptance


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the five-role AI workforce acceptance twice on one QMT snapshot")
    parser.add_argument("--symbol", default="600498.SH")
    parser.add_argument("--output", default="outputs/acceptance/600498")
    args = parser.parse_args()
    result = run_acceptance(args.symbol, args.output)
    print(json.dumps({"fact_bundle": result["fact_bundle"], "comparison": result["comparison"]},
                     ensure_ascii=False, indent=2))
