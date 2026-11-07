"""config/drift.yml: the real file is valid, matches the bundle, and scaling out is one entry."""
import copy

import pytest
import yaml
from conftest import ROOT

from drift import config

CFG = config.load(ROOT / "config" / "drift.yml")


def test_the_real_config_plans_dev_and_prod():
    assert [s["key"] for s in config.matrix(CFG)["include"]] == [
        "dev-workspace", "dev-workspace-bootstrap", "prod-workspace", "prod-workspace-bootstrap"]
    prod = config.matrix(CFG)["include"][3]
    assert prod == {"key": "prod-workspace-bootstrap", "environment": "prod", "stack": "workspace-bootstrap",
                    "path": "live/prod/workspace-bootstrap", "severity": "high"}


def test_the_bundle_resources_match_the_output_section():
    res = yaml.safe_load((ROOT / "resources" / "drift.yml").read_text(encoding="utf-8"))["resources"]
    out = CFG["output"]
    assert res["schemas"]["drift"]["catalog_name"] == out["catalog"]
    assert res["schemas"]["drift"]["name"] == out["schema"]
    assert res["volumes"]["plan_findings"]["name"] == out["volume"]


def test_scaling_out_is_one_entry():
    cfg = copy.deepcopy(CFG)
    cfg["environments"]["uat"] = {"workspace_id": 123, "stacks": ["workspace"], "severity": "medium"}
    config.validate(cfg)
    assert config.matrix(cfg)["include"][-1]["path"] == "live/uat/workspace"
    assert config.environment_of(cfg, "123") == "uat"


@pytest.mark.parametrize("mutate, message", [
    (lambda c: c["environments"]["prod"].update(workspace_id="7405604784566671"), "numeric workspace ID"),
    (lambda c: c["environments"]["prod"].update(stacks=[]), "stacks"),
    (lambda c: c["environments"]["prod"].update(severity="urgent"), "severity"),
    (lambda c: c["pipeline_identities"].append({"name": "sp-x", "application_id": "TODO"}), "application ID"),
    (lambda c: c["infra"].pop("ref"), "infra.ref"),
])
def test_mistakes_are_named(mutate, message):
    cfg = copy.deepcopy(CFG)
    mutate(cfg)
    with pytest.raises(config.ConfigError, match=message):
        config.validate(cfg)
