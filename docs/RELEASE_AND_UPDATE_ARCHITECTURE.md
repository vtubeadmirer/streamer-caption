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
6. GitHub Actions creates a draft Release.
7. Review release notes and publish the draft. Only publication makes it visible to normal update checks.

Repository configuration

Copy `updater.example.json` to the installed root as `updater.json`, then replace `GITHUB_USERNAME/streamer-caption` with the final public repository. A private repository requires authentication and must not embed a personal access token in the app.

Public-build exclusions

The release builder rejects or excludes live configuration, slang dictionaries, protected and blocked terms, logs, reports, recordings, transcript databases, virtual environments, caches, downloads, state, and existing ZIP files.

Decisions still required before public launch

- Choose the project license. Do not publish as open source until the owner selects a license.
- Replace the GitHub repository placeholder.
- Test full dependency installation and a live caption session on the RTX 4060 Ti machine.
- Consider Windows code signing to reduce SmartScreen warnings.
