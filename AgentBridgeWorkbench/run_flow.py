"""Run an AgentBridge Workbench template or exported JSON workflow bundle from the CLI.

Enables Antigravity, Codex, scripts, and terminal users to execute visual DAG workflows
built in AgentBridge Workbench directly from the command line.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import sys

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from workbench.adapters import AdapterRegistry
from workbench.executor import WorkflowExecutor
from workbench.storage import WorkbenchStore


def _default_db_path() -> Path:
    env_db = os.environ.get("AGENTBRIDGE_WORKBENCH_DB", "").strip()
    return Path(env_db) if env_db else PROJECT_DIR / "workbench.sqlite3"


def _parse_mappings(items: list[str] | None) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"Invalid --map entry {item!r}; expected OLD_CARD_ID=LOCAL_CARD_ID")
        source, target = item.split("=", 1)
        source, target = source.strip(), target.strip()
        if not source or not target:
            raise ValueError(f"Invalid --map entry {item!r}; both sides must be non-empty")
        mapping[source] = target
    return mapping


def _read_prompt(args: argparse.Namespace) -> str:
    if args.prompt is not None and args.prompt_file is not None:
        raise ValueError("Specify only one of --prompt or --prompt-file")
    if args.prompt_file is not None:
        text = Path(args.prompt_file).read_text(encoding="utf-8-sig")
    elif args.prompt == "-":
        text = sys.stdin.read()
    elif args.prompt is not None:
        text = args.prompt
    else:
        raise ValueError("Missing prompt: pass --prompt <text>, --prompt -, or --prompt-file <path>")
    if not text.strip():
        raise ValueError("Prompt must be non-empty")
    return text


def _resolve_workflow(
    store: WorkbenchStore,
    *,
    template_ref: str | None,
    bundle_path: Path | None,
    card_mapping: dict[str, str],
    workdir: str | None,
) -> tuple[dict, str]:
    if bool(template_ref) == bool(bundle_path):
        raise ValueError("Specify exactly one of --template or --bundle")

    if bundle_path is not None:
        raw = json.loads(Path(bundle_path).read_text(encoding="utf-8-sig"))
        if isinstance(raw, dict) and "workflow" in raw:
            bundle = {"format_version": 2, **raw}
        else:
            bundle = {"format_version": 2, "name": Path(bundle_path).stem, "workflow": raw}
        imported = store.import_workflow({
            "bundle": bundle,
            "card_mapping": card_mapping,
            "save_as_template": False,
        })
        unmapped = imported.get("unmapped_nodes") or []
        if unmapped:
            available = ", ".join(card["id"] for card in store.list_model_cards())
            details = ", ".join(f"{item['node_id']}->{item['model_card_id']}" for item in unmapped)
            raise ValueError(
                f"Unmapped model cards in bundle ({details}). "
                f"Pass --map OLD_ID=LOCAL_ID. Available local cards: {available}"
            )
        workflow = imported["workflow"]
        label = bundle.get("name") or Path(bundle_path).stem
    else:
        templates = store.list_templates()
        match = next(
            (tpl for tpl in templates if tpl["id"] == template_ref or tpl["name"] == template_ref),
            None,
        )
        if match is None:
            available = ", ".join(f"{tpl['id']} ({tpl['name']})" for tpl in templates)
            raise ValueError(f"Template not found: {template_ref!r}. Available: {available}")
        workflow = deepcopy(match["workflow"])
        if card_mapping:
            for node in workflow.get("nodes", []):
                if node.get("type") == "model":
                    cfg = node.setdefault("config", {})
                    old_id = cfg.get("model_card_id")
                    if old_id in card_mapping:
                        cfg["model_card_id"] = card_mapping[old_id]
        label = match["name"]

    if workdir:
        workspace = dict(workflow.get("workspace") or {})
        workspace["workdir"] = str(Path(workdir).resolve())
        workflow["workspace"] = workspace
    return workflow, label


def run_cli(argv: list[str] | None = None, *, adapter_registry: AdapterRegistry | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=_default_db_path(), help="SQLite database path")
    parser.add_argument("--list-templates", action="store_true", help="List available local workflow templates")
    parser.add_argument("--list-models", action="store_true", help="List available local model cards")
    parser.add_argument("--template", help="Saved template ID or name to execute")
    parser.add_argument("--bundle", type=Path, help="Path to an exported v2 workflow JSON bundle")
    parser.add_argument(
        "--map", dest="mappings", action="append", default=[],
        help="Remap a bundle/template model card ID to a local card (format: OLD_ID=LOCAL_ID, repeatable)",
    )
    parser.add_argument("--workdir", help="Optional project working directory override for this run")
    parser.add_argument("--prompt", help="Prompt text to send into the workflow (use '-' to read stdin)")
    parser.add_argument("--prompt-file", type=Path, help="Read prompt text from a UTF-8 file")
    parser.add_argument("--conversation-id", help="Run inside an existing conversation ID instead of a temporary run")
    parser.add_argument("--save-title", help="Persist the run as a new conversation with this title")
    parser.add_argument("--json", dest="as_json", action="store_true", help="Print complete JSON turn result & trace")
    args = parser.parse_args(argv)

    store = WorkbenchStore(args.db)
    try:
        if args.list_templates:
            items = [
                {"id": t["id"], "name": t["name"], "description": t.get("description", "")}
                for t in store.list_templates()
            ]
            print(json.dumps(items, ensure_ascii=False, indent=2))
            return 0
        if args.list_models:
            items = [
                {
                    "id": c["id"],
                    "name": c["name"],
                    "provider": c["provider"],
                    "model_id": c["model_id"],
                }
                for c in store.list_model_cards()
            ]
            print(json.dumps(items, ensure_ascii=False, indent=2))
            return 0

        prompt = _read_prompt(args)
        mapping = _parse_mappings(args.mappings)
        executor = WorkflowExecutor(store, adapter_registry or AdapterRegistry())

        temporary_conv_id = None
        if args.conversation_id:
            conv_id = args.conversation_id
            if args.template or args.bundle:
                workflow, _ = _resolve_workflow(
                    store, template_ref=args.template, bundle_path=args.bundle,
                    card_mapping=mapping, workdir=args.workdir,
                )
                store.update_conversation_workflow(conv_id, workflow)
        else:
            workflow, label = _resolve_workflow(
                store, template_ref=args.template, bundle_path=args.bundle,
                card_mapping=mapping, workdir=args.workdir,
            )
            title = args.save_title or f"[CLI] {label}"
            conv = store.create_conversation(title=title, workflow=workflow)
            conv_id = conv["id"]
            if not args.save_title:
                temporary_conv_id = conv_id

        try:
            turn = executor.run_turn(conv_id, prompt)
        finally:
            if temporary_conv_id is not None:
                try:
                    store.delete_conversation(temporary_conv_id)
                except KeyError:
                    pass

        if args.as_json:
            print(json.dumps(turn, ensure_ascii=False, indent=2))
        else:
            if turn.get("final_response"):
                print(turn["final_response"])
            if turn.get("error_message"):
                print(f"[status={turn.get('status')}] {turn['error_message']}", file=sys.stderr)
        return 0 if turn.get("status") in {"completed", "partial"} else 1
    except Exception as exc:
        print(f"run_flow error: {exc}", file=sys.stderr)
        return 2
    finally:
        store.close()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    return run_cli()


if __name__ == "__main__":
    raise SystemExit(main())
