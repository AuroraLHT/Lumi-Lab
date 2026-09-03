#!/usr/bin/env python
"""Build the .ipynb files from the plain-Python sources beside them.

Notebooks are JSON, which makes them miserable to review and worse to merge -- a
one-line change to a cell shows up as a rewritten blob. So each notebook is authored
here as an ordinary list of (kind, text) cells and generated. Edit the `_cells`
function of the notebook you want to change and re-run:

    uv run notebooks/build_notebooks.py

Outputs are written with no execution counts and no outputs, so a committed notebook
is a diff of its source and nothing else.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

MD = "markdown"
CODE = "code"


def notebook(cells: list[tuple[str, str]]) -> dict:
    return {
        "cells": [
            {
                "cell_type": kind,
                "metadata": {},
                "source": (text.strip("\n") + "\n").splitlines(keepends=True),
                **({"execution_count": None, "outputs": []} if kind == CODE else {}),
            }
            for kind, text in cells
        ],
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "version": "3.11"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def write(name: str, cells: list[tuple[str, str]]) -> Path:
    path = HERE / name
    path.write_text(json.dumps(notebook(cells), indent=1) + "\n")
    return path


def main() -> None:
    from _single_deposition import cells as single_cells
    from _bo_deposition import cells as bo_cells

    for name, cells in [
        ("SingleDeposition.ipynb", single_cells()),
        ("BODeposition.ipynb", bo_cells()),
    ]:
        path = write(name, cells)
        print(f"wrote {path.relative_to(HERE.parent)} ({len(cells)} cells)")


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(HERE))
    main()
