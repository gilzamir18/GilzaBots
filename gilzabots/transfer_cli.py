"""``gilzabots transfer ...``: move monitors and connectors to another machine.

    gilzabots transfer export [-o FILE] [--only connectors|monitors]
                              [--connector ID ...] [--monitor ID ...]
    gilzabots transfer import FILE [--dry-run] [--project SOURCE=TARGET ...]
                              [--secret KEY.NAME=VALUE ...] [--secrets-file FILE]

See `gilzabots/transfer.py` for what a bundle carries and what an import does.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import transfer


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gilzabots transfer",
        description="Export monitors and connectors to a bundle, or import one on this machine.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="Write a bundle (secrets are never included)")
    export.add_argument("-o", "--output", help="The bundle file to write (default: standard output)")
    export.add_argument("--only", choices=("connectors", "monitors"), help="Export only this kind")
    export.add_argument("--connector", action="append", metavar="ID",
                        help="Export this connector (repeatable; default: every connector)")
    export.add_argument("--monitor", action="append", metavar="ID",
                        help="Export this monitor (repeatable; default: every active or paused monitor)")

    imp = sub.add_parser("import", help="Import a bundle: connectors arrive disabled, monitors paused")
    imp.add_argument("file", help="The bundle file ('-' reads standard input)")
    imp.add_argument("--dry-run", action="store_true", help="Show what would be imported, changing nothing")
    imp.add_argument("--project", action="append", default=[], metavar="SOURCE=TARGET",
                     help="Import the monitors of project SOURCE into project TARGET of this machine")
    imp.add_argument("--secret", action="append", default=[], metavar="KEY.NAME=VALUE",
                     help="A secret the bundle left out, e.g. github.bearer_token=... or gh.env:TOKEN=...")
    imp.add_argument("--secrets-file", metavar="FILE",
                     help='A JSON file {"<connector id or monitor id>": {"<name>": "<value>"}}')
    return parser


def _pairs(values: list[str], option: str) -> list[tuple[str, str]]:
    pairs = []
    for value in values:
        left, sep, right = value.partition("=")
        if not sep or not left:
            raise transfer.TransferError(f"{option} expects NAME=VALUE, got '{value}'.")
        pairs.append((left, right))
    return pairs


def parse_secrets(values: list[str], secrets_file: str | None = None) -> dict:
    secrets: dict[str, dict] = {}
    if secrets_file:
        try:
            with open(secrets_file, encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (OSError, ValueError) as exc:
            raise transfer.TransferError(f"The secrets file {secrets_file} cannot be read: {exc}") from exc
        if not isinstance(loaded, dict) or not all(isinstance(v, dict) for v in loaded.values()):
            raise transfer.TransferError("The secrets file must map each id to {name: value}.")
        for key, entries in loaded.items():
            secrets.setdefault(key, {}).update({str(k): str(v) for k, v in entries.items()})
    for target, value in _pairs(values, "--secret"):
        key, sep, name = target.partition(".")
        if not sep or not key or not name:
            raise transfer.TransferError(f"--secret expects KEY.NAME=VALUE (e.g. github.bearer_token=...), got '{target}'.")
        secrets.setdefault(key, {})[name] = value
    return secrets


def _cmd_export(args) -> int:
    bundle, notes = transfer.build_bundle(
        connector_ids=args.connector, monitor_ids=args.monitor,
        include_connectors=args.only != "monitors", include_monitors=args.only != "connectors",
    )
    text = json.dumps(bundle, indent=2, ensure_ascii=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    else:
        print(text)
    out = sys.stderr if not args.output else sys.stdout
    for note in notes:
        print(f"Note: {note}", file=out)
    needs = sum(len(item.get("needs") or []) for item in bundle["connectors"] + bundle["monitors"])
    print(f"Exported {len(bundle['connectors'])} connector(s) and {len(bundle['monitors'])} monitor(s)"
          + (f" to {args.output}" if args.output else "") + "."
          + (f" {needs} secret(s) were left out; supply them when importing." if needs else ""), file=out)
    return 0


_LABELS = {
    transfer.RESULT_READY: "would import",
    transfer.RESULT_IMPORTED: "imported",
    transfer.RESULT_EXISTS: "already here",
    transfer.RESULT_NEEDS_SECRETS: "needs secrets",
    transfer.RESULT_NO_PROJECT: "no project",
    transfer.RESULT_NO_CHAT: "no chat",
    transfer.RESULT_INVALID: "invalid",
}


def print_report(report: dict) -> None:
    for result in report["results"]:
        label = _LABELS.get(result["status"], result["status"])
        where = f" [{result['project']}]" if result.get("project") else ""
        print(f"{result['kind']:<9} {result['name']}{where} ({result['key']}): {label}")
        if result.get("message"):
            print(f"          {result['message']}")
        for name in result.get("missing") or []:
            print(f"          --secret {result['key']}.{name}=<value>")
        for warning in result.get("warnings") or []:
            print(f"          warning: {warning}")
    verb = "would be imported" if report["dry_run"] else "imported"
    print(f"{report['imported']} item(s) {verb}, {report['existing']} already here, "
          f"{report['failed']} not imported.")


def _cmd_import(args) -> int:
    try:
        if args.file == "-":
            bundle = json.load(sys.stdin)
        else:
            with open(args.file, encoding="utf-8") as handle:
                bundle = json.load(handle)
    except (OSError, ValueError) as exc:
        raise transfer.TransferError(f"The bundle {args.file} cannot be read: {exc}") from exc
    report = transfer.import_bundle(
        bundle,
        secrets=parse_secrets(args.secret, args.secrets_file),
        project_map=dict(_pairs(args.project, "--project")),
        dry_run=args.dry_run,
    )
    print_report(report)
    return 0 if report["failed"] == 0 else 1


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        return _cmd_export(args) if args.command == "export" else _cmd_import(args)
    except transfer.TransferError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
