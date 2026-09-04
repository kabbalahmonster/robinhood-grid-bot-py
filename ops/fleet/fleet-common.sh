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
  local requested_config="${1:-}" root_path home_path bot_file bot_dir discovery_file name names_configured dirs_configured
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
  : "${FLEET_TREASURY_RECIPIENT:=}"
  : "${FLEET_BOT_ROOT:=${HOME}/bot-farm/rh-bots}"
  : "${FLEET_CHECKOUT_DIRNAME:=robinhood-grid-bot-py}"
  : "${FLEET_DISCOVERY_MAX_DEPTH:=4}"

  [[ "$FLEET_RESTART_DELAY" =~ ^[0-9]+([.][0-9]+)?$ ]] || fleet_die "FLEET_RESTART_DELAY must be a non-negative number"
  [[ "$FLEET_START_STAGGER" =~ ^[0-9]+([.][0-9]+)?$ ]] || fleet_die "FLEET_START_STAGGER must be a non-negative number"
  [[ "$FLEET_DISCOVERY_MAX_DEPTH" =~ ^[2-9][0-9]*$ ]] || fleet_die "FLEET_DISCOVERY_MAX_DEPTH must be an integer of at least 2"
  if [[ -n "$FLEET_TREASURY_RECIPIENT" && ! "$FLEET_TREASURY_RECIPIENT" =~ ^0x[0-9a-fA-F]{40}$ ]]; then
    fleet_die "FLEET_TREASURY_RECIPIENT must be empty or a 20-byte 0x-prefixed EVM address"
  fi

  names_configured=0
  dirs_configured=0
  declare -p FLEET_BOT_NAMES >/dev/null 2>&1 && ((${#FLEET_BOT_NAMES[@]} > 0)) && names_configured=1
  declare -p FLEET_BOT_DIRS >/dev/null 2>&1 && ((${#FLEET_BOT_DIRS[@]} > 0)) && dirs_configured=1
  ((names_configured == 0 || dirs_configured == 0)) || fleet_die "Configure FLEET_BOT_NAMES or FLEET_BOT_DIRS, not both"

  if ((names_configured)); then
    [[ "$FLEET_CHECKOUT_DIRNAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || fleet_die "FLEET_CHECKOUT_DIRNAME must be one safe directory name"
    FLEET_BOT_DIRS=()
    for name in "${FLEET_BOT_NAMES[@]}"; do
      [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || fleet_die "Invalid bot name in FLEET_BOT_NAMES: $name"
      FLEET_BOT_DIRS+=("$FLEET_BOT_ROOT/$name/$FLEET_CHECKOUT_DIRNAME")
    done
    FLEET_MEMBERSHIP_SOURCE="explicit FLEET_BOT_NAMES"
  elif ((dirs_configured)); then
    FLEET_MEMBERSHIP_SOURCE="explicit FLEET_BOT_DIRS"
  else
    [[ "$FLEET_ENTRYPOINT" != */* ]] || fleet_die "Root discovery requires FLEET_ENTRYPOINT to be a filename, not a path"
    [[ -d "$FLEET_BOT_ROOT" ]] || fleet_die "Fleet bot root does not exist: $FLEET_BOT_ROOT"
    root_path="$(readlink -f -- "$FLEET_BOT_ROOT")"
    home_path="$(readlink -f -- "$HOME")"
    [[ "$root_path" != "/" && "$root_path" != "$home_path" ]] || fleet_die "Refusing dangerously broad fleet bot root: $root_path"

    discovery_file="$(mktemp)" || fleet_die "Could not create fleet discovery workspace"
    if ! (
      set -o pipefail
      find "$root_path" -mindepth 2 -maxdepth "$FLEET_DISCOVERY_MAX_DEPTH" \
        \( -type d \( -name .git -o -name .venv -o -name venv -o -name __pycache__ \) -prune \) -o \
        \( -type f -name "$FLEET_ENTRYPOINT" -print0 \) | sort -z > "$discovery_file"
    ); then
      rm -f -- "$discovery_file"
      fleet_die "Fleet discovery failed while scanning: $root_path"
    fi

    FLEET_BOT_DIRS=()
    while IFS= read -r -d '' bot_file; do
      bot_dir="$(dirname -- "$bot_file")"
      FLEET_BOT_DIRS+=("$bot_dir")
    done < "$discovery_file"
    rm -f -- "$discovery_file"
    ((${#FLEET_BOT_DIRS[@]} > 0)) || fleet_die "No bot checkouts containing '$FLEET_ENTRYPOINT' found under: $root_path"
    FLEET_BOT_ROOT="$root_path"
    FLEET_MEMBERSHIP_SOURCE="discovered under $FLEET_BOT_ROOT"
  fi
}

fleet_require_tmux() {
  command -v tmux >/dev/null 2>&1 || fleet_die "tmux is required but was not found in PATH"
}

fleet_session_exists() {
  tmux has-session -t "=$FLEET_SESSION" 2>/dev/null
}

# A marker is used instead of the service state because the systemd unit may
# remain active while operators deliberately leave the tmux fleet stopped.
# Scope it to both config and session so multiple fleets owned by one user do
# not accidentally control one another.
fleet_desired_state_file() {
  local identity digest state_root
  identity="$(readlink -f -- "$FLEET_CONFIG_PATH")"$'\n'"$FLEET_SESSION"
  if command -v sha256sum >/dev/null 2>&1; then
    digest="$(printf '%s' "$identity" | sha256sum | awk '{print $1}')"
  else
    digest="$(printf '%s' "$identity" | cksum | awk '{print $1}')"
  fi
  state_root="${FLEET_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/rh-grid-bot/fleet}"
  printf '%s/%s.desired-stopped\n' "$state_root" "$digest"
}

fleet_should_run() {
  [[ ! -e "$(fleet_desired_state_file)" ]]
}

fleet_record_stopped() {
  local marker marker_dir temporary
  marker="$(fleet_desired_state_file)"
  marker_dir="$(dirname -- "$marker")"
  mkdir -p -- "$marker_dir"
  temporary="$(mktemp "$marker_dir/.desired-stopped.XXXXXX")"
  printf 'config=%s\nsession=%s\n' "$FLEET_CONFIG_PATH" "$FLEET_SESSION" > "$temporary"
  mv -f -- "$temporary" "$marker"
}

fleet_record_running() {
  rm -f -- "$(fleet_desired_state_file)"
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

fleet_bot_name() {
  local bot_dir="$1" repo_name
  repo_name="$(basename -- "$bot_dir")"
  if [[ "$repo_name" == "robinhood-grid-bot-py" ]]; then
    basename -- "$(dirname -- "$bot_dir")"
  else
    printf '%s\n' "$repo_name"
  fi
}

fleet_sort_targets_by_name() {
  local bot_dir name record
  local -a records=() sorted=()
  for bot_dir in "${FLEET_BOT_DIRS[@]}"; do
    name="$(fleet_bot_name "$bot_dir")"
    records+=("${name}"$'\t'"${bot_dir}")
  done
  mapfile -t sorted < <(printf '%s\n' "${records[@]}" | LC_ALL=C sort -f -t $'\t' -k1,1 -k2,2)
  FLEET_BOT_DIRS=()
  for record in "${sorted[@]}"; do
    FLEET_BOT_DIRS+=("${record#*$'\t'}")
  done
}

fleet_apply_selection() {
  local only_csv="${1:-}" exclude_csv="${2:-}"
  local bot_dir name normalized token match
  local -a selected=() tokens=()
  local -A known=() included=() excluded=()

  for bot_dir in "${FLEET_BOT_DIRS[@]}"; do
    name="$(fleet_bot_name "$bot_dir")"
    normalized="${name,,}"
    [[ -z "${known[$normalized]+x}" ]] || fleet_die "Duplicate fleet bot name '$name' (case-insensitive); use unique checkout parent names"
    known["$normalized"]="$bot_dir"
  done

  if [[ -n "$only_csv" ]]; then
    IFS=',' read -r -a tokens <<< "$only_csv"
    for token in "${tokens[@]}"; do
      token="${token#"${token%%[![:space:]]*}"}"
      token="${token%"${token##*[![:space:]]}"}"
      [[ -n "$token" ]] || fleet_die "--only contains an empty bot name"
      normalized="${token,,}"
      [[ -n "${known[$normalized]+x}" ]] || fleet_die "Unknown bot in --only: $token"
      included["$normalized"]=1
    done
  fi

  tokens=()
  if [[ -n "$exclude_csv" ]]; then
    IFS=',' read -r -a tokens <<< "$exclude_csv"
    for token in "${tokens[@]}"; do
      token="${token#"${token%%[![:space:]]*}"}"
      token="${token%"${token##*[![:space:]]}"}"
      [[ -n "$token" ]] || fleet_die "--exclude contains an empty bot name"
      normalized="${token,,}"
      [[ -n "${known[$normalized]+x}" ]] || fleet_die "Unknown bot in --exclude: $token"
      excluded["$normalized"]=1
    done
  fi

  FLEET_SELECTED_NAMES=()
  for bot_dir in "${FLEET_BOT_DIRS[@]}"; do
    name="$(fleet_bot_name "$bot_dir")"
    normalized="${name,,}"
    match=1
    [[ -z "$only_csv" || -n "${included[$normalized]+x}" ]] || match=0
    [[ -z "${excluded[$normalized]+x}" ]] || match=0
    if ((match)); then
      selected+=("$bot_dir")
      FLEET_SELECTED_NAMES+=("$name")
    fi
  done
  ((${#selected[@]} > 0)) || fleet_die "Fleet selection resolved to zero bots"
  FLEET_BOT_DIRS=("${selected[@]}")
  FLEET_SELECTION_DESCRIPTION="${FLEET_SELECTED_NAMES[*]}"
}

fleet_pane_for_bot_dir() {
  local wanted_dir="$1" pane_id pane_path resolved
  wanted_dir="$(readlink -f -- "$wanted_dir")"
  while IFS=$'\t' read -r pane_id pane_path; do
    resolved="$(readlink -f -- "$pane_path" 2>/dev/null || true)"
    if [[ "$resolved" == "$wanted_dir" ]]; then
      printf '%s\n' "$pane_id"
      return 0
    fi
  done < <(tmux list-panes -t "=$FLEET_SESSION:$FLEET_WINDOW" -F $'#{pane_id}\t#{pane_current_path}')
  return 1
}

fleet_validate_bots() {
  local bot_dir bot_file python_bin
  local -A seen=()

  printf 'Fleet membership: %s (%d selected)\n' "$FLEET_MEMBERSHIP_SOURCE" "${#FLEET_BOT_DIRS[@]}"
  printf 'Fleet targets: %s\n' "${FLEET_SELECTION_DESCRIPTION:-$(for bot_dir in "${FLEET_BOT_DIRS[@]}"; do fleet_bot_name "$bot_dir"; done | paste -sd' ' -)}"

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
  # Sending literal text into an interactive shell preserves readable tmux
  # panes. Operators should use restart-bot rather than Ctrl+Z/Up-arrow: the
  # latter leaves the old Python process suspended and can exhaust the host.
  tmux send-keys -t "$pane_target" -l "$command_text"
  tmux send-keys -t "$pane_target" C-m
}
