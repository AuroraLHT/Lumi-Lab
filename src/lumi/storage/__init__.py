"""HDF5 recordings: the v2 writer (v2), readers for v1 and v2 (reader), the History
API's archive (archive), the storage node's handlers (handlers), v1 -> v2 (convert).

    from lumi.storage import open_recording
"""

from __future__ import annotations


def __getattr__(name: str):
    # Lazy, so importing lumi.storage.handlers does not pay for the reader's imports.
    if name in ("open_recording", "Recording"):
        from lumi.storage import reader
        return getattr(reader, name)
    raise AttributeError(name)
