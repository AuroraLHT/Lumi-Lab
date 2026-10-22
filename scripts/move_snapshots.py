"""Move the take_snapshot stills to a new folder, keeping every snapshot readable.

    uv run python scripts/move_snapshots.py --dry-run     # say what would move
    uv run python scripts/move_snapshots.py               # move them
    uv run python scripts/move_snapshots.py --from cfg/snapshots --to /data/lumi/snapshots

Then point `experiment.snapshots_path` at the new folder (if `--to` is not already it)
and start the experiment node.

growth.db stores each still's files relative to the snapshots folder
(`snapshot.raw_path` / `jpeg_path`), so moving the folder's contents is the whole
job: nothing in the database changes. This moves one snapshot folder at a time,
never overwrites, and never deletes a snapshot -- only the emptied source folder is
removed at the end. Afterwards every snapshot row is checked against the new folder:
both files present, the .npy matching its recorded SHA-256.

Defaults: `--from` is the old location, a `snapshots` folder beside growth.db (what an
empty `snapshots_path` means); `--to` is `experiment.snapshots_path`. Stop the
experiment node first -- it holds the old path and would go on writing there.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from lumi.config import settings


def _experiment_node_running() -> bool:
    try:
        return subprocess.run(["pgrep", "-f", r"nodes/experiment\.py"],
                              capture_output=True).returncode == 0
    except FileNotFoundError:  # no pgrep: cannot tell, so do not block on it
        return False


def _rows(db_path: Path) -> list[tuple[str, str, str, str]]:
    # Read-only: this script never writes growth.db.
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        return conn.execute(
            "SELECT snapshot_uuid, raw_path, jpeg_path, raw_sha256 FROM snapshot"
        ).fetchall()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(root: Path, rows: list[tuple[str, str, str, str]]) -> list[str]:
    """Every snapshot row's files under `root`, as a list of problems."""
    problems = []
    for snapshot_uuid, raw_path, jpeg_path, raw_sha256 in rows:
        raw, jpeg = root / raw_path, root / jpeg_path
        if not raw.is_file():
            problems.append(f"{snapshot_uuid}: missing {raw}")
        elif _sha256(raw) != raw_sha256:
            problems.append(f"{snapshot_uuid}: {raw} does not match its recorded SHA-256")
        if not jpeg.is_file():
            problems.append(f"{snapshot_uuid}: missing {jpeg}")
    return problems


def main() -> int:
    growth_db = Path(settings.experiment.growth_db_path)
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--from", dest="src", type=Path, default=growth_db.parent / "snapshots",
                        help="where the stills are now (default: beside growth.db)")
    parser.add_argument("--to", dest="dst", type=Path,
                        default=Path(settings.experiment.get("snapshots_path") or growth_db.parent / "snapshots"),
                        help="where they go (default: experiment.snapshots_path)")
    parser.add_argument("--db", type=Path, default=growth_db, help=f"growth.db (default {growth_db})")
    parser.add_argument("--dry-run", action="store_true", help="say what would move, move nothing")
    parser.add_argument("--force", action="store_true", help="run even with the experiment node up")
    args = parser.parse_args()

    src, dst = args.src, args.dst
    if src.resolve() == dst.resolve():
        print(f"--from and --to are the same folder ({src}); nothing to move.")
        return 0
    if not src.is_dir():
        print(f"nothing to move: {src} does not exist.")
        return 0
    if not args.dry_run and not args.force and _experiment_node_running():
        print("error: the experiment node is running. It keeps writing to the old folder and "
              "loses track of moved stills until restarted. Stop it first "
              "(scripts/stop_nodes.sh), or pass --force.", file=sys.stderr)
        return 1

    folders = sorted(p for p in src.iterdir() if p.is_dir())
    clashes = [p.name for p in folders if (dst / p.name).exists()]
    if clashes:
        print(f"error: {len(clashes)} snapshot folder(s) already exist in {dst}, refusing to "
              f"overwrite: {', '.join(clashes[:5])}", file=sys.stderr)
        return 1

    print(f"{'would move' if args.dry_run else 'moving'} {len(folders)} snapshot folder(s): {src} -> {dst}")
    if args.dry_run:
        return 0

    dst.mkdir(parents=True, exist_ok=True)
    for folder in folders:
        shutil.move(str(folder), str(dst / folder.name))
    # Only the now-empty source; anything else left in it (a stray file) stays put.
    try:
        src.rmdir()
    except OSError:
        print(f"note: {src} still has files that are not snapshot folders; left in place.")

    rows = _rows(args.db) if args.db.is_file() else []
    problems = verify(dst, rows)
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    print(f"checked {len(rows)} snapshot record(s) in {args.db}: "
          f"{'all readable' if not problems else f'{len(problems)} problem(s)'}")

    configured = Path(settings.experiment.get("snapshots_path") or growth_db.parent / "snapshots")
    if configured.resolve() != dst.resolve():
        print(f"next: set experiment.snapshots_path = \"{dst}\" in cfg/settings.toml "
              f"(it is now {configured}).")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
