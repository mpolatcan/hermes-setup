from __future__ import annotations

import argparse

import uvicorn

from .app import create_app
from .config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Honcho Codex Adapter")
    parser.add_argument("--config", help="absolute path to adapter TOML config")
    parser.add_argument("--check-config", action="store_true", help="validate configuration and exit")
    parser.add_argument(
        "--print-effective-config",
        action="store_true",
        help="print validated non-secret effective configuration and exit",
    )
    args = parser.parse_args()
    config = load_config(args.config)
    if args.print_effective_config:
        print(config.effective_json())
        return
    if args.check_config:
        print("adapter config: ok")
        return
    uvicorn.run(
        create_app(config=config),
        host=config.server.host,
        port=config.server.port,
        access_log=config.server.access_log,
    )


if __name__ == "__main__":
    main()
