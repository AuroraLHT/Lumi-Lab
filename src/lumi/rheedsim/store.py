"""Structures kept by name: the built-ins, plus whatever has been saved.

A saved structure is one JSON file in the store's folder, holding what it was given
(the CIF text or the manual cell) rather than the expanded cell, so it is re-read
with whatever the parser does then -- and a CIF can be copied back out as it came in.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from lumi.contracts.payloads.simulation import ManualStructure, SaveStructure, StructureSpec

from .crystal import Crystal, from_cif, from_manual
from .library import BUILTINS, builtin


class StructureStore:
    def __init__(self, root: str | Path | None) -> None:
        #: None = built-ins only; nothing can be saved.
        self.root = Path(root) if root else None

    # --- reading -----------------------------------------------------------

    def names(self) -> list[str]:
        """Built-ins first, in their own order, then saved ones by name."""
        return [*BUILTINS, *(n for n in self.saved_names() if n not in BUILTINS)]

    def saved_names(self) -> list[str]:
        if self.root is None or not self.root.is_dir():
            return []
        return sorted(p.stem for p in self.root.glob("*.json"))

    def get(self, name: str) -> Crystal:
        crystal = builtin(name)
        if crystal is not None:
            return crystal
        path = self._path(name)
        if path is None or not path.is_file():
            raise KeyError(f"no structure named {name!r}; known: {', '.join(self.names())}")
        record = json.loads(path.read_text())
        if record.get("cif") is not None:
            return from_cif(record["cif"], name=name, source="saved",
                            description=record.get("description", ""))
        return from_manual(name, ManualStructure.model_validate(record["manual"]),
                           source="saved", description=record.get("description", ""))

    def cif(self, name: str) -> str | None:
        path = self._path(name)
        if path is None or not path.is_file():
            return None
        return json.loads(path.read_text()).get("cif")

    def resolve(self, spec: StructureSpec) -> Crystal:
        if spec.name is not None:
            return self.get(spec.name)
        if spec.cif is not None:
            return from_cif(spec.cif)
        assert spec.manual is not None
        return from_manual("manual", spec.manual)

    # --- writing -----------------------------------------------------------

    def save(self, req: SaveStructure) -> Crystal:
        if self.root is None:
            raise ValueError("this simulator has no structures folder; nothing can be saved "
                             "(set simulation.rheed.structures_path)")
        if req.name in BUILTINS:
            raise ValueError(f"{req.name!r} is a built-in structure; pick another name")
        path = self._path(req.name)
        assert path is not None
        if path.exists() and not req.overwrite:
            raise ValueError(f"a structure named {req.name!r} exists; pass overwrite=true")
        # Parse before writing: a structure that cannot be simulated is not kept.
        if req.cif is not None:
            crystal = from_cif(req.cif, name=req.name, source="saved", description=req.description)
        else:
            assert req.manual is not None
            crystal = from_manual(req.name, req.manual, source="saved", description=req.description)
        record = {"description": req.description,
                  "cif": req.cif,
                  "manual": req.manual.model_dump() if req.manual else None}
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.part")
        tmp.write_text(json.dumps(record, indent=1))
        os.replace(tmp, path)
        return crystal

    def delete(self, name: str) -> None:
        if name in BUILTINS:
            raise ValueError(f"{name!r} is built in and cannot be deleted")
        path = self._path(name)
        if path is None or not path.is_file():
            raise KeyError(f"no saved structure named {name!r}")
        path.unlink()

    def _path(self, name: str) -> Path | None:
        if self.root is None or "/" in name or "\\" in name or name.startswith("."):
            return None
        return self.root / f"{name}.json"
