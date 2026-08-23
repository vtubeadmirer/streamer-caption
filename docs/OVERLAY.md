# Overlay Usage

Open:

```text
<project-root>\overlay\caption.html
```

Common query parameters:

```text
caption.html?preview=0
caption.html?holdMs=8000
caption.html?pendingStaleMs=2000
caption.html?preview=0&holdMs=8000&pendingStaleMs=2000
```

- `preview=0`: transparent background for OBS/browser source.
- `holdMs`: how long confirmed captions remain visible.
- `pendingStaleMs`: how long pending captions remain if not confirmed.

The overlay ignores duplicate pending captions, pending captions older than the
latest pending offset, and pending captions older than the latest confirmed
caption.
