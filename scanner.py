\
from __future__ import annotations
import os
import re
import sys
import time
import stat
import tarfile
import zipfile
from dataclasses import dataclass
from typing import Iterator, List, Optional, Tuple, Dict, Set

# Known archive extensions. We detect by normalized lower-case file name.
# We separate "supported to open" from "known by extension".
SUPPORTED_ARCHIVES = {
    "zip",
    "tar",
    "tar.gz", "tgz",
    "tar.bz2", "tbz2",
    "tar.xz", "txz",
}

KNOWN_ARCHIVES = SUPPORTED_ARCHIVES | {
    "gz", "bz2", "xz", "z", "7z", "rar", "tar.zst", "zst", "tar.lz4", "lz4", "tar.lz", "lz",
}

# Regex to capture compound extensions like .tar.gz, .tar.bz2, .tar.xz, .tar.zst, etc.
COMPOUND_EXT_RE = re.compile(r"\.(tar\.(?:gz|bz2|xz|zst)|tar)$", re.IGNORECASE)

def normalize_ext(name: str) -> str:
    n = name.lower()
    m = COMPOUND_EXT_RE.search(n)
    if m:
        return m.group(1)
    # handle tgz, tbz2, txz
    if n.endswith(".tgz"):
        return "tgz"
    if n.endswith(".tbz2"):
        return "tbz2"
    if n.endswith(".txz"):
        return "txz"
    # simple extension after last dot
    if "." in n:
        return n.rsplit(".", 1)[1]
    return ""

@dataclass
class Finding:
    """Represents a found archive or nested archive indicator."""
    path: str                    # Real file path OR virtual path like "/outer.zip::/inner.tar.gz"
    ext: str                     # normalized extension key
    size: int                    # size in bytes if available
    depth: int                   # 0 = filesystem, 1+ = inside archives
    open_supported: bool         # True if our tool can open to inspect recursively
    container: Optional[str] = None  # container virtual path if nested

def is_probably_archive(name: str) -> Tuple[bool, str]:
    ext = normalize_ext(name)
    if ext in KNOWN_ARCHIVES:
        return True, ext
    return False, ext

def _iter_files(root: str) -> Iterator[Tuple[str, os.DirEntry]]:
    """Yield file system entries depth-first. Avoids following symlinks to prevent loops."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Skip hidden directories like .git? We keep them.
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            try:
                with os.scandir(dirpath) as it:
                    pass  # no-op; just to hint permission errors handled by os.walk already
            except Exception:
                pass
        with os.scandir(dirpath) as it:
            for entry in it:
                if entry.is_file(follow_symlinks=False):
                    yield dirpath, entry

def _safe_stat(path: str) -> int:
    try:
        st = os.stat(path, follow_symlinks=False)
        return int(st.st_size)
    except Exception:
        return -1

def scan_directory(path: str, recurse_archives: bool = False, max_depth: int = 3) -> List[Finding]:
    """
    Scan the directory for archives. If recurse_archives is True, open supported archives and
    inspect their member names for nested archives, up to max_depth levels.
    """
    findings: List[Finding] = []

    def add_finding(path: str, ext: str, size: int, depth: int, open_supported: bool, container: Optional[str] = None):
        findings.append(Finding(path=path, ext=ext, size=size, depth=depth, open_supported=open_supported, container=container))

    # First pass: filesystem
    for dirpath, entry in _iter_files(path):
        name = entry.name
        fpath = os.path.join(dirpath, name)
        is_arch, ext = is_probably_archive(name)
        if is_arch:
            size = _safe_stat(fpath)
            add_finding(fpath, ext, size, depth=0, open_supported=(ext in SUPPORTED_ARCHIVES))
            if recurse_archives and ext in SUPPORTED_ARCHIVES:
                _scan_inside_archive(fpath, ext, add_finding, max_depth=max_depth)

    return findings

def _scan_inside_archive(fpath: str, ext: str, add_finding, max_depth: int, depth: int = 1, parent_virtual: Optional[str] = None):
    """Inspect archive contents and add findings for nested archives by member names. Recursive up to max_depth."""
    if depth > max_depth:
        return
    virt_prefix = (parent_virtual + "::" if parent_virtual else "") + fpath

    # Zip files
    if ext == "zip":
        try:
            with zipfile.ZipFile(fpath) as zf:
                for zi in zf.infolist():
                    if zi.is_dir():
                        continue
                    name = zi.filename
                    is_arch, ext2 = is_probably_archive(name)
                    if is_arch:
                        vpath = f"{virt_prefix}::/{name}"
                        add_finding(vpath, ext2, zi.file_size, depth, open_supported=(ext2 in SUPPORTED_ARCHIVES), container=fpath)
                        # Recursive deep inspection requires loading nested archive. Avoid for large members.
                        if ext2 in {"zip", "tar", "tar.gz", "tgz", "tar.bz2", "tbz2", "tar.xz", "txz"}:
                            # Read only small members to avoid RAM blowups
                            if zi.file_size <= 64 * 1024 * 1024 and depth < max_depth:
                                try:
                                    data = zf.read(zi)
                                    _scan_bytes_archive(data, ext2, add_finding, max_depth, depth+1, parent_virtual=vpath)
                                except Exception:
                                    pass
        except Exception:
            return

    # Tar family
    elif ext in {"tar", "tar.gz", "tgz", "tar.bz2", "tbz2", "tar.xz", "txz"}:
        mode = "r"
        if ext in {"tar.gz", "tgz"}:
            mode = "r:gz"
        elif ext in {"tar.bz2", "tbz2"}:
            mode = "r:bz2"
        elif ext in {"tar.xz", "txz"}:
            mode = "r:xz"
        try:
            with tarfile.open(fpath, mode) as tf:
                for ti in tf:
                    if not ti.isreg():
                        continue
                    name = ti.name
                    is_arch, ext2 = is_probably_archive(name)
                    if is_arch:
                        vpath = f"{virt_prefix}::/{name}"
                        add_finding(vpath, ext2, ti.size, depth, open_supported=(ext2 in SUPPORTED_ARCHIVES), container=fpath)
                        if ext2 in {"zip", "tar", "tar.gz", "tgz", "tar.bz2", "tbz2", "tar.xz", "txz"} and depth < max_depth and ti.size <= 64*1024*1024:
                            try:
                                fobj = tf.extractfile(ti)
                                if fobj is not None:
                                    data = fobj.read()
                                    _scan_bytes_archive(data, ext2, add_finding, max_depth, depth+1, parent_virtual=vpath)
                            except Exception:
                                pass
        except Exception:
            return

def _scan_bytes_archive(data: bytes, ext: str, add_finding, max_depth: int, depth: int, parent_virtual: Optional[str] = None):
    """Open an in-memory archive from bytes to inspect its members by name only."""
    import io
    if ext == "zip":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for zi in zf.infolist():
                    if zi.is_dir():
                        continue
                    name = zi.filename
                    is_arch, ext2 = is_probably_archive(name)
                    if is_arch:
                        vpath = f"{parent_virtual}::/{name}"
                        add_finding(vpath, ext2, zi.file_size, depth, open_supported=(ext2 in SUPPORTED_ARCHIVES), container=parent_virtual)
        except Exception:
            return
    else:
        mode = "r"
        if ext in {"tar.gz", "tgz"}:
            mode = "r:gz"
        elif ext in {"tar.bz2", "tbz2"}:
            mode = "r:bz2"
        elif ext in {"tar.xz", "txz"}:
            mode = "r:xz"
        try:
            import io, tarfile
            with tarfile.open(fileobj=io.BytesIO(data), mode=mode) as tf:
                for ti in tf:
                    if not ti.isreg():
                        continue
                    name = ti.name
                    is_arch, ext2 = is_probably_archive(name)
                    if is_arch:
                        vpath = f"{parent_virtual}::/{name}"
                        add_finding(vpath, ext2, ti.size, depth, open_supported=(ext2 in SUPPORTED_ARCHIVES), container=parent_virtual)
        except Exception:
            return
