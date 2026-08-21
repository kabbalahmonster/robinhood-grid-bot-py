#!/usr/bin/env bash
# Sweep USDG from every bot checkout in a fleet. Dry run unless --execute is set.
set -uo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/sweep_fleet_usdg.sh --recipient ADDRESS [options]

Options:
  --fleet-root DIRECTORY       Root containing bot checkouts (default: ~/bots)
  --recipient ADDRESS          Central treasury wallet receiving USDG (required)
  --execute                    Broadcast transfers; otherwise only print dry-run plans
  --confirm-fleet-stopped      Required with --execute; confirms every bot is stopped
  --python PATH                Python interpreter to use (default: each bot's .venv/bin/python, then python3)
  -h, --help                   Show this help

The script does not stop processes. Stop every bot first, run a dry run and
review it, then repeat the same command with --execute --confirm-fleet-stopped.
EOF
}

fleet_root="${HOME}/bots"
recipient=""
execute=0
fleet_stopped=0
python_override=""

while (($#)); do
  case "$1" in
    --fleet-root)
      (($# >= 2)) || { echo "--fleet-root needs a directory" >&2; exit 2; }
      fleet_root="$2"
      shift 2
      ;;
    --recipient)
      (($# >= 2)) || { echo "--recipient needs an address" >&2; exit 2; }
      recipient="$2"
      shift 2
      ;;
    --execute) execute=1; shift ;;
    --confirm-fleet-stopped) fleet_stopped=1; shift ;;
    --python)
      (($# >= 2)) || { echo "--python needs a path" >&2; exit 2; }
      python_override="$2"
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$recipient" ]] || { echo "--recipient is required" >&2; usage >&2; exit 2; }
[[ -d "$fleet_root" ]] || { echo "Fleet root does not exist: $fleet_root" >&2; exit 2; }
if ((execute && !fleet_stopped)); then
  echo "Refusing to broadcast: --execute requires --confirm-fleet-stopped" >&2
  exit 2
fi

if [[ -n "$python_override" ]] && ! command -v "$python_override" >/dev/null 2>&1 && [[ ! -x "$python_override" ]]; then
  echo "Python interpreter not found: $python_override" >&2
  exit 2
fi

declare -a bot_dirs=()
while IFS= read -r -d '' bot_file; do
  bot_dirs+=("$(dirname "$bot_file")")
done < <(find "$fleet_root" -type f -name grid_bot.py -not -path '*/.venv/*' -print0 | sort -z)

((${#bot_dirs[@]})) || { echo "No grid_bot.py files found under: $fleet_root" >&2; exit 1; }

mode="DRY RUN"
((execute)) && mode="EXECUTE"
printf 'Fleet USDG sweep: %s\nRecipient: %s\nBots found: %d\n\n' "$mode" "$recipient" "${#bot_dirs[@]}"

successes=0
failures=0
for bot_dir in "${bot_dirs[@]}"; do
  if [[ -n "$python_override" ]]; then
    python_bin="$python_override"
  elif [[ -x "$bot_dir/.venv/bin/python" ]]; then
    python_bin="$bot_dir/.venv/bin/python"
  else
    python_bin="python3"
  fi

  printf '%s\n%s\n' '------------------------------------------------------------' "$bot_dir"
  cmd=("$python_bin" grid_bot.py --sweep-usdg "$recipient")
  if ((execute)); then
    cmd+=(--execute --confirm-bot-stopped)
  fi

  if (cd "$bot_dir" && "${cmd[@]}"); then
    ((successes += 1))
  else
    echo "FAILED: $bot_dir" >&2
    ((failures += 1))
  fi
done

printf '\nFleet sweep complete: %d succeeded, %d failed (%s).\n' "$successes" "$failures" "$mode"
((failures == 0))
