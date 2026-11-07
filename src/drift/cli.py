"""Commands for the drift workflow (GitHub Actions), run as: PYTHONPATH=src python -m drift.cli <command>.

  matrix  --config F                     the job list, one entry per stack (JSON, for strategy.matrix)
  infra   --config F                     the infra and output settings, as key=value lines (for $GITHUB_OUTPUT)
  report  --config F --key K --exit-code N [--plan plan.json] --out F
                                         one stack's findings (JSON); prints a Markdown summary for the run page
"""
import argparse
import json
import os
import sys
from pathlib import Path

from drift import config as cfgmod
from drift import plan as planmod


def _summary(rep):
    icon = {"clean": "✅", "drift": "🔴", "not_applied": "🟡", "error": "❌"}[rep["status"]]
    lines = [f"### {icon} {rep['environment']} / {rep['stack']}: {rep['status']} (exit code {rep['exit_code']})", ""]
    if rep["findings"]:
        lines += ["| Resource | What happened | Attributes | Remediation |", "| --- | --- | --- | --- |"]
        lines += [f"| `{f['address']}` | {f['category']} ({f['planned_action']}) | {', '.join(f['attributes']) or '-'} "
                  f"| {f['remediation']} |" for f in rep["findings"]]
    elif rep["status"] == "error":
        lines.append("The plan failed: see the Plan step's log.")
    else:
        lines.append("Code and reality agree.")
    return "\n".join(lines) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(prog="drift.cli")
    p.add_argument("command", choices=("matrix", "infra", "report"))
    p.add_argument("--config", required=True)
    p.add_argument("--key")
    p.add_argument("--exit-code", type=int)
    p.add_argument("--plan")
    p.add_argument("--out")
    a = p.parse_args(argv)
    cfg = cfgmod.load(a.config)

    if a.command == "matrix":
        print(json.dumps(cfgmod.matrix(cfg), separators=(",", ":")))
    elif a.command == "infra":
        for k, v in {**cfg["infra"], **{f"output_{k}": v for k, v in cfg["output"].items()}}.items():
            print(f"{k}={v}")
    else:
        stack = next((s for s in cfgmod.stacks(cfg) if s.key == a.key), None)
        if stack is None:
            sys.exit(f"unknown stack {a.key!r}: not in {a.config}")
        plan = json.loads(Path(a.plan).read_text(encoding="utf-8")) if a.plan and Path(a.plan).is_file() else None
        run = {"github_run_id": os.environ.get("GITHUB_RUN_ID", "local"),
               "infra_commit": os.environ.get("INFRA_SHA", cfg["infra"]["ref"])}
        rep = planmod.report(stack, a.exit_code, plan, run)
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(rep, indent=2), encoding="utf-8")
        print(_summary(rep), end="")


if __name__ == "__main__":
    main()
