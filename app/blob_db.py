"""Sync the SQLite file to Vercel Blob.

Local runs and tests leave this unused. On Vercel the function disk is
ephemeral, so the day's canvas lives in a Blob object and is pulled
into /tmp for each process.
"""

from __future__ import annotations

import os

PATHNAME = "agent-pixel/state.sqlite"


class BlobMissing(Exception):
    """The canvas object has not been written yet."""


def configured() -> bool:
    if os.environ.get("BLOB_READ_WRITE_TOKEN", "").strip():
        return True
    if os.environ.get("VERCEL_BLOB_READ_WRITE_TOKEN", "").strip():
        return True
    return bool(os.environ.get("VERCEL") and os.environ.get("BLOB_STORE_ID", "").strip())


def pathname() -> str:
    return os.environ.get("BLOB_PATHNAME", PATHNAME).strip() or PATHNAME


def access() -> str:
    mode = os.environ.get("BLOB_ACCESS", "private").strip().lower()
    if mode not in ("private", "public"):
        return "private"
    return mode


def stat(name: str) -> str | None:
    from vercel.blob import BlobNotFoundError
    from vercel.blob.sync import head_blob

    try:
        head = head_blob(name)
    except BlobNotFoundError as exc:
        raise BlobMissing() from exc
    return head.etag


def download(name: str) -> tuple[bytes, str | None]:
    from vercel.blob import BlobNotFoundError
    from vercel.blob.sync import get_blob

    try:
        result = get_blob(name, access=access())
    except BlobNotFoundError as exc:
        raise BlobMissing() from exc
    return result.body, result.metadata.etag


def upload(name: str, body: bytes) -> str:
    from vercel.blob.sync import put_blob

    result = put_blob(
        name,
        body,
        access=access(),
        content_type="application/x-sqlite3",
        add_random_suffix=False,
        allow_overwrite=True,
        cache_control_max_age=0,
    )
    return result.etag
