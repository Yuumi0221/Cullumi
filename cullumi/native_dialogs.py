from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


def _run_powershell_dialog(script: str) -> str:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-STA", "-Command", script],
        capture_output=True,
        creationflags=flags,
        check=False,
    )
    return result.stdout.decode("utf-8", errors="replace").strip()


def _webview_dialog(kind: str, **options: Any) -> str | None:
    try:
        import webview

        if not webview.windows:
            return None
        kinds = {
            "FOLDER": webview.FileDialog.FOLDER,
            "OPEN": webview.FileDialog.OPEN,
            "SAVE": webview.FileDialog.SAVE,
        }
        selected = webview.windows[0].create_file_dialog(kinds[kind], **options)
        if isinstance(selected, (list, tuple)):
            return str(selected[0]) if selected else ""
        return str(selected or "")
    except Exception:
        return None


def choose_directory(title: str) -> str:
    selected = _webview_dialog("FOLDER")
    if selected is not None:
        return selected
    safe_title = title.replace("'", "''")
    return _run_powershell_dialog(
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
        f"$d.Description='{safe_title}';"
        "if($d.ShowDialog() -eq 'OK'){[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
        "[Console]::Write($d.SelectedPath)}"
    )


def choose_csv(title: str) -> str:
    selected = _webview_dialog(
        "OPEN", allow_multiple=False, file_types=("CSV 文件 (*.csv)",)
    )
    if selected is not None:
        return selected
    safe_title = title.replace("'", "''")
    return _run_powershell_dialog(
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$d=New-Object System.Windows.Forms.OpenFileDialog;"
        f"$d.Title='{safe_title}';$d.Filter='CSV 文件 (*.csv)|*.csv|所有文件 (*.*)|*.*';"
        "if($d.ShowDialog() -eq 'OK'){[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
        "[Console]::Write($d.FileName)}"
    )


def choose_save_csv(title: str, default_dir: Path, default_name: str) -> str:
    selected = _webview_dialog(
        "SAVE",
        directory=str(default_dir),
        save_filename=default_name,
        file_types=("CSV 文件 (*.csv)",),
    )
    if selected is not None:
        return selected
    safe_title = title.replace("'", "''")
    safe_dir = str(default_dir).replace("'", "''")
    safe_name = default_name.replace("'", "''")
    return _run_powershell_dialog(
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$d=New-Object System.Windows.Forms.SaveFileDialog;"
        f"$d.Title='{safe_title}';$d.InitialDirectory='{safe_dir}';$d.FileName='{safe_name}';"
        "$d.Filter='CSV 文件 (*.csv)|*.csv';$d.DefaultExt='csv';$d.AddExtension=$true;"
        "if($d.ShowDialog() -eq 'OK'){[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
        "[Console]::Write($d.FileName)}"
    )
