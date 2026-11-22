"""Exercise the framework with local verifiers and in-memory model replies."""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from collections import deque
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from agentbridge import framework
from agentbridge.bridge import DEFAULT_AGY, DEFAULT_CODEX, WORKDIR
from agentbridge.iteration_files import IntegrityError, RunFiles, digests, file_sha256, json_text
from agentbridge.process import ProcessResult, run_process
from agentbridge.project_spec import ProjectSpec
from agentbridge.verifiers import VERIFIER_REGISTRY, run_verifiers
from run_agentbridge import main


def neon(decision="PASS", *, category="none", **extra):
    return {"decision": decision, "issue_category": category, "reason": "Review reason",
            "feedback": "Use the independent evidence.", **extra}


def plan(label="implement the requested behavior"):
    return neon(plan=label)


def astra(decision="APPROVE", **extra):
    return {"decision": decision, "reason": "Scientific review reason", **extra}


def verdict(decision="PASS", status="SUPPORTED", *, claim="claim"):
    return astra(decision, claim_verdicts=[{"claim_id": claim, "status": status,
                                          "evidence_summary": "Independent measurement.",
                                          "scope_note": "Fixture scope."}])


def local_only(argv, **options):
    if argv[0] != sys.executable:
        raise AssertionError(f"unexpected nonlocal call: {argv[0]}")
    return run_process(argv, **options)


class MockModels:
    def __init__(self, case, *, neon_replies=(), astra_replies=(), sol_replies=()):
        self.case = case
        self.responses = {"neon": deque(neon_replies), "astra": deque(astra_replies),
                          "sol": deque(sol_replies)}
        self.calls = []

    def __call__(self, argv, *, cwd, timeout_s, stdin, env):
        self.case.assertEqual(env["AGENTBRIDGE_IN_SUBPROCESS"], "1")
        self.case.assertEqual(env["PYTHONUTF8"], "1")
        self.case.assertEqual(env["PYTHONIOENCODING"], "utf-8")
        self.case.assertGreater(timeout_s, 0)
        if argv[0] == sys.executable:
            return local_only(argv, cwd=cwd, timeout_s=timeout_s, stdin=stdin, env=env)
        if argv[0] == str(DEFAULT_AGY):
            role = "neon"
            self.case.assertIsNone(stdin)
            self.case.assertEqual(argv[:9], [
                str(DEFAULT_AGY), "--model", "gemini-4-neon-high", "--output-format", "json",
                "--mode", "plan", "--disable-slash-commands", "--print-timeout",
            ])
            prompt = argv[-1]
        else:
            self.case.assertEqual(argv[0], str(DEFAULT_CODEX))
            model = argv[argv.index("-m") + 1]
            role = {"gpt-6.2-sol": "sol", "gpt-6.2-astra": "astra"}[model]
            self.case.assertTrue(all(arg.isascii() for arg in argv))
            for variable in ("TEMP", "TMP", "TMPDIR"):
                self.case.assertEqual(env.get(variable), os.environ.get(variable))
            scope = "read-only" if role == "astra" else "workspace-write"
            if any(call[0] == role for call in self.calls) and "resume" in argv:
                expected = [str(DEFAULT_CODEX), "exec", "-s", scope, "-C", str(cwd), "resume",
                            "-m", model, "-c", "model_reasoning_effort=high", "--skip-git-repo-check",
                            "--json", f"{role}-thread", "-"]
            else:
                expected = [str(DEFAULT_CODEX), "exec", "-m", model, "-c", "model_reasoning_effort=high",
                            "-s", scope, "-C", str(cwd), "--skip-git-repo-check", "--json", "-"]
            self.case.assertEqual(argv, expected)
            prompt = stdin.decode("utf-8")
        self.calls.append((role, argv, prompt))
        self.case.assertTrue(self.responses[role], f"unexpected {role} model call")
        reply = self.responses[role].popleft()
        if callable(reply):
            reply = reply(cwd)
        if isinstance(reply, ProcessResult):
            return reply
        if role == "neon":
            return ProcessResult(exit_code=0, stdout=json.dumps(
                {"status": "success", "conversation_id": "neon-thread", "response": reply}))
        message = json.dumps(reply, ensure_ascii=False) if role == "astra" else reply
        events = [
            {"type": "thread.started", "thread_id": f"{role}-thread"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": message}},
            {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 3}},
        ]
        return ProcessResult(exit_code=0, stdout="\n".join(map(json.dumps, events)))

    def roles(self):
        return [role for role, _, _ in self.calls]

    def prompts(self, role):
        return [prompt for label, _, prompt in self.calls if label == role]


class Fixture(unittest.TestCase):
    temp_parent = WORKDIR

    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=self.temp_parent, prefix="framework_test_")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.project = self.root / "project"
        self.verification = self.root / "verification"
        self.output = self.root / "runs"
        self.project.mkdir()
        self.verification.mkdir()
        self.config_path = self.root / "project.json"
        self.set_answer(0)
        (self.verification / "measure.py").write_text(
            "import json, sys\n"
            "from pathlib import Path\n"
            "source = Path(sys.argv[1]) / 'answer.txt'\n"
            "result = {'nested': {'value': int(source.read_text())}, 'unit': 'eV'}\n"
            "Path(sys.argv[2]).write_text(json.dumps(result), encoding='utf-8')\n"
            "print('independent measurement')\n", encoding="utf-8")

    def set_answer(self, value):
        (self.project / "answer.txt").write_text(f"{value}\n", encoding="utf-8")

    def fix(self, cwd):
        self.assertEqual(cwd, self.project)
        self.set_answer(42)
        return "Implementation updated"

    def config(self, *, science=False, **overrides):
        verifiers = [
            {"id": "measure", "type": "command", "argv": [
                "{python}", "-I", "-B", "{verification_dir}/measure.py",
                "{project_dir}", "{artifacts_dir}/metrics.json"],
             "must_contain": ["independent measurement"]},
            {"id": "value", "type": "numeric_metrics", "path": "metrics.json", "rules": [
                {"metric": "nested.value", "op": "eq", "expected": 42, "unit": "eV"}]},
        ]
        result = {"schema_version": 1, "project_id": "generic", "name": "离线测试",
                  "task_type": "scientific" if science else "engineering", "goal": "修复输出",
                  "project_dir": "project", "verification_dir": "verification", "output_dir": "runs",
                  "agents": {"require_astra": science}, "verifiers": verifiers}
        if science:
            result["scientific_claims"] = [{"claim_id": "claim", "statement": "Value is 42.",
                                            "required_verifiers": ["value"], "scope_limitation": "Fixture."}]
        return {**result, **overrides}

    def spec(self, data=None, **overrides):
        self.config_path.write_text(json.dumps(data if data is not None else self.config(**overrides),
                                              ensure_ascii=False), encoding="utf-8")
        return ProjectSpec.from_file(self.config_path)

    def run_checks(self, spec, runner=local_only, step="check"):
        folder = spec.output_dir / uuid4().hex
        folder.mkdir(parents=True)
        return run_verifiers(spec, RunFiles(folder), step, runner, 30)

    def run_models(self, models, *, science=False, pause_after=None, **config):
        return framework.start(self.spec(science=science, **config), runner=models, pause_after=pause_after)


class ProjectSpecTests(Fixture):
    def test_examples_validate_with_expected_defaults_and_claims(self):
        ledger, lj = [ProjectSpec.from_file(WORKDIR / "examples" / name / "project.json")
                      for name in ("mini_ledger", "lj_physics")]
        self.assertFalse(ledger.agents["require_astra"])
        self.assertIn("ledger.py", ledger.goal)
        self.assertEqual(ledger.verifiers[0]["min_tests"], 8)
        self.assertTrue(lj.agents["require_astra"])
        self.assertEqual(len(lj.scientific_claims), 2)
        self.assertEqual(lj.output_dir, WORKDIR / "runs")
        self.assertEqual(lj.limits["max_model_calls"], 12)

    def test_contract_hash_binds_semantics_order_and_file_hashes(self):
        data = self.config()
        data["verifiers"].insert(0, {"id": "artifact", "type": "artifact", "scope": "project",
                                     "path": "answer.txt"})
        spec = self.spec(data)
        original = spec.contract_hash(spec.verification_manifest)
        raw = spec.to_dict()
        raw["limits"]["max_sol_attempts"] = 1
        self.assertEqual(ProjectSpec(raw).contract_hash(spec.verification_manifest), original)
        raw["verifiers"][0], raw["verifiers"][1] = raw["verifiers"][1], raw["verifiers"][0]
        self.assertNotEqual(ProjectSpec(raw).contract_hash(spec.verification_manifest), original)
        (self.verification / "new.txt").write_text("new content", encoding="utf-8")
        with self.assertRaisesRegex(IntegrityError, "SHA-256"):
            spec.guard_verification()
        changed = ProjectSpec.from_file(self.config_path)
        self.assertNotEqual(changed.contract_hash(changed.verification_manifest), original)

    def test_path_isolation_protection_and_strict_input(self):
        for changes in (
            {"project_dir": ".."}, {"project_dir": str(WORKDIR / "agentbridge")},
            {"project_dir": str(WORKDIR.parent)}, {"verification_dir": "project"},
            {"output_dir": "verification/nested"}, {"output_dir": "project/runs"},
            {"schema_version": True}, {"task_type": "other"}, {"verifiers": []},
            {"agents": {"require_astra": True}}, {"goal_file": "goal.txt"},
            {"limits": {"max_sol_attempts": 101}}, {"limits": {"max_astra_calls": 101}},
            {"limits": {"max_model_calls": 501}}, {"limits": {"max_plan_revisions": 51}},
            {"limits": {"timeout_s": False}},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.spec(**changes)
        self.config_path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            ProjectSpec.from_file(self.config_path)

    def test_verifier_definitions_and_claim_references_are_validated(self):
        for item in (
            {"id": "a", "type": "command", "argv": ["{python}", "{unknown}"]},
            {"id": "a", "type": "unittest", "tests_dir": ".."},
            {"id": "a", "type": "numeric_metrics", "path": "metrics.json",
             "rules": [{"metric": "a", "op": "close", "expected": True}]},
            {"id": "a", "type": "artifact", "path": "../memo"},
        ):
            with self.subTest(item=item), self.assertRaises(ValueError):
                self.spec(verifiers=[item])
        data = self.config(science=True)
        data["scientific_claims"][0]["required_verifiers"] = ["absent"]
        with self.assertRaisesRegex(ValueError, "verifier references"):
            self.spec(data)


class VerifierTests(Fixture):
    def test_command_and_artifact_use_fresh_artifacts_and_typed_json(self):
        data = self.config()
        data["verifiers"].append({"id": "file", "type": "artifact", "path": "metrics.json",
                                  "format": "json", "required_keys": ["nested"],
                                  "exact_fields": {"unit": "eV", "nested": {"value": 42}},
                                  "must_contain": ["eV"], "min_bytes": 15})
        self.set_answer(42)
        spec = self.spec(data)
        report = self.run_checks(spec)
        self.assertEqual(set(VERIFIER_REGISTRY), {"command", "artifact", "numeric_metrics", "unittest", "pytest"})
        self.assertTrue(report.all_passed, report.results)
        self.assertEqual(report.metrics_snapshots["value"]["nested"]["value"], 42)
        self.assertFalse(Path(report.artifacts_dir).is_relative_to(self.project))
        self.assertEqual(report.results[1]["producer"], "measure")
        data["verifiers"][-1]["exact_fields"]["nested"]["value"] = True
        self.assertEqual(self.run_checks(self.spec(data)).failed_verifiers, ["file"])

    def test_command_exit_utf8_and_substring_checked(self):
        command = {"id": "cmd", "type": "command", "argv": [
            "{python}", "-B", "-c", "import sys; print('ready'); sys.exit(7)"],
            "expected_exit_code": 7, "must_contain": ["ready"]}
        spec = self.spec(verifiers=[command])
        self.assertTrue(self.run_checks(spec).all_passed)
        bad = self.run_checks(spec, runner=lambda *_a, **_k: ProcessResult(
            exit_code=7, stdout="ready", invalid_utf8_base64={"stdout": "/w=="}))
        self.assertFalse(bad.all_passed)
        command["must_contain"] = ["missing"]
        self.assertFalse(self.run_checks(self.spec(verifiers=[command])).all_passed)

    def test_numeric_comparisons_audit_value_margin_and_threshold(self):
        self.set_answer(42)
        data = self.config()
        rules = {"le": {"threshold": 42}, "lt": {"threshold": 43}, "ge": {"threshold": 40},
                 "gt": {"threshold": 40}, "abs_le": {"threshold": 50},
                 "close": {"expected": 41.8, "abs_tol": 0.3, "rel_tol": 0.001},
                 "eq": {"expected": 42}, "between": {"min": 40, "max": 44}}
        data["verifiers"][1]["rules"] = [
            {"metric": "nested.value", "op": op, "unit": "eV", **fields} for op, fields in rules.items()]
        report = self.run_checks(self.spec(data))
        self.assertTrue(report.all_passed)
        for check in report.results[1]["checks"]:
            self.assertEqual(check["actual"], 42)
            self.assertGreaterEqual(check["margin"], 0)
            self.assertEqual(check["rule"]["unit"], "eV")

    def test_numeric_rejects_nonfinite_booleans_missing_and_untrusted_metrics(self):
        spec = self.spec()
        (self.project / "metrics.json").write_text('{"nested":{"value":42}}', encoding="utf-8")
        for raw in ('{"nested":{"value":NaN}}', '{"nested":{"value":Infinity}}', '{"nested":{"value":-Infinity}}',
                    '{"nested":{"value":1e999}}', '{"nested":{"value":true}}', '{"nested":{}}'):
            def write(_argv, *, cwd, **_options):
                (cwd / "metrics.json").write_text(raw, encoding="utf-8")
                return ProcessResult(exit_code=0, stdout="independent measurement")

            with self.subTest(raw=raw):
                self.assertEqual(self.run_checks(spec, runner=write).failed_verifiers, ["value"])
        with self.assertRaisesRegex(ValueError, "earlier command verifier"):
            self.spec(verifiers=spec.verifiers[1:])
        for outcome in (ProcessResult(exit_code=0, stdout="independent measurement"),
                        ProcessResult(exit_code=1, stdout="independent measurement")):
            report = self.run_checks(spec, runner=lambda *_a, **_k: outcome)
            self.assertIn("generated by a successful command", report.results[1]["reason"])

    def test_artifact_project_scope_and_no_reuse_of_step(self):
        spec = self.spec(verifiers=[{"id": "answer", "type": "artifact", "scope": "project",
                                     "path": "answer.txt", "format": "text", "must_contain": ["0"]}])
        report = self.run_checks(spec)
        self.assertTrue(report.all_passed)
        with self.assertRaisesRegex(IntegrityError, "already in use"):
            run_verifiers(spec, RunFiles(Path(report.artifacts_dir).parent.parent), "check", local_only, 30)

    def test_unittest_proof_and_skip_policy(self):
        (self.verification / "test_program.py").write_text(
            "import unittest\n"
            "class TestProgram(unittest.TestCase):\n"
            "    def test_ok(self): self.assertTrue(True)\n"
            "    @unittest.skip('fixture')\n"
            "    def test_skip(self): pass\n", encoding="utf-8")
        definition = {"id": "unit", "type": "unittest", "tests_dir": "."}
        self.assertFalse(self.run_checks(self.spec(verifiers=[definition])).all_passed)
        definition["allow_skipped"] = True
        self.assertTrue(self.run_checks(self.spec(verifiers=[definition])).all_passed)
        definition["min_tests"] = 2
        self.assertFalse(self.run_checks(self.spec(verifiers=[definition])).all_passed)
        definition["min_tests"] = 1
        summary = ProcessResult(exit_code=0, stderr="-" * 70 + "\nRan 1 test in 0.001s\nOK\n")
        report = self.run_checks(self.spec(verifiers=[definition]), runner=lambda *a, **k: summary)
        self.assertIn("completion receipt", report.results[0]["reason"])

    @unittest.skipUnless(importlib.util.find_spec("pytest"), "pytest is an optional acceptance tool")
    def test_pytest_local_runner_and_skip_policy(self):
        (self.project / "answer.py").write_text("value = 42\n", encoding="utf-8")
        (self.verification / "test_program.py").write_text(
            "import pytest\n"
            "from answer import value\n"
            "def test_ok(): assert value == 42\n"
            "@pytest.mark.skip(reason='fixture')\n"
            "def test_skip(): pass\n", encoding="utf-8")
        definition = {"id": "pt", "type": "pytest", "target": "test_program.py"}
        strict = self.run_checks(self.spec(verifiers=[definition]))
        self.assertEqual(strict.results[0]["counts"]["skipped"], 1, strict.results)
        self.assertFalse(strict.all_passed)
        definition["allow_skipped"] = True
        accepted = self.run_checks(self.spec(verifiers=[definition]))
        self.assertTrue(accepted.all_passed, accepted.results)
        self.assertEqual(accepted.results[0]["counts"]["passed"], 1)

    def test_pytest_requires_real_counts_and_successful_exit(self):
        spec = self.spec(verifiers=[{"id": "pt", "type": "pytest", "target": "."}])
        for code, output in ((0, "passed"), (0, "=== 1 passed, 1 error in 0.01s ==="),
                             (0, "=== 1 passed, 1 failed in 0.01s ==="),
                             (1, "=== 1 passed in 0.01s ===")):
            with self.subTest(code=code, output=output):
                result = self.run_checks(spec, runner=lambda *_a, **_k: ProcessResult(
                    exit_code=code, stdout=output))
                self.assertFalse(result.all_passed)

    def test_verification_manifest_guard_before_and_after_process(self):
        spec = self.spec()

        def tamper(_argv, **_options):
            (self.verification / "new.txt").write_text("modified", encoding="utf-8")
            return ProcessResult(exit_code=0, stdout="independent measurement")

        with self.assertRaisesRegex(IntegrityError, "locked SHA-256"):
            self.run_checks(spec, runner=tamper)
        with self.assertRaisesRegex(IntegrityError, "locked SHA-256"):
            self.run_checks(spec, runner=lambda *a, **k: self.fail("already modified"))

    def test_later_command_cannot_overwrite_sealed_numeric_evidence(self):
        data = self.config()
        data["verifiers"].append({"id": "overwrite", "type": "command", "argv": [
            "{python}", "-B", "-c", "from pathlib import Path; Path('metrics.json').write_text('{{}}')"]})
        self.set_answer(42)
        with self.assertRaisesRegex(IntegrityError, "changed after numerical verification"):
            self.run_checks(self.spec(data))


class FrameworkTests(Fixture):
    def test_engineering_success_has_zero_astra_calls(self):
        mock = MockModels(self, neon_replies=[plan(), neon()], sol_replies=[self.fix])
        state = self.run_models(mock)
        self.assertEqual(state["current_state"], "COMPLETED_PASS", state["stop_reason"])
        self.assertEqual(mock.roles(), ["neon", "sol", "neon"])
        self.assertEqual(state["astra_calls"], 0)
        self.assertIsNone(state["astra_thread_id"])
        self.assertEqual(state["locked_contract_hash"], state["contract_hash"])
        self.assertIn("修复输出", mock.prompts("sol")[0])
        self.assertFalse(state["baseline"]["all_passed"])
        self.assertTrue(state["verification"]["all_passed"])

    def test_contract_revise_and_sol_retry_keep_each_model_session(self):
        mock = MockModels(
            self, neon_replies=[plan(), plan("revised baseline plan"), neon("RETRY", category="numerical"), neon()],
            astra_replies=[astra("REVISE", guidance="clarify numerical check"), astra(), verdict()],
            sol_replies=["First implementation", self.fix],
        )
        state = self.run_models(mock, science=True)
        self.assertEqual(state["current_state"], "COMPLETED_PASS", state["stop_reason"])
        self.assertEqual(mock.roles(), ["neon", "astra", "neon", "astra", "sol", "neon", "sol", "neon", "astra"])
        self.assertEqual(state["astra_calls"], 3)
        self.assertEqual(state["plan_revisions"], 1)
        self.assertEqual(state["sol_attempts"], 2)
        self.assertIn("clarify numerical check", mock.prompts("neon")[1])
        self.assertIn('"actual": 0', mock.prompts("sol")[1])
        self.assertIn('"feedback": "Use the independent evidence."', mock.prompts("sol")[1])
        self.assertIn('"actual": 42', mock.prompts("astra")[-1])
        self.assertIn("+42", mock.prompts("astra")[-1])
        self.assertIn("scope_limitation", mock.prompts("astra")[-1])
        self.assertTrue(all("--conversation" in args for role, args, _ in mock.calls[2:] if role == "neon"))

    def test_neon_replans_then_escalates_with_original_scientific_evidence(self):
        def partial(_cwd):
            self.set_answer(17)
            return "Partial revision"

        mock = MockModels(
            self, neon_replies=[plan(), neon("REPLAN", category="engineering", plan_adjustment="Try a new route"),
                                neon("ESCALATE_ASTRA", category="scientific"), neon()],
            astra_replies=[astra(), astra("CONTINUE_WITH_GUIDANCE", guidance="Inspect calculation"), verdict()],
            sol_replies=[partial, "Second implementation", self.fix],
        )
        state = self.run_models(mock, science=True, limits={"max_consecutive_failures": 3})
        self.assertEqual(state["current_state"], "COMPLETED_PASS", state["stop_reason"])
        self.assertEqual(state["locked_contract_hash"], state["contract_hash"])
        self.assertEqual(state["plan_revisions"], 1)
        self.assertIn("Try a new route", mock.prompts("sol")[1])
        self.assertIn('"actual": 17', mock.prompts("astra")[1])
        self.assertIn("Inspect calculation", mock.prompts("sol")[2])
        self.assertIn("round_2_astra_consult", state["completed_steps"])

    def test_astra_consult_revises_the_work_plan(self):
        mock = MockModels(self, neon_replies=[plan(), neon("ESCALATE_ASTRA", category="scientific"), neon()],
                          astra_replies=[astra(), astra("REVISE_PLAN", guidance="Check the reference"),
                                         verdict()], sol_replies=["Partial", self.fix])
        state = self.run_models(mock, science=True, pause_after="astra_consult")
        self.assertEqual(state["current_state"], "PAUSED", state["stop_reason"])
        self.assertEqual(state["resume_state"], "ROUND_DECIDE")
        state = framework.resume(state["run_dir"], runner=mock)
        self.assertEqual(state["current_state"], "COMPLETED_PASS", state["stop_reason"])
        self.assertEqual(state["plan_revisions"], 1)
        self.assertIn("Check the reference", mock.prompts("sol")[1])
        self.assertEqual(mock.roles().count("astra"), 3)

    def test_independent_failure_vetoes_neon_pass_before_astra_acceptance(self):
        mock = MockModels(self, neon_replies=[plan(), neon()], astra_replies=[astra(), verdict()],
                          sol_replies=["I claim everything passed"])
        state = self.run_models(mock, science=True)
        self.assertEqual(state["current_state"], "STOPPED_NEED_HUMAN")
        self.assertIn("independent verification failed: value", state["override_reason"])
        self.assertEqual(mock.roles(), ["neon", "astra", "sol", "neon"])
        self.assertEqual(len(mock.responses["astra"]), 1)
        self.assertFalse(state["verification"]["all_passed"])

    def test_disabled_astra_never_receives_escalations(self):
        mock = MockModels(self, neon_replies=[plan(), neon("ESCALATE_ASTRA", category="scientific")],
                          sol_replies=[self.fix])
        state = self.run_models(mock)
        self.assertEqual(state["current_state"], "STOPPED_NEED_HUMAN")
        self.assertEqual(mock.roles(), ["neon", "sol", "neon"])

    def test_astra_unavailable_fails_closed_and_resumes_at_contract(self):
        mock = MockModels(self, neon_replies=[plan(), neon()],
                          astra_replies=[ProcessResult(launch_error="offline"), astra(), verdict()],
                          sol_replies=[self.fix])
        state = self.run_models(mock, science=True)
        self.assertEqual(state["current_state"], "STOPPED_MODEL_UNAVAILABLE")
        self.assertIsNone(state["locked_contract_hash"])
        self.assertEqual(mock.roles(), ["neon", "astra"])
        self.assertEqual(state["resume_state"], "ASTRA_CONTRACT_REVIEW")
        state = framework.resume(state["run_dir"], runner=mock)
        self.assertEqual(state["current_state"], "COMPLETED_PASS", state["stop_reason"])
        self.assertEqual(mock.roles(), ["neon", "astra", "astra", "sol", "neon", "astra"])

    def test_astra_claim_review_blocks_and_can_reverify_without_sol_replay(self):
        mock = MockModels(
            self, neon_replies=[plan(), neon(), neon()],
            astra_replies=[astra(), verdict("SUPPLEMENT_VERIFICATION", "INSUFFICIENT_EVIDENCE"), verdict()],
            sol_replies=[self.fix],
        )
        state = self.run_models(mock, science=True)
        self.assertEqual(state["current_state"], "STOPPED_SCIENTIFIC_REVIEW")
        self.assertTrue(state["resumable"])
        self.assertEqual(state["resume_state"], "ROUND_VERIFY")
        final = framework.resume(state["run_dir"], runner=mock)
        self.assertEqual(final["current_state"], "COMPLETED_PASS", final["stop_reason"])
        self.assertEqual(mock.roles(), ["neon", "astra", "sol", "neon", "astra", "neon", "astra"])
        self.assertIn("round_1_verify_review_2", final["completed_steps"])

    def test_astra_pass_with_an_unsupported_claim_is_vetoed(self):
        mock = MockModels(self, neon_replies=[plan(), neon()], astra_replies=[astra(), verdict(status="UNSUPPORTED")],
                          sol_replies=[self.fix])
        state = self.run_models(mock, science=True)
        self.assertEqual(state["current_state"], "STOPPED_SCIENTIFIC_REVIEW")
        self.assertIn("lacked support", state["override_reason"])

    def test_astra_claim_ids_must_match_locked_claims(self):
        mock = MockModels(self, neon_replies=[plan(), neon()], astra_replies=[astra(), verdict(claim="unrelated")],
                          sol_replies=[self.fix])
        state = self.run_models(mock, science=True)
        self.assertEqual(state["current_state"], "FAILED")
        self.assertIn("exactly the locked claims", state["stop_reason"])

    def test_tamper_during_sol_stops_before_verification_or_review(self):
        def tamper(_cwd):
            (self.verification / "measure.py").write_text("# changed", encoding="utf-8")
            return "PASS"

        mock = MockModels(self, neon_replies=[plan()], sol_replies=[tamper])
        result = self.run_models(mock)
        self.assertEqual(result["current_state"], "STOPPED_PERMISSION_OR_TAMPER")
        self.assertFalse(result["tamper_ok"])
        self.assertIn("SHA-256", result["override_reason"])
        self.assertEqual(mock.roles(), ["neon", "sol"])

    def test_paused_resume_at_contract_and_sol_has_no_replay(self):
        mock = MockModels(self, neon_replies=[plan(), neon()], astra_replies=[astra(), verdict()],
                          sol_replies=[self.fix])
        state = self.run_models(mock, science=True, pause_after="astra_contract")
        self.assertEqual(state["current_state"], "PAUSED", state["stop_reason"])
        self.assertEqual(state["resume_state"], "ROUND_SOL_EXEC")
        run_dir = state["run_dir"]
        contract = state["locked_contract_hash"]
        state = framework.resume(run_dir, pause_after="sol_exec", runner=mock, max_model_calls=7,
                                 budget_reason="Trim the remaining model budget.")
        self.assertEqual(state["current_state"], "PAUSED", state["stop_reason"])
        self.assertEqual(state["resume_state"], "ROUND_VERIFY")
        self.assertEqual(mock.roles(), ["neon", "astra", "sol"])
        state = framework.resume(run_dir, runner=mock)
        self.assertEqual(state["current_state"], "COMPLETED_PASS", state["stop_reason"])
        self.assertEqual(state["locked_contract_hash"], contract)
        self.assertEqual(mock.roles(), ["neon", "astra", "sol", "neon", "astra"])
        self.assertEqual(len(state["completed_steps"]), len(set(state["completed_steps"])))
        self.assertEqual(framework.resume(run_dir, runner=mock), state)

    def test_changing_locked_configuration_during_pause_stops_run(self):
        mock = MockModels(self, neon_replies=[plan()])
        state = self.run_models(mock, pause_after="neon_plan")
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["verifiers"][1]["rules"][0]["expected"] = 0
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        state = framework.resume(state["run_dir"], runner=mock)
        self.assertEqual(state["current_state"], "STOPPED_PERMISSION_OR_TAMPER")
        self.assertIn("locked configuration", state["override_reason"])

    def test_changing_checkpoint_contract_hash_during_pause_stops_run(self):
        mock = MockModels(self, neon_replies=[plan()])
        state = self.run_models(mock, pause_after="neon_plan")
        files = RunFiles(Path(state["run_dir"]))
        state["locked_contract_hash"] = "0" * 64
        files.write("state.json", json.dumps(state))
        state = framework.resume(state["run_dir"], runner=mock)
        self.assertEqual(state["current_state"], "STOPPED_PERMISSION_OR_TAMPER")
        self.assertIn("contract hash changed", state["override_reason"])

    def test_paused_report_rejects_changed_artifact(self):
        mock = MockModels(self, neon_replies=[plan()], sol_replies=[self.fix])
        state = self.run_models(mock, pause_after="round_verify")
        (Path(state["verification"]["artifacts_dir"]) / "metrics.json").write_text("{}", encoding="utf-8")
        state = framework.resume(state["run_dir"], runner=mock)
        self.assertEqual(state["current_state"], "STOPPED_PERMISSION_OR_TAMPER")
        self.assertIn("verification evidence changed", state["override_reason"])

    def test_pending_call_after_crash_is_not_replayed(self):
        def crash(_cwd):
            raise RuntimeError("process disappeared during Sol")

        mock = MockModels(self, neon_replies=[plan()], sol_replies=[crash])
        with self.assertRaisesRegex(RuntimeError, "process disappeared"):
            self.run_models(mock)
        run_dir = next(self.output.iterdir())
        original = framework.status(run_dir)
        self.assertEqual(original["pending_step"]["step"], "round_1_sol_exec")
        state = framework.resume(run_dir, runner=mock)
        self.assertEqual(state["current_state"], "STOPPED_NEED_HUMAN")
        self.assertEqual(mock.roles(), ["neon", "sol"])

    def test_interrupt_after_model_returns_keeps_step_pending(self):
        mock = MockModels(self, neon_replies=[plan()])
        with patch("agentbridge.framework._neon_payload", side_effect=KeyboardInterrupt):
            state = self.run_models(mock)
        self.assertEqual(state["current_state"], "STOPPED_NEED_HUMAN")
        self.assertEqual(state["pending_step"]["step"], "neon_plan_0")
        self.assertEqual(framework.resume(state["run_dir"], runner=mock), state)
        self.assertEqual(mock.roles(), ["neon"])

    def test_external_pause_marker_and_resume_model_budget(self):
        def mark(_cwd):
            active = next(self.output.iterdir())
            self.assertTrue(framework.pause(active)["pause_requested"])
            return plan()

        mock = MockModels(self, neon_replies=[mark, neon()], sol_replies=[self.fix])
        state = self.run_models(mock, limits={"max_model_calls": 1, "max_sol_attempts": 1})
        self.assertEqual(state["current_state"], "PAUSED")
        state = framework.resume(state["run_dir"], runner=mock)
        self.assertEqual(state["current_state"], "STOPPED_BUDGET_EXCEEDED")
        self.assertEqual(mock.roles(), ["neon"])
        state = framework.resume(state["run_dir"], runner=mock, max_model_calls=3,
                                 budget_reason="Allow implementation and review.")
        self.assertEqual(state["current_state"], "COMPLETED_PASS", state["stop_reason"])

    def test_astra_budget_blocks_acceptance_until_budget_is_raised(self):
        mock = MockModels(self, neon_replies=[plan(), neon()], astra_replies=[astra(), verdict()],
                          sol_replies=[self.fix])
        state = self.run_models(mock, science=True, limits={"max_astra_calls": 1})
        self.assertEqual(state["current_state"], "STOPPED_BUDGET_EXCEEDED")
        self.assertEqual(state["resume_state"], "ASTRA_SCIENTIFIC_ACCEPT")
        self.assertTrue(state["verification"]["all_passed"])
        self.assertEqual(mock.roles(), ["neon", "astra", "sol", "neon"])
        state = framework.resume(state["run_dir"], runner=mock, max_astra_calls=2,
                                 budget_reason="Allow the scientific acceptance call.")
        self.assertEqual(state["current_state"], "COMPLETED_PASS", state["stop_reason"])
        self.assertEqual(mock.roles(), ["neon", "astra", "sol", "neon", "astra"])

    def test_plan_revision_cap_keeps_contract_unlocked_and_blocks_sol(self):
        mock = MockModels(self, neon_replies=[plan(), plan(), plan(), neon()],
                          astra_replies=[astra("REVISE"), astra("REVISE"), astra(), verdict()],
                          sol_replies=[self.fix])
        state = self.run_models(mock, science=True, limits={"max_plan_revisions": 1})
        self.assertEqual(state["current_state"], "STOPPED_BUDGET_EXCEEDED")
        self.assertIn("revision limit", state["stop_reason"])
        self.assertIsNone(state["locked_contract_hash"])
        self.assertEqual(mock.roles(), ["neon", "astra", "neon", "astra"])
        previous = state["completed_steps"]
        state = framework.resume(state["run_dir"], runner=mock, max_plan_revisions=2,
                                 budget_reason="Review a second plan revision.")
        self.assertEqual(state["current_state"], "COMPLETED_PASS", state["stop_reason"])
        self.assertEqual(state["completed_steps"][:len(previous)], previous)
        self.assertEqual(mock.roles(), ["neon", "astra", "neon", "astra", "neon", "astra", "sol", "neon", "astra"])

    def test_total_wall_clock_budget_caps_subprocess_and_stops_model_calls(self):
        now = 0.0

        def clock():
            return now

        def budget_runner(argv, *, timeout_s, **_options):
            nonlocal now
            self.assertEqual(argv[0], sys.executable)
            self.assertEqual(timeout_s, 1)
            now += timeout_s
            return ProcessResult(timed_out=True)

        state = framework.start(self.spec(limits={"max_total_seconds": 1}), runner=budget_runner, clock=clock)
        self.assertEqual(state["current_state"], "STOPPED_TIMEOUT_BUDGET")
        self.assertEqual(state["total_model_calls"], 0)
        self.assertIsNone(state["pending_step"])

    def test_cli_validate_pause_resume_status_and_error_codes(self):
        self.spec()
        mock = MockModels(self, neon_replies=[plan(), neon()], sol_replies=[self.fix])

        def invoke(*args):
            with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
                code = main(args, runner=mock)
            return code, json.loads(output.getvalue()) if output.getvalue() else None

        code, validation = invoke("validate", "--config", str(self.config_path))
        self.assertEqual(code, 0)
        self.assertEqual(len(validation["contract_hash"]), 64)
        code, state = invoke("start", "--config", str(self.config_path), "--pause-after", "baseline_verify",
                             "--output-dir", str(self.root / "alternative"))
        self.assertEqual(code, 2)
        self.assertEqual(mock.roles(), [])
        run = state["run_dir"]
        self.assertEqual(Path(run).parent.name, "alternative")
        self.assertEqual(invoke("pause", "--run-dir", run)[0], 0)
        self.assertEqual(invoke("status", "--run-dir", run)[1]["current_state"], "PAUSED")
        self.assertEqual(invoke("resume", "--run-dir", run)[0], 0)
        self.assertEqual(invoke("validate")[0], 1)


class ExternalProjectTests(Fixture):
    temp_parent = None

    def setUp(self):
        super().setUp()
        self.assertFalse(self.root.is_relative_to(WORKDIR))
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        self.samples = self.inputs / "samples"
        self.samples.mkdir()
        (self.samples / "dataset.bin").write_bytes(b"dataset sample")
        (self.samples / "cache.tmp").write_text("scratch", encoding="utf-8")
        self.critical = self.inputs / "critical.tmp"
        self.critical.write_text("immutable sample", encoding="utf-8")
        self.goal = self.inputs / "goal.txt"
        self.goal.write_text("External goal", encoding="utf-8")
        self.config_path = self.root / "contracts" / "project.json"
        self.config_path.parent.mkdir()

    def config(self, **overrides):
        data = super().config(**{"allow_external_paths": True, "project_dir": "../project",
                                  "verification_dir": str(self.verification), "output_dir": "../runs",
                                  "readonly_inputs": [str(self.critical), "../inputs/samples"], **overrides})
        if "goal" not in overrides:
            del data["goal"]
            data.setdefault("goal_file", "../inputs/goal.txt")
        return data

    def test_external_validation_start_resume_and_status(self):
        mock = MockModels(self, neon_replies=[plan(), neon()], sol_replies=[self.fix])
        state = self.run_models(mock, pause_after="baseline_verify")
        self.assertEqual(state["current_state"], "PAUSED", state["stop_reason"])
        run = state["run_dir"]
        self.assertEqual(Path(run).parent, self.output)
        self.assertEqual(framework.status(run)["goal"], "External goal")
        self.assertFalse(framework.pause(run)["pause_requested"])
        state = framework.resume(run, runner=mock)
        self.assertEqual(state["current_state"], "COMPLETED_PASS", state["stop_reason"])
        self.assertEqual(state["config"]["readonly_inputs"], [str(self.critical), str(self.samples)])
        self.assertEqual(state["readonly_manifest"][str(self.samples)]["cache.tmp"]["integrity_mode"], "ignored")
        new_config = self.config_path.with_name("project-v2.json")
        new_config.write_text(json.dumps(self.config(goal="Revised external goal")), encoding="utf-8")
        child = framework.revise(run, new_config, reason="Check a revised external goal",
                                 pause_after="baseline_verify", runner=local_only)
        self.assertEqual(child["parent_run_id"], state["run_id"])
        self.assertEqual(Path(child["run_dir"]).parent, self.output)
        self.assertEqual(child["current_state"], "PAUSED")

    def test_external_opt_in_blacklist_overlap_and_checkpoint_binding(self):
        for overrides in (
            {"allow_external_paths": False}, {"allow_external_paths": 1},
            {"project_dir": self.project.anchor}, {"project_dir": str(Path.home())},
            {"project_dir": str(WORKDIR / "agentbridge")}, {"project_dir": str(WORKDIR / "runs")},
            {"output_dir": str(self.output / ".git" / "logs")},
            {"output_dir": str(Path(self.project.anchor) / "Windows" / "logs")},
            {"output_dir": str(Path(self.project.anchor) / "Program Files" / "logs")},
            {"goal_file": "../.ssh/id_rsa"}, {"readonly_inputs": ["../project/answer.txt"]},
            {"readonly_inputs": ["../runs"]}, {"verification_dir": "../project"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.spec(**overrides)
        state = self.run_models(MockModels(self), pause_after="baseline_verify")
        files = RunFiles(Path(state["run_dir"]), allow_external=True)
        state["output_dir"] = str(self.project)
        files.write("state.json", json.dumps(state))
        with self.assertRaisesRegex(IntegrityError, "does not match"):
            framework.status(files.directory)
        unrecognized = self.output / uuid4().hex
        unrecognized.mkdir()
        with self.assertRaises(OSError):
            framework.resume(unrecognized, runner=local_only)
        self.assertFalse((unrecognized / "run.lock").exists())

    def test_sol_cannot_change_explicit_readonly_file_even_if_glob_matches(self):
        def tamper(_cwd):
            self.critical.write_text("changed sample", encoding="utf-8")
            self.set_answer(42)
            return "changed input"

        mock = MockModels(self, neon_replies=[plan()], sol_replies=[tamper])
        state = self.run_models(mock, file_policy={"ignore_globs": ["*.tmp"], "max_full_hash_bytes": 8})
        self.assertEqual(state["current_state"], "STOPPED_PERMISSION_OR_TAMPER")
        self.assertIn("readonly input", state["stop_reason"])
        self.assertEqual(state["readonly_manifest"][str(self.critical)]["critical.tmp"]["integrity_mode"],
                         "content_and_hash")
        self.assertEqual(mock.roles(), ["neon", "sol"])

    def test_hardlink_is_rejected_even_under_ignored_directory(self):
        folder = self.project / "node_modules"
        folder.mkdir()
        os.link(self.critical, folder / "linked.tmp")
        spec = self.spec(readonly_inputs=[])
        with self.assertRaisesRegex(IntegrityError, "unlinked"):
            spec.snapshot_project()


class FilePolicyTests(Fixture):
    def test_mixed_project_changes_record_integrity_modes_and_diff(self):
        files = {"sample.bin": b"\0" * 500, "dataset.bin": b"x" * 2200,
                 "forced.bin": b"y" * 2300, "cache.tmp": b"ignored"}
        for name, value in files.items():
            (self.project / name).write_bytes(value)

        def update(cwd):
            for name, value in files.items():
                (cwd / name).write_bytes(value + b"!")
            return self.fix(cwd)

        mock = MockModels(self, neon_replies=[plan()], sol_replies=[update])
        state = self.run_models(mock, pause_after="sol_exec", file_policy={
            "max_text_diff_bytes": 32, "max_full_hash_bytes": 2048, "stream_hash_globs": ["forced.bin"]})
        self.assertEqual(state["current_state"], "PAUSED", state["stop_reason"])
        manifest = state["project_manifest"]
        self.assertEqual(manifest["answer.txt"]["integrity_mode"], "content_and_hash")
        self.assertEqual(manifest["cache.tmp"]["integrity_mode"], "ignored")
        for name in ("sample.bin", "forced.bin"):
            self.assertEqual(manifest[name]["integrity_mode"], "stream_hash_only")
            self.assertEqual(manifest[name]["sha256"], file_sha256(self.project / name))
        self.assertEqual(manifest["dataset.bin"]["integrity_mode"], "metadata_only")
        self.assertNotIn("sha256", manifest["dataset.bin"])
        self.assertIn("mtime_ns", manifest["dataset.bin"])
        execution = state["rounds"]["1"]["executions"][0]
        self.assertNotIn("cache.tmp", execution["changes"]["modified"])
        diff = (Path(state["run_dir"]) / execution["patch_file"]).read_text(encoding="utf-8")
        self.assertIn("--- a/answer.txt", diff)
        self.assertIn("+42", diff)
        self.assertIn("未做内容完整性校验", diff)
        self.assertIn("stream_hash_only: SHA-256 / size changed", diff)

    def test_patch_bound_marks_utf8_diff_truncated(self):
        def update(cwd):
            (cwd / "answer.txt").write_text("试" * 2000, encoding="utf-8")
            return "large edit"

        state = self.run_models(MockModels(self, neon_replies=[plan()], sol_replies=[update]),
                                pause_after="sol_exec", file_policy={"max_patch_bytes": 128})
        execution = state["rounds"]["1"]["executions"][0]
        patch_file = Path(state["run_dir"]) / execution["patch_file"]
        self.assertTrue(execution["patch_truncated"])
        self.assertLessEqual(patch_file.stat().st_size, 128)
        self.assertIn("patch truncated", patch_file.read_text(encoding="utf-8"))
        self.assertFalse((patch_file.parent / ".sol-before").exists())

    def test_verification_file_and_config_always_hash_even_when_glob_matches(self):
        check = self.verification / "fixed.tmp"
        check.write_text("old", encoding="utf-8")
        mock = MockModels(self)
        state = self.run_models(mock, pause_after="baseline_verify", file_policy={"ignore_globs": ["*"]})
        entry = state["verification_manifest"]["fixed.tmp"]
        self.assertEqual(entry["integrity_mode"], "stream_hash_only")
        check.write_text("new", encoding="utf-8")
        os.utime(check, ns=(entry["mtime_ns"], entry["mtime_ns"]))
        state = framework.resume(state["run_dir"], runner=mock)
        self.assertEqual(state["current_state"], "STOPPED_PERMISSION_OR_TAMPER")
        self.assertIn("SHA-256", state["stop_reason"])

    def test_disk_capture_bounds_audits_and_parses_untruncated_protocol(self):
        measure = self.verification / "measure.py"
        with measure.open("a", encoding="utf-8") as stream:
            stream.write("print('x' * 80000, 'REQUIRED-MIDDLE', 'y' * 80000)\n"
                         "print('z' * 140000, file=sys.stderr)\n")
        data = self.config(file_policy={"max_audit_stream_bytes": 120, "max_context_chars": 1200})
        data["verifiers"][0]["must_contain"].append("REQUIRED-MIDDLE")
        spec = self.spec(data)

        def local_neon(_path, _timeout, _prompt, conversation):
            reply = neon() if conversation else plan()
            # Build large noise at runtime to keep the command line short.
            script = "import sys; print('x'*85000); print(sys.argv[1]); print('y'*85000)"
            return [sys.executable, "-I", "-c", script, json.dumps({
                "status": "success", "conversation_id": "local-neon", "response": reply})]

        def local_sol(*_):
            script = ("import json; from pathlib import Path; "
                      "Path('answer.txt').write_text('42'); "
                      "print(json.dumps({'type':'thread.started','thread_id':'local-sol'})); "
                      "[print(json.dumps({'type':'noise','text':'x'*1024})) for _ in range(170)]; "
                      "print(json.dumps({'type':'item.completed','item':"
                      "{'type':'agent_message','text':'actual result'}})); "
                      "print(json.dumps({'type':'turn.completed'}))")
            return [sys.executable, "-I", "-c", script]

        with (patch("agentbridge.framework.neon_command", side_effect=local_neon),
              patch("agentbridge.framework.sol_command", side_effect=local_sol),
              patch("agentbridge.process.MAX_PROTOCOL_RECORD_BYTES", 4096)):
            state = framework.start(spec)
        self.assertEqual(state["current_state"], "COMPLETED_PASS", state["stop_reason"])
        self.assertEqual(state["sol_thread_id"], "local-sol")
        run = Path(state["run_dir"])
        for path in [*run.glob("model_*.json"), run / "baseline_verify.json", run / "round_1_verify.json"]:
            report = json.loads(path.read_text(encoding="utf-8"))
            streams = report if path.name.startswith("model_") else report["results"][0]
            for channel in ("stdout", "stderr"):
                output = streams[channel]
                if channel == "stderr" and path.name.startswith("model_"):
                    self.assertEqual(output, "")
                else:
                    self.assertTrue(output["truncated"])
                    self.assertGreater(output["original_bytes"], 100000)
                    self.assertLessEqual(len(output["excerpt"].encode("utf-8")), 120)
        self.assertEqual(state["baseline"]["audit_file"], "baseline_verify.json")
        for result in state["verification"]["results"]:
            self.assertNotIn("stdout", result)
            self.assertNotIn("stderr", result)

    def test_sealed_artifact_is_not_rehashed_at_later_checkpoints(self):
        with (self.verification / "measure.py").open("a", encoding="utf-8") as stream:
            stream.write("Path('history.bin').write_bytes(b'x' * 70000)\n")
        mock = MockModels(self, neon_replies=[plan(), neon()], sol_replies=[self.fix])
        state = self.run_models(mock, pause_after="baseline_verify")
        historical = Path(state["baseline"]["artifacts_dir"]) / "history.bin"
        key = "artifacts/baseline_verify/history.bin"
        self.assertEqual(state["audit_index"][key]["sha256"], file_sha256(historical))
        with patch("agentbridge.iteration_files.file_sha256", wraps=file_sha256) as hashed:
            state = framework.resume(state["run_dir"], runner=mock)
        self.assertEqual(state["current_state"], "COMPLETED_PASS", state["stop_reason"])
        self.assertNotIn(historical, [args[0] for args, _ in hashed.call_args_list])


class HumanResumeTests(Fixture):
    def test_legacy_paused_checkpoint_upgrades_with_original_contract_hash(self):
        mock = MockModels(self, neon_replies=[plan(), neon()], sol_replies=[self.fix])
        state = self.run_models(mock, pause_after="baseline_verify")
        files = RunFiles(Path(state["run_dir"]))
        config = state["config"]
        for key in ("allow_external_paths", "readonly_inputs", "file_policy"):
            del config[key]
        state["verification_manifest"] = digests(state["verification_manifest"])
        state["project_manifest"] = digests(state["project_manifest"])
        del state["audit_index"]
        old_contract = {**config, "limits": {key: config["limits"][key] for key in
                                           ("timeout_s", "max_plan_revisions")}}
        old_hash = sha256(json_text({"spec": old_contract, "verification_manifest": state["verification_manifest"]})
                          .encode("utf-8")).hexdigest()
        state["contract_hash"] = old_hash
        files.write("state.json", json_text(state))
        final = framework.resume(files.directory, runner=mock)
        self.assertEqual(final["current_state"], "COMPLETED_PASS", final["stop_reason"])
        self.assertEqual(final["locked_contract_hash"], old_hash)
        self.assertEqual(final["completed_steps"].count("baseline_verify"), 1)

    def test_neon_human_stop_resumes_same_plan_session_without_replay(self):
        mock = MockModels(self, neon_replies=[neon("NEED_HUMAN"), plan(), neon()], sol_replies=[self.fix])
        original = self.run_models(mock)
        self.assertEqual(original["current_state"], "STOPPED_NEED_HUMAN")
        decision = self.root / "decision.json"
        decision.write_text(json.dumps({"note": "Use the original measurement", "reason": "Confirmed scope"}),
                            encoding="utf-8")
        run = original["run_dir"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["resume", "--run-dir", run, "--decision-file", str(decision)], runner=mock), 0)
        final = framework.status(run, full=True)
        self.assertEqual(final["completed_steps"][:len(original["completed_steps"])], original["completed_steps"])
        self.assertEqual(final["neon_conversation_id"], original["neon_conversation_id"])
        self.assertIn("--conversation", mock.calls[1][1])
        self.assertIn("Use the original measurement", mock.prompts("neon")[1])
        self.assertEqual(final["human_decisions"][0]["reason"], "Confirmed scope")

    def test_scientific_guidance_keeps_sessions_and_old_evidence(self):
        mock = MockModels(self, neon_replies=[plan(), neon(), neon()], sol_replies=[self.fix],
                          astra_replies=[astra(), verdict("INSUFFICIENT_EVIDENCE", "INSUFFICIENT_EVIDENCE"),
                                         verdict()])
        original = self.run_models(mock, science=True)
        run = original["run_dir"]
        final = framework.resume(run, runner=mock, human_note="Use the locked measurement and scope.")
        self.assertEqual(final["current_state"], "COMPLETED_PASS", final["stop_reason"])
        self.assertEqual(final["completed_steps"][:len(original["completed_steps"])], original["completed_steps"])
        for key in ("neon_conversation_id", "sol_thread_id", "astra_thread_id"):
            self.assertEqual(final[key], original[key])
        self.assertIn("Use the locked measurement", mock.prompts("neon")[-1])
        self.assertEqual(mock.roles().count("sol"), 1)

    def test_retry_pending_sol_is_explicit_and_counts_new_attempt(self):
        def interrupted(_cwd):
            self.set_answer(17)
            raise KeyboardInterrupt

        mock = MockModels(self, neon_replies=[plan(), neon()], sol_replies=[interrupted, self.fix])
        state = self.run_models(mock)
        run = state["run_dir"]
        status = framework.status(run)
        self.assertEqual(status["active_role"], "human")
        self.assertTrue(any("project" in text for text in status["pending_check_items"]))
        self.assertEqual(len(status["next_step"]["commands"]), 2)
        final = framework.resume(run, runner=mock, resolve_pending="retry", human_note="Complete the edit")
        self.assertEqual(final["current_state"], "COMPLETED_PASS", final["stop_reason"])
        self.assertEqual(final["sol_attempts"], 2)
        self.assertTrue(final["rounds"]["1"]["executions"][0]["interrupted"])
        self.assertIn("Complete the edit", mock.prompts("sol")[-1])
        self.assertEqual(final["human_decisions"][0]["action"], "retry")

    def test_reverify_pending_sol_uses_patch_and_cannot_skip_failing_acceptance(self):
        def interrupted(_cwd):
            self.set_answer(17)
            raise KeyboardInterrupt

        mock = MockModels(self, neon_replies=[plan(), neon()], sol_replies=[interrupted])
        state = self.run_models(mock)
        run = state["run_dir"]
        final = framework.resume(run, resolve_pending="reverify", human_reason="Changes are on disk", runner=mock)
        self.assertEqual(final["current_state"], "STOPPED_NEED_HUMAN", final["stop_reason"])
        self.assertFalse(final["verification"]["all_passed"])
        self.assertEqual(mock.roles(), ["neon", "sol", "neon"])
        diff = Path(framework.status(run)["logs"]["latest_diff"])
        self.assertIn("+17", diff.read_text(encoding="utf-8"))
        self.assertEqual(final["completed_steps"].count("round_1_sol_exec"), 1)
        self.assertEqual(final["human_decisions"][-1]["affected_steps"], ["round_1_sol_exec", "ROUND_VERIFY"])

    def test_retry_interrupted_verifier_preserves_partial_artifacts(self):
        def interrupted(argv, *, cwd, **_):
            self.assertEqual(argv[0], sys.executable)
            (cwd / "partial.txt").write_text("partial", encoding="utf-8")
            raise KeyboardInterrupt

        state = framework.start(self.spec(), runner=interrupted)
        self.assertEqual(state["pending_step"]["step"], "baseline_verify")
        with self.assertRaisesRegex(ValueError, "only for interrupted Sol"):
            framework.resume(state["run_dir"], runner=local_only, resolve_pending="reverify")
        final = framework.resume(state["run_dir"], runner=local_only, resolve_pending="retry",
                                 pause_after="baseline_verify")
        self.assertEqual(final["current_state"], "PAUSED", final["stop_reason"])
        partial = Path(final["human_decisions"][0]["interrupted_evidence"]["artifacts_dir"])
        self.assertEqual((partial / "partial.txt").read_text(encoding="utf-8"), "partial")
        self.assertNotEqual(str(partial), final["baseline"]["artifacts_dir"])
        self.assertIn("baseline_verify", final["completed_steps"])

    def test_revise_creates_child_runs_baseline_and_astra_and_preserves_old_failure(self):
        mock = MockModels(self, neon_replies=[plan(), neon()], sol_replies=["no change"])
        old = self.run_models(mock)
        old_dir = Path(old["run_dir"])
        old_bytes = {file.name: file.read_bytes() for file in old_dir.iterdir() if file.is_file()}
        config = self.config(science=True, goal="Test the revised scientific hypothesis")
        config["verifiers"][1]["rules"][0]["expected"] = 0
        revised = self.root / "new_project.json"
        revised.write_text(json.dumps(config), encoding="utf-8")
        new_models = MockModels(self, neon_replies=[plan()], astra_replies=[astra()])
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = main(["revise", "--run-dir", str(old_dir), "--config", str(revised), "--reason",
                         "Change the comparison hypothesis", "--pause-after", "astra_contract"], runner=new_models)
        self.assertEqual(code, 2, stdout.getvalue())
        new = framework.status(json.loads(stdout.getvalue())["run_dir"], full=True)
        self.assertEqual(new["contract_version"], 2)
        self.assertEqual(new["parent_run_id"], old["run_id"])
        self.assertEqual(new["parent_contract_hash"], old["contract_hash"])
        self.assertFalse(new["inherited_evidence"]["verification"]["all_passed"])
        self.assertTrue(new["baseline"]["all_passed"])
        self.assertIn("goal", new["contract_diff"])
        self.assertIn("verifiers", new["contract_diff"])
        self.assertIn("ASTRA_CONTRACT_REVIEW", new["affected_steps"])
        self.assertEqual(new_models.roles(), ["neon", "astra"])
        self.assertEqual(new["locked_contract_hash"], new["contract_hash"])
        for name, content in old_bytes.items():
            self.assertEqual((old_dir / name).read_bytes(), content)

    def test_project_budgets_and_model_cannot_adjust_them(self):
        large = {"max_sol_attempts": 10, "max_model_calls": 30, "max_astra_calls": 12, "max_plan_revisions": 8,
                 "max_consecutive_failures": 7, "timeout_s": 480, "max_total_seconds": 4000}
        self.assertEqual(self.spec(limits=large).limits, large)
        for key, value in (("max_sol_attempts", 0), ("max_plan_revisions", True), ("timeout_s", float("inf")),
                           ("max_total_seconds", -1), ("max_model_calls", 1)):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                ProjectSpec.validate_limits({key: value})
        mock = MockModels(self, neon_replies=[{**plan(), "limits": large}, neon()], sol_replies=[self.fix])
        old = self.run_models(mock, limits={"max_model_calls": 1, "max_sol_attempts": 1})
        self.assertEqual(old["current_state"], "STOPPED_BUDGET_EXCEEDED")
        self.assertEqual(old["config"]["limits"]["max_model_calls"], 1)
        self.assertEqual(old["budget_history"], [])
        with self.assertRaisesRegex(ValueError, "budget-reason"):
            framework.resume(old["run_dir"], runner=mock, max_model_calls=3)
        final = framework.resume(old["run_dir"], runner=mock, max_model_calls=3, budget_reason="Finish review")
        self.assertEqual(final["current_state"], "COMPLETED_PASS", final["stop_reason"])
        history = final["budget_history"][0]
        self.assertEqual((history["before"]["max_model_calls"], history["after"]["max_model_calls"]), (1, 3))
        self.assertEqual(history["reason"], "Finish review")
        events = [json.loads(line) for line in (Path(final["run_dir"]) / "events.jsonl").read_text(
            encoding="utf-8").splitlines()]
        self.assertEqual(next(event for event in events if event["event"] == "budget_adjusted")["timestamp"],
                         history["timestamp"])

    def test_status_cli_summary_and_full(self):
        mock = MockModels(self, neon_replies=[plan(), neon()], sol_replies=["no change"])
        state = self.run_models(mock)
        args = ["status", "--run-dir", state["run_dir"]]
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(args, runner=mock), 0)
        summary = json.loads(output.getvalue())
        self.assertTrue({"project_id", "goal", "contract_version", "parent_run_id", "active_role",
                         "latest_result", "next_step", "budgets", "sessions", "logs",
                         "human_decisions", "budget_history"}.issubset(summary))
        self.assertNotIn("pending_check_items", summary)
        self.assertEqual(summary["active_role"], "human")
        self.assertTrue(summary["latest_result"]["model"]["overridden"])
        self.assertFalse(summary["latest_result"]["verification"]["all_passed"])
        self.assertTrue(Path(summary["logs"]["latest_verification"]).is_file())
        self.assertTrue(Path(summary["logs"]["latest_model_audit"]).is_file())
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([*args, "--full"], runner=mock), 0)
        self.assertEqual(json.loads(output.getvalue()), state)


class LennardJonesExampleTests(Fixture):
    def test_baseline_fails_and_both_independent_repairs_are_required(self):
        folder = self.root / "lj_physics"
        shutil.copytree(WORKDIR / "examples" / "lj_physics", folder)
        spec = ProjectSpec.from_file(folder / "project.json", output_dir=self.output)
        base = self.run_checks(spec)
        self.assertEqual(base.failed_verifiers, ["lj_tests", "physics_thresholds"], base.results)
        self.assertEqual(base.results[0]["tests_run"], 8)
        self.assertGreater(base.metrics_snapshots["physics_thresholds"]["forces"]["minimum_force_norm"], 1e-3)
        model = folder / "project" / "lj_model.py"
        source = model.read_text(encoding="utf-8")
        force_fix = source.replace("(sr12 - sr6) / r2", "(2 * sr12 - sr6) / r2")
        model.write_text(force_fix, encoding="utf-8")
        force_only = self.run_checks(spec)
        ratio = force_only.metrics_snapshots["physics_thresholds"]["nve"]["coarse_to_fine_ratio"]
        self.assertAlmostEqual(ratio, 2.0, delta=0.05)
        self.assertEqual(force_only.failed_verifiers, ["lj_tests", "physics_thresholds"])
        model.write_text(force_fix.replace("zip(half_step, forces)", "zip(half_step, new_forces)"), encoding="utf-8")
        fixed = self.run_checks(spec)
        self.assertTrue(fixed.all_passed, fixed.results)
        ratio = fixed.metrics_snapshots["physics_thresholds"]["nve"]["coarse_to_fine_ratio"]
        self.assertAlmostEqual(ratio, 4.0, delta=0.05)


class AstraFirstPlanningTests(Fixture):
    def _astra_first_agents(self):
        return {"require_astra": True, "planning_order": "astra_first"}

    def test_validation_rejects_invalid_or_astra_disabled_planning_order(self):
        with self.assertRaisesRegex(ValueError, "planning_order"):
            self.spec(self.config(agents={"require_astra": False, "planning_order": "astra_first"}))
        with self.assertRaisesRegex(ValueError, "planning_order"):
            self.spec(self.config(science=True, agents={"require_astra": True, "planning_order": "unknown"}))

    def test_contract_hash_distinguishes_astra_first_and_preserves_default_hash(self):
        default_spec = self.spec(self.config(science=True))
        explicit_default = self.spec(self.config(science=True, agents={"require_astra": True, "planning_order": "neon_first"}))
        astra_first_spec = self.spec(self.config(science=True, agents=self._astra_first_agents()))
        self.assertEqual(
            default_spec.contract_hash(default_spec.verification_manifest),
            explicit_default.contract_hash(explicit_default.verification_manifest),
        )
        self.assertNotEqual(
            default_spec.contract_hash(default_spec.verification_manifest),
            astra_first_spec.contract_hash(astra_first_spec.verification_manifest),
        )

    def test_astra_first_happy_path_and_pause_checkpoints(self):
        mock = MockModels(
            self,
            astra_replies=[astra("APPROVE", guidance="Use exact C2 switching."), verdict()],
            neon_replies=[plan("Follow Astra C2 guidance"), neon()],
            sol_replies=[self.fix],
        )
        state = self.run_models(mock, science=True, agents=self._astra_first_agents(), pause_after="baseline_verify")
        self.assertEqual(state["current_state"], "PAUSED")
        self.assertEqual(state["resume_state"], "ASTRA_CONTRACT_REVIEW")
        self.assertEqual(framework.status(state["run_dir"])["active_role"], "astra")
        self.assertEqual(mock.roles(), [])

        state = framework.resume(state["run_dir"], pause_after="astra_contract", runner=mock)
        self.assertEqual(state["current_state"], "PAUSED")
        self.assertEqual(state["resume_state"], "NEON_PLAN")
        self.assertIsNone(state["locked_contract_hash"])
        self.assertEqual(mock.roles(), ["astra"])

        state = framework.resume(state["run_dir"], pause_after="neon_plan", runner=mock)
        self.assertEqual(state["current_state"], "PAUSED")
        self.assertEqual(state["resume_state"], "ROUND_SOL_EXEC")
        self.assertEqual(state["locked_contract_hash"], state["contract_hash"])
        self.assertEqual(mock.roles(), ["astra", "neon"])
        self.assertIn("Use exact C2 switching.", mock.prompts("neon")[0])

        final = framework.resume(state["run_dir"], runner=mock)
        self.assertEqual(final["current_state"], "COMPLETED_PASS", final["stop_reason"])
        self.assertEqual(mock.roles(), ["astra", "neon", "sol", "neon", "astra"])
        self.assertIn("Follow Astra C2 guidance", mock.prompts("sol")[0])
        self.assertIn("Use exact C2 switching.", mock.prompts("sol")[0])

    def test_astra_first_initial_revise_requires_explicit_astra_approval_before_lock(self):
        mock = MockModels(
            self,
            astra_replies=[astra("REVISE", guidance="Tighten numerical guardrails."), astra("APPROVE"), verdict()],
            neon_replies=[plan("Plan with tightened guardrails"), neon()],
            sol_replies=[self.fix],
        )
        state = self.run_models(mock, science=True, agents=self._astra_first_agents(), pause_after="neon_plan")
        self.assertEqual(state["current_state"], "PAUSED")
        self.assertEqual(state["resume_state"], "ASTRA_CONTRACT_REVIEW")
        self.assertIsNone(state["locked_contract_hash"])
        self.assertEqual(mock.roles(), ["astra", "neon"])

        final = framework.resume(state["run_dir"], runner=mock)
        self.assertEqual(final["current_state"], "COMPLETED_PASS", final["stop_reason"])
        self.assertEqual(final["locked_contract_hash"], final["contract_hash"])
        self.assertEqual(mock.roles(), ["astra", "neon", "astra", "sol", "neon", "astra"])

    def test_astra_first_neon_escalate_respects_plan_revision_cap_and_resumes(self):
        mock = MockModels(
            self,
            astra_replies=[
                astra("APPROVE", guidance="Initial framing"),
                astra("APPROVE", guidance="Clarified framing"),
                astra("APPROVE", guidance="Final framing"),
                verdict(),
            ],
            neon_replies=[
                neon("ESCALATE_ASTRA", category="scientific", plan="Need first clarification"),
                neon("ESCALATE_ASTRA", category="scientific", plan="Need second clarification"),
                plan("Finalized engineering plan"),
                neon(),
            ],
            sol_replies=[self.fix],
        )
        stopped = self.run_models(
            mock,
            science=True,
            agents=self._astra_first_agents(),
            limits={"max_plan_revisions": 1},
        )
        self.assertEqual(stopped["current_state"], "STOPPED_BUDGET_EXCEEDED")
        self.assertIn("revision limit", stopped["stop_reason"])
        self.assertIsNone(stopped["locked_contract_hash"])
        self.assertEqual(mock.roles(), ["astra", "neon", "astra", "neon"])

        final = framework.resume(
            stopped["run_dir"],
            runner=mock,
            max_plan_revisions=2,
            budget_reason="Allow second Neon-to-Astra scientific clarification.",
        )
        self.assertEqual(final["current_state"], "COMPLETED_PASS", final["stop_reason"])
        self.assertEqual(final["plan_revisions"], 2)
        self.assertEqual(mock.roles(), ["astra", "neon", "astra", "neon", "astra", "neon", "sol", "neon", "astra"])
        self.assertIn("Final framing", mock.prompts("neon")[2])

    def test_astra_first_human_stop_resumes_at_astra_then_neon(self):
        mock = MockModels(
            self,
            astra_replies=[astra("NEED_HUMAN"), astra("APPROVE", guidance="Proceed with confirmed scope"), verdict()],
            neon_replies=[plan("Confirmed plan"), neon()],
            sol_replies=[self.fix],
        )
        stopped = self.run_models(mock, science=True, agents=self._astra_first_agents())
        self.assertEqual(stopped["current_state"], "STOPPED_NEED_HUMAN")
        self.assertEqual(stopped["resume_state"], "ASTRA_CONTRACT_REVIEW")
        final = framework.resume(
            stopped["run_dir"],
            runner=mock,
            human_note="Keep the frozen baseline tolerances.",
            human_reason="Confirmed by researcher.",
        )
        self.assertEqual(final["current_state"], "COMPLETED_PASS", final["stop_reason"])
        self.assertEqual(mock.roles(), ["astra", "astra", "neon", "sol", "neon", "astra"])
        self.assertIn("Keep the frozen baseline tolerances.", mock.prompts("astra")[1])
