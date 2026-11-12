"""Prepare portable inputs and run a checkpointed offline reconstruction."""

import argparse
import json
import sys
from pathlib import Path

from .pipeline import cancel, feedback, plan, run, status, verify_bundle
from .prepare import prepare, verify
from .toolchain import doctor


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
    command = commands.add_parser("doctor")
    command.add_argument("--workspace", type=Path, required=True)
    command.add_argument("--output", type=Path, required=True)
    command.add_argument("--colmap", type=Path)
    command.add_argument("--openmvs-dir", type=Path)
    command = commands.add_parser("plan")
    command.add_argument("--workspace", type=Path, required=True)
    command.add_argument("--job", required=True)
    command.add_argument("--run", required=True)
    command.add_argument("--config", type=Path, required=True)
    command.add_argument("--toolchain", type=Path, required=True)
    command.add_argument("--mode", choices=("full", "coarse"), default="full")
    command.add_argument("--seed-run")
    command.add_argument("--max-seconds", type=int)
    for name in ("run", "resume", "status", "cancel", "feedback"):
        command = commands.add_parser(name)
        command.add_argument("--workspace", type=Path, required=True)
        command.add_argument("--run", required=True)
        if name == "feedback":
            command.add_argument("--request", required=True)
    command = commands.add_parser("verify-bundle")
    command.add_argument("--bundle", type=Path, required=True)
    for name in ("create", "add-round", "run", "status", "review", "import", "final"):
        command = commands.add_parser("project-" + name)
        command.add_argument("--workspace", type=Path, required=True)
        command.add_argument("--project", required=True)
        if name == "create":
            command.add_argument("--config", type=Path, required=True)
            command.add_argument("--toolchain", type=Path, required=True)
            command.add_argument("--policy", type=Path)
        elif name == "add-round":
            command.add_argument("--bundle", type=Path, action="append", default=[])
            command.add_argument("--toolchain", type=Path)
        elif name in ("run", "review", "final"):
            command.add_argument("--round", required=name == "review")
            if name == "review":
                command.add_argument("--review", required=True)
            elif name == "final":
                command.add_argument("--final", default="final-001")
                command.add_argument("--toolchain", type=Path)
        elif name == "import":
            command.add_argument("--response", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command.startswith("project-"):
            from .project import (
                add_round,
                create_project,
                export_review,
                finalize,
                import_review,
                project_status,
                run_round,
            )

            if args.command == "project-create":
                result = create_project(args.workspace, args.project, args.config, args.toolchain, args.policy)
            elif args.command == "project-add-round":
                result = add_round(args.workspace, args.project, args.bundle, args.toolchain)
            elif args.command == "project-run":
                result = run_round(args.workspace, args.project, args.round)
            elif args.command == "project-status":
                result = project_status(args.workspace, args.project)
            elif args.command == "project-review":
                result = export_review(args.workspace, args.project, args.round, args.review)
            elif args.command == "project-import":
                result = import_review(args.workspace, args.project, args.response)
            else:
                result = finalize(args.workspace, args.project, args.round, args.final, args.toolchain)
        elif args.command == "prepare":
            result = prepare(args.workspace, args.bundle, args.job)
        elif args.command == "verify":
            result = verify(args.workspace, args.job)
        elif args.command == "doctor":
            result = doctor(args.workspace, args.output, args.colmap, args.openmvs_dir)
        elif args.command == "plan":
            result = plan(
                args.workspace,
                args.job,
                args.run,
                args.config,
                args.toolchain,
                mode=args.mode,
                seed_run=args.seed_run,
                max_seconds=args.max_seconds,
            )
        elif args.command in ("run", "resume"):
            result = run(args.workspace, args.run, resume=args.command == "resume")
        elif args.command == "status":
            result = status(args.workspace, args.run)
        elif args.command == "cancel":
            result = cancel(args.workspace, args.run)
        elif args.command == "feedback":
            result = feedback(args.workspace, args.run, args.request)
        else:
            result = verify_bundle(args.bundle)
    except KeyboardInterrupt as error:
        report_failure("CANCELLED", error)
        return 130
    except (ValueError, OSError, KeyError, TypeError, StopIteration) as error:
        report_failure("INVALID", error)
        return 1
    if args.command in ("prepare", "verify"):
        print(f"PREPARED INPUTS {result['image_count']} images; reconstruction=not_run; quality=not_assessed")
    elif args.command == "doctor":
        print(
            json.dumps(
                {"ready": result["ready"], "tools": {name: tool["status"] for name, tool in result["tools"].items()}}
            )
        )
        return 0 if result["ready"] else 1
    elif args.command == "plan":
        print(json.dumps({key: result[key] for key in ("run_id", "build_key", "stages", "blockers")}))
    elif args.command == "verify-bundle":
        print(json.dumps({key: result[key] for key in ("revision", "status", "scale_status")}))
    else:
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
