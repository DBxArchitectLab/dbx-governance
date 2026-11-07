"""Layer 4, act: the results as something an owner can work from. One Markdown report per workspace (the body of its
governance issue and the run page), and a summary the workflow reads to open, update or close the issues.

Reports carry object names, principals and fixes; they never carry data.
"""
_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_ICON = {"FAIL": "🔴", "ERROR": "❌", "EXCEPTION": "🟡"}


def _cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def markdown(score, results):
    env = score["environment"]
    head = "✅ compliant" if score["compliant"] else f"🔴 {score['failed']} failed, {score['errors']} not read"
    lines = [f"### {env}: {head} · score {score['score']}% ({score['passed']} passed, {score['exceptions']} "
             f"exceptions, of {score['checks']} checks)", ""]
    open_ = sorted([r for r in results if r["status"] in ("FAIL", "ERROR")],
                   key=lambda r: (_RANK.get(r["severity"], 9), r["rule_id"], r["object"]))
    if open_:
        lines += ["| | Severity | Rule | Evidence | Fix |", "| --- | --- | --- | --- | --- |"]
        lines += [f"| {_ICON[r['status']]} | {r['severity']} | {r['rule_id']} {_cell(r['title'])} | "
                  f"{_cell(r['evidence'])} | {_cell(r['remediation'] + (' ' + r['exception'] if r['exception'] else ''))} |"
                  for r in open_]
        sql = [r["remediation_sql"] for r in open_ if r["remediation_sql"]]
        if sql:
            lines += ["", "Unity Catalog fixes, for the owner to review and run in the workspace:", "", "```sql",
                      *("\n".join(sql).splitlines()), "```"]
    excepted = [r for r in results if r["status"] == "EXCEPTION"]
    if excepted:
        lines += ["", f"Exceptions ({len(excepted)}): " + "; ".join(
            sorted({f"{r['rule_id']} {r['principal'] or r['object']}: {r['exception']}" for r in excepted}))]
    if not open_:
        lines += ["", "Every check passed or is covered by an exception."]
    return "\n".join(lines) + "\n"


def summary(scores):
    """For the workflow: per environment, compliant or not, the worst failing severity, and the owner."""
    return {"environments": [{k: s[k] for k in ("environment", "owner", "compliant", "failed", "errors", "score",
                                                 "worst")} for s in scores]}


def worst(results):
    failing = [r["severity"] for r in results if r["status"] in ("FAIL", "ERROR")]
    return min(failing, key=lambda s: _RANK.get(s, 9)) if failing else ""
