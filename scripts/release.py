#!/usr/bin/env python3
"""Release identity, artifact digest, tool catalog, release manifest and published references
(tasks 8.6, 9.1, 9.4; REL-01..REL-04, REL-07; contracts D4, D6, D10).

Build stage (once per commit):

* :func:`mint_release_id` - ``rel_`` + ULID, shared by every environment of the build;
* :func:`assembly_digest` - ``sha256:`` over the sorted (path, SHA-256) list of every assembly file, so
  the beta, gamma and prod manifests of one release record the same ``artifact_digest`` (REL-04);
* :class:`ReleaseInfo` (``release-info.json`` in BuildOutput): release ID, source commit, artifact
  digest, pinned contract version and wheel digest, served majors, region;
* :func:`store_build_output` / :func:`fetch_build_output` - the release ledger in the pipeline store
  (``releases/<release_id>/``), used by rollback (no rebuild; digest re-verified; REL-07).

Each deploy (``PublishRelease`` action, :func:`publish_release`), as the ``pipeline`` writer bound to the
environment (:func:`finplan_contracts.ssm.check_write` on every write, so nothing outside
``/finplan/<env>/financelambdastool/`` can be written):

1. reads the outputs of ``finplan-<env>-financelambdastool-tools``;
2. publishes one alias-qualified reference per tool, ``lambda/<tool-kebab>-arn`` (REL-01), and one per
   role class, ``lambda/role-<class>-arn`` (the platform's resource policy names them);
3. publishes ``contract/tool-catalog`` (REL-02), validated against the pinned ``tool-catalog`` schema
   (every schema ``$id`` resolves in the pinned package, every Lambda reference parameter is this
   environment's);
4. publishes ``config/budget-enforced-role-names`` = the ``submitter`` role name only, for the
   FinancialPlanning budget deny action at 100% of USD 50 (task 8.6; this repo creates no budget);
5. validates and writes ``release/manifest`` (contract ``release-manifest``; ``outputs`` lists every
   reference above; prod adds ``approved_by``/``approved_at``; a rollback records
   ``rolled_back_from``) and ``release/current-release-id``, and copies the manifest into the ledger.

All AWS access goes through injected clients; the unit suite uses moto and fakes.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import zipfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from finplan_contracts import ssm as contract_ssm
from finplan_contracts.validate import validate
from ulid import ULID

from finplan_tools.core.registry import CATALOG, ROLE_CLASSES
from infra.stacks import naming as n

__all__ = [
    "ManifestError",
    "ReleaseInfo",
    "approval_record",
    "assembly_digest",
    "build_manifest",
    "build_tool_catalog",
    "contract_pin",
    "enforced_role_names",
    "fetch_build_output",
    "mint_release_id",
    "planned_parameters",
    "publish_release",
    "stack_outputs",
    "store_build_output",
    "tool_descriptions",
]

REPO = n.REPO
RELEASES_PREFIX = "releases/"
#: SSM standard-tier limit; a larger value (a long tool catalog) needs the Advanced tier (billed).
STANDARD_TIER_BYTES = 4096
APPROVAL_ACTION = "ApproveProd"


class ManifestError(ValueError):
    pass


# ===================================================================== build stage
def mint_release_id(now: datetime | None = None) -> str:
    return "rel_" + str(ULID.from_datetime(now) if now else ULID())


def assembly_digest(assembly: str | Path) -> str:
    root = Path(assembly)
    if not (root / "manifest.json").is_file():
        raise ManifestError(f"{root} is not a cloud assembly (manifest.json missing)")
    h = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        h.update(rel.encode("utf-8") + b"\0" + hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii") + b"\n")
    return "sha256:" + h.hexdigest()


def contract_pin(root: str | Path) -> tuple[str, str]:
    pin = json.loads((Path(root) / "contracts-pin.json").read_text(encoding="utf-8"))
    return str(pin["version"]), "sha256:" + str(pin["sha256"])


@dataclass
class ReleaseInfo:
    release_id: str
    source_commit: str
    artifact_digest: str
    contract_version: str
    contract_digest: str
    served_contract_majors: list[int]
    region: str
    built_at: str
    rollback: bool = False
    synthetic: bool = True

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"

    @classmethod
    def load(cls, path: str | Path) -> ReleaseInfo:
        return cls(**json.loads(Path(path).read_text(encoding="utf-8")))


# ===================================================================== references
def _name(env: str, category: str, name: str) -> str:
    return contract_ssm.build(env, REPO, category, name)


def stack_outputs(cfn: Any, env: str) -> dict[str, str]:
    """Outputs of ``finplan-<env>-financelambdastool-tools`` (``{OutputKey: OutputValue}``)."""
    name = n.stack_name(env)
    try:
        stacks = cfn.describe_stacks(StackName=name)["Stacks"]
    except Exception as exc:  # noqa: BLE001
        raise ManifestError(f"stack {name} is not deployed ({type(exc).__name__})") from None
    return {str(o["OutputKey"]): str(o["OutputValue"]) for o in stacks[0].get("Outputs") or []}


def tool_descriptions() -> dict[str, str]:
    """Descriptions of the registered tools (``finplan_tools.tools``)."""
    from finplan_tools.core.registry import registered_tools
    from finplan_tools.tools import load_all

    load_all()
    return {spec.name: spec.description for spec in registered_tools()}


def build_tool_catalog(env: str, release_id: str, contract_version: str, descriptions: Mapping[str, str], *, synthetic: bool = True) -> dict[str, Any]:
    """The tool catalog of ``env`` (REL-02), validated against the pinned ``tool-catalog`` schema."""
    from finplan_tools.core.contracts import schema_id

    missing = sorted(set(CATALOG) - set(descriptions))
    if missing:
        raise ManifestError(f"tools without an implementation in this release: {missing}; every catalog tool must be registered before a release is published")
    tools = []
    for name, entry in sorted(CATALOG.items()):
        tools.append(
            {
                "name": name,
                "description": descriptions[name],
                "input_schema_id": schema_id(entry.input_schema),
                "output_schema_id": schema_id(entry.output_schema),
                "state_changing": entry.state_changing,
                "role_class": entry.role_class,
                "lambda_ref_parameter": _name(env, "lambda", entry.lambda_ref_name),
            }
        )
    doc = {"environment": env, "release_id": release_id, "contract_version": contract_version, "tools": tools, "synthetic": synthetic}
    res = validate(doc, "tool-catalog")
    problems = [f"{i.pointer or '/'}: {i.message}" for i in res.issues]
    for t in tools:
        if not t["lambda_ref_parameter"].startswith(f"/finplan/{env}/{REPO}/lambda/"):
            problems.append(f"{t['name']}: Lambda reference parameter is not this environment's")
    if problems:
        raise ManifestError("tool catalog is invalid: " + "; ".join(dict.fromkeys(problems)))
    return doc


def enforced_role_names(env: str) -> list[str]:
    """Role names the platform's budget action denies at 100% (task 8.6: the ``submitter`` class only;
    it is the only role that can start spend: ingestion and job submission)."""
    return [n.role_class_role_name(env, "submitter")]


def planned_parameters(env: str, outputs: Mapping[str, str], catalog: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    """``{manifest output key: (SSM name, value)}`` for every reference this deploy publishes."""
    from infra.stacks.tools import output_key, role_output_key

    plan: dict[str, tuple[str, str]] = {}
    for tool, entry in sorted(CATALOG.items()):
        value = outputs.get(output_key(tool))
        if not value:
            raise ManifestError(f"the {env} deploy did not produce the output {output_key(tool)}")
        if not value.endswith(f":{n.TOOL_ALIAS}"):
            raise ManifestError(f"{tool}: the published reference must be alias-qualified (:{n.TOOL_ALIAS})")
        key = entry.lambda_ref_name
        plan[key] = (_name(env, "lambda", key), value)
    for cls in ROLE_CLASSES:
        value = outputs.get(role_output_key(cls))
        if not value:
            raise ManifestError(f"the {env} deploy did not produce the output {role_output_key(cls)}")
        plan[f"role-{cls}-arn"] = (_name(env, "lambda", f"role-{cls}-arn"), value)
    plan["tool-catalog"] = (_name(env, "contract", "tool-catalog"), json.dumps(catalog, sort_keys=True, separators=(",", ":")))
    plan["budget-enforced-role-names"] = (_name(env, "config", "budget-enforced-role-names"), ",".join(enforced_role_names(env)))
    return plan


# ===================================================================== manifest
def build_manifest(info: ReleaseInfo, env: str, *, deployed_at: str, previous_release_id: str | None, outputs: Mapping[str, str], approval: Mapping[str, str] | None = None, rolled_back_from: str | None = None) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "repo": REPO,
        "environment": env,
        "region": info.region,
        "release_id": info.release_id,
        "source_commit": info.source_commit,
        "artifact_digest": info.artifact_digest,
        "contract_version": info.contract_version,
        "contract_digest": info.contract_digest,
        "deployed_at": deployed_at,
        "previous_release_id": previous_release_id,
        "outputs": dict(outputs),
        "served_contract_majors": sorted(set(info.served_contract_majors)),
        "synthetic": info.synthetic,
    }
    if approval:
        doc["approved_by"] = approval["approved_by"]
        doc["approved_at"] = approval["approved_at"]
    if rolled_back_from is not None:
        doc["rolled_back_from"] = rolled_back_from
    res = validate(doc, "release-manifest")
    problems = [f"{i.pointer or '/'}: {i.message}" for i in res.issues]
    problems += contract_ssm.validate_value(_name(env, "release", "manifest"), json.dumps(doc))
    if problems:
        raise ManifestError("release manifest is invalid: " + "; ".join(dict.fromkeys(problems)))
    return doc


def _ts(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def approval_record(codepipeline: Any, pipeline_name: str, execution_id: str, action_name: str = APPROVAL_ACTION) -> dict[str, str]:
    token: str | None = None
    while True:
        kw: dict[str, Any] = {"pipelineName": pipeline_name, "filter": {"pipelineExecutionId": execution_id}}
        if token:
            kw["nextToken"] = token
        resp = codepipeline.list_action_executions(**kw)
        for d in resp.get("actionExecutionDetails") or []:
            if d.get("actionName") == action_name and d.get("status") == "Succeeded":
                who = d.get("updatedBy") or ((d.get("output") or {}).get("executionResult") or {}).get("externalExecutionSummary")
                when = d.get("lastUpdateTime")
                if who and when:
                    return {"approved_by": str(who), "approved_at": _ts(when)}
        token = resp.get("nextToken")
        if not token:
            break
    raise ManifestError(f"no succeeded manual approval '{action_name}' in pipeline execution {execution_id}; prod manifests require approved_by and approved_at")


# ===================================================================== publish
def _get(ssm: Any, name: str) -> str | None:
    try:
        return ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None
        if code == "ParameterNotFound":
            return None
        raise


def _put(ssm: Any, env: str, name: str, value: str) -> None:
    decision = contract_ssm.check_write(name, contract_ssm.Writer(REPO, "pipeline", env))
    if not decision:
        raise ManifestError("; ".join(decision.reasons))
    problems = contract_ssm.validate_value(name, value)
    if problems:
        raise ManifestError(f"{name}: " + "; ".join(problems))
    tier = "Advanced" if len(value.encode("utf-8")) > STANDARD_TIER_BYTES else "Standard"
    ssm.put_parameter(Name=name, Value=value, Type="String", Overwrite=True, Tier=tier)


def publish_release(
    info: ReleaseInfo,
    env: str,
    *,
    ssm: Any,
    cfn: Any,
    s3: Any | None = None,
    store_bucket: str | None = None,
    now: datetime | None = None,
    approval: Mapping[str, str] | None = None,
    descriptions: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Publish the references, the tool catalog, the manifest and the current-release pointer."""
    if env == "prod" and not approval:
        raise ManifestError("prod manifests require the approval record (approved_by, approved_at)")
    outputs = stack_outputs(cfn, env)
    catalog = build_tool_catalog(env, info.release_id, info.contract_version, descriptions if descriptions is not None else tool_descriptions(), synthetic=info.synthetic)
    plan = planned_parameters(env, outputs, catalog)
    for _key, (name, value) in sorted(plan.items()):
        _put(ssm, env, name, value)
    pointer = _name(env, "release", "current-release-id")
    manifest_name = _name(env, "release", "manifest")
    current = _get(ssm, pointer)
    previous: str | None = current
    rolled_back_from: str | None = None
    if current == info.release_id:  # republish of the same release (configuration-only redeploy)
        existing = json.loads(_get(ssm, manifest_name) or "{}")
        previous = existing.get("previous_release_id")
        rolled_back_from = existing.get("rolled_back_from")
    elif info.rollback:
        rolled_back_from = current
    manifest = build_manifest(info, env, deployed_at=_ts(now or datetime.now(UTC)), previous_release_id=previous, approval=approval, rolled_back_from=rolled_back_from, outputs={k: name for k, (name, _v) in plan.items()})
    body = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    _put(ssm, env, manifest_name, body)
    _put(ssm, env, pointer, info.release_id)
    if s3 is not None and store_bucket:
        s3.put_object(Bucket=store_bucket, Key=f"{RELEASES_PREFIX}{info.release_id}/manifests/{env}.json", Body=body.encode("utf-8"), ContentType="application/json")
    return manifest


# ===================================================================== release ledger
def zip_dir(directory: Path) -> bytes:
    from scripts.publish_assets import deterministic_zip

    return deterministic_zip(directory)


def store_build_output(s3: Any, bucket: str, info: ReleaseInfo, out_dir: str | Path) -> str:
    key = f"{RELEASES_PREFIX}{info.release_id}/build-output.zip"
    s3.put_object(Bucket=bucket, Key=key, Body=zip_dir(Path(out_dir)), IfNoneMatch="*")
    s3.put_object(Bucket=bucket, Key=f"{RELEASES_PREFIX}{info.release_id}/release-info.json", Body=info.to_json().encode("utf-8"), IfNoneMatch="*")
    return key


def fetch_build_output(s3: Any, bucket: str, release_id: str, out_dir: str | Path) -> ReleaseInfo:
    """Re-emit a recorded release (REL-07): no rebuild, the digest is re-verified."""
    if not release_id.startswith("rel_"):
        raise ManifestError(f"{release_id!r} is not a release_id")
    try:
        body = s3.get_object(Bucket=bucket, Key=f"{RELEASES_PREFIX}{release_id}/build-output.zip")["Body"].read()
    except Exception as exc:
        raise ManifestError(f"release {release_id} has no stored build output in the pipeline store") from exc
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        for member in zf.namelist():
            if member.startswith("/") or ".." in Path(member).parts:
                raise ManifestError(f"unsafe path in stored build output: {member}")
        zf.extractall(out)
    info = ReleaseInfo.load(out / "release-info.json")
    if info.release_id != release_id:
        raise ManifestError("stored build output belongs to another release")
    if assembly_digest(out / "cdk.out") != info.artifact_digest:
        raise ManifestError("stored build output digest does not match its release record")
    info.rollback = True
    (out / "release-info.json").write_text(info.to_json(), encoding="utf-8")
    return info
