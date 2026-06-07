from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator, BinaryIO, Protocol, runtime_checkable

import aiofiles


@runtime_checkable
class StorageBackend(Protocol):
    """Abstract storage interface. LocalStorage today, S3Storage post-MVP.

    Swapping backends should require only changing the binding in the
    dependency container — no caller changes.
    """

    async def save_stream(
        self,
        file_id: str,
        filename: str,
        stream: AsyncIterator[bytes],
        max_bytes: int,
    ) -> tuple[Path, int]:
        ...

    def get_path(self, file_id: str, filename: str) -> Path: ...

    async def delete(self, file_id: str, filename: str) -> None: ...

    async def exists(self, file_id: str, filename: str) -> bool: ...


class FileTooLargeError(Exception):
    def __init__(self, limit_bytes: int):
        super().__init__(f"upload exceeds limit of {limit_bytes} bytes")
        self.limit_bytes = limit_bytes


def _safe_object_name(file_id: str, filename: str) -> str:
    safe = Path(filename).name
    return f"{file_id}_{safe}"


class LocalStorage:
    """Filesystem-backed storage. Files land under `<root>/<file_id>_<filename>`.

    Path layout intentionally flat — keeps it trivial to mirror onto S3 keys later.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def get_path(self, file_id: str, filename: str) -> Path:
        return self.root / _safe_object_name(file_id, filename)

    async def save_stream(
        self,
        file_id: str,
        filename: str,
        stream: AsyncIterator[bytes],
        max_bytes: int,
    ) -> tuple[Path, int]:
        target = self.get_path(file_id, filename)
        written = 0
        async with aiofiles.open(target, "wb") as f:
            async for chunk in stream:
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    await f.close()
                    target.unlink(missing_ok=True)
                    raise FileTooLargeError(max_bytes)
                await f.write(chunk)
        return target, written

    async def exists(self, file_id: str, filename: str) -> bool:
        return self.get_path(file_id, filename).exists()

    async def delete(self, file_id: str, filename: str) -> None:
        self.get_path(file_id, filename).unlink(missing_ok=True)


def build_storage(backend: str, root: Path) -> StorageBackend:
    if backend == "local":
        return LocalStorage(root)
    raise ValueError(
        f"unsupported storage backend: {backend!r} (S3Storage is post-MVP)"
    )
