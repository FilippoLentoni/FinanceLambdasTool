"""Plan tools (tasks 7.1-7.6) against the mock platform plan API."""

from __future__ import annotations

import copy

import pytest

from finplan_tools.backends.platform import PlatformClient
from finplan_tools.core.contracts import contract_version
from finplan_tools.core.idempotency import DERIVED_KEY_PATTERN
from finplan_tools.core.registry import CATALOG, inventory_problems, registered_tools
from finplan_tools.core.transport import CallMeta
from finplan_tools.tools import load_all
from finplan_tools_testing.mock_platform import MockPlatform
from finplan_tools_testing.scenarios import SCENARIOS

WRITES = ("create_plan_version", "validate_plan_version", "publish_plan_version")


def is_error(resp, code=None):
    return {"code", "message", "retryable", "correlation_id"} <= set(resp) and (code is None or resp["code"] == code)


@pytest.fixture
def world(offline):
    pf = offline.platform.add_portfolio()
    pl, pv = offline.platform.add_plan(pf)
    return offline, {"portfolio_id": pf, "plan_id": pl, "plan_version_id": pv}


def override(ids, revision=1, key="ovr-key-0001", **content):
    c = MockPlatform.default_content()
    c.update(content)
    return {"plan_id": ids["plan_id"], "parent_plan_version_id": ids["plan_version_id"], "expected_revision": revision, "idempotency_key": key, "domain": "finance", "domain_schema_version": "1.0", "content": c, "synthetic": True}


def changed_content(cash=0.3, spy=0.7):
    return {"allocation": {"weights": [{"instrument_id": "SPY", "weight": spy}], "cash_weight": cash}}


def test_all_catalog_tools_registered_and_inventory_clean():
    load_all()
    assert sorted(t.name for t in registered_tools()) == sorted(CATALOG)
    assert inventory_problems(CATALOG) == []
    assert inventory_problems(["execute_plan"]) and inventory_problems(["place_order"])  # PLN-08 negative


# =================================================================== reads
def test_pln01_get_plan_version_lineage_status_checksum(world, invoke):
    o, ids = world
    resp = invoke(o, "get_plan_version", {"plan_version_id": ids["plan_version_id"]})
    v = resp["plan_version"]
    assert v["plan_id"] == ids["plan_id"] and v["parent_plan_version_id"] is None and v["status"] == "validated"
    for k in ("input_snapshot_id", "configuration_id", "origin", "checksum"):
        assert v[k]
    assert resp["content_ref"]["checksum"].startswith("sha256:") and resp["content_summary"]["top_weights"]
    assert resp["truncated"] is False


def test_pln01_agent_and_website_path_agree(world, invoke):
    o, ids = world
    tool = invoke(o, "get_plan_version", {"plan_version_id": ids["plan_version_id"]})
    direct = PlatformClient(o.platform).get_plan_version(ids["plan_version_id"], CallMeta("website-read-0001", contract_version()))
    for k in ("plan_version_id", "checksum", "content_ref"):
        assert tool["plan_version"][k] == direct["plan_version"][k]


def test_pln02_unknown_version_not_found(world, invoke):
    o, _ = world
    assert is_error(invoke(o, "get_plan_version", {"plan_version_id": "pv_01KM9999999999999999999999"}), "NOT_FOUND")


def test_pln02_get_plan_head_and_publication(world, invoke):
    o, ids = world
    resp = invoke(o, "get_plan", {"plan_id": ids["plan_id"]})
    assert resp["plan"]["head"] == {"current_version_id": ids["plan_version_id"], "revision": 1} and resp["current_publication"] is None
    invoke(o, "publish_plan_version", {"plan_id": ids["plan_id"], "plan_version_id": ids["plan_version_id"], "expected_revision": 1, "idempotency_key": "pub-key-0001"})
    resp = invoke(o, "get_plan", {"plan_id": ids["plan_id"]})
    assert resp["current_publication"]["plan_version_id"] == ids["plan_version_id"] and resp["plan"]["head"]["revision"] == 2


def test_pln02_head_never_cached(world, invoke):
    o, ids = world
    invoke(o, "get_plan", {"plan_id": ids["plan_id"]})
    invoke(o, "create_override_version", override(ids, **changed_content()))
    assert invoke(o, "get_plan", {"plan_id": ids["plan_id"]})["plan"]["head"]["revision"] == 2


@pytest.mark.parametrize("tool,op", [("get_plan", "get_plan"), ("list_plan_versions", "list_plan_versions")])
def test_read_routes_absent_in_older_platform_release(world, invoke, tool, op):
    o, ids = world
    o.platform.missing_routes.add(op)
    resp = invoke(o, tool, {"plan_id": ids["plan_id"]})
    assert is_error(resp, "DEPENDENCY_UNAVAILABLE") and resp["details"]["reason"] == "route_not_deployed"


def _many_versions(o, ids, n):
    parent = ids["plan_version_id"]
    for i in range(n):
        o.clock.advance(60)
        c = copy.deepcopy(MockPlatform.default_content())
        c["allocation"]["cash_weight"] = round(0.4 - i * 0.001, 6)
        o.platform._new_version(ids["plan_id"], parent, c, origin="manual_override", status="pending_validation")  # noqa: SLF001
    return sorted((pv for pv, v in o.platform.versions.items() if v["plan_id"] == ids["plan_id"]), key=lambda pv: o.platform.versions[pv]["created_at"] + pv, reverse=True)


def _walk(o, invoke, args):
    seen, token, pages = [], None, 0
    while True:
        resp = invoke(o, "list_plan_versions", dict(args, **({"next_token": token} if token else {})))
        assert not is_error(resp), resp
        seen += [v["plan_version_id"] for v in resp["versions"]]
        pages += 1
        token = resp["next_token"]
        if token is None:
            return seen, pages
        assert pages < 100


def test_pln03_pagination_no_duplicates(world, invoke):
    o, ids = world
    expected = _many_versions(o, ids, 24)
    seen, pages = _walk(o, invoke, {"plan_id": ids["plan_id"], "page_size": 10})
    assert seen == expected and len(set(seen)) == 25 and pages == 3


def test_pln03_byte_limit_cut_resumes_without_gaps(offline_factory, invoke):
    o = offline_factory("beta", tool_limits={"response_max_bytes": 2048})
    pf = o.platform.add_portfolio()
    pl, pv = o.platform.add_plan(pf)
    expected = _many_versions(o, {"plan_id": pl, "plan_version_id": pv}, 19)
    seen, pages = _walk(o, invoke, {"plan_id": pl, "page_size": 8})
    assert seen == expected and pages > 3


def test_pln03_token_of_other_tool_or_env_rejected(world, invoke, offline_factory):
    o, ids = world
    _many_versions(o, ids, 5)
    token = invoke(o, "list_plan_versions", {"plan_id": ids["plan_id"], "page_size": 2})["next_token"]
    gamma = offline_factory("gamma")
    assert is_error(invoke(gamma, "list_plan_versions", {"plan_id": ids["plan_id"], "next_token": token}), "VALIDATION_FAILED")
    sid = o.platform.add_snapshot()
    assert is_error(invoke(o, "query_market_data", {"input_snapshot_id": sid, "next_token": token}), "VALIDATION_FAILED")


def test_list_page_size_capped_by_configuration(world, invoke):
    o, ids = world
    invoke(o, "list_plan_versions", {"plan_id": ids["plan_id"], "page_size": 100})
    assert o.platform.calls[-1].query["page_size"] == 100
    invoke(o, "list_plan_versions", {"plan_id": ids["plan_id"]})
    assert o.platform.calls[-1].query["page_size"] == 20


# =================================================================== create_override_version
def test_pln04_child_created_parent_unchanged(world, invoke):
    o, ids = world
    parent_before = copy.deepcopy(o.platform.versions[ids["plan_version_id"]])
    content_before = copy.deepcopy(o.platform.contents[ids["plan_version_id"]])
    resp = invoke(o, "create_override_version", override(ids, **changed_content()))
    assert not is_error(resp), resp
    assert resp["parent_plan_version_id"] == ids["plan_version_id"] and resp["origin"] == "manual_override"
    assert resp["plan_version_id"] in o.platform.versions and resp["plan_version_id"] != ids["plan_version_id"]
    assert resp["no_effect"] is False and resp["revision"] == 2
    assert o.platform.versions[ids["plan_version_id"]] == parent_before and o.platform.contents[ids["plan_version_id"]] == content_before
    body = o.platform.calls[-1].body
    assert DERIVED_KEY_PATTERN.match(body["idempotency_key"]) and body["expected_revision"] == 1
    assert o.platform.ops() == ["get_plan", "get_portfolio", "create_plan_version"]


def test_pln04_stale_revision_conflict(world, invoke):
    o, ids = world
    s = SCENARIOS["conflict"](o)
    resp = invoke(o, s.tool, s.requests[0])
    assert is_error(resp, "CONFLICT") and resp["retryable"] is False and "get_plan" in resp["details"]["hint"]


def test_pln04_concurrent_second_override_conflicts(world, invoke):
    o, ids = world
    assert not is_error(invoke(o, "create_override_version", override(ids, key="ovr-key-a001", **changed_content())))
    resp = invoke(o, "create_override_version", override(ids, key="ovr-key-b001", **changed_content(0.2, 0.8)))
    assert is_error(resp, "CONFLICT")


def test_pln04_no_effect(offline, invoke):
    s = SCENARIOS["override_no_effect"](offline)
    resp = invoke(offline, s.tool, s.requests[0])
    assert resp["no_effect"] is True and resp["checksum"] == offline.platform.versions[s.ids["plan_version_id"]]["checksum"]


def test_pln04_duplicate_override_one_child(world, invoke):
    o, ids = world
    a = invoke(o, "create_override_version", override(ids, **changed_content()))
    b = invoke(o, "create_override_version", override(ids, **changed_content()))
    assert a["plan_version_id"] == b["plan_version_id"] and len(o.platform.versions) == 2


@pytest.mark.parametrize("field", ["plan_version_id", "target_plan_version_id"])
def test_pln05_in_place_edit_is_immutable_record(world, invoke, field):
    o, ids = world
    r = override(ids)
    r[field] = ids["plan_version_id"]
    resp = invoke(o, "create_override_version", r)
    assert is_error(resp, "IMMUTABLE_RECORD") and "child" in resp["message"] and o.platform.count() == 0


def test_pln05_platform_immutable_record_passed_through(world, invoke):
    o, ids = world
    o.platform.fail_next("create_plan_version", "IMMUTABLE_RECORD")
    assert is_error(invoke(o, "create_override_version", override(ids, **changed_content())), "IMMUTABLE_RECORD")


def test_pln09_non_synthetic_portfolio_no_write(offline, invoke):
    s = SCENARIOS["non_synthetic_portfolio"](offline)
    resp = invoke(offline, s.tool, s.requests[0])
    assert is_error(resp, "OPERATION_NOT_PERMITTED") and not any(offline.platform.count(op) for op in WRITES)


def test_pln09_guard_covers_validate_and_publish(offline, invoke):
    pf = offline.platform.add_portfolio(synthetic=False)
    pl, pv = offline.platform.add_plan(pf)
    assert is_error(invoke(offline, "validate_plan_version", {"plan_version_id": pv, "idempotency_key": "val-key-0001"}), "OPERATION_NOT_PERMITTED")
    assert is_error(invoke(offline, "publish_plan_version", {"plan_id": pl, "plan_version_id": pv, "expected_revision": 1, "idempotency_key": "pub-key-0001"}), "OPERATION_NOT_PERMITTED")
    assert not any(offline.platform.count(op) for op in WRITES)


# =================================================================== validate / publish
def test_pln06_weights_sum_107_invalid_with_finding(world, invoke):
    o, ids = world
    child = invoke(o, "create_override_version", override(ids, **changed_content(cash=0.4, spy=0.67)))
    resp = invoke(o, "validate_plan_version", {"plan_version_id": child["plan_version_id"], "idempotency_key": "val-key-0001"})
    assert resp["status"] == "invalid" and resp["findings"] == [{"code": "VALIDATION_FAILED", "message": "weights plus cash do not sum to 1", "pointer": "/allocation"}]


def test_validate_valid_child(world, invoke):
    o, ids = world
    child = invoke(o, "create_override_version", override(ids, **changed_content()))
    resp = invoke(o, "validate_plan_version", {"plan_version_id": child["plan_version_id"], "idempotency_key": "val-key-0002"})
    assert resp["status"] == "validated" and resp["findings"] == []


def test_pln07_publish_validated_version(world, invoke):
    o, ids = world
    resp = invoke(o, "publish_plan_version", {"plan_id": ids["plan_id"], "plan_version_id": ids["plan_version_id"], "expected_revision": 1, "idempotency_key": "pub-key-0001"})
    assert resp["publication_id"] in o.platform.publications and resp["plan_version_id"] == ids["plan_version_id"]
    assert resp["plan_version_checksum"] == o.platform.versions[ids["plan_version_id"]]["checksum"]


def test_pln07_publish_invalid_version_precondition_failed(world, invoke):
    o, ids = world
    child = invoke(o, "create_override_version", override(ids, **changed_content(cash=0.4, spy=0.67)))
    invoke(o, "validate_plan_version", {"plan_version_id": child["plan_version_id"], "idempotency_key": "val-key-0001"})
    resp = invoke(o, "publish_plan_version", {"plan_id": ids["plan_id"], "plan_version_id": child["plan_version_id"], "expected_revision": 2, "idempotency_key": "pub-key-0002"})
    assert is_error(resp, "PRECONDITION_FAILED") and not o.platform.publications


def test_pln07_duplicate_publish_one_publication(world, invoke):
    o, ids = world
    r = {"plan_id": ids["plan_id"], "plan_version_id": ids["plan_version_id"], "expected_revision": 1, "idempotency_key": "pub-key-0003"}
    a, b = invoke(o, "publish_plan_version", r), invoke(o, "publish_plan_version", dict(r))
    assert a["publication_id"] == b["publication_id"] and len(o.platform.publications) == 1


@pytest.mark.parametrize("extra", [{"execute": True}, {"mode": "live"}])
def test_pln08_execution_fields_rejected_without_call(world, invoke, extra):
    o, ids = world
    r = {"plan_id": ids["plan_id"], "plan_version_id": ids["plan_version_id"], "expected_revision": 1, "idempotency_key": "pub-key-0004", **extra}
    resp = invoke(o, "publish_plan_version", r)
    assert is_error(resp, "VALIDATION_FAILED") and o.platform.count() == 0


def test_pln_prod_direct_writes_forbidden(offline_factory, invoke):
    o = offline_factory("prod")
    pf = o.platform.add_portfolio()
    pl, pv = o.platform.add_plan(pf)
    assert is_error(invoke(o, "publish_plan_version", {"plan_id": pl, "plan_version_id": pv, "expected_revision": 1, "idempotency_key": "pub-key-0005"}), "FORBIDDEN")
    assert not is_error(invoke(o, "get_plan_version", {"plan_version_id": pv}))  # read-only smoke allowed
    assert not any(o.platform.count(op) for op in WRITES)
