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
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from agentbridge import framework
from agentbridge.bridge import DEFAULT_AGY, DEFAULT_CODEX, WORKDIR
from agentbridge.iteration_files import IntegrityError, RunFiles
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
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=WORKDIR, prefix="framework_test_")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
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
            {"limits": {"max_sol_attempts": 4}}, {"limits": {"max_astra_calls": 5}},
            {"limits": {"max_model_calls": 13}}, {"limits": {"max_plan_revisions": 3}},
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
        state = framework.resume(run_dir, pause_after="sol_exec", runner=mock, max_model_calls=7)
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
        state = self.run_models(mock, limits={"max_model_calls": 1})
        self.assertEqual(state["current_state"], "PAUSED")
        state = framework.resume(state["run_dir"], runner=mock)
        self.assertEqual(state["current_state"], "STOPPED_BUDGET_EXCEEDED")
        self.assertEqual(mock.roles(), ["neon"])
        state = framework.resume(state["run_dir"], runner=mock, max_model_calls=3)
        self.assertEqual(state["current_state"], "COMPLETED_PASS", state["stop_reason"])

    def test_astra_budget_blocks_acceptance_until_budget_is_raised(self):
        mock = MockModels(self, neon_replies=[plan(), neon()], astra_replies=[astra(), verdict()],
                          sol_replies=[self.fix])
        state = self.run_models(mock, science=True, limits={"max_astra_calls": 1})
        self.assertEqual(state["current_state"], "STOPPED_BUDGET_EXCEEDED")
        self.assertEqual(state["resume_state"], "ASTRA_SCIENTIFIC_ACCEPT")
        self.assertTrue(state["verification"]["all_passed"])
        self.assertEqual(mock.roles(), ["neon", "astra", "sol", "neon"])
        state = framework.resume(state["run_dir"], runner=mock, max_astra_calls=2)
        self.assertEqual(state["current_state"], "COMPLETED_PASS", state["stop_reason"])
        self.assertEqual(mock.roles(), ["neon", "astra", "sol", "neon", "astra"])

    def test_plan_revision_cap_keeps_contract_unlocked_and_blocks_sol(self):
        mock = MockModels(self, neon_replies=[plan()], astra_replies=[astra("REVISE")])
        state = self.run_models(mock, science=True, limits={"max_plan_revisions": 0})
        self.assertEqual(state["current_state"], "STOPPED_NEED_HUMAN")
        self.assertIn("revision limit", state["stop_reason"])
        self.assertIsNone(state["locked_contract_hash"])
        self.assertEqual(mock.roles(), ["neon", "astra"])

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
