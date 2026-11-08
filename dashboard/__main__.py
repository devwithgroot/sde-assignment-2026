"""python -m dashboard --db data/phase3_verify.sqlite [--sim-url http://127.0.0.1:8700]"""

from __future__ import annotations

import argparse

import uvicorn

from dashboard.app import create_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only controller monitoring dashboard")
    parser.add_argument("--db", default="data/controller.sqlite", help="Controller SQLite path")
    parser.add_argument("--sim-url", default=None, help="Optional simulator URL for live clock (GET only)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8800)
    args = parser.parse_args(argv)
    app = create_app(args.db, sim_url=args.sim_url)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
