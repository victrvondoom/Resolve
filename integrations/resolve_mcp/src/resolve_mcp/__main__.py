"""Command-line entry point.

    resolve-mcp serve      # MCP over STDIO. Protocol bytes go to stdout only; diagnostics go to stderr.
    resolve-mcp selftest   # environment, RESOLVE reachability, session, identity, capabilities, tool discovery

Configuration is read from the environment; see README.md. Nothing is listened on a network port.
"""
from __future__ import annotations

import argparse
import logging
import sys

from .config import GatewayConfig
from .errors import GatewayError


def _configure_logging() -> None:
    # stderr only: stdout is reserved for MCP JSON-RPC frames when serving over STDIO.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="resolve-mcp", description="RESOLVE proof-carrying MCP gateway")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="serve MCP over STDIO (default)")
    sub.add_parser("selftest", help="verify the installation and the RESOLVE connection")
    args = parser.parse_args(argv)
    command = args.command or "serve"
    _configure_logging()

    if command == "selftest":
        from .selftest import run_selftest
        try:
            config: GatewayConfig | None = GatewayConfig.from_env()
        except GatewayError as exc:
            sys.stdout.write(f"[FAIL] configuration: {exc}\nSELFTEST FAIL: configuration\n")
            return 1
        return run_selftest(config, sys.stdout)

    try:
        config = GatewayConfig.from_env()
    except GatewayError as exc:
        sys.stderr.write(f"resolve-mcp: configuration error: {exc}\n")
        return 2
    from .gateway import ResolveGateway
    from .server import build_server
    build_server(ResolveGateway(config)).run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
