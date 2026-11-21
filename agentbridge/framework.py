"""Run a project through independent checks, model review, and durable checkpoints."""

import math
import os
import re
import subprocess
from pathlib import Path
from time import monotonic
from uuid import uuid4

from .bridge import DEFAULT_AGY, DEFAULT_CODEX
from .iteration import reject_nested
from .iteration_evidence import (
    NeonUnavailable, bounded_json, excerpt, neon_command, parse_neon, parse_sol,
    process_problem, required_text, sol_command, validate_sol,
)
from .iteration_files import (
    IntegrityError, RunFiles, changes_and_patch, checked_path, json_text, manifest, safe_kind,
    snapshot, workspace_path,
)
from .process import run_process
from .project_spec import ASTRA_MODEL, RESUME_LIMITS, ProjectSpec, output_path
from .protocol import parse_model_object
from .verifiers import run_verifiers

PAUSE_STAGES = ("baseline_verify", "neon_plan", "astra_contract", "sol_exec", "round_verify",
                "neon_review", "astra_consult")
ACTIVE = ("INIT", "BASELINE_VERIFY", "NEON_PLAN", "ASTRA_CONTRACT_REVIEW", "ROUND_SOL_EXEC",
          "ROUND_VERIFY", "ROUND_NEON_REVIEW", "ROUND_ASTRA_CONSULT", "ROUND_DECIDE",
          "ASTRA_SCIENTIFIC_ACCEPT")
LOCKED_STATES = set(ACTIVE[4:])
RESUMABLE = {"PAUSED", "STOPPED_MAX_ATTEMPTS", "STOPPED_BUDGET_EXCEEDED",
             "STOPPED_TIMEOUT_BUDGET", "STOPPED_CONSECUTIVE_FAILURES",
             "STOPPED_MODEL_UNAVAILABLE", "STOPPED_SCIENTIFIC_REVIEW"}
NEON_DECISIONS = ("PASS", "RETRY", "REPLAN", "ESCALATE_ASTRA", "NEED_HUMAN")
ISSUE_CATEGORIES = ("none", "engineering", "numerical", "scientific", "resource_or_permission")
ASTRA_CONTRACT = ("APPROVE", "REVISE", "NEED_HUMAN")
ASTRA_CONSULT = ("CONTINUE_WITH_GUIDANCE", "REVISE_PLAN", "NEED_HUMAN")
ASTRA_ACCEPT = ("PASS", "SUPPLEMENT_VERIFICATION", "INSUFFICIENT_EVIDENCE", "NEED_HUMAN")
CLAIM_STATUSES = ("SUPPORTED", "UNSUPPORTED", "INSUFFICIENT_EVIDENCE")
TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens",
                "reasoning_output_tokens", "total_tokens")


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
        state.update(resume_state=resume_state or state["current_state"],
                     resumable=name in RESUMABLE, stop_reason=reason)
        self.move(name, "stopped", reason=reason, override_reason=state["override_reason"])

    def remaining(self):
        remaining = self.spec.limits["max_total_seconds"] - self.elapsed
        if remaining <= 0:
            raise _Stop("STOPPED_TIMEOUT_BUDGET", "total active wall-clock budget exhausted")
        return remaining

    def guard(self, *, check_project=True):
        self.spec.guard_verification()
        self.spec.guard_sources()
        state = self.state
        actual = self.spec.contract_hash(self.spec.verification_manifest)
        locked = state.get("locked_contract_hash")
        if (actual != state.get("contract_hash") or (locked is not None and actual != locked) or
                (state["current_state"] in LOCKED_STATES and locked is None)):
            raise IntegrityError("the verification contract hash changed or its lock is missing")
        expected = state["project_manifest"]
        if check_project and expected is not None and manifest(snapshot(self.spec.project_dir)) != expected:
            raise IntegrityError("project changed outside a Sol execution")
        for step, evidence in state["verification_evidence"].items():
            filename = f"{step}.json"
            digest = manifest({filename: self.files.path(filename).read_bytes()})[filename]
            if digest != evidence["report"] or manifest(snapshot(
                    self.files.directory / "artifacts" / step)) != evidence["artifacts"]:
                raise IntegrityError(f"verification evidence changed: {step}")

    def audit_manifest(self, *, new_artifacts=None):
        contents = {}
        for path in self.files.directory.iterdir():
            kind = safe_kind(path)
            # Windows denies reads of the byte range held by RunFiles.lock().
            if path.name in {"run.lock", "PAUSE"} and kind == "file":
                continue
            if kind == "file":
                contents[path.name] = path.read_bytes()
            elif kind == "directory":
                prefix = f"{new_artifacts}/" if path.name == "artifacts" and new_artifacts else None
                contents.update({f"{path.name}/{name}": data for name, data in snapshot(path).items()
                                 if not prefix or not name.startswith(prefix)})
            else:
                raise IntegrityError(f"invalid audit entry: {path}")
        return manifest(contents)

    def guard_audit(self, expected, *, new_artifacts=None):
        if expected != self.audit_manifest(new_artifacts=new_artifacts):
            raise IntegrityError("a subprocess changed the run's existing audit or evidence files")

    def pending(self, step, audit_file):
        if self.state["pending_step"] or step in self.state["completed_steps"]:
            raise IntegrityError(f"attempted to replay a pending or completed step: {step}")
        self.state["pending_step"] = {"step": step, "audit_file": audit_file}
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
        if next_state in ACTIVE and stage and (
                stage == self.pause_after or self.files.path("PAUSE").is_file()):
            self.pause(f"pause after {stage}")

    def pause(self, reason):
        self.stop("PAUSED", reason)
        self.files.path("PAUSE").unlink(missing_ok=True)

    def interrupted(self):
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
        return self.state

    def initialize(self):
        self.state["project_manifest"] = manifest(snapshot(self.spec.project_dir))
        self.complete("init", "BASELINE_VERIFY")

    def verify_step(self, step):
        self.remaining()
        audit_file = f"{step}.json"
        self.pending(step, audit_file)
        trusted = self.audit_manifest(new_artifacts=step)
        budget = self.spec.limits["max_total_seconds"] - self.elapsed
        try:
            report = run_verifiers(self.spec, self.files, step, self.runner, budget, clock=self.clock)
        finally:
            self.guard_audit(trusted, new_artifacts=step)
            self.guard()
        data = report.to_dict()
        serialized = json_text(data) + "\n"
        self.files.write(audit_file, serialized)
        self.state["verification_evidence"][step] = {
            "report": manifest({audit_file: serialized.encode("utf-8")})[audit_file],
            "artifacts": manifest(snapshot(Path(data["artifacts_dir"]))),
        }
        self.state["verification"] = data
        return data

    def baseline(self):
        self.state["baseline"] = self.verify_step("baseline_verify")
        self.complete("baseline_verify", "NEON_PLAN", "baseline_verify")

    def model(self, role, step, prompt, *, after_sol=None, validate=None):
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
        argv = (neon_command(DEFAULT_AGY, min(spec.limits["timeout_s"], math.ceil(budget)),
                             prompt, session) if role == "neon" else
                (sol_command if role == "sol" else astra_command)(
                    DEFAULT_CODEX, spec.project_dir, spec.agents[f"{role}_effort"], session))
        if role == "neon":
            if len(subprocess.list2cmdline(argv).encode("utf-16-le")) // 2 >= 32767:
                raise ValueError("Neon prompt exceeds the Windows command-line limit")
        elif any(not arg.isascii() for arg in argv):
            raise ValueError("Codex expects ASCII arguments and UTF-8 stdin")
        stdin = None if role == "neon" else prompt.encode("utf-8")
        filename = f"model_{number:03d}_{role}.json"
        state["total_model_calls"] = number
        state["astra_calls"] += int(role == "astra")
        state["sol_attempts"] += int(role == "sol")
        self.pending(step, filename)
        trusted = self.audit_manifest()
        budget = spec.limits["max_total_seconds"] - self.elapsed
        if budget <= 0:
            state["total_model_calls"] -= 1
            state["astra_calls"] -= int(role == "astra")
            state["sol_attempts"] -= int(role == "sol")
            state["pending_step"] = None
            self.remaining()
        env = dict(os.environ, AGENTBRIDGE_IN_SUBPROCESS="1", AGENTBRIDGE_RUN_ID=state["run_id"],
                   PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        result = self.runner(argv, cwd=spec.project_dir, timeout_s=min(spec.limits["timeout_s"], budget),
                             stdin=stdin, env=env)
        audit = {"step": step, "role": role, "argv": argv, "prompt": prompt, "stdout": result.stdout,
                 "stderr": result.stderr, "process": result.audit_metadata()}
        self.guard_audit(trusted)
        self.files.write(filename, json_text(audit) + "\n")
        self.guard(check_project=role != "sol")
        if after_sol:
            after_sol()
        if result.timed_out and budget <= spec.limits["timeout_s"]:
            state["pending_step"] = None
            raise _Stop("STOPPED_TIMEOUT_BUDGET", f"{step}: remaining wall-clock budget exhausted")
        if problem := process_problem(result):
            state["pending_step"] = None
            status = "FAILED" if result.exit_code == 0 and not result.utf8_ok else "STOPPED_MODEL_UNAVAILABLE"
            raise _Stop(status, f"{step}: {problem}")
        try:
            if role == "neon":
                envelope, payload = parse_neon(result.stdout, session)
                reported = envelope["conversation_id"]
            else:
                envelope, reported = parse_sol(result.stdout)
                audit["envelope"] = envelope
                if envelope["failure_events"]:
                    raise NeonUnavailable(f"{role} reported a failed turn")
                validate_sol(envelope, reported, session)
                payload = (parse_model_object(envelope["agent_message"]) if role == "astra" else
                           {"agent_message": envelope["agent_message"]})
            audit.update(envelope=envelope, payload=payload)
            state[session_key] = reported
            self.record_usage(role, envelope.get("usage"))
            if validate:
                validate(payload)
        except (ValueError, RecursionError) as exc:
            audit["protocol_error"] = str(exc)
            state["pending_step"] = None
            status = "STOPPED_MODEL_UNAVAILABLE" if isinstance(exc, NeonUnavailable) else "FAILED"
            raise _Stop(status, f"{step}: {exc}") from exc
        finally:
            self.files.write(filename, json_text(audit) + "\n")
        return payload

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
                if key not in {"project_dir", "verification_dir", "output_dir", "limits"}}

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
        step = f"neon_plan_{self.state['plan_revisions']}"
        prompt = (
            "Plan the executor's work without tools. Include a plan string unless NEED_HUMAN. "
            + self.neon_instruction()
            + (" Astra review is enabled." if self.spec.agents["require_astra"] else " Astra is disabled.")
            + "\n\nProject contract:\n" + bounded_json(self.contract_context(), 7500)
            + "\n\nIndependent baseline:\n" + self.neon_report(self.state["baseline"], 8500)
            + "\n\nEarlier plan and scientific review:\n"
            + bounded_json({"plan": self.state["plan"], "review": self.state["astra_contract_review"]}, 3500)
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

    def revise(self, step, adjustment=None):
        if self.state["plan_revisions"] >= self.spec.limits["max_plan_revisions"]:
            self.checkpoint_step(step)
            raise _Stop("STOPPED_NEED_HUMAN", "work plan revision limit reached")
        self.state["plan_revisions"] += 1
        if adjustment:
            self.state["plan"]["plan"] += "\n\nLatest work plan adjustment:\n" + adjustment

    def contract_review(self):
        step = f"astra_contract_{self.state['plan_revisions']}"
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
            self.revise(step)
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
        before = snapshot(self.spec.project_dir)
        if manifest(before) != state["project_manifest"]:
            raise IntegrityError("project changed before Sol execution")
        prompt = (
            "Implement the goal inside the current project workspace. Keep the independent verifiers, "
            "contract, scientific criteria, and audit files outside the project intact. "
            "Report your changes. The scheduler will run independent acceptance.\n\n"
            + json_text({"goal": self.spec.goal, "constraints": self.spec.constraints,
                         "plan": state["plan"]["plan"], "locked_contract_hash": state["locked_contract_hash"],
                         "astra_contract_review": state["astra_contract_review"]})
        )
        if number > 1:
            prompt += "\n\nPrevious independent evidence and review:\n" + json_text(
                self.round_evidence(state["rounds"][str(number - 1)])
            )
        if data["executions"]:
            prompt += "\n\nEarlier executions in this round:\n" + json_text(self.round_evidence(data))

        def capture():
            after = snapshot(self.spec.project_dir)
            changes, diff = changes_and_patch(before, after)
            filename = f"round_{number}_attempt_{state['sol_attempts']}_diff.patch"
            self.files.write(filename, diff)
            data["executions"].append({"changes": changes, "patch_file": filename,
                                       "patch_sha256": manifest({filename: diff.encode("utf-8")})[filename]})
            state["project_manifest"] = manifest(after)

        data["sol"] = self.model("sol", step, prompt, after_sol=capture)
        self.complete(step, "ROUND_VERIFY", "sol_exec")

    def verify(self):
        step = self.round_step("verify")
        self.round["verification"] = self.verify_step(step)
        self.complete(step, "ROUND_NEON_REVIEW", "round_verify")

    def round_evidence(self, data):
        evidence = {key: data[key] for key in
                    ("verification", "review", "consult", "sol", "astra_acceptance") if key in data}
        diffs = []
        for execution in data["executions"]:
            filename = execution["patch_file"]
            content = self.files.path(filename).read_bytes()
            if manifest({filename: content})[filename] != execution["patch_sha256"]:
                raise IntegrityError(f"recorded code diff changed: {filename}")
            diffs.append({**execution, "unified_diff": content.decode("utf-8")})
        evidence["diffs"] = diffs
        return evidence

    @staticmethod
    def neon_report(report, limit):
        # Preserve test failures and numeric comparisons when subprocess logs are long.
        results = []
        for result in report["results"]:
            summary = {key: value for key, value in result.items() if key not in {"argv", "stdout", "stderr"}}
            if not result["passed"]:
                summary.update({key: excerpt(result[key], 1400) for key in ("stdout", "stderr") if key in result})
            results.append(summary)
        return bounded_json({**report, "results": results}, limit)

    def review(self):
        step = self.round_step("neon_review")
        evidence = self.round_evidence(self.round)
        override = self.state["override_reason"]
        prompt = (
            "Review the executor round without tools. " + self.neon_instruction()
            + "\n\nIndependent verification:\n" + self.neon_report(evidence.pop("verification"), 11000)
            + "\n\nChanges:\n" + bounded_json(evidence, 8500)
            + "\n\nLocked contract and previous override:\n"
            + bounded_json({"hash": self.state["locked_contract_hash"], "previous_override": override,
                            "scientific_claims": self.spec.scientific_claims}, 3200)
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
                self.revise(step, consult.get("guidance", consult["reason"]))
        elif decision == "REPLAN":
            self.revise(step, data["review"].get("plan_adjustment", data["review"]["feedback"]))
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
    reject_nested()
    _pause_stage(pause_after)
    spec.guard_sources()
    checked_path(spec.output_dir, "directory", missing_ok=True).mkdir(parents=True, exist_ok=True)
    directory = spec.output_dir / uuid4().hex
    directory.mkdir()
    files = RunFiles(directory)
    state = {
        "version": 1, "framework": "agentbridge", "run_id": directory.name, "run_dir": str(directory),
        "config": spec.to_dict(), "source_hashes": spec.sources,
        "verification_manifest": spec.verification_manifest,
        "contract_hash": spec.contract_hash(spec.verification_manifest), "locked_contract_hash": None,
        "current_state": "INIT", "current_round": 0, "next_round": 1, "resume_state": None,
        "completed_steps": [], "pending_step": None, "resumable": True, "tamper_ok": True,
        "neon_conversation_id": None, "sol_thread_id": None, "astra_thread_id": None,
        "total_model_calls": 0, "astra_calls": 0, "sol_attempts": 0, "plan_revisions": 0,
        "elapsed_seconds": 0.0, "consecutive_failures": 0, "stop_reason": None, "override_reason": None,
        "baseline": None, "plan": None, "astra_contract_review": None, "project_manifest": None,
        "rounds": {}, "verification": None, "verification_evidence": {},
        "tokens": {role: dict.fromkeys(TOKEN_FIELDS, 0) for role in ("neon", "sol", "astra", "combined")},
    }
    with files.lock():
        files.record(state, "created", {})
        return _Framework(spec, files, state, pause_after, runner or run_process, clock).drive()


def _open_run(run_dir):
    directory = workspace_path(Path(run_dir))
    if not re.fullmatch(r"[0-9a-f]{32}", directory.name):
        raise ValueError("expected a generated AgentBridge run directory")
    output_path(directory.parent)
    return RunFiles(directory)


def _load(files):
    state = files.read("state.json")
    if (not isinstance(state, dict) or state.get("framework") != "agentbridge" or
            state.get("version") != 1 or state.get("run_id") != files.directory.name or
            state.get("run_dir") != str(files.directory)):
        raise ValueError("unsupported framework checkpoint or mismatched run directory")
    state.setdefault("locked_contract_hash", None)
    return state


def resume(run_dir, *, pause_after=None, runner=None, clock=monotonic, **limits):
    reject_nested()
    _pause_stage(pause_after)
    if limits.keys() - set(RESUME_LIMITS):
        raise ValueError("unsupported resume limit")
    files = _open_run(run_dir)
    with files.lock():
        state = _load(files)
        if not state["resumable"]:
            return state
        config = dict(state["config"])
        config["limits"] = ProjectSpec.validate_limits({**config["limits"], **limits})
        try:
            spec = ProjectSpec(config, sources=state["source_hashes"],
                               verification_manifest=state["verification_manifest"])
            if spec.output_dir != files.directory.parent:
                raise IntegrityError("audit directory differs from the recorded output directory")
        except (OSError, ValueError, RecursionError) as exc:
            state.update(current_state="STOPPED_PERMISSION_OR_TAMPER", resumable=False,
                         tamper_ok=False, stop_reason=str(exc), override_reason=str(exc))
            files.record(state, "stopped", {"reason": str(exc), "override_reason": str(exc)})
            return state
        state["config"] = spec.to_dict()
        if state["current_state"] == "PAUSED":
            files.path("PAUSE").unlink(missing_ok=True)
        return _Framework(spec, files, state, pause_after, runner or run_process, clock).drive()


def pause(run_dir):
    reject_nested()
    files = _open_run(run_dir)
    state = _load(files)
    requested = state["current_state"] in ACTIVE
    if requested:
        files.path("PAUSE").touch(exist_ok=True)
    return {"run_dir": str(files.directory), "pause_requested": requested,
            "current_state": state["current_state"]}


def status(run_dir):
    reject_nested()
    return _load(_open_run(run_dir))
