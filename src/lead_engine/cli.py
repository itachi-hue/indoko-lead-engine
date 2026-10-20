"""Command-line entry point: `lead-engine <command>`.

Every command runs under the single-run file lock and is recorded in the `runs` table.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from . import __version__
from .context import Context, setup_logging
from .runtime import AlreadyRunning, Run, run_lock

log = logging.getLogger("lead_engine")

COMMANDS: dict[str, dict] = {}


def command(name: str, help_: str, lock: bool = True):
    """Register a command. `configure(parser)` adds arguments; the function runs it."""
    def wrap(fn):
        COMMANDS[name] = {"fn": fn, "help": help_, "lock": lock, "configure": None}
        return fn
    return wrap


def configure(name: str):
    def wrap(fn):
        COMMANDS[name]["configure"] = fn
        return fn
    return wrap


# ---------------- M0 commands ----------------
@command("init", "Create the database and data folders; with --sheet, add the Sheet tabs")
def cmd_init(ctx: Context, args, run: Run) -> int:
    ctx.settings.data_dir.mkdir(parents=True, exist_ok=True)
    _ = ctx.con
    print(f"database ready: {ctx.settings.db_path}")
    if args.sheet:
        from .sheets import layout

        layout.ensure_layout(ctx)
        print("sheet tabs and headers ready")
    run.ok += 1
    return 0


@configure("init")
def _init_args(p):
    p.add_argument("--sheet", action="store_true", help="also create the Sheet tabs and headers")


@command("check", "Check configuration and every API key", lock=False)
def cmd_check(ctx: Context, args, run: Run) -> int:
    s = ctx.settings
    results: dict[str, dict] = {}
    missing_cfg = [k for k in ("sheet.spreadsheet_id", "sender.name", "sender.postal_address",
                               "gemini.model") if not s.get(k)]
    results["config"] = {"ok": not missing_cfg, "missing": missing_cfg}
    for key in ("EXA_API_KEY", "GEMINI_API_KEY", "INSTANTLY_API_KEY", "GOOGLE_SERVICE_ACCOUNT_FILE"):
        results.setdefault("secrets", {})[key] = bool(s.secret(key))
    if args.live:
        checks = {
            "gemini": lambda: ctx.gemini.ping(),
            "instantly": lambda: ctx.instantly.ping(),
            "sheets": lambda: {"ok": True, "title": ctx.sheets.metadata()["properties"]["title"]},
            "exa": lambda: ctx.exa.ping(),
        }
        for name, fn in checks.items():
            try:
                results[name] = fn()
            except Exception as exc:  # report every failure, keep checking the rest
                results[name] = {"ok": False, "error": str(exc)[:200]}
    print(json.dumps(results, indent=2))
    ok = results["config"]["ok"] and all(results["secrets"].values()) and all(
        v.get("ok", True) for k, v in results.items() if k not in ("config", "secrets"))
    run.ok += int(ok)
    run.failed += int(not ok)
    return 0 if ok else 1


@configure("check")
def _check_args(p):
    p.add_argument("--live", action="store_true",
                   help="call each API (Exa costs about $0.007 for one test search)")


# ---------------- entry point ----------------
def _load_command_modules() -> None:
    """Import modules that register more commands. Missing optional modules are skipped."""
    import importlib

    for mod in ("lead_engine.commands",):
        try:
            importlib.import_module(mod)
        except ModuleNotFoundError as exc:
            if exc.name != mod:
                raise


def build_parser() -> argparse.ArgumentParser:
    _load_command_modules()
    parser = argparse.ArgumentParser(prog="lead-engine", description="Indoko lead engine")
    parser.add_argument("--version", action="version", version=f"lead-engine {__version__}")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be sent or written outside the database; do nothing")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, spec in COMMANDS.items():
        p = sub.add_parser(name, help=spec["help"], description=spec["help"])
        if spec["configure"]:
            spec["configure"](p)
    return parser


def main(argv: list[str] | None = None, ctx: Context | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ctx = ctx or Context(dry_run=args.dry_run)
    ctx.dry_run = ctx.dry_run or args.dry_run
    setup_logging(ctx.settings.data_dir, args.verbose)
    spec = COMMANDS[args.command]
    try:
        if spec["lock"]:
            with run_lock(ctx.settings.lock_path):
                with Run(ctx.con, args.command) as run:
                    return spec["fn"](ctx, args, run)
        with Run(ctx.con, args.command) as run:
            return spec["fn"](ctx, args, run)
    except AlreadyRunning as exc:
        print(f"already running: {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
