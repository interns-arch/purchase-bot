from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK_SIZE = 65536


def sha256_of_file(file_path: Path) -> str:
    """Compute the SHA-256 hex digest of a file's raw bytes."""
    digest = hashlib.sha256()

    with file_path.open("rb") as file:
        for chunk in iter(lambda: file.read(_CHUNK_SIZE), b""):
            digest.update(chunk)

    return digest.hexdigest()
