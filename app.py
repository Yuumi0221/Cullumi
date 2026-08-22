from __future__ import annotations

import secrets
import sys
import threading
import traceback
import webbrowser
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from cullumi import http_api
from cullumi.config import ConfigStore, app_data_dir
from cullumi.project_store import ProjectManager
from cullumi.scanner import Scanner
from cullumi.similarity import SimilarityGroupCache


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


CONFIG = ConfigStore()
MANAGER = ProjectManager(CONFIG)
SIMILARITY_GROUPS = SimilarityGroupCache()
SCANNER = Scanner(CONFIG, MANAGER, SIMILARITY_GROUPS)
TOKEN = secrets.token_urlsafe(24)
WEB_ROOT = resource_path("web")
APP_ICON = WEB_ROOT / "assets" / "icons" / "brand-icon.ico"
APPLICATION = http_api.ApplicationContext(
    CONFIG, MANAGER, SCANNER, SIMILARITY_GROUPS, TOKEN, WEB_ROOT
)
http_api.configure(APPLICATION)


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


def run() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), http_api.Handler)
    server.application = APPLICATION
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/?token={TOKEN}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import webview

        window = webview.create_window(
            "Cullumi", url, width=1460, height=940, min_size=(980, 680)
        )
        webview.start(apply_native_window_icon, (window,), icon=str(APP_ICON))
    except Exception:
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
