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
6. It extracts into a staging folder and validates `VERSION`.
7. It moves the complete folder into `versions` and updates `state.json` atomically.
8. On launch failure, the launcher offers rollback to `previous_version`.

GitHub release process

1. Change `VERSION` and `release-manifest.json` to the same version.
2. Run `tests/test_release_system.py`.
3. Run `BUILD_RELEASE.bat`.
4. Confirm the ZIP and `release-manifest.json` in `dist`.
5. Push a matching tag such as `v1.0.0`.
6. GitHub Actions rejects a tag that does not match `VERSION` and creates a draft Release.
7. Tags containing `-` are marked as prereleases, for example `v1.0.0-dev.1`.
8. Review release notes and publish the draft. Only publication makes it visible to normal update checks.

Repository configuration

The installer copies `updater.example.json` to the installed root as `updater.json`. It targets `vtubeadmirer/streamer-caption`. The repository must be public before unauthenticated users can check and download releases. Do not embed a personal access token in the app.

Public-build exclusions

The release builder rejects or excludes live configuration, slang dictionaries, protected and blocked terms, logs, reports, recordings, transcript databases, virtual environments, caches, downloads, state, and existing ZIP files.

Decisions still required before public launch

- Choose the project license. Do not publish as open source until the owner selects a license.
- Test full dependency installation and a live caption session on the RTX 4060 Ti machine.
- Consider Windows code signing to reduce SmartScreen warnings.
