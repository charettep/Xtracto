\
from __future__ import annotations
import argparse
import os
import sys
import time
import shutil
import threading
from typing import List
from .scanner import scan_directory, Finding

PROG = "xtrak"

def human(n: int) -> str:
    if n < 0:
        return "n/a"
    units = ["B","KB","MB","GB","TB"]
    f = float(n)
    i = 0
    while f >= 1024 and i < len(units)-1:
        f /= 1024.0
        i += 1
    if f >= 100 or f.is_integer():
        return f"{int(f)}{units[i]}"
    return f"{f:.1f}{units[i]}"

def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=PROG, description="Scan directories for archive files. Optionally peek inside archives for nested archives.")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("scan", help="Scan a directory for archives")
    sp.add_argument("path", help="Directory to scan")
    sp.add_argument("-R", "--recursive", action="store_true", help="Inspect supported archives' contents for nested archives")
    sp.add_argument("--max-depth", type=int, default=3, help="Maximum nested depth when using -R")
    sp.add_argument("-q", "--quiet", action="store_true", help="Quiet mode. Do not print each scanned file.")
    sp.add_argument("--no-color", action="store_true", help="Disable ANSI control sequences")
    return p

class LiveBar:
    """A simple sticky progress bar drawn one line above the log stream using ANSI codes. No external deps."""
    def __init__(self, enabled: bool = True):
        self.enabled = enabled and sys.stdout.isatty()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._state = {"scanned": 0, "found": 0, "started": time.time()}
        self._thread = None

    def update(self, scanned_delta=0, found_delta=0):
        if not self.enabled:
            return
        with self._lock:
            self._state["scanned"] += scanned_delta
            self._state["found"] += found_delta

    def _render_line(self) -> str:
        with self._lock:
            scanned = self._state["scanned"]
            found = self._state["found"]
            elapsed = max(0.0001, time.time() - self._state["started"])
        rate = scanned / elapsed
        # Simple ASCII bar with spinner
        spinner = "|/-\\"[int(time.time() * 10) % 4]
        termw = shutil.get_terminal_size((80, 20)).columns
        msg = f" {spinner} scanned:{scanned}  archives:{found}  rate:{rate:.1f}/s  elapsed:{int(elapsed)}s "
        # pad and trim
        if len(msg) < termw:
            msg = msg + " " * (termw - len(msg))
        else:
            msg = msg[:termw]
        return msg

    def start(self):
        if not self.enabled:
            return
        # Print one empty line to host the sticky bar
        sys.stdout.write("\n")
        sys.stdout.flush()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            line = self._render_line()
            # Move cursor up one line, write the bar, move back down.
            sys.stdout.write("\x1b[1A" + line + "\x1b[0m" + "\n")
            sys.stdout.flush()
            time.sleep(0.1)

    def stop(self):
        if not self.enabled:
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        # Final render
        line = self._render_line()
        sys.stdout.write("\x1b[1A" + line + "\x1b[0m" + "\n")
        sys.stdout.flush()

def cmd_scan(args):
    path = os.path.abspath(args.path)
    if not os.path.isdir(path):
        print(f"error: not a directory: {path}", file=sys.stderr)
        return 2

    bar = LiveBar(enabled=not args.no_color)
    bar.start()

    try:
        findings: List[Finding] = []
        # Walk file system and track progress
        for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
            for fname in filenames:
                fpath = os.path.join(dirpath, fname)
                bar.update(scanned_delta=1)
                # On each file, check archive
                from .scanner import is_probably_archive, normalize_ext, SUPPORTED_ARCHIVES
                is_arch, ext = is_probably_archive(fname)
                if is_arch:
                    size = -1
                    try:
                        size = os.stat(fpath, follow_symlinks=False).st_size
                    except Exception:
                        pass
                    findings.append(Finding(path=fpath, ext=ext, size=size, depth=0, open_supported=(ext in SUPPORTED_ARCHIVES)))
                    bar.update(found_delta=1)
                    if not args.quiet:
                        print(f"[archive] {fpath}  ({ext}, {human(size)})")
                    # If recursive, inspect inside the archive by names
                    if args.recursive and ext in SUPPORTED_ARCHIVES:
                        from .scanner import _scan_inside_archive
                        def add_finding(p, e, s, d, open_supported, container=None):
                            findings.append(Finding(path=p, ext=e, size=s, depth=d, open_supported=open_supported, container=container))
                            bar.update(found_delta=1)
                            if not args.quiet:
                                print(f"[nested d={d}] {p}  ({e}, {human(s)})")
                        _scan_inside_archive(fpath, ext, add_finding, max_depth=args.max_depth)
                else:
                    if not args.quiet:
                        print(f"        {fpath}")
        # Final summary
        bar.stop()
        print(f"\nSummary: {len(findings)} archives found.")
        # Pretty list
        for f in findings:
            indent = "  " * f.depth
            where = f.path
            support = "open" if f.open_supported else "name-only"
            print(f"{indent}- {where}  [{f.ext}, {human(f.size)}, {support}]")
        return 0
    finally:
        try:
            bar.stop()
        except Exception:
            pass

def main(argv: list[str] | None = None):
    parser = make_parser()
    args = parser.parse_args(argv)
    if args.command == "scan":
        return cmd_scan(args)
    parser.print_help()
    return 1

if __name__ == "__main__":
    raise SystemExit(main())
