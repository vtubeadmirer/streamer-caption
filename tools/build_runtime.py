"""Build the portable Python runtime that the no-install launcher downloads on first run.

Layout inside the ZIP (mirrors a venv so every existing .bat keeps working through
the versions/<ver>/.venv junction):

    .venv/
      RUNTIME_VERSION          e.g. py312-v2
      Scripts/python.exe       CPython embeddable distribution
      Scripts/python312._pth   adds ../Lib/site-packages and enables `import site`
      Lib/site-packages/       pip-installed requirements.txt (incl. NVIDIA cuBLAS for GPU captions)

Usage:
    python tools/build_runtime.py --runtime-version py312-v2 [--dist dist]

Produces dist/runtime-<runtime-version>.zip and dist/runtime-manifest.json
({runtime_version, asset_name, sha256, size, python_version}).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = "3.12.10"  # last 3.12.x with an embeddable build (3.12.11+ are source-only)
PYTHON_TAG = "".join(PYTHON_VERSION.split(".")[:2])  # "312" as in python312.zip / python312._pth
EMBED_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
EMBED_SHA256 = ""  # filled from python.org when known; verification is best-effort below
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"
PRUNE_DIRS = {"__pycache__"}  # never prune package "tests"/"testing" dirs: torch imports torch.testing at runtime
# Never imported at runtime (torch C++ headers for building extensions, onnxruntime's offline
# ORT-format model converters) and home of the deepest paths in the ZIP.
PRUNE_TREES = ("torch/include", "onnxruntime/tools")
# The launcher extracts to <root>\runtime\.staging-<32 hex>\<entry>; entries up to 120 characters
# keep that under MAX_PATH (260) for install roots up to ~88 characters without long-path support.
MAX_ENTRY_PATH = 120
# ctranslate2 loads cuBLAS by name at the first GPU matmul, so a runtime without it passes every
# import check and only fails on a PC that has no CUDA Toolkit. ctranslate2 4.8 never calls cuDNN.
REQUIRED_CUDA_DLLS = ("nvidia/cublas/bin/cublas64_12.dll", "nvidia/cublas/bin/cublasLt64_12.dll")


SITECUSTOMIZE = '"""Embedded CPython (python3xx._pth) skips the normal sys.path setup: the script\'s own\ndirectory is not importable and PYTHONPATH is ignored. Streamer Caption runs scripts from\nthe app folder and imports core/, features/, tools/ relative to it, so put the script\ndirectory (or the working directory for -c / -m) first on sys.path, like regular Python."""\nimport os\nimport sys\n\n_argv0 = sys.argv[0] if sys.argv else ""\n_base = os.getcwd() if _argv0 in ("", "-c", "-m") else os.path.dirname(os.path.abspath(_argv0))\nif _base and _base not in sys.path:\n    sys.path.insert(0, _base)\n'

SMOKE_TEST = """
import ctypes, importlib.util, os
import faster_whisper, numpy, sounddevice, av, websockets, soundcard, ctranslate2, silero_vad
bin_dir = os.path.join(importlib.util.find_spec("nvidia.cublas").submodule_search_locations[0], "bin")
cublas = ctypes.WinDLL(os.path.join(bin_dir, "cublas64_12.dll"))
version = []
for prop in range(3):
    value = ctypes.c_int()
    if cublas.cublasGetProperty(prop, ctypes.byref(value)) != 0:
        raise SystemExit("cublasGetProperty failed")
    version.append(str(value.value))
print("ok ctranslate2", ctranslate2.__version__, "cublas", ".".join(version))
"""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "streamer-caption-runtime-build"})
    with urllib.request.urlopen(request, timeout=120) as response, target.open("wb") as handle:
        shutil.copyfileobj(response, handle, length=1024 * 1024)


def run(args: list[str], cwd: Path | None = None) -> None:
    result = subprocess.run(args, cwd=cwd, text=True)
    if result.returncode != 0:
        raise SystemExit(f"command failed ({result.returncode}): {' '.join(args)}")


def prune(site_packages: Path) -> int:
    removed = 0
    for relative in PRUNE_TREES:
        if (site_packages / relative).is_dir():
            shutil.rmtree(site_packages / relative)
            removed += 1
    for path in sorted(site_packages.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and path.name in PRUNE_DIRS and path.parent != site_packages:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    return removed


def fold_licenses(site_packages: Path) -> int:
    """Replace each *.dist-info/licenses tree with one licenses.txt. Its nested third-party paths
    are the longest in the ZIP, but the license texts must still ship with the redistributed wheels."""
    folded = 0
    for tree in sorted(site_packages.glob("*.dist-info/licenses")):
        target = tree.parent / "licenses.txt"
        if not tree.is_dir() or target.exists():
            continue
        parts = []
        for path in sorted(p for p in tree.rglob("*") if p.is_file()):
            text = path.read_text(encoding="utf-8", errors="replace").rstrip()
            parts.append(f"===== {path.relative_to(tree).as_posix()} =====\n{text}\n\n")
        target.write_text("".join(parts), encoding="utf-8")
        shutil.rmtree(tree)
        folded += 1
    return folded


def check_cuda_libraries(site_packages: Path) -> None:
    missing = [name for name in REQUIRED_CUDA_DLLS if not (site_packages / name).is_file()]
    if missing:
        raise SystemExit("runtime is missing CUDA libraries, GPU captions would need a CUDA Toolkit: " + ", ".join(missing))
    ctranslate2_dll = (site_packages / "ctranslate2" / "ctranslate2.dll").read_bytes()
    wanted = {name.decode() for name in re.findall(rb"cublas64_\d+\.dll", ctranslate2_dll)}
    shipped = {Path(name).name for name in REQUIRED_CUDA_DLLS}
    if not wanted or not wanted <= shipped:
        raise SystemExit(f"ctranslate2.dll loads {sorted(wanted)} but the runtime ships {sorted(shipped)}: align nvidia-cublas-cu* with ctranslate2's CUDA build")


def build(runtime_version: str, dist: Path, requirements: Path, keep_build: bool = False) -> dict:
    dist.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="sc-runtime-"))
    try:
        venv = work / ".venv"
        scripts = venv / "Scripts"
        site_packages = venv / "Lib" / "site-packages"
        scripts.mkdir(parents=True)
        site_packages.mkdir(parents=True)

        embed_zip = work / "python-embed.zip"
        print(f"downloading {EMBED_URL}")
        download(EMBED_URL, embed_zip)
        with zipfile.ZipFile(embed_zip) as archive:
            archive.extractall(scripts)
        pth = scripts / f"python{PYTHON_TAG}._pth"
        if not pth.is_file() or not (scripts / f"python{PYTHON_TAG}.zip").is_file():
            raise SystemExit(f"embeddable Python {PYTHON_VERSION} has no python{PYTHON_TAG}._pth / python{PYTHON_TAG}.zip")
        pth.write_text(f"python{PYTHON_TAG}.zip\n.\n..\\Lib\\site-packages\nimport site\n", encoding="utf-8")

        get_pip = work / "get-pip.py"
        download(GET_PIP_URL, get_pip)
        python = scripts / "python.exe"
        run([str(python), str(get_pip), "--no-warn-script-location", "--target", str(site_packages)])
        run([str(python), "-m", "pip", "install", "--no-warn-script-location", "--target", str(site_packages), "-r", str(requirements)])

        removed = prune(site_packages)
        folded = fold_licenses(site_packages)
        print(f"pruned {removed} dirs, folded {folded} license trees")
        check_cuda_libraries(site_packages)
        (site_packages / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
        (venv / "RUNTIME_VERSION").write_text(runtime_version + "\n", encoding="utf-8")

        # Smoke test inside the built runtime before packaging. -B: whether its imports leave
        # __pycache__ behind would otherwise depend on the build host (PYTHONDONTWRITEBYTECODE).
        check = subprocess.run([str(python), "-B", "-c", SMOKE_TEST], capture_output=True, text=True, cwd=str(venv))
        if check.returncode != 0:
            raise SystemExit(f"runtime smoke test failed:\n{check.stdout}\n{check.stderr}")
        print("runtime smoke test:", check.stdout.strip())

        entries = [(path, (Path(".venv") / path.relative_to(venv)).as_posix()) for path in sorted(venv.rglob("*")) if path.is_file()]
        too_long = sorted((name for _, name in entries if len(name) > MAX_ENTRY_PATH), key=len, reverse=True)
        if too_long:
            raise SystemExit(f"{len(too_long)} runtime paths exceed {MAX_ENTRY_PATH} characters (Windows MAX_PATH on extract):\n" + "\n".join(too_long[:20]))

        asset = dist / f"runtime-{runtime_version}.zip"
        if asset.exists():
            asset.unlink()
        with zipfile.ZipFile(asset, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path, name in entries:
                archive.write(path, name)
        manifest = {
            "runtime_version": runtime_version,
            "asset_name": asset.name,
            "sha256": sha256_of(asset),
            "size": asset.stat().st_size,
            "python_version": PYTHON_VERSION,
            "requirements_sha256": sha256_of(requirements),
            "pruned_dirs": removed,
        }
        (dist / "runtime-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return manifest
    finally:
        if not keep_build:
            shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the portable runtime ZIP")
    parser.add_argument("--runtime-version", default="py312-v2")
    parser.add_argument("--dist", default=str(ROOT / "dist"))
    parser.add_argument("--requirements", default=str(ROOT / "requirements.txt"))
    parser.add_argument("--keep-build", action="store_true")
    args = parser.parse_args()
    manifest = build(args.runtime_version, Path(args.dist), Path(args.requirements), keep_build=args.keep_build)
    print(json.dumps({"ok": True, **manifest}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
