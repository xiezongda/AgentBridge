"""Workbench records, defaults, and input normalization."""

from copy import deepcopy
import os
from pathlib import Path
import platform
import shutil
from urllib.parse import urlsplit


NODE_TYPES = frozenset({"user_input", "context_input", "model", "final_output"})
_CLI_COMMANDS = {"agy_cli": "agy", "codex_cli": "codex"}
_SUPPORTED_PROVIDERS = (*_CLI_COMMANDS, "openai_compat", "ollama")
PROVIDERS = frozenset(_SUPPORTED_PROVIDERS)
EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "ultra", "max"})
SANDBOX_MODES = frozenset({"read-only", "workspace-write", "danger-full-access"})
AGY_MODES = frozenset({"plan", "accept-edits", "auto"})
FAILURE_POLICIES = frozenset({"stop_on_error", "continue_on_error"})

WORKSPACE_DEFAULTS = {
    "workdir": None,
    "extra_dirs": [],
    "agy_project": None,
    "agy_mode": "plan",
    "agy_agent": None,
    "enable_plugins_and_skills": False,
    "codex_profile": None,
    "codex_trust": False,
    "extra_cli_config": [],
    "sandbox_mode": "read-only",
}

DEFAULT_SCHEDULE = {
    "max_concurrency": 2,
    "max_calls_per_turn": 12,
    "default_node_timeout_s": 900,
    "turn_timeout_s": 1800,
    "max_context_chars": 24000,
    "failure_policy": "stop_on_error",
    "default_max_retries": 0,
}

SCHEDULE_MINIMUMS = {
    "max_concurrency": 1,
    "max_calls_per_turn": 1,
    "default_node_timeout_s": 1,
    "turn_timeout_s": 1,
    "max_context_chars": 200,
    "default_max_retries": 0,
}

DEFAULT_NODE_CONFIGS = {
    "user_input": {},
    "context_input": {"text": ""},
    "model": {
        "model_card_id": "",
        "system_prompt": "",
        "include_history": True,
        "history_turns": 4,
        "input_order": [],
    },
    "final_output": {},
}

_MODEL_CONFIG_FIELDS = (
    *DEFAULT_NODE_CONFIGS["model"], "effort_override", "timeout_s_override",
    "max_retries_override", "sandbox_mode_override", *WORKSPACE_DEFAULTS,
)

_MODEL_CARD_SEEDS = [
    {
        "id": "neon",
        "name": "Neon",
        "provider": "agy_cli",
        "model_id": "gemini-4-neon-high",
        "effort": "high",
        "timeout_s": 300,
        "sandbox_mode": "read-only",
    },
    {
        "id": "sol",
        "name": "Sol",
        "provider": "codex_cli",
        "model_id": "gpt-6.2-sol",
        "effort": "high",
        "timeout_s": 600,
        "sandbox_mode": "read-only",
    },
    {
        "id": "astra",
        "name": "Astra",
        "provider": "codex_cli",
        "model_id": "gpt-6.2-astra",
        "effort": "ultra",
        "timeout_s": 900,
        "sandbox_mode": "read-only",
    },
]


def _node(node_id, node_type, label, x, y, **config):
    return {
        "id": node_id,
        "type": node_type,
        "label": label,
        "position": {"x": x, "y": y},
        "config": {**deepcopy(DEFAULT_NODE_CONFIGS[node_type]), **config},
    }


def _input():
    return _node("user_input", "user_input", "用户输入", 40, 140)


def _output(x):
    return _node("final_output", "final_output", "最终输出", x, 140)


def _template(template_id, name, description, nodes, connections):
    return {
        "id": template_id,
        "name": name,
        "description": description,
        "workflow": {
            "nodes": nodes,
            "edges": [
                {"id": f"{source}-{target}", "source": source, "target": target}
                for source, target in connections
            ],
            "schedule": deepcopy(DEFAULT_SCHEDULE),
        },
    }


DEFAULT_TEMPLATES = [
    _template("blank", "空白流程", "从用户输入和最终输出开始搭建流程。", [_input(), _output(560)], []),
    _template(
        "direct_sol", "Sol 直连", "把消息交给 Sol 并返回答复。",
        [_input(), _node("sol", "model", "Sol", 310, 140, model_card_id="sol"), _output(580)],
        [("user_input", "sol"), ("sol", "final_output")],
    ),
    _template(
        "draft_review", "起草与审阅", "Neon 起草，Astra 根据原始提问审阅并给出答复。",
        [
            _input(),
            _node("neon", "model", "Neon", 310, 30, model_card_id="neon",
                  system_prompt="Draft a response to the user's request for a reviewer."),
            _node("astra", "model", "Astra", 580, 140, model_card_id="astra",
                  system_prompt="Review the draft against the user's request and write the final response.",
                  input_order=["user_input", "neon"]),
            _output(850),
        ],
        [("user_input", "neon"), ("user_input", "astra"), ("neon", "astra"), ("astra", "final_output")],
    ),
    _template(
        "parallel_synth", "并行综合", "Neon 和 Sol 并行作答，Astra 综合两个结果。",
        [
            _input(),
            _node("neon", "model", "Neon", 310, 30, model_card_id="neon"),
            _node("sol", "model", "Sol", 310, 250, model_card_id="sol"),
            _node("astra", "model", "Astra", 580, 140, model_card_id="astra",
                  system_prompt="Synthesize the upstream responses into a single answer.",
                  input_order=["neon", "sol"]),
            _output(850),
        ],
        [
            ("user_input", "neon"), ("user_input", "sol"), ("neon", "astra"),
            ("sol", "astra"), ("astra", "final_output"),
        ],
    ),
]


def is_integer_at_least(value, minimum):
    return type(value) is int and value >= minimum


def has_choice(value, choices):
    return isinstance(value, str) and value in choices


def require_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


_CLI_ENV_VARS = {"agy_cli": "AGY_CLI_PATH", "codex_cli": "CODEX_CLI_PATH"}


def _existing_file(path):
    try:
        candidate = Path(path)
        return str(candidate) if candidate.is_file() else None
    except OSError:
        return None


def _newest_glob(root, pattern):
    try:
        base = Path(root)
        if not base.is_dir():
            return None
        matches = [p for p in base.glob(pattern) if p.is_file()]
        if not matches:
            return None
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return str(matches[0])
    except OSError:
        return None


def detect_cli_executable(provider):
    """Find a CLI via env override, PATH, or standard cross-platform install locations."""
    command = _CLI_COMMANDS.get(provider)
    if not command:
        return None

    env_var = _CLI_ENV_VARS.get(provider)
    env_value = os.environ.get(env_var, "").strip() if env_var else ""
    if env_value:
        resolved = _existing_file(env_value) or shutil.which(env_value)
        if resolved:
            return resolved

    found = shutil.which(command)
    if found:
        return found

    home = Path.home()
    local_app = os.environ.get("LOCALAPPDATA")
    roaming_app = os.environ.get("APPDATA")

    if provider == "codex_cli":
        if local_app:
            desktop_bin = _newest_glob(Path(local_app) / "OpenAI" / "Codex" / "bin", "*/codex.exe")
            if desktop_bin:
                return desktop_bin
        candidates = [
            *(
                (Path(roaming_app) / "npm" / "codex.cmd", Path(roaming_app) / "npm" / "codex.exe")
                if roaming_app else ()
            ),
            home / ".codex" / "bin" / "codex.exe",
            home / ".local" / "bin" / "codex",
            home / ".npm-global" / "bin" / "codex",
            Path("/Applications/Codex.app/Contents/Resources/codex"),
            Path("/opt/homebrew/bin/codex"),
            Path("/usr/local/bin/codex"),
        ]
    else:
        candidates = [
            *(
                (
                    Path(local_app) / "Antigravity" / "bin" / "agy.exe",
                    Path(local_app) / "Programs" / "Antigravity" / "agy.exe",
                )
                if local_app else ()
            ),
            *((Path(roaming_app) / "npm" / "agy.cmd",) if roaming_app else ()),
            home / ".antigravity" / "bin" / "agy.exe",
            home / ".local" / "bin" / "agy.exe",
            home / ".local" / "bin" / "agy",
            Path("/opt/homebrew/bin/agy"),
            Path("/usr/local/bin/agy"),
        ]

    for candidate in candidates:
        resolved = _existing_file(candidate)
        if resolved:
            return resolved
    return None


def environment_info():
    detected_clis = {}
    for provider, command in _CLI_COMMANDS.items():
        executable = detect_cli_executable(provider)
        detected_clis[provider] = {"command": executable or command, "available": executable is not None}
    return {
        "platform": platform.system(),
        "python_version": platform.python_version(),
        "detected_clis": detected_clis,
        "supported_providers": list(_SUPPORTED_PROVIDERS),
    }


def workspace_errors(settings, prefix):
    """Validate supplied values while allowing None to inherit a default."""
    if not isinstance(settings, dict):
        return [f"{prefix} must be an object"]
    errors = []
    for field in WORKSPACE_DEFAULTS:
        value = settings.get(field)
        if value is None:
            continue
        if field in ("extra_dirs", "extra_cli_config"):
            if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
                errors.append(f"{prefix}: {field} must be an array of non-empty strings")
        elif field in ("enable_plugins_and_skills", "codex_trust"):
            if type(value) is not bool:
                errors.append(f"{prefix}: {field} must be a boolean")
        elif field == "agy_mode":
            if not has_choice(value, AGY_MODES):
                errors.append(f"{prefix}: invalid agy_mode")
        elif field == "sandbox_mode":
            if not has_choice(value, SANDBOX_MODES):
                errors.append(f"{prefix}: invalid sandbox_mode")
        elif not isinstance(value, str):
            errors.append(f"{prefix}: {field} must be a string")
    return errors


def _validate_base_url(value):
    url = require_text(value, "base_url")
    try:
        parts = urlsplit(url)
        parts.port  # Accessing the port also validates its format and range.
        valid = (
            parts.scheme in ("http", "https") and parts.hostname
            and parts.username is None and parts.password is None
            and not parts.query and not parts.fragment
            and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Model card base_url must be an HTTP or HTTPS URL without credentials, query, or fragment")
    return url


def normalize_model_card(card, *, changes=None):
    """Keep canonical and default-prefixed card settings synchronized."""
    if changes is None:
        changes = {}
    if not isinstance(card, dict) or not isinstance(changes, dict):
        raise ValueError("Model card must be an object")
    record = deepcopy({**card, **changes})
    record.pop("has_api_key", None)
    if changes.get("api_key") is None and "api_key" in card:
        record["api_key"] = card["api_key"]
    for field, alias, default in (
        ("effort", "default_effort", "high"),
        ("timeout_s", "default_timeout_s", 300),
        ("sandbox_mode", "default_sandbox_mode", "read-only"),
    ):
        previous = card.get(field, card.get(alias, default))
        value = changes.get(field, changes.get(alias, previous))
        record[field] = record[alias] = value
    for field in ("id", "name", "provider", "model_id"):
        record[field] = require_text(record.get(field), field)
    for field, choices in (("provider", PROVIDERS), ("effort", EFFORTS), ("sandbox_mode", SANDBOX_MODES)):
        if not has_choice(record[field], choices):
            raise ValueError(f"Invalid model card {field}")
    if not is_integer_at_least(record["timeout_s"], 1):
        raise ValueError("Model card timeout_s must be an integer >= 1")
    provider = record["provider"]
    executable = record.get("executable")
    if provider in _CLI_COMMANDS:
        if executable is None:
            command = _CLI_COMMANDS[provider]
            executable = detect_cli_executable(provider) or command
        record["executable"] = require_text(executable, "executable")
    else:
        record["base_url"] = _validate_base_url(record.get("base_url"))
        if isinstance(executable, str) and not executable.strip():
            record.pop("executable", None)
        elif executable is not None:
            record["executable"] = require_text(executable, "executable")
    workspace_issues = workspace_errors(record, "Model card")
    if workspace_issues:
        raise ValueError(workspace_issues[0])
    for field in ("api_key_env", "api_key"):
        if record.get(field) is not None and not isinstance(record[field], str):
            raise ValueError(f"Model card {field} must be a string")
    if isinstance(record.get("api_key_env"), str):
        record["api_key_env"] = record["api_key_env"].strip() or None
    temperature = record.get("temperature")
    if temperature is not None and (
        type(temperature) not in (float, int) or not 0 <= temperature < float("inf")
    ):
        raise ValueError("Model card temperature must be a finite non-negative number")
    max_tokens = record.get("max_tokens")
    if max_tokens is not None and not is_integer_at_least(max_tokens, 1):
        raise ValueError("Model card max_tokens must be an integer >= 1")
    return record


def redact_model_card(card):
    """Copy a card for API responses and snapshots, retaining its secret status."""
    def remove_secrets(value):
        if isinstance(value, dict):
            return {key: remove_secrets(item) for key, item in value.items() if key != "api_key"}
        if isinstance(value, list):
            return [remove_secrets(item) for item in value]
        return deepcopy(value)

    redacted = remove_secrets(card)
    redacted["has_api_key"] = bool(card.get("api_key", card.get("has_api_key", False)))
    redacted.setdefault("api_key_env", None)
    return redacted


def default_model_cards():
    return [normalize_model_card(card) for card in _MODEL_CARD_SEEDS]


DEFAULT_MODEL_CARDS = default_model_cards()


def node_config(node):
    config = node.get("config", {})
    if not isinstance(config, dict):
        raise ValueError("Node config must be an object")
    node_type = node.get("type")
    defaults = DEFAULT_NODE_CONFIGS[node_type] if has_choice(node_type, NODE_TYPES) else {}
    fields = _MODEL_CONFIG_FIELDS if node_type == "model" else defaults
    inline_config = {field: node[field] for field in fields if field in node}
    return {**deepcopy(defaults), **inline_config, **config}


def normalize_workflow(workflow):
    """Copy a workflow and fill omitted schedule and config fields."""
    if not isinstance(workflow, dict):
        raise ValueError("Workflow must be an object")
    workflow = deepcopy(workflow)
    schedule = workflow.get("schedule", {})
    if not isinstance(schedule, dict):
        raise ValueError("Workflow schedule must be an object")
    workflow["schedule"] = {**DEFAULT_SCHEDULE, **schedule}
    if not isinstance(workflow.get("nodes"), list) or not isinstance(workflow.get("edges"), list):
        raise ValueError("Workflow nodes and edges must be arrays")
    for node in workflow["nodes"]:
        if not isinstance(node, dict):
            raise ValueError("Workflow nodes must be objects")
        node["config"] = node_config(node)
    return workflow


def normalize_template(template):
    if not isinstance(template, dict):
        raise ValueError("Template must be an object")
    template = deepcopy(template)
    for field in ("id", "name"):
        template[field] = require_text(template.get(field), field)
    template.setdefault("description", "")
    if not isinstance(template["description"], str):
        raise ValueError("Template description must be a string")
    template["workflow"] = normalize_workflow(template.get("workflow"))
    return template


def truncate_context(text, limit):
    marker = "[context truncated]"
    if len(text) <= limit:
        return text, False
    if limit <= len(marker):
        return marker[:limit], True
    prefix = text[:limit - len(marker) - 1].rstrip()
    return f"{prefix}\n{marker}", True
