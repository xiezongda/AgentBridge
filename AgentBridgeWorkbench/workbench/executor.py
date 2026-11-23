"""Execute one workflow snapshot and record its result as an immutable turn."""

import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import deepcopy
from subprocess import TimeoutExpired
from time import monotonic

from .graph import build_node_prompt, validate_workflow
from .models import WORKSPACE_DEFAULTS, normalize_workflow, redact_model_card


def _incoming(workflow):
    incoming = {node["id"]: [] for node in workflow["nodes"]}
    for edge in workflow["edges"]:
        incoming[edge["target"]].append(edge["source"])
    return incoming


def _milliseconds(start):
    return round((monotonic() - start) * 1000)


def _error_message(exc):
    if isinstance(exc, TimeoutExpired):
        return f"Model timed out after {exc.timeout:g} seconds."
    return str(exc) or type(exc).__name__


def _workspace_overrides(settings):
    overrides = {key: settings[key] for key in WORKSPACE_DEFAULTS if settings.get(key) is not None}
    if settings.get("sandbox_mode_override") is not None:
        overrides["sandbox_mode"] = settings["sandbox_mode_override"]
    return overrides


class _TurnExecution:
    def __init__(self, workflow, cards, user_message, history, registry, validation):
        self.workflow = workflow
        self.cards = cards
        self.user_message = user_message
        self.history = history
        self.registry = registry
        self.schedule = workflow["schedule"]
        self.deadline = monotonic() + self.schedule["turn_timeout_s"]
        self.incoming = _incoming(workflow)
        self.nodes = {node["id"]: node for node in workflow["nodes"]}
        workspace = _workspace_overrides(workflow.get("workspace") or {})
        self.model_settings = {}
        self.effective_workspaces = {}
        for node_id, node in self.nodes.items():
            if node["type"] != "model":
                continue
            config = node["config"]
            card = self.cards[config["model_card_id"]]
            settings = {**card, **workspace, **_workspace_overrides(config)}
            self.model_settings[node_id] = settings
            self.effective_workspaces[node_id] = deepcopy({
                key: settings[key] if settings.get(key) is not None else default
                for key, default in WORKSPACE_DEFAULTS.items()
            })
        active = set(validation["active_node_ids"])
        self.order = [
            node_id for layer in validation["layers"] for node_id in layer if node_id in active
        ]
        self.outputs = {}
        self.runs = {}
        self.attempts = {}
        self.calls = 0
        self.lock = threading.Lock()
        self.aborted = False
        self.stop_on_error = self.schedule["failure_policy"] == "stop_on_error"

    def _run_record(self, node_id, status, *, start=None, output="", error=None, **details):
        node = self.nodes[node_id]
        card_id = node["config"]["model_card_id"] if node["type"] == "model" else None
        workspace = self.effective_workspaces.get(node_id)
        if workspace is not None:
            details = {**workspace, "effective_workspace": workspace, **details}
        return {
            "node_id": node_id,
            "node_type": node["type"],
            "label": node.get("label") or node_id,
            "model_card_id": card_id,
            "model_id": self.cards.get(card_id, {}).get("model_id"),
            "status": status,
            "latency_ms": _milliseconds(start) if start is not None else 0,
            "output_text": output,
            "error_message": error,
            **details,
        }

    def _final_output(self, node_id):
        return "\n\n".join(
            self.outputs[source] for source in self.incoming[node_id] if self.outputs.get(source)
        )

    def _run_non_model(self, node_id):
        node = self.nodes[node_id]
        if node["type"] == "user_input":
            output = self.user_message
        elif node["type"] == "context_input":
            output = node["config"].get("text", "")
        else:
            output = self._final_output(node_id)
        return self._run_record(node_id, "success", output=output)

    def _reserve_call(self, timeout_s, attempts):
        with self.lock:
            if self.aborted:
                return None, "Turn execution stopped."
            remaining = self.deadline - monotonic()
            if remaining <= 0:
                return None, "Turn timeout exceeded."
            if self.calls >= self.schedule["max_calls_per_turn"]:
                return None, "Model call budget exhausted (max_calls_per_turn)."
            effective_timeout = min(timeout_s, remaining)
            self.calls += 1
            attempts.append({
                "attempt": len(attempts) + 1,
                "status": "running",
                "timeout_s": effective_timeout,
            })
            return effective_timeout, None

    def _run_model(self, node_id, upstream):
        start = monotonic()
        node = self.nodes[node_id]
        config = node["config"]
        card = self.model_settings[node_id]
        workspace = self.effective_workspaces[node_id]
        effort = config.get("effort_override") or card["effort"]
        schedule_timeout = self.schedule["default_node_timeout_s"]
        timeout_s = config.get("timeout_s_override") or min(
            schedule_timeout, card.get("timeout_s", schedule_timeout)
        )
        sandbox = workspace["sandbox_mode"]
        retries = config.get("max_retries_override")
        if retries is None:
            retries = self.schedule["default_max_retries"]
        attempts = self.attempts[node_id]
        error = None
        details = {
            "effective_effort": effort,
            "effort": effort,
            "sandbox_mode": sandbox,
            "timeout_s": timeout_s,
            "rendered_prompt": "",
        }
        try:
            prompt = build_node_prompt(
                node, self.workflow, upstream, self.history,
                max_context_chars=self.schedule["max_context_chars"], provider=card["provider"],
            )
            details["rendered_prompt"] = prompt["prompt"]
            for _ in range(retries + 1):
                effective_timeout, budget_error = self._reserve_call(timeout_s, attempts)
                if budget_error is not None:
                    error = f"{error} {budget_error}" if error else budget_error
                    break
                call_start = monotonic()
                try:
                    result = self.registry.invoke(
                        card, prompt["prompt"], effort=effort, timeout_s=effective_timeout,
                        sandbox_mode=sandbox, workdir=workspace["workdir"], node_id=node_id,
                    )
                    output = result["output_text"]
                except Exception as exc:
                    error = _error_message(exc)
                    with self.lock:
                        attempts[-1].update(
                            status="failed", latency_ms=_milliseconds(call_start), error_message=error
                        )
                else:
                    with self.lock:
                        attempts[-1].update(status="success", latency_ms=_milliseconds(call_start))
                    return self._run_record(
                        node_id, "success", start=start, output=output,
                        session_id=result.get("session_id"), usage=result.get("usage"),
                        **details,
                    )
        except Exception as exc:
            error = _error_message(exc)
        return self._run_record(
            node_id, "failed", start=start, error=error, **details,
        )

    def _record(self, node_id, run):
        self.runs[node_id] = run
        if run["status"] == "success":
            self.outputs[node_id] = run["output_text"]

    def run(self):
        pending = set(self.order)
        running = {}
        failure = None
        stopped_by = None
        concurrency = self.schedule["max_concurrency"]
        pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="workbench-model")
        try:
            while pending or running:
                if monotonic() >= self.deadline:
                    stopped_by = "Turn timeout exceeded."
                    break
                for node_id in self.order:
                    if node_id not in pending or not all(
                        source in self.runs for source in self.incoming[node_id]
                    ):
                        continue
                    if self.nodes[node_id]["type"] == "model":
                        if len(running) >= concurrency:
                            continue
                        upstream = {
                            source: self.outputs[source] for source in self.incoming[node_id]
                            if source in self.outputs
                        }
                        with self.lock:
                            self.attempts[node_id] = []
                        running[pool.submit(self._run_model, node_id, upstream)] = (
                            node_id, monotonic()
                        )
                    else:
                        self._record(node_id, self._run_non_model(node_id))
                    pending.remove(node_id)
                if running:
                    done, _ = wait(
                        running, timeout=max(0, self.deadline - monotonic()),
                        return_when=FIRST_COMPLETED,
                    )
                    if not done:
                        stopped_by = "Turn timeout exceeded."
                        break
                    for future in list(running):
                        if future not in done:
                            continue
                        node_id, _ = running.pop(future)
                        run = future.result()
                        self._record(node_id, run)
                        if run["status"] == "failed" and failure is None:
                            failure = run["error_message"]
                    if failure is not None and self.stop_on_error:
                        stopped_by = failure
                        break
        finally:
            with self.lock:
                self.aborted = True
                attempts = deepcopy(self.attempts)
                calls = self.calls
            pool.shutdown(wait=False, cancel_futures=True)

        if stopped_by is not None:
            for _, (node_id, started) in running.items():
                active_attempts = attempts.get(node_id, [])
                for attempt in active_attempts:
                    if attempt["status"] == "running":
                        attempt.update(
                            status="failed", latency_ms=_milliseconds(started),
                            error_message=stopped_by,
                        )
                self.runs[node_id] = self._run_record(
                    node_id, "failed", start=started, error=stopped_by
                )
            for node_id in pending:
                self.runs[node_id] = self._run_record(node_id, "skipped", error=stopped_by)

        final_id = next(node_id for node_id in self.order if self.nodes[node_id]["type"] == "final_output")
        error = stopped_by or failure
        status = "completed"
        final_response = self.outputs.get(final_id, "")
        if error is not None:
            status = "failed" if self.stop_on_error else "partial"
            if not self.stop_on_error and final_id not in self.outputs:
                final_response = self._final_output(final_id)
        return {
            "node_runs": [
                {
                    **self.runs[node_id],
                    "attempts": len(attempts.get(node_id, [])),
                    "attempt_count": len(attempts.get(node_id, [])),
                    "attempt_log": attempts.get(node_id, []),
                }
                for node_id in self.order
            ],
            "total_model_calls": calls,
            "final_response": final_response,
            "status": status,
            "error_message": error,
        }


class WorkflowExecutor:
    def __init__(self, store, adapter_registry):
        self.store = store
        self.adapter_registry = adapter_registry

    def preview_prompt(self, conversation_id, workflow, node_id, sample_user_message):
        if not isinstance(sample_user_message, str):
            raise ValueError("sample_user_message must be a string.")
        if conversation_id is not None:
            conversation, cards = self.store.capture_run_context(conversation_id)
            history = conversation["turns"]
        else:
            cards = {card["id"]: card for card in self.store.list_model_cards()}
            history = []
        validation = validate_workflow(workflow, cards, require_executable=False)
        if not validation["valid"]:
            raise ValueError("\n".join(validation["errors"]))
        workflow = normalize_workflow(workflow)
        nodes = {node["id"]: node for node in workflow["nodes"]}
        if node_id not in nodes:
            raise KeyError(node_id)
        node = nodes[node_id]
        if node["type"] != "model":
            raise ValueError("Select a model node to preview its prompt.")
        card = cards.get(node.get("config", {}).get("model_card_id"), {})
        upstream = {}
        for source in _incoming(workflow)[node_id]:
            source_node = nodes[source]
            if source_node["type"] == "user_input":
                upstream[source] = sample_user_message
            elif source_node["type"] == "context_input":
                upstream[source] = source_node.get("config", {}).get("text", "")
            else:
                label = source_node.get("label") or source
                upstream[source] = f"[Preview output from {label}]"
        return build_node_prompt(
            node, workflow, upstream, history,
            max_context_chars=workflow["schedule"]["max_context_chars"],
            provider=card.get("provider"),
        )

    def run_turn(self, conversation_id, user_message):
        if not isinstance(user_message, str) or not user_message.strip():
            raise ValueError("A non-empty user message is required.")
        conversation, cards = self.store.capture_run_context(conversation_id)
        workflow = conversation["workflow"]
        validation = validate_workflow(workflow, cards)
        if not validation["valid"]:
            raise ValueError("\n".join(validation["errors"]))
        workflow = normalize_workflow(workflow)
        model_ids = dict.fromkeys(
            node.get("config", {}).get("model_card_id")
            for node in workflow["nodes"] if node["type"] == "model"
        )
        run_cards = {card_id: cards[card_id] for card_id in model_ids if card_id in cards}
        execution = _TurnExecution(
            workflow, run_cards, user_message, conversation["turns"],
            self.adapter_registry, validation,
        )
        return self.store.create_turn(conversation_id, {
            "user_message": user_message,
            "workflow_version": conversation["workflow_version"],
            "workflow_snapshot": workflow,
            "model_cards_snapshot": {card_id: redact_model_card(card) for card_id, card in run_cards.items()},
            **execution.run(),
        })
