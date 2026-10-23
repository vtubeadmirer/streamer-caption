Streamer Caption third-party notice inventory

Status

This file is a release-readiness inventory, not the full license text bundle. The root project uses the MIT License. Keep the repository private until every shipped or downloaded component has been verified. Installed Python wheels normally carry their own license files; the final installer must preserve them.

Direct runtime dependencies

- faster-whisper 1.2.1: MIT
- NumPy 2.5.1: BSD-3-Clause plus bundled-component licenses listed in its wheel metadata
- python-sounddevice 0.5.5: MIT
- PyAV 18.0.0: BSD-3-Clause; review the licenses of the FFmpeg libraries included by the selected wheel
- silero-vad 6.2.1: MIT
- websockets 16.1: BSD-3-Clause
- SoundCard 0.4.6: BSD-3-Clause
- CTranslate2 4.8.3: MIT
- NVIDIA cuBLAS 12.8.4.1 (`nvidia-cublas-cu12`, Windows only; bundled in the portable runtime so GPU captions need no CUDA Toolkit): NVIDIA CUDA Toolkit EULA, which lists the cuBLAS DLLs as distributable (Attachment A). The text ships as `nvidia_cublas_cu12-12.8.4.1.dist-info/License.txt`. The runtime build folds each wheel's `*.dist-info/licenses/` tree into `licenses.txt` next to it instead of dropping it.

Optional SimulStreaming dependencies

- SimulStreaming vendored source: MIT; full text is in `experiments/vendor/SimulStreaming/LICENCE.txt`
- librosa 0.11.0: ISC
- tqdm 4.68.4: MPL-2.0 AND MIT
- tiktoken 0.12.0: MIT
- torch and torchaudio CUDA wheels: preserve the license and bundled-component notices from the exact installed wheels

Model files and external runtimes

- The default `Systran/faster-whisper-large-v3` model card declares MIT. The vendored SimulStreaming path downloads official OpenAI Whisper weights under MIT. Model files are downloaded separately and are not stored in the release ZIP. User-configured model repositories must be reviewed separately.
- The Silero VAD package and upstream `snakers4/silero-vad` repository declare MIT.
- Optional FFmpeg installation is user-triggered and not bundled. Its build configuration determines whether LGPL or GPL terms apply.
- NVIDIA CUDA and cuDNN components have separate NVIDIA license terms and are not project-owned code.

Release action

Before the first public release, generate a complete notice bundle from the exact locked dependency set and include all required license texts in the installed application. Do not treat this inventory as legal advice.
