"""Commands for the governance workflow (GitHub Actions), run as: PYTHONPATH=src python -m govern.cli <command>.

  matrix   --config F                    the collector job list, one entry per workspace (JSON, for strategy.matrix)
  scopes   --config F                    the remediation job list: every workspace, and the account (JSON)
  output   --config F                    the output settings, as key=value lines (for $GITHUB_OUTPUT)
  collect  --config F --env E --out F    one workspace's snapshot (JSON): workspace API + information schema. Signs in
                                         to estate.<E>.host with the Databricks SDK's default authentication (here:
                                         ARM_CLIENT_ID, ARM_CLIENT_SECRET, ARM_TENANT_ID, the drift environment's
                                         service principal). Read-only.
  collect-account --config F --out F     the account snapshot (JSON): account API, signed in to account.host with
                                         DATABRICKS_ACCOUNT_ID and the same credentials. Read-only.
"""
import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

from govern import account, collect
from govern import config as cfgmod


def main(argv=None):
    p = argparse.ArgumentParser(prog="govern.cli")
    p.add_argument("command", choices=("matrix", "scopes", "output", "collect", "collect-account"))
    p.add_argument("--config", required=True)
    p.add_argument("--env")
    p.add_argument("--out")
    a = p.parse_args(argv)
    cfg = cfgmod.load(a.config)

    if a.command == "matrix":
        print(json.dumps(cfgmod.matrix(cfg), separators=(",", ":")))
    elif a.command == "scopes":
        print(json.dumps(cfgmod.scopes(cfg), separators=(",", ":")))
    elif a.command == "output":
        for k, v in cfg["output"].items():
            print(f"{k}={v}")
    elif a.command == "collect-account":
        from databricks.sdk import AccountClient
        account_id = os.environ["DATABRICKS_ACCOUNT_ID"]
        acc = AccountClient(host=cfg["account"]["host"], account_id=account_id)
        snap = account.snapshot(lambda method, path, query=None, body=None:
                                acc.api_client.do(method, path, query=query, body=body), cfg, account_id)
        _write(a.out, snap, "account")
    else:
        if a.env not in cfg["estate"]:
            sys.exit(f"unknown environment {a.env!r}: not in the estate of {a.config}")
        from databricks.sdk import WorkspaceClient
        w = WorkspaceClient(host=cfg["estate"][a.env]["host"])
        snap = collect.snapshot(lambda method, path, query=None, body=None:
                                w.api_client.do(method, path, query=query, body=body), cfg, a.env)
        _write(a.out, snap, a.env)


def _write(out, snap, label):
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(snap, indent=2, default=str), encoding="utf-8")
    print(f"### {label}: {len(snap['observations'])} objects observed\n")
    for source in sorted({o["source"] for o in snap["observations"]}):
        counts = Counter(o["object_type"] for o in snap["observations"] if o["source"] == source)
        print(f"- {source}: " + " · ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    for e in snap["errors"]:
        print(f"\n❌ {e['source']} / {e['domain']} could not be read: {e['error']}")


if __name__ == "__main__":
    main()
