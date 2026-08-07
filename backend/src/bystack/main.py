"""Entry point.

    python -m bystack                      # local engine, zero config
    python -m bystack --config bystack.yaml
"""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from bystack.api.app import create_app
from bystack.config import Settings


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="bystack", description="Infrastructure control plane")
    parser.add_argument("--config", help="Path to bystack.yaml; omit to manage the local engine")
    parser.add_argument("--host", help="Override the configured bind address")
    parser.add_argument("--port", type=int, help="Override the configured port")
    parser.add_argument("--reload", action="store_true", help="Auto-reload (development)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        settings = Settings.load(args.config) if args.config else Settings.default()
    except (FileNotFoundError, ValueError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    uvicorn.run(
        create_app(settings),
        host=args.host or settings.api.host,
        port=args.port or settings.api.port,
        log_level=settings.log_level.lower(),
        reload=args.reload,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
