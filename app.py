from __future__ import annotations

import json
import mimetypes
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import traceback
import urllib.parse
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from photoculler import __version__
from photoculler.core import (
    BUILTIN_PROFILES,
    ConfigStore,
    ProjectManager,
    Scanner,
    apply_quarantine,
    build_similarity_groups,
    classify,
    classification_percentiles,
    clear_decisions,
    connect_db,
    export_decisions,
    import_decisions,
    parse_photo_filter,
    photo_filter_where,
    photo_library_counts,
    PHOTO_AI_FILTERS,
    PHOTO_DECISION_FILTERS,
    quarantine_preview,
    restore_batch,
    validate_profile,
    app_data_dir,
)


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


CONFIG = ConfigStore()
MANAGER = ProjectManager(CONFIG)
SCANNER = Scanner(CONFIG, MANAGER)
TOKEN = secrets.token_urlsafe(24)
WEB_ROOT = resource_path("web")


def choose_directory(title: str) -> str:
    try:
        import webview

        if webview.windows:
            selected = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
            return selected[0] if selected else ""
    except Exception:
        pass
    safe_title = title.replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
        f"$d.Description='{safe_title}';"
        "if($d.ShowDialog() -eq 'OK'){[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
        "[Console]::Write($d.SelectedPath)}"
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-STA", "-Command", script],
        capture_output=True, creationflags=flags, check=False,
    )
    return result.stdout.decode("utf-8", errors="replace").strip()


def choose_csv(title: str) -> str:
    try:
        import webview

        if webview.windows:
            selected = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False, file_types=("CSV 文件 (*.csv)",)
            )
            return selected[0] if selected else ""
    except Exception:
        pass
    safe_title = title.replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$d=New-Object System.Windows.Forms.OpenFileDialog;"
        f"$d.Title='{safe_title}';$d.Filter='CSV 文件 (*.csv)|*.csv|所有文件 (*.*)|*.*';"
        "if($d.ShowDialog() -eq 'OK'){[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
        "[Console]::Write($d.FileName)}"
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-STA", "-Command", script],
        capture_output=True, creationflags=flags, check=False,
    )
    return result.stdout.decode("utf-8", errors="replace").strip()


def choose_save_csv(title: str, default_dir: Path, default_name: str) -> str:
    try:
        import webview

        if webview.windows:
            selected = webview.windows[0].create_file_dialog(
                webview.SAVE_DIALOG,
                directory=str(default_dir),
                save_filename=default_name,
                file_types=("CSV 文件 (*.csv)",),
            )
            if isinstance(selected, (list, tuple)):
                return str(selected[0]) if selected else ""
            return str(selected or "")
    except Exception:
        pass
    safe_title = title.replace("'", "''")
    safe_dir = str(default_dir).replace("'", "''")
    safe_name = default_name.replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$d=New-Object System.Windows.Forms.SaveFileDialog;"
        f"$d.Title='{safe_title}';$d.InitialDirectory='{safe_dir}';$d.FileName='{safe_name}';"
        "$d.Filter='CSV 文件 (*.csv)|*.csv';$d.DefaultExt='csv';$d.AddExtension=$true;"
        "if($d.ShowDialog() -eq 'OK'){[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
        "[Console]::Write($d.FileName)}"
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-STA", "-Command", script],
        capture_output=True, creationflags=flags, check=False,
    )
    return result.stdout.decode("utf-8", errors="replace").strip()


def json_safe_row(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def photo_payload(project_id: str, row: Any) -> dict[str, Any]:
    data = json_safe_row(row)
    data["project_id"] = project_id
    data["thumb_url"] = f"/api/thumb?project_id={project_id}&id={row['id']}&token={TOKEN}"
    data["photo_url"] = f"/api/photo?project_id={project_id}&id={row['id']}&token={TOKEN}"
    return data


def project_summary(project_id: str) -> dict[str, Any]:
    project = MANAGER.from_id(project_id)
    conn = connect_db(project.db_path)
    counts = {
        row["suggestion"]: row["count"]
        for row in conn.execute(
            "SELECT suggestion,COUNT(*) count FROM photos WHERE status='active' GROUP BY suggestion"
        )
    }
    decisions = {
        row["decision"]: row["count"]
        for row in conn.execute(
            "SELECT decision,COUNT(*) count FROM photos WHERE status='active' AND decision<>'' GROUP BY decision"
        )
    }
    total = conn.execute("SELECT COUNT(*) FROM photos WHERE status='active'").fetchone()[0]
    library_counts = photo_library_counts(conn)
    pairs = conn.execute("SELECT COUNT(*) FROM similar_pairs").fetchone()[0]
    profile = CONFIG.profiles().get(project.profile_id, BUILTIN_PROFILES["balanced"])
    similar_groups = len(build_similarity_groups(conn, profile))
    conn.close()
    return {
        "id": project_id,
        "root": str(project.root),
        "cache_root": str(project.cache_root),
        "profile_id": project.profile_id,
        "total": total,
        "library_counts": library_counts,
        "counts": counts,
        "decisions": decisions,
        "pairs": pairs,
        "similar_groups": similar_groups,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = f"PhotoCuller/{__version__}"

    def log_message(self, format: str, *args: Any) -> None:
        if os.environ.get("PHOTOCULLER_DEBUG"):
            super().log_message(format, *args)

    def _parsed(self):
        return urllib.parse.urlparse(self.path)

    def _query(self) -> dict[str, list[str]]:
        return urllib.parse.parse_qs(self._parsed().query, keep_blank_values=True)

    def _authorized(self) -> bool:
        if not self._parsed().path.startswith("/api/"):
            return True
        query_token = self._query().get("token", [""])[0]
        header_token = self.headers.get("X-App-Token", "")
        return secrets.compare_digest(query_token or header_token, TOKEN)

    def _send_json(self, value: Any, status: int = 200) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path: Path, content_type: str | None = None) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        if not self._authorized():
            self._send_json({"error": "unauthorized"}, 403)
            return
        try:
            parsed = self._parsed()
            if parsed.path == "/":
                html = (WEB_ROOT / "index.html").read_text(encoding="utf-8").replace("__APP_TOKEN__", TOKEN)
                data = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            if parsed.path.startswith("/static/"):
                candidate = (WEB_ROOT / parsed.path.removeprefix("/static/")).resolve()
                if WEB_ROOT.resolve() not in candidate.parents:
                    self.send_error(403)
                else:
                    self._send_file(candidate)
                return
            routes = {
                "/api/bootstrap": self.api_bootstrap,
                "/api/project": self.api_project,
                "/api/progress": self.api_progress,
                "/api/photos": self.api_photos,
                "/api/pairs": self.api_pairs,
                "/api/similar-groups": self.api_similar_groups,
                "/api/similar-group": self.api_similar_group,
                "/api/thumb": self.api_thumb,
                "/api/photo": self.api_photo,
                "/api/profiles": self.api_profiles,
                "/api/export": self.api_export,
                "/api/quarantine/preview": self.api_quarantine_preview,
                "/api/quarantine/batches": self.api_batches,
            }
            handler = routes.get(parsed.path)
            if not handler:
                self.send_error(404)
            else:
                handler()
        except ValueError as error:
            self._send_json({"error": str(error)}, 400)
        except Exception as error:
            self._send_json({"error": str(error)}, 500)

    def do_POST(self) -> None:
        if not self._authorized():
            self._send_json({"error": "unauthorized"}, 403)
            return
        try:
            routes = {
                "/api/choose-folder": self.api_choose_folder,
                "/api/choose-cache": self.api_choose_cache,
                "/api/choose-csv": self.api_choose_csv,
                "/api/project/open": self.api_open_project,
                "/api/project/cache": self.api_project_cache,
                "/api/project/cache/cleanup": self.api_cache_cleanup,
                "/api/project/open-folder": self.api_project_open_folder,
                "/api/project/remove-recent": self.api_project_remove_recent,
                "/api/open-github": self.api_open_github,
                "/api/scan": self.api_scan,
                "/api/scan/cancel": self.api_scan_cancel,
                "/api/decision": self.api_decision,
                "/api/decision/clear": self.api_decision_clear,
                "/api/settings": self.api_settings,
                "/api/profile/save": self.api_profile_save,
                "/api/profile/delete": self.api_profile_delete,
                "/api/profile/apply": self.api_profile_apply,
                "/api/profile/estimate": self.api_profile_estimate,
                "/api/export/save": self.api_export_save,
                "/api/import": self.api_import,
                "/api/quarantine/apply": self.api_quarantine_apply,
                "/api/quarantine/restore": self.api_restore,
            }
            handler = routes.get(self._parsed().path)
            if not handler:
                self.send_error(404)
            else:
                handler(self._body())
        except ValueError as error:
            self._send_json({"error": str(error)}, 400)
        except Exception as error:
            self._send_json({"error": str(error)}, 500)

    def api_bootstrap(self) -> None:
        recent = []
        for pid in CONFIG.data.get("recent_projects", []):
            project = CONFIG.data.get("projects", {}).get(pid)
            if project:
                recent.append({"id": pid, **project, "available": Path(project["root"]).is_dir()})
        self._send_json({
            "version": __version__,
            "settings": {
                "default_cache_root": CONFIG.data["default_cache_root"],
                "auto_advance": CONFIG.data.get("auto_advance", True),
                "theme": CONFIG.data.get("theme", "day"),
            },
            "recent_projects": recent,
            "profiles": list(CONFIG.profiles().values()),
        })

    def api_project(self) -> None:
        pid = self._query().get("project_id", [""])[0]
        self._send_json(project_summary(pid))

    def api_progress(self) -> None:
        pid = self._query().get("project_id", [""])[0]
        self._send_json(SCANNER.progress.get(pid, {"stage": "idle", "done": True}))

    def api_photos(self) -> None:
        query = self._query()
        pid = query.get("project_id", [""])[0]
        search = query.get("search", [""])[0]
        limit = min(500, max(1, int(query.get("limit", ["200"])[0])))
        offset = max(0, int(query.get("offset", ["0"])[0]))
        project = MANAGER.from_id(pid)

        file_state = query.get("file", ["readable"])[0]
        raw_decisions = query.get("decisions", [None])[0]
        raw_ai = query.get("ai_states", [None])[0]
        category = query.get("category", [None])[0]
        if category and raw_decisions is None and raw_ai is None and "file" not in query:
            if category == "quality":
                suggestion = query.get("suggestion", [""])[0]
                raw_ai = suggestion if suggestion in {"remove", "review"} else "remove,review"
            elif category == "unreadable":
                file_state = "unreadable"
            elif category == "decided":
                raw_decisions = "keep,remove"
            elif category != "all":
                raise ValueError(f"category 包含无效值：{category}")

        decisions = parse_photo_filter(
            raw_decisions, PHOTO_DECISION_FILTERS, "decisions"
        )
        ai_states = parse_photo_filter(raw_ai, PHOTO_AI_FILTERS, "ai_states")
        where, params = photo_filter_where(file_state, decisions, ai_states)
        if search:
            where += " AND relative_path LIKE ?"
            params.append(f"%{search}%")
        conn = connect_db(project.db_path)
        total = conn.execute(f"SELECT COUNT(*) FROM photos WHERE {where}", params).fetchone()[0]
        rows = conn.execute(
            f"""SELECT * FROM photos WHERE {where}
                ORDER BY CASE suggestion WHEN 'remove' THEN 0 WHEN 'review' THEN 1 WHEN 'unreadable' THEN 2 ELSE 3 END,
                relative_path LIMIT ? OFFSET ?""",
            [*params, limit, offset],
        ).fetchall()
        conn.close()
        self._send_json({"total": total, "items": [photo_payload(pid, row) for row in rows]})

    def api_pairs(self) -> None:
        query = self._query()
        pid = query.get("project_id", [""])[0]
        search = query.get("search", [""])[0]
        project = MANAGER.from_id(pid)
        conn = connect_db(project.db_path)
        rows = conn.execute(
            """SELECT sp.*, a.relative_path a_path, b.relative_path b_path
               FROM similar_pairs sp JOIN photos a ON a.id=sp.a_id JOIN photos b ON b.id=sp.b_id
               WHERE a.status='active' AND b.status='active'
               ORDER BY sp.kind='exact' DESC,sp.score DESC"""
        ).fetchall()
        items = []
        for pair in rows:
            if search and search.casefold() not in (pair["a_path"] + " " + pair["b_path"]).casefold():
                continue
            a = conn.execute("SELECT * FROM photos WHERE id=?", (pair["a_id"],)).fetchone()
            b = conn.execute("SELECT * FROM photos WHERE id=?", (pair["b_id"],)).fetchone()
            items.append({
                "id": pair["id"], "score": pair["score"], "kind": pair["kind"],
                "recommended_id": pair["recommended_id"], "face_safe": bool(pair["face_safe"]),
                "a": photo_payload(pid, a), "b": photo_payload(pid, b),
            })
        conn.close()
        self._send_json({"total": len(items), "items": items})

    def api_similar_groups(self) -> None:
        query = self._query()
        pid = query.get("project_id", [""])[0]
        search = query.get("search", [""])[0].casefold()
        project = MANAGER.from_id(pid)
        profile = CONFIG.profiles().get(project.profile_id, BUILTIN_PROFILES["balanced"])
        conn = connect_db(project.db_path)
        groups = build_similarity_groups(conn, profile)
        items = []
        for group in groups:
            if search and not any(
                search in str(row["relative_path"]).casefold() for row in group["members"]
            ):
                continue
            items.append(
                {
                    "id": group["id"],
                    "count": len(group["members"]),
                    "kind": group["kind"],
                    "recommended_id": group["recommended_id"],
                    "recommended": photo_payload(pid, group["recommended"]),
                    "covers": [photo_payload(pid, row) for row in group["covers"]],
                    "face_safe": group["face_safe"],
                }
            )
        conn.close()
        self._send_json({"total": len(items), "items": items})

    def api_similar_group(self) -> None:
        query = self._query()
        pid = query.get("project_id", [""])[0]
        group_id = query.get("group_id", [""])[0]
        search = query.get("search", [""])[0].casefold()
        project = MANAGER.from_id(pid)
        profile = CONFIG.profiles().get(project.profile_id, BUILTIN_PROFILES["balanced"])
        conn = connect_db(project.db_path)
        group = next(
            (item for item in build_similarity_groups(conn, profile) if item["id"] == group_id),
            None,
        )
        if not group:
            conn.close()
            raise ValueError("相似照片组不存在或已发生变化")
        members = []
        for row in group["members"]:
            if search and search not in str(row["relative_path"]).casefold():
                continue
            item = photo_payload(pid, row)
            item["group_similarity"] = group["confidence"].get(int(row["id"]), 0.0)
            members.append(item)
        result = {
            "id": group["id"],
            "count": len(group["members"]),
            "kind": group["kind"],
            "recommended_id": group["recommended_id"],
            "face_safe": group["face_safe"],
            "members": members,
        }
        conn.close()
        self._send_json(result)

    def _photo_row(self):
        query = self._query()
        pid = query.get("project_id", [""])[0]
        photo_id = int(query.get("id", ["0"])[0])
        project = MANAGER.from_id(pid)
        conn = connect_db(project.db_path)
        row = conn.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
        conn.close()
        if not row:
            raise ValueError("照片不存在")
        return project, row

    def api_thumb(self) -> None:
        _, row = self._photo_row()
        self._send_file(Path(row["thumbnail"]), "image/jpeg")

    def api_photo(self) -> None:
        project, row = self._photo_row()
        path = project.root / row["relative_path"]
        if path.suffix.lower() in {".heic", ".heif", ".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2", ".pef"}:
            self._send_file(Path(row["thumbnail"]), "image/jpeg")
        else:
            self._send_file(path)

    def api_profiles(self) -> None:
        self._send_json({"items": list(CONFIG.profiles().values())})

    def api_export(self) -> None:
        pid = self._query().get("project_id", [""])[0]
        project = MANAGER.from_id(pid)
        data = export_decisions(project).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="photo-review.csv"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def api_quarantine_preview(self) -> None:
        pid = self._query().get("project_id", [""])[0]
        self._send_json(quarantine_preview(MANAGER.from_id(pid)))

    def api_batches(self) -> None:
        pid = self._query().get("project_id", [""])[0]
        project = MANAGER.from_id(pid)
        conn = connect_db(project.db_path)
        rows = conn.execute("SELECT * FROM quarantine_batches ORDER BY created_at DESC").fetchall()
        conn.close()
        self._send_json({"items": [json_safe_row(row) for row in rows]})

    def api_choose_folder(self, body: dict[str, Any]) -> None:
        self._send_json({"path": choose_directory("选择照片文件夹")})

    def api_choose_cache(self, body: dict[str, Any]) -> None:
        self._send_json({"path": choose_directory("选择缩略图和数据库存储位置")})

    def api_choose_csv(self, body: dict[str, Any]) -> None:
        self._send_json({"path": choose_csv("选择筛选结果 CSV")})

    def api_open_project(self, body: dict[str, Any]) -> None:
        project = MANAGER.open(body["root"], body.get("cache_root"))
        self._send_json(project_summary(project.project_id))

    def api_project_cache(self, body: dict[str, Any]) -> None:
        self._send_json(MANAGER.migrate_cache(body["project_id"], body["cache_root"]))

    def api_cache_cleanup(self, body: dict[str, Any]) -> None:
        self._send_json(MANAGER.cleanup_old_cache(body["project_id"], body["path"]))

    def api_project_open_folder(self, body: dict[str, Any]) -> None:
        root = MANAGER.project_root(body["project_id"])
        if not hasattr(os, "startfile"):
            raise ValueError("当前系统不支持文件管理器操作")
        os.startfile(str(root))
        self._send_json({"opened": True})

    def api_project_remove_recent(self, body: dict[str, Any]) -> None:
        MANAGER.remove_from_recent(body["project_id"])
        self._send_json({"removed": True})

    def api_open_github(self, body: dict[str, Any]) -> None:
        webbrowser.open("https://github.com/Yuumi0221/photo-culler")
        self._send_json({"opened": True})

    def api_scan(self, body: dict[str, Any]) -> None:
        SCANNER.start(body["project_id"])
        self._send_json({"started": True})

    def api_scan_cancel(self, body: dict[str, Any]) -> None:
        SCANNER.cancel(body["project_id"])
        self._send_json({"cancelled": True})

    def api_decision(self, body: dict[str, Any]) -> None:
        project = MANAGER.from_id(body["project_id"])
        decision = body.get("decision", "")
        if decision not in {"", "keep", "remove"}:
            raise ValueError("无效决定")
        conn = connect_db(project.db_path)
        conn.execute("UPDATE photos SET decision=? WHERE id=?", (decision, int(body["photo_id"])))
        conn.commit()
        conn.close()
        self._send_json({"saved": True})

    def api_decision_clear(self, body: dict[str, Any]) -> None:
        project = MANAGER.from_id(body["project_id"])
        self._send_json({"cleared": clear_decisions(project)})

    def api_settings(self, body: dict[str, Any]) -> None:
        if "default_cache_root" in body:
            path = Path(body["default_cache_root"]).resolve()
            path.mkdir(parents=True, exist_ok=True)
            CONFIG.data["default_cache_root"] = str(path)
        if "auto_advance" in body:
            CONFIG.data["auto_advance"] = bool(body["auto_advance"])
        if "theme" in body:
            theme = str(body["theme"])
            if theme not in {"day", "night"}:
                raise ValueError("主题必须为 day 或 night")
            CONFIG.data["theme"] = theme
        CONFIG.save()
        self._send_json({"saved": True, "settings": CONFIG.data})

    def api_profile_save(self, body: dict[str, Any]) -> None:
        self._send_json(CONFIG.save_custom_profile(body["profile"]))

    def api_profile_delete(self, body: dict[str, Any]) -> None:
        CONFIG.delete_custom_profile(body["profile_id"])
        self._send_json({"deleted": True})

    def api_profile_apply(self, body: dict[str, Any]) -> None:
        project = MANAGER.from_id(body["project_id"])
        profile_id = body["profile_id"]
        profile = CONFIG.get_profile(profile_id)
        CONFIG.data["projects"][project.project_id]["profile_id"] = profile_id
        CONFIG.save()
        conn = connect_db(project.db_path)
        conn.execute("UPDATE project SET profile_id=?,updated_at=datetime('now') WHERE id=1", (profile_id,))
        SCANNER.reclassify(project, conn, profile)
        SCANNER.rebuild_similarity(project, conn, profile)
        conn.close()
        self._send_json(project_summary(project.project_id))

    def api_profile_estimate(self, body: dict[str, Any]) -> None:
        profile = body["profile"]
        validate_profile(profile)
        project = MANAGER.from_id(body["project_id"])
        conn = connect_db(project.db_path)
        rows = conn.execute("SELECT * FROM photos WHERE status='active'").fetchall()
        percentiles = classification_percentiles(rows, profile)
        counts = {"remove": 0, "review": 0, "keep": 0, "unreadable": 0}
        for row in rows:
            suggestion, _ = classify(row, profile, percentiles)
            counts[suggestion] = counts.get(suggestion, 0) + 1
        estimate_conn = sqlite3.connect(":memory:")
        estimate_conn.row_factory = sqlite3.Row
        conn.backup(estimate_conn)
        conn.close()
        SCANNER.rebuild_similarity(project, estimate_conn, profile)
        estimated_pairs = estimate_conn.execute("SELECT COUNT(*) FROM similar_pairs").fetchone()[0]
        estimated_groups = len(build_similarity_groups(estimate_conn, profile))
        estimate_conn.close()
        self._send_json({
            "counts": counts,
            "estimated_pairs": estimated_pairs,
            "estimated_groups": estimated_groups,
        })

    def api_export_save(self, body: dict[str, Any]) -> None:
        project = MANAGER.from_id(body["project_id"])
        selected = choose_save_csv("保存筛选结果 CSV", project.root, "照片筛选结果.csv")
        if not selected:
            self._send_json({"saved": False, "cancelled": True})
            return
        path = Path(selected)
        if path.suffix.lower() != ".csv":
            path = path.with_suffix(".csv")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(export_decisions(project), encoding="utf-8")
        self._send_json({"saved": True, "path": str(path)})

    def api_import(self, body: dict[str, Any]) -> None:
        project = MANAGER.from_id(body["project_id"])
        csv_path = Path(body.get("path") or "")
        if not csv_path.exists():
            raise ValueError("CSV 文件不存在")
        self._send_json(import_decisions(project, csv_path))

    def api_quarantine_apply(self, body: dict[str, Any]) -> None:
        self._send_json(apply_quarantine(MANAGER.from_id(body["project_id"])))

    def api_restore(self, body: dict[str, Any]) -> None:
        self._send_json(restore_batch(MANAGER.from_id(body["project_id"]), body["batch_id"]))


def run() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/?token={TOKEN}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import webview

        webview.create_window("通用照片筛选器", url, width=1460, height=940, min_size=(980, 680))
        webview.start()
    except Exception as error:
        try:
            log_path = app_data_dir() / "webview-error.log"
            log_path.write_text(
                f"{datetime.now().isoformat(timespec='seconds')}\n{traceback.format_exc()}",
                encoding="utf-8",
            )
        except Exception:
            pass
        webbrowser.open(url)
        try:
            while True:
                threading.Event().wait(3600)
        except KeyboardInterrupt:
            pass
    finally:
        server.shutdown()


if __name__ == "__main__":
    run()
