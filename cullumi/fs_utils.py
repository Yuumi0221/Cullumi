from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def is_within(path: Path, root: Path) -> bool:
    """Return whether ``path`` resolves to ``root`` or one of its descendants."""
    resolved_path = path.resolve()
    resolved_root = root.resolve()
    return resolved_path == resolved_root or resolved_root in resolved_path.parents


def atomic_write_json(path: Path, value: Any) -> None:
    """Replace a JSON file only after the complete payload is on disk."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)
