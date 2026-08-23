# Transfer to RTX 4060 Ti PC

## What to Copy

Use one of these:

1. Copy the whole `streamer-caption-v2` folder.
2. Or run `make_transfer_zip.bat` and copy the generated
   `reports\streamer_caption_v2_transfer_4060ti_*.zip`.

Do not rely on copying `.venv` from this PC. Recreate it on the 4060 Ti PC.

## Required on the New PC

- Windows 10/11
- NVIDIA driver installed and working
- RTX 4060 Ti 16GB
- Python 3.11 or newer
- Internet access for the first dependency/model install
- A microphone device visible to Windows

## First Run on the New PC

Open PowerShell or double-click these files from the project folder:

```bat
setup_4060ti_pc.bat
START_HERE.bat
```

In `START_HERE.bat`, run:

1. `1. Health check`
2. `2. Check microphone devices`
3. `3. Probe input device levels`
4. `X. Import latest audio from samples\incoming` if you copied a test audio file
5. `L. Apply VAD tuning from test clip`
6. `H. Benchmark v2_light streaming test clip`
7. `M. Run v2_light 10 minute stability test`

## OBS Overlay

Use this browser source path:

```text
overlay/caption.html
```

The local WebSocket is:

```text
ws://127.0.0.1:8765
```

Admin page:

```text
http://127.0.0.1:8766/admin
```

## Slang and Protected Terms

Open the admin page and use `Slang / Protected Terms`.

Use pronunciation replacements when Whisper hears one word but you want another
caption:

```text
샌드위치=샹도웨쭈
```

Use protected terms when the defense filter hides a streamer-specific word:

```text
샹도웨쭈
```

After saving slang, restart `B. Run v2_light` so the Whisper initial prompt is
rebuilt with the latest dictionary.

For several possible misheard variants, add all variants to the same display
word:

```text
샌드위치=샹도웨쭈
산도웨쭈=샹도웨쭈
샨도웨이쭈=샹도웨쭈
```

If common words such as `안녕하세요` or `감사합니다` are hidden, add them to
protected terms.

Caption size changes apply to `http://127.0.0.1:8766/caption` automatically
within about one second. Use `Caption > 한 줄 글자 수` to shorten long captions.
Use `Caption > 저스트 채팅 모드` with `최대 줄 수` between 6 and 12 to keep a
rolling on-screen caption history.

## Current Recommended 4060 Ti Settings

The current `config.json` is already tuned for 4060 Ti 16GB:

- profile: `rtx_4060ti_16gb`
- model: `large-v3`
- device: `cuda`
- compute type: `int8_float16`
- VAD: `silero`

If settings get changed, run:

```bat
apply_4060ti_profile.bat
apply_recommended_4060ti.bat
```

## Updating Without Losing Settings

After the first install, prefer update ZIPs:

```bat
make_update_zip.bat
```

The update ZIP keeps these user files on the target PC:

- `config.json`
- `data\slang_dict.json`
- `data\whitelist.txt`
- `data\blocklist.txt`

Before a risky update on the target PC:

```bat
backup_user_settings.bat
```

If settings are accidentally overwritten:

```bat
restore_user_settings.bat
```

## Sending Logs Back

Logs are useful. Run:

```bat
make_support_bundle.bat
```

Send the generated `reports\streamer_caption_v2_support_*.zip`. It contains
the latest logs, reports, config, slang dictionary, and protected terms. This is
better than sending the whole project folder.
