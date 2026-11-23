"""Start the AgentBridge Workbench on a local HTTP server."""

import argparse
import os
import sys
import webbrowser
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from workbench.server import create_server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="address to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="port to bind (default: 8765)")
    parser.add_argument(
        "--db", type=Path,
        default=os.environ.get("AGENTBRIDGE_WORKBENCH_DB") or PROJECT_DIR / "workbench.sqlite3",
        help="SQLite database path (defaults to AGENTBRIDGE_WORKBENCH_DB or the project database)",
    )
    parser.add_argument("--no-browser", action="store_true", help="skip opening the web browser")
    args = parser.parse_args()
    with create_server(args.db, args.host, args.port) as server:
        browser_host = "127.0.0.1" if args.host in ("", "0.0.0.0") else args.host
        url = f"http://{browser_host}:{server.server_port}/"
        print(f"AgentBridge Workbench is listening at {url}", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
