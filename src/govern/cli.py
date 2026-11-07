"""Commands for the governance workflow (GitHub Actions), run as: PYTHONPATH=src python -m govern.cli <command>.

  matrix   --config F                    the collector job list, one entry per workspace (JSON, for strategy.matrix)
  output   --config F                    the output settings, as key=value lines (for $GITHUB_OUTPUT)
  collect  --config F --env E --out F    one workspace's snapshot (JSON). Signs in to estate.<E>.host with the
                                         Databricks SDK's default authentication (here: ARM_CLIENT_ID, ARM_CLIENT_SECRET,
                                         ARM_TENANT_ID, the drift environment's service principal). Read-only.
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from govern import collect
from govern import config as cfgmod


def main(argv=None):
    p = argparse.ArgumentParser(prog="govern.cli")
    p.add_argument("command", choices=("matrix", "output", "collect"))
    p.add_argument("--config", required=True)
    p.add_argument("--env")
    p.add_argument("--out")
    a = p.parse_args(argv)
    cfg = cfgmod.load(a.config)

    if a.command == "matrix":
        print(json.dumps(cfgmod.matrix(cfg), separators=(",", ":")))
    elif a.command == "output":
        for k, v in cfg["output"].items():
            print(f"{k}={v}")
    else:
        if a.env not in cfg["estate"]:
            sys.exit(f"unknown environment {a.env!r}: not in the estate of {a.config}")
        from databricks.sdk import WorkspaceClient
        w = WorkspaceClient(host=cfg["estate"][a.env]["host"])
        snap = collect.snapshot(lambda method, path, query: w.api_client.do(method, path, query=query), cfg, a.env)
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(snap, indent=2, default=str), encoding="utf-8")
        counts = Counter(o["object_type"] for o in snap["observations"])
        print(f"### {a.env}: {len(snap['observations'])} objects observed\n")
        print(" · ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "nothing")
        for e in snap["errors"]:
            print(f"\n❌ {e['domain']} could not be read: {e['error']}")


if __name__ == "__main__":
    main()
