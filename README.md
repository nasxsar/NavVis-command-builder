# NavVis Post-Processing Command Builder

Desktop GUI for building and running `navvis-postprocessing` commands.

## Project structure

- `main.py` - application entry point (PySide6 UI + Tkinter fallback)
- `arguments_schema.json` - schema-driven CLI argument definition
- `presets/` - built-in profiles
- `logs/` - timestamped run logs (created at runtime)

## Requirements

- Python 3.10+
- PySide6 (recommended)
  - Fallback mode uses Tkinter (included with standard Python on Ubuntu/WSL)

Install PySide6:

```bash
python3 -m pip install PySide6
```

## Run

```bash
python3 main.py
```

## Ubuntu notes

- Ensure `navvis-postprocessing` is on `PATH` or set the executable path in the app.

## WSL2 notes

- GUI works through WSLg.
- Windows paths like `C:\data\...` or `\\wsl$\...` are converted with `wslpath`.
- The app warns when datasets are on `/mnt/c` because I/O is slower than Linux filesystem paths.

## Features

- Schema-driven options UI grouped by:
  - Standard
  - SLAM
  - Point cloud generation
  - Point cloud coloring
  - Panorama rendering
  - Image enhancement
  - Force
  - Other
- Typed controls (checkboxes, dropdowns, numeric fields, multi-select cloud formats, path pickers)
- Live command preview with shell-safe quoting
- Only emits non-default options
- Dataset list management (folder add/remove/clear, wildcard support, optional subfolder expansion)
- Validation rules:
  - `--proc-base-dir` required
  - `--anchor-poses` and `--no-auto-transform` depend on `--use-anchors`
  - warns for invalid laser/time ranges
  - warns before `--force`
- Run in background with live logs, stop support, exit-code summary, timestamped log file
- Queue mode for multiple jobs
- Save/load JSON profiles + built-in presets
- Executable test (`navvis-postprocessing --help`)
- Dry-run mode

## Built-in presets

- Indoor standard
- Outdoor large area
- High-confidence clean
- Fast preview

## Extending `arguments_schema.json`

Each entry defines one CLI argument:

- `name`: internal key
- `flag`: CLI flag
- `type`: `boolean`, `choice`, `multi_choice`, `float`, `int`, `path_dir`, `path_file`, `dataset_list`
- `default`
- `choices` (for choice types)
- `help`
- `group`
- `dependencies` (list of argument `name` values)
- optional: `deprecated`, `min`, `max`, `step`

After schema edits, restart the app. The form is generated dynamically.

## Future improvements

- Add richer numeric widgets (spin boxes) in Tk fallback mode.
- Add persistent recent datasets and recent output folders.
- Add optional per-job executable overrides in queue mode.
- Add structured progress parsing when NavVis outputs progress markers.
