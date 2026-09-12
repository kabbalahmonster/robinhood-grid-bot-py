#!/usr/bin/env python3
"""Safely combine each selected fleet bot's newest log."""

import argparse
import heapq
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?(?:Z|[+-]\d{2}:?\d{2})?)")
AGE = re.compile(r"^([1-9][0-9]*)(min|m|h|d|w)$", re.I)
SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:PRIVATE_KEY|API_KEY|API_TOKEN|ACCESS_TOKEN|SECRET|PASSWORD|AUTHORIZATION)[A-Z0-9_]*)\s*([=:])\s*([^\s,;]+)"
)
BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
URL_SECRET = re.compile(r"(?i)([?&](?:api[_-]?key|token|secret|auth)=)[^&\s]+")
URL_PATH_SECRET = re.compile(r"(?i)(https?://[^\s/]+/(?:v2|v3)/)[^/?#\s]+")
HEX_PRIVATE = re.compile(r"(?<![0-9A-Fa-f])(?:0x)?[0-9A-Fa-f]{64}(?![0-9A-Fa-f])")


def parse_age(raw):
    match = AGE.fullmatch(raw.strip())
    if not match:
        raise argparse.ArgumentTypeError("use 30min, 6h, 2d, or 1w")
    count = int(match.group(1))
    unit = match.group(2).lower()
    seconds = count * {"m": 60, "min": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    return timedelta(seconds=seconds)


def redact(text):
    text = SENSITIVE_ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
    text = BEARER.sub(lambda m: f"{m.group(1)} [REDACTED]", text)
    text = URL_SECRET.sub(lambda m: f"{m.group(1)}[REDACTED]", text)
    text = URL_PATH_SECRET.sub(lambda m: f"{m.group(1)}[REDACTED]", text)
    # Preserve public transaction hashes when clearly labelled as tx/hash.
    return HEX_PRIVATE.sub(
        lambda m: m.group(0) if re.search(r"(?i)(?:tx|hash)[^\n]{0,8}$", text[:m.start()]) else "[REDACTED_32_BYTE_HEX]",
        text,
    )


def newest_log(bot_dir):
    root = Path(bot_dir).resolve()
    logs = root / "logs"
    try:
        info = logs.lstat()
    except FileNotFoundError:
        return None, "logs directory missing"
    except OSError as exc:
        return None, f"cannot inspect logs directory: {exc}"
    if logs.is_symlink() or not stat.S_ISDIR(info.st_mode):
        return None, "logs path is not a real directory"
    candidates = []
    try:
        paths = list(logs.iterdir())
    except OSError as exc:
        return None, f"cannot list logs directory: {exc}"
    for path in paths:
        try:
            item = path.lstat()
        except OSError:
            continue
        if (stat.S_ISREG(item.st_mode) and not path.is_symlink()
                and (path.name.endswith(".log") or ".log." in path.name)):
            candidates.append((item.st_mtime_ns, path.name, path, item))
    if not candidates:
        return None, "no regular log files found"
    return max(candidates, key=lambda row: (row[0], row[1])), None


def timestamp(line, fallback):
    match = STAMP.match(line)
    if not match:
        return None
    raw = match.group(1).replace(",", ".")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def records(path, file_time, max_lines, cutoff):
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        lines = handle.readlines()
    if max_lines and len(lines) > max_lines:
        lines = lines[-max_lines:]
    result = []
    current = None
    sequence = 0
    for line in lines:
        line = line.rstrip("\r\n")
        parsed = timestamp(line, file_time)
        if parsed is not None:
            if current is not None:
                result.append(current)
            current = [parsed, sequence, [line]]
            sequence += 1
        elif current is not None:
            current[2].append(line)
        else:
            current = [file_time, sequence, [line]]
            sequence += 1
    if current is not None:
        result.append(current)
    return [item for item in result if cutoff is None or item[0] >= cutoff]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--since", type=parse_age)
    parser.add_argument("--max-lines-per-bot", type=int)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-redact", action="store_true")
    parser.add_argument("targets", nargs="+")
    args = parser.parse_args()
    if len(args.targets) % 2:
        parser.error("targets must be NAME PATH pairs")
    output = Path(args.output).expanduser().resolve()
    if output.exists() and not args.force:
        raise SystemExit(f"refusing to replace existing output without --force: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    generated = datetime.now(timezone.utc)
    cutoff = generated - args.since if args.since else None
    manifest, merged, failures = [], [], 0
    for order in range(0, len(args.targets), 2):
        name, bot_dir = args.targets[order:order + 2]
        selected, error = newest_log(bot_dir)
        if error:
            failures += 1
            manifest.append((name, None, error, 0, None))
            continue
        mtime_ns, filename, path, original = selected
        try:
            current = path.lstat()
            if (path.is_symlink() or not stat.S_ISREG(current.st_mode)
                    or (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino)):
                raise OSError("file changed after selection")
            file_time = datetime.fromtimestamp(current.st_mtime, timezone.utc)
            items = records(path, file_time, args.max_lines_per_bot, cutoff)
        except OSError as exc:
            failures += 1
            manifest.append((name, filename, str(exc), 0, None))
            continue
        manifest.append((name, filename, "ok", len(items), current.st_size))
        for item in items:
            merged.append((item[0], name.casefold(), item[1], name, filename, item[2]))
    merged.sort(key=lambda item: (item[0], item[1], item[2]))
    fd, temporary = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("# RH GRID FLEET LOG BUNDLE\n")
            handle.write(f"# generated_utc: {generated.isoformat()}\n")
            handle.write(f"# redaction: {'disabled' if args.no_redact else 'enabled'}\n")
            handle.write(f"# cutoff_utc: {cutoff.isoformat() if cutoff else 'none'}\n")
            handle.write(f"# selected_bots: {len(manifest)}\n")
            handle.write("# manifest:\n")
            for name, filename, state, count, size in manifest:
                handle.write(f"#   {name}: source={filename or '-'} bytes={size if size is not None else '-'} records={count} status={state}\n")
            handle.write("#\n")
            for at, _, _, name, filename, lines in merged:
                body = "\n".join(lines)
                if not args.no_redact:
                    body = redact(body)
                body_lines = body.split("\n")
                handle.write(f"[{at.isoformat()}] [{name}] [{filename}] {body_lines[0]}\n")
                for continuation in body_lines[1:]:
                    handle.write(f"[CONT] [{name}] [{filename}] {continuation}\n")
            handle.flush()
            os.fsync(handle.fileno())
        if output.exists() and not args.force:
            raise FileExistsError(output)
        os.replace(temporary, output)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    print(f"Wrote {output} ({len(merged)} records from {len(manifest) - failures}/{len(manifest)} bots; {failures} failed)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
