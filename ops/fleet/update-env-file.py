#!/usr/bin/env python3
"""Validate or atomically update assignments in one dotenv file."""

import argparse
import os
import re
import stat
import tempfile


NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SENSITIVE_RE = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|MNEMONIC|CREDENTIAL)", re.I)


def parse_assignment(raw):
    if "=" not in raw:
        raise ValueError(f"Assignment must be NAME=VALUE: {raw}")
    name, value = raw.split("=", 1)
    if not NAME_RE.fullmatch(name):
        raise ValueError(f"Invalid variable name: {name}")
    if "\n" in value or "\r" in value:
        raise ValueError(f"Value for {name} contains a newline")
    return name, value


def inline_comment(value):
    quote = None
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if char in "'\"":
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
            continue
        if char == "#" and quote is None and (index == 0 or value[index - 1].isspace()):
            return value[index:].rstrip("\r\n")
    return ""


def display_value(name, value):
    return "<redacted>" if SENSITIVE_RE.search(name) else value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-add", action="store_true")
    parser.add_argument("assignments", nargs="+")
    args = parser.parse_args()

    assignments = dict(parse_assignment(raw) for raw in args.assignments)
    if len(assignments) != len(args.assignments):
        raise ValueError("Each variable may be assigned only once")

    with open(args.file, "r", encoding="utf-8") as handle:
        lines = handle.readlines()

    found = {name: [] for name in assignments}
    patterns = {
        name: re.compile(rf"^(\s*(?:export\s+)?{re.escape(name)}\s*=)(.*?)(\r?\n)?$")
        for name in assignments
    }
    for index, line in enumerate(lines):
        for name, pattern in patterns.items():
            match = pattern.match(line)
            if match:
                found[name].append((index, match))

    for name, matches in found.items():
        if len(matches) > 1:
            raise ValueError(f"{args.file}: {name} is defined more than once")
        if not matches and not args.allow_add:
            raise ValueError(f"{args.file}: {name} is missing (use --allow-add to append it)")

    for name, new_value in assignments.items():
        if found[name]:
            _, match = found[name][0]
            old_value = match.group(2).strip()
        else:
            old_value = "<missing>"
        print(f"{args.file}: {name}: {display_value(name, old_value)} -> {display_value(name, new_value)}")

    if not args.apply:
        return

    for name, new_value in assignments.items():
        if found[name]:
            index, match = found[name][0]
            comment = inline_comment(match.group(2))
            suffix = f" {comment}" if comment else ""
            newline = match.group(3) or "\n"
            lines[index] = f"{match.group(1)}{new_value}{suffix}{newline}"
        else:
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += "\n"
            lines.append(f"{name}={new_value}\n")

    original_mode = stat.S_IMODE(os.stat(args.file).st_mode)
    directory = os.path.dirname(os.path.abspath(args.file))
    fd, temp_path = tempfile.mkstemp(prefix=".env.update.", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, original_mode)
        os.replace(temp_path, args.file)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


if __name__ == "__main__":
    main()
