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
TOURNAMENT_EVENT = re.compile(r"\bRoute tournament (?:candidate|winner)\b", re.I)
TOURNAMENT_CANDIDATE = re.compile(r"\bRoute tournament candidate\b", re.I)
TOURNAMENT_WINNER = re.compile(r"\bRoute tournament winner\b", re.I)
TOURNAMENT_ID = re.compile(r"\btournament_id=([A-Za-z0-9_.:-]{1,128})\b")
ANALYSIS_EVENT = re.compile(
    r"\b(?:Route tournament |Bot runtime provenance\b|Bot cycle performance\b)", re.I
)


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


def tournament_rounds(items):
    """Return correlated events, with a legacy candidate-to-winner fallback."""
    correlated = []
    states = {}
    for item in items:
        body = "\n".join(item[2])
        match = TOURNAMENT_ID.search(body)
        if not match or "Route tournament" not in body:
            continue
        tournament_id = match.group(1)
        correlated.append(item)
        state = states.setdefault(tournament_id, {"winner": False})
        if TOURNAMENT_WINNER.search(body):
            state["winner"] = True
    if correlated:
        complete = sum(state["winner"] for state in states.values())
        incomplete = len(states) - complete
        return correlated, complete, incomplete, "id"

    # Backward compatibility for logs produced before correlation IDs existed.
    selected, current = [], []
    complete = incomplete = 0
    for item in items:
        body = "\n".join(item[2])
        starts = bool(TOURNAMENT_CANDIDATE.search(body))
        ends = bool(TOURNAMENT_WINNER.search(body))
        if current or starts:
            current.append(item)
        elif ends:
            # Defensive support for a winner-only round (for example, zero
            # configured candidates) even though normal rounds log candidates.
            current = [item]
        if ends and current:
            selected.extend(current)
            current = []
            complete += 1
    if current:
        # Keep a truncated/crashed final round: it is valuable failure evidence,
        # and the manifest makes its incomplete state explicit.
        selected.extend(current)
        incomplete = 1
    return selected, complete, incomplete, "legacy_order"


def next_numbered_output(requested):
    """Return the first available NAME-NUMBER.SUFFIX sibling."""
    number = 1
    while True:
        candidate = requested.with_name(
            f"{requested.stem}-{number}{requested.suffix}"
        )
        if not candidate.exists():
            return candidate
        number += 1


def write_record(handle, item, no_redact):
    at, _, _, name, filename, lines = item
    body = "\n".join(lines)
    if not no_redact:
        body = redact(body)
    body_lines = body.split("\n")
    handle.write(f"[{at.isoformat()}] [{name}] [{filename}] {body_lines[0]}\n")
    for continuation in body_lines[1:]:
        handle.write(f"[CONT] [{name}] [{filename}] {continuation}\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--since", type=parse_age)
    parser.add_argument("--max-lines-per-bot", type=int)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-redact", action="store_true")
    parser.add_argument("--tournament-only", action="store_true")
    parser.add_argument("--tournament-rounds-only", action="store_true")
    parser.add_argument("--analysis-sample-only", action="store_true")
    parser.add_argument("--chronological", action="store_true")
    parser.add_argument("targets", nargs="+")
    args = parser.parse_args()
    if args.analysis_sample_only and (args.tournament_only or args.tournament_rounds_only):
        parser.error("--analysis-sample-only cannot be combined with tournament-only modes")
    if len(args.targets) % 2:
        parser.error("targets must be NAME PATH pairs")
    requested_output = Path(args.output).expanduser().resolve()
    requested_output.parent.mkdir(parents=True, exist_ok=True)
    output = requested_output
    if output.exists() and not args.force:
        output = next_numbered_output(requested_output)
        print(f"Output already exists: {requested_output}; writing {output} instead")
    generated = datetime.now(timezone.utc)
    cutoff = generated - args.since if args.since else None
    manifest, merged, failures, skipped = [], [], 0, 0
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
        if args.analysis_sample_only:
            items = [
                item for item in items
                if any(ANALYSIS_EVENT.search(line) for line in item[2])
            ]
            if not items:
                skipped += 1
                manifest.append((name, filename, "skipped: no analysis telemetry in included records", 0, current.st_size))
                continue
            state = "ok: analysis_sample"
        elif args.tournament_rounds_only:
            items, complete_rounds, incomplete_rounds, correlation = tournament_rounds(items)
            if not items:
                skipped += 1
                manifest.append((name, filename, "skipped: no tournament in included records", 0, current.st_size))
                continue
            state = (f"ok: rounds={complete_rounds} incomplete={incomplete_rounds} "
                     f"correlation={correlation}")
        elif args.tournament_only and not any(
                TOURNAMENT_EVENT.search(line) for item in items for line in item[2]):
            skipped += 1
            manifest.append((name, filename, "skipped: no tournament in included records", 0, current.st_size))
            continue
        else:
            state = "ok"
        manifest.append((name, filename, state, len(items), current.st_size))
        for item in items:
            merged.append((item[0], name.casefold(), item[1], name, filename, item[2]))
    merged.sort(key=lambda item: (item[0], item[1], item[2]))
    included = len(manifest) - failures - skipped
    fd, temporary = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("# RH GRID FLEET LOG BUNDLE\n")
            handle.write(f"# generated_utc: {generated.isoformat()}\n")
            handle.write(f"# redaction: {'disabled' if args.no_redact else 'enabled'}\n")
            handle.write(f"# cutoff_utc: {cutoff.isoformat() if cutoff else 'none'}\n")
            handle.write(f"# tournament_only: {'enabled' if args.tournament_only else 'disabled'}\n")
            handle.write(f"# tournament_rounds_only: {'enabled' if args.tournament_rounds_only else 'disabled'}\n")
            handle.write(f"# analysis_sample_only: {'enabled' if args.analysis_sample_only else 'disabled'}\n")
            handle.write(f"# layout: {'chronological' if args.chronological else 'grouped_by_bot'}\n")
            handle.write(f"# selected_bots: {len(manifest)}\n")
            handle.write(f"# included_bots: {included}\n")
            handle.write(f"# skipped_bots: {skipped}\n")
            handle.write(f"# failed_bots: {failures}\n")
            handle.write(f"# total_records: {len(merged)}\n")
            handle.write("# timestamp_timezone: UTC\n")
            handle.write("# continuation_format: [CONT] lines belong to the preceding record\n")
            handle.write("# manifest:\n")
            for name, filename, state, count, size in manifest:
                handle.write(f"#   {name}: source={filename or '-'} bytes={size if size is not None else '-'} records={count} status={state}\n")
            handle.write("#\n")
            if args.chronological:
                handle.write("# === FLEET-WIDE CHRONOLOGICAL RECORDS ===\n")
                for item in merged:
                    write_record(handle, item, args.no_redact)
            else:
                for name, filename, state, count, size in manifest:
                    bot_records = [item for item in merged if item[3] == name]
                    handle.write("#\n# ==============================================================================\n")
                    handle.write(f"# BOT SECTION: {name}\n")
                    handle.write(f"# source_file: {filename or '-'}\n")
                    handle.write(f"# source_bytes: {size if size is not None else '-'}\n")
                    handle.write(f"# status: {state}\n")
                    handle.write(f"# included_records: {count}\n")
                    handle.write(
                        f"# time_range_utc: {bot_records[0][0].isoformat()} -> "
                        f"{bot_records[-1][0].isoformat()}\n" if bot_records
                        else "# time_range_utc: -\n"
                    )
                    handle.write("# ------------------------------------------------------------------------------\n")
                    if not bot_records:
                        handle.write("# No records included for this bot. See status above.\n")
                    for item in bot_records:
                        write_record(handle, item, args.no_redact)
            handle.flush()
            os.fsync(handle.fileno())
        if args.force:
            os.replace(temporary, output)
        else:
            # A hard link installs the synced file without any overwrite race.
            # If another bundler claimed the name meanwhile, advance again.
            while True:
                try:
                    os.link(temporary, output)
                    os.unlink(temporary)
                    break
                except FileExistsError:
                    output = next_numbered_output(requested_output)
                    print(f"Output name was claimed; writing {output} instead")
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    print(f"Wrote {output} ({len(merged)} records from {included}/{len(manifest)} bots; "
          f"{skipped} skipped; {failures} failed)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
