"""Run the web app locally.

    python run_web.py              # http://127.0.0.1:8000
    python run_web.py --port 9000 --host 0.0.0.0

Plain HTTP for local use, so the session cookie must not be marked Secure -
that is set here rather than left to the caller.
"""

import argparse
import multiprocessing
import os
import webbrowser


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="restart on code changes")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    os.environ.setdefault("ELBASH_SECURE_COOKIES", "0")

    import uvicorn

    if not args.no_browser and not args.reload:
        host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
        webbrowser.open(f"http://{host}:{args.port}/")

    uvicorn.run("web.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    # The PDF stage fans pages out to worker processes.
    multiprocessing.freeze_support()
    main()
