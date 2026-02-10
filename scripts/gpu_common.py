from __future__ import annotations

import pathlib
import subprocess
from typing import Any


def tail_lines(text: str, max_lines: int = 120) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def stream_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def git_root(path: pathlib.Path) -> pathlib.Path:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=path,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip() or "not a git repository")
    return pathlib.Path(proc.stdout.strip()).resolve()
