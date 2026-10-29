"""Sync the SQLite file to one private Vercel Blob.

Local runs and tests leave this unused. On Vercel each instance has its own
disk, so the canvas lives at ``agent-pixel/state.sqlite``. Uploads use the
etag from the read they were based on (``x-if-match``). A mismatch means
another instance stored a newer file and this upload did not land. Reads of
the bytes bypass the CDN cache (``cache=0``); a cached copy can stay stale
for about a minute after an overwrite, which is long enough for a tick to
report the previous slice.
"""

from __future__ import annotations

import os

PATHNAME = "agent-pixel/state.sqlite"


class BlobMissing(Exception):
    """The canvas object has not been written yet."""


class BlobConflict(Exception):
    """The etag we edited is no longer current. Nothing was uploaded."""


class BlobStale(Exception):
    """The downloaded bytes did not match the catalog etag."""


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


def etag_key(value: str | None) -> str | None:
    """Compare etags without weak-validator or quote differences."""
    if not value:
        return None
    text = value.strip()
    if text.upper().startswith("W/"):
        text = text[2:].strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    return text or None


def stat(name: str) -> str | None:
    from vercel.blob import BlobNotFoundError
    from vercel.blob.sync import head_blob

    try:
        head = head_blob(name)
    except BlobNotFoundError as exc:
        raise BlobMissing() from exc
    return head.etag


def download(name: str) -> tuple[bytes, str | None]:
    """Return the latest bytes and the catalog etag they belong to."""
    body, catalog_etag, body_etag = _download_once(name)
    if _mismatched(catalog_etag, body_etag):
        body, catalog_etag, body_etag = _download_once(name)
        if _mismatched(catalog_etag, body_etag):
            raise BlobStale("The canvas download did not match the blob catalog.")
    return body, catalog_etag or body_etag


def upload(name: str, body: bytes, if_match: str | None = None) -> str:
    """Store the SQLite file only if the blob still has ``if_match``.

    The first save omits ``if_match``. Later saves send the etag from the
    read this file was edited from. A 412 leaves the blob unchanged.
    """
    return put_bytes(
        name,
        body,
        exclusive=False,
        if_match=if_match,
        content_type="application/x-sqlite3",
    )


def put_bytes(
    name: str,
    body: bytes,
    *,
    exclusive: bool,
    if_match: str | None = None,
    content_type: str = "application/json",
) -> str:
    """Store ``body``. ``exclusive`` refuses to overwrite an existing object.

    An update passes the etag from the read it was based on. A mismatch or an
    exclusive put against an object that already exists raises ``BlobConflict``
    and leaves the blob unchanged.
    """
    from vercel._internal.core.http import RawBody
    from vercel._internal.core.iter_coroutine import iter_coroutine
    from vercel._internal.core.session import get_active_sync_session
    from vercel.blob._internal.api_client import _PutRequest
    from vercel.blob._internal.service import get_sync_blob_service
    from vercel.blob.errors import BlobError, BlobPreconditionFailedError

    service = get_sync_blob_service(get_active_sync_session())
    request = _PutRequest(
        pathname=name,
        access=access(),
        allow_overwrite=not exclusive,
        content_type=content_type,
        cache_control_max_age=0,
    )

    async def _put() -> str:
        credentials = await service._get_credentials()
        headers = request._to_headers()
        if if_match:
            headers["x-if-match"] = if_match
        try:
            response = await service._api_client._request(
                "PUT",
                "",
                credentials=credentials,
                params={"pathname": request.pathname},
                headers=headers,
                body=RawBody(body),
            )
        except BlobPreconditionFailedError as exc:
            raise BlobConflict() from exc
        except BlobError as exc:
            if exc.status_code in (409, 412):
                raise BlobConflict() from exc
            raise
        payload = response.json()
        etag = payload.get("etag") if isinstance(payload, dict) else None
        if not isinstance(etag, str) or not etag:
            raise BlobStale("The blob upload did not return an etag.")
        return etag

    try:
        return iter_coroutine(_put())
    except BlobPreconditionFailedError as exc:
        raise BlobConflict() from exc
    except BlobError as exc:
        if exc.status_code in (409, 412):
            raise BlobConflict() from exc
        raise


def _mismatched(catalog_etag: str | None, body_etag: str | None) -> bool:
    if not catalog_etag or not body_etag:
        return False
    return etag_key(catalog_etag) != etag_key(body_etag)


def _download_once(name: str) -> tuple[bytes, str | None, str | None]:
    from vercel._internal.core.iter_coroutine import iter_coroutine
    from vercel._internal.core.session import get_active_sync_session
    from vercel.blob import BlobNotFoundError
    from vercel.blob._internal.service import get_sync_blob_service

    service = get_sync_blob_service(get_active_sync_session())

    async def _get() -> tuple[bytes, str | None, str | None]:
        try:
            meta = await service.head(name)
        except BlobNotFoundError as exc:
            raise BlobMissing() from exc
        url = meta.download_url
        if "cache=0" not in url:
            url = url + ("&" if "?" in url else "?") + "cache=0"
        credentials = await service._get_credentials()
        stream, metadata = await service._api_client.open_download(url, credentials=credentials)
        try:
            body = await stream.read()
        except BlobNotFoundError as exc:
            raise BlobMissing() from exc
        return body, meta.etag, metadata.etag

    try:
        return iter_coroutine(_get())
    except BlobNotFoundError as exc:
        raise BlobMissing() from exc
