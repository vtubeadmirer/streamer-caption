Streamer Caption third-party notice inventory

Status

This file is a release-readiness inventory, not the full license text bundle. Keep the repository private until the project license is chosen and every shipped or downloaded component has been verified. Installed Python wheels normally carry their own license files; the final installer must preserve them.

Direct runtime dependencies

- faster-whisper 1.2.1: MIT
- NumPy 2.5.1: BSD-3-Clause plus bundled-component licenses listed in its wheel metadata
- python-sounddevice 0.5.5: MIT
- PyAV 18.0.0: BSD-3-Clause; review the licenses of the FFmpeg libraries included by the selected wheel
- silero-vad 6.2.1: MIT
- websockets 16.1: BSD-3-Clause
- SoundCard 0.4.6: BSD-3-Clause

Optional SimulStreaming dependencies

- SimulStreaming vendored source: MIT; full text is in `experiments/vendor/SimulStreaming/LICENCE.txt`
- librosa: ISC; pin and verify the selected version before release
- tqdm: MPL-2.0 AND MIT; pin and verify the selected version before release
- tiktoken: MIT; pin and verify the selected version before release
- torch and torchaudio CUDA wheels: preserve the license and bundled-component notices from the exact installed wheels

Model files and external runtimes

- Whisper model weights are downloaded separately and are not stored in the release ZIP. Verify the model-card license for each configured model before public distribution.
- Optional FFmpeg installation is user-triggered and not bundled. Its build configuration determines whether LGPL or GPL terms apply.
- NVIDIA CUDA and cuDNN components have separate NVIDIA license terms and are not project-owned code.

Release action

Before the first public release, generate a complete notice bundle from the exact locked dependency set and include all required license texts in the installed application. Do not treat this inventory as legal advice.
