import argparse
import json
import math
import time
from pathlib import Path

from .probe import run_probe


def bounded_number(value, lower, upper, integer=False):
    result = int(value) if integer else float(value)
    if not lower <= result <= upper or not math.isfinite(result):
        raise argparse.ArgumentTypeError(f"value must be between {lower} and {upper}")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Windows Endfield diagnostics and manually positioned capture")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "probe"):
        command = commands.add_parser(name)
        command.add_argument("--workspace", required=True, type=Path)
        if name == "probe":
            command.add_argument("--count", type=lambda value: bounded_number(value, 1, 60, True), default=12)
            command.add_argument("--interval", type=lambda value: bounded_number(value, 0.25, 30), default=2)
            command.add_argument("--start-delay", type=lambda value: bounded_number(value, 0, 60), default=5)
        else:
            command.set_defaults(count=1, interval=2, start_delay=5)
    for name in ("start", "status", "pause", "stop", "resume", "logs", "export"):
        command = commands.add_parser(name)
        command.add_argument("--workspace", required=True, type=Path)
        command.add_argument("--task", required=True)
        if name == "start":
            command.add_argument("--count", type=lambda value: bounded_number(value, 1, 10000, True), default=12)
            command.add_argument("--interval", type=lambda value: bounded_number(value, 0.25, 3600), default=2)
            command.add_argument("--region-label", required=True)
        if name in ("start", "resume"):
            command.add_argument("--start-delay", type=lambda value: bounded_number(value, 0, 60), default=5)
        if name in ("pause", "stop", "resume"):
            command.add_argument("--generation")
            command.add_argument("--command-id")
        if name == "logs":
            command.add_argument("--follow", action="store_true")
        if name == "export":
            command.add_argument("--session", help="session ID from status/logs; defaults to the latest session")
    args = parser.parse_args(argv)
    try:
        if args.command in ("doctor", "probe"):
            return run_probe(args.workspace, args.command, args.count, args.interval, args.start_delay)
        from . import task

        if args.command == "start":
            return task.start(args.workspace, args.task, args.count, args.interval, args.region_label, args.start_delay)
        if args.command == "status":
            result = task.status(args.workspace, args.task)
        elif args.command in ("pause", "stop", "resume"):
            result = task.control(
                args.workspace,
                args.task,
                args.command,
                args.command_id,
                args.generation,
                start_delay=getattr(args, "start_delay", 5),
            )
            print(json.dumps(result, ensure_ascii=False, indent=4))
            return result.get("run_exit_code", 0 if result["result"] == "applied" else 1)
        elif args.command == "export":
            result = {"bundle": str(task.export(args.workspace, args.task, args.session))}
        else:
            displayed = 0
            while True:
                events = task.logs(args.workspace, args.task)
                for event in events[displayed:]:
                    print(json.dumps(event, ensure_ascii=False), flush=True)
                displayed = len(events)
                if not args.follow or not task.status(args.workspace, args.task)["active"]:
                    return 0
                time.sleep(0.25)
        print(json.dumps(result, ensure_ascii=False, indent=4))
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError) as error:
        parser.exit(1, f"Capture setup failed: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
