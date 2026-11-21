"""Composable, ordered acceptance checks for a locked project specification."""

import math
import os
import re
import shutil
import sys
from dataclasses import asdict, dataclass
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from secrets import token_hex
from string import Formatter
from time import monotonic

from .iteration_evidence import process_problem, required_text, test_result, unittest_command
from .iteration_files import IntegrityError, digests, file_sha256, json_text, stream_manifest
from .process import MAX_PROTOCOL_RECORD_BYTES, ProcessResult, captured_process
from .protocol import _load_json

PLACEHOLDERS = {"python", "project_dir", "verification_dir", "run_dir", "artifacts_dir"}
COMPARISONS = {
    "le": {"threshold"}, "lt": {"threshold"}, "ge": {"threshold"}, "gt": {"threshold"},
    "abs_le": {"threshold"}, "close": {"expected"}, "eq": {"expected"}, "between": {"min", "max"},
}


def object_fields(value, allowed, label):
    if not isinstance(value, dict) or value.keys() - allowed:
        raise ValueError(f"invalid {label} fields: {value!r}")
    return value


def string_list(value, label, *, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(f"{label} must be a list of strings")
    for item in value:
        required_text({label: item}, label)
    return value


def integer(value, label, minimum=None, maximum=None):
    if (type(value) is not int or (minimum is not None and value < minimum)
            or (maximum is not None and value > maximum)):
        raise ValueError(f"{label} must be an integer within [{minimum}, {maximum}]")
    return value


def relative_path(value, label):
    path = Path(value)
    if path.anchor or ".." in path.parts or any(":" in part for part in path.parts):
        raise ValueError(f"{label} must be a relative path without traversal")
    return path


def _number(value):
    try:
        if type(value) in (int, float) and math.isfinite(value):
            return value
    except OverflowError:
        pass
    raise ValueError("expected a finite JSON number")


def _template(arg, paths):
    parts = []
    for literal, field, fmt, conversion in Formatter().parse(arg):
        parts.append(literal)
        if field is not None:
            if field not in PLACEHOLDERS or fmt or conversion:
                raise ValueError(f"invalid command placeholder: {field!r}")
            parts.append(paths[field])
    return "".join(parts)


def _test_config(value, spec):
    key = "tests_dir" if value["type"] == "unittest" else "target"
    object_fields(value, {"id", "type", key, "min_tests", "allow_skipped"}, value["type"])
    path = spec.verification_dir / relative_path(required_text(value, key), key)
    kind = "file" if key == "target" and path.is_file() else "directory"
    spec.path(path, kind)
    integer(value.setdefault("min_tests", 1), "min_tests", 1)
    if type(value.setdefault("allow_skipped", False)) is not bool:
        raise ValueError("allow_skipped must be a boolean")
    return value


def _command_config(value, spec):
    object_fields(value, {"id", "type", "argv", "expected_exit_code", "must_contain"}, "command")
    argv = value.get("argv")
    if (not isinstance(argv, list) or not argv or
            any(not isinstance(arg, str) or "\0" in arg for arg in argv) or not argv[0].strip()):
        raise ValueError("command argv must be an explicit list with a nonempty executable")
    paths = dict.fromkeys(PLACEHOLDERS, "")
    for arg in argv:
        _template(arg, paths)
    integer(value.setdefault("expected_exit_code", 0), "expected_exit_code")
    string_list(value.setdefault("must_contain", []), "must_contain")
    return value


def _file_config(value):
    path = relative_path(required_text(value, "path"), "path")
    if path == Path("."):
        raise ValueError("path must name a file")


def _metric_config(value, spec):
    object_fields(value, {"id", "type", "path", "rules"}, "numeric_metrics")
    _file_config(value)
    rules = value.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError("numeric_metrics requires at least one rule")
    for rule in rules:
        object_fields(rule, {"metric", "op", "unit", "threshold", "expected",
                             "min", "max", "abs_tol", "rel_tol"}, "metric rule")
        key = required_text(rule, "metric")
        if not all(key.split(".")):
            raise ValueError(f"invalid metric path: {key!r}")
        op = required_text(rule, "op")
        if op not in COMPARISONS:
            raise ValueError(f"unsupported metric operator: {op}")
        parameters = COMPARISONS[op]
        optional = {"abs_tol", "rel_tol"} if op == "close" else set()
        if rule.keys() - {"metric", "op", "unit"} - parameters - optional:
            raise ValueError(f"unexpected threshold fields for {op}")
        for name in parameters | (rule.keys() & optional):
            _number(rule.get(name))
        if "unit" in rule:
            required_text(rule, "unit")
        if op == "between" and rule["min"] > rule["max"]:
            raise ValueError("between requires min <= max")
        if any(rule.get(tol, 0) < 0 for tol in optional) or (
            op == "abs_le" and rule["threshold"] < 0
        ):
            raise ValueError("absolute thresholds and tolerances must be nonnegative")
    return value


def _artifact_config(value, spec):
    object_fields(value, {"id", "type", "path", "scope", "min_bytes", "format", "required_keys",
                          "exact_fields", "must_contain"}, "artifact")
    _file_config(value)
    if value.setdefault("scope", "artifacts") not in ("artifacts", "project"):
        raise ValueError("artifact scope must be artifacts or project")
    fmt = value.setdefault("format", "file")
    if fmt not in ("file", "text", "json"):
        raise ValueError("artifact format must be file, text or json")
    integer(value.setdefault("min_bytes", 1), "min_bytes", 0)
    string_list(value.setdefault("required_keys", []), "required_keys")
    string_list(value.setdefault("must_contain", []), "must_contain")
    if not isinstance(value.setdefault("exact_fields", {}), dict):
        raise ValueError("exact_fields must be an object")
    if (value["exact_fields"] or value["required_keys"]) and fmt != "json":
        raise ValueError("required_keys and exact_fields require JSON format")
    return value


@dataclass(frozen=True)
class VerificationReport:
    all_passed: bool
    results: list[dict]
    failed_verifiers: list[str]
    metrics_snapshots: dict
    artifacts_dir: str
    tamper_ok: bool = True

    def to_dict(self):
        return asdict(self)


class _Verification:
    def __init__(self, spec, files, step, runner, deadline, clock):
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", step) or step in {".", ".."}:
            raise ValueError(f"invalid verification step: {step}")
        self.spec, self.files, self.runner, self.clock = spec, files, runner, clock
        self.deadline = deadline
        parent = spec.path(files.directory / "artifacts", "directory", missing_ok=True)
        if files.directory.parent != spec.output_dir:
            raise IntegrityError("verification run location differs from the configured output")
        parent.mkdir(exist_ok=True)
        self.artifacts = spec.path(parent / step, "directory", missing_ok=True)
        try:
            self.artifacts.mkdir()
        except FileExistsError as exc:
            raise IntegrityError(f"verification artifacts directory is already in use: {self.artifacts}") from exc
        self.generated = {}
        self.metrics = {}
        self.metric_files = {}

    @contextmanager
    def run(self, argv, *, stdin=None, env_extra=None):
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise ValueError("verification wall-clock budget exhausted")
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith(("PYTHON", "PYTEST_"))}
        env.update(AGENTBRIDGE_IN_SUBPROCESS="1", AGENTBRIDGE_RUN_ID=self.files.directory.name,
                   PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONSAFEPATH="1",
                   PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
        env.update(env_extra or {})
        with captured_process(self.runner, argv, cwd=self.artifacts,
                              timeout_s=min(self.spec.limits["timeout_s"], remaining),
                              stdin=stdin, env=env) as result:
            yield result, {"argv": argv, **result.audit(self.spec.file_policy["max_audit_stream_bytes"])}


def _unittest(ctx, value):
    proof = token_hex(16)
    cache = ctx.artifacts / f"_cache_{token_hex(16)}"
    with ctx.run(unittest_command(ctx.spec.verification_dir / value["tests_dir"],
                                  ctx.spec.project_dir, cache), stdin=proof.encode("ascii")) as (result, audit):
        summary = test_result(result, value["min_tests"], value["allow_skipped"], proof)
    summary.pop("process")
    return {**audit, "passed": summary.pop("test_passed"), **summary}


_PYTEST_SUMMARY = re.compile(r"^=+ (.+) in [0-9.]+s =+$")
_PYTEST_COUNT = re.compile(r"(\d{1,9}) (passed|failed|errors?|skipped|warnings?|"
                           r"xfailed|xpassed|deselected)")


def _pytest(ctx, value):
    # PYTHONPATH exposes the project for tests. Keep it from supplying the test
    # runner itself, Python startup hooks, or a local pytest dependency.
    for name in ("pytest", "_pytest", "pluggy", "packaging", "iniconfig", "sitecustomize",
                 "usercustomize", "colorama", "tomli", "exceptiongroup"):
        for path in (ctx.spec.project_dir / name, ctx.spec.project_dir / (name + ".py"),
                     ctx.spec.project_dir / (name + ".pyc"), ctx.spec.project_dir / (name + ".pyd")):
            if path.exists():
                raise IntegrityError(f"project shadows pytest or Python startup: {path}")
    with ctx.run(
        [sys.executable, "-B", "-m", "pytest", "-p", "no:cacheprovider", "-v",
         str(ctx.spec.verification_dir / value["target"])],
        env_extra={"PYTEST_ADDOPTS": "--color=no", "PYTEST_PLUGINS": "",
                   "PYTHONPATH": str(ctx.spec.project_dir),
                   "PYTHONPYCACHEPREFIX": str(ctx.artifacts / f"_cache_{token_hex(16)}")},
    ) as (result, audit):
        last_line = ""
        for line in result.lines("stdout"):
            if line.strip():
                last_line = line.strip()
    match = _PYTEST_SUMMARY.fullmatch(last_line)
    counts = dict.fromkeys(("passed", "failed", "error", "skipped", "xfailed", "xpassed"), 0)
    seen = set()
    valid = match is not None
    for part in match[1].split(", ") if match else ():
        count = _PYTEST_COUNT.fullmatch(part)
        if not count:
            valid = False
            break
        key = count[2].removesuffix("s")
        if key in seen:
            valid = False
            break
        seen.add(key)
        if key in counts:
            counts[key] = int(count[1])
    problem = process_problem(result)
    if not problem:
        if not valid or counts["passed"] < value["min_tests"]:
            problem = "pytest did not report the minimum number of passed tests"
        elif counts["failed"] or counts["error"] or counts["xpassed"]:
            problem = "pytest reported failed tests, errors or unexpected passes"
        elif not value["allow_skipped"] and (counts["skipped"] or counts["xfailed"]):
            problem = "pytest skipped or expected-failure tests are not allowed"
    return {**audit, "passed": problem is None, "counts": counts, "reason": problem}


def _command(ctx, value):
    paths = {"python": str(sys.executable), "project_dir": str(ctx.spec.project_dir),
             "verification_dir": str(ctx.spec.verification_dir), "run_dir": str(ctx.files.directory),
             "artifacts_dir": str(ctx.artifacts)}
    argv = [_template(arg, paths) for arg in value["argv"]]
    executable = shutil.which(argv[0])
    if not executable:
        raise ValueError(f"command executable unavailable: {argv[0]}")
    executable = Path(executable).resolve()
    if (executable.is_relative_to(ctx.spec.project_dir) or executable.is_relative_to(ctx.spec.output_dir)
            or executable.suffix.casefold() in {".cmd", ".bat"}):
        raise IntegrityError(f"command requires a trusted executable: {executable}")
    argv[0] = str(executable)
    before = digests(stream_manifest(ctx.artifacts))
    with ctx.run(argv) as (result, audit):
        healthy = result.utf8_ok and not (result.launch_error or result.timed_out)
        reason = None
        if not healthy:
            reason = process_problem(result)
        elif result.exit_code != value["expected_exit_code"]:
            reason = f"expected exit {value['expected_exit_code']}, got {result.exit_code}"
        elif not result.contains(value["must_contain"]):
            reason = "command output is missing a required substring"
    after = digests(stream_manifest(ctx.artifacts))
    outputs = {}
    for name in before.keys() | after.keys():
        if before.get(name) != after.get(name):
            ctx.generated.pop(name, None)
            if reason is None and name in after:
                outputs[name] = after[name]
    ctx.generated.update({name: (value["id"], digest) for name, digest in outputs.items()})
    return {**audit, "passed": reason is None, "reason": reason, "generated_files": outputs}


def _compare(rule, data):
    result = {"rule": rule, "actual": None, "margin": None, "passed": False}
    try:
        for part in rule["metric"].split("."):
            if not isinstance(data, dict) or part not in data:
                raise ValueError(f"missing metric {rule['metric']}")
            data = data[part]
        actual = _number(data)
    except ValueError as exc:
        return {**result, "reason": str(exc)}
    op = rule["op"]
    if op in ("le", "lt", "abs_le"):
        magnitude = abs(actual) if op == "abs_le" else actual
        margin = rule["threshold"] - magnitude
        passed = magnitude < rule["threshold"] if op == "lt" else magnitude <= rule["threshold"]
    elif op in ("ge", "gt"):
        margin = actual - rule["threshold"]
        passed = actual > rule["threshold"] if op == "gt" else actual >= rule["threshold"]
    elif op == "between":
        margin = min(actual - rule["min"], rule["max"] - actual)
        passed = rule["min"] <= actual <= rule["max"]
    else:
        expected = rule["expected"]
        if op == "eq":
            margin, passed = -abs(actual - expected), actual == expected
        else:
            abs_tol, rel_tol = rule.get("abs_tol", 0), rule.get("rel_tol", 0)
            passed = math.isclose(actual, expected, abs_tol=abs_tol, rel_tol=rel_tol)
            margin = max(abs_tol, rel_tol * max(abs(actual), abs(expected))) - abs(actual - expected)
    try:
        margin = _number(margin)
    except ValueError:
        margin = None
    return {**result, "passed": passed, "actual": actual, "margin": margin}


def _numeric(ctx, value):
    path = relative_path(value["path"], "path").as_posix()
    source = ctx.generated.get(path)
    if source is None:
        raise ValueError("metrics require a file generated by a successful command in this step")
    target = ctx.spec.path(ctx.artifacts / path, "file")
    payload = _json_bytes(target)
    producer, digest = source
    if ctx.metric_files.get(path, digest) != digest:
        raise IntegrityError(f"metrics changed after numerical verification: {path}")
    if sha256(payload).hexdigest() != digest:
        raise IntegrityError(f"metrics changed after command {producer}: {path}")
    data = _load_json(payload.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("metrics must be a JSON object")
    ctx.metrics[value["id"]] = data
    ctx.metric_files[path] = digest
    checks = [_compare(rule, data) for rule in value["rules"]]
    passed = all(check["passed"] for check in checks)
    return {"passed": passed, "reason": None if passed else "numeric criteria failed",
            "producer": producer, "sha256": digest, "checks": checks}


def _artifact(ctx, value):
    root = ctx.artifacts if value["scope"] == "artifacts" else ctx.spec.project_dir
    path = ctx.spec.path(root / value["path"], "file", missing_ok=True)
    if not path.is_file():
        raise ValueError("required artifact is missing")
    size = path.stat().st_size
    if size < value["min_bytes"]:
        raise ValueError("artifact is smaller than min_bytes")
    fmt = value["format"]
    if fmt != "file" or value["must_contain"]:
        with path.open("rb") as stream:
            result = ProcessResult(_streams={"stdout": stream})
            for _ in result.chunks("stdout", errors="strict"):
                pass
            if not result.contains(value["must_contain"], channels=("stdout",)):
                raise ValueError("artifact is missing a required substring")
        if fmt == "json":
            obj = _load_json(_json_bytes(path).decode("utf-8"))
            if value["required_keys"] or value["exact_fields"]:
                if not isinstance(obj, dict) or set(value["required_keys"]) - obj.keys():
                    raise ValueError("artifact JSON lacks required keys")
                for key, expected in value["exact_fields"].items():
                    if key not in obj or json_text(obj[key]) != json_text(expected):
                        raise ValueError(f"artifact JSON field differs: {key}")
    return {"passed": True, "bytes": size, "path": str(path), "sha256": file_sha256(path)}


def _json_bytes(path):
    with path.open("rb") as stream:
        data = stream.read(MAX_PROTOCOL_RECORD_BYTES + 1)
    if len(data) > MAX_PROTOCOL_RECORD_BYTES:
        raise ValueError(f"JSON artifact exceeds 16 MiB: {path}")
    return data


# Registration binds definition validation to the corresponding runtime handler.
VERIFIER_REGISTRY = {
    "unittest": (_test_config, _unittest),
    "pytest": (_test_config, _pytest),
    "command": (_command_config, _command),
    "numeric_metrics": (_metric_config, _numeric),
    "artifact": (_artifact_config, _artifact),
}


def validate_verifiers(values, spec):
    if not isinstance(values, list) or not values:
        raise ValueError("verifiers must be a nonempty ordered list")
    seen = set()
    has_command = False
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("verifier must be an object")
        identifier = required_text(value, "id")
        category = required_text(value, "type")
        if identifier in seen or category not in VERIFIER_REGISTRY:
            raise ValueError(f"duplicate verifier ID or unsupported type: {identifier}, {category}")
        if category == "numeric_metrics" and not has_command:
            raise ValueError("numeric_metrics requires an earlier command verifier")
        VERIFIER_REGISTRY[category][0](value, spec)
        seen.add(identifier)
        has_command = has_command or category == "command"
    return values


def run_verifiers(spec, run_files, step, runner, remaining_budget_s, *, clock=monotonic):
    deadline = clock() + remaining_budget_s
    spec.guard_verification()
    ctx = _Verification(spec, run_files, step, runner, deadline, clock)
    results = []
    for value in spec.verifiers:
        try:
            if clock() >= ctx.deadline:
                raise ValueError("verification wall-clock budget exhausted")
            result = VERIFIER_REGISTRY[value["type"]][1](ctx, value)
        except (IntegrityError, PermissionError):
            raise
        except (OSError, ValueError, RecursionError) as exc:
            result = {"passed": False, "reason": str(exc)}
        finally:
            spec.guard_verification()
        results.append({"id": value["id"], "type": value["type"], **result})
    for path, digest in ctx.metric_files.items():
        target = spec.path(ctx.artifacts / path, "file")
        if file_sha256(target) != digest:
            raise IntegrityError(f"metrics changed after numerical verification: {path}")
    failures = [result["id"] for result in results if not result["passed"]]
    return VerificationReport(not failures, results, failures, ctx.metrics, str(ctx.artifacts))
