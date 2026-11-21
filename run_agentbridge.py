"""Validate a project or manage an AgentBridge framework run."""

import argparse
import json
import sys
from pathlib import Path

from agentbridge import framework
from agentbridge.iteration import reject_nested
from agentbridge.project_spec import ProjectSpec


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def _parser():
    parser = _Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "start"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        if name == "start":
            command.add_argument("--pause-after", choices=framework.PAUSE_STAGES)
            command.add_argument("--output-dir", type=Path)
    for name in ("pause", "resume", "status", "revise"):
        command = commands.add_parser(name)
        command.add_argument("--run-dir", type=Path, required=True)
        if name == "resume":
            command.add_argument("--pause-after", choices=framework.PAUSE_STAGES)
            command.add_argument("--human-note")
            command.add_argument("--human-reason")
            command.add_argument("--decision-file", type=Path)
            command.add_argument("--resolve-pending", choices=("retry", "reverify"))
            command.add_argument("--budget-reason")
            for limit in framework.RESUME_LIMITS:
                command.add_argument("--" + limit.replace("_", "-"), type=int)
        elif name == "revise":
            command.add_argument("--config", type=Path, required=True)
            command.add_argument("--reason", required=True)
            command.add_argument("--human-note")
            command.add_argument("--pause-after", choices=framework.PAUSE_STAGES)
        elif name == "status":
            command.add_argument("--full", action="store_true")
    return parser


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def main(argv=None, *, runner=None):
    try:
        reject_nested()
        options = vars(_parser().parse_args(argv))
        command = options.pop("command")
        if command == "validate":
            spec = ProjectSpec.from_file(options["config"])
            _print({"spec": spec.to_dict(), "contract_hash": spec.contract_hash(spec.verification_manifest),
                    "verification_manifest": spec.verification_manifest})
            return 0
        if command == "start":
            spec = ProjectSpec.from_file(options["config"], output_dir=options["output_dir"])
            state = framework.start(spec, pause_after=options["pause_after"], runner=runner)
        elif command == "resume":
            state = framework.resume(runner=runner, **{key: value for key, value in options.items()
                                                       if value is not None})
        elif command == "revise":
            state = framework.revise(runner=runner, **options)
        else:
            _print(getattr(framework, command)(**options))
            return 0
    except (OSError, ValueError, RecursionError) as exc:
        print(f"agentbridge: {exc}", file=sys.stderr)
        return 1
    _print({key: state[key] for key in ("run_dir", "current_state", "current_round", "stop_reason",
                                       "override_reason", "resumable", "locked_contract_hash")})
    if state["current_state"] == "COMPLETED_PASS":
        return 0
    return 2 if state["current_state"] == "PAUSED" else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
