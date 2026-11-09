"""Prepare or verify portable inputs from frozen CaptureBundles."""

import argparse
import sys
from pathlib import Path

from tools.capture.validate import BundleValidationError

from .prepare import prepare, verify


def report_failure(label, error):
    diagnostic = getattr(error, "preparation_diagnostics", None)
    suffix = f"; uncommitted diagnostics: {diagnostic}" if diagnostic else ""
    print(f"{label}: {error}{suffix}", file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--workspace", type=Path, required=True)
        command.add_argument("--job", required=True, help="new portable job ID: letters, digits, hyphens, underscores")
        if name == "prepare":
            command.add_argument("--bundle", type=Path, action="append", required=True)
    args = parser.parse_args(argv)
    try:
        result = (
            prepare(args.workspace, args.bundle, args.job)
            if args.command == "prepare"
            else verify(args.workspace, args.job)
        )
    except KeyboardInterrupt as error:
        report_failure("CANCELLED", error)
        return 130
    except (BundleValidationError, OSError) as error:
        report_failure("INVALID", error)
        return 1
    print(f"PREPARED INPUTS {result['image_count']} images; reconstruction=not_run; quality=not_assessed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
