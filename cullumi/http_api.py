from __future__ import annotations

import json
import mimetypes
import os
import secrets
import sqlite3
import stat
import urllib.parse
import webbrowser
from contextlib import closing
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

from . import __version__
from .classification import (
    PHOTO_AI_FILTERS,
    PHOTO_DECISION_FILTERS,
    classification_percentiles,
    classify,
    parse_photo_filter,
    photo_filter_where,
    project_photo_counts,
)
from .config import ConfigStore, validate_profile
from .media import DISPLAY_PREVIEW_EXTENSIONS, ensure_display_preview
from .native_dialogs import choose_csv, choose_directory, choose_save_csv
from .project_store import (
    ProjectManager,
    connect_db,
    project_id_for,
    project_thumbnail_path,
    safe_relative_path,
)
from .scanner import Scanner
from .similarity import SimilarityGroupCache, build_similarity_groups
from .updates import RELEASES_PAGE_URL, check_for_update, download_release_asset
from .workflows import (
    apply_quarantine,
    clear_decisions,
    export_decisions,
    import_decisions,
    mark_ai_remove_suggestions,
    quarantine_preview,
    restore_batch,
)

CONFIG: ConfigStore
MANAGER: ProjectManager
SCANNER: Scanner
SIMILARITY_GROUPS: SimilarityGroupCache
TOKEN = secrets.token_urlsafe(24)
WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
FILE_RESPONSE_CHUNK_SIZE = 256 * 1024
GET_ROUTES = {
    "/api/bootstrap": "api_bootstrap",
    "/api/recent-project": "api_recent_project",
    "/api/project": "api_project",
    "/api/progress": "api_progress",
    "/api/photos": "api_photos",
    "/api/similar-groups": "api_similar_groups",
    "/api/similar-group": "api_similar_group",
    "/api/thumb": "api_thumb",
    "/api/photo": "api_photo",
    "/api/quarantine/preview": "api_quarantine_preview",
    "/api/quarantine/batches": "api_batches",
}
POST_ROUTES = {
    "/api/choose-folder": "api_choose_folder",
    "/api/choose-cache": "api_choose_cache",
    "/api/choose-csv": "api_choose_csv",
    "/api/project/open": "api_open_project",
    "/api/project/cache": "api_project_cache",
    "/api/project/cache/cleanup": "api_cache_cleanup",
    "/api/project/open-folder": "api_project_open_folder",
    "/api/project/remove-recent": "api_project_remove_recent",
    "/api/open-github": "api_open_github",
    "/api/update/check": "api_update_check",
    "/api/update/download": "api_update_download",
    "/api/update/open": "api_update_open",
    "/api/scan": "api_scan",
    "/api/scan/cancel": "api_scan_cancel",
    "/api/decision": "api_decision",
    "/api/decision/ai-remove": "api_decision_ai_remove",
    "/api/decision/clear": "api_decision_clear",
    "/api/settings": "api_settings",
    "/api/profile/save": "api_profile_save",
    "/api/profile/delete": "api_profile_delete",
    "/api/profile/apply": "api_profile_apply",
    "/api/profile/estimate": "api_profile_estimate",
    "/api/export/save": "api_export_save",
    "/api/import": "api_import",
    "/api/quarantine/apply": "api_quarantine_apply",
    "/api/quarantine/restore": "api_restore",
}


def configure(
    config: ConfigStore,
    manager: ProjectManager,
    scanner: Scanner,
    similarity_groups: SimilarityGroupCache,
    token: str,
    web_root: Path,
) -> None:
    global CONFIG, MANAGER, SCANNER, SIMILARITY_GROUPS, TOKEN, WEB_ROOT
    CONFIG = config
    MANAGER = manager
    SCANNER = scanner
    SIMILARITY_GROUPS = similarity_groups
    TOKEN = token
    WEB_ROOT = web_root


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
        photo_counts = project_photo_counts(conn)
        pairs = conn.execute("SELECT COUNT(*) FROM similar_pairs").fetchone()[0]
        profile = CONFIG.get_profile(project.profile_id)
        similar_groups = SIMILARITY_GROUPS.count(project_id, conn, profile)
    return {
        "id": project_id,
        "root": str(project.root),
        "cache_root": str(project.cache_root),
        "profile_id": project.profile_id,
        **photo_counts,
        "pairs": pairs,
        "similar_groups": similar_groups,
    }


def recent_project_stub(project_id: str, stored: dict[str, Any]) -> dict[str, Any]:
    available = Path(stored["root"]).is_dir()
    return {
        "id": project_id,
        **stored,
        "available": available,
        "total": 0,
        "kept": 0,
        "thumbnail_url": "",
        "stats_loaded": not available,
        "load_error": "",
    }


def recent_project_payload(project_id: str, stored: dict[str, Any]) -> dict[str, Any]:
    payload = recent_project_stub(project_id, stored)
    available = payload["available"]
    if not available:
        return payload
    conn = None
    try:
        project = MANAGER.from_id(project_id)
        if not project.db_path.is_file():
            payload["stats_loaded"] = True
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
        if cover and project_thumbnail_path(project, cover["thumbnail"]).is_file():
            query = urllib.parse.urlencode({
                "project_id": project_id,
                "id": cover["id"],
                "token": TOKEN,
            })
            payload["thumbnail_url"] = f"/api/thumb?{query}"
    except Exception as error:
        payload["load_error"] = str(error) or error.__class__.__name__
    finally:
        if conn is not None:
            conn.close()
    payload["stats_loaded"] = True
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
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("请求正文必须是 JSON 对象")
        return value

    def _dispatch(self, routes: dict[str, str], body: dict[str, Any] | None = None) -> None:
        method_name = routes.get(self._parsed().path)
        if method_name is None:
            self.send_error(404)
            return
        method = getattr(self, method_name)
        if body is None:
            method()
        else:
            method(body)

    def _handle_error(self, error: Exception) -> None:
        status = 400 if isinstance(error, (ValueError, KeyError, TypeError, json.JSONDecodeError)) else 500
        message = str(error)
        if isinstance(error, KeyError):
            message = f"缺少请求字段：{error.args[0]}"
        self._send_json({"error": message}, status)

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
            self._dispatch(GET_ROUTES)
        except Exception as error:
            self._handle_error(error)

    def do_POST(self) -> None:
        if not self._authorized():
            self._send_json({"error": "unauthorized"}, 403)
            return
        try:
            self._dispatch(POST_ROUTES, self._body())
        except Exception as error:
            self._handle_error(error)

    def api_bootstrap(self) -> None:
        config_data = CONFIG.snapshot()
        recent = []
        for pid in config_data.get("recent_projects", []):
            project = config_data.get("projects", {}).get(pid)
            if project:
                recent.append(recent_project_stub(pid, project))
        self._send_json({
            "version": __version__,
            "startup_warning": CONFIG.load_warning,
            "settings": {
                "default_cache_root": config_data["default_cache_root"],
                "auto_advance": config_data.get("auto_advance", True),
                "auto_check_updates": config_data.get("auto_check_updates", True),
                "theme": config_data.get("theme", "day"),
            },
            "recent_projects": recent,
            "profiles": list(CONFIG.profiles().values()),
        })

    def api_recent_project(self) -> None:
        project_id = self._query().get("project_id", [""])[0]
        stored = CONFIG.snapshot().get("projects", {}).get(project_id)
        if not stored:
            raise ValueError("项目不存在")
        self._send_json(recent_project_payload(project_id, stored))

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
        project, row = self._photo_row()
        self._send_file(project_thumbnail_path(project, row["thumbnail"]), "image/jpeg")

    def api_photo(self) -> None:
        project, row = self._photo_row()
        path = safe_relative_path(project.root, row["relative_path"], "照片路径")
        if path.suffix.lower() not in DISPLAY_PREVIEW_EXTENSIONS:
            self._send_file(path)
            return
        thumbnail = project_thumbnail_path(project, row["thumbnail"])
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
        project_id = body["project_id"]
        decision = body.get("decision", "")
        if decision not in {"", "keep", "remove"}:
            raise ValueError("无效决定")
        with MANAGER.data_operation(project_id):
            project = MANAGER.from_id(project_id)
            with closing(connect_db(project.db_path)) as conn:
                photo_id = int(body["photo_id"])
                cursor = conn.execute(
                    "UPDATE photos SET decision=? WHERE id=? AND status='active'",
                    (decision, photo_id),
                )
                if not cursor.rowcount:
                    raise ValueError("照片不存在或当前不可用")
                conn.commit()
                counts = project_photo_counts(conn)
        self._send_json({
            "saved": True,
            "photo_id": photo_id,
            "decision": decision,
            "project_counts": counts,
        })

    def api_decision_clear(self, body: dict[str, Any]) -> None:
        project_id = body["project_id"]
        with MANAGER.data_operation(project_id):
            project = MANAGER.from_id(project_id)
            result = clear_decisions(project)
            with closing(connect_db(project.db_path)) as conn:
                counts = project_photo_counts(conn)
        self._send_json({"cleared": result, "project_counts": counts})

    def api_decision_ai_remove(self, body: dict[str, Any]) -> None:
        project_id = body["project_id"]
        with MANAGER.data_operation(project_id):
            project = MANAGER.from_id(project_id)
            result = mark_ai_remove_suggestions(project)
            with closing(connect_db(project.db_path)) as conn:
                counts = project_photo_counts(conn)
        self._send_json({"marked": result, "project_counts": counts})

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
        self._send_json({"saved": True, "settings": CONFIG.snapshot()})

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
            with MANAGER.data_operation(project.project_id):
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
        project_id = body["project_id"]
        csv_path = Path(body.get("path") or "")
        if not csv_path.exists():
            raise ValueError("CSV 文件不存在")
        with MANAGER.data_operation(project_id):
            project = MANAGER.from_id(project_id)
            result = import_decisions(project, csv_path)
        self._send_json(result)

    def api_quarantine_apply(self, body: dict[str, Any]) -> None:
        project_id = body["project_id"]
        with SCANNER.project_operation(project_id, "隔离照片"):
            with MANAGER.data_operation(project_id):
                result = apply_quarantine(MANAGER.from_id(project_id))
        SIMILARITY_GROUPS.invalidate(project_id)
        self._send_json(result)

    def api_restore(self, body: dict[str, Any]) -> None:
        project_id = body["project_id"]
        with SCANNER.project_operation(project_id, "恢复照片"):
            with MANAGER.data_operation(project_id):
                result = restore_batch(MANAGER.from_id(project_id), body["batch_id"])
        SIMILARITY_GROUPS.invalidate(project_id)
        self._send_json(result)
