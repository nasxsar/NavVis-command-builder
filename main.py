from __future__ import annotations

import datetime as dt
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

APP_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = APP_DIR / "arguments_schema.json"
PRESETS_DIR = APP_DIR / "presets"
LOGS_DIR = APP_DIR / "logs"
CONFIG_PATH = Path.home() / ".config" / "navvis-command-builder" / "config.json"

GROUP_ORDER = [
    "Standard",
    "SLAM",
    "Point cloud generation",
    "Point cloud coloring",
    "Panorama rendering",
    "Image enhancement",
    "Force",
    "Other",
]


def is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def shell_join(parts: list[str]) -> str:
    return " ".join(shlex.quote(p) for p in parts)


def windows_path_like(raw: str) -> bool:
    return bool(re.match(r"^[a-zA-Z]:\\", raw)) or raw.startswith("\\\\wsl$")


def convert_windows_path_if_needed(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    if not is_wsl() or not windows_path_like(raw):
        return raw
    try:
        result = subprocess.run(["wslpath", "-u", raw], capture_output=True, text=True, check=True)
        return result.stdout.strip() or raw
    except (subprocess.SubprocessError, FileNotFoundError):
        return raw


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


@dataclass
class BuildResult:
    command: list[str]
    errors: list[str]
    warnings: list[str]
    datasets: list[str]


class CommandBuilder:
    def __init__(self, schema: list[dict[str, Any]]) -> None:
        self.schema = schema
        self.by_name = {entry["name"]: entry for entry in schema}

    def _depends_ok(self, name: str, values: dict[str, Any]) -> bool:
        deps = self.by_name[name].get("dependencies", [])
        return all(bool(values.get(dep)) for dep in deps)

    def _expand_datasets(self, entries: list[str], subfolder_root: str, include_subfolders: bool) -> tuple[list[str], list[str], list[str]]:
        errors: list[str] = []
        warnings: list[str] = []
        expanded: list[str] = []

        candidates: list[str] = [e.strip() for e in entries if e.strip()]
        if include_subfolders and subfolder_root.strip():
            root = Path(convert_windows_path_if_needed(subfolder_root.strip())).expanduser()
            if not root.exists() or not root.is_dir():
                errors.append(f"Subfolder root does not exist: {root}")
            else:
                candidates.extend(str(p) for p in sorted(root.iterdir()) if p.is_dir())

        for item in candidates:
            converted = convert_windows_path_if_needed(item)
            found = glob.glob(converted)
            if found:
                expanded.extend(found)
            else:
                expanded.append(converted)

        normalized: list[str] = []
        seen: set[str] = set()
        for item in expanded:
            path = Path(item).expanduser()
            if not path.exists() or not path.is_dir():
                errors.append(f"Dataset folder does not exist: {path}")
                continue
            as_posix = str(path)
            if as_posix not in seen:
                if is_wsl() and as_posix.startswith("/mnt/c/"):
                    warnings.append("Dataset on /mnt/c detected. Linux filesystem is usually faster in WSL2.")
                seen.add(as_posix)
                normalized.append(as_posix)

        return normalized, errors, warnings

    def _coerce_number(self, value: Any) -> Any:
        if isinstance(value, (int, float)):
            return value
        if value in ("", None):
            return ""
        text = str(value).strip()
        if not text:
            return ""
        try:
            if "." in text:
                return float(text)
            return int(text)
        except ValueError:
            return value

    def build(
        self,
        executable: str,
        values: dict[str, Any],
        dataset_entries: list[str],
        include_subfolders: bool,
        subfolder_root: str,
        force_no_color: bool = False,
    ) -> BuildResult:
        errors: list[str] = []
        warnings: list[str] = []
        command: list[str] = [executable.strip() or "navvis-postprocessing"]

        datasets, dataset_errors, dataset_warnings = self._expand_datasets(dataset_entries, subfolder_root, include_subfolders)
        errors.extend(dataset_errors)
        warnings.extend(dataset_warnings)

        proc_base_dir = convert_windows_path_if_needed(str(values.get("proc-base-dir", "")).strip())
        if not proc_base_dir:
            errors.append("--proc-base-dir is required.")

        min_range = self._coerce_number(values.get("min-laser-range", ""))
        max_range = self._coerce_number(values.get("max-laser-range", ""))
        if min_range not in ("", None) and max_range not in ("", None):
            try:
                if float(min_range) >= float(max_range):
                    warnings.append("min-laser-range should be smaller than max-laser-range.")
            except (TypeError, ValueError):
                pass

        start_time = self._coerce_number(values.get("start-time", ""))
        stop_time = self._coerce_number(values.get("stop-time", ""))
        if start_time not in ("", None) and stop_time not in ("", None):
            try:
                if float(start_time) >= float(stop_time):
                    warnings.append("start-time should be smaller than stop-time.")
            except (TypeError, ValueError):
                pass

        for entry in self.schema:
            name = entry["name"]
            if entry.get("deprecated"):
                continue
            if name == "input":
                continue

            value = values.get(name, entry.get("default"))
            if not self._depends_ok(name, values):
                continue

            if entry["type"] == "boolean":
                default = bool(entry.get("default", False))
                current = bool(value)
                if current != default and current:
                    command.append(entry["flag"])
                continue

            if value in (None, ""):
                continue

            default = entry.get("default")
            if entry["type"] == "multi_choice":
                current_list = list(value)
                if sorted(current_list) == sorted(default or []):
                    continue
                command.append(entry["flag"])
                command.extend(current_list)
                continue

            if entry["type"] in {"path_file", "path_dir"}:
                value = convert_windows_path_if_needed(str(value))

            if value == default:
                continue

            command.extend([entry["flag"], str(value)])

        if force_no_color and "--no-color" not in command:
            command.append("--no-color")

        if not datasets:
            errors.append("At least one valid dataset folder is required.")
        command.extend(datasets)

        return BuildResult(command=command, errors=errors, warnings=warnings, datasets=datasets)


def load_schema() -> list[dict[str, Any]]:
    raw = load_json(SCHEMA_PATH, [])
    if not isinstance(raw, list):
        raise ValueError("Schema file must contain a list")
    return raw


class ProcessRunner:
    def __init__(self, on_line: Callable[[str], None], on_done: Callable[[int, Path], None]) -> None:
        self.on_line = on_line
        self.on_done = on_done
        self.proc: subprocess.Popen[str] | None = None
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, command: list[str], dry_run: bool) -> None:
        if self._thread and self._thread.is_alive():
            return

        def _run() -> None:
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            log_path = LOGS_DIR / f"run_{now_stamp()}.log"
            with log_path.open("w", encoding="utf-8") as log_file:
                log_file.write(shell_join(command) + "\n\n")
                if dry_run:
                    msg = "[DRY RUN] Command was not executed."
                    self.on_line(msg)
                    log_file.write(msg + "\n")
                    self.on_done(0, log_path)
                    return

                try:
                    self.proc = subprocess.Popen(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                    )
                except OSError as exc:
                    msg = f"Failed to start process: {exc}"
                    self.on_line(msg)
                    log_file.write(msg + "\n")
                    self.on_done(1, log_path)
                    return

                assert self.proc.stdout is not None
                for line in self.proc.stdout:
                    text = line.rstrip("\n")
                    self.on_line(text)
                    log_file.write(text + "\n")
                    if self._stop_requested.is_set():
                        break

                if self._stop_requested.is_set() and self.proc.poll() is None:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()

                code = self.proc.wait() if self.proc.poll() is None else self.proc.returncode
                self.on_done(code or 0, log_path)

        self._stop_requested.clear()
        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_requested.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


try:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QDoubleSpinBox,
        QFileDialog,
        QFormLayout,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QInputDialog,
        QLabel,
        QLineEdit,
        QListWidget,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QPlainTextEdit,
        QProgressBar,
        QSpinBox,
        QTabWidget,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )

    PYSIDE_AVAILABLE = True
except ImportError:
    PYSIDE_AVAILABLE = False


if PYSIDE_AVAILABLE:
    class MainWindow(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("NavVis Post-Processing Command Builder")
            self.resize(1300, 900)

            self.schema = load_schema()
            self.builder = CommandBuilder(self.schema)
            self.widgets: dict[str, Any] = {}
            self.dependency_widgets: dict[str, list[QWidget]] = {}
            self.queue: list[dict[str, Any]] = []
            self.running_queue = False
            self.runner = ProcessRunner(self._append_log, self._on_run_done)

            self.config = load_json(CONFIG_PATH, {})
            self._building = False

            self._setup_ui()
            self._apply_config()
            self._refresh_dependency_states()
            self.update_preview()

        def _setup_ui(self) -> None:
            root = QWidget()
            main = QVBoxLayout(root)

            exe_group = QGroupBox("Executable")
            exe_layout = QHBoxLayout(exe_group)
            self.exe_input = QLineEdit()
            self.exe_input.setPlaceholderText("navvis-postprocessing")
            self.exe_input.textChanged.connect(self._save_config)
            exe_layout.addWidget(self.exe_input)
            browse_exe = QPushButton("Browse")
            browse_exe.clicked.connect(self._browse_executable)
            exe_layout.addWidget(browse_exe)
            test_exe = QPushButton("Test")
            test_exe.clicked.connect(self._test_executable)
            exe_layout.addWidget(test_exe)
            main.addWidget(exe_group)

            dataset_group = QGroupBox("Datasets")
            dataset_layout = QVBoxLayout(dataset_group)
            self.dataset_list = QListWidget()
            self.dataset_list.itemSelectionChanged.connect(self.update_preview)
            dataset_layout.addWidget(self.dataset_list)

            ds_buttons = QHBoxLayout()
            add_btn = QPushButton("Add folder")
            add_btn.clicked.connect(self._add_dataset_folder)
            ds_buttons.addWidget(add_btn)
            add_wc_btn = QPushButton("Add wildcard")
            add_wc_btn.clicked.connect(self._add_dataset_wildcard)
            ds_buttons.addWidget(add_wc_btn)
            remove_btn = QPushButton("Remove selected")
            remove_btn.clicked.connect(self._remove_selected_dataset)
            ds_buttons.addWidget(remove_btn)
            clear_btn = QPushButton("Clear")
            clear_btn.clicked.connect(self._clear_datasets)
            ds_buttons.addWidget(clear_btn)
            dataset_layout.addLayout(ds_buttons)

            subfolders_layout = QHBoxLayout()
            self.subfolder_checkbox = QCheckBox("Select all subfolders in datasets_rec folder")
            self.subfolder_checkbox.stateChanged.connect(self.update_preview)
            subfolders_layout.addWidget(self.subfolder_checkbox)
            self.subfolder_root = QLineEdit()
            self.subfolder_root.setPlaceholderText("/path/to/datasets_rec")
            self.subfolder_root.textChanged.connect(self.update_preview)
            subfolders_layout.addWidget(self.subfolder_root)
            subfolder_browse = QPushButton("Browse")
            subfolder_browse.clicked.connect(self._browse_subfolder_root)
            subfolders_layout.addWidget(subfolder_browse)
            dataset_layout.addLayout(subfolders_layout)
            main.addWidget(dataset_group)

            options_tabs = QTabWidget()
            grouped: dict[str, list[dict[str, Any]]] = {group: [] for group in GROUP_ORDER}
            for entry in self.schema:
                if entry.get("deprecated"):
                    continue
                if entry["name"] == "input":
                    continue
                grouped.setdefault(entry["group"], []).append(entry)

            for group in GROUP_ORDER:
                entries = grouped.get(group, [])
                tab = QWidget()
                form = QFormLayout(tab)
                for entry in entries:
                    row_widget = self._build_widget(entry)
                    form.addRow(entry["flag"], row_widget)
                options_tabs.addTab(tab, group)

            main.addWidget(options_tabs)

            preview_group = QGroupBox("Command preview")
            preview_layout = QVBoxLayout(preview_group)
            self.preview = QTextEdit()
            self.preview.setReadOnly(True)
            preview_layout.addWidget(self.preview)
            preview_buttons = QHBoxLayout()
            copy_btn = QPushButton("Copy command")
            copy_btn.clicked.connect(self._copy_command)
            preview_buttons.addWidget(copy_btn)
            export_btn = QPushButton("Export as .sh script")
            export_btn.clicked.connect(self._export_script)
            preview_buttons.addWidget(export_btn)
            preview_layout.addLayout(preview_buttons)
            main.addWidget(preview_group)

            status_group = QGroupBox("Run")
            status_layout = QVBoxLayout(status_group)
            top_row = QHBoxLayout()
            self.dry_run_checkbox = QCheckBox("Dry run (build command only)")
            self.dry_run_checkbox.stateChanged.connect(self.update_preview)
            top_row.addWidget(self.dry_run_checkbox)
            start_btn = QPushButton("Start")
            start_btn.clicked.connect(self.start_run)
            top_row.addWidget(start_btn)
            stop_btn = QPushButton("Stop")
            stop_btn.clicked.connect(self.stop_run)
            top_row.addWidget(stop_btn)
            add_queue_btn = QPushButton("Add current settings to queue")
            add_queue_btn.clicked.connect(self._queue_add)
            top_row.addWidget(add_queue_btn)
            run_queue_btn = QPushButton("Run queue")
            run_queue_btn.clicked.connect(self._queue_run)
            top_row.addWidget(run_queue_btn)
            clear_queue_btn = QPushButton("Clear queue")
            clear_queue_btn.clicked.connect(self._queue_clear)
            top_row.addWidget(clear_queue_btn)
            status_layout.addLayout(top_row)

            self.queue_view = QListWidget()
            status_layout.addWidget(self.queue_view)

            self.status_text = QLabel("Idle")
            status_layout.addWidget(self.status_text)
            self.progress = QProgressBar()
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
            status_layout.addWidget(self.progress)

            self.log = QPlainTextEdit()
            self.log.setReadOnly(True)
            status_layout.addWidget(self.log)
            main.addWidget(status_group)

            profile_group = QGroupBox("Profiles")
            profile_layout = QHBoxLayout(profile_group)
            save_profile_btn = QPushButton("Save profile")
            save_profile_btn.clicked.connect(self._save_profile)
            profile_layout.addWidget(save_profile_btn)
            load_profile_btn = QPushButton("Load profile")
            load_profile_btn.clicked.connect(self._load_profile)
            profile_layout.addWidget(load_profile_btn)
            load_preset_btn = QPushButton("Load built-in preset")
            load_preset_btn.clicked.connect(self._load_builtin_preset)
            profile_layout.addWidget(load_preset_btn)
            main.addWidget(profile_group)

            self.warning_label = QLabel("")
            self.warning_label.setStyleSheet("color: #d08700;")
            self.warning_label.setWordWrap(True)
            main.addWidget(self.warning_label)

            self.setCentralWidget(root)

        def _build_widget(self, entry: dict[str, Any]) -> QWidget:
            widget: QWidget
            name = entry["name"]
            default = entry.get("default")
            help_text = entry.get("help", "")

            if entry["type"] == "boolean":
                checkbox = QCheckBox()
                checkbox.setChecked(bool(default))
                checkbox.setToolTip(help_text)
                checkbox.stateChanged.connect(self._on_option_changed)
                self.widgets[name] = checkbox
                widget = checkbox
            elif entry["type"] == "choice":
                combo = QComboBox()
                for choice in entry.get("choices", []):
                    combo.addItem(str(choice))
                if default not in (None, ""):
                    ix = combo.findText(str(default))
                    if ix >= 0:
                        combo.setCurrentIndex(ix)
                combo.currentIndexChanged.connect(self._on_option_changed)
                combo.setToolTip(help_text)
                self.widgets[name] = combo
                widget = combo
            elif entry["type"] == "multi_choice":
                holder = QWidget()
                grid = QGridLayout(holder)
                checks: dict[str, QCheckBox] = {}
                defaults = set(default if isinstance(default, list) else [])
                for i, choice in enumerate(entry.get("choices", [])):
                    chk = QCheckBox(str(choice))
                    chk.setChecked(str(choice) in defaults)
                    chk.stateChanged.connect(self._on_option_changed)
                    chk.setToolTip(help_text)
                    checks[str(choice)] = chk
                    grid.addWidget(chk, i // 4, i % 4)
                self.widgets[name] = checks
                widget = holder
            elif entry["type"] in {"int", "float"}:
                holder = QWidget()
                row = QHBoxLayout(holder)
                if entry["type"] == "int":
                    spin: QSpinBox | QDoubleSpinBox = QSpinBox()
                    spin.setSingleStep(1)
                else:
                    spin = QDoubleSpinBox()
                    spin.setDecimals(6)
                    spin.setSingleStep(float(entry.get("step", 0.1)))
                min_value = float(entry.get("min", -1000000))
                max_value = float(entry.get("max", 1000000))
                if isinstance(spin, QSpinBox):
                    spin.setRange(int(min_value), int(max_value))
                else:
                    spin.setRange(min_value, max_value)

                optional = default in ("", None)
                if optional:
                    set_value_chk = QCheckBox("set")
                    set_value_chk.setChecked(False)
                    row.addWidget(set_value_chk)
                    spin.setEnabled(False)

                    def _toggle_numeric(state: int, widget_spin: QSpinBox | QDoubleSpinBox = spin) -> None:
                        widget_spin.setEnabled(bool(state))
                        self._on_option_changed()

                    set_value_chk.stateChanged.connect(_toggle_numeric)
                    self.widgets[name] = {"_kind": "numeric_optional", "check": set_value_chk, "spin": spin}
                else:
                    if default not in ("", None):
                        spin.setValue(float(default))
                    self.widgets[name] = {"_kind": "numeric", "spin": spin}

                spin.valueChanged.connect(self._on_option_changed)
                spin.setToolTip(help_text)
                row.addWidget(spin)
                widget = holder
            elif entry["type"] in {"path_file", "path_dir"}:
                holder = QWidget()
                row = QHBoxLayout(holder)
                line = QLineEdit(str(default or ""))
                line.textChanged.connect(self._on_option_changed)
                line.setToolTip(help_text)
                row.addWidget(line)
                browse = QPushButton("Browse")

                def _browse() -> None:
                    if entry["type"] == "path_dir":
                        path = QFileDialog.getExistingDirectory(self, "Select directory")
                    else:
                        file_path, _ = QFileDialog.getOpenFileName(self, "Select file")
                        path = file_path
                    if path:
                        line.setText(path)

                browse.clicked.connect(_browse)
                row.addWidget(browse)
                self.widgets[name] = line
                widget = holder
            else:
                line = QLineEdit(str(default if default not in (None, "") else ""))
                line.textChanged.connect(self._on_option_changed)
                line.setToolTip(help_text)
                self.widgets[name] = line
                widget = line

            for dep in entry.get("dependencies", []):
                self.dependency_widgets.setdefault(dep, []).append(widget)

            return widget

        def _on_option_changed(self) -> None:
            self._refresh_dependency_states()
            self.update_preview()

        def _refresh_dependency_states(self) -> None:
            use_anchors = bool(self._value_for("use-anchors"))
            for widget in self.dependency_widgets.get("use-anchors", []):
                widget.setEnabled(use_anchors)

        def _value_for(self, name: str) -> Any:
            w = self.widgets[name]
            if isinstance(w, QCheckBox):
                return w.isChecked()
            if isinstance(w, QComboBox):
                return w.currentText()
            if isinstance(w, QLineEdit):
                return w.text().strip()
            if isinstance(w, dict):
                if w.get("_kind") == "numeric_optional":
                    if not w["check"].isChecked():
                        return ""
                    spin = w["spin"]
                    return int(spin.value()) if isinstance(spin, QSpinBox) else float(spin.value())
                if w.get("_kind") == "numeric":
                    spin = w["spin"]
                    return int(spin.value()) if isinstance(spin, QSpinBox) else float(spin.value())
                return [key for key, checkbox in w.items() if checkbox.isChecked()]
            return ""

        def _collect_values(self) -> dict[str, Any]:
            values: dict[str, Any] = {}
            for name in self.widgets:
                values[name] = self._value_for(name)
            return values

        def _collect_datasets(self) -> list[str]:
            return [self.dataset_list.item(i).text() for i in range(self.dataset_list.count())]

        def _current_executable(self) -> str:
            return self.exe_input.text().strip() or "navvis-postprocessing"

        def _save_config(self) -> None:
            save_json(CONFIG_PATH, {"executable": self.exe_input.text().strip()})

        def _apply_config(self) -> None:
            configured = str(self.config.get("executable", "")).strip()
            detected = shutil.which("navvis-postprocessing") or ""
            self.exe_input.setText(configured or detected or "navvis-postprocessing")

        def _append_log(self, line: str) -> None:
            def _append() -> None:
                self.log.appendPlainText(line)

            QTimer.singleShot(0, _append)

        def _on_run_done(self, code: int, log_path: Path) -> None:
            def _finish() -> None:
                self.progress.setRange(0, 1)
                self.progress.setValue(0)
                text = f"Finished with exit code {code}. Log: {log_path}"
                self.status_text.setText(text)
                self.log.appendPlainText(text)
                if self.running_queue:
                    self._queue_run_next()

            QTimer.singleShot(0, _finish)

        def update_preview(self) -> None:
            values = self._collect_values()
            result = self.builder.build(
                executable=self._current_executable(),
                values=values,
                dataset_entries=self._collect_datasets(),
                include_subfolders=self.subfolder_checkbox.isChecked(),
                subfolder_root=self.subfolder_root.text().strip(),
                force_no_color=False,
            )

            preview_text = shell_join(result.command)
            if result.errors:
                preview_text += "\n\nErrors:\n- " + "\n- ".join(result.errors)
            if result.warnings:
                preview_text += "\n\nWarnings:\n- " + "\n- ".join(sorted(set(result.warnings)))
            self.preview.setPlainText(preview_text)
            self.warning_label.setText("\n".join(sorted(set(result.warnings))))

        def _browse_executable(self) -> None:
            path, _ = QFileDialog.getOpenFileName(self, "Select navvis-postprocessing executable")
            if path:
                self.exe_input.setText(path)
                self._save_config()

        def _test_executable(self) -> None:
            exe = self._current_executable()
            try:
                result = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=20)
            except (OSError, subprocess.SubprocessError) as exc:
                QMessageBox.critical(self, "Executable test failed", str(exc))
                return
            if result.returncode == 0:
                QMessageBox.information(self, "Executable test", "Executable test succeeded.")
            else:
                QMessageBox.warning(self, "Executable test", result.stderr or result.stdout or "Unknown failure")

        def _add_dataset_folder(self) -> None:
            path = QFileDialog.getExistingDirectory(self, "Select dataset folder")
            if path:
                self.dataset_list.addItem(path)
                self.update_preview()

        def _add_dataset_wildcard(self) -> None:
            text, ok = QInputDialog.getText(self, "Add wildcard", "Wildcard pattern:")
            if ok and text.strip():
                self.dataset_list.addItem(text.strip())
                self.update_preview()

        def _remove_selected_dataset(self) -> None:
            for item in self.dataset_list.selectedItems():
                self.dataset_list.takeItem(self.dataset_list.row(item))
            self.update_preview()

        def _clear_datasets(self) -> None:
            self.dataset_list.clear()
            self.update_preview()

        def _browse_subfolder_root(self) -> None:
            path = QFileDialog.getExistingDirectory(self, "Select datasets_rec directory")
            if path:
                self.subfolder_root.setText(path)
                self.update_preview()

        def _copy_command(self) -> None:
            QApplication.clipboard().setText(self.preview.toPlainText().splitlines()[0] if self.preview.toPlainText() else "")

        def _export_script(self) -> None:
            values = self._collect_values()
            result = self.builder.build(
                executable=self._current_executable(),
                values=values,
                dataset_entries=self._collect_datasets(),
                include_subfolders=self.subfolder_checkbox.isChecked(),
                subfolder_root=self.subfolder_root.text().strip(),
                force_no_color=False,
            )
            if result.errors:
                QMessageBox.warning(self, "Cannot export", "\n".join(result.errors))
                return
            path, _ = QFileDialog.getSaveFileName(self, "Export shell script", str(APP_DIR / "run_navvis.sh"), "Shell script (*.sh)")
            if not path:
                return
            script = "#!/usr/bin/env bash\nset -euo pipefail\n" + shell_join(result.command) + "\n"
            Path(path).write_text(script, encoding="utf-8")
            os.chmod(path, 0o755)

        def start_run(self) -> None:
            values = self._collect_values()
            result = self.builder.build(
                executable=self._current_executable(),
                values=values,
                dataset_entries=self._collect_datasets(),
                include_subfolders=self.subfolder_checkbox.isChecked(),
                subfolder_root=self.subfolder_root.text().strip(),
                force_no_color=True,
            )

            if result.errors:
                QMessageBox.warning(self, "Validation errors", "\n".join(result.errors))
                return
            if values.get("force"):
                answer = QMessageBox.warning(
                    self,
                    "Force mode",
                    "--force will overwrite existing output. Continue?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return

            self.log.clear()
            self.log.appendPlainText(shell_join(result.command))
            self.status_text.setText("Running...")
            self.progress.setRange(0, 0)
            self.runner.start(result.command, self.dry_run_checkbox.isChecked())

        def stop_run(self) -> None:
            self.runner.stop()
            self.status_text.setText("Stopping...")

        def _queue_snapshot(self) -> dict[str, Any]:
            return {
                "values": self._collect_values(),
                "datasets": self._collect_datasets(),
                "include_subfolders": self.subfolder_checkbox.isChecked(),
                "subfolder_root": self.subfolder_root.text().strip(),
            }

        def _queue_add(self) -> None:
            snap = self._queue_snapshot()
            self.queue.append(snap)
            self.queue_view.addItem(f"Job {len(self.queue)} ({len(snap['datasets'])} datasets)")

        def _queue_clear(self) -> None:
            self.queue.clear()
            self.queue_view.clear()
            self.running_queue = False

        def _queue_run(self) -> None:
            if not self.queue:
                QMessageBox.information(self, "Queue", "Queue is empty.")
                return
            if self.running_queue:
                return
            self.running_queue = True
            self._queue_run_next()

        def _queue_run_next(self) -> None:
            if not self.queue:
                self.running_queue = False
                self.status_text.setText("Queue finished")
                return
            job = self.queue.pop(0)
            if self.queue_view.count() > 0:
                self.queue_view.takeItem(0)

            result = self.builder.build(
                executable=self._current_executable(),
                values=job["values"],
                dataset_entries=job["datasets"],
                include_subfolders=job["include_subfolders"],
                subfolder_root=job["subfolder_root"],
                force_no_color=True,
            )
            if result.errors:
                self.log.appendPlainText("Queue job skipped due to validation errors: " + "; ".join(result.errors))
                self._queue_run_next()
                return

            self.log.clear()
            self.log.appendPlainText(shell_join(result.command))
            self.status_text.setText("Running queued job...")
            self.progress.setRange(0, 0)
            self.runner.start(result.command, self.dry_run_checkbox.isChecked())

        def _save_profile(self) -> None:
            payload = self._queue_snapshot()
            path, _ = QFileDialog.getSaveFileName(self, "Save profile", str(APP_DIR / "profile.json"), "JSON (*.json)")
            if not path:
                return
            save_json(Path(path), payload)

        def _load_profile(self) -> None:
            path, _ = QFileDialog.getOpenFileName(self, "Load profile", str(APP_DIR), "JSON (*.json)")
            if not path:
                return
            payload = load_json(Path(path), {})
            self._apply_profile(payload)

        def _load_builtin_preset(self) -> None:
            options = sorted(PRESETS_DIR.glob("*.json"))
            if not options:
                QMessageBox.information(self, "Presets", "No presets found.")
                return
            names = [p.stem for p in options]
            name, ok = QInputDialog.getItem(self, "Load preset", "Preset:", names, 0, False)
            if not ok:
                return
            payload = load_json(PRESETS_DIR / f"{name}.json", {})
            self._apply_profile(payload)

        def _apply_profile(self, payload: dict[str, Any]) -> None:
            settings = payload.get("values") or payload.get("settings") or {}
            datasets = payload.get("datasets") or []
            for name, value in settings.items():
                if name not in self.widgets:
                    continue
                w = self.widgets[name]
                if isinstance(w, QCheckBox):
                    w.setChecked(bool(value))
                elif isinstance(w, QComboBox):
                    idx = w.findText(str(value))
                    if idx >= 0:
                        w.setCurrentIndex(idx)
                elif isinstance(w, QLineEdit):
                    w.setText(str(value))
                elif isinstance(w, dict):
                    if w.get("_kind") == "numeric_optional":
                        if value in (None, ""):
                            w["check"].setChecked(False)
                        else:
                            w["check"].setChecked(True)
                            w["spin"].setValue(float(value))
                    elif w.get("_kind") == "numeric":
                        if value not in (None, ""):
                            w["spin"].setValue(float(value))
                    else:
                        selected = set(value if isinstance(value, list) else [])
                        for key, chk in w.items():
                            chk.setChecked(key in selected)

            self.dataset_list.clear()
            for ds in datasets:
                self.dataset_list.addItem(str(ds))
            self.subfolder_checkbox.setChecked(bool(payload.get("include_subfolders", False)))
            self.subfolder_root.setText(str(payload.get("subfolder_root", "")))
            self.update_preview()


def run_tk_fallback() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    schema = load_schema()
    builder = CommandBuilder(schema)

    root = tk.Tk()
    root.title("NavVis Post-Processing Command Builder (Tk fallback)")
    root.geometry("1100x800")

    vars_map: dict[str, Any] = {}

    top = ttk.Frame(root)
    top.pack(fill="x", padx=8, pady=8)
    ttk.Label(top, text="Executable:").pack(side="left")
    exe_var = tk.StringVar(value=shutil.which("navvis-postprocessing") or "navvis-postprocessing")
    ttk.Entry(top, textvariable=exe_var, width=80).pack(side="left", padx=8)

    datasets_frame = ttk.LabelFrame(root, text="Datasets")
    datasets_frame.pack(fill="x", padx=8, pady=8)
    datasets_box = tk.Listbox(datasets_frame, height=4)
    datasets_box.pack(fill="x", padx=4, pady=4)

    def add_folder() -> None:
        path = filedialog.askdirectory()
        if path:
            datasets_box.insert("end", path)
            update_preview()

    btns = ttk.Frame(datasets_frame)
    btns.pack(fill="x")
    ttk.Button(btns, text="Add folder", command=add_folder).pack(side="left")

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True, padx=8, pady=8)

    groups: dict[str, list[dict[str, Any]]] = {group: [] for group in GROUP_ORDER}
    for entry in schema:
        if entry.get("deprecated") or entry["name"] == "input":
            continue
        groups.setdefault(entry["group"], []).append(entry)

    for group_name in GROUP_ORDER:
        frame = ttk.Frame(notebook)
        notebook.add(frame, text=group_name)
        for entry in groups.get(group_name, []):
            row = ttk.Frame(frame)
            row.pack(fill="x", padx=6, pady=2)
            ttk.Label(row, text=entry["flag"], width=30).pack(side="left")
            typ = entry["type"]
            default = entry.get("default")
            if typ == "boolean":
                var = tk.BooleanVar(value=bool(default))
                ttk.Checkbutton(row, variable=var, command=lambda: update_preview()).pack(side="left")
                vars_map[entry["name"]] = var
            elif typ == "choice":
                var = tk.StringVar(value=str(default))
                combo = ttk.Combobox(row, textvariable=var, values=entry.get("choices", []), state="readonly")
                combo.pack(side="left", fill="x", expand=True)
                combo.bind("<<ComboboxSelected>>", lambda _e: update_preview())
                vars_map[entry["name"]] = var
            elif typ == "multi_choice":
                selected: dict[str, tk.BooleanVar] = {}
                holder = ttk.Frame(row)
                holder.pack(side="left", fill="x", expand=True)
                defaults = set(default if isinstance(default, list) else [])
                for choice in entry.get("choices", []):
                    v = tk.BooleanVar(value=choice in defaults)
                    ttk.Checkbutton(holder, text=choice, variable=v, command=lambda: update_preview()).pack(side="left")
                    selected[choice] = v
                vars_map[entry["name"]] = selected
            else:
                var = tk.StringVar(value=str(default if default not in (None, "") else ""))
                e = ttk.Entry(row, textvariable=var)
                e.pack(side="left", fill="x", expand=True)
                e.bind("<KeyRelease>", lambda _e: update_preview())
                vars_map[entry["name"]] = var

    preview = tk.Text(root, height=10)
    preview.pack(fill="both", expand=False, padx=8, pady=8)

    log = tk.Text(root, height=10)
    log.pack(fill="both", expand=True, padx=8, pady=8)

    runner = ProcessRunner(lambda line: root.after(0, lambda: (log.insert("end", line + "\n"), log.see("end"))), lambda code, path: root.after(0, lambda: log.insert("end", f"Done ({code}) -> {path}\n")))

    dry_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(root, text="Dry run", variable=dry_var, command=lambda: update_preview()).pack(anchor="w", padx=8)

    def collect_values() -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, obj in vars_map.items():
            if isinstance(obj, tk.BooleanVar):
                out[name] = obj.get()
            elif isinstance(obj, tk.StringVar):
                out[name] = obj.get().strip()
            elif isinstance(obj, dict):
                out[name] = [choice for choice, v in obj.items() if v.get()]
        return out

    def collect_datasets() -> list[str]:
        return [datasets_box.get(i) for i in range(datasets_box.size())]

    def update_preview() -> None:
        result = builder.build(
            executable=exe_var.get().strip() or "navvis-postprocessing",
            values=collect_values(),
            dataset_entries=collect_datasets(),
            include_subfolders=False,
            subfolder_root="",
            force_no_color=False,
        )
        preview.delete("1.0", "end")
        preview.insert("1.0", shell_join(result.command))
        if result.errors:
            preview.insert("end", "\n\nErrors:\n- " + "\n- ".join(result.errors))

    def start_run() -> None:
        result = builder.build(
            executable=exe_var.get().strip() or "navvis-postprocessing",
            values=collect_values(),
            dataset_entries=collect_datasets(),
            include_subfolders=False,
            subfolder_root="",
            force_no_color=True,
        )
        if result.errors:
            messagebox.showerror("Validation", "\n".join(result.errors))
            return
        runner.start(result.command, dry_var.get())

    controls = ttk.Frame(root)
    controls.pack(fill="x", padx=8, pady=8)
    ttk.Button(controls, text="Start", command=start_run).pack(side="left")
    ttk.Button(controls, text="Stop", command=runner.stop).pack(side="left", padx=4)

    update_preview()
    root.mainloop()


def main() -> int:
    if PYSIDE_AVAILABLE:
        app = QApplication(sys.argv)
        window = MainWindow()
        window.show()
        return app.exec()

    run_tk_fallback()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
