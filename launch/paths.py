"""Local path selection for project inputs and generated files."""
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def has_case_data(folder: Path) -> bool:
    """Return True when a folder has at least one recognizable object input."""
    return any(
        any((object_dir / name).is_file() for name in ("object.json", "invoice.csv", "telemetry.csv"))
        for object_dir in object_folders(folder)
    )


def object_folders(folder: Path) -> list[Path]:
    """Discover BuildingN folders, including incomplete objects that need a warning."""
    if not folder.is_dir():
        return []
    candidates = [
        child for child in folder.iterdir()
        if child.is_dir() and re.fullmatch(r"Building\d+", child.name, flags=re.IGNORECASE)
    ]
    return sorted(candidates, key=lambda path: (int(re.search(r"\d+", path.name).group()), path.name.casefold()))


def default_object_id(folder: Path) -> str:
    """Return the stable object_id used for a BuildingN folder."""
    match = re.fullmatch(r"Building(\d+)", folder.name, flags=re.IGNORECASE)
    if match:
        return f"building_{int(match.group(1))}"
    return re.sub(r"[^a-z0-9_-]+", "_", folder.name.casefold()).strip("_")


def case_data_dir(root: Path = ROOT) -> Path:
    """Use an explicit path first, then the project data folder, then Downloads/data."""
    configured = os.environ.get("LAUNCH_DATA_DIR")
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else (root / path).resolve()

    candidates = (root / "data", Path.home() / "Downloads" / "data")
    for candidate in candidates:
        if has_case_data(candidate):
            return candidate.resolve()
    return (root / "data").resolve()
