# Tmux fleet operations

These scripts operate many independently configured bot checkouts as one tmux
fleet. Each bot gets its own pane, working directory, `.env`, virtual
environment, and persistent `data/` directory.

The start command deliberately creates an **interactive shell first** and uses
`tmux send-keys` to enter the restart loop. This preserves the established
operator workflow:

- `Ctrl+C` stops the current Python process; its loop restarts it after the
  configured delay.
- `Ctrl+Z` suspends the foreground loop and returns to the same Bash prompt.
- `Up` recalls the complete loop command; `Enter` starts it again.
- `jobs`, `fg`, and `bg` remain available because the pane contains a real
  interactive shell.

Be aware that `Ctrl+Z` suspends rather than terminates the old job. Before
starting another copy, use `jobs` to inspect suspended jobs and `kill %1`
(adjusting the job number) to remove one, or `fg` to resume it. Do not run two
copies of the same bot checkout simultaneously.

## Requirements

- Bash 4 or newer
- tmux
- Git (for `update-fleet`)
- One complete bot checkout per fleet member
- A working `.venv`, `venv`, or system `python3` for every checkout

On Debian or Ubuntu:

```bash
sudo apt update
sudo apt install tmux git python3 python3-venv
```

## Fresh-clone setup

1. Keep this first clone as your template/operations checkout, or use one of
   the bot checkouts to run the scripts.
2. Create one independent clone per bot. Independent checkouts are important:
   each bot needs its own `.env`, branch, virtual environment, and `data/`.

   ```bash
   mkdir -p "$HOME/bot-farm/rh-bots/example-one"
   git clone https://github.com/kabbalahmonster/robinhood-grid-bot-py.git \
     "$HOME/bot-farm/rh-bots/example-one/robinhood-grid-bot-py"
   ```

3. In every bot checkout, create its environment and configure that bot:

   ```bash
   cd "$HOME/bot-farm/rh-bots/example-one/robinhood-grid-bot-py"
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   cp .env.robinhood .env
   chmod 600 .env
   nano .env
   ```

4. In the checkout from which you will operate the fleet:

   ```bash
   cd robinhood-grid-bot-py
   cp ops/fleet/fleet.conf.example ops/fleet/fleet.conf
   nano ops/fleet/fleet.conf
   chmod +x ops/fleet/start-fleet ops/fleet/stop-fleet \
     ops/fleet/restart-fleet ops/fleet/update-fleet
   ```

   Replace the example directories in `FLEET_BOT_DIRS` with every real bot
   checkout. `ops/fleet/fleet.conf` is gitignored.

5. Optional: put convenient command names in `~/bin`:

   ```bash
   mkdir -p "$HOME/bin"
   ln -sf "$PWD/ops/fleet/start-fleet" "$HOME/bin/start-fleet"
   ln -sf "$PWD/ops/fleet/stop-fleet" "$HOME/bin/stop-fleet"
   ln -sf "$PWD/ops/fleet/restart-fleet" "$HOME/bin/restart-fleet"
   ln -sf "$PWD/ops/fleet/update-fleet" "$HOME/bin/update-fleet"
   ```

   Ensure `~/bin` is in `PATH`, or invoke the scripts by their repository paths.

The scripts look for configuration in this order:

1. `--config PATH`
2. the `FLEET_CONFIG` environment variable
3. `ops/fleet/fleet.conf`
4. `${XDG_CONFIG_HOME:-$HOME/.config}/rh-grid-bot/fleet.conf`

## Commands

Start and attach:

```bash
ops/fleet/start-fleet
```

Start without attaching:

```bash
ops/fleet/start-fleet --detach
```

Detach from tmux without stopping bots with `Ctrl+B`, then `D`. Reattach with:

```bash
tmux attach-session -t bot_farm
```

Stop the entire fleet:

```bash
ops/fleet/stop-fleet
```

Restart from the files currently on disk:

```bash
ops/fleet/restart-fleet
```

Update every clean checkout using `git pull --ff-only`:

```bash
ops/fleet/update-fleet
```

Update all checkouts and restart only after every update succeeds:

```bash
ops/fleet/update-fleet --restart
```

Add `--detach` to either restart form to leave the new session in the
background. Every command accepts `--config PATH`.

## Safety and update behavior

- `start-fleet` validates every directory, entrypoint, interpreter, and
  duplicate path before creating tmux. A failed launch removes its partial
  session.
- Python selection is per checkout: `.venv/bin/python`, then
  `venv/bin/python`, then `python3` from `PATH`.
- `stop-fleet` terminates the tmux session and therefore every bot process in
  it. It does not delete files or persistent bot state.
- `update-fleet` preflights all repositories before changing any of them. It
  refuses dirty worktrees, detached HEADs, and branches without upstreams, and
  only permits fast-forward pulls.
- Separate Git repositories cannot be updated as one atomic transaction. A
  later network/pull failure may occur after earlier repositories updated; in
  that case the script reports failure and does not restart the fleet.
- Updating files does not alter already-running Python processes. Use
  `update-fleet --restart` when the new code should become active immediately.
- The config is sourced as Bash and must be treated as trusted local code. It
  should contain paths/topology only—never private keys or API credentials.

## Tmux navigation and troubleshooting

- `Ctrl+B`, arrow key: move between panes.
- `Ctrl+B`, `Z`: zoom/unzoom the current pane.
- `Ctrl+B`, `D`: detach while leaving the fleet running.
- `Ctrl+C`: restart one bot through its loop.
- `Ctrl+Z`: suspend one bot loop and return to its shell.
- `tmux list-sessions`: list running sessions.
- `tmux kill-session -t bot_farm`: emergency equivalent of `stop-fleet`.

If an older launcher installed a global no-prefix `Ctrl+Z` tmux binding, remove
that one-time override before using this interactive design:

```bash
tmux unbind-key -n C-z
```

Do not add a custom `Ctrl+Z` binding to these scripts. Normal terminal job
control is the intended mechanism.
