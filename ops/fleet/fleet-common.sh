#!/usr/bin/env bash

# Shared helpers for the fleet operation commands. This file is sourced by the
# executable scripts; it is not intended to be run directly.

fleet_die() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

fleet_script_dir() {
  cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd
}

fleet_default_config() {
  local script_dir xdg_config
  script_dir="$(fleet_script_dir)"
  xdg_config="${XDG_CONFIG_HOME:-${HOME}/.config}/rh-grid-bot/fleet.conf"

  if [[ -f "$script_dir/fleet.conf" ]]; then
    printf '%s\n' "$script_dir/fleet.conf"
  elif [[ -f "$xdg_config" ]]; then
    printf '%s\n' "$xdg_config"
  else
    printf '%s\n' "$script_dir/fleet.conf"
  fi
}

fleet_load_config() {
  local requested_config="${1:-}"
  FLEET_CONFIG_PATH="${requested_config:-${FLEET_CONFIG:-$(fleet_default_config)}}"
  [[ -f "$FLEET_CONFIG_PATH" ]] || fleet_die "Fleet config not found: $FLEET_CONFIG_PATH
Copy ops/fleet/fleet.conf.example to ops/fleet/fleet.conf and edit it,
or pass --config PATH / set FLEET_CONFIG."

  # fleet.conf is a trusted local Bash configuration file so it can use $HOME.
  # shellcheck disable=SC1090
  source "$FLEET_CONFIG_PATH"

  : "${FLEET_SESSION:=bot_farm}"
  : "${FLEET_WINDOW:=fleet}"
  : "${FLEET_ENTRYPOINT:=grid_bot.py}"
  : "${FLEET_RESTART_DELAY:=5}"
  : "${FLEET_START_STAGGER:=1}"

  declare -p FLEET_BOT_DIRS >/dev/null 2>&1 || fleet_die "FLEET_BOT_DIRS is not defined in $FLEET_CONFIG_PATH"
  ((${#FLEET_BOT_DIRS[@]} > 0)) || fleet_die "FLEET_BOT_DIRS is empty in $FLEET_CONFIG_PATH"
  [[ "$FLEET_RESTART_DELAY" =~ ^[0-9]+([.][0-9]+)?$ ]] || fleet_die "FLEET_RESTART_DELAY must be a non-negative number"
  [[ "$FLEET_START_STAGGER" =~ ^[0-9]+([.][0-9]+)?$ ]] || fleet_die "FLEET_START_STAGGER must be a non-negative number"
}

fleet_require_tmux() {
  command -v tmux >/dev/null 2>&1 || fleet_die "tmux is required but was not found in PATH"
}

fleet_session_exists() {
  tmux has-session -t "=$FLEET_SESSION" 2>/dev/null
}

fleet_python_for() {
  local bot_dir="$1"
  if [[ -x "$bot_dir/.venv/bin/python" ]]; then
    printf '%s\n' "$bot_dir/.venv/bin/python"
  elif [[ -x "$bot_dir/venv/bin/python" ]]; then
    printf '%s\n' "$bot_dir/venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    command -v python3
  else
    return 1
  fi
}

fleet_validate_bots() {
  local bot_dir bot_file python_bin
  local -A seen=()

  for bot_dir in "${FLEET_BOT_DIRS[@]}"; do
    [[ "$bot_dir" != *$'\n'* ]] || fleet_die "Bot directory contains a newline: $bot_dir"
    [[ -z "${seen[$bot_dir]+x}" ]] || fleet_die "Duplicate bot directory in fleet config: $bot_dir"
    seen["$bot_dir"]=1
    [[ -d "$bot_dir" ]] || fleet_die "Bot directory does not exist: $bot_dir"
    bot_file="$bot_dir/$FLEET_ENTRYPOINT"
    [[ -f "$bot_file" ]] || fleet_die "Missing bot entrypoint: $bot_file"
    python_bin="$(fleet_python_for "$bot_dir")" || fleet_die "No Python interpreter found for: $bot_dir"
    [[ -x "$python_bin" ]] || fleet_die "Python interpreter is not executable: $python_bin"
  done
}

fleet_bot_command() {
  local bot_dir="$1" python_bin
  python_bin="$(fleet_python_for "$bot_dir")" || return 1

  printf 'cd %q && while true; do %q %q; exit_code=$?; printf %q "$exit_code"; sleep %q; done' \
    "$bot_dir" \
    "$python_bin" \
    "$FLEET_ENTRYPOINT" \
    $'Bot exited with status %s; restarting in '"$FLEET_RESTART_DELAY"$' seconds...\n' \
    "$FLEET_RESTART_DELAY"
}

fleet_send_command() {
  local pane_target="$1" command_text="$2"
  # Sending literal text into an already-running interactive shell is
  # intentional. It preserves Bash job control and command history, including
  # the established Ctrl+Z -> prompt -> Up-arrow workflow.
  tmux send-keys -t "$pane_target" -l "$command_text"
  tmux send-keys -t "$pane_target" C-m
}
