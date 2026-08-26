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
     ops/fleet/treasury-transfer ops/fleet/update-variable \
     ops/fleet/backup-private-keys ops/fleet/fleet-discover \
     ops/fleet/liquidate-assets ops/fleet/fleet-doctor \
     ops/fleet/fleet-inventory ops/fleet/fleet-audit \
     ops/fleet/dashboard-remove
   ```

   Set `FLEET_BOT_ROOT` to the directory containing your bot checkouts. The
   default is `$HOME/bot-farm/rh-bots`. Checkouts containing
   `FLEET_ENTRYPOINT` are discovered recursively and sorted. If you instead
   define a non-empty `FLEET_BOT_DIRS` array, that explicit list takes complete
   priority over root discovery. Optionally set `FLEET_TREASURY_RECIPIENT` to
   the public address used when a transfer command omits `--recipient`.
   `ops/fleet/fleet.conf` is gitignored.

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
   ln -sf "$PWD/ops/fleet/backup-private-keys" "$HOME/bin/backup-private-keys"
   ln -sf "$PWD/ops/fleet/fleet-discover" "$HOME/bin/fleet-discover"
   ln -sf "$PWD/ops/fleet/liquidate-assets" "$HOME/bin/liquidate-assets"
   ln -sf "$PWD/ops/fleet/fleet-doctor" "$HOME/bin/fleet-doctor"
   ln -sf "$PWD/ops/fleet/fleet-inventory" "$HOME/bin/fleet-inventory"
   ln -sf "$PWD/ops/fleet/fleet-audit" "$HOME/bin/fleet-audit"
   ln -sf "$PWD/ops/fleet/dashboard-remove" "$HOME/bin/dashboard-remove"
   ```

   Ensure `~/bin` is in `PATH`, or invoke the scripts by their repository paths.

The scripts look for configuration in this order:

1. `--config PATH`
2. the `FLEET_CONFIG` environment variable
3. `ops/fleet/fleet.conf`
4. `${XDG_CONFIG_HOME:-$HOME/.config}/rh-grid-bot/fleet.conf`

### Fleet membership: root discovery or explicit list

The recommended configuration names the authoritative fleet below one root:

```bash
FLEET_BOT_ROOT="$HOME/bot-farm/rh-bots"
FLEET_CHECKOUT_DIRNAME="robinhood-grid-bot-py"
FLEET_BOT_NAMES=(ai brodie cashcat)
```

Every name resolves to
`$FLEET_BOT_ROOT/<name>/$FLEET_CHECKOUT_DIRNAME`. All fleet commands therefore
share the same membership and deterministic order. `fleet-discover` scans the
root and prints a suggested `FLEET_BOT_NAMES` block; review it before copying
it into `fleet.conf`.

For backward compatibility, a configuration without `FLEET_BOT_NAMES` or
`FLEET_BOT_DIRS` discovers bot checkouts below one root:

```bash
FLEET_BOT_ROOT="$HOME/bot-farm/rh-bots"
FLEET_DISCOVERY_MAX_DEPTH=4
```

A directory joins the discovered fleet only when a regular file named by
`FLEET_ENTRYPOINT` (normally `grid_bot.py`) is found within the configured
depth. `.git`, `.venv`, `venv`, and `__pycache__` trees are excluded. Results
are sorted for deterministic pane and batch-operation order. The loader rejects
`/` and the whole home directory as dangerously broad roots, and fails rather
than operating when discovery finds no bots.

For exact membership, define a non-empty array:

```bash
FLEET_BOT_DIRS=(
  "$HOME/bot-farm/rh-bots/seedcoin/robinhood-grid-bot-py"
  "$HOME/bot-farm/rh-bots/tendies/robinhood-grid-bot-py"
)
```

A non-empty `FLEET_BOT_DIRS` always wins and `FLEET_BOT_ROOT` is ignored. An
undefined or empty array falls back to root discovery. Every operational
command prints whether membership was explicit or discovered and the resolved
bot count before acting. Root discovery is convenient, but it also means any
valid checkout placed beneath that root can join future financial operations;
use the explicit array whenever that is not desirable.

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

## Selecting part of the fleet

Batch and diagnostic commands accept exact, comma-separated bot names through
`--only` and `--exclude`. For the standard layout, the bot name is the parent
directory containing `robinhood-grid-bot-py`:

```bash
ops/fleet/fleet-doctor --only seedcoin,tendies
ops/fleet/fleet-inventory --exclude ai,closed
ops/fleet/treasury-transfer --only seedcoin --asset ETH --amount 0.0005
```

Selection happens after configured membership is resolved: `--only` narrows
the fleet first, then `--exclude` removes names. Unknown names, duplicate bot
names, empty list items, and a selection containing no bots are errors. Every
command prints the final names before doing work. Selectors are supported by
`start-fleet`, `update-fleet`, `update-variable`, both treasury tools,
`liquidate-assets`, `fleet-doctor`, `fleet-inventory`, and `fleet-audit`.

`stop-fleet` and `restart-fleet` remain whole-session operations because tmux
owns one fleet session. `update-fleet --restart` and
`update-variable --restart` deliberately reject selectors: update a subset,
then perform an explicitly reviewed whole-fleet restart separately if needed.
This prevents a partial maintenance command from unexpectedly recycling every
bot.

## Read-only health checks and inventory

Run the doctor before starting a new fleet or moving funds:

```bash
ops/fleet/fleet-doctor
ops/fleet/fleet-doctor --only seedcoin,tendies
ops/fleet/fleet-doctor --json > fleet-doctor.json
```

For each checkout it checks Git branch/commit/upstream/dirty state, `.env`
existence and permissions, configuration loading, configured versus actual RPC
chain ID, derived public wallet address, configured token/USDG/WETH contract
code and metadata, dashboard settings, and a small read-only WETH-to-token route
quote through the configured provider/fallback. It also reports local
ahead/behind tracking state and whether the configured tmux fleet session is
running. `--no-quote` skips only the
provider quote when offline diagnosis is desired. It never prints a private key
or API credential and never signs, approves, broadcasts, or modifies files.
Failures make the command exit nonzero; warnings (such as an intentionally
disabled dashboard) remain visible but do not.

Use inventory for a concise current-state snapshot without the route probe:

```bash
ops/fleet/fleet-inventory
ops/fleet/fleet-inventory --json > fleet-inventory.json
```

It reports each public wallet, chain, native balance, configured reserve and
spendable native balance, configured managed-token balances and raw units,
classic/gridless position counts, Git identity, and latest local treasury and
liquidation timestamps. JSON retains raw integer balances for automation;
human output is deliberately shorter. Inventory is read-only, but it does make
RPC calls and therefore may fail on an unavailable or misconfigured endpoint.

## Reconciling fleet transaction history

`fleet-audit` reads the durable local logs written immediately by treasury and
managed-liquidation operations. It never contacts a signer and never retries a
transaction:

```bash
ops/fleet/fleet-audit
ops/fleet/fleet-audit --failures-only
ops/fleet/fleet-audit --json > fleet-audit.json
```

It summarizes confirmed and failed treasury records, liquidation asset
receipts, incomplete/failed liquidation runs, whether successful liquidation
cleared positions, and chain-appropriate explorer links for known transaction
hashes. Missing terminal records and uncleared positions are marked for
attention and produce a nonzero exit status.

After a partial batch, this emits only the exact comma-separated names needing
review:

```bash
ops/fleet/fleet-audit --emit-only
```

That output can help construct a later reviewed `--only` command, but is never
executed automatically. Audit is historical reconciliation, not proof of live
balances; use `fleet-inventory` to inspect current residual balances before a
retry. Local files can also be missing or manually altered, so retain explorer
receipts and never treat audit output as an automatic authorization to resend.

## Whole-fleet USDG sweep

`usdg-sweep` invokes each bot's guarded `--sweep-usdg` maintenance command
sequentially using the same membership resolution and per-checkout Python
selection as the lifecycle commands. Membership comes from the explicit list
when non-empty, otherwise from guarded root discovery. It never stops bots
automatically.

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

If `FLEET_TREASURY_RECIPIENT` is set in `fleet.conf`, `--recipient` may be
omitted:

```bash
ops/fleet/usdg-sweep
```

An explicit `--recipient ADDRESS` always overrides the configured default for
that run. The default recipient does not replace per-bot
`TREASURY_ALLOWED_RECIPIENTS`; every bot still independently enforces its
allowlist or requires the exact `--confirm-recipient` acknowledgement.

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
`ETH`, `USDG`, or an ERC-20 contract address. Exact native transfers preserve
the gas reserve. Native `all` is a separate, explicitly confirmed liquidation
mode.

When `FLEET_TREASURY_RECIPIENT` is configured, the same default applies here:

```bash
ops/fleet/treasury-transfer --asset ETH --amount 0.0005
```

Pass `--recipient ADDRESS` to deliberately override it for one invocation.

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

To liquidate the native ETH balance of every configured wallet, leaving only
the buffered maximum fee needed by each transfer:

```bash
# Plan only; --confirm-liquidate is required even to produce this plan.
ops/fleet/treasury-transfer \
  --asset ETH \
  --amount all \
  --recipient "$TREASURY" \
  --confirm-liquidate

# Broadcast only after reviewing every calculated amount.
ops/fleet/stop-fleet
ops/fleet/treasury-transfer \
  --asset ETH \
  --amount all \
  --recipient "$TREASURY" \
  --confirm-liquidate \
  --execute \
  --confirm-fleet-stopped
```

Liquidation intentionally sets the retained reserve to zero and sends
`balance - buffered maximum fee`. It is allowed only to an externally owned
account (an address without deployed bytecode), because a contract's receive
logic may consume different gas and invalidate the subtraction. Gas prices can
still move between construction and mining; a failed transaction may consume
gas without completing the transfer. Inspect every receipt before retrying.

## Liquidating bot-managed assets to native ETH

`liquidate-assets` converts all configured bot-managed ERC-20 balances in each
wallet into native ETH: the full `TOKEN_ADDRESS` balance (including untracked
moonbags), the full `USDG_ADDRESS` balance, and the full `WETH_ADDRESS` balance.
WETH is unwrapped directly. Duplicate addresses are processed once, existing
native ETH stays in the wallet, and unknown airdrops or unrelated tokens are
deliberately ignored. EVM wallets do not natively enumerate tokens; external
discovery would let spam or malicious assets enter a destructive workflow.

Create and review the complete read-only plan first:

```bash
ops/fleet/liquidate-assets --confirm-liquidate-assets
```

To preserve every wallet's configured USDG balance, add `--keep-usdg` to both
the plan and the later execution command:

```bash
ops/fleet/liquidate-assets --confirm-liquidate-assets --keep-usdg
```

In this mode USDG is not read as a liquidation target, quoted, approved,
swapped, or required to reach zero. Trading-token and WETH handling—and every
position-clearing guard—remain unchanged.

The confirmation is required even for planning. A dry run reads balances and
quotes but sends no approval, swap, or unwrap transaction and changes no files.
To execute after reviewing every wallet:

```bash
ops/fleet/stop-fleet
ops/fleet/liquidate-assets \
  --confirm-liquidate-assets \
  --execute \
  --confirm-fleet-stopped
```

If the reviewed plan used `--keep-usdg`, add the same flag to this execution
command. Do not change inclusion/exclusion choices between plan and execution.

Execution refuses to start while the configured tmux session exists. It uses
the configured primary/fallback providers and deliberately ignores profit
thresholds, moonbag retention, and profit banking.

Position clearing is the final commit step. After all transaction receipts
confirm, the command re-reads every managed-token balance and requires it to be
exactly zero. Only then does it create timestamped
`*.pre-liquidation.*.bak` files and atomically replace both
`data/positions.json` and `data/gridless_positions.json` with empty objects.
Any quote, approval, transaction, residual balance, or file-write failure leaves
position data uncleared. Each checkout records execution results in
`data/asset_liquidations.json`.

Fleet execution is not atomic: earlier wallets may complete before a later one
fails. Inspect receipts, residual balances, audit files, and backups before any
retry. The tmux check cannot detect a bot launched outside the configured
session, so `--confirm-fleet-stopped` remains a human safety assertion.

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

The command remains fail-fast by default. Use `--skip-errors` to inspect every
target and exclude bots with a missing/unwritable `.env` or invalid dotenv
assignment while previewing or applying to the valid remainder:

```bash
ops/fleet/update-variable --apply --allow-add --skip-errors ETH_GAS_RESERVE=0.0005
```

Skipped bots are named in the summary and receive no backup or edit. An apply
remains atomic across the valid subset. If combined with `--restart`, the
normal fleet-wide restart still includes skipped bots, which retain their old
environment.

## Backing up fleet private keys

`backup-private-keys` reads `PRIVATE_KEY` and `TOKEN_SYMBOL` from every
configured bot and creates a structured plaintext JSON backup:

```bash
ops/fleet/backup-private-keys --output "$HOME/fleet-private-keys.json"
```

The command requires an explicit output path, creates it with owner-only mode
`0600`, never prints key material, and uses exclusive creation: if anything
already exists at that path, it fails without changing the file. It validates
the entire fleet before creating the backup so a missing `.env`, missing or
duplicate field, or malformed private key cannot silently produce an
incomplete file. The resulting file contains all fleet signing authority in
plaintext; protect and remove copies accordingly.

## Removing permanently retired bots from DoomDash

`dashboard-remove` deletes one or more retired bot cards and their persisted
DoomDash status histories. It previews by default and reads `DASHBOARD_URL`
and `DASHBOARD_API_KEY` from the first configured checkout without printing or
passing the key on the command line.

Stop the retired bot processes first. Preview the exact IDs, then execute:

```bash
ops/fleet/dashboard-remove OLDCOIN ABANDONED-BOT
ops/fleet/dashboard-remove --execute --confirm-retired OLDCOIN ABANDONED-BOT
```

Use `--credentials-from NAME` when the first checkout does not contain the
shared dashboard credentials. A bot that reports again will recreate its card.
Each deletion is attempted independently; the command exits nonzero if any
target fails.

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
  each checkout's existing `.env`. A public default recipient may live in
  `fleet.conf`; private keys and API credentials never should.
- `treasury-transfer` applies the same batch guards to native ETH, USDG, and
  arbitrary ERC-20 contracts. Native transfers also enforce the configured gas
  reserve after an exact amount and estimated maximum fee. Native liquidation
  bypasses the reserve only with `--amount all --confirm-liquidate`.
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
