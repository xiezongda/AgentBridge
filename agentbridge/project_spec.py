"""Load and lock a human-editable version 1 project contract."""

import re
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

from .bridge import DEFAULT_OUTPUT, NEON_MODEL, SOL_MODEL, WORKDIR
from .iteration_evidence import required_text
from .iteration_files import IntegrityError, checked_path, json_text, manifest, overlap, snapshot
from .protocol import _load_json
from .verifiers import integer, object_fields, string_list, validate_verifiers

ASTRA_MODEL = "gpt-6.2-astra"
DEFAULT_AGENTS = {
    "coordinator": NEON_MODEL, "executor": SOL_MODEL, "scientist": ASTRA_MODEL,
    "require_astra": False, "sol_effort": "high", "astra_effort": "high",
}
DEFAULT_LIMITS = {
    "max_sol_attempts": 3, "max_model_calls": 12, "max_astra_calls": 4,
    "max_plan_revisions": 2, "max_consecutive_failures": 2, "timeout_s": 240,
    "max_total_seconds": 1200,
}
HARD_LIMITS = {
    "max_sol_attempts": (1, 3), "max_model_calls": (1, 12),
    "max_astra_calls": (1, 4), "max_plan_revisions": (0, 2),
}
RESUME_LIMITS = ("max_sol_attempts", "max_model_calls", "max_astra_calls", "max_total_seconds",
                 "max_consecutive_failures")
DEFAULT_CONSTRAINTS = {"physical_constraints": [], "units": {}, "assumptions": [],
                       "scope_limitations": []}
TASK_TYPES = ("engineering", "scientific", "numerical", "data_analysis")
PROTECTED = {".git", ".agents", ".codex", ".aws", ".github", "agentbridge", "tests", "runs"}
ACCEPTANCE_NAMES = {"acceptance", "acceptance_tests", "acceptance_tests_dir", "verification"}


def _protected(path, role):
    parts = tuple(part.casefold() for part in path.relative_to(WORKDIR).parts)
    reserved = PROTECTED - {"runs"} if role == "output" else PROTECTED
    if role == "verification":
        reserved = reserved - {"tests"}
    if (parts[0] in reserved or
            (role != "verification" and (ACCEPTANCE_NAMES.intersection(parts) or parts == ("examples",)))):
        raise IntegrityError(f"protected {role} directory: {path}")


def output_path(path):
    output = checked_path(path, "directory", missing_ok=True)
    _protected(output, "output")
    return output


def _verification_manifest(directory):
    files = snapshot(directory)
    # The shared snapshot excludes bytecode. Keep the locked tree source-only so
    # every file admitted to verification is covered by the manifest.
    for path in directory.rglob("*"):
        if path.name.casefold() == "__pycache__" or path.suffix.casefold() == ".pyc":
            raise IntegrityError(f"verification tree contains Python bytecode: {path}")
    return manifest(files)


class ProjectSpec:
    """Resolved paths, normalized definitions, and the pre-run verifier manifest."""

    @classmethod
    def from_file(cls, path, *, output_dir=None):
        path = checked_path(Path(path), "file")
        source = path.read_bytes()
        data = _load_json(source.decode("utf-8-sig"))
        sources = {str(path): sha256(source).hexdigest()}
        if isinstance(data, dict) and "goal_file" in data:
            if "goal" in data:
                raise ValueError("specify exactly one of goal or goal_file")
            goal = checked_path(path.parent / required_text(data, "goal_file"), "file")
            content = goal.read_bytes()
            sources[str(goal)] = sha256(content).hexdigest()
            data["goal"] = content.decode("utf-8-sig")
            del data["goal_file"]
        if output_dir is not None:
            data = {**object_fields(data, cls.FIELDS, "project specification"),
                    "output_dir": str(output_path(Path(output_dir)))}
        return cls(data, base=path.parent, sources=sources)

    FIELDS = {"schema_version", "project_id", "name", "task_type", "goal", "project_dir",
              "verification_dir", "output_dir", "agents", "constraints", "verifiers",
              "scientific_claims", "limits"}

    def __init__(self, data, *, base=WORKDIR, sources=None, verification_manifest=None):
        data = deepcopy(object_fields(data, self.FIELDS, "project specification"))
        self.schema_version = integer(data.get("schema_version"), "schema_version", 1, 1)
        self.project_id = required_text(data, "project_id")
        self.name = required_text(data, "name")
        self.goal = required_text(data, "goal")
        self.task_type = required_text(data, "task_type")
        if self.task_type not in TASK_TYPES:
            raise ValueError(f"unsupported task_type: {self.task_type}")
        self.project_dir = checked_path(base / required_text(data, "project_dir"), "directory")
        self.verification_dir = checked_path(base / required_text(data, "verification_dir"), "directory")
        self.output_dir = output_path(base / required_text(data, "output_dir")
                                      if "output_dir" in data else DEFAULT_OUTPUT)
        _protected(self.project_dir, "project")
        _protected(self.verification_dir, "verification")
        if not str(self.project_dir).isascii() or re.search(r'[%!&|<>^()"\r\n]', str(self.project_dir)):
            raise ValueError("Codex project path must be ASCII and free of cmd metacharacters")
        project, verification, output = self.project_dir, self.verification_dir, self.output_dir
        if overlap(project, verification) or overlap(project, output) or overlap(verification, output):
            raise IntegrityError("project, verification and output directories must be disjoint")
        self.sources = dict(sources or {})
        for source in self.sources:
            source_path = checked_path(Path(source), "file")
            if source_path.is_relative_to(project) or source_path.is_relative_to(output):
                raise IntegrityError(f"configuration and goal must be outside writable directories: {source}")
        self.agents = self._agents(data.get("agents", {}))
        self.constraints = self._constraints(data.get("constraints", {}))
        self.limits = self.validate_limits(data.get("limits", {}))
        self.verifiers = validate_verifiers(data.get("verifiers"), self)
        self.scientific_claims = self._claims(data.get("scientific_claims", []))
        self.verification_manifest = (_verification_manifest(verification) if verification_manifest is None
                                      else dict(verification_manifest))

    @staticmethod
    def validate_limits(limits):
        limits = {**DEFAULT_LIMITS, **object_fields(limits, DEFAULT_LIMITS.keys(), "limits")}
        for name, value in limits.items():
            integer(value, name, *HARD_LIMITS.get(name, (1, None)))
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
        return deepcopy(data)

    def contract_hash(self, verification_manifest):
        contract = self.to_dict()
        for key in RESUME_LIMITS:
            del contract["limits"][key]
        return sha256(json_text({"spec": contract, "verification_manifest": verification_manifest})
                      .encode("utf-8")).hexdigest()

    def guard_verification(self):
        if _verification_manifest(self.verification_dir) != self.verification_manifest:
            raise IntegrityError("verification directory differs from its locked SHA-256 manifest")

    def guard_sources(self):
        for name, digest in self.sources.items():
            if sha256(checked_path(Path(name), "file").read_bytes()).hexdigest() != digest:
                raise IntegrityError(f"locked configuration or goal changed: {name}")
