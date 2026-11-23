---
name: agentbridge-workbench
description: >-
  Run saved templates or exported v2 JSON workflow bundles built in AgentBridge Workbench
  (visual multi-model DAG editor) directly from Antigravity or Codex conversations via run_flow.py.
---

# AgentBridge Workbench Flow Runner Skill

Use this skill when the user asks to execute a workflow template or exported `.json` flow bundle created in **AgentBridge Workbench** (for example: "用工作台里的『并行综合』模板跑一下这个问题", "用导出的 flow.json 模板分析这段内容", or "查看我有哪些工作台流程模板").

## Locate `run_flow.py`

Locate the local AgentBridge Workbench directory containing `run_flow.py` (for example `<repo>/AgentBridgeWorkbench/run_flow.py` or a user-specified path). Never hardcode a machine-specific path when running on another user's computer; check the workspace or ask once if not found.

## Core Commands

### 1. List Available Local Templates & Model Cards
```bash
python -B <workbench_dir>/run_flow.py --list-templates
python -B <workbench_dir>/run_flow.py --list-models
```

### 2. Run a Saved Local Template
```bash
python -B <workbench_dir>/run_flow.py --template "<template_id_or_name>" --prompt "<user_request>" --json
```

### 3. Run an Exported v2 JSON Bundle
```bash
python -B <workbench_dir>/run_flow.py --bundle "<path/to/exported_flow.json>" --prompt "<user_request>" --json
```

If the bundle references model card IDs that do not exist in the local database, `run_flow.py` reports the unmapped nodes and available local card IDs. Re-run with repeatable `--map OLD_ID=LOCAL_ID` flags:
```bash
python -B <workbench_dir>/run_flow.py --bundle "<path/to/exported_flow.json>" --map "old_card=sol" --prompt "<user_request>" --json
```

### 4. Optional Overrides
- `--workdir "<project_dir>"`: override the workflow's project working directory for this execution.
- `--prompt-file "<path>"` or `--prompt -` (stdin): pass long or multi-line prompts safely without shell quoting issues.
- `--save-title "<title>"`: persist the execution as a visible conversation inside the Workbench UI (otherwise CLI runs clean up their temporary conversation automatically).
- `--db "<path>"`: target a specific SQLite database (defaults to `AGENTBRIDGE_WORKBENCH_DB` or `<workbench_dir>/workbench.sqlite3`).

## Reporting Results to the User

When using `--json`, summarize clearly for the user:
1. The `final_response` produced by the workflow.
2. A concise execution trace from `node_runs` (each model node's label, `model_id`, `effective_effort`, `status`, and latency), plus any errors if `status` is `partial` or `failed`.
