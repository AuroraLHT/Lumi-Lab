#!/usr/bin/env bash
#
# Install the detection node's model stack: torch, torchvision, OpenMMLab's
# mmengine/mmcv/mmdet (via `mim`, which picks prebuilt wheels matched to the
# installed torch+CUDA build), and the internal `rhana` package (rhana.labeler
# imports torchvision directly). None of these are declared in pyproject.toml's
# `detection` extra because they are version-coupled to the host's CUDA build, which
# uv/pip cannot pin portably -- see the comment above that extra.
#
# Usage:
#   scripts/install_detection_deps.sh [--cuda-index cu121] [--torch-version 2.1.0]
#                                      [--torchvision-version 0.16.0]
#                                      [--rhana-ref main] [--force]
#
# Requires an existing venv (run 'uv sync --extra detection' first) on Python <=3.11
# -- see below. Safe to re-run: torch and the mim packages are skipped if already
# importable, unless --force.
#
# torch defaults to 2.1.0, not latest, and this only works on Python <=3.11:
# mmdet hard-asserts mmcv<2.2.0 at import time (mmdet/__init__.py), but OpenMMLab's
# prebuilt mmcv wheels for Python 3.12 start at 2.2.0 -- there is no <2.2.0 wheel for
# cp312 at all, only cp38-cp311, so mmdet cannot run on a 3.12 venv without compiling
# mmcv from source (which then needs a system CUDA toolkit whose *major* version
# exactly matches torch's build -- a real system-level dependency, not something this
# script installs). On <=3.11, torch2.1.0+<cuda-index> is the newest series with a
# prebuilt mmcv wheel still under 2.2.0 (checked against
# https://download.openmmlab.com/mmcv/dist/<cuda-index>/torchX.Y.0/index.html); newer
# torch series on that index only ship mmcv==2.2.0, which mmdet refuses to import.
# torchvision defaults to 0.16.0 to match -- see pytorch.org's compatibility matrix
# if you override --torch-version, since torch/torchvision are released in lockstep.
#
# This also forces numpy<2 at the end: torch2.1.0's compiled extensions predate
# NumPy 2.0 (June 2024) and crash on it ("Numpy is not available"), but the project's
# core dependencies declare numpy>=2.0. This script deliberately does NOT lower that
# floor in pyproject.toml -- only this venv gets the numpy<2 override, and running
# `uv sync` again afterward will silently bump numpy back to >=2.0 and re-break torch.
# Re-run this script (or `uv pip install "numpy<2"`) after any `uv sync`. This whole
# stack is a stopgap until mmdet is replaced -- see the pyproject.toml detection
# extra's comment.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

CUDA_INDEX="cu121"
TORCH_VERSION="2.1.0"
TORCHVISION_VERSION="0.16.0"
RHANA_REF=""
FORCE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --cuda-index) CUDA_INDEX="$2"; shift 2 ;;
        --torch-version) TORCH_VERSION="$2"; shift 2 ;;
        --torchvision-version) TORCHVISION_VERSION="$2"; shift 2 ;;
        --rhana-ref) RHANA_REF="$2"; shift 2 ;;
        --force) FORCE=1; shift ;;
        -h|--help) sed -n '3,38p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
done

PYTHON="$PROJECT_ROOT/.venv/bin/python"
MIM="$PROJECT_ROOT/.venv/bin/mim"
if [[ ! -x "$PYTHON" ]]; then
    echo "no virtualenv at .venv -- run 'uv sync --extra detection' first" >&2
    exit 1
fi

PY_VERSION="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PY_MAJOR="${PY_VERSION%.*}"
PY_MINOR="${PY_VERSION#*.}"
if [[ "$PY_MAJOR" -eq 3 && "$PY_MINOR" -ge 12 ]]; then
    echo "error: Python $PY_VERSION detected -- mmdet requires mmcv<2.2.0, and there is" >&2
    echo "  no prebuilt mmcv<2.2.0 wheel for Python 3.12+ (only cp38-cp311 exist)." >&2
    echo "  This install cannot succeed without compiling mmcv from source against a" >&2
    echo "  system CUDA toolkit matching torch's CUDA major version exactly -- not" >&2
    echo "  something this script does. Recreate .venv on Python 3.11 or earlier:" >&2
    echo "    rm -rf .venv && uv venv --python 3.11 .venv && uv sync --all-extras" >&2
    exit 1
fi

# mim imports pkg_resources at CLI startup. Whatever setuptools happens to be in
# the venv may be either too old (pre-3.12 releases unconditionally touch the
# now-removed pkgutil.ImpImporter, crashing on import) or too new (setuptools>=81
# dropped pkg_resources entirely). Pin into the window that has both the 3.12 fix
# and pkg_resources; drop this once mim/mmcv stop depending on pkg_resources.
pin_setuptools() {
    echo "==> pinning setuptools>=70,<81 (mim needs pkg_resources, which newer setuptools drops)"
    # A lower bound is required, not just <81: uv treats "install X<81" as already
    # satisfied by whatever is present (even the too-old 60.2.0 that openmim's own
    # resolution installs), and silently no-ops instead of reinstalling.
    uv pip install "setuptools>=70,<81"
}

# --all-extras, not --extra detection: `uv sync` reconciles the venv to exactly
# what's declared, so scoping this to a single extra would uninstall any other
# extra (camera, pascal, storage, api) already present in a normal dev venv. It
# also means every run of this script wipes the unmanaged torch/mmcv/mmdet/rhana/
# numpy<2 stack below (uv doesn't know about them -- they're not in pyproject.toml),
# so this script always reinstalls them regardless of the "already installed"
# checks; that's a few seconds from uv's cache, not a real cost.
echo "==> uv sync --all-extras"
uv sync --all-extras
pin_setuptools

if [[ $FORCE -eq 0 ]] && "$PYTHON" -c "import torch, torchvision" >/dev/null 2>&1; then
    echo "==> torch/torchvision already installed, skipping (--force to reinstall)"
else
    echo "==> installing torch==$TORCH_VERSION, torchvision==$TORCHVISION_VERSION (CUDA index: $CUDA_INDEX)"
    uv pip install "torch==$TORCH_VERSION" "torchvision==$TORCHVISION_VERSION" \
        --index-url "https://download.pytorch.org/whl/$CUDA_INDEX"
fi

if [[ $FORCE -eq 0 ]] && "$PYTHON" -c "import mmengine, mmcv, mmdet" >/dev/null 2>&1; then
    echo "==> mmengine/mmcv/mmdet already installed, skipping (--force to reinstall)"
else
    echo "==> installing openmim, then mmengine, mmcv, mmdet in order"
    uv pip install -U openmim
    # openmim's own dependency resolution drags setuptools back down to an old,
    # pre-3.12-safe version -- re-pin before invoking mim.
    pin_setuptools
    "$MIM" install mmengine
    # mmdet (see its __init__.py) hard-asserts mmcv<2.2.0 at import time, so pin the
    # upper bound explicitly -- otherwise mim happily installs the newest matching
    # wheel (2.2.0) and mmdet refuses to import.
    "$MIM" install "mmcv>=2.0.0,<2.2.0"
    "$MIM" install mmdet
    # mim's own resolution can leave `platformdirs` satisfied only by the copy
    # vendored inside setuptools/_vendor (visible to pip's metadata check, but not
    # on the normal import path), so yapf/mmengine's `import platformdirs` 404s at
    # runtime unless a real top-level install is forced here.
    uv pip install platformdirs
fi

RHANA_URL="git+https://github.com/AuroraLHT/rhana.git"
[[ -n "$RHANA_REF" ]] && RHANA_URL="${RHANA_URL}@${RHANA_REF}"

if [[ $FORCE -eq 0 ]] && "$PYTHON" -c "import rhana" >/dev/null 2>&1; then
    echo "==> rhana already installed, skipping (--force to reinstall)"
else
    echo "==> installing rhana from $RHANA_URL"
    uv pip install "$RHANA_URL"
fi

echo "==> forcing numpy<2 (torch==$TORCH_VERSION predates NumPy 2.0 ABI support)"
uv pip install "numpy<2"

echo
echo "detection stack installed:"
"$PYTHON" -c "
import numpy, torch, torchvision, mmengine, mmcv, mmdet, rhana
print(f'  numpy       {numpy.__version__}')
print(f'  torch       {torch.__version__} (cuda available: {torch.cuda.is_available()})')
print(f'  torchvision {torchvision.__version__}')
print(f'  mmengine    {mmengine.__version__}')
print(f'  mmcv        {mmcv.__version__}')
print(f'  mmdet       {mmdet.__version__}')
print(f'  rhana       {getattr(rhana, \"__version__\", \"unknown\")}')
"
echo
echo "note: re-run this script after any 'uv sync' -- it re-resolves numpy>=2.0 per"
echo "  pyproject.toml's core deps and will silently break torch/mmdet again."
