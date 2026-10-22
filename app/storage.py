"""Task-file storage. Primary: Supabase Storage, a private bucket, signed URLs generated
only for the GA console (never posted to Teams). Falls back to the local TaskFile table
(app/db.py) when SUPABASE_URL/SUPABASE_SERVICE_KEY aren't set - every function here
degrades gracefully without them, same pattern as app/llm.py without a Gemini key."""
import logging
import uuid

import requests

from .config import settings

log = logging.getLogger("virtual_ga.storage")
BUCKET = "task-files"


def enabled() -> bool:
    return bool(settings.SUPABASE_URL and settings.SUPABASE_SERVICE_KEY)


def _headers() -> dict:
    return {"Authorization": f"Bearer {settings.SUPABASE_SERVICE_KEY}"}


def upload(data: bytes, mime: str) -> str | None:
    """Returns the storage object path on success, or None (caller falls back to the
    local TaskFile table) if Supabase isn't configured or the upload fails."""
    if not enabled():
        return None
    path = str(uuid.uuid4())
    try:
        r = requests.post(f"{settings.SUPABASE_URL}/storage/v1/object/{BUCKET}/{path}",
                          headers={**_headers(), "Content-Type": mime}, data=data, timeout=30)
        if r.status_code >= 300:
            log.warning("Supabase upload failed (%s): %s", r.status_code, r.text[:300])
            return None
        return path
    except Exception as e:
        log.warning("Supabase upload error: %s", e)
        return None


def signed_url(path: str, expires_in: int = 300) -> str | None:
    """A short-lived URL for the GA console to open/download the file. Never generated
    for or sent to Teams."""
    if not enabled():
        return None
    try:
        r = requests.post(f"{settings.SUPABASE_URL}/storage/v1/object/sign/{BUCKET}/{path}",
                          headers=_headers(), json={"expiresIn": expires_in}, timeout=15)
        if r.status_code >= 300:
            log.warning("Supabase sign failed (%s): %s", r.status_code, r.text[:300])
            return None
        signed = r.json().get("signedURL")
        return f"{settings.SUPABASE_URL}/storage/v1{signed}" if signed else None
    except Exception as e:
        log.warning("Supabase sign error: %s", e)
        return None


def delete(path: str) -> None:
    if not enabled():
        return
    try:
        r = requests.delete(f"{settings.SUPABASE_URL}/storage/v1/object/{BUCKET}/{path}",
                            headers=_headers(), timeout=15)
        if r.status_code >= 300:
            log.warning("Supabase delete failed (%s): %s", r.status_code, r.text[:300])
    except Exception as e:
        log.warning("Supabase delete error: %s", e)
