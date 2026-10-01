"""Where a measurement's attached files live: a folder beside growth.db.

growth.db indexes them (the `measurement_file` table); the bytes sit here, one folder
per file named by its uuid, holding the file under a cleaned version of the name it was
uploaded with. The uuid keeps two uploads of `scan.raw` apart; the name keeps the
folder browsable by a person, which matters for the one copy of an instrument's raw
data.

A file is written to a temporary name and renamed into place, so a crash mid-upload
leaves a stray temp file rather than a truncated one that the index points at.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

#: Windows-illegal characters, path separators and control characters.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(name: str) -> str:
    """The upload's own name, made safe to use as a file name on any OS."""
    name = _UNSAFE.sub("_", Path(name.replace("\\", "/")).name).strip(" .")
    return name[:200] or "file"


def guess_media_type(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


@dataclass(frozen=True)
class StoredFile:
    file_uuid: str
    #: Relative to the store's root; what the index records.
    stored_path: str
    size_bytes: int
    sha256: str


class MeasurementFileStore:
    def __init__(self, root: str | Path, max_bytes: int,
                 limit_setting: str = "experiment.measurement_file_max_bytes") -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes
        #: Named in the refusal, so whoever hits the limit knows what to raise.
        self.limit_setting = limit_setting

    def save(self, data: bytes, file_name: str, *, file_uuid: str | None = None) -> StoredFile:
        """Write one file. Passing the `file_uuid` of an earlier save puts this file in
        the same folder -- for a record made of several files, like a snapshot's frame
        and its JPEG."""
        if len(data) == 0:
            raise ValueError("the file is empty")
        if len(data) > self.max_bytes:
            raise ValueError(f"the file is {len(data)} bytes; the limit is {self.max_bytes} "
                             f"({self.limit_setting})")
        shared = file_uuid is not None
        file_uuid = file_uuid or uuid.uuid4().hex
        folder = self.root / file_uuid
        folder.mkdir(parents=True, exist_ok=shared)
        final = folder / safe_name(file_name)
        tmp = folder / f".{final.name}.part"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, final)
        return StoredFile(
            file_uuid=file_uuid,
            stored_path=final.relative_to(self.root).as_posix(),
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )

    def path(self, stored_path: str) -> Path:
        path = (self.root / stored_path).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError(f"stored path escapes the file store: {stored_path!r}")
        return path

    def read(self, stored_path: str) -> bytes:
        path = self.path(stored_path)
        if not path.is_file():
            raise FileNotFoundError(f"the file is indexed but missing on disk: {path}")
        return path.read_bytes()
