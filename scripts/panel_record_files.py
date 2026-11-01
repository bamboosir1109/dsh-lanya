"""List actual works beneath the fixed made root, without reading content."""
import os
from pathlib import Path
import stat
from urllib.parse import quote

_ENTRY_EXTENSIONS = frozenset((".html", ".htm", ".png", ".jpg", ".jpeg", ".gif",
                               ".webp", ".bmp", ".svg", ".pdf", ".mp4", ".webm",
                               ".mp3", ".ogg", ".wav"))
_INTERNAL_DIRECTORIES = frozenset((
    "node_modules", "__pycache__", "cache", "caches", "code cache", "gpucache",
    "shadercache", "grshadercache", "dawncache", "extensions", "local storage",
    "session storage", "service worker", "archive", "archives", "_archive",
    "_uitest", "_effect", "_videoframes"))


def _internal_directory(folder):
    """Prune tooling/profile trees before they consume the traversal budget."""
    name = folder.name.casefold()
    if name in _INTERNAL_DIRECTORIES or name in ("_edge", "_edge2") or name.startswith("_edge-"):
        return True
    # Browser profiles can have arbitrary generated names. Detect their layout,
    # rather than suppressing legitimate games with names such as "profile".
    default = folder / "Default"
    return ((folder / "Local State").is_file() and default.is_dir()) or (
        (default / "Preferences").is_file() and any(
            (default / child).is_dir() for child in ("Cache", "Extensions", "Local Storage")))


def _ordinary(path, directory=False):
    path = Path(path)
    for item in (*reversed(path.parents), path):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("WorksPathIsLinked")
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or (not directory and info.st_nlink != 1):
        raise ValueError("WorksPathIsNotOrdinary")
    return info


def works_snapshot(root, excluded=None, max_entries=1000, max_nodes=5000, max_depth=8):
    """Preserve flat files; nested applications have one real index entry.

    Only ordinary files under the root are returned. Nested assets belonging
    to an index page are not separate works. No client-supplied path is opened.
    """
    if any(type(value) is not int or value < 1 for value in (max_entries, max_nodes, max_depth)):
        raise ValueError("InvalidWorksBounds")
    root = Path(os.path.abspath(os.fspath(root)))
    _ordinary(root, directory=True)
    excluded = excluded or (lambda _name: False)
    entries = []
    visited = skipped = internal = 0
    truncated = False

    def add(path, entry_type):
        nonlocal skipped, truncated
        if len(entries) >= max_entries:
            truncated = True
            return
        try:
            before = _ordinary(path)
            relative = path.relative_to(root).as_posix()
            if not relative or any(part in ("", ".", "..") or "\\" in part
                                   for part in relative.split("/")):
                raise ValueError("InvalidWorksRelativePath")
            after = path.lstat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise ValueError("WorksEntryChanged")
            entries.append({"name": relative, "size": after.st_size,
                            "mtime": int(after.st_mtime), "entry_type": entry_type,
                            "url": "./" + quote(relative, safe="/")})
        except (OSError, ValueError):
            skipped += 1

    def visit(folder, depth):
        nonlocal visited, skipped, truncated, internal
        if depth > max_depth or len(entries) >= max_entries or visited >= max_nodes:
            truncated = True
            return
        try:
            _ordinary(folder, directory=True)
            if depth and _internal_directory(folder):
                internal += 1
                return
            children = sorted(folder.iterdir(), key=lambda item: item.name.casefold())
        except (OSError, ValueError):
            skipped += 1
            return
        if depth:
            indexes = [item for item in children if item.name.casefold() == "index.html"]
            if len(indexes) == 1:
                add(indexes[0], "directory_entry")
                return
        for child in children:
            visited += 1
            if visited > max_nodes or len(entries) >= max_entries:
                truncated = True
                break
            if child.name.startswith(".") or excluded(child.name):
                continue
            try:
                info = child.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    skipped += 1
                elif stat.S_ISDIR(info.st_mode):
                    visit(child, depth + 1)
                elif stat.S_ISREG(info.st_mode):
                    if depth == 0 or child.suffix.lower() in _ENTRY_EXTENSIONS:
                        add(child, "file")
                else:
                    skipped += 1
            except OSError:
                skipped += 1

    visit(root, 0)
    entries.sort(key=lambda item: (-item["mtime"], item["name"].casefold()))
    return {"ok": True, "files": entries, "truncated": truncated,
            "skipped_unsafe_or_unreadable": skipped, "skipped_internal_directories": internal,
            "source": "made_filesystem"}
