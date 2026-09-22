"""Small dependency-free HTTP probe for container health and contract tests."""

from __future__ import annotations

import argparse
import sys
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--data", help="JSON request body; switches the request to POST")
    args = parser.parse_args()

    body = args.data.encode("utf-8") if args.data is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    request = urllib.request.Request(args.url, data=body, headers=headers)

    # Health checks must stay local and deterministic even if a deployment
    # environment defines HTTP_PROXY.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5) as response:
        sys.stdout.buffer.write(response.read())


if __name__ == "__main__":
    main()
