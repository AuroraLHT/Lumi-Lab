"""`uv run lumi-codegen [--check]`

Generates every derived artifact from src/lumi/contracts/. With --check it
regenerates in memory and diffs against what is on disk, exiting non-zero on drift --
which is what CI runs.

The --check gate is not optional infrastructure. Generated code without a drift gate
rots within a week: someone edits a contract, forgets to regenerate, and the committed
client silently describes a wire format that no longer exists. That is exactly how
tests/pascal/test_communication.py ended up asserting on a header schema
(`headers["type"] == "execution_result"`) that had been renamed long before.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

from lumi.contracts import REGISTRY, canonical_dict, contract_hash
from lumi.path import PROJECT_ROOT

from . import python_client, ts_client

PY_CLIENTS = PROJECT_ROOT / "src" / "lumi" / "generated" / "clients"
TS_CLIENT = PROJECT_ROOT / "web" / "src" / "generated" / "lumi.ts"
SCHEMAS = PROJECT_ROOT / "schemas"


def artifacts() -> dict[Path, str]:
    """Every file we own, and what it should contain."""
    out: dict[Path, str] = {}

    for contract in REGISTRY.values():
        out[PY_CLIENTS / f"{contract.name}.py"] = python_client.emit(contract)

    out[PY_CLIENTS / "__init__.py"] = _clients_init()
    # No API bridge: the browser talks directly to the broker over STOMP (see the
    # generated TS client). The API's remaining job is auth / profile / login, which is
    # not derived from the message contract.
    out[TS_CLIENT] = ts_client.emit()
    out[SCHEMAS / "contract.json"] = json.dumps(canonical_dict(), indent=2, sort_keys=True) + "\n"
    return out


def _clients_init() -> str:
    from .common import banner, pascal

    lines = [banner(), '"""Generated clients. One per node."""', ""]
    for name in REGISTRY:
        lines.append(f"from .{name} import {pascal(name)}Client")
    lines += ["", "__all__ = ["]
    for name in REGISTRY:
        lines.append(f'    "{pascal(name)}Client",')
    lines += ["]", ""]
    return "\n".join(lines)


def validate(files: dict[Path, str]) -> None:
    """Never write Python that does not parse.

    `--check` only diffs text, so it will happily report a syntactically broken file
    as "in sync" -- which is how a generated bridge.py with a stray `,)` in it got
    past the gate once already.
    """
    for path, content in files.items():
        if path.suffix != ".py":
            continue
        try:
            compile(content, str(path), "exec")
        except SyntaxError as exc:
            raise SystemExit(
                f"lumi-codegen produced invalid Python for {path.name}: "
                f"line {exc.lineno}: {exc.msg}\n"
                f"This is a bug in the emitter, not in the contract."
            ) from exc


def write(files: dict[Path, str]) -> list[Path]:
    validate(files)
    written = []
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_text() == content:
            continue
        path.write_text(content)
        written.append(path)
    return written


def check(files: dict[Path, str]) -> list[str]:
    problems = []
    for path, expected in files.items():
        rel = path.relative_to(PROJECT_ROOT)
        if not path.exists():
            problems.append(f"{rel}: missing -- run `uv run lumi-codegen`")
            continue
        actual = path.read_text()
        if actual == expected:
            continue
        diff = "\n".join(
            list(
                difflib.unified_diff(
                    actual.splitlines(), expected.splitlines(),
                    fromfile=f"{rel} (on disk)", tofile=f"{rel} (from contract)",
                    lineterm="",
                )
            )[:40]
        )
        problems.append(f"{rel}: out of date --\n{diff}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(prog="lumi-codegen")
    parser.add_argument(
        "--check", action="store_true",
        help="fail if anything on disk differs from the contract (used by CI)",
    )
    args = parser.parse_args()

    files = artifacts()

    if args.check:
        validate(files)  # a broken emitter must fail the gate, not slip through it
        problems = check(files)
        if problems:
            print("Generated code is out of sync with src/lumi/contracts/:\n", file=sys.stderr)
            for p in problems:
                print(f"  {p}\n", file=sys.stderr)
            print("Run `uv run lumi-codegen` and commit the result.", file=sys.stderr)
            return 1
        print(f"generated code is in sync (contract {contract_hash()})")
        return 0

    written = write(files)
    if not written:
        print(f"already up to date (contract {contract_hash()})")
    else:
        for path in written:
            print(f"wrote {path.relative_to(PROJECT_ROOT)}")
        print(f"\n{len(written)} file(s) from contract {contract_hash()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
