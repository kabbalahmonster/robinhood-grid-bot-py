# Tmux fleet operations

These scripts operate many independently configured bot checkouts as one tmux
fleet. Each bot gets its own pane, working directory, `.env`, virtual
environment, and persistent `data/` directory. The same local fleet
configuration also drives guarded whole-fleet USDG sweeps.

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
     ops/fleet/restart-fleet ops/fleet/update-fleet ops/fleet/usdg-sweep \
     ops/fleet/treasury-transfer ops/fleet/update-variable
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
   ln -sf "$PWD/ops/fleet/usdg-sweep" "$HOME/bin/usdg-sweep"
   ln -sf "$PWD/ops/fleet/treasury-transfer" "$HOME/bin/treasury-transfer"
   ln -sf "$PWD/ops/fleet/update-variable" "$HOME/bin/update-variable"
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

## Whole-fleet USDG sweep

`usdg-sweep` invokes each bot's guarded `--sweep-usdg` maintenance command
sequentially using the same explicit `FLEET_BOT_DIRS` list and per-checkout
Python selection as the lifecycle commands. It never discovers extra
directories and never stops bots automatically.

First set a shell variable to the actual reviewed treasury address and run a
dry run while the fleet is still available:

```bash
TREASURY=0xYourActualCentralWalletAddress
ops/fleet/usdg-sweep --recipient "$TREASURY"
```

Review the wallet, token, balance, amount, and recipient printed for **every**
bot. Then stop the fleet and execute using the same shell variable:

```bash
ops/fleet/stop-fleet
ops/fleet/usdg-sweep \
  --recipient "$TREASURY" \
  --execute \
  --confirm-fleet-stopped
```

Broadcast mode requires both `--execute` and `--confirm-fleet-stopped`, and it
also refuses to run if the configured tmux session still exists. Each bot must
allow the destination through its own `TREASURY_ALLOWED_RECIPIENTS`. For an
intentional one-off destination, repeat the exact address with
`--confirm-recipient "$TREASURY"`; the individual bot CLI still performs its
normal validation.

The sweep is not atomic across wallets. A later bot can fail after earlier
transfers confirm, so the script continues through the configured fleet,
reports per-bot failures, and exits nonzero if any failed. Each bot retains its
own audit trail in `data/treasury_transfers.json`. Never retry blindly: inspect
the output and receipts first. The tmux-session check cannot detect bots
started elsewhere, so the explicit stopped-fleet confirmation remains a human
safety assertion.

## Native ETH and other fleet treasury transfers

`treasury-transfer` is the general guarded batch command. Its `--asset` may be
`ETH`, `USDG`, or an ERC-20 contract address. Native ETH always requires an
exact amount; deliberately, there is no `all` mode for ETH.

To plan sending exactly `0.0005 ETH` from every configured wallet:

```bash
TREASURY=0xYourActualCentralWalletAddress
ops/fleet/treasury-transfer \
  --asset ETH \
  --amount 0.0005 \
  --recipient "$TREASURY"
```

For each wallet, the plan prints its balance, exact send amount, estimated
maximum gas cost, configured reserve, and minimum remaining balance. The bot
refuses the transfer unless:

```text
balance >= amount + estimated maximum gas cost + ETH_GAS_RESERVE
```

After reviewing every plan, stop the fleet and repeat with the guards:

```bash
ops/fleet/stop-fleet
ops/fleet/treasury-transfer \
  --asset ETH \
  --amount 0.0005 \
  --recipient "$TREASURY" \
  --execute \
  --confirm-fleet-stopped
```

For another ERC-20, use its contract address and either an exact token amount
or `all`. USDG is accepted as a named asset. All recipient allowlist,
one-time-confirmation, stopped-process, receipt, partial-failure, and non-atomic
warnings from `usdg-sweep` apply equally here.

## Updating one variable across the fleet

`update-variable` previews or updates variables in every configured checkout's
`.env`. It requires an existing variable by default, refuses duplicate
definitions and malformed names, writes atomically, preserves file permissions
and inline comments, and creates timestamped `.env.bak.*` files before applying
anything. If an apply step fails, it restores every `.env` from that run.

Preview changing the fleet gas reserve:

```bash
ops/fleet/update-variable ETH_GAS_RESERVE=0.0005
```

Review every old/new line, then apply and restart so all processes load it:

```bash
ops/fleet/update-variable --apply --restart ETH_GAS_RESERVE=0.0005
```

Multiple assignments may be updated together:

```bash
ops/fleet/update-variable POLL_INTERVAL_SECONDS=8 ETH_GAS_RESERVE=0.0005
```

Use `--allow-add` only when intentionally adding a variable missing from one or
more files. Without `--restart`, running bots keep their old in-memory values
until the next restart. Values must already be valid dotenv syntax; quote a
value as required by dotenv itself. Avoid secrets on the command line because
shell history and process listings can expose arguments (the preview redacts
common secret-like names, but the shell cannot).

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
- `usdg-sweep` is dry-run by default, validates the recipient format, and uses
  each checkout's existing `.env`; treasury addresses and keys never belong in
  `fleet.conf`.
- `treasury-transfer` applies the same batch guards to native ETH, USDG, and
  arbitrary ERC-20 contracts. Native transfers also enforce the configured gas
  reserve after the amount and estimated maximum fee.
- `update-variable` is preview-only without `--apply`; backups are intentionally
  retained for operator recovery and gitignored.

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
