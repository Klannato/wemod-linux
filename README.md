# wemod-linux

A WeMod-style cheat trainer for Linux — built from Cheat Engine (`.ct`) tables
found on GitHub, applied to running games with a native Linux memory engine,
plus a free-LLM "agent" that finds values for you.

**Single-player / offline use only.** This edits your own game process memory;
it does not bypass DRM and must not be used in multiplayer.

## Goals

1. **Collect** — scrape Cheat Engine tables for as many games as possible from
   GitHub collection repos, indexed locally.
2. **Apply** — parse a `.ct` table, resolve each cheat's address (absolute,
   `module+offset`, or pointer path) in a live process, and read/write it.
3. **Scan** — find an *unknown* value by narrowing (search → changed/increased →
   set), via `scanmem`.
4. **Inject** — translate Auto Assembler scripts (AOB scan + code-cave + `jmp`
   hook) so the "script" cheats (god-mode, inf-ammo) work, not just value edits.
5. **Automate** — a free LLM agent (OpenRouter) turns a natural-language goal
   into a scan plan, and process detection finds the game's PID for you.

## Tech

Python 3, no daemons, no root (except one `sysctl` for ptrace — see below).

| Layer | Library / technique |
|---|---|
| Memory read/write | `process_vm_readv` / `process_vm_writev` (same-uid; still gated by Yama — see below) |
| Code injection | `ptrace` (`ATTACH`/`POKEDATA`/`GETREGS`) + remote `mmap` code caves |
| Trace permission | `native/ptrace_allow.so` — `PR_SET_PTRACER` + `PR_SET_DUMPABLE` from inside the game |
| AOB scanning | own compiler (`??` / `*` wildcards) over `/proc/<pid>/maps` |
| Assembly / disasm | `keystone` (assemble) + `capstone` (RIP-relative fixup) |
| Scan mode | `scanmem` driven over a pty |
| LLM agent | OpenRouter free models via `urllib` (no SDK) |
| Process / installed detection | `/proc` scan + Steam `appmanifest_*.acf` |
| GUI | Dear PyGui |

## Files

| File | Role |
|---|---|
| `ctfetch.py` | Table scraper + local library (`index`/`list`/`search`/`fetch`/`library`) |
| `github_fallback.py` | Rate-limited GitHub code-search fallback for games missed by the repos |
| `ctapply.py` | Apply engine: parse `.ct`, resolve addresses, read/write memory |
| `ctscan.py` | `scanmem` driver (search / filter / set / lock) |
| `ctaa.py` | AOB scan + AA script parser + `run_aa_script` (text → live injection) |
| `ctinject.py` | ptrace injection primitives + keystone + remote `mmap` code caves |
| `ctagent.py` | OpenRouter free-model agent (`scan` / `translate`) |
| `ctdetect.py` | Running/installed-game detection (PID finder + Steam library) |
| `ctclean.py` | Normalizes `.ct` tables into a clean per-game cheat list (JSON sidecar) |
| `cttranslate.py` | English labels for non-English cheat names (offline, cached) |
| `ctcheck.py` | Is a newer table available? (`gh` code search + commit dates) |
| `ctremap.py` | Relocate a cheat from the table's own signature |
| `ctfind.py` | Build a cheat from scratch: value scan → refine → write watchpoint |
| `ctlaunch.py` | Launches a Steam game under Proton with the tracer shim loaded |
| `ctwidgets.py` | Custom controls (the pill switch Dear PyGui lacks) |
| `cttheme.py` | Material Design (dark) theme + type scale for the GUI |
| `native/ptrace_allow.c` | `LD_PRELOAD` shim: lets the trainer trace a game it launched |
| `ctgui.py` | Dear PyGui front end (games, cheats, log, agent chat) |
| `test_inject.c` / `test_target.py` | Test targets for the injection and scan layers |

## Install

```bash
# system deps
sudo apt install scanmem gcc

# python deps (into whatever venv you use — PEP 668 needs the flag)
python3 -m pip install --break-system-packages keystone-engine capstone dearpygui

# OpenRouter key for the agent (optional; value cheats work without it)
mkdir -p ~/.config/ctagent && echo "sk-or-..." > ~/.config/ctagent/api_key
```

One-time build of the tracer shim (no root, no sysctl):

```bash
gcc -shared -fPIC -O2 -o native/ptrace_allow.so native/ptrace_allow.c -lpthread
```

Why it is needed: Ubuntu ships `kernel.yama.ptrace_scope=1`, so a process may only
be traced by its own descendants — and `process_vm_readv` is gated by the same
check. Wine's `wineserver` reparents the game out of our subtree, so "launch it
as a child" is not enough. The shim is `LD_PRELOAD`ed into the game by
`ctlaunch.py`; it declares the trainer as its tracer with
`prctl(PR_SET_PTRACER, ...)` and restores `PR_SET_DUMPABLE` (Wine clears it),
which the kernel honours with `ptrace_scope` still at 1.

Optional, only if you want to attach to games started *from Steam* instead of
from the trainer — this weakens the machine for every app you run:

```bash
sudo sysctl kernel.yama.ptrace_scope=0
```

## Usage

```bash
python3 ctfetch.py index                                   # build the table index
python3 ctfetch.py fetch "pal world"                       # download a game's tables
python3 ctgui.py                                           # the GUI

# or piecewise:
python3 ctfetch.py search "no man's sky"
python3 ctapply.py list ~/.local/share/ctfetch/tables/"No Man's Sky"/some.CT
python3 ctapply.py modules <PID>                           # list module bases
```

Clean the tables once (169 sidecars, ~16k rows out of 57k raw entries):

```bash
python3 ctclean.py                # every game -> ~/.local/share/ctfetch/clean/<Game>.json
python3 ctclean.py --game "Far Cry 5"
python3 ctclean.py --stats
```

Translate the non-English names once (offline; 335 unique strings, 98% resolved):

```bash
python3 cttranslate.py --stats    # coverage
python3 cttranslate.py --apply    # writes description_en into the sidecars
python3 cttranslate.py --review   # what stayed untranslated
```

Each cheat gets a confidence: `high` (has an Auto Assembler script), `medium`
(cheat-like name, English or CJK), `low` (struct field). The GUI shows high and
medium; `Show raw entries` reveals the rest. Originals are never modified.

GUI flow: pick a game → **Launch & attach** (starts it through Proton with the
tracer shim, waits for the process, verifies memory access, attaches) → toggle a
script cheat or edit a value → or ask the agent "find 100 and set it to 9999".

For titles that only work under Steam's runtime (Far Cry 5 dies with
`X Error … BadAlloc (GLX)` when started through Proton directly), put this in the
game's **Launch Options** (Steam → right-click → Properties → Launch Options) and
then start the game normally from Steam:

```
LD_PRELOAD=/home/geo/wemod-linux/native/ptrace_allow.so CT_TRACER_PID_FILE=/home/geo/.cache/ct_tracer.pid %command%
```

The shim reads the trainer's pid from that file (Steam can't substitute a live pid
at launch time), so the Steam-launched game declares the trainer as its tracer —
no sudo, no `setcap`, `ptrace_scope` stays at 1. `ctlaunch.steam_launch_options()`
prints the current string, and the GUI shows it in the log whenever an attach is
refused.

**Launch via Steam** asks the Steam client to start the game. Use it for titles
that need Steam's runtime container — running Proton directly makes them die with
`X Error … BadAlloc (GLX)`, which Far Cry 5 does. The catch: Steam's child has no
tracer shim, so attaching to it needs `sudo setcap cap_sys_ptrace+ep` on a helper
or `sudo sysctl kernel.yama.ptrace_scope=0`.

Every launch writes a transcript to
`~/.local/share/ctfetch/logs/launch-<Game>-<timestamp>.log` (the game's own
stdout/stderr included) and echoes a running commentary — command, env, poll
ticks every 5 s, and a diagnosis of the known failure patterns — into the GUI log.

The library lands in `~/.local/share/ctfetch/` (`index.json` +
`tables/<Game>/*.ct`); the API key lives in `~/.config/ctagent/api_key`
(gitignored).

## Troubleshooting

- **`attach failed` / ptrace EPERM** — expected for a game started outside the
  trainer: `ptrace_scope=1` only allows descendants, and Wine daemonises the
  game away from you. Use **Launch & attach**. If you must attach to an
  already-running Steam game, either `sudo sysctl kernel.yama.ptrace_scope=0`
  (machine-wide) or `sudo setcap cap_sys_ptrace+ep` on a narrow helper.
- **Steam can't stop a game / it stays "running"** — the wineserver only answers
  `-k` for the exact `WINEPREFIX` string Proton launched it with
  (`…/compatdata/<appid>/pfx/`, trailing slash included). Use **Force stop** in the
  GUI: it signals every process carrying that prefix (SIGTERM → SIGKILL) and then
  runs `wineserver -k` / `-k9` with the right string. A launch that times out does
  this automatically.
- **32-bit games** — `LD_PRELOAD` of a 64-bit `.so` into a 32-bit Wine process is
  ignored ("wrong ELF class"), so those processes stay untraceable. Needs a
  `-m32` build of `native/ptrace_allow.c` (gcc-multilib) and a 32-bit shim path.
- **`AOB not found`** — the table targets a different game build than the one
  installed. Cheat tables are version-specific; try another table or a newer one.
- **Agent returns nothing / errors** — check `~/.config/ctagent/api_key`; the
  free models rotate and some are retired (the code falls back across several).
- **`Installed only` is empty** — you have few Steam games installed right now;
  install one and hit Refresh.

## Where tables come from

The scraper walks public GitHub cheat-table repos (seed: `MBRKiNG/CE-Tables`,
`bbfox0703/Mydev-Cheat-Engine-Tables`, `atul1503/Cheat-Engine-Tables`,
`Hexorg/CheatEngineTables`) plus GitHub code search for the rest. The canonical
source, FearlessRevolution, is login + Cloudflare-walled and not used. Downloads
are validated as real `CheatTable` XML before being accepted.

## Limitations (named)

- AA translation covers the value-write/trampoline shape (AOB scan, code caves,
  `readmem`, label arithmetic, RIP-relative fixup, `db` restore). **Not** handled:
  Lua (`{$lua}`) scripts — they need Cheat Engine's Lua engine; `reassemble`;
  `@f`/`@b` anonymous-label branches.
- AOB signatures are build-specific; a game patch breaks them until the table is
  updated.
- Direct-address value cheats read/write via `process_vm_readv`; the kernel gates
  that on the same check as ptrace, so the game must have been launched by the
  trainer (or ptrace relaxed).
- 88% of table entries address memory by an Auto Assembler *symbol* that only
  exists once the table's own script has run — see `HANDOFF.md` for the planned
  symbol registry.

## When a table is incompatible

Order of escalation, cheapest and most reliable first:

```bash
python3 ctcheck.py "Far Cry 5"            # 1. is there a newer table? (--fetch to grab it)
# 2. automatic: ctremap re-anchors from the table's own signature (assert + comment bytes)
python3 ctfind.py <PID> int32 health     # 3. no table? rebuild the cheat by observation
```

`ctfind` walks you through the human workflow: it scans for the value you see in
game, tells you what to do to change it ("take damage until health drops", "buy or
sell something"), refines the candidate list with each new value, then sets a
hardware watchpoint on the survivor and reports the instruction that writes it
(`mov dword ptr [rax], edx`) plus the patch it suggests. Watchpoints need ptrace,
i.e. a game launched by the trainer.

## Tests

No CI suite — everything runs manually:

```bash
# tracer shim: control (denied) vs shimmed (allowed), fork and exec shapes
python3 tests/test_ptrace_allow.py

# shim inside a real Wine process (uses Proton's bundled wine, throwaway prefix)
python3 tests/test_wine_preload.py

# Material theme + font loading, headless (no viewport)
python3 tests/test_theme.py

# GUI renders N frames then exits (needs a display); optional 2nd arg = preselect a game
python3 tests/test_gui_render.py 120 "Far Cry 5"

# toggle path: writes 1/0 into a live child process and refuses bad addresses
python3 tests/test_toggle.py

# scan target for the injection layer
gcc -no-pie -O0 -fcf-protection=none -z execstack -o test_inject test_inject.c
python3 test_target.py &
```

`test_ptrace_allow.py` is the load-bearing one: it proves the control case is
refused and that both detach shapes (fork+exec, fork-only) become traceable, and
it fails loudly if someone "simplifies" the shim back to a bare `PR_SET_PTRACER`.

The injection path (`ctaa`/`ctinject`) is exercised by running `auto_enable`
against `test_inject` — AOB scan, forward-label resolution, RIP-relative fixup,
and multi-cave allocation are all covered this way. Value apply (`ctapply`) is
verified by a read/write round-trip on a live process. The GUI is smoke-tested
headlessly (build + render + data layer); its visual layout has no automated test.
