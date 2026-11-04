#!/usr/bin/env python
"""Inject the OAuth clients a build of GilzaBots ships for MCP connectors.

X accepts OAuth logins only through an app registered in its Developer Console.
With GilzaBots' own app embedded, connecting X is "click Connect, sign in";
without it, each user has to create an app and paste its client id. The client
lands in ``gilzabots/connectors/bundled_oauth_clients.json`` (git-ignored), which
the packaged app reads as package data (gilzabots/connectors/bundled_clients.py).

The app is a public client (PKCE, no secret), but its id still identifies one
developer account, so it belongs in the release pipeline's secret store.

    python scripts/embed_connector_clients.py --from-env     # GILZABOTS_X_CLIENT_ID
    python scripts/embed_connector_clients.py --x-client-id ...
    python scripts/embed_connector_clients.py --clear

Exit codes: 0 on success, 1 when no client was given.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

TARGET = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "gilzabots", "connectors", "bundled_oauth_clients.json",
)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--x-client-id", default="")
    parser.add_argument("--from-env", action="store_true", help="read GILZABOTS_X_CLIENT_ID")
    parser.add_argument("--clear", action="store_true", help="remove the bundled clients")
    args = parser.parse_args(argv)

    if args.clear:
        try:
            os.remove(TARGET)
        except FileNotFoundError:
            pass
        print(f"Removed {TARGET}")
        return 0

    x_client_id = args.x_client_id.strip()
    if args.from_env:
        x_client_id = x_client_id or os.environ.get("GILZABOTS_X_CLIENT_ID", "").strip()
    if not x_client_id:
        print("No client given: pass --x-client-id or set GILZABOTS_X_CLIENT_ID.", file=sys.stderr)
        return 1

    with open(TARGET, "w", encoding="utf-8") as handle:
        json.dump({"x": {"client_id": x_client_id}}, handle, indent=2)
        handle.write("\n")
    print(f"Embedded the X client in {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
