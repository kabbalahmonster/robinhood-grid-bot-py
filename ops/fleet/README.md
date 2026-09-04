# Tmux fleet operations

These scripts operate many independently configured bot checkouts as one tmux
fleet. Each bot gets its own pane, working directory, `.env`, virtual
environment, and persistent `data/` directory. The same local fleet
configuration also drives guarded whole-fleet USDG sweeps.

For the concise production sequence—update, freeze, consolidate, redistribute,
restore capacity, and restart—see [`OPERATOR_RUNBOOK.md`](OPERATOR_RUNBOOK.md).

`start-fleet` creates panes alphabetically by bot name regardless of membership
order in `fleet.conf`, so next/previous navigation stays predictable as the
fleet grows.

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
- A Python environment containing `eth-account` (for `initialize-bots` wallet
  generation)
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
     ops/fleet/restart-fleet ops/fleet/update-fleet \
     ops/fleet/update-this-checkout ops/fleet/update-all \
     ops/fleet/usdg-sweep \
     ops/fleet/treasury-transfer ops/fleet/fund-bots ops/fleet/update-variable \
     ops/fleet/adjust-positions ops/fleet/fleet-membership \
     ops/fleet/position-capacity.py \
     ops/fleet/backup-private-keys ops/fleet/fleet-discover \
     ops/fleet/liquidate-assets ops/fleet/sell-moonbags ops/fleet/fleet-doctor \
     ops/fleet/fleet-inventory ops/fleet/fleet-audit \
     ops/fleet/dashboard-remove ops/fleet/initialize-bots \
     ops/fleet/reconcile-position-balances ops/fleet/reconcile-position-balances.py \
     ops/fleet/initialize-bot-env.py ops/fleet/fleet-watch \
     ops/fleet/fleet-watch.py
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
   ln -sf "$PWD/ops/fleet/stop-bot" "$HOME/bin/stop-bot"
   ln -sf "$PWD/ops/fleet/restart-bot" "$HOME/bin/restart-bot"
   ln -sf "$PWD/ops/fleet/update-fleet" "$HOME/bin/update-fleet"
   ln -sf "$PWD/ops/fleet/update-this-checkout" "$HOME/bin/update-this-checkout"
   ln -sf "$PWD/ops/fleet/update-all" "$HOME/bin/update-all"
   ln -sf "$PWD/ops/fleet/usdg-sweep" "$HOME/bin/usdg-sweep"
   ln -sf "$PWD/ops/fleet/treasury-transfer" "$HOME/bin/treasury-transfer"
   ln -sf "$PWD/ops/fleet/fund-bots" "$HOME/bin/fund-bots"
   ln -sf "$PWD/ops/fleet/update-variable" "$HOME/bin/update-variable"
   ln -sf "$PWD/ops/fleet/adjust-positions" "$HOME/bin/adjust-positions"
   ln -sf "$PWD/ops/fleet/fleet-membership" "$HOME/bin/fleet-membership"
   ln -sf "$PWD/ops/fleet/backup-private-keys" "$HOME/bin/backup-private-keys"
   ln -sf "$PWD/ops/fleet/fleet-discover" "$HOME/bin/fleet-discover"
   ln -sf "$PWD/ops/fleet/liquidate-assets" "$HOME/bin/liquidate-assets"
   ln -sf "$PWD/ops/fleet/sell-moonbags" "$HOME/bin/sell-moonbags"
   ln -sf "$PWD/ops/fleet/fleet-doctor" "$HOME/bin/fleet-doctor"
   ln -sf "$PWD/ops/fleet/fleet-inventory" "$HOME/bin/fleet-inventory"
   ln -sf "$PWD/ops/fleet/fleet-audit" "$HOME/bin/fleet-audit"
   ln -sf "$PWD/ops/fleet/dashboard-remove" "$HOME/bin/dashboard-remove"
   ln -sf "$PWD/ops/fleet/initialize-bots" "$HOME/bin/initialize-bots"
   ln -sf "$PWD/ops/fleet/fleet-watch" "$HOME/bin/fleet-watch"
   ln -sf "$PWD/ops/fleet/reconcile-position-balances" "$HOME/bin/reconcile-position-balances"
   ```

   Ensure `~/bin` is in `PATH`, or invoke the scripts by their repository paths.

### Phone-friendly live view

The tiled tmux window remains useful for opening an individual bot's complete
console, but dozens of panes cannot carry useful information on a narrow phone.
Run this from a second terminal tab (or detach from tmux first):

```bash
fleet-watch
```

`fleet-watch` gives every bot one row containing its name, filled/capacity
positions, best open-position P&L, native balance, session profit (when the
terminal is wide enough), and operational state. Recent errors, warnings,
capacity blocks, sell-quote waits, stale reports, and missing reports sort above
healthy bots. The display adapts at 54 columns, refreshes every two seconds, and
uses local atomic snapshots: it creates no additional RPC or dashboard traffic.

Useful variants:

```bash
fleet-watch --only prism,net
fleet-watch --interval 5
fleet-watch --once --no-color
```

The snapshots begin appearing after each updated bot completes its first round.
Use the normal tmux fleet window when an alerted bot needs full logs or keyboard
control. In `COMPACT_MODE`, each round ends with a short labelled footer such as
`------ EARN`, so the bot name remains the bottom visible line in its pane and
the correct pane is easy to identify before restarting it.

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

Use `fleet-membership` for membership-only changes. It previews by default,
accepts multiple or comma-separated names, and never edits bot folders or
processes:

```bash
fleet-membership add brodie index
fleet-membership --apply add brodie,index
fleet-membership remove pausedcoin
fleet-membership --apply remove pausedcoin oldcoin
```

Adding validates that each standard checkout already exists. Removing leaves
every checkout untouched. The applied list is stored as one replaceable managed
override in `fleet.conf`; configs using `FLEET_BOT_DIRS` are rejected.

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

### Operator command index

All commands below are shell entrypoints in `ops/fleet/`. Run them from the
operations checkout; pass `--config PATH` where supported to select a nondefault
fleet configuration. Financial and configuration mutations preview by default
unless their section explicitly says otherwise.

| Command | Purpose | Changes state? |
| --- | --- | --- |
| `fleet-discover` | Generate a guarded fleet config from checkout discovery | Prints only |
| `fleet-doctor` | Validate Git, config, RPC, contracts, providers, and dashboard | No |
| `fleet-inventory` | Read balances, positions, reserves, Git, and audit timestamps | No |
| `fleet-watch` | Phone-friendly live view from local status snapshots | No |
| `fleet-audit` | Reconcile local treasury/liquidation audit records | No |
| `start-fleet` / `stop-fleet` / `restart-fleet` | Manage the configured tmux fleet | Processes only |
| `stop-bot NAME` / `restart-bot NAME` | Stop or cleanly restart one bot pane and its complete old process tree | Processes only |
| `update-this-checkout` | Fast-forward the dedicated operations clone | Yes, Git |
| `update-fleet` / `update-all` | Fast-forward bot clones; full wrapper can restart | Yes, Git/processes |
| `initialize-bots` | Create bot clones, wallets, configs, and optional membership | `--apply` only |
| `fleet-membership` | Add/remove explicit configured bot names | `--apply` only |
| `update-variable` | Safely update selected bot `.env` values | `--apply` only |
| `adjust-positions` | Change capacity without rewriting filled positions | `--apply` only |
| `reconcile-position-balances` | Haircut overstated tracked balances to wallet reality | `--apply` only |
| `fund-bots` | Top selected wallets up from a separate treasury signer | `--execute` only |
| `usdg-sweep` | Sweep USDG to treasury | `--execute` only |
| `treasury-transfer` | Transfer native ETH, USDG, or an ERC-20 | `--execute` only |
| `sell-moonbags` | Sell unallocated managed tokens; optionally forward proceeds | `--execute` only |
| `liquidate-assets` | Convert all managed assets and conditionally clear positions | `--execute` only |
| `backup-private-keys` | Produce an owner-only plaintext recovery file | Always writes output |
| `dashboard-remove` | Remove permanently retired DoomDash bot records | `--execute` only |
| `probe-uniswap-gateway.py` | Read-only targeted Uniswap transport diagnostic | No |

Files ending in `.py` other than `probe-uniswap-gateway.py` are implementation
helpers called by these shell entrypoints. Prefer the shell command: it owns
fleet selection, stopped-process checks, previews, and batch summaries.

Start and attach:

```bash
ops/fleet/start-fleet
```

Restart one bot without touching the rest of the fleet:

```bash
ops/fleet/restart-bot hookr
```

Stop one bot and leave its pane at a clean shell prompt:

```bash
ops/fleet/stop-bot hookr
```

Do not use `Ctrl+Z` followed by Up-arrow to restart a bot. `Ctrl+Z` suspends
the Python process instead of terminating it; launching the command again then
stacks another bot process in the same pane. Repeating this consumes memory and
can freeze the host. Both per-bot commands use `tmux respawn-pane -k`, which
terminates the pane's entire prior process tree—including suspended jobs—before
stopping or launching exactly one bot. Bot selectors are case-insensitive.

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

### Update the operations checkout only

An operations clone may live outside `FLEET_BOT_ROOT` and remain absent from
`FLEET_BOT_DIRS`. That is the recommended home for the fleet scripts: it can
manage the live clones without becoming a runnable fleet member itself.

From that clone, check for an update without changing files:

```bash
ops/fleet/update-this-checkout --check
```

Then fast-forward only that clone:

```bash
ops/fleet/update-this-checkout
```

For the common full sequence—update the operations checkout, update every
configured bot, then restart only after all updates succeed—install and run:

```bash
ln -sf "$PWD/ops/fleet/update-all" "$HOME/bin/update-all"
update-all
```

The operations path defaults to
`$HOME/bot-farm/fleet-command/robinhood-grid-bot-py`.
`FLEET_COMMAND_CHECKOUT` is a shell environment variable read by `update-all`;
it is **not** a `fleet.conf` setting. The wrapper needs this path before it can
locate the repository or any fleet configuration, so putting the override in
`fleet.conf` would be too late.

Override it for one invocation without editing the script:

```bash
FLEET_COMMAND_CHECKOUT=/different/path/robinhood-grid-bot-py update-all
```

Set it for the current shell and later `update-all` commands:

```bash
export FLEET_COMMAND_CHECKOUT=/different/path/robinhood-grid-bot-py
update-all
```

For a permanent per-user override, add the export to the deployment user's
shell startup file (for example `~/.bashrc` when using Bash), then start a new
shell or source that file:

```bash
export FLEET_COMMAND_CHECKOUT=/different/path/robinhood-grid-bot-py
```

Normal deployments using the documented default path do not need to set the
variable anywhere.

The script follows its own canonical path, so the `~/bin` symlink shown during
setup still identifies the correct checkout. It never loads `fleet.conf`,
discovers or updates bot clones, or restarts processes. It refuses dirty
worktrees, detached HEADs, missing upstreams, and diverged branches; after a
fetch it performs only a fast-forward merge.

### Initialize new bot checkouts

`initialize-bots` provisions one or many independent bots using the standard
`$FLEET_BOT_ROOT/<lowercase-symbol>/$FLEET_CHECKOUT_DIRNAME` layout. It clones
the repository, copies the configured dotenv template, generates a new
wallet, and fills `PRIVATE_KEY`, `TOKEN_SYMBOL`, and `TOKEN_ADDRESS`. It never
prints private keys; only each public wallet address appears in the summary.

#### Preview versus `--apply`

The command is a dry run unless `--apply` is present:

- **Without `--apply`:** validate the template, symbols, addresses, overrides,
  destinations, fleet layout, repository, and Python interpreter; then print
  the exact plan. It does **not** create folders, clone Git, generate wallets,
  write `.env`/`wallet.txt`, or install dependencies.
- **With `--apply`:** perform that validated plan: stage the clones, generate
  wallets, create protected `.env` and `wallet.txt` files, optionally install
  dependencies, and publish the completed bot folders.

The intended workflow is to run the command once without `--apply`, review the
plan, then repeat the same command with `--apply` added. Add `--add-to-fleet` to
also append the new folder names to `FLEET_BOT_NAMES` after the full batch is
successfully created. It does not fund wallets or start/restart processes.

Preview a mixed batch in one command:

```bash
initialize-bots --template "$HOME/bot.env.template" \
  NET=0x1234567890abcdef1234567890abcdef12345678 \
  INDEX \
  OTHER=0xabcdefabcdefabcdefabcdefabcdefabcdefabcd
```

For the shortest normal command, set the default once in `fleet.conf`:

```bash
FLEET_ENV_TEMPLATE="$HOME/bot-farm/fleet-command/.env.template"
```

Then omit `--template`:

```bash
initialize-bots --add-to-fleet UP=0x1234567890abcdef1234567890abcdef12345678
```

Optionally fail closed against a fresh DoomScout verdict before any clone or
wallet is created. The public endpoint is read-only; the guard requires an
assessment no older than one hour and accepts only `PASS` (not `CAUTION`):

```bash
initialize-bots --require-scout-pass --scout-url https://doomdash.ca \
  --add-to-fleet LEMON=0xf0E17e54239CD945Cd7bEa471a3a2CA6a8C7f7A3
```

`DOOM_SCOUT_URL=https://doomdash.ca` may be configured instead of repeating
`--scout-url`. Run `/scout` or `/watch` first; missing, stale, caution, and
rejected reports all stop initialization safely.

Pass `--template PATH` whenever one batch needs a different template; the
command-line path overrides `FLEET_ENV_TEMPLATE` for that run only.

Apply shared strategy defaults to every new `.env` by repeating
`--overwrite-default NAME=VALUE`:

```bash
initialize-bots --template "$HOME/bot.env.template" --apply \
  --overwrite-default MAX_POSITIONS=6 \
  --overwrite-default POLL_INTERVAL_SECONDS=12 \
  NET INDEX OTHER
```

An override replaces one existing assignment or appends it when the template
does not contain that name. Empty values are allowed (`NAME=`). Variable names
and duplicate overrides are validated before cloning. `PRIVATE_KEY`,
`TOKEN_SYMBOL`, and `TOKEN_ADDRESS` remain managed by the initializer and
cannot be overridden through this option. Put secrets in the protected template
instead: command-line values can remain in shell history and process listings,
although secret-like values are redacted from the initializer's preview.

`NET` becomes folder `net` and `TOKEN_SYMBOL=NET`. `INDEX` becomes folder
`index` with an intentionally blank `TOKEN_ADDRESS=`. Apply the same plan with:

```bash
initialize-bots --template "$HOME/bot.env.template" --apply \
  --add-to-fleet \
  NET=0x1234567890abcdef1234567890abcdef12345678 INDEX
```

The template may already contain the three managed assignments; if one is
absent, it is appended. Duplicate definitions are rejected. `.env` and
`wallet.txt` are created with mode `0600`. Existing destination folders,
duplicate case-normalized symbols, malformed addresses, and unsafe names are
rejected before cloning anything. All clones and wallets are prepared beneath
a private staging directory; a failure leaves no published bot from that
batch. The helper uses the operations checkout's `.venv`, then `venv`, then
`python3`; use `--python PATH` to select another interpreter.

Useful options:

- `--template PATH` overrides the `FLEET_ENV_TEMPLATE` default from
  `fleet.conf` for one run.
- `--repo URL` overrides the operations checkout's `origin`.
- `--branch NAME` clones one explicit branch.
- `--install-deps` creates `.venv` and installs `requirements.txt` for each bot.
- `--add-to-fleet` atomically appends the new lowercase folder names to
  `FLEET_BOT_NAMES` after successful creation. Preview mode shows the change
  without writing it. Configs using explicit `FLEET_BOT_DIRS` are rejected.
- Repeatable `--overwrite-default NAME=VALUE` applies shared `.env` defaults to
  the entire new batch.
- `--show-private-keys` prints `SYMBOL`, public wallet address, and private key
  as a tab-separated MetaMask import list after the full batch succeeds.
- `--config PATH` uses another fleet root/layout configuration.

Private-key display is disabled by default. Only use `--show-private-keys` in a
private terminal whose scrollback and session logs are protected; the output
grants full control of every newly created wallet. With or without that flag,
each checkout retains the normal generator output at `wallet.txt` as well as
the key in `.env`, both with mode `0600`.

The command does **not** add names to `FLEET_BOT_NAMES`, start processes, fund
wallets, or guess token contracts. For every new bot, finish the remaining
`.env` settings, fill any blank token address, install dependencies if that was
not requested, back up the signing material securely, and run:

```bash
cd "$HOME/bot-farm/rh-bots/net/robinhood-grid-bot-py"
.venv/bin/python grid_bot.py --check-config
```

Only after that check succeeds should you add the lowercase folder name to
`FLEET_BOT_NAMES`, run `fleet-doctor --only net`, fund it, and start/restart
the fleet. Initialization does not create position state; never point a fresh
bot at an already-funded trading wallet with unknown holdings.

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

For the frequent stop → execute sweep → start sequence, use `--cycle-fleet`
after reviewing the normal dry run:

```bash
usdg-sweep
usdg-sweep --execute --cycle-fleet
```

Cycle mode requires the configured tmux fleet to be running, stops it, executes
the sweep, and restarts the full configured fleet detached. The restart runs
from an exit trap even if one or more wallet sweeps fail. `--execute` remains
mandatory; `--cycle-fleet` replaces the manual `--confirm-fleet-stopped`
acknowledgement because the command performs and verifies the stop itself.

## Native ETH and other fleet treasury transfers

`treasury-transfer` is the general guarded batch command. Its `--asset` may be
`ETH`, `USDG`, or an ERC-20 contract address. Exact native transfers preserve
the gas reserve. Native `available` sends all ETH above the configured reserve
and the estimated maximum transfer fee. Native `all` is a separate, explicitly
confirmed liquidation mode.

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

To consolidate every wallet's currently unreserved native ETH, preview first:

```bash
ops/fleet/treasury-transfer \
  --asset ETH \
  --amount available \
  --recipient "$TREASURY"
```

Each wallet independently sends:

```text
balance
− ETH_GAS_RESERVE
− (open position count × TREASURY_POSITION_RESERVE_ETH)
− estimated maximum transfer fee
```

`TREASURY_POSITION_RESERVE_ETH` defaults to `0`, so existing bots retain their
previous behavior until it is set in that bot's `.env`. The command counts the
active position store selected by `USE_GRIDLESS`: `data/gridless_positions.json`
in gridless mode or `data/positions.json` in classic mode. When a positive
per-position reserve applies, missing, malformed, or structurally invalid
position state or an invalid position balance refuses that bot's transfer
rather than risking an undersized reserve. Only records with `balance > 0`
count as open; unfilled classic-grid slots do not consume a reserve.

Override the per-position amount for this run only (the `.env` is not changed):

```bash
ops/fleet/treasury-transfer \
  --asset ETH \
  --amount available \
  --position-reserve-eth 0.003 \
  --recipient "$TREASURY"
```

The dry run prints the open-position count, reserve per position, total position
reserve, estimated maximum gas, and final send amount for every wallet. This
calculated mode is restricted to an externally owned recipient. After reviewing
the complete fleet plan, stop the fleet and repeat with
`--execute --confirm-fleet-stopped`. The override is valid only with native ETH
`available`; it does not affect exact transfers or the explicit `all` liquidation
mode.

Gas is floored against the latest and pending block base fees. If the RPC still rejects an
ERC-20 or native transfer before broadcast because the block base fee overtook
the prepared fee, the command uses the rejection's reported base fee plus a
surge margin, refreshes the nonce, and retries exactly once. Native `available`
and `all` modes also recalculate the send amount and
revalidate the reserve. A transaction that received a hash is never retried.

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
logic may consume different gas and invalidate the subtraction. Other failures
may consume gas without completing the transfer. Inspect every receipt before
retrying.

## Funding selected bots from treasury

`fund-bots` performs the reverse operation: it tops selected bot wallets up to
one target total ETH balance from a treasury key stored in a separate env file.
The file must be mode `600` and contain `PRIVATE_KEY`, `RPC_URL`, `CHAIN_ID`,
and `ETH_GAS_RESERVE` (or pass `--treasury-reserve`). The key is never printed.

Preview three selected wallets at a `0.013933 ETH` target:

```bash
chmod 600 ~/bot-farm/treasury.env
ops/fleet/fund-bots \
  --only bow,hookr,earn \
  --from-env ~/bot-farm/treasury.env \
  --target-balance 0.013933
```

The plan prints the derived treasury address, current destination balances,
individual top-ups, live RPC-estimated and buffered gas limits, total value,
maximum planned transfer gas, and treasury reserve. A bot already at or above
the target receives nothing. After stopping
the fleet, repeat the command with the exact source address printed by preview:

```bash
ops/fleet/stop-fleet
ops/fleet/fund-bots \
  --only bow,hookr,earn \
  --from-env ~/bot-farm/treasury.env \
  --target-balance 0.013933 \
  --confirm-source 0xExactDerivedTreasuryAddress \
  --execute \
  --confirm-fleet-stopped
```

Immediately before each transfer, the command refreshes the destination and
treasury balances, RPC gas-limit estimate, gas price, pending nonce, and reserve
check. It does not assume Ethereum's 21,000 intrinsic gas minimum is sufficient
for another EVM chain. A stale-base-fee RPC
rejection before any hash is assigned rebuilds once. Confirmed and failed
attempts are recorded in `data/fleet_funding.json`; execution stops at the first
failure because a multi-wallet funding batch is not atomic.

## Selling unallocated moonbags

`sell-moonbags` sells only the configured trading-token balance above the raw
amount allocated to positions. It accepts one coin, multiple space- or
comma-separated coins, or `all`; selectors match bot names and `TOKEN_SYMBOL`
case-insensitively:

```bash
ops/fleet/sell-moonbags CHUMP
ops/fleet/sell-moonbags CHUMP WTH Index
ops/fleet/sell-moonbags all
```

These are dry runs: each selected wallet prints its total balance, protected
position allocation, exact moonbag sale amount, provider, quote, projected
swap gas, and estimated proceeds after swap gas. To execute the same reviewed
selection:

```bash
ops/fleet/stop-fleet
ops/fleet/sell-moonbags \
  --execute \
  --confirm-fleet-stopped \
  CHUMP WTH Index
```

Execution refuses while the fleet tmux session exists. Every bot independently
fails closed on missing/malformed position state, an allocation greater than
its wallet balance, the sell gas cap, the native gas reserve, or a quote whose
output cannot cover projected transaction gas. Position files are never
changed by this command.

Add `--send-to-treasury` to both the dry run and execution commands to forward
only the sale's actual net proceeds to `FLEET_TREASURY_RECIPIENT`. WETH
settlement is unwrapped first. The workflow subtracts approval, swap, unwrap,
and treasury-transfer gas while preserving the wallet's pre-sale native ETH
and `ETH_GAS_RESERVE`; it never invokes the broader `--amount available` sweep.

```bash
ops/fleet/sell-moonbags --send-to-treasury CHUMP WTH
ops/fleet/sell-moonbags \
  --send-to-treasury \
  --execute \
  --confirm-fleet-stopped \
  CHUMP WTH

# Or process every configured bot:
ops/fleet/sell-moonbags --send-to-treasury all
ops/fleet/sell-moonbags \
  --send-to-treasury \
  --execute \
  --confirm-fleet-stopped \
  all
```

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

### Adjusting position capacity by bot name

`adjust-positions` changes position capacity relatively for one bot or a
comma-separated list. It defaults to adding one position and previewing only:

```bash
adjust-positions earn
adjust-positions earn,scopl 2
```

Apply the reviewed change, or subtract capacity with `--remove`:

```bash
adjust-positions --apply earn,scopl 2
adjust-positions --remove --apply seedcoin 1
```

Use signed `NAME=DELTA` assignments when each bot needs a different change,
including mixed additions and removals in one atomic run:

```bash
adjust-positions earn=+2 scopl=-1 hookr=+3
adjust-positions --apply earn=+2 scopl=-1 hookr=+3
```

Assignment deltas must be nonzero integers. Do not combine assignment form
with `--remove`; use a negative delta for each bot being reduced.

To freeze selected bots at their current filled-position count without
deleting any position, use `--set-to-filled`. `--all` explicitly selects the
entire configured fleet:

```bash
adjust-positions --set-to-filled --all
adjust-positions --set-to-filled --all --apply --restart
adjust-positions --set-to-filled earn,scopl --apply
```

An empty bot is set to capacity zero, which prevents its initial entry. A bot
with filled positions retains exactly enough capacity for those positions and
cannot open another one.

Add `--restart` with `--apply` to restart the configured fleet after all
changes succeed. Removal is refused if the resulting capacity would be below
the bot's currently filled-position count. The command changes only
`MAX_ACTIVE_POSITIONS`; when a legacy bot has only `MAX_POSITIONS`, that value
is used as its current capacity and a modern `MAX_ACTIVE_POSITIONS` override is
appended. The legacy value remains untouched. This also lets an empty legacy
bot freeze safely at zero without violating the positive `MAX_POSITIONS` grid
validation. The command never creates, deletes, or rewrites filled position
records. Timestamped `.env` backups are created before applying changes.

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
already exists at that path, it exits cleanly without a traceback or changing
the file. To intentionally refresh an existing backup, use explicit atomic
replacement:

```bash
ops/fleet/backup-private-keys \
  --output "$HOME/fleet-private-keys.json" \
  --overwrite
```

The replacement is written and synced to a protected temporary file before it
is moved over the old backup, so an interrupted write does not truncate the
last good copy. The command validates
the entire fleet before creating the backup so a missing `.env`, missing or
duplicate field, or malformed private key cannot silently produce an
incomplete file. The resulting file contains all fleet signing authority in
plaintext; protect and remove copies accordingly.

### Full fleet recovery set

The private-key JSON alone cannot recreate the fleet. Maintain an encrypted,
access-controlled recovery set containing:

- every checkout's `.env` and entire `data/` directory
- `ops/fleet/fleet.conf` (or the selected external `FLEET_CONFIG`)
- the Git remote, branch, and deployed commit for each checkout
- the key backup above, preferably stored separately from config/state
- host-level tmux/systemd conventions and DoomDash endpoint/DNS details

Never commit this recovery set. Verify it periodically on an isolated host:
clone one checkout, restore its `.env` and `data/`, run `--check-config`, then
run `fleet-doctor` and `fleet-inventory` without starting trading. A useful
backup must preserve file ownership/mode and decrypt successfully; merely
having an archive filename is not verification.

For a single-bot rebuild, restore into the deterministic path implied by
`FLEET_BOT_ROOT`, `FLEET_BOT_NAMES`, and `FLEET_CHECKOUT_DIRNAME`. Review the
wallet, chain, token, provider, reserve, positions, and dashboard ID before
starting it. If position state is missing for a funded wallet, stop and
reconcile on-chain assets and receipts rather than generating fresh positions.

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
- `update-fleet` preflights all repositories before changing any of them and
  reports every blocker in one pass. It refuses tracked modifications,
  detached HEADs, and branches without upstreams, and only permits
  fast-forward pulls. Untracked runtime/local files are allowed; Git still
  refuses a pull if an incoming tracked path would overwrite one.
- `update-this-checkout` uses a stricter completely-clean-worktree rule for the
  dedicated operations clone containing the script. It does not consult fleet
  membership or restart anything; keep that clone free of bot runtime files.
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
  arbitrary ERC-20 contracts. Exact native transfers preserve the gas reserve.
  Native `available` also preserves `TREASURY_POSITION_RESERVE_ETH` for every
  open position after live maximum transfer gas; `--position-reserve-eth`
  overrides it for one run. Native liquidation bypasses both reserves only with
  `--amount all --confirm-liquidate`.
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
