"""config/drift.yml: load it, check it, and turn it into the drift workflow's job list."""
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

SEVERITIES = ("low", "medium", "high")
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Stack:
    environment: str
    stack: str
    path: str          # in the infra repository, e.g. live/prod/workspace-bootstrap
    workspace_id: int
    severity: str

    @property
    def key(self):
        return f"{self.environment}-{self.stack}"


def load(path):
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    validate(cfg)
    return cfg


def validate(cfg):
    """Fail early, with the field that is wrong: a typo here would silently skip a workspace."""
    for key in ("infra", "environments", "pipeline_identities", "audit", "output"):
        if not cfg.get(key):
            raise ConfigError(f"missing section: {key}")
    for key in ("repository", "ref", "live_root", "terraform_version", "terragrunt_version", "state_storage_account",
                "state_resource_group"):
        if not cfg["infra"].get(key):
            raise ConfigError(f"infra.{key} is missing")
    for env, e in cfg["environments"].items():
        if not _NAME.match(env):
            raise ConfigError(f"environment name {env!r}: lowercase letters, digits and hyphens only")
        if not isinstance(e.get("workspace_id"), int):
            raise ConfigError(f"environments.{env}.workspace_id must be the numeric workspace ID")
        if not e.get("stacks") or not all(isinstance(s, str) and _NAME.match(s) for s in e["stacks"]):
            raise ConfigError(f"environments.{env}.stacks: one or more stack folder names")
        if e.get("severity", "medium") not in SEVERITIES:
            raise ConfigError(f"environments.{env}.severity must be one of {', '.join(SEVERITIES)}")
    for p in cfg["pipeline_identities"]:
        if not _GUID.match(str(p.get("application_id", ""))):
            raise ConfigError(f"pipeline_identities: {p.get('name')!r} needs its application ID (a GUID)")
    if not isinstance(cfg["audit"].get("lookback_days"), int) or cfg["audit"]["lookback_days"] < 1:
        raise ConfigError("audit.lookback_days must be a positive number of days")
    if not cfg["audit"].get("watched"):
        raise ConfigError("audit.watched: at least one service and its actions")
    for key in ("catalog", "schema", "volume"):
        if not cfg["output"].get(key):
            raise ConfigError(f"output.{key} is missing")


def stacks(cfg):
    """Every stack to plan, one per environment and stack folder."""
    root = cfg["infra"]["live_root"].rstrip("/")
    return [Stack(env, s, f"{root}/{env}/{s}", e["workspace_id"], e.get("severity", "medium"))
            for env, e in cfg["environments"].items() for s in e["stacks"]]


def matrix(cfg):
    """The GitHub Actions matrix: {"include": [...]}, one entry per stack."""
    return {"include": [{"key": s.key, "environment": s.environment, "stack": s.stack, "path": s.path,
                         "severity": s.severity} for s in stacks(cfg)]}


def pipeline_ids(cfg):
    return sorted(p["application_id"] for p in cfg["pipeline_identities"])


def environment_of(cfg, workspace_id):
    return next((env for env, e in cfg["environments"].items() if e["workspace_id"] == int(workspace_id)), None)
