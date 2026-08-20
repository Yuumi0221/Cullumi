from __future__ import annotations

import json
import mimetypes
import os
import secrets
import sqlite3
import stat
import subprocess
import sys
import threading
import traceback
import urllib.parse
import webbrowser
from contextlib import closing
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from cullumi import __version__
from cullumi.core import (
    ConfigStore,
    DISPLAY_PREVIEW_EXTENSIONS,
    ProjectManager,
    Scanner,
    SimilarityGroupCache,
    apply_quarantine,
    build_similarity_groups,
    classify,
    classification_percentiles,
    clear_decisions,
    connect_db,
    ensure_display_preview,
    export_decisions,
    import_decisions,
    mark_ai_remove_suggestions,
    parse_photo_filter,
    photo_filter_where,
    photo_library_counts,
    PHOTO_AI_FILTERS,
    PHOTO_DECISION_FILTERS,
    project_id_for,
    quarantine_preview,
    restore_batch,
    safe_relative_path,
    validate_profile,
    app_data_dir,
)
from cullumi.updates import (
    RELEASES_PAGE_URL,
    check_for_update,
    download_release_asset,
)


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


CONFIG = ConfigStore()
MANAGER = ProjectManager(CONFIG)
SIMILARITY_GROUPS = SimilarityGroupCache()
SCANNER = Scanner(CONFIG, MANAGER, SIMILARITY_GROUPS)
TOKEN = secrets.token_urlsafe(24)
WEB_ROOT = resource_path("web")
APP_ICON = WEB_ROOT / "brand-icon.ico"
FILE_RESPONSE_CHUNK_SIZE = 256 * 1024


def apply_native_window_icon(window: Any) -> None:
    if sys.platform != "win32" or not APP_ICON.is_file():
        return
    if not window.events.shown.wait(15):
        return
    try:
        from System import Action
        from System.Drawing import Icon

        native = window.native
        icon = Icon(str(APP_ICON))
        native.Invoke(Action(lambda: setattr(native, "Icon", icon)))
    except Exception:
        pass


def choose_directory(title: str) -> str:
    try:
        import webview

        if webview.windows:
            selected = webview.windows[0].create_file_dialog(webview.FileDialog.FOLDER)
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
                webview.FileDialog.OPEN, allow_multiple=False, file_types=("CSV 文件 (*.csv)",)
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
                webview.FileDialog.SAVE,
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
    with closing(connect_db(project.db_path)) as conn:
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
        profile = CONFIG.get_profile(project.profile_id)
        similar_groups = SIMILARITY_GROUPS.count(project_id, conn, profile)
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


def recent_project_payload(project_id: str, stored: dict[str, Any]) -> dict[str, Any]:
    available = Path(stored["root"]).is_dir()
    payload = {
        "id": project_id,
        **stored,
        "available": available,
        "total": 0,
        "kept": 0,
        "thumbnail_url": "",
    }
    if not available:
        return payload
    conn = None
    try:
        project = MANAGER.from_id(project_id)
        if not project.db_path.is_file():
            return payload
        conn = connect_db(project.db_path)
        counts = conn.execute(
            """SELECT COUNT(*) total,
                      SUM(CASE WHEN decision='keep' THEN 1 ELSE 0 END) kept
                 FROM photos WHERE status='active'"""
        ).fetchone()
        payload["total"] = int(counts["total"] or 0)
        payload["kept"] = int(counts["kept"] or 0)
        cover = conn.execute(
            """SELECT id,thumbnail FROM photos
                WHERE status='active' AND error='' AND thumbnail<>''
                ORDER BY CASE WHEN decision='keep' THEN 0 ELSE 1 END,
                         mtime DESC, id DESC LIMIT 1"""
        ).fetchone()
        if cover and Path(cover["thumbnail"]).is_file():
            query = urllib.parse.urlencode({
                "project_id": project_id,
                "id": cover["id"],
                "token": TOKEN,
            })
            payload["thumbnail_url"] = f"/api/thumb?{query}"
    except Exception:
        pass
    finally:
        if conn is not None:
            conn.close()
    return payload


class Handler(BaseHTTPRequestHandler):
    server_version = f"Cullumi/{__version__}"

    def log_message(self, format: str, *args: Any) -> None:
        if os.environ.get("CULLUMI_DEBUG"):
            super().log_message(format, *args)

    def _parsed(self):
        return urllib.parse.urlparse(self.path)

    def _query(self) -> dict[str, list[str]]:
        return urllib.parse.parse_qs(self._parsed().query, keep_blank_values=True)

    def _authorized(self) -> bool:
        path = self._parsed().path
        if path != "/" and not path.startswith("/api/"):
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
        try:
            source = path.open("rb")
        except (FileNotFoundError, IsADirectoryError, PermissionError):
            self.send_error(404)
            return

        with source:
            file_stat = os.fstat(source.fileno())
            if not stat.S_ISREG(file_stat.st_mode):
                self.send_error(404)
                return

            self.send_response(200)
            self.send_header(
                "Content-Type",
                content_type
                or mimetypes.guess_type(path.name)[0]
                or "application/octet-stream",
            )
            self.send_header("Content-Length", str(file_stat.st_size))
            self.send_header("Cache-Control", "private, max-age=3600")
            self.end_headers()

            remaining = file_stat.st_size
            try:
                while remaining:
                    chunk = source.read(min(FILE_RESPONSE_CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except ConnectionError:
                # The client closed the connection while a large file was streaming.
                return

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
                "/api/similar-groups": self.api_similar_groups,
                "/api/similar-group": self.api_similar_group,
                "/api/thumb": self.api_thumb,
                "/api/photo": self.api_photo,
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
                "/api/update/check": self.api_update_check,
                "/api/update/download": self.api_update_download,
                "/api/update/open": self.api_update_open,
                "/api/scan": self.api_scan,
                "/api/scan/cancel": self.api_scan_cancel,
                "/api/decision": self.api_decision,
                "/api/decision/ai-remove": self.api_decision_ai_remove,
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
                recent.append(recent_project_payload(pid, project))
        self._send_json({
            "version": __version__,
            "startup_warning": CONFIG.load_warning,
            "settings": {
                "default_cache_root": CONFIG.data["default_cache_root"],
                "auto_advance": CONFIG.data.get("auto_advance", True),
                "auto_check_updates": CONFIG.data.get("auto_check_updates", True),
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
        self._send_json(SCANNER.get_progress(pid))

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

        decisions = parse_photo_filter(
            raw_decisions, PHOTO_DECISION_FILTERS, "decisions"
        )
        ai_states = parse_photo_filter(raw_ai, PHOTO_AI_FILTERS, "ai_states")
        where, params = photo_filter_where(file_state, decisions, ai_states)
        if search:
            where += " AND relative_path LIKE ?"
            params.append(f"%{search}%")
        with closing(connect_db(project.db_path)) as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM photos WHERE {where}", params).fetchone()[0]
            rows = conn.execute(
                f"""SELECT * FROM photos WHERE {where}
                    ORDER BY CASE suggestion WHEN 'remove' THEN 0 WHEN 'review' THEN 1 WHEN 'unreadable' THEN 2 ELSE 3 END,
                    relative_path LIMIT ? OFFSET ?""",
                [*params, limit, offset],
            ).fetchall()
        self._send_json({"total": total, "items": [photo_payload(pid, row) for row in rows]})

    def api_similar_groups(self) -> None:
        query = self._query()
        pid = query.get("project_id", [""])[0]
        search = query.get("search", [""])[0].casefold()
        project = MANAGER.from_id(pid)
        profile = CONFIG.get_profile(project.profile_id)
        with closing(connect_db(project.db_path)) as conn:
            groups = SIMILARITY_GROUPS.get(pid, conn, profile)
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
        self._send_json({"total": len(items), "items": items})

    def api_similar_group(self) -> None:
        query = self._query()
        pid = query.get("project_id", [""])[0]
        group_id = query.get("group_id", [""])[0]
        search = query.get("search", [""])[0].casefold()
        project = MANAGER.from_id(pid)
        profile = CONFIG.get_profile(project.profile_id)
        with closing(connect_db(project.db_path)) as conn:
            group = next(
                (item for item in SIMILARITY_GROUPS.get(pid, conn, profile) if item["id"] == group_id),
                None,
            )
        if not group:
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
        self._send_json(result)

    def _photo_row(self):
        query = self._query()
        pid = query.get("project_id", [""])[0]
        photo_id = int(query.get("id", ["0"])[0])
        project = MANAGER.from_id(pid)
        with closing(connect_db(project.db_path)) as conn:
            row = conn.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
        if not row:
            raise ValueError("照片不存在")
        return project, row

    def api_thumb(self) -> None:
        _, row = self._photo_row()
        self._send_file(Path(row["thumbnail"]), "image/jpeg")

    def api_photo(self) -> None:
        project, row = self._photo_row()
        path = safe_relative_path(project.root, row["relative_path"], "照片路径")
        if path.suffix.lower() not in DISPLAY_PREVIEW_EXTENSIONS:
            self._send_file(path)
            return
        thumbnail = Path(row["thumbnail"])
        try:
            preview = ensure_display_preview(path, thumbnail)
        except (OSError, RuntimeError):
            # A previously generated thumbnail is still more useful than a
            # broken viewer if the source becomes temporarily unavailable.
            preview = thumbnail
        self._send_file(preview, "image/jpeg")

    def api_quarantine_preview(self) -> None:
        pid = self._query().get("project_id", [""])[0]
        self._send_json(quarantine_preview(MANAGER.from_id(pid)))

    def api_batches(self) -> None:
        pid = self._query().get("project_id", [""])[0]
        project = MANAGER.from_id(pid)
        with closing(connect_db(project.db_path)) as conn:
            rows = conn.execute("SELECT * FROM quarantine_batches ORDER BY created_at DESC").fetchall()
        self._send_json({"items": [json_safe_row(row) for row in rows]})

    def api_choose_folder(self, body: dict[str, Any]) -> None:
        self._send_json({"path": choose_directory("从文件夹导入")})

    def api_choose_cache(self, body: dict[str, Any]) -> None:
        self._send_json({"path": choose_directory("选择缩略图和数据库存储位置")})

    def api_choose_csv(self, body: dict[str, Any]) -> None:
        self._send_json({"path": choose_csv("选择筛选结果 CSV")})

    def api_open_project(self, body: dict[str, Any]) -> None:
        project_id = project_id_for(Path(body["root"]).resolve())
        with SCANNER.project_operation(project_id, "打开项目"):
            project = MANAGER.open(body["root"], body.get("cache_root"))
        SIMILARITY_GROUPS.invalidate(project.project_id)
        self._send_json(project_summary(project.project_id))

    def api_project_cache(self, body: dict[str, Any]) -> None:
        project_id = body["project_id"]
        with SCANNER.project_operation(project_id, "迁移缓存"):
            result = MANAGER.migrate_cache(project_id, body["cache_root"])
        SIMILARITY_GROUPS.invalidate(project_id)
        self._send_json(result)

    def api_cache_cleanup(self, body: dict[str, Any]) -> None:
        project_id = body["project_id"]
        with SCANNER.project_operation(project_id, "清理缓存"):
            result = MANAGER.cleanup_old_cache(project_id, body["path"])
        self._send_json(result)

    def api_project_open_folder(self, body: dict[str, Any]) -> None:
        root = MANAGER.project_root(body["project_id"])
        if not hasattr(os, "startfile"):
            raise ValueError("当前系统不支持文件管理器操作")
        os.startfile(str(root))
        self._send_json({"opened": True})

    def api_project_remove_recent(self, body: dict[str, Any]) -> None:
        project_id = body["project_id"]
        delete_cache = bool(body.get("delete_cache", False))
        if delete_cache:
            with SCANNER.project_operation(project_id, "删除项目缓存"):
                result = MANAGER.remove_from_recent(project_id, True)
        else:
            result = MANAGER.remove_from_recent(project_id, False)
        SIMILARITY_GROUPS.invalidate(project_id)
        self._send_json(result)

    def api_open_github(self, body: dict[str, Any]) -> None:
        webbrowser.open("https://github.com/Yuumi0221/Cullumi")
        self._send_json({"opened": True})

    def api_update_check(self, body: dict[str, Any]) -> None:
        self._send_json(check_for_update(__version__))

    def api_update_download(self, body: dict[str, Any]) -> None:
        update = check_for_update(__version__)
        if not update["update_available"]:
            raise ValueError("当前已经是最新版本")
        if not update["download_available"]:
            raise ValueError("最新版本没有可下载的 Windows 附件，请前往发布页查看")
        path = download_release_asset(update["download_url"], update["asset_name"])
        self._send_json({"downloaded": True, "path": str(path), "version": update["latest_version"]})

    def api_update_open(self, body: dict[str, Any]) -> None:
        webbrowser.open(RELEASES_PAGE_URL)
        self._send_json({"opened": True})

    def api_scan(self, body: dict[str, Any]) -> None:
        started = SCANNER.start(body["project_id"])
        self._send_json({"started": started})

    def api_scan_cancel(self, body: dict[str, Any]) -> None:
        SCANNER.cancel(body["project_id"])
        self._send_json({"cancelled": True})

    def api_decision(self, body: dict[str, Any]) -> None:
        project = MANAGER.from_id(body["project_id"])
        decision = body.get("decision", "")
        if decision not in {"", "keep", "remove"}:
            raise ValueError("无效决定")
        with closing(connect_db(project.db_path)) as conn:
            conn.execute("UPDATE photos SET decision=? WHERE id=?", (decision, int(body["photo_id"])))
            conn.commit()
        self._send_json({"saved": True})

    def api_decision_clear(self, body: dict[str, Any]) -> None:
        project = MANAGER.from_id(body["project_id"])
        self._send_json({"cleared": clear_decisions(project)})

    def api_decision_ai_remove(self, body: dict[str, Any]) -> None:
        project = MANAGER.from_id(body["project_id"])
        self._send_json({"marked": mark_ai_remove_suggestions(project)})

    def api_settings(self, body: dict[str, Any]) -> None:
        updates: dict[str, Any] = {}
        if "theme" in body:
            theme = str(body["theme"])
            if theme not in {"day", "night"}:
                raise ValueError("主题必须为 day 或 night")
            updates["theme"] = theme
        for key in ("auto_advance", "auto_check_updates"):
            if key in body:
                if not isinstance(body[key], bool):
                    raise ValueError(f"{key} 必须为布尔值")
                updates[key] = body[key]
        cache_path = None
        if "default_cache_root" in body:
            raw_path = body["default_cache_root"]
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise ValueError("默认缓存位置不能为空")
            cache_path = Path(raw_path).resolve()
            updates["default_cache_root"] = str(cache_path)
        if cache_path:
            cache_path.mkdir(parents=True, exist_ok=True)
        with CONFIG.edit() as data:
            data.update(updates)
        self._send_json({"saved": True, "settings": CONFIG.data})

    def api_profile_save(self, body: dict[str, Any]) -> None:
        self._send_json(CONFIG.save_custom_profile(body["profile"]))

    def api_profile_delete(self, body: dict[str, Any]) -> None:
        CONFIG.delete_custom_profile(body["profile_id"])
        self._send_json({"deleted": True})

    def api_profile_apply(self, body: dict[str, Any]) -> None:
        project = MANAGER.from_id(body["project_id"])
        profile_id = body["profile_id"]
        profiles = CONFIG.profiles()
        if profile_id not in profiles:
            raise ValueError("筛选模式不存在")
        profile = profiles[profile_id]
        with SCANNER.project_operation(project.project_id, "应用筛选模式"):
            with CONFIG.lock:
                old_profile_id = CONFIG.data["projects"][project.project_id].get(
                    "profile_id", "conservative"
                )
            with closing(connect_db(project.db_path)) as conn:
                config_saved = False
                try:
                    conn.execute(
                        "UPDATE project SET profile_id=?,updated_at=datetime('now') WHERE id=1",
                        (profile_id,),
                    )
                    SCANNER.reclassify(project, conn, profile, commit=False)
                    SCANNER.rebuild_similarity(project, conn, profile, commit=False)
                    with CONFIG.edit() as data:
                        data["projects"][project.project_id]["profile_id"] = profile_id
                    config_saved = True
                    conn.commit()
                except Exception:
                    conn.rollback()
                    if config_saved:
                        with CONFIG.edit() as data:
                            data["projects"][project.project_id]["profile_id"] = old_profile_id
                    raise
        SIMILARITY_GROUPS.invalidate(project.project_id)
        self._send_json(project_summary(project.project_id))

    def api_profile_estimate(self, body: dict[str, Any]) -> None:
        profile = body["profile"]
        validate_profile(profile)
        project = MANAGER.from_id(body["project_id"])
        with closing(connect_db(project.db_path)) as conn:
            rows = conn.execute("SELECT * FROM photos WHERE status='active'").fetchall()
            percentiles = classification_percentiles(rows, profile)
            counts = {"remove": 0, "review": 0, "keep": 0, "unreadable": 0}
            for row in rows:
                suggestion, _ = classify(row, profile, percentiles)
                counts[suggestion] = counts.get(suggestion, 0) + 1
            with closing(sqlite3.connect(":memory:")) as estimate_conn:
                estimate_conn.row_factory = sqlite3.Row
                conn.backup(estimate_conn)
                SCANNER.rebuild_similarity(project, estimate_conn, profile)
                estimated_pairs = estimate_conn.execute("SELECT COUNT(*) FROM similar_pairs").fetchone()[0]
                estimated_groups = len(build_similarity_groups(estimate_conn, profile))
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
        project_id = body["project_id"]
        with SCANNER.project_operation(project_id, "隔离照片"):
            result = apply_quarantine(MANAGER.from_id(project_id))
        SIMILARITY_GROUPS.invalidate(project_id)
        self._send_json(result)

    def api_restore(self, body: dict[str, Any]) -> None:
        project_id = body["project_id"]
        with SCANNER.project_operation(project_id, "恢复照片"):
            result = restore_batch(MANAGER.from_id(project_id), body["batch_id"])
        SIMILARITY_GROUPS.invalidate(project_id)
        self._send_json(result)


def run() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/?token={TOKEN}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import webview

        window = webview.create_window("Cullumi", url, width=1460, height=940, min_size=(980, 680))
        webview.start(apply_native_window_icon, (window,), icon=str(APP_ICON))
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
