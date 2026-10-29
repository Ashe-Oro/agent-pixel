#!/usr/bin/env python3
"""Check a deployed Agent Pixel host.

Usage:
    python scripts/check_prod.py https://your-project.vercel.app
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def fetch(url: str) -> tuple[int, str, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "agent-pixel-check"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.headers.get("content-type", ""), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("content-type", ""), exc.read()


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].startswith("http"):
        print("Usage: python scripts/check_prod.py https://<project>.vercel.app", file=sys.stderr)
        return 2
    base = argv[1].rstrip("/")
    failed = False

    status, _, body = fetch(base + "/health")
    print(f"GET /health {status}")
    persistent = None
    try:
        health = json.loads(body.decode())
    except json.JSONDecodeError:
        health = {}
    if status != 200 or health.get("service") != "agent-pixel":
        print("  expected 200 and service=agent-pixel")
        failed = True
    else:
        persistent = health.get("persistent")
        print(f"  persistent={persistent}")

    status, content_type, body = fetch(base + "/skill.md")
    text = body.decode("utf-8", errors="replace")
    print(f"GET /skill.md {status} {content_type}")
    if status != 200 or not text.startswith("# Agent Pixel") or "3-minute" not in text or "08:00" not in text:
        print("  expected the Agent Pixel skill with the morning schedule")
        failed = True

    status, content_type, body = fetch(base + "/watch")
    page = body.decode("utf-8", errors="replace")
    print(f"GET /watch {status} {content_type}")
    if (
        status != 200
        or "Agent Pixel" not in page
        or "mascot.png" not in page
        or 'data-canvas-outline="256"' not in page
        or "border: 1px solid #ffd36a" not in page
    ):
        print("  expected the watch page, the mascot, and a 256 canvas rim")
        failed = True

    status, content_type, body = fetch(base + "/static/mascot.png")
    print(f"GET /static/mascot.png {status} {content_type} {len(body)} bytes")
    if status != 200 or not body.startswith(b"\x89PNG"):
        print("  expected a PNG mascot")
        failed = True

    if persistent is False:
        print("Canvas storage is ephemeral. Link a Vercel Blob store before the first production paint.")
        failed = True
    if failed:
        return 1
    print("Agent Pixel is up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
