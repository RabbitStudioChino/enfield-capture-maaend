import argparse
import math
from pathlib import Path

from .probe import run_probe


def bounded_number(value, lower, upper, integer=False):
    result = int(value) if integer else float(value)
    if not lower <= result <= upper or not math.isfinite(result):
        raise argparse.ArgumentTypeError(f"value must be between {lower} and {upper}")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Finite Windows Endfield capture diagnostics")
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
    args = parser.parse_args(argv)
    try:
        return run_probe(args.workspace, args.command, args.count, args.interval, args.start_delay)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Capture setup failed: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
