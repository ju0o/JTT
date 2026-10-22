# Documentation media source

These files render **recorded application cells**, not a fabricated UI and not
a physical desktop/window recording. They are not imported by JTT and introduce
no runtime dependency. The product version and Release assets remain unchanged.

- `captures.json.gz`: accepted synthetic-data frames from three isolated takes.
- `capture-evidence.json`: source hashes and identity/cleanup verification summary.
- `timeline.json`: exact durations, bilingual captions and scene selection.
- `render.py`: Pillow rasterization and FFmpeg encoding, including continuous
  cell-aligned border glyphs, a labeled detail crop and editorial detach title.
- `verify.py`: media decode/duration, subtitle parity, actual narrow-panel
  identity, bounded privacy checks and checksum verification; no process capture.

Use a separate media venv with `Pillow==12.3.0` and `imageio-ffmpeg==0.6.0`.
Supply locally installed fonts; none are bundled. Production used DejaVu Sans
Mono and Malgun Gothic. Font substitution changes pixels and therefore hashes.

```sh
python render.py --mono-font /path/to/monospace.ttf --cjk-font /path/to/korean.ttf
python verify.py
```

This command only reads capture data and writes media; it does not run JTT,
shells, tmux, Agents or network clients. Rendering replaces the named output
files in the parent media directory, so use a separate checkout for experiments.
Never record real user state for replacement captures.

Native Windows Terminal QA remains pending. The documented isolated PTY tests
are not substitutes for native visual/input compatibility testing.
