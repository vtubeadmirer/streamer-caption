Streamer Caption release architecture

Purpose

This release layer keeps executable versions separate from user-owned data. An update installs a new version beside the current version and changes only the version pointer after SHA-256 and archive validation succeed.

Installed layout

```text
StreamerCaption/
|-- START_STREAMER_CAPTION.bat
|-- state.json
|-- updater.json
|-- launcher/
|   |-- launcher.ps1
|   `-- updater.py
|-- versions/
|   |-- 1.0.0/
|   `-- 1.1.0/
|-- runtime/
|   `-- .venv/
|-- user-data/
|   |-- config.json
|   |-- data/
|   |-- logs/
|   |-- reports/
|   `-- samples/
`-- downloads/
```

Why this layout

- Version folders are immutable after installation, so an interrupted update cannot damage the running version.
- `state.json` stores `current_version` and `previous_version`; rollback swaps those pointers.
- `user-data` is never part of a release ZIP. Configuration, slang, protected terms, recordings, logs, and transcript databases survive updates.
- One shared Python runtime avoids duplicating several gigabytes for every application version.

First installation

1. Extract the GitHub Release ZIP.
2. Double-click `INSTALL.bat`.
3. The installer locates Python 3.12 or installs it through winget.
4. It creates the shared virtual environment and installs pinned direct dependencies.
5. It initializes user data and selects a capacity-based GPU profile.
6. Start the app with `START_STREAMER_CAPTION.bat` in the installed folder.

FFmpeg policy

FFmpeg is not bundled because the normal Faster Whisper and PyAV path does not require an external FFmpeg executable. Experimental features can request it separately through `INSTALL_OPTIONAL_FFMPEG.bat`. This keeps the normal release smaller and makes the extra license and download visible.

GPU policy

- No NVIDIA GPU detected: CPU compatible
- Less than 12GB VRAM: Light
- 12GB through 19GB VRAM: Balanced
- 20GB or more VRAM: High quality

The UI should expose Auto, Light, Balanced, and High quality rather than product-series names. The selected profile can still be changed later.

Update flow

The launcher checks GitHub at most once every `check_interval_hours` (24 hours by default). A manual `updater.py check --force` bypasses the cache. Cached results are reused only while the repository, prerelease policy, and installed version still match.

1. The launcher reads `updater.json`.
2. It checks the latest published GitHub Release, not every commit or draft.
3. It downloads `release-manifest.json` and the named ZIP asset.
4. It verifies launcher compatibility and SHA-256.
5. It rejects symbolic links and paths that escape the staging directory.
6. It extracts into a staging folder (`versions/.staging-<8 hex>`) and validates `VERSION`.
7. It moves the complete folder into `versions` and updates `state.json` atomically.
8. On launch failure, the launcher offers rollback to `previous_version`.

Versions compare numerically, prerelease parts included (`1.2.0-dev.9 < 1.2.0-dev.10 < 1.2.0-rc.1 < 1.2.0`, SemVer precedence).

Housekeeping (`keep_versions`, default 2 in `updater.json`): after every successful `install_archive`, `prune_install()` deletes the oldest `versions/<v>` folders until at most `keep_versions` remain. It never deletes `current_version`, `previous_version` (rollback), the version that is running the update (the updater infers it from its own path, `versions/<v>/launcher/updater.py`), or a version folder holding files a release never ships (`config.json`, slang/allow/block lists, `*.sqlite3`, recordings, real `logs`/`reports`/`samples` folders), which means the app once ran from there without the launcher's `user-data`. A folder is renamed to `versions/.trash-*` before deletion, so a version still in use by any process (open file, working directory) is skipped whole instead of half-deleted, and the junctions inside it (`.venv` to the runtime, `logs`/`reports`/`samples` to `user-data`) are removed as links, never followed. It also deletes `downloads/*.zip` and `*.partial` except the app ZIP just installed and a runtime ZIP that `runtime.json` still waits for, and sweeps `versions/.staging-*`, `versions/.trash-*`, `runtime/.staging-*` and `runtime/.old-*` folders older than an hour (left by interrupted installs). Housekeeping is best effort: failures are listed in the result's `pruned.errors` and never fail the install.

GitHub release process

1. Change `VERSION` and `release-manifest.json` to the same version.
2. Run `tests/test_release_system.py`.
3. Run `BUILD_RELEASE.bat`.
4. Confirm the ZIP and `release-manifest.json` in `dist`.
5. Push a matching tag such as `v1.0.0`.
6. GitHub Actions rejects a tag that does not match `VERSION` and publishes the Release (a tag with `-` becomes a prerelease).
7. Tags containing `-` are marked as prereleases, for example `v1.0.0-dev.1`.
8. Stable tags are visible to every install; prerelease tags only to installs with `allow_prerelease` (dev-channel portable builds set it automatically).

Repository configuration

The installer copies `updater.example.json` to the installed root as `updater.json`. It targets `vtubeadmirer/streamer-caption`. The repository must be public before unauthenticated users can check and download releases. Do not embed a personal access token in the app.

Public-build exclusions

The release builder rejects or excludes live configuration, slang dictionaries, protected and blocked terms, logs, reports, recordings, transcript databases, virtual environments, caches, downloads, state, and existing ZIP files.

Decisions still required before public launch

- Choose the project license. Do not publish as open source until the owner selects a license.
- Test full dependency installation and a live caption session on the RTX 4060 Ti machine.
- Consider Windows code signing to reduce SmartScreen warnings.

## 9. In-app update banner, relaunch and bug bundles

- `core/app_info.py` discovers the layout (`install_root()` = parent of `versions/`
  when `state.json` exists) and loads `launcher/updater.py` from the install root.
- `core/app_control.py` (`AppController`, owned by `AdminState`) runs the update
  check in a background thread with a one-hour in-process cache, installs
  releases with a progress hook (`updater._download(progress=...)`) and records
  relaunch requests. The admin page only reads `GET /api/update/status`; that
  call starts a non-forced check (answered by the 24-hour `update-check-cache.json`
  when possible) when none ran in the last hour (ten minutes after a failed one),
  so page loads and restart reloads do not spend GitHub's anonymous API quota
  (60 requests per hour). `POST /api/update/check` forces a check and is kept for
  an explicit user action or tools.
- Admin routes: `GET /api/app`, `GET /api/update/status`,
  `POST /api/update/check|install|skip`, `POST /api/app/restart`,
  `POST /api/support/bundle`, `POST /api/support/reveal`.
- Relaunch contract: `main.py` exits with `RELAUNCH_EXIT_CODE = 76`;
  `tools/run_supervised.py` restarts in place on a source checkout and returns
  76 on a managed install; `run_v2_light.bat` / `START_HERE.bat` propagate 76;
  `launcher/launcher.ps1` loops while the exit code is 76 and re-reads
  `state.json`, so an installed update becomes active without a console prompt.
- `tools/make_support_bundle.py --privacy` (`make_issue_bundle`) builds the
  transcript-free bundle used by the bug button; `tests/test_support_bundle_privacy.py`
  asserts that caption text never reaches the ZIP.

## 8. Portable (no-install) mode

The portable ZIP (`StreamerCaption-<version>-portable.zip`) contains the same
layout as an installation, minus the runtime:

```text
StreamerCaption/
|-- StreamerCaption.bat         one-button entry (bootstrap.ps1 -> launcher.ps1 -Direct)
|-- README-FIRST.txt
|-- runtime.json                {runtime_version, asset_name, sha256, size, url}
|-- state.json                  {current_version: <version>, portable: true}
|-- updater.json                copied from updater.example.json
|-- launcher/                   bootstrap.ps1, launcher.ps1, updater.py
|-- versions/<version>/         the app (same content as the release ZIP)
|-- runtime/                    filled on first run (.venv layout)
`-- user-data/                  created on first run
```

`launcher/bootstrap.ps1` (PowerShell, present on every Windows) is the only code
that runs before Python exists. It:

1. Reads `runtime.json`; if `runtime/.venv/RUNTIME_VERSION` differs, downloads the
   runtime ZIP (or uses one placed next to the .bat), verifies SHA-256, extracts
   to a staging folder and swaps it in.
2. On first run creates `user-data/` (`tools/bootstrap_user_data.py`), applies the
   auto GPU preset and writes `first-run.flag`.
3. Hands over to `launcher.ps1 -Direct`, which performs the normal update check,
   junctions, starts `tools/run_supervised.py`, and opens `/setup` on the first run.

Runtime ZIPs are built by `tools/build_runtime.py` (embedded CPython 3.12 placed in
a venv-shaped tree so every existing .bat keeps working) and published by the
`Build portable runtime` workflow as GitHub release `runtime-<runtime_version>`.
The app release manifest carries `runtime_version`, `runtime_asset_name`,
`runtime_sha256`, `runtime_size`, `runtime_release_tag` and `runtime_url`; the
release workflow fills them from the published `runtime-manifest.json`.
A runtime release is never replaced (installed apps pin its SHA-256) and never
marked Latest (the updater reads `/releases/latest`); new requirements mean a new
`runtime_version`. The runtime ships NVIDIA cuBLAS (`nvidia-cublas-cu12`) because
ctranslate2 loads `cublas64_12.dll` by name at the first GPU matmul; the baseline
engine puts its `bin` folder on `PATH`, so only the NVIDIA driver is required.
`updater.py update` calls `stage_runtime()` before activating a new app version:
on a portable install it downloads and SHA-256-verifies the runtime into
`downloads/` and rewrites `runtime.json`, but never swaps `runtime/.venv` itself
(the updater and the app run from it, and Windows cannot rename a directory with
mapped DLLs). On the relaunch, `launcher.ps1` sees `runtime.json` differ from
`runtime/.venv/RUNTIME_VERSION` and exits 77; `bootstrap.ps1` then runs
`Ensure-Runtime` with no Python alive and starts the launcher again. Venv installs
(no `runtime.json`) ignore the runtime fields. `install_archive` also copies the new
version's `launcher/` scripts (`bootstrap.ps1`, `launcher.ps1`, `updater.py`) over
`<root>/launcher/`, and `StreamerCaption.bat` over `<root>/StreamerCaption.bat` when
one is already there (portable layout; it is never put into `launcher/`, and the stray
copy 1.1.3 left there is removed), so launcher fixes reach existing installs; the app
loads `updater.py` from the running version first. `bootstrap.ps1` extracts the runtime
to `runtime/.staging-<8 hex>` (short, for MAX_PATH) and moves the old one to
`runtime/.old-<8 hex>` before deleting it.

`tools/build_runtime.py` pins its two bootstrap downloads by SHA-256: the embeddable
CPython ZIP and a pip wheel from PyPI (pip runs from the wheel, replacing the unpinned
`get-pip.py`); any mismatch fails the build. `--verify-downloads-only` fetches and
checks just those two files (and that pip runs from the wheel) without building.

## 10. Recaps through installed CLIs

When no Ollama or OpenAI-compatible endpoint answers, recaps can use the streamer's
own logged-in `claude` or `codex` CLI (`features/recap.py`, `CliProvider`). The transcript
is untrusted input (PC audio includes other people) and the answer goes on the stream
overlay, so the CLI runs as a plain text completion:

- the resolved path from `shutil.which` is executed (npm installs are `claude.cmd` /
  `codex.cmd`, which CreateProcess does not find by bare name);
- the prompt goes through stdin, never argv (visible to other processes, length-capped);
- the working directory is a fresh empty temp folder, never the app folder;
- Claude: `claude -p --output-format text --no-session-persistence --strict-mcp-config
  --tools ""` (no tools, no MCP servers, no saved session);
  Codex: `codex exec --skip-git-repo-check --ephemeral --sandbox read-only --color never
  --output-last-message <tmp>/last-message.txt -` (`-` reads stdin; exec never asks for
  approval; Codex cannot run without tools, so the read-only sandbox is its limit);
- a timeout (`archive.recap.timeout_sec`, 60 s) kills the whole process tree;
  any non-zero exit or timeout (including a CLI too old for these flags) falls back
  to the extractive recap;
- whatever any model returns is cut to at most `max_bullets` bullets of 120
  characters and a 60-character headline, with control and format characters removed.

Ollama prefers the smallest installed chat model of at most 8B parameters (preferred
families first), so a recap does not compete with the speech model for VRAM, and logs
the model it picked.
