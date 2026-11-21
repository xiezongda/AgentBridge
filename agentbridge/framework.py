"""Run a project through independent checks, model review, and durable checkpoints."""

import math
import os
import re
import subprocess
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from time import monotonic
from uuid import uuid4

from .bridge import DEFAULT_AGY, DEFAULT_CODEX
from .iteration import reject_nested
from .iteration_evidence import (
    NeonUnavailable, neon_command, parse_neon, process_problem, required_text, session_id,
    sol_command, validate_sol,
)
from .iteration_files import (
    IntegrityError, RunFiles, checked_authorized_path, digests, file_sha256, json_text, stream_entry,
    stream_manifest, tracked_equal, tracked_patch,
)
from .process import audit_excerpt, captured_process, run_process
from .project_spec import ASTRA_MODEL, RESUME_LIMITS, ProjectSpec, output_path
from .protocol import _load_json, parse_codex_jsonl, parse_model_object
from .verifiers import object_fields, run_verifiers

PAUSE_STAGES = ("baseline_verify", "neon_plan", "astra_contract", "sol_exec", "round_verify",
                "neon_review", "astra_consult")
ACTIVE = ("INIT", "BASELINE_VERIFY", "NEON_PLAN", "ASTRA_CONTRACT_REVIEW", "ROUND_SOL_EXEC",
          "ROUND_VERIFY", "ROUND_NEON_REVIEW", "ROUND_ASTRA_CONSULT", "ROUND_DECIDE",
          "ASTRA_SCIENTIFIC_ACCEPT")
LOCKED_STATES = set(ACTIVE[4:])
RESUMABLE = {"PAUSED", "STOPPED_MAX_ATTEMPTS", "STOPPED_BUDGET_EXCEEDED",
             "STOPPED_TIMEOUT_BUDGET", "STOPPED_CONSECUTIVE_FAILURES",
             "STOPPED_MODEL_UNAVAILABLE", "STOPPED_SCIENTIFIC_REVIEW", "STOPPED_NEED_HUMAN"}
NEON_DECISIONS = ("PASS", "RETRY", "REPLAN", "ESCALATE_ASTRA", "NEED_HUMAN")
ISSUE_CATEGORIES = ("none", "engineering", "numerical", "scientific", "resource_or_permission")
ASTRA_CONTRACT = ("APPROVE", "REVISE", "NEED_HUMAN")
ASTRA_CONSULT = ("CONTINUE_WITH_GUIDANCE", "REVISE_PLAN", "NEED_HUMAN")
ASTRA_ACCEPT = ("PASS", "SUPPLEMENT_VERIFICATION", "INSUFFICIENT_EVIDENCE", "NEED_HUMAN")
CLAIM_STATUSES = ("SUPPORTED", "UNSUPPORTED", "INSUFFICIENT_EVIDENCE")
TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens",
                "reasoning_output_tokens", "total_tokens")
VOLATILE_FILES = {"run.lock", "PAUSE", "state.json", "events.jsonl"}
SOL_BEFORE = ".sol-before"


def _cap(text, limit):
    marker = "\n[context truncated]\n"
    if len(text) <= limit:
        return text
    if limit <= len(marker):
        return text[:limit]
    head = (limit - len(marker)) * 3 // 4
    return text[:head] + marker + text[-(limit - len(marker) - head):]


def _audit_value(value, limit):
    excerpt = audit_excerpt(json_text(value), limit)
    return excerpt if isinstance(excerpt, dict) else value


def _report_summary(report, audit_file):
    results = []
    for result in report["results"]:
        summary = {key: value for key, value in result.items()
                   if key not in {"argv", "stdout", "stderr", "process", "generated_files"}}
        if not result["passed"]:
            log = result.get("stderr") or result.get("stdout") or ""
            summary["failure_excerpt"] = _cap(log["excerpt"] if isinstance(log, dict) else log, 800)
        results.append(summary)
    return {**report, "audit_file": audit_file, "results": results}


def astra_command(path, project, effort, thread):
    model = ["-m", ASTRA_MODEL, "-c", f"model_reasoning_effort={effort}"]
    scope = ["-s", "read-only", "-C", str(project)]
    tail = ["--skip-git-repo-check", "--json"]
    argv = [str(path), "exec"]
    if thread:
        return argv + scope + ["resume"] + model + tail + [thread, "-"]
    return argv + model + scope + tail + ["-"]


def _choice(payload, field, choices):
    value = required_text(payload, field)
    if value not in choices:
        raise ValueError(f"invalid {field}: {value}; expected one of {', '.join(choices)}")
    return value


def _neon_payload(payload, *, plan=False):
    _choice(payload, "decision", NEON_DECISIONS)
    _choice(payload, "issue_category", ISSUE_CATEGORIES)
    required_text(payload, "reason")
    required_text(payload, "feedback")
    if plan and payload["decision"] != "NEED_HUMAN":
        required_text(payload, "plan")
    if "plan_adjustment" in payload:
        required_text(payload, "plan_adjustment")
    if "alternatives" in payload and not isinstance(payload["alternatives"], list):
        raise ValueError("alternatives must be a list")


def _astra_payload(payload, choices):
    _choice(payload, "decision", choices)
    required_text(payload, "reason")
    if "guidance" in payload:
        required_text(payload, "guidance")


def _claim_payload(payload, claims):
    _astra_payload(payload, ASTRA_ACCEPT)
    verdicts = payload.get("claim_verdicts")
    if not isinstance(verdicts, list):
        raise ValueError("claim_verdicts must be a list")
    seen = set()
    for verdict in verdicts:
        if not isinstance(verdict, dict):
            raise ValueError("claim verdict must be an object")
        identifier = required_text(verdict, "claim_id")
        if identifier in seen:
            raise ValueError(f"duplicate claim verdict: {identifier}")
        seen.add(identifier)
        _choice(verdict, "status", CLAIM_STATUSES)
        required_text(verdict, "evidence_summary")
        required_text(verdict, "scope_note")
    if seen != {claim["claim_id"] for claim in claims}:
        raise ValueError("claim_verdicts must cover exactly the locked claims")


class _Stop(Exception):
    def __init__(self, state, reason):
        self.state, self.reason = state, reason


class _Framework:
    def __init__(self, spec, files, state, pause_after, runner, clock):
        self.spec, self.files, self.state = spec, files, state
        self.pause_after, self.runner, self.clock = pause_after, runner, clock
        self.previous_elapsed, self.started = state["elapsed_seconds"], clock()

    @property
    def elapsed(self):
        return self.previous_elapsed + (self.clock() - self.started)

    @property
    def round(self):
        return self.state["rounds"][str(self.state["current_round"])]

    def save(self, event, **details):
        self.state["elapsed_seconds"] = round(self.elapsed, 6)
        self.files.record(self.state, event, details)

    def move(self, name, event, **details):
        self.state["current_state"] = name
        self.save(event, **details)

    def stop(self, name, reason, *, resume_state=None):
        state = self.state
        target = resume_state or (state["current_state"] if state["current_state"] in ACTIVE
                                  else state["resume_state"])
        state.update(resume_state=target,
                     resumable=name in RESUMABLE, stop_reason=reason)
        self.move(name, "stopped", reason=reason, override_reason=state["override_reason"])

    def remaining(self):
        remaining = self.spec.limits["max_total_seconds"] - self.elapsed
        if remaining <= 0:
            raise _Stop("STOPPED_TIMEOUT_BUDGET", "total active wall-clock budget exhausted")
        return remaining

    def guard(self, *, check_project=True, interrupted=False, new_artifacts=None):
        self.spec.guard_verification()
        self.spec.guard_sources()
        self.spec.guard_readonly()
        state = self.state
        actual = self.spec.contract_hash(self.spec.verification_manifest)
        locked = state.get("locked_contract_hash")
        locked_stage = state["current_state"] in LOCKED_STATES or state["resume_state"] in LOCKED_STATES
        if (actual != state.get("checkpoint_contract_hash", state["contract_hash"])
                or (locked is not None and state["contract_hash"] != locked)
                or (locked_stage and locked is None)):
            raise IntegrityError("the verification contract hash changed or its lock is missing")
        expected = state["project_manifest"]
        if check_project and expected is not None and not tracked_equal(
                expected, self.spec.snapshot_project()[0]):
            raise IntegrityError("project changed outside a Sol execution")
        skip = VOLATILE_FILES | {SOL_BEFORE}
        if new_artifacts:
            skip.add(f"artifacts/{new_artifacts}")
        index = state["audit_index"]
        if interrupted:
            pending = state["pending_step"]
            if pending["audit_file"] not in index:
                skip.add(pending["audit_file"])
            if "_sol_exec" in pending["step"] and self.patch_filename() not in index:
                skip.add(self.patch_filename())
            if not pending["audit_file"].startswith("model_"):
                skip.add(f"artifacts/{pending.get('artifacts_step', pending['step'])}")
        actual = stream_manifest(self.files.directory, previous=index, skip=skip)
        expected_index = {name: entry for name, entry in index.items()
                          if not any(name == part or name.startswith(part + "/") for part in skip)}
        if digests(actual) != digests(expected_index):
            raise IntegrityError("sealed audit or verification evidence changed")
        index.update(actual)

    def seal(self, name):
        self.state["audit_index"][name] = stream_entry(self.files.path(name))

    def audit_manifest(self, *, new_artifacts=None, previous=None):
        skip = {"run.lock", "PAUSE"}
        if new_artifacts:
            skip.add(f"artifacts/{new_artifacts}")
        cached = dict(self.state["audit_index"] if previous is None else previous)
        for name in ("state.json", "events.jsonl"):
            cached.pop(name, None)
        pending = self.state["pending_step"]
        if previous is None and pending and pending.get("before_spool"):
            cached[SOL_BEFORE] = pending["before_spool"]
        return stream_manifest(self.files.directory, previous=cached, skip=skip)

    def guard_audit(self, expected, *, new_artifacts=None):
        if digests(expected) != digests(self.audit_manifest(new_artifacts=new_artifacts, previous=expected)):
            raise IntegrityError("a subprocess changed the run's existing audit or evidence files")

    def pending(self, step, audit_file, **details):
        if self.state["pending_step"] or step in self.state["completed_steps"]:
            raise IntegrityError(f"attempted to replay a pending or completed step: {step}")
        self.state["pending_step"] = {"step": step, "audit_file": audit_file, **details}
        self.save("step_started", step=step, audit_file=audit_file)

    def checkpoint_step(self, step):
        if step in self.state["completed_steps"]:
            raise IntegrityError(f"attempted to replay completed step: {step}")
        self.state["pending_step"] = None
        self.state["completed_steps"].append(step)

    def complete(self, step, next_state, stage=None):
        self.checkpoint_step(step)
        self.move(next_state, "step_completed", step=step,
                  locked_contract_hash=self.state["locked_contract_hash"])
        if stage == "sol_exec":
            self.files.path(SOL_BEFORE).unlink(missing_ok=True)
        if next_state in ACTIVE and stage and (
                stage == self.pause_after or self.files.path("PAUSE").is_file()):
            self.pause(f"pause after {stage}")

    def pause(self, reason):
        self.stop("PAUSED", reason)
        self.files.path("PAUSE").unlink(missing_ok=True)

    def interrupted(self):
        if self.state["current_state"] == "STOPPED_NEED_HUMAN":
            return
        pending = self.state["pending_step"]["step"]
        self.stop("STOPPED_NEED_HUMAN", f"interrupted during {pending}; the external step may have run")

    def drive(self):
        handlers = {
            "INIT": self.initialize, "BASELINE_VERIFY": self.baseline, "NEON_PLAN": self.plan,
            "ASTRA_CONTRACT_REVIEW": self.contract_review, "ROUND_SOL_EXEC": self.sol,
            "ROUND_VERIFY": self.verify, "ROUND_NEON_REVIEW": self.review,
            "ROUND_ASTRA_CONSULT": self.consult, "ROUND_DECIDE": self.decide,
            "ASTRA_SCIENTIFIC_ACCEPT": self.scientific_accept,
        }
        try:
            if self.state["pending_step"]:
                self.interrupted()
                return self.state
            if self.state["current_state"] not in ACTIVE:
                target = self.state["resume_state"]
                if target not in ACTIVE:
                    raise IntegrityError("invalid resume checkpoint")
                self.state.update(stop_reason=None, resume_state=None, resumable=True)
                self.move(target, "resumed")
            while self.state["current_state"] in ACTIVE:
                self.guard()
                if self.files.path("PAUSE").is_file():
                    self.pause("pause marker")
                    break
                self.remaining()
                self.save("enter_state")
                handlers[self.state["current_state"]]()
        except (IntegrityError, PermissionError) as exc:
            self.state.update(tamper_ok=False, override_reason=str(exc))
            self.stop("STOPPED_PERMISSION_OR_TAMPER", str(exc))
        except _Stop as exc:
            self.stop(exc.state, exc.reason)
        except KeyboardInterrupt:
            if self.state["pending_step"]:
                self.interrupted()
            else:
                self.pause("interrupted between steps")
        except (OSError, ValueError, RecursionError) as exc:
            self.stop("FAILED", str(exc))
        if not self.state["pending_step"]:
            self.files.path(SOL_BEFORE).unlink(missing_ok=True)
        return self.state

    def initialize(self):
        self.state["project_manifest"] = self.spec.snapshot_project()[0]
        self.complete("init", "BASELINE_VERIFY")

    def verify_step(self, step):
        self.remaining()
        attempt = self.state["verification_retries"].get(step, step)
        audit_file = f"{attempt}.json"
        self.pending(step, audit_file, artifacts_step=attempt)
        trusted = self.audit_manifest(new_artifacts=attempt)
        budget = self.spec.limits["max_total_seconds"] - self.elapsed
        try:
            report = run_verifiers(self.spec, self.files, attempt, self.runner, budget, clock=self.clock)
        finally:
            self.guard_audit(trusted, new_artifacts=attempt)
            self.guard(new_artifacts=attempt)
        data = report.to_dict()
        durations = [result["process"]["elapsed_seconds"] for result in data["results"] if "process" in result]
        if durations:
            self.state["last_subprocess_seconds"] = durations[-1]
        serialized = json_text(data) + "\n"
        self.files.write(audit_file, serialized)
        self.seal(audit_file)
        artifacts = stream_manifest(Path(data["artifacts_dir"]))
        self.state["audit_index"].update({f"artifacts/{attempt}/{name}": entry
                                          for name, entry in artifacts.items()})
        self.state["verification_evidence"][attempt] = {
            "report": self.state["audit_index"][audit_file]["sha256"], "artifacts": digests(artifacts)}
        data = _report_summary(data, audit_file)
        self.state["verification"] = data
        return data

    def baseline(self):
        self.state["baseline"] = self.verify_step("baseline_verify")
        self.complete("baseline_verify", "NEON_PLAN", "baseline_verify")

    def model(self, role, step, prompt, *, validate=None):
        state, spec = self.state, self.spec
        if state["total_model_calls"] >= spec.limits["max_model_calls"]:
            raise _Stop("STOPPED_BUDGET_EXCEEDED", "model call budget exhausted")
        if role == "astra" and state["astra_calls"] >= spec.limits["max_astra_calls"]:
            raise _Stop("STOPPED_BUDGET_EXCEEDED", "Astra call budget exhausted")
        session_key = {"neon": "neon_conversation_id", "sol": "sol_thread_id",
                       "astra": "astra_thread_id"}[role]
        session = state[session_key]
        number = state["total_model_calls"] + 1
        budget = self.remaining()
        limit = spec.file_policy["max_context_chars"]
        if role == "neon":
            # The prompt is an argv element on Windows. Reserve room for quoting and UTF-16.
            limit = min(limit, 32700)
        note = next((item for item in reversed(state["human_decisions"])
                     if item["note"] or item["action"] == "guidance"), None)
        if note:
            guidance = "Latest human guidance for this run: " + json_text(
                {key: note[key] for key in ("note", "reason")})
            prompt = _cap(guidance, max(1, limit // 4)) + "\n\n" + prompt
        prompt = _cap(prompt, limit)
        argv = (neon_command(DEFAULT_AGY, min(spec.limits["timeout_s"], math.ceil(budget)),
                             prompt, session) if role == "neon" else
                (sol_command if role == "sol" else astra_command)(
                    DEFAULT_CODEX, spec.project_dir, spec.agents[f"{role}_effort"], session))
        if role == "neon":
            if len(subprocess.list2cmdline(argv).encode("utf-16-le")) // 2 >= 32767:
                base = neon_command(DEFAULT_AGY, min(spec.limits["timeout_s"], math.ceil(budget)), "", session)
                overhead = len(subprocess.list2cmdline(base).encode("utf-16-le")) // 2
                prompt = _cap(prompt, (32760 - overhead) // 2)
                argv = neon_command(DEFAULT_AGY, min(spec.limits["timeout_s"], math.ceil(budget)), prompt, session)
                if len(subprocess.list2cmdline(argv).encode("utf-16-le")) // 2 >= 32767:
                    raise ValueError("Neon prompt exceeds the Windows command-line limit")
        elif any(not arg.isascii() for arg in argv):
            raise ValueError("Codex expects ASCII arguments and UTF-8 stdin")
        stdin = None if role == "neon" else prompt.encode("utf-8")
        filename = f"model_{number:03d}_{role}.json"
        pending_details = self.prepare_sol() if role == "sol" else {}
        state["total_model_calls"] = number
        state["astra_calls"] += int(role == "astra")
        state["sol_attempts"] += int(role == "sol")
        self.pending(step, filename, **pending_details)
        trusted = self.audit_manifest()
        budget = spec.limits["max_total_seconds"] - self.elapsed
        if budget <= 0:
            state["total_model_calls"] -= 1
            state["astra_calls"] -= int(role == "astra")
            state["sol_attempts"] -= int(role == "sol")
            state["pending_step"] = None
            if role == "sol":
                self.files.path(SOL_BEFORE).unlink(missing_ok=True)
            self.remaining()
        env = dict(os.environ, AGENTBRIDGE_IN_SUBPROCESS="1", AGENTBRIDGE_RUN_ID=state["run_id"],
                   PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        with captured_process(self.runner, argv, cwd=spec.project_dir,
                              timeout_s=min(spec.limits["timeout_s"], budget),
                              stdin=stdin, env=env) as result:
            self.guard_audit(trusted)
            state["last_subprocess_seconds"] = result.elapsed_seconds
            audit_limit = spec.file_policy["max_audit_stream_bytes"]
            audit = {"step": step, "role": role, "argv": argv, "prompt": prompt,
                     **result.audit(audit_limit)}
            try:
                self.guard(check_project=role != "sol")
                if result.timed_out and budget <= spec.limits["timeout_s"]:
                    raise _Stop("STOPPED_TIMEOUT_BUDGET", f"{step}: remaining wall-clock budget exhausted")
                if problem := process_problem(result):
                    status = "FAILED" if not result.utf8_ok and result.exit_code == 0 else "STOPPED_MODEL_UNAVAILABLE"
                    raise _Stop(status, f"{step}: {problem}")
                if role == "neon":
                    envelope, payload = parse_neon(result.neon_output(), session)
                    reported = envelope["conversation_id"]
                else:
                    envelope = parse_codex_jsonl(result.lines("stdout", protocol=True))
                    audit["envelope"] = _audit_value(envelope, audit_limit)
                    if envelope["failure_events"]:
                        raise NeonUnavailable(f"{role} reported a failed turn")
                    reported = session_id(envelope["thread_id"])
                    validate_sol(envelope, reported, session)
                    payload = (parse_model_object(envelope["agent_message"]) if role == "astra" else
                               {"agent_message": envelope["agent_message"]})
                audit.update(envelope=_audit_value(envelope, audit_limit),
                             payload=_audit_value(payload, audit_limit))
                state[session_key] = reported
                self.record_usage(role, envelope.get("usage"))
                if validate:
                    validate(payload)
            except IntegrityError:
                raise
            except _Stop:
                self.finish_failed_model(role)
                raise
            except (ValueError, RecursionError) as exc:
                audit["protocol_error"] = _cap(str(exc), 2000)
                self.finish_failed_model(role)
                status = "STOPPED_MODEL_UNAVAILABLE" if isinstance(exc, NeonUnavailable) else "FAILED"
                raise _Stop(status, f"{step}: {audit['protocol_error']}") from exc
            finally:
                self.files.write(filename, json_text(audit) + "\n")
                self.seal(filename)
                state["latest_model_audit"] = filename
        if role == "sol":
            self.capture_sol()
        summary = self.model_summary(role, step, payload)
        state["latest_model_result"] = {"role": role, "step": step,
                                        **{key: summary[key] for key in ("decision",) if key in summary}}
        message = summary.get("reason") or summary.get("agent_message")
        if message:
            state["latest_model_result"]["summary"] = _cap(message, 500)
        return summary

    def finish_failed_model(self, role):
        if role == "sol":
            self.capture_sol()
        self.state["pending_step"] = None

    def model_summary(self, role, step, payload):
        limit = self.spec.file_policy["max_context_chars"]
        fields = {"neon": {"plan", "decision", "issue_category", "reason", "feedback",
                           "plan_adjustment", "alternatives"},
                  "astra": {"decision", "reason", "guidance"},
                  "sol": {"agent_message"}}[role]
        if role == "astra" and "_astra_accept" in step:
            fields.add("claim_verdicts")
        result = {}
        for key in fields & payload.keys():
            value = payload[key]
            if key in {"decision", "issue_category"}:
                result[key] = value
            elif isinstance(value, str):
                result[key] = _cap(value, limit)
            elif key == "claim_verdicts":
                result[key] = [{**{field: verdict[field] for field in ("claim_id", "status")},
                                **{field: _cap(verdict[field], limit) for field in
                                   ("evidence_summary", "scope_note")}} for verdict in value]
            elif key == "alternatives":
                result[key] = _audit_value(value, limit)
        return result

    def record_usage(self, role, usage):
        if isinstance(usage, dict):
            counts = {key: usage[key] for key in TOKEN_FIELDS
                      if type(usage.get(key)) is int and usage[key] >= 0}
            if "input_tokens" in counts and "output_tokens" in counts:
                counts.setdefault("total_tokens", counts["input_tokens"] + counts["output_tokens"])
            for key, value in counts.items():
                self.state["tokens"][role][key] += value
                self.state["tokens"]["combined"][key] += value

    def contract_context(self):
        return {key: value for key, value in self.spec.to_dict().items()
                if key not in {"project_dir", "verification_dir", "output_dir", "limits", "file_policy"}}

    def neon_instruction(self):
        return (
            "Use the supplied evidence. Reply with one JSON object. "
            f"issue_category: {', '.join(ISSUE_CATEGORIES)}. decision: {', '.join(NEON_DECISIONS)}. "
            "Include reason and actionable feedback strings. Optional plan_adjustment is a string "
            "and alternatives is a list comparing benefits, risks, and costs. "
            "ESCALATE_ASTRA is for scientific conflicts. Independent checks must pass before acceptance. "
            "Keep the acceptance contract and scientific criteria fixed."
        )

    def plan(self):
        step = self.control_step("neon_plan")
        prompt = (
            "Plan the executor's work without tools. Include a plan string unless NEED_HUMAN. "
            + self.neon_instruction()
            + (" Astra review is enabled." if self.spec.agents["require_astra"] else " Astra is disabled.")
            + "\n\nProject contract:\n" + _cap(json_text(self.contract_context()), 7500)
            + "\n\nIndependent baseline:\n" + self.neon_report(self.state["baseline"], 5000)
            + "\n\nEarlier plan and scientific review:\n"
            + _cap(json_text({"plan": self.state["plan"], "review": self.state["astra_contract_review"]}), 3500)
        )
        self.state["plan"] = self.model("neon", step, prompt, validate=lambda p: _neon_payload(p, plan=True))
        if self.state["plan"]["decision"] == "NEED_HUMAN":
            self.checkpoint_step(step)
            self.stop("STOPPED_NEED_HUMAN", self.state["plan"]["reason"])
            return
        if self.spec.agents["require_astra"]:
            self.complete(step, "ASTRA_CONTRACT_REVIEW", "neon_plan")
        else:
            self.lock_contract()
            self.complete(step, "ROUND_SOL_EXEC", "neon_plan")

    def lock_contract(self):
        self.guard()
        self.state["locked_contract_hash"] = self.state["contract_hash"]

    def revise_plan(self, step, adjustment=None):
        if self.state["plan_revisions"] >= self.spec.limits["max_plan_revisions"]:
            self.state["blocked_plan_revision"] = {"stage": self.state["current_state"],
                                                   "adjustment": adjustment}
            self.checkpoint_step(step)
            raise _Stop("STOPPED_BUDGET_EXCEEDED", "work plan revision limit reached")
        self.state["plan_revisions"] += 1
        if adjustment:
            self.state["plan"]["plan"] += "\n\nLatest work plan adjustment:\n" + adjustment

    def contract_review(self):
        step = self.control_step("astra_contract")
        prompt = (
            "Review the scientific contract in read-only mode. Assess the question, physical assumptions, "
            "units, tolerances and controls. Return JSON with decision APPROVE, REVISE or NEED_HUMAN, "
            "reason, and optional guidance. REVISE sends guidance for the work plan. "
            "If the locked checks themselves would require changes, use NEED_HUMAN.\n\n"
            + json_text({"contract_hash": self.state["contract_hash"], "contract": self.contract_context(),
                         "plan": self.state["plan"], "baseline": self.state["baseline"]})
        )
        review = self.model("astra", step, prompt, validate=lambda p: _astra_payload(p, ASTRA_CONTRACT))
        self.state["astra_contract_review"] = review
        if review["decision"] == "REVISE":
            self.revise_plan(step)
            self.complete(step, "NEON_PLAN", "astra_contract")
        elif review["decision"] == "APPROVE":
            self.lock_contract()
            self.complete(step, "ROUND_SOL_EXEC", "astra_contract")
        else:
            self.checkpoint_step(step)
            self.stop("STOPPED_NEED_HUMAN", review["reason"])

    def round_step(self, stage):
        index = self.round.get("review_cycle", 1)
        suffix = f"_review_{index}" if index > 1 else ""
        return f"round_{self.state['current_round']}_{stage}{suffix}"

    def control_step(self, stage):
        step = f"{stage}_{self.state['plan_revisions']}"
        serial = self.state["human_cycles"].get(stage, 0)
        return f"{step}_human_{serial}" if serial else step

    def sol(self):
        state = self.state
        if state["sol_attempts"] >= self.spec.limits["max_sol_attempts"]:
            raise _Stop("STOPPED_MAX_ATTEMPTS", "Sol attempt limit reached")
        if state["consecutive_failures"] >= self.spec.limits["max_consecutive_failures"]:
            raise _Stop("STOPPED_CONSECUTIVE_FAILURES", "consecutive unsuccessful verification limit reached")
        number = state["next_round"]
        state["current_round"] = number
        data = state["rounds"].setdefault(str(number), {"executions": []})
        step = self.round_step("sol_exec")
        prompt = (
            "Implement the goal inside the current project workspace. Keep the independent verifiers, "
            "contract, scientific criteria, readonly inputs, and audit files outside the project intact. "
            "Report your changes. The scheduler will run independent acceptance.\n\n"
            + json_text({"goal": self.spec.goal, "constraints": self.spec.constraints,
                         "plan": state["plan"]["plan"], "locked_contract_hash": state["locked_contract_hash"],
                         "readonly_inputs": [str(path) for path in self.spec.readonly_inputs],
                         "astra_contract_review": state["astra_contract_review"]})
        )
        if number > 1:
            prompt += "\n\nPrevious independent evidence and review:\n" + json_text(
                self.round_evidence(state["rounds"][str(number - 1)])
            )
        if data["executions"]:
            prompt += "\n\nEarlier executions in this round:\n" + json_text(self.round_evidence(data))

        data["sol"] = self.model("sol", step, prompt)
        self.complete(step, "ROUND_VERIFY", "sol_exec")

    def prepare_sol(self):
        path = self.files.path(SOL_BEFORE)
        with path.open("wb") as spool:
            before, locations = self.spec.snapshot_project(spool=spool)
            spool.flush()
            os.fsync(spool.fileno())
        if not tracked_equal(before, self.state["project_manifest"]):
            raise IntegrityError("project changed before Sol execution")
        self.state["project_manifest"] = before
        return {"before_text": locations, "before_spool": stream_entry(path)}

    def capture_sol(self, *, interrupted=False, require_changes=False):
        state = self.state
        pending = state["pending_step"]
        path = self.files.path(SOL_BEFORE)
        if file_sha256(path) != pending["before_spool"]["sha256"]:
            raise IntegrityError("pre-Sol snapshot changed")
        after = self.spec.snapshot_project()[0]
        with path.open("rb") as spool:
            changes, diff, truncated = tracked_patch(
                state["project_manifest"], after, self.spec.project_dir, spool,
                pending["before_text"], self.spec.file_policy["max_patch_bytes"])
        if require_changes and not changes["details"]:
            raise ValueError("reverify requires tracked Sol changes; use retry if none were written")
        filename = self.patch_filename()
        self.files.write(filename, diff)
        self.seal(filename)
        self.round["executions"].append({"changes": changes, "patch_file": filename,
                                        "patch_sha256": state["audit_index"][filename]["sha256"],
                                        "patch_truncated": truncated, "patch_bytes": len(diff.encode("utf-8")),
                                        "interrupted": interrupted})
        state["project_manifest"] = after

    def patch_filename(self):
        return f"round_{self.state['current_round']}_attempt_{self.state['sol_attempts']}_diff.patch"

    def verify(self):
        step = self.round_step("verify")
        self.round["verification"] = self.verify_step(step)
        self.complete(step, "ROUND_NEON_REVIEW", "round_verify")

    def round_evidence(self, data):
        evidence = {key: data[key] for key in
                    ("verification", "review", "consult", "sol", "astra_acceptance") if key in data}
        if data["executions"]:
            execution = data["executions"][-1]
            filename = execution["patch_file"]
            limit = min(6000, self.spec.file_policy["max_context_chars"])
            with self.files.path(filename).open("rb") as stream:
                content = stream.read(limit * 4).decode("utf-8", errors="ignore")
            evidence["diffs"] = [{"patch_file": filename, "unified_diff": _cap(content, limit),
                                 "patch_truncated": execution.get("patch_truncated", False)}]
        return evidence

    @staticmethod
    def neon_report(report, limit):
        return _cap(json_text(report), limit)

    def review(self):
        step = self.round_step("neon_review")
        evidence = self.round_evidence(self.round)
        override = self.state["override_reason"]
        prompt = (
            "Review the executor round without tools. " + self.neon_instruction()
            + "\n\nIndependent verification:\n" + self.neon_report(evidence.pop("verification"), 6500)
            + "\n\nChanges:\n" + _cap(json_text(evidence), 6500)
            + "\n\nLocked contract and previous override:\n"
            + _cap(json_text({"hash": self.state["locked_contract_hash"], "previous_override": override,
                             "scientific_claims": self.spec.scientific_claims}), 1800)
        )
        review = self.model("neon", step, prompt, validate=_neon_payload)
        self.round["review"] = review
        escalate = (review["decision"] == "ESCALATE_ASTRA" and self.spec.agents["require_astra"]
                    and review["issue_category"] == "scientific")
        self.complete(step, "ROUND_ASTRA_CONSULT" if escalate else "ROUND_DECIDE", "neon_review")

    def consult(self):
        step = self.round_step("astra_consult")
        prompt = (
            "Diagnose the scientific conflict using the original independent checks and code diff. "
            "Keep the scientific contract fixed. Return JSON with decision CONTINUE_WITH_GUIDANCE, "
            "REVISE_PLAN or NEED_HUMAN, reason, and optional guidance.\n\n"
            + json_text({"contract": self.contract_context(), "evidence": self.round_evidence(self.round),
                         "locked_contract_hash": self.state["locked_contract_hash"]})
        )
        self.round["consult"] = self.model("astra", step, prompt,
                                           validate=lambda p: _astra_payload(p, ASTRA_CONSULT))
        self.complete(step, "ROUND_DECIDE", "astra_consult")

    def acceptance_veto(self):
        report = self.round["verification"]
        if not self.state["tamper_ok"] or not report["tamper_ok"]:
            return "independent integrity checks failed"
        if not report["all_passed"]:
            return "independent verification failed: " + ", ".join(report["failed_verifiers"])
        passed = {result["id"] for result in report["results"] if result["passed"]}
        if any(set(claim["required_verifiers"]) - passed for claim in self.spec.scientific_claims):
            return "scientific claim lacks a required passing verifier"
        return None

    def decide(self):
        step, data = self.round_step("decide"), self.round
        self.guard()
        decision = data["review"]["decision"]
        veto = self.acceptance_veto()
        self.state["consecutive_failures"] = (self.state["consecutive_failures"] + 1 if veto else 0)
        if decision == "PASS":
            if veto:
                self.state["override_reason"] = veto
                self.round["override_reason"] = veto
                self.state["latest_model_result"]["override_reason"] = veto
                self.checkpoint_step(step)
                self.stop("STOPPED_NEED_HUMAN", veto)
            elif self.spec.agents["require_astra"]:
                self.complete(step, "ASTRA_SCIENTIFIC_ACCEPT")
            else:
                self.pass_run(step)
            return
        reason = data["review"]["reason"]
        if decision == "ESCALATE_ASTRA":
            consult = data.get("consult")
            if consult is None or consult["decision"] == "NEED_HUMAN":
                self.checkpoint_step(step)
                self.stop("STOPPED_NEED_HUMAN", consult["reason"] if consult else reason)
                return
            if consult["decision"] == "REVISE_PLAN":
                self.revise_plan(step, consult.get("guidance", consult["reason"]))
        elif decision == "REPLAN":
            self.revise_plan(step, data["review"].get("plan_adjustment", data["review"]["feedback"]))
        elif decision == "NEED_HUMAN":
            self.checkpoint_step(step)
            self.stop("STOPPED_NEED_HUMAN", reason)
            return
        self.state["next_round"] = self.state["current_round"] + 1
        self.complete(step, "ROUND_SOL_EXEC")

    def scientific_accept(self):
        step = self.round_step("astra_accept")
        if veto := self.acceptance_veto():
            self.state["override_reason"] = veto
            self.stop("STOPPED_NEED_HUMAN", veto)
            return
        claims = self.spec.scientific_claims
        prompt = (
            "Independently assess every locked claim against the original deterministic report and code diff. "
            "Return JSON with reason, decision (PASS, SUPPLEMENT_VERIFICATION, INSUFFICIENT_EVIDENCE, "
            "or NEED_HUMAN) and claim_verdicts, exactly one per claim_id. Each verdict includes claim_id, "
            "status (SUPPORTED, UNSUPPORTED, or INSUFFICIENT_EVIDENCE), evidence_summary, and scope_note. "
            "Return PASS only when every claim is supported within its stated scope.\n\n"
            + json_text({"scientific_claims": claims, "constraints": self.spec.constraints,
                         "locked_contract_hash": self.state["locked_contract_hash"],
                         "evidence": self.round_evidence(self.round)})
        )
        review = self.model("astra", step, prompt, validate=lambda p: _claim_payload(p, claims))
        self.round["astra_acceptance"] = review
        if review["decision"] == "PASS" and all(v["status"] == "SUPPORTED" for v in review["claim_verdicts"]):
            self.pass_run(step)
        elif review["decision"] == "NEED_HUMAN":
            self.checkpoint_step(step)
            self.stop("STOPPED_NEED_HUMAN", review["reason"])
        else:
            if review["decision"] == "PASS":
                self.state["override_reason"] = "Astra PASS lacked support for every locked claim"
                self.round["override_reason"] = self.state["override_reason"]
                self.state["latest_model_result"]["override_reason"] = self.state["override_reason"]
            self.round["review_cycle"] = self.round.get("review_cycle", 1) + 1
            self.checkpoint_step(step)
            self.stop("STOPPED_SCIENTIFIC_REVIEW", self.state["override_reason"] or review["reason"],
                      resume_state="ROUND_VERIFY")

    def pass_run(self, step):
        self.guard()
        if veto := self.acceptance_veto():
            self.state["override_reason"] = veto
            self.stop("STOPPED_NEED_HUMAN", veto)
            return
        self.state.update(resumable=False, resume_state=None, override_reason=None,
                          stop_reason="independent verification and required reviews passed")
        self.complete(step, "COMPLETED_PASS")


def _pause_stage(stage):
    if stage is not None and stage not in PAUSE_STAGES:
        raise ValueError(f"invalid pause stage: {stage}")


def start(spec, *, pause_after=None, runner=None, clock=monotonic):
    return _start(spec, pause_after=pause_after, runner=runner, clock=clock)


def _start(spec, *, pause_after, runner, clock, revision=None):
    reject_nested()
    _pause_stage(pause_after)
    spec.guard_sources()
    spec.path(spec.output_dir, "directory", missing_ok=True).mkdir(parents=True, exist_ok=True)
    directory = spec.output_dir / uuid4().hex
    directory.mkdir()
    files = RunFiles(directory, allow_external=spec.allow_external_paths)
    state = {
        "version": 1, "framework": "agentbridge", "run_id": directory.name, "run_dir": str(directory),
        "config": spec.to_dict(), "output_dir": str(spec.output_dir), "source_hashes": spec.sources,
        "verification_manifest": spec.verification_manifest,
        "readonly_manifest": spec.readonly_manifest, "audit_index": {},
        "contract_hash": spec.contract_hash(spec.verification_manifest), "locked_contract_hash": None,
        "contract_version": 1, "parent_run_id": None, "human_decisions": [], "budget_history": [],
        "human_cycles": {}, "blocked_plan_revision": None, "verification_retries": {},
        "current_state": "INIT", "current_round": 0, "next_round": 1, "resume_state": None,
        "completed_steps": [], "pending_step": None, "resumable": True, "tamper_ok": True,
        "neon_conversation_id": None, "sol_thread_id": None, "astra_thread_id": None,
        "total_model_calls": 0, "astra_calls": 0, "sol_attempts": 0, "plan_revisions": 0,
        "elapsed_seconds": 0.0, "consecutive_failures": 0, "stop_reason": None, "override_reason": None,
        "baseline": None, "plan": None, "astra_contract_review": None, "project_manifest": None,
        "rounds": {}, "verification": None, "verification_evidence": {},
        "tokens": {role: dict.fromkeys(TOKEN_FIELDS, 0) for role in ("neon", "sol", "astra", "combined")},
    }
    if revision:
        state.update(revision)
    with files.lock():
        files.record(state, "created", {"contract_version": state["contract_version"],
                                         "parent_run_id": state["parent_run_id"]})
        if revision:
            files.record(state, "human_decision", state["human_decisions"][0])
        return _Framework(spec, files, state, pause_after, runner or run_process, clock).drive()


def _open_run(run_dir, *, include_state=False):
    directory = checked_authorized_path(Path(run_dir), "directory", allow_external=True)
    if not re.fullmatch(r"[0-9a-f]{32}", directory.name):
        raise ValueError("expected a generated AgentBridge run directory")
    output_path(directory.parent, allow_external=True)
    files = RunFiles(directory, allow_external=True)
    state = _load(files)
    return (files, state) if include_state else files


def _load(files):
    state = files.read("state.json")
    if (not isinstance(state, dict) or state.get("framework") != "agentbridge" or
            state.get("version") != 1 or state.get("run_id") != files.directory.name or
            state.get("run_dir") != str(files.directory)):
        raise ValueError("unsupported framework checkpoint or mismatched run directory")
    config = state.get("config")
    if not isinstance(config, dict) or not isinstance(config.get("output_dir"), str):
        raise ValueError("run checkpoint has no recorded output directory")
    expected = config["output_dir"]
    if state.get("output_dir", expected) != expected or output_path(
            Path(expected), allow_external=ProjectSpec._external(config)) != files.directory.parent:
        raise IntegrityError("run directory does not match the recorded output directory")
    return state


def _timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _decision(run_id, action, note, reason, affected_steps):
    return {"decision_id": uuid4().hex, "timestamp": _timestamp(), "action": action,
            "note": note, "reason": reason, "related_run_id": run_id,
            "affected_steps": affected_steps}


def _stop_integrity(files, state, exc):
    state.update(current_state="STOPPED_PERMISSION_OR_TAMPER", resumable=False,
                 tamper_ok=False, stop_reason=str(exc), override_reason=str(exc))
    files.record(state, "stopped", {"reason": str(exc), "override_reason": str(exc)})
    return state


def _upgrade_checkpoint(engine):
    """Validate the original digests before upgrading an unsealed phase-three checkpoint."""
    state, spec, files = engine.state, engine.spec, engine.files
    if state["pending_step"]:
        raise ValueError("a legacy checkpoint with a pending step lacks a sealed recovery snapshot")
    old_contract = dict(state["config"])
    old_contract["limits"] = {key: value for key, value in old_contract["limits"].items()
                              if key not in set(RESUME_LIMITS) - {"max_plan_revisions"}}
    old_hash = sha256(json_text({"spec": old_contract, "verification_manifest": state["verification_manifest"]})
                      .encode("utf-8")).hexdigest()
    if old_hash != state["contract_hash"]:
        raise IntegrityError("legacy contract hash changed")
    spec.verification_manifest = spec.guard_verification()
    audit = stream_manifest(files.directory, skip=VOLATILE_FILES | {SOL_BEFORE})
    for step, evidence in state["verification_evidence"].items():
        prefix = f"artifacts/{step}/"
        found = {name[len(prefix):]: entry["sha256"] for name, entry in audit.items()
                 if name.startswith(prefix) and not _legacy_bytecode(Path(name))}
        if evidence["artifacts"] != found or audit.get(f"{step}.json", {}).get("sha256") != evidence["report"]:
            raise IntegrityError(f"legacy verification evidence changed: {step}")
    for data in state["rounds"].values():
        for execution in data["executions"]:
            if audit.get(execution["patch_file"], {}).get("sha256") != execution["patch_sha256"]:
                raise IntegrityError("legacy code diff changed")
    project, _ = spec.snapshot_project()
    if state["project_manifest"] is not None:
        old_project = {name: entry["sha256"] for name, entry in stream_manifest(spec.project_dir).items()
                       if not _legacy_bytecode(Path(name))}
        if old_project != state["project_manifest"]:
            raise IntegrityError("legacy project changed outside a Sol execution")
    initial_limits = state["config"]["limits"]
    state.update(config=spec.to_dict(), output_dir=str(spec.output_dir), audit_index=audit,
                 verification_manifest=spec.verification_manifest,
                 readonly_manifest=spec.readonly_manifest, checkpoint_contract_hash=spec.contract_hash(
                     spec.verification_manifest), project_manifest=project, contract_version=1, parent_run_id=None,
                 human_decisions=[], budget_history=[], human_cycles={}, blocked_plan_revision=None,
                 verification_retries={}, initial_limits=initial_limits)
    for owner, field in [(state, "baseline"), (state, "verification"),
                         *((data, "verification") for data in state["rounds"].values())]:
        if report := owner.get(field):
            owner[field] = _report_summary(report, Path(report["artifacts_dir"]).name + ".json")


def _legacy_bytecode(path):
    return "__pycache__" in (part.casefold() for part in path.parts) or path.suffix.casefold() == ".pyc"


def _check_budget_history(state):
    current = state.get("initial_limits")
    if current is None:
        return
    for change in state["budget_history"]:
        if change["before"] != current:
            raise IntegrityError("the recorded budget history is inconsistent")
        current = change["after"]
    if current != state["config"]["limits"]:
        raise IntegrityError("the checkpoint budget differs from the authorized budget")


def _human_target(state):
    target = state["resume_state"]
    if state["current_state"] != "STOPPED_NEED_HUMAN":
        return target
    if target in {"NEON_PLAN", "ASTRA_CONTRACT_REVIEW"}:
        for key in ("neon_plan", "astra_contract"):
            state["human_cycles"][key] = state["human_cycles"].get(key, 0) + 1
        return "NEON_PLAN"
    if target == "ROUND_DECIDE":
        state["next_round"] = state["current_round"] + 1
        return "ROUND_SOL_EXEC"
    if target == "ASTRA_SCIENTIFIC_ACCEPT":
        current = state["rounds"][str(state["current_round"])]
        current["review_cycle"] = current.get("review_cycle", 1) + 1
        return "ROUND_VERIFY"
    return target


def _seal_interrupted(engine, pending, decision):
    files, spec = engine.files, engine.spec
    audit = files.path(pending["audit_file"])
    evidence = decision["interrupted_evidence"] = {}
    if audit.is_file():
        if audit.name not in engine.state["audit_index"]:
            engine.seal(audit.name)
        evidence["audit_file"] = str(audit)
    if audit.name.startswith("model_"):
        return
    step = pending["step"]
    relative = f"artifacts/{pending.get('artifacts_step', step)}"
    artifacts = spec.path(files.directory / relative, "directory", missing_ok=True)
    if artifacts.is_dir():
        engine.state["audit_index"].update({f"{relative}/{name}": entry
                                            for name, entry in stream_manifest(artifacts).items()})
        evidence["artifacts_dir"] = str(artifacts)
    engine.state["verification_retries"][step] = f"{step}_retry_{decision['decision_id']}"


def resume(run_dir, *, pause_after=None, runner=None, clock=monotonic, human_note=None,
           human_reason=None, decision_file=None, resolve_pending=None, budget_reason=None, **limits):
    reject_nested()
    _pause_stage(pause_after)
    if decision_file is not None:
        path = checked_authorized_path(Path(decision_file), "file", allow_external=True)
        decision = object_fields(_load_json(path.read_text(encoding="utf-8-sig")),
                                 {"note", "reason", "resolve_pending", "budget_reason", "limits"},
                                 "decision file")
        human_note = human_note if human_note is not None else decision.get("note")
        human_reason = human_reason if human_reason is not None else decision.get("reason")
        resolve_pending = resolve_pending if resolve_pending is not None else decision.get("resolve_pending")
        budget_reason = budget_reason if budget_reason is not None else decision.get("budget_reason")
        limits = {**object_fields(decision.get("limits", {}), set(RESUME_LIMITS), "decision budgets"), **limits}
    if limits.keys() - set(RESUME_LIMITS):
        raise ValueError("unsupported resume limit")
    for label, value in (("human note", human_note), ("human reason", human_reason),
                         ("budget reason", budget_reason)):
        if value is not None:
            required_text({label: value}, label)
    if resolve_pending is not None and resolve_pending not in ("retry", "reverify"):
        raise ValueError("resolve_pending must be retry or reverify")
    files = _open_run(run_dir)
    with files.lock():
        state = _load(files)
        pending = state["pending_step"]
        if resolve_pending and not pending:
            raise ValueError("--resolve-pending requires a pending step")
        sol_pending = pending and re.fullmatch(r"round_\d+_sol_exec(?:_review_\d+)?", pending["step"])
        if resolve_pending == "reverify" and not sol_pending:
            raise ValueError("reverify is available only for interrupted Sol execution")
        if not state["resumable"] and not (state.get("tamper_ok")
                                            and state["current_state"] == "STOPPED_NEED_HUMAN"):
            return state
        config = dict(state["config"])
        before = config["limits"]
        try:
            _check_budget_history(state)
        except IntegrityError as exc:
            return _stop_integrity(files, state, exc)
        after = ProjectSpec.validate_limits({**before, **limits})
        if before != after and not budget_reason:
            raise ValueError("budget changes require --budget-reason")
        config["limits"] = after
        try:
            spec = ProjectSpec(config, sources=state["source_hashes"],
                               verification_manifest=state["verification_manifest"],
                               readonly_manifest=state.get("readonly_manifest"))
        except (OSError, ValueError, RecursionError) as exc:
            return _stop_integrity(files, state, exc)
        engine = _Framework(spec, files, state, pause_after, runner or run_process, clock)
        legacy = "audit_index" not in state
        try:
            if legacy:
                _upgrade_checkpoint(engine)
            state.setdefault("verification_retries", {})
            engine.guard(check_project=not sol_pending, interrupted=bool(pending))
        except (OSError, IntegrityError) as exc:
            return _stop_integrity(files, state, exc)
        if pending and not resolve_pending and not (human_note or human_reason or limits):
            return engine.drive()
        needs_guidance = (state["current_state"] == "STOPPED_NEED_HUMAN" and not pending
                          and not (human_note or human_reason))
        if needs_guidance and before == after:
            return _load(files)

        target = state["resume_state"] if state["current_state"] not in ACTIVE else state["current_state"]
        affected = [pending["step"]] if pending else [target]
        if resolve_pending == "reverify":
            affected.append("ROUND_VERIFY")
        if resolve_pending and sol_pending:
            try:
                engine.capture_sol(interrupted=True, require_changes=resolve_pending == "reverify")
            except (OSError, IntegrityError) as exc:
                return _stop_integrity(files, state, exc)
        human = None
        was_paused = state["current_state"] == "PAUSED"
        if not needs_guidance and not pending and state["current_state"] == "STOPPED_NEED_HUMAN":
            state["resume_state"] = _human_target(state)
            affected = list(dict.fromkeys([*affected, state["resume_state"]]))
        if human_note or human_reason or resolve_pending or before != after:
            human = _decision(state["run_id"], resolve_pending or ("guidance" if human_note or human_reason
                                                                  else "budget_adjustment"),
                              human_note, human_reason or budget_reason, affected)
            if resolve_pending:
                try:
                    _seal_interrupted(engine, pending, human)
                except (OSError, IntegrityError) as exc:
                    return _stop_integrity(files, state, exc)
            state["human_decisions"].append(human)
        event = None
        if resolve_pending == "reverify":
            engine.checkpoint_step(pending["step"])
            state.update(current_state="ROUND_VERIFY", resume_state=None, stop_reason=None)
            event = "pending_reverified"
        elif resolve_pending == "retry":
            state.update(current_state=target, pending_step=None, resume_state=None, stop_reason=None)
            event = "pending_retry"
        state["config"] = spec.to_dict()
        if legacy:
            engine.save("checkpoint_upgraded")
        if before != after:
            change = {"decision_id": human["decision_id"], "timestamp": human["timestamp"],
                      "reason": budget_reason, "before": before, "after": after}
            state["budget_history"].append(change)
            engine.save("budget_adjusted", **change)
        if human:
            engine.save("human_decision", **human)
        if needs_guidance:
            return state
        if was_paused:
            files.path("PAUSE").unlink(missing_ok=True)
        if event:
            engine.save(event, step=pending["step"])
            if sol_pending:
                files.path(SOL_BEFORE).unlink(missing_ok=True)
            if resolve_pending == "reverify" and (pause_after == "sol_exec" or files.path("PAUSE").is_file()):
                engine.pause("pause after sol_exec")
                return state
        if blocked := state.get("blocked_plan_revision"):
            if state["plan_revisions"] >= after["max_plan_revisions"]:
                return state
            state["blocked_plan_revision"] = None
            state["plan_revisions"] += 1
            if blocked["stage"] == "ASTRA_CONTRACT_REVIEW":
                state["resume_state"] = "NEON_PLAN"
            else:
                if blocked["adjustment"]:
                    state["plan"]["plan"] += "\n\nLatest work plan adjustment:\n" + blocked["adjustment"]
                state["next_round"] = state["current_round"] + 1
                state["resume_state"] = "ROUND_SOL_EXEC"
        return engine.drive()


def pause(run_dir):
    reject_nested()
    files, state = _open_run(run_dir, include_state=True)
    requested = state["current_state"] in ACTIVE
    if requested:
        files.path("PAUSE").touch(exist_ok=True)
    return {"run_dir": str(files.directory), "pause_requested": requested,
            "current_state": state["current_state"]}


def _log_paths(state):
    directory = Path(state["run_dir"])
    verification = state.get("verification") or {}
    model = state.get("latest_model_audit")
    rounds = [number for number, data in state["rounds"].items() if data["executions"]]
    latest_diff = state["rounds"][max(rounds, key=int)]["executions"][-1] if rounds else None
    audit = verification.get("audit_file")
    if audit is None and verification.get("artifacts_dir"):
        audit = Path(verification["artifacts_dir"]).name + ".json"
    return {"run_dir": str(directory), "state_file": str(directory / "state.json"),
            "events_file": str(directory / "events.jsonl"),
            "latest_diff": str(directory / latest_diff["patch_file"]) if latest_diff else None,
            "latest_verification": str(directory / audit) if audit else None,
            "latest_model_audit": str(directory / model) if model else None}


def _latest_verification(state):
    report = state.get("verification")
    if report is None:
        return None
    return {key: report.get(key) for key in ("all_passed", "failed_verifiers", "audit_file", "tamper_ok")}


def revise(run_dir, config, *, reason, human_note=None, pause_after=None, runner=None, clock=monotonic):
    reject_nested()
    _pause_stage(pause_after)
    required_text({"revision reason": reason}, "revision reason")
    if human_note is not None:
        required_text({"human note": human_note}, "human note")
    files = _open_run(run_dir)
    with files.lock():
        old = _load(files)
        if old["current_state"] in ACTIVE and not old["pending_step"]:
            raise ValueError("pause the parent run before revising its contract")
        spec = config if isinstance(config, ProjectSpec) else ProjectSpec.from_file(config)
        if (spec.project_dir.is_relative_to(files.directory) or files.directory.is_relative_to(spec.project_dir)
                or spec.output_dir.is_relative_to(files.directory)):
            raise IntegrityError("a revised project or output location overlaps the parent run")
        new = spec.to_dict()
        diff = {}
        for key in ("goal", "constraints", "verifiers", "scientific_claims", "verification_dir", "agents"):
            if old["config"][key] != new[key]:
                diff[key] = {"before": old["config"][key], "after": new[key]}
        before = {name: entry if isinstance(entry, str) else entry["sha256"]
                  for name, entry in old["verification_manifest"].items()}
        after = digests(spec.verification_manifest)
        if before != after:
            diff["verification_manifest"] = {
                name: {"before": before.get(name), "after": after.get(name)}
                for name in sorted(before.keys() | after.keys()) if before.get(name) != after.get(name)
            }
        affected = ["BASELINE_VERIFY", "ROUND_VERIFY"]
        if spec.agents["require_astra"]:
            affected.extend(["ASTRA_CONTRACT_REVIEW", "ASTRA_SCIENTIFIC_ACCEPT"])
        revision = {
            "contract_version": old.get("contract_version", 1) + 1,
            "parent_run_id": old["run_id"], "parent_contract_hash": old["contract_hash"],
            "revision_reason": reason, "contract_diff": diff, "affected_steps": affected,
            "inherited_evidence": {"parent_run_id": old["run_id"], "state": old["current_state"],
                                   "stop_reason": old["stop_reason"], "override_reason": old["override_reason"],
                                   "verification": _latest_verification(old), "logs": _log_paths(old)},
            "human_decisions": [_decision(old["run_id"], "revise", human_note, reason, affected)],
        }
    return _start(spec, pause_after=pause_after, runner=runner, clock=clock, revision=revision)


STAGE_STATUS = {
    "INIT": ("verifier", "Initialize the run and snapshot the project."),
    "BASELINE_VERIFY": ("verifier", "Run independent baseline verification."),
    "NEON_PLAN": ("neon", "Request the work plan."),
    "ASTRA_CONTRACT_REVIEW": ("astra", "Review the scientific contract before it is locked."),
    "ROUND_SOL_EXEC": ("sol", "Run the next implementation attempt."),
    "ROUND_VERIFY": ("verifier", "Run independent verification on the current project."),
    "ROUND_NEON_REVIEW": ("neon", "Review the latest independent results and diff."),
    "ROUND_ASTRA_CONSULT": ("astra", "Assess the scientific conflict."),
    "ROUND_DECIDE": ("verifier", "Apply the independent acceptance gate and select the next step."),
    "ASTRA_SCIENTIFIC_ACCEPT": ("astra", "Check the evidence for each locked scientific claim."),
}
BUDGET_COUNTERS = {
    "max_sol_attempts": "sol_attempts", "max_model_calls": "total_model_calls",
    "max_astra_calls": "astra_calls", "max_plan_revisions": "plan_revisions",
    "max_total_seconds": "elapsed_seconds", "max_consecutive_failures": "consecutive_failures",
}


def status(run_dir, *, full=False):
    reject_nested()
    _, state = _open_run(run_dir, include_state=True)
    if full:
        return state
    current, pending = state["current_state"], state["pending_step"]
    target = current if current in ACTIVE else state["resume_state"]
    role, description = STAGE_STATUS.get(target, ("none", "The run has ended."))
    config = state["config"]
    logs = _log_paths(state)
    command = "python run_agentbridge.py resume --run-dir '" + state["run_dir"].replace("'", "''") + "'"
    next_step = {"description": description, "command": command if current not in ACTIVE else None}
    checks = []
    if pending and state.get("tamper_ok", True):
        role = "human"
        sol = "_sol_exec" in pending["step"]
        audit = Path(state["run_dir"]) / pending["audit_file"]
        checks = [f"Confirm the subprocess for {pending['step']} has exited.",
                  f"Inspect {audit} if it exists, and review {logs['events_file']}.",
                  f"Check for partial changes under {config['project_dir']}."]
        commands = [command + " --resolve-pending retry"]
        if sol:
            checks.append("Use reverify if Sol wrote the intended changes. Independent checks still run.")
            commands.append(command + " --resolve-pending reverify")
        next_step = {"description": "Review the interrupted step and choose a recovery action.",
                     "command": commands[0], "commands": commands}
    elif not state["resumable"]:
        role = "none"
        next_step = {"description": state["stop_reason"] or "The run has ended.", "command": None}
    elif current == "STOPPED_NEED_HUMAN":
        role = "human"
        next_step = {"description": "Add guidance to continue with the existing model sessions.",
                     "command": command + " --human-note '<guidance>'"}
    elif "BUDGET" in current or current in {"STOPPED_MAX_ATTEMPTS", "STOPPED_CONSECUTIVE_FAILURES"}:
        role = "human"
        reason = state["stop_reason"] or ""
        key = ("max_plan_revisions" if "plan revision" in reason else
               "max_astra_calls" if "Astra" in reason else
               {"STOPPED_MAX_ATTEMPTS": "max_sol_attempts",
                "STOPPED_CONSECUTIVE_FAILURES": "max_consecutive_failures",
                "STOPPED_TIMEOUT_BUDGET": "max_total_seconds"}.get(current, "max_model_calls"))
        next_step = {"description": reason, "command": command + " --" + key.replace("_", "-")
                     + " <new-limit> --budget-reason '<reason>'"}
    elif current == "STOPPED_SCIENTIFIC_REVIEW":
        role = "human"
        next_step["description"] = "Review the scientific findings and add guidance before reverification."
        next_step["command"] += " --human-note '<guidance>'"
    elif current == "STOPPED_MODEL_UNAVAILABLE":
        role = "human"
        next_step["description"] = "Restore access to the model service, then resume this run."
    summary = {key: state[key] for key in ("run_id", "run_dir", "current_state", "current_round",
                                           "pending_step", "resumable", "stop_reason", "override_reason")}
    if pending:
        summary["pending_step"] = {key: pending[key] for key in ("step", "audit_file")}
        summary["pending_check_items"] = checks
    model = state.get("latest_model_result")
    budgets = {limit: {"used": state[counter], "limit": config["limits"][limit]}
               for limit, counter in BUDGET_COUNTERS.items()}
    budgets["timeout_s"] = {"used": state.get("last_subprocess_seconds"),
                            "limit": config["limits"]["timeout_s"]}
    return {**summary, "project_id": config["project_id"], "goal": config["goal"],
            "contract_version": state.get("contract_version", 1), "parent_run_id": state.get("parent_run_id"),
            "active_role": role, "next_step": next_step,
            "latest_result": {"verification": _latest_verification(state),
                              "model": ({**model, "overridden": bool(model.get("override_reason"))}
                                        if model else None)},
            "budgets": budgets,
            "sessions": {key: state[key] for key in ("neon_conversation_id", "sol_thread_id", "astra_thread_id")},
            "logs": logs, "human_decisions": state.get("human_decisions", []),
            "budget_history": state.get("budget_history", [])}
