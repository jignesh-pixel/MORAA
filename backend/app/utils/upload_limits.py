"""Bounded reading of uploaded files (SEC-8).

``await file.read()`` pulls a whole file into memory before anything checks its size. ``read_capped`` reads in
chunks and stops as soon as the size limit is passed, so an oversized upload costs at most one chunk beyond the limit.
"""

from app.config import settings

CHUNK_BYTES = 256 * 1024

# Base64 text is 4/3 the size of the bytes it carries; the slack covers a data-URL prefix.
MAX_BASE64_CHARS = settings.MAX_UPLOAD_SIZE_BYTES * 4 // 3 + 1024


async def read_capped(upload_file, limit: int | None = None) -> bytes:
    """Read an UploadFile fully, refusing (ValueError) once it is larger than ``limit`` bytes."""
    limit = settings.MAX_UPLOAD_SIZE_BYTES if limit is None else limit
    chunks = []
    total = 0
    while True:
        chunk = await upload_file.read(CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ValueError(f"File too large. Maximum size is {settings.MAX_UPLOAD_SIZE_MB}MB")
        chunks.append(chunk)
    return b"".join(chunks)
