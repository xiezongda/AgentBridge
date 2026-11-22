"""Load and lock a human-editable version 1 project contract."""

import re
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

from .bridge import DEFAULT_OUTPUT, NEON_MODEL, SOL_MODEL, WORKDIR
from .iteration_evidence import required_text
from .iteration_files import (
    FILE_POLICY, IntegrityError, checked_authorized_path, digests, file_sha256, json_text,
    overlap, stream_manifest, tracked_equal, tracked_snapshot,
)
from .protocol import _load_json
from .verifiers import integer, object_fields, string_list, validate_verifiers

ASTRA_MODEL = "gpt-6.2-astra"
PLANNING_ORDERS = ("neon_first", "astra_first")
DEFAULT_AGENTS = {
    "coordinator": NEON_MODEL, "executor": SOL_MODEL, "scientist": ASTRA_MODEL,
    "require_astra": False, "sol_effort": "high", "astra_effort": "high",
    "planning_order": "neon_first",
}
DEFAULT_LIMITS = {
    "max_sol_attempts": 3, "max_model_calls": 12, "max_astra_calls": 4,
    "max_plan_revisions": 2, "max_consecutive_failures": 2, "timeout_s": 240,
    "max_total_seconds": 1200,
}
HARD_LIMITS = {
    "max_sol_attempts": (1, 100), "max_model_calls": (1, 500),
    "max_astra_calls": (1, 100), "max_plan_revisions": (1, 50),
    "max_consecutive_failures": (1, 100), "timeout_s": (1, 7200),
    "max_total_seconds": (1, 86400),
}
RESUME_LIMITS = ("max_sol_attempts", "max_model_calls", "max_astra_calls", "max_total_seconds",
                 "max_consecutive_failures", "max_plan_revisions")
DEFAULT_CONSTRAINTS = {"physical_constraints": [], "units": {}, "assumptions": [],
                       "scope_limitations": []}
TASK_TYPES = ("engineering", "scientific", "numerical", "data_analysis")
PROTECTED = {".git", ".agents", ".codex", ".aws", ".github", "agentbridge", "tests", "runs"}
ACCEPTANCE_NAMES = {"acceptance", "acceptance_tests", "acceptance_tests_dir", "verification"}


def _protected(path, role):
    if not path.is_relative_to(WORKDIR):
        if overlap(path, WORKDIR):
            raise IntegrityError(f"protected {role} directory: {path}")
        return
    parts = tuple(part.casefold() for part in path.relative_to(WORKDIR).parts)
    reserved = PROTECTED - {"runs"} if role == "output" else PROTECTED
    if role == "verification":
        reserved = reserved - {"tests"}
    if (parts[0] in reserved or
            (role != "verification" and (ACCEPTANCE_NAMES.intersection(parts) or parts == ("examples",)))):
        raise IntegrityError(f"protected {role} directory: {path}")


def output_path(path, *, allow_external=False):
    output = checked_authorized_path(path, "directory", missing_ok=True, allow_external=allow_external)
    _protected(output, "output")
    return output


def _verification_manifest(directory):
    entries = stream_manifest(directory)
    for name in entries:
        path = Path(name.lower())
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            raise IntegrityError(f"verification tree contains Python bytecode: {directory / name}")
    return entries


class ProjectSpec:
    """Resolved paths, normalized definitions, and the pre-run verifier manifest."""

    @classmethod
    def from_file(cls, path, *, output_dir=None):
        path = checked_authorized_path(Path(path), "file", allow_external=True)
        source = path.read_bytes()
        data = _load_json(source.decode("utf-8-sig"))
        object_fields(data, cls.FIELDS | {"goal_file"}, "project specification")
        external = cls._external(data)
        checked_authorized_path(path, "file", allow_external=external)
        sources = {str(path): sha256(source).hexdigest()}
        if isinstance(data, dict) and "goal_file" in data:
            if "goal" in data:
                raise ValueError("specify exactly one of goal or goal_file")
            goal = checked_authorized_path(path.parent / required_text(data, "goal_file"), "file",
                                           allow_external=external)
            content = goal.read_bytes()
            sources[str(goal)] = sha256(content).hexdigest()
            data["goal"] = content.decode("utf-8-sig")
            del data["goal_file"]
        if output_dir is not None:
            data = {**data, "output_dir": str(output_path(Path(output_dir)))}
        return cls(data, base=path.parent, sources=sources)

    FIELDS = {"schema_version", "project_id", "name", "task_type", "goal", "project_dir",
              "verification_dir", "output_dir", "agents", "constraints", "verifiers",
              "scientific_claims", "limits", "allow_external_paths", "readonly_inputs", "file_policy"}

    def __init__(self, data, *, base=WORKDIR, sources=None, verification_manifest=None,
                 readonly_manifest=None):
        data = deepcopy(object_fields(data, self.FIELDS, "project specification"))
        self.schema_version = integer(data.get("schema_version"), "schema_version", 1, 1)
        self.project_id = required_text(data, "project_id")
        self.name = required_text(data, "name")
        self.goal = required_text(data, "goal")
        self.task_type = required_text(data, "task_type")
        if self.task_type not in TASK_TYPES:
            raise ValueError(f"unsupported task_type: {self.task_type}")
        self.allow_external_paths = self._external(data)
        self.project_dir = self.path(base / required_text(data, "project_dir"), "directory")
        self.verification_dir = self.path(base / required_text(data, "verification_dir"), "directory")
        self.output_dir = output_path(base / required_text(data, "output_dir")
                                      if "output_dir" in data else DEFAULT_OUTPUT,
                                      allow_external=self.allow_external_paths)
        _protected(self.project_dir, "project")
        _protected(self.verification_dir, "verification")
        if not str(self.project_dir).isascii() or re.search(r'[%!&|<>^()"\r\n]', str(self.project_dir)):
            raise ValueError("Codex project path must be ASCII and free of cmd metacharacters")
        project, verification, output = self.project_dir, self.verification_dir, self.output_dir
        if overlap(project, verification) or overlap(project, output) or overlap(verification, output):
            raise IntegrityError("project, verification and output directories must be disjoint")
        self.sources = dict(sources or {})
        for source in self.sources:
            source_path = self.path(Path(source), "file")
            if source_path.is_relative_to(project) or source_path.is_relative_to(output):
                raise IntegrityError(f"configuration and goal must be outside writable directories: {source}")
        self.agents = self._agents(data.get("agents", {}))
        self.constraints = self._constraints(data.get("constraints", {}))
        self.limits = self.validate_limits(data.get("limits", {}))
        self.file_policy = self._file_policy(data.get("file_policy", {}))
        self.readonly_inputs = [
            self.path(base / entry) for entry in string_list(data.get("readonly_inputs", []), "readonly_inputs")
        ]
        if len(set(self.readonly_inputs)) != len(self.readonly_inputs):
            raise ValueError("readonly_inputs must be unique")
        for entry in self.readonly_inputs:
            if WORKDIR.is_relative_to(entry) or overlap(entry, project) or overlap(entry, output):
                raise IntegrityError(f"readonly input overlaps a writable directory: {entry}")
        self.verifiers = validate_verifiers(data.get("verifiers"), self)
        self.protected_project_files = {
            Path(item["path"]).as_posix() for item in self.verifiers
            if item["type"] == "artifact" and item["scope"] == "project"
        }
        self.scientific_claims = self._claims(data.get("scientific_claims", []))
        self.verification_manifest = (_verification_manifest(verification) if verification_manifest is None
                                      else dict(verification_manifest))
        self.readonly_manifest = (self.snapshot_readonly() if readonly_manifest is None
                                  else readonly_manifest)

    @staticmethod
    def _external(data):
        external = data.get("allow_external_paths", False)
        if type(external) is not bool:
            raise ValueError("allow_external_paths must be a boolean")
        return external

    def path(self, path, kind=None, *, missing_ok=False):
        return checked_authorized_path(path, kind, allow_external=self.allow_external_paths,
                                       missing_ok=missing_ok)

    @staticmethod
    def _file_policy(policy):
        policy = {**deepcopy(FILE_POLICY),
                  **object_fields(policy, FILE_POLICY.keys(), "file_policy")}
        for key, value in policy.items():
            if key.endswith("_globs"):
                string_list(value, key)
            else:
                integer(value, key, 0 if key in {"max_text_diff_bytes", "max_full_hash_bytes"} else 1)
        return policy

    @staticmethod
    def validate_limits(limits):
        limits = {**DEFAULT_LIMITS, **object_fields(limits, DEFAULT_LIMITS.keys(), "limits")}
        for name, value in limits.items():
            integer(value, name, *HARD_LIMITS[name])
        if limits["max_model_calls"] < limits["max_sol_attempts"]:
            raise ValueError("max_model_calls must be at least max_sol_attempts")
        return limits

    @staticmethod
    def _agents(agents):
        agents = {**DEFAULT_AGENTS, **object_fields(agents, DEFAULT_AGENTS.keys(), "agents")}
        for role in ("coordinator", "executor", "scientist"):
            if agents[role] != DEFAULT_AGENTS[role]:
                raise ValueError(f"unsupported {role} model: {agents[role]!r}")
        if type(agents["require_astra"]) is not bool:
            raise ValueError("require_astra must be a boolean")
        for field in ("sol_effort", "astra_effort"):
            if not re.fullmatch(r"[a-z][a-z0-9_]*", required_text(agents, field)):
                raise ValueError(f"{field} must be a lowercase ASCII identifier")
        if required_text(agents, "planning_order") not in PLANNING_ORDERS:
            raise ValueError(f"unsupported planning_order: {agents['planning_order']!r}")
        if agents["planning_order"] == "astra_first" and not agents["require_astra"]:
            raise ValueError("planning_order 'astra_first' requires require_astra=true")
        return agents

    @staticmethod
    def _constraints(constraints):
        constraints = {**deepcopy(DEFAULT_CONSTRAINTS),
                       **object_fields(constraints, DEFAULT_CONSTRAINTS.keys(), "constraints")}
        for key, value in constraints.items():
            if key == "units":
                if not isinstance(value, dict):
                    raise ValueError("units must map quantity names to unit strings")
                string_list(list(value.values()), key)
            else:
                string_list(value, key)
        return constraints

    def _claims(self, claims):
        if not isinstance(claims, list) or (self.agents["require_astra"] and not claims):
            raise ValueError("scientific_claims must be a list and Astra requires at least one claim")
        identifiers = set()
        verifier_ids = {item["id"] for item in self.verifiers}
        for claim in claims:
            object_fields(claim, {"claim_id", "statement", "required_verifiers", "scope_limitation"},
                          "scientific claim")
            claim_id = required_text(claim, "claim_id")
            required_text(claim, "statement")
            required_text(claim, "scope_limitation")
            required = string_list(claim.get("required_verifiers"), "required_verifiers", nonempty=True)
            if claim_id in identifiers or len(set(required)) != len(required) or set(required) - verifier_ids:
                raise ValueError(f"duplicate claim or invalid verifier references: {claim_id}")
            identifiers.add(claim_id)
        return claims

    def to_dict(self):
        data = {key: getattr(self, key) for key in self.FIELDS}
        for name in ("project_dir", "verification_dir", "output_dir"):
            data[name] = str(data[name])
        data["readonly_inputs"] = [str(path) for path in self.readonly_inputs]
        return deepcopy(data)

    def contract_hash(self, verification_manifest):
        contract = self.to_dict()
        for key in RESUME_LIMITS:
            del contract["limits"][key]
        if contract["agents"].get("planning_order") == "neon_first":
            del contract["agents"]["planning_order"]
        verified = digests(verification_manifest)
        return sha256(json_text({"spec": contract, "verification_manifest": verified})
                      .encode("utf-8")).hexdigest()

    def guard_verification(self):
        self.path(self.verification_dir, "directory")
        current = _verification_manifest(self.verification_dir)
        if digests(current) != digests(self.verification_manifest):
            raise IntegrityError("verification directory differs from its locked SHA-256 manifest")
        return current

    def guard_sources(self):
        for name, digest in self.sources.items():
            if file_sha256(self.path(Path(name), "file")) != digest:
                raise IntegrityError(f"locked configuration or goal changed: {name}")

    def snapshot_project(self, *, spool=None):
        self.path(self.project_dir, "directory")
        return tracked_snapshot(self.project_dir, self.file_policy, spool=spool,
                                protected=self.protected_project_files)

    def snapshot_readonly(self):
        return {str(path): tracked_snapshot(self.path(path), self.file_policy)[0]
                for path in self.readonly_inputs}

    def guard_readonly(self):
        actual = self.snapshot_readonly()
        if actual.keys() != self.readonly_manifest.keys() or any(
                not tracked_equal(expected, actual[name]) for name, expected in self.readonly_manifest.items()):
            raise IntegrityError("a registered readonly input changed")
