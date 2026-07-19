from __future__ import annotations

import uvicorn


def main() -> None:
    uvicorn.run(
        "honcho_codex_adapter.app:create_app",
        factory=True,
        host="127.0.0.1",
        port=18080,
        access_log=False,
    )


if __name__ == "__main__":
    main()
