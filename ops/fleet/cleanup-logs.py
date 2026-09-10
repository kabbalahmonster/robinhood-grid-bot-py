#!/usr/bin/env python3
"""Plan or safely remove selected fleet bot log files."""

import argparse
import stat
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--older-seconds", type=int, required=True)
    parser.add_argument("--keep-latest", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("targets", nargs="+")
    args = parser.parse_args()
    if len(args.targets) % 2:
        raise SystemExit("invalid name/path target list")
    cutoff = time.time() - args.older_seconds if args.older_seconds >= 0 else None
    total_files = total_bytes = failures = 0
    for index in range(0, len(args.targets), 2):
        name, bot_path = args.targets[index:index + 2]
        log_dir = Path(bot_path).resolve() / "logs"
        files = []
        try:
            log_dir_info = log_dir.lstat()
        except FileNotFoundError:
            log_dir_info = None
        except OSError as exc:
            failures += 1
            print(f"{name}: FAILED to inspect logs directory: {exc}", file=sys.stderr)
            continue
        if log_dir_info is not None and stat.S_ISDIR(log_dir_info.st_mode) and not log_dir.is_symlink():
            for path in log_dir.iterdir():
                try:
                    info = path.lstat()
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode) and not path.is_symlink() and (
                    path.name.endswith(".log") or ".log." in path.name
                ):
                    files.append((path, info))
        elif log_dir_info is not None:
            failures += 1
            print(f"{name}: REFUSED logs path that is not a real directory: {log_dir}", file=sys.stderr)
            continue
        files.sort(key=lambda item: (item[1].st_mtime_ns, item[0].name), reverse=True)
        protected = {path for path, _ in files[:args.keep_latest]}
        selected = [(path, info) for path, info in files
                    if path not in protected and (cutoff is None or info.st_mtime < cutoff)]
        selected_bytes = sum(info.st_size for _, info in selected)
        print(f"{name}: {len(selected)} file(s), {selected_bytes} bytes "
              f"{'selected' if not args.apply else 'to delete'}")
        for path, original in selected:
            print(f"  {'DELETE' if args.apply else 'would delete'} {path.name} ({original.st_size} bytes)")
            if args.apply:
                try:
                    current = path.lstat()
                    if path.is_symlink() or not stat.S_ISREG(current.st_mode):
                        raise OSError("file type changed after planning")
                    if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
                        raise OSError("file changed after planning")
                    path.unlink()
                except OSError as exc:
                    failures += 1
                    print(f"  FAILED {path.name}: {exc}", file=sys.stderr)
                    continue
            total_files += 1
            total_bytes += original.st_size
    verb = "Deleted" if args.apply else "Would delete"
    print(f"\n{verb} {total_files} log file(s), {total_bytes} bytes total; {failures} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
