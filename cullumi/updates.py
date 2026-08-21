from __future__ import annotations

import json
import os
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

RELEASES_API_URL = "https://api.github.com/repos/Yuumi0221/Cullumi/releases/latest"
RELEASES_PAGE_URL = "https://github.com/Yuumi0221/Cullumi/releases"
_VERSION_PATTERN = re.compile(r"(?<!\d)(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def version_key(value: str) -> tuple[int, int, int]:
    match = _VERSION_PATTERN.search(str(value))
    if not match:
        return (0, 0, 0)
    return tuple(int(part or 0) for part in match.groups())


def _request(url: str, current_version: str = "") -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"Cullumi/{current_version or 'update'}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def select_release_asset(assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates: list[tuple[int, dict[str, Any]]] = []
    for asset in assets:
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        lowered = name.casefold()
        suffix = Path(name).suffix.casefold()
        if not url or suffix not in {".zip", ".exe", ".msi"}:
            continue
        if any(word in lowered for word in ("source", "源码", "symbols", "debug")):
            continue
        score = {".zip": 30, ".exe": 20, ".msi": 10}[suffix]
        if any(word in lowered for word in ("windows", "win64", "win-x64", "portable", "便携")):
            score += 50
        if "cullumi" in lowered:
            score += 30
        candidates.append((score, asset))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def check_for_update(
    current_version: str,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(_request(RELEASES_API_URL, current_version), timeout=12) as response:
            release = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {
                "current_version": current_version,
                "latest_version": "",
                "update_available": False,
                "download_available": False,
                "release_url": RELEASES_PAGE_URL,
                "no_release": True,
            }
        raise RuntimeError(f"GitHub 返回错误状态 {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise RuntimeError("无法连接 GitHub，请检查网络后重试") from error
    except (ValueError, TypeError, KeyError) as error:
        raise RuntimeError("GitHub 更新信息格式无效") from error

    if not isinstance(release, dict):
        raise RuntimeError("GitHub 更新信息格式无效")
    tag = str(release.get("tag_name") or "")
    latest_version = tag.lstrip("vV")
    asset = select_release_asset(list(release.get("assets") or []))
    update_available = version_key(latest_version) > version_key(current_version)
    return {
        "current_version": current_version,
        "latest_version": latest_version,
        "update_available": update_available,
        "download_available": bool(update_available and asset),
        "release_url": str(release.get("html_url") or RELEASES_PAGE_URL),
        "release_name": str(release.get("name") or tag),
        "asset_name": str(asset.get("name") or "") if asset else "",
        "download_url": str(asset.get("browser_download_url") or "") if asset else "",
        "no_release": False,
    }


def downloads_directory() -> Path:
    if os.name == "nt":
        try:
            import winreg

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            value_name = "{374DE290-123F-4565-9164-39C4925E467B}"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _ = winreg.QueryValueEx(key, value_name)
            return Path(os.path.expandvars(value)).resolve()
        except (OSError, ValueError):
            pass
    return (Path.home() / "Downloads").resolve()


def _unused_download_path(directory: Path, name: str) -> Path:
    safe_name = Path(name).name.strip() or "Cullumi-update.zip"
    candidate = directory / safe_name
    for index in range(1, 1000):
        if not candidate.exists():
            return candidate
        candidate = directory / f"{Path(safe_name).stem} ({index}){Path(safe_name).suffix}"
    raise RuntimeError("Downloads 文件夹中同名更新文件过多")


def download_release_asset(
    download_url: str,
    asset_name: str,
    destination: Path | None = None,
    opener: Callable[..., Any] | None = None,
) -> Path:
    parsed = urllib.parse.urlparse(download_url)
    if parsed.scheme != "https" or parsed.hostname not in {"github.com", "www.github.com"}:
        raise ValueError("更新下载地址不是受信任的 GitHub 地址")
    directory = (destination or downloads_directory()).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    target = _unused_download_path(directory, asset_name)
    temp = target.with_name(target.name + ".part")
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(_request(download_url), timeout=30) as response, temp.open("wb") as handle:
            shutil.copyfileobj(response, handle, length=1024 * 1024)
        if not temp.is_file() or temp.stat().st_size == 0:
            raise RuntimeError("下载的更新文件为空")
        temp.replace(target)
        return target
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        temp.unlink(missing_ok=True)
        raise RuntimeError("更新下载失败，请检查网络和 Downloads 文件夹权限") from error
    except Exception:
        temp.unlink(missing_ok=True)
        raise
