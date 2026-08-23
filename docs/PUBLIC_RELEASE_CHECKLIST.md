Streamer Caption public release checklist

Repository decision

- Use `vtubeadmirer/streamer-caption` for both source and GitHub Releases.
- The owner selected the MIT License for the root project.
- Keep the repository private until the remaining history and dependency audits pass.
- Do not publish a personal access token with the updater. Public release checks are unauthenticated.

Before changing repository visibility

- Confirm the root `LICENSE` file contains the approved MIT License text.
- Review all direct and vendored dependency licenses and add third-party notices where required.
- Pin the currently unpinned SimulStreaming dependencies before shipping that optional runtime.
- Review the licenses for downloaded Whisper model weights separately from the Python packages.
- Confirm that no live `config.json`, slang dictionary, protected terms, recordings, logs, reports, database, model cache, runtime, or virtual environment is tracked.
- Search for absolute user paths, email addresses, access tokens, private keys, and credentials.
- Review historical commits as well as the current tree before making the repository public.
- Enable GitHub secret scanning and Dependabot where available.

Prerelease validation

- Set `VERSION` and `release-manifest.json` to the same prerelease version.
- Use a matching tag such as `v1.0.0-dev.1`.
- Confirm GitHub Actions passes both release-system and updater-cache tests.
- Keep the generated release as Draft until its ZIP and manifest have been reviewed.
- Mark development tags as Pre-release.
- Verify the manifest SHA-256 matches the uploaded ZIP.
- Publish the prerelease only after repository visibility and license decisions are complete.

RTX 4060 Ti test

- Install on a clean Windows account using `INSTALL.bat`.
- Confirm automatic GPU profile selection.
- Confirm microphone capture, PC playback capture, admin UI, caption overlay, and text export.
- Run a 10-minute fast-speech stability session.
- Confirm a published prerelease is detected only when `allow_prerelease` is enabled.
- Update while preserving configuration, slang, protected terms, logs, recordings, and transcript database.
- Roll back and confirm the previous version still launches.

Stable release

- Change the version to `1.0.0` and disable prerelease updates by default.
- Build from a clean commit and push the matching `v1.0.0` tag.
- Review and publish the Draft Release.
- Install the public ZIP on a second machine and verify the first-run workflow.
- Confirm the normal launcher detects future stable versions no more than once per configured interval.

Deferred hardening

- Sign the Windows installer or launcher to reduce SmartScreen warnings.
- Add a signed update manifest if release authenticity must survive a repository-account compromise.
- Add conditional API requests if the user base grows beyond simple daily polling.
