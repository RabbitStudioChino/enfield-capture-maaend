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
    parser = argparse.ArgumentParser(description="Windows Endfield region navigation, photography and capture tasks")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("auto")
    command.add_argument("--workspace", required=True, type=Path)
    selection = command.add_mutually_exclusive_group(required=True)
    selection.add_argument("--region")
    selection.add_argument("--plan", type=Path)
    command.add_argument("--task")
    command.add_argument("--radius", type=lambda value: bounded_number(value, 10, 600), default=60)
    command.add_argument("--spacing", type=lambda value: bounded_number(value, 5, 200), default=20)
    command.add_argument("--views", type=lambda value: bounded_number(value, 1, 12, True), default=8)
    command.add_argument("--max-points", type=lambda value: bounded_number(value, 1, 512, True), default=160)
    command.add_argument("--start-delay", type=lambda value: bounded_number(value, 0, 60), default=5)
    command = commands.add_parser("serve")
    command.add_argument("--workspace", required=True, type=Path)
    command.add_argument("--port", type=lambda value: bounded_number(value, 1024, 65535, True), default=8780)
    for name in ("regions", "list"):
        command = commands.add_parser(name)
        command.add_argument("--workspace", required=True, type=Path)
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
        if args.command == "serve":
            from .server import serve

            return serve(args.workspace, port=args.port) or 0
        if args.command in ("regions", "list"):
            from .regions import region_catalog

            print(json.dumps(region_catalog(), ensure_ascii=False, indent=4))
            return 0
        if args.command == "auto":
            from .auto_task import start_auto
            from .task import check_path, read_json
            from .validate import require

            if args.plan:
                path = check_path(args.plan)
                require(path.is_relative_to(check_path(args.workspace)), "Automatic plan must stay inside workspace")
                request = read_json(path)
            else:
                request = {
                    "region": args.region,
                    "radius": args.radius,
                    "spacing": args.spacing,
                    "views": args.views,
                    "max_points": args.max_points,
                }
            return start_auto(args.workspace, request, args.task, args.start_delay)
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
