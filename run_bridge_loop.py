"""Command-line entry point for one AgentBridge handoff."""

import argparse
import json
import sys
from pathlib import Path

from agentbridge.bridge import DEFAULT_AGY, DEFAULT_CODEX, DEFAULT_OUTPUT, BridgeConfig, run_loop


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one Neon → Codex Sol → Neon handoff.")
    parser.add_argument("--timeout", "--timeout-s", dest="timeout_s", type=int, default=180)
    parser.add_argument("--sol-effort", default="medium")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--agy-path", type=Path, default=DEFAULT_AGY)
    parser.add_argument("--codex-path", type=Path, default=DEFAULT_CODEX)
    args = parser.parse_args(argv)
    try:
        config = BridgeConfig(**vars(args))
    except ValueError as exc:
        parser.error(str(exc))
    summary = run_loop(config)
    print(json.dumps({
        "run_dir": summary["run_dir"],
        "call_count": summary["call_count"],
        "model_decision": summary["model_decision"],
        "program_passed": summary["program_passed"],
        "final_decision": summary["final_decision"],
        "override_reason": summary["override_reason"],
    }, ensure_ascii=False, indent=2))
    return summary["exit_code"]


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
