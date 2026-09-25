import argparse
import os
import threading
import webbrowser
from pathlib import Path

import uvicorn
from dotenv import load_dotenv


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="Your local LiteLLM engineering ROI dashboard")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8787")))
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "0.0.0.0"])
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--demo", action="store_true", help="Open sample data; makes no external requests")
    args = parser.parse_args()
    if args.data_dir:
        os.environ["ROI_DATA_DIR"] = str(args.data_dir)
    from .app import create_app
    url = f"http://localhost:{args.port}" + ("/?demo=1" if args.demo else "")
    print(f"\n  LiteLLM ROI Calculator → {url}\n")
    if not args.no_browser:
        timer = threading.Timer(1, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
    uvicorn.run(create_app(demo_only=args.demo), host=args.host, port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
