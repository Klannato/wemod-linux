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
| Memory read/write | `process_vm_readv` / `process_vm_writev` (no ptrace needed for values) |
| Code injection | `ptrace` (`ATTACH`/`POKEDATA`/`GETREGS`) + remote `mmap` code caves |
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

One-time kernel setting so you can attach to games you didn't spawn
(scanmem and injection both use ptrace):

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

GUI flow: launch a game → **Refresh** → **Auto-attach** (finds the PID) → pick
the game → toggle a script cheat or edit a value → or ask the agent "find 100 and
set it to 9999".

The library lands in `~/.local/share/ctfetch/` (`index.json` +
`tables/<Game>/*.ct`); the API key lives in `~/.config/ctagent/api_key`
(gitignored).

## Troubleshooting

- **`attach failed` / ptrace EPERM** — run the `sysctl` above, or launch the game
  as a child of the tool, or run as root.
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
- Direct-address value cheats read/write via `process_vm_readv` (same-uid only);
  injection needs ptrace.

## Tests

No CI suite — the harness is the two test targets, run manually:

```bash
gcc -no-pie -O0 -fcf-protection=none -z execstack -o test_inject test_inject.c
python3 test_target.py &   # scan target (holds 1234567, sets PR_SET_PTRACER)
```

The injection path (`ctaa`/`ctinject`) is exercised by running `auto_enable`
against `test_inject` — AOB scan, forward-label resolution, RIP-relative fixup,
and multi-cave allocation are all covered this way. Value apply (`ctapply`) is
verified by a read/write round-trip on a live process. The GUI is smoke-tested
headlessly (build + render + data layer); its visual layout has no automated test.
