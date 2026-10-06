#!/usr/bin/env python3
"""Smart Weighing Scale - launcher.

    python run.py                 desktop window (default)
    python run.py --web           browser dashboard instead
    python run.py --web --port 9000
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Smart Weighing Scale")
    parser.add_argument("--web", action="store_true",
                        help="serve the browser dashboard instead of the window")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if args.web:
        from smartscale.web import serve
        serve(port=args.port, open_browser=not args.no_browser)
    else:
        from smartscale.app import main as tk_main
        tk_main()


if __name__ == "__main__":
    main()
