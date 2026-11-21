"""Command-line interface for the AgentBridge engineering scheduler."""

import argparse
import json
import sys
from pathlib import Path

from agentbridge.bridge import DEFAULT_OUTPUT
from agentbridge.iteration import IterationConfig, PAUSE_STAGES, pause, reject_nested, resume, start, status
from agentbridge.iteration_files import checked_path


def _limits(parser, *, defaults):
    parser.add_argument("--max-sol-attempts", type=int, default=3 if defaults else None)
    parser.add_argument("--max-model-calls", type=int, default=7 if defaults else None)
    parser.add_argument("--max-consecutive-failures", type=int, default=2 if defaults else None)
    parser.add_argument("--max-total-seconds", type=int, default=900 if defaults else None)


def _parser():
    parser = argparse.ArgumentParser(description="Run a feedback-driven AgentBridge engineering task.")
    commands = parser.add_subparsers(dest="command", required=True)
    launch = commands.add_parser("start", help="start a new engineering run")
    goal = launch.add_mutually_exclusive_group(required=True)
    goal.add_argument("--goal")
    goal.add_argument("--goal-file", type=Path)
    launch.add_argument("--project-dir", type=Path, required=True)
    launch.add_argument("--tests-dir", type=Path, required=True)
    launch.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    _limits(launch, defaults=True)
    launch.add_argument("--timeout", dest="timeout_s", type=int, default=240)
    launch.add_argument("--sol-effort", default="high")
    launch.add_argument("--pause-after", choices=PAUSE_STAGES)
    launch.add_argument("--min-tests", type=int, default=1)
    launch.add_argument("--allow-skipped", action="store_true")
    launch.add_argument("--acceptance-cmd", nargs=argparse.REMAINDER, metavar="ARG",
                        help="trusted unittest-reporting command with arguments; place last")

    for name, description in (
        ("resume", "continue an existing run"),
        ("pause", "request a pause at the next checkpoint"),
        ("status", "read the latest state as JSON"),
    ):
        command = commands.add_parser(name, help=description)
        command.add_argument("--run-dir", type=Path, required=True)
        if name == "resume":
            command.add_argument("--pause-after", choices=PAUSE_STAGES)
            _limits(command, defaults=False)
    return parser


def main(argv=None, *, runner=None) -> int:
    parser = _parser()
    try:
        reject_nested()
        args = vars(parser.parse_args(argv))
        command = args.pop("command")
        if command == "start":
            pause_after = args.pop("pause_after")
            goal_file = args.pop("goal_file")
            if goal_file is not None:
                args["goal"] = checked_path(goal_file, "file").read_text(encoding="utf-8-sig")
            state = start(IterationConfig(**args), pause_after=pause_after, runner=runner)
        elif command == "resume":
            state = resume(runner=runner, **{name: value for name, value in args.items()
                                             if value is not None})
        else:
            result = (pause if command == "pause" else status)(args["run_dir"])
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    print(json.dumps({key: state[key] for key in (
        "run_dir", "current_state", "current_round", "stop_reason", "override_reason", "resumable",
    )}, ensure_ascii=False, indent=2))
    if state["current_state"] == "COMPLETED_PASS":
        return 0
    return 2 if state["current_state"] == "PAUSED" else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
