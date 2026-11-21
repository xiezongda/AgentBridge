"""Drive an engineering iteration through independently verified checkpoints."""

import math
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from secrets import token_hex
from time import monotonic
from uuid import uuid4

from .bridge import DEFAULT_AGY, DEFAULT_CODEX, DEFAULT_OUTPUT, WORKDIR
from .iteration_evidence import (
    NeonUnavailable, bounded_json, excerpt, neon_command, parse_neon, parse_sol, process_problem,
    required_text, sol_command, test_result, unittest_command, validate_review, validate_sol,
)
from .iteration_files import (
    IntegrityError, RunFiles, changes_and_patch, checked_path, json_text, manifest,
    overlap, snapshot, workspace_path,
)
from .process import run_process

PAUSE_STAGES = ("baseline_test", "neon_plan", "sol_exec", "round_test", "neon_review")
_ACTIVE = ("INIT", "BASELINE_TEST", "NEON_PLAN", "ROUND_SOL_EXEC", "ROUND_TEST",
           "ROUND_NEON_REVIEW", "ROUND_DECIDE")
_RESUMABLE = {
    "PAUSED", "STOPPED_MAX_ATTEMPTS", "STOPPED_BUDGET_EXCEEDED",
    "STOPPED_TIMEOUT_BUDGET", "STOPPED_CONSECUTIVE_FAILURES", "STOPPED_MODEL_UNAVAILABLE",
}
_LIMIT_OPTIONS = ("max_sol_attempts", "max_model_calls", "max_total_seconds",
                  "max_consecutive_failures")
_RESERVED = {".git", ".agents", ".codex", ".aws", "agentbridge", "tests", "runs"}
_ACCEPTANCE = {"acceptance_tests", "acceptance_tests_dir"}
_TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens",
                 "reasoning_output_tokens", "total_tokens")


def reject_nested() -> None:
    if os.environ.get("AGENTBRIDGE_IN_SUBPROCESS") == "1":
        raise ValueError("nested AgentBridge scheduling is forbidden (AGENTBRIDGE_IN_SUBPROCESS=1)")


def _check_pause_stage(stage: str | None) -> None:
    if stage is not None and stage not in PAUSE_STAGES:
        raise ValueError(f"invalid pause stage: {stage}")


def _output_folder(path: Path) -> Path:
    path = workspace_path(path)
    parts = [part.casefold() for part in path.relative_to(WORKDIR).parts]
    if parts[0] in _RESERVED - {"runs"} or _ACCEPTANCE.intersection(parts):
        raise ValueError(f"reserved audit output directory: {path}")
    return path


@dataclass(frozen=True)
class IterationConfig:
    goal: str
    project_dir: Path
    tests_dir: Path
    output_dir: Path = DEFAULT_OUTPUT
    max_sol_attempts: int = 3
    max_model_calls: int = 7
    max_consecutive_failures: int = 2
    timeout_s: int = 240
    max_total_seconds: int = 900
    sol_effort: str = "high"
    min_tests: int = 1
    allow_skipped: bool = False
    acceptance_cmd: tuple[str, ...] | None = None

    def __post_init__(self):
        required_text({"goal": self.goal}, "goal")
        for name, cap in (("max_sol_attempts", 3), ("max_model_calls", 7),
                          ("max_consecutive_failures", None), ("timeout_s", None),
                          ("max_total_seconds", None), ("min_tests", None)):
            value = getattr(self, name)
            if type(value) is not int or value < 1 or (cap is not None and value > cap):
                limit = "a positive integer" if cap is None else f"an integer from 1 to {cap}"
                raise ValueError(f"{name} must be {limit}")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", self.sol_effort):
            raise ValueError("sol_effort must be a lowercase ASCII identifier")
        for name in ("project_dir", "tests_dir", "output_dir"):
            object.__setattr__(self, name, workspace_path(Path(getattr(self, name))))
        project, tests, output = self.project_dir, self.tests_dir, self.output_dir
        project_parts, tests_parts = (
            [part.casefold() for part in path.relative_to(WORKDIR).parts]
            for path in (project, tests)
        )
        if project_parts[0] in _RESERVED or _ACCEPTANCE.intersection(project_parts):
            raise ValueError(f"reserved project directory: {project}")
        if not str(project).isascii() or re.search(r'[%!&|<>^()"\r\n]', str(project)):
            raise ValueError("Codex project path must be ASCII and free of cmd metacharacters")
        if tests_parts[0] in _RESERVED - {"tests"}:
            raise ValueError(f"reserved test directory: {tests}")
        _output_folder(output)
        if overlap(project, tests) or overlap(output, project) or overlap(output, tests):
            raise ValueError("project, acceptance tests, and audit output must be disjoint")
        if self.acceptance_cmd is not None:
            if not isinstance(self.acceptance_cmd, (tuple, list)) or not self.acceptance_cmd:
                raise ValueError("acceptance_cmd must contain an executable and its arguments")
            if any(not isinstance(arg, str) or "\0" in arg for arg in self.acceptance_cmd):
                raise ValueError("acceptance_cmd arguments must be strings without NUL")
            if not self.acceptance_cmd[0]:
                raise ValueError("acceptance_cmd executable cannot be empty")
            object.__setattr__(self, "acceptance_cmd", tuple(self.acceptance_cmd))

    def persisted(self) -> dict:
        config = asdict(self)
        for name in ("project_dir", "tests_dir", "output_dir"):
            config[name] = str(config[name])
        return config


class _Stop(Exception):
    def __init__(self, state: str, reason: str):
        self.state, self.reason = state, reason


class _Iteration:
    def __init__(self, config, files, state, pause_after, runner, clock):
        self.config = config
        self.files = files
        self.state = state
        self.pause_after = pause_after
        self.runner = runner
        self.clock = clock
        self.previous_elapsed = state["elapsed_seconds"]
        self.started = clock()

    @property
    def elapsed(self):
        return self.previous_elapsed + (self.clock() - self.started)

    @property
    def round(self):
        return self.state["rounds"][str(self.state["current_round"])]

    def _save(self, event, **details):
        self.state["elapsed_seconds"] = round(self.elapsed, 6)
        self.files.record(self.state, event, details)

    def _move(self, name, event, **details):
        self.state["current_state"] = name
        self._save(event, **details)

    def _stop(self, name, reason):
        current = self.state["current_state"]
        self.state.update(resume_state=current if current in _ACTIVE else self.state["resume_state"],
                          resumable=name in _RESUMABLE, stop_reason=reason)
        self._move(name, "stopped", reason=reason)

    def drive(self):
        handlers = {
            "INIT": self._initialize, "BASELINE_TEST": self._baseline, "NEON_PLAN": self._plan,
            "ROUND_SOL_EXEC": self._sol, "ROUND_TEST": self._test,
            "ROUND_NEON_REVIEW": self._review, "ROUND_DECIDE": self._decide,
        }
        try:
            if self.state["pending_step"]:
                return self._interrupted()
            if self.state["current_state"] not in _ACTIVE:
                next_state = self.state["resume_state"]
                if next_state not in _ACTIVE:
                    raise ValueError("invalid resume checkpoint")
                self.state.update(stop_reason=None, resume_state=None, resumable=True)
                self._move(next_state, "resumed")
            while self.state["current_state"] in _ACTIVE:
                state = self.state["current_state"]
                if state != "INIT":
                    self._guard_acceptance()
                    self._guard_tested_project()
                if self.files.path("PAUSE").is_file():
                    self._pause("pause marker")
                    break
                self._remaining()
                self._save("enter_state")
                handlers[state]()
        except IntegrityError as exc:
            self.state.update(tamper_ok=False, tampering_detected=True)
            self._stop("STOPPED_PERMISSION_OR_TAMPER", str(exc))
        except PermissionError as exc:
            self.state["tamper_ok"] = False
            self._stop("STOPPED_PERMISSION_OR_TAMPER", str(exc))
        except _Stop as exc:
            self._stop(exc.state, exc.reason)
        except KeyboardInterrupt:
            if self.state["pending_step"]:
                return self._interrupted()
            self._pause("interrupted between steps")
        except (OSError, ValueError, RecursionError) as exc:
            self._stop("FAILED", str(exc))
        return self.state

    def _interrupted(self):
        pending = self.state["pending_step"]["step"]
        self._stop("STOPPED_NEED_HUMAN",
                   f"interrupted during {pending}; the external step may already have run")
        return self.state

    def _pause(self, reason):
        self._stop("PAUSED", reason)
        self.files.path("PAUSE").unlink(missing_ok=True)

    def _complete(self, step, next_state, pause_stage=None):
        self.state["pending_step"] = None
        self.state["completed_steps"].append(step)
        self._move(next_state, "step_completed", step=step)
        if pause_stage == self.pause_after and pause_stage is not None:
            self._pause(f"pause after {pause_stage}")

    def _initialize(self):
        snapshot(self.config.project_dir)
        self.state["acceptance_manifest"] = manifest(snapshot(self.config.tests_dir))
        self.state["tamper_ok"] = True
        self._complete("init", "BASELINE_TEST")

    def _guard_acceptance(self):
        current = manifest(snapshot(self.config.tests_dir))
        expected = self.state["acceptance_manifest"]
        if current != expected:
            drift = {
                "added": sorted(current.keys() - expected.keys()),
                "deleted": sorted(expected.keys() - current.keys()),
                "modified": sorted(name for name in current.keys() & expected.keys()
                                   if current[name] != expected[name]),
            }
            self.state["acceptance_drift"] = drift
            raise IntegrityError(f"acceptance test baseline changed: {json_text(drift)}")

    def _guard_tested_project(self):
        if self.state["current_state"] in {"ROUND_TEST", "ROUND_NEON_REVIEW", "ROUND_DECIDE"}:
            if manifest(snapshot(self.config.project_dir)) != self.round["project_manifest"]:
                raise IntegrityError("project content changed after the Sol checkpoint")

    def _remaining(self):
        budget = self.config.max_total_seconds - self.elapsed
        if budget <= 0:
            raise _Stop("STOPPED_TIMEOUT_BUDGET", "total active wall-clock budget exhausted")
        return budget

    def _invoke(self, step, role, make_command, *, prompt=None, stdin=None):
        state, config = self.state, self.config
        if step in state["completed_steps"]:
            raise ValueError(f"checkpoint attempted to replay completed step {step}")
        if role != "test" and state["total_model_calls"] >= config.max_model_calls:
            raise _Stop("STOPPED_BUDGET_EXCEEDED", "model call budget exhausted")
        # Use a fresh audit path even when a failed invocation is explicitly retried.
        number = state["total_process_calls"] + 1
        audit_file = f"{number:03d}_{step.replace('.', '_')}.json"
        cache = self.files.path(f"unused_pycache_{number:03d}")
        if cache.exists():
            raise IntegrityError(f"expected an unused Python bytecode cache path: {cache}")
        budget = self._remaining()
        argv = make_command(min(config.timeout_s, math.ceil(budget)), cache)
        if role == "neon" and len(subprocess.list2cmdline(argv).encode("utf-16-le")) // 2 >= 32767:
            raise ValueError("Neon prompt exceeds the Windows command-line limit")
        env = dict(os.environ, AGENTBRIDGE_IN_SUBPROCESS="1", AGENTBRIDGE_RUN_ID=state["run_id"],
                   PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        if role == "test":
            env.update(PYTHONPATH=str(config.project_dir), PYTHONDONTWRITEBYTECODE="1",
                       PYTHONPYCACHEPREFIX=str(cache))
        state["total_process_calls"] = number
        state["total_model_calls"] += int(role != "test")
        state["sol_attempts"] += int(role == "sol")
        state["pending_step"] = {"step": step, "audit_file": audit_file}
        self._save("invocation_started", step=step, audit_file=audit_file)
        budget = config.max_total_seconds - self.elapsed
        if budget <= 0:
            state["total_process_calls"] -= 1
            state["total_model_calls"] -= int(role != "test")
            state["sol_attempts"] -= int(role == "sol")
            state["pending_step"] = None
            raise _Stop("STOPPED_TIMEOUT_BUDGET", "wall-clock budget exhausted before launch")
        result = self.runner(argv, cwd=config.project_dir, timeout_s=min(config.timeout_s, budget),
                             stdin=stdin, env=env)
        audit = {"argv": argv, "role": role, "prompt": prompt, "stdout": result.stdout,
                 "stderr": result.stderr, "process": result.audit_metadata()}
        self._write_audit(audit_file, audit)
        return result, audit_file, audit, budget <= config.timeout_s

    def _write_audit(self, name, audit):
        self.files.write(name, json_text(audit) + "\n")

    def _total_timeout(self, result, budget_limited):
        if result.timed_out and budget_limited:
            self.state["pending_step"] = None
            raise _Stop("STOPPED_TIMEOUT_BUDGET", "subprocess exhausted the remaining wall-clock budget")

    def _record_usage(self, role, envelope):
        usage = envelope.get("usage")
        if isinstance(usage, dict):
            counts = {key: usage[key] for key in _TOKEN_FIELDS
                      if type(usage.get(key)) is int and usage[key] >= 0}
            if "input_tokens" in counts and "output_tokens" in counts:
                counts.setdefault("total_tokens", counts["input_tokens"] + counts["output_tokens"])
            for key, value in counts.items():
                self.state["tokens"][role][key] += value
                self.state["tokens"]["combined"][key] += value

    def _accept(self, step):
        def command(_timeout, cache):
            return (list(self.config.acceptance_cmd) if self.config.acceptance_cmd else
                    unittest_command(self.config.tests_dir, self.config.project_dir, cache))

        proof = None if self.config.acceptance_cmd else token_hex(16)
        result, filename, audit, budget_limited = self._invoke(
            step, "test", command, stdin=proof.encode("ascii") if proof else None,
        )
        summary = test_result(result, self.config.min_tests, self.config.allow_skipped, proof)
        self._write_audit(filename, {**audit, "summary": summary})
        summary["audit_file"] = filename
        self.state["test_status"] = summary
        self._guard_acceptance()
        self._guard_tested_project()
        self._total_timeout(result, budget_limited)
        return summary

    def _baseline(self):
        summary = self._accept("baseline_test")
        self.state["baseline_test"] = summary
        process = summary["process"]
        if process["launch_error"] or process["timed_out"] or not process["utf8_ok"]:
            self.state["pending_step"] = None
            raise _Stop("FAILED", f"baseline_test: {summary['reason']}")
        self._complete("baseline_test", "NEON_PLAN", "baseline_test")

    def _test_feedback(self, summary):
        audit = self.files.read(summary["audit_file"])
        return {"summary": summary, "stdout": excerpt(audit["stdout"], 2000),
                "stderr": excerpt(audit["stderr"], 3500)}

    def _neon(self, step, prompt, validate):
        conversation = self.state["neon_conversation_id"]
        result, filename, audit, budget_limited = self._invoke(
            step, "neon",
            lambda timeout, _cache: neon_command(DEFAULT_AGY, timeout, prompt, conversation),
            prompt=prompt,
        )
        self._guard_acceptance()
        self._guard_tested_project()
        self._total_timeout(result, budget_limited)
        self.state["pending_step"] = None
        if problem := process_problem(result):
            code = "FAILED" if result.exit_code == 0 and not result.utf8_ok else "STOPPED_MODEL_UNAVAILABLE"
            raise _Stop(code, f"{step}: {problem}")
        try:
            envelope, payload = parse_neon(result.stdout, conversation)
            audit.update(envelope=envelope, payload=payload)
            self._record_usage("neon", envelope)
            validate(payload)
        except (ValueError, RecursionError) as exc:
            self._write_audit(filename, {**audit, "protocol_error": str(exc)})
            code = "STOPPED_MODEL_UNAVAILABLE" if isinstance(exc, NeonUnavailable) else "FAILED"
            raise _Stop(code, f"{step}: {exc}") from exc
        self._write_audit(filename, audit)
        self.state["neon_conversation_id"] = envelope["conversation_id"]
        return payload

    def _plan(self):
        baseline = bounded_json(self._test_feedback(self.state["baseline_test"]), 7500)
        prompt = (
            "Plan the code changes for Sol using only the supplied goal and independent baseline. "
            "Do not use tools or access files. Return a JSON object with a nonempty plan string.\n\n"
            f"Goal:\n{excerpt(self.config.goal, 3500)}\n\nIndependent baseline:\n{baseline}"
        )
        self.state["plan"] = self._neon("neon_plan", prompt, lambda obj: required_text(obj, "plan"))
        self._complete("neon_plan", "ROUND_SOL_EXEC", "neon_plan")

    def _round_feedback(self, data, diff_limit):
        execution = data["executions"][-1]
        diff = self.files.path(execution["patch_file"]).read_bytes().decode("utf-8")
        feedback = {"files": bounded_json(execution["changes"], 2500),
                    "unified_diff": excerpt(diff, diff_limit),
                    "sol_summary": excerpt(execution.get("agent_message") or "", 1800)}
        if "test" in data:
            feedback["independent_test"] = self._test_feedback(data["test"])
        if "review" in data:
            feedback["neon_review"] = data["review"]
        if len(data["executions"]) > 1:
            feedback["earlier_changes"] = [
                {"files": bounded_json(prior["changes"], 1000),
                 "unified_diff": excerpt(self.files.path(prior["patch_file"])
                                         .read_bytes().decode("utf-8"), 1500)}
                for prior in data["executions"][:-1]
            ]
        return feedback

    def _sol(self):
        state = self.state
        if state["sol_attempts"] >= self.config.max_sol_attempts:
            raise _Stop("STOPPED_MAX_ATTEMPTS", "Sol attempt limit reached")
        if state["consecutive_failures"] >= self.config.max_consecutive_failures:
            raise _Stop("STOPPED_CONSECUTIVE_FAILURES", "consecutive unsuccessful round limit reached")
        if state["total_model_calls"] >= self.config.max_model_calls:
            raise _Stop("STOPPED_BUDGET_EXCEEDED", "model call budget exhausted")

        number = state["next_round"]
        state["current_round"] = number
        state["rounds"].setdefault(str(number), {"executions": []})
        step = f"round_{number}.sol_exec"
        thread = state["sol_thread_id"]
        before = snapshot(self.config.project_dir)
        prompt = (
            "Implement the project goal. Modify files only in the current project workspace. "
            "Do not access outside directories, the independent tests, or AgentBridge itself. "
            "The scheduler runs the independent tests. Report what changed in your final message.\n\n"
            f"Goal:\n{self.config.goal}\n\nNeon plan:\n{state['plan']['plan']}"
        )
        if number > 1:
            prior = state["rounds"][str(number - 1)]
            prompt += "\n\nPrevious round feedback:\n" + json_text(self._round_feedback(prior, 16000))
        if self.round["executions"]:
            prompt += "\n\nPrior attempt in this round:\n"
            prompt += json_text(self._round_feedback(self.round, 16000))
        result, filename, audit, budget_limited = self._invoke(
            step, "sol",
            lambda _timeout, _cache: sol_command(DEFAULT_CODEX, self.config.project_dir,
                                                   self.config.sol_effort, thread),
            prompt=prompt, stdin=prompt.encode("utf-8"),
        )
        after = snapshot(self.config.project_dir)
        changes, diff = changes_and_patch(before, after)
        patch = f"round_{number}_attempt_{state['sol_attempts']}_diff.patch"
        self.files.write(patch, diff)
        self.files.write(f"round_{number}_diff.patch", diff)
        envelope, reported_thread = parse_sol(result.stdout)
        execution = {"audit_file": filename, "patch_file": patch, "changes": changes,
                     "agent_message": envelope["agent_message"] if isinstance(
                         envelope["agent_message"], str) else None}
        self.round["executions"].append(execution)
        self.round.update(changes=changes, patch_file=f"round_{number}_diff.patch",
                          project_manifest=manifest(after))
        self._record_usage("sol", envelope)
        self._write_audit(filename, {**audit, "envelope": envelope, "execution": execution})
        if result.utf8_ok and not thread and reported_thread:
            state["sol_thread_id"] = reported_thread
        self._guard_acceptance()
        self._total_timeout(result, budget_limited)
        state["pending_step"] = None
        if problem := process_problem(result):
            code = "FAILED" if result.exit_code == 0 and not result.utf8_ok else "STOPPED_MODEL_UNAVAILABLE"
            raise _Stop(code, f"{step}: {problem}")
        validate_sol(envelope, reported_thread, thread)
        self._complete(step, "ROUND_TEST", "sol_exec")

    def _test(self):
        step = f"round_{self.state['current_round']}.test"
        self.round["test"] = self._accept(step)
        self._complete(step, "ROUND_NEON_REVIEW", "round_test")

    def _review(self):
        step = f"round_{self.state['current_round']}.neon_review"
        prompt = (
            "Review Sol's engineering round using only the provided evidence. Do not use tools. "
            "Return a JSON object with decision (PASS, RETRY or NEED_HUMAN), a nonempty reason "
            "and a nonempty feedback string with actionable guidance. A failing independent "
            "test cannot be accepted.\n\n"
            + bounded_json(self._round_feedback(self.round, 6500), 16000)
        )
        self.round["review"] = self._neon(step, prompt, validate_review)
        self._complete(step, "ROUND_DECIDE", "neon_review")

    def _decide(self):
        state, data = self.state, self.round
        number = state["current_round"]
        decision = data["review"]["decision"]
        state["model_decision"] = decision
        passed = data["test"]["test_passed"]
        state["consecutive_failures"] = 0 if passed else state["consecutive_failures"] + 1
        step = f"round_{number}.decide"
        if decision == "PASS" and passed and state["tamper_ok"]:
            state["completed_steps"].append(step)
            self._stop("COMPLETED_PASS", "independent acceptance and Neon review passed")
            return
        if decision == "PASS":
            state["override_reason"] = data["test"]["reason"] or "independent integrity checks failed"
        if decision != "RETRY":
            state["completed_steps"].append(step)
            self._stop("STOPPED_NEED_HUMAN", state["override_reason"] or data["review"]["reason"])
            return
        state["next_round"] = number + 1
        self._complete(step, "ROUND_SOL_EXEC")


def start(config: IterationConfig, *, pause_after=None, runner=None, clock=monotonic) -> dict:
    reject_nested()
    _check_pause_stage(pause_after)
    checked_path(config.project_dir, "directory")
    checked_path(config.tests_dir, "directory")
    checked_path(config.output_dir, "directory", missing_ok=True).mkdir(parents=True, exist_ok=True)
    directory = config.output_dir / uuid4().hex
    directory.mkdir()
    files = RunFiles(directory)
    with files.lock():
        state = {
            "version": 1, "run_id": directory.name, "run_dir": str(directory),
            "config": config.persisted(), "current_state": "INIT", "current_round": 0,
            "next_round": 1, "resume_state": None, "completed_steps": [], "pending_step": None,
            "neon_conversation_id": None, "sol_thread_id": None, "model_decision": None,
            "sol_attempts": 0, "total_model_calls": 0, "total_process_calls": 0,
            "elapsed_seconds": 0.0, "consecutive_failures": 0, "stop_reason": None,
            "override_reason": None, "resumable": True, "rounds": {}, "test_status": None,
            "acceptance_manifest": None, "tamper_ok": None, "tampering_detected": False,
            "tokens": {role: dict.fromkeys(_TOKEN_FIELDS, 0)
                       for role in ("sol", "neon", "combined")},
        }
        files.record(state, "created", {})
        return _Iteration(config, files, state, pause_after, runner or run_process, clock).drive()


def _load(files: RunFiles) -> dict:
    state = files.read("state.json")
    if not isinstance(state, dict) or state.get("version") != 1:
        raise ValueError("unsupported iteration state")
    if state.get("run_id") != files.directory.name or state.get("run_dir") != str(files.directory):
        raise ValueError("iteration state does not match its run directory")
    return state


def _open_run(run_dir: Path) -> RunFiles:
    directory = workspace_path(run_dir)
    if not re.fullmatch(r"[0-9a-f]{32}", directory.name):
        raise ValueError("expected a generated iteration run directory")
    _output_folder(directory.parent)
    return RunFiles(directory)


def resume(run_dir: Path, *, pause_after=None, runner=None, clock=monotonic, **limits) -> dict:
    reject_nested()
    _check_pause_stage(pause_after)
    files = _open_run(run_dir)
    _load(files)
    with files.lock():
        state = _load(files)
        if not state["resumable"]:
            return state
        if set(limits) - set(_LIMIT_OPTIONS):
            raise ValueError("unsupported resume limit")
        config = IterationConfig(**{**state["config"], **limits})
        if files.directory.parent != config.output_dir:
            raise ValueError("iteration run directory does not match the recorded output directory")
        state["config"] = config.persisted()
        if state["current_state"] == "PAUSED":
            files.path("PAUSE").unlink(missing_ok=True)
        return _Iteration(config, files, state, pause_after, runner or run_process, clock).drive()


def pause(run_dir: Path) -> dict:
    reject_nested()
    files = _open_run(run_dir)
    state = _load(files)
    if state["current_state"] in _ACTIVE:
        files.path("PAUSE").touch(exist_ok=True)
        return {"run_dir": str(files.directory), "pause_requested": True}
    return {"run_dir": str(files.directory), "pause_requested": False,
            "current_state": state["current_state"]}


def status(run_dir: Path) -> dict:
    reject_nested()
    files = _open_run(run_dir)
    state = _load(files)
    keys = ("run_id", "run_dir", "current_state", "current_round", "completed_steps",
            "pending_step", "neon_conversation_id", "sol_thread_id", "sol_attempts",
            "total_model_calls", "elapsed_seconds", "consecutive_failures", "tokens", "test_status",
            "stop_reason", "override_reason", "resumable", "tampering_detected")
    return {key: state[key] for key in keys}
