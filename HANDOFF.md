# HANDOFF — WeMod-style clean UI over cheat tables

Repo `~/wemod-linux` (branch main, remote `Klannato/wemod-linux`). Skill: `cheat-table-scraping`.
Scope: single-player/offline only.

## Status — 2026-11-06: offline table/build verification
`ctcompat.py` proves whether a table matches the installed build **without
launching the game**: it reads every `assert(MODULE+offset, bytes)` out of a
table, resolves the module to the file on disk (minimal PE parse → RVA to file
offset) and compares bytes. Per table it reports matched/checked, the observed
drifts, and whether remapping is safe.

Far Cry 5 result: `0/21 asserts` — and the drifts are **not uniform**
(`+0x1050`, `+0x12f0`), so a single shift is meaningless. That is exactly why the
automatic +0x1050 patch at 20:00:43 crashed the game, and why shifted patches now
require confirmation plus an instruction-alignment check before any write.
Verdicts: matches / different build / different build (inconsistent drift) / partial.

The GUI runs this on game selection ("0/21 asserts match this build") and behind a
**Check table** button, and the shift-confirmation modal shows it.

## Status — 2026-11-06: freshness check + from-scratch discovery
`ctcheck.py` answers "is there a newer table?" before any heuristic: local table
mtimes vs the commit date of matching `.CT` files found by `gh search code`.
Queries must try the compact name ("FarCry5", not "Far Cry 5"), hits must match the
game stem (`FarCryNewDawn` is not `FarCry5`), and error bodies must never compare as
dates (tests/test_ctcheck.py). For Far Cry 5 the two online tables are older than
the local copy, so remapping is correctly the fallback.

`ctfind.py` rebuilds a cheat by observation when no table fits: `scan_value` →
guided refine (it tells the user what to do in game to change the value) →
`watch_writes` (ptrace DR0/DR7 hardware watchpoint) → `instruction_ending_at`
(back-decodes the writer; disassembling from a fixed offset desyncs on x86, so it
tries every start offset and keeps decodes landing exactly on RIP) →
`suggest_patch` ("replace `mov dword ptr [rax], edx` with NOPs").
tests/test_ctfind.py compiles a C writer fixture and verifies the whole chain,
including that the reported RIP is in executable memory.

Along the way: `ctapply.parse_maps` was dropping **anonymous mappings** (5-field
lines have no path) — i.e. every heap region, where game values live. Fixed.

## Status — 2026-11-06: remap engine (agent fallback)
`ctremap.py` re-locates a cheat when the table's address no longer exists and the
local ±0x8000 drift search is ambiguous. Evidence ladder, longest first: the hex
comment table authors paste (`//F3 0F 5E F7 …`, 20 bytes in FC5's scripts), then
DISABLE `DB` bytes, then the assert bytes. Confidence rules: unique hit across the
module's readable mappings, signature must contain the assert bytes (that locates
the patch point inside it), and nothing under 8 bytes is ever trusted. The patch
is applied at the derived address and the evidence is recorded in the handle and
logged ("re-mapped with a 20-byte hex-comment signature").

Verified in tests/test_remap.py: a mapped `FakeModule.dll` with garbage at the
table's address, a decoy assert (making drift ambiguous) and one unique 20-byte
signature — the patch lands on the signature-derived address and restores.

Not built yet, explicitly: **string/xref-driven discovery** (search memory for
"Invincible"/"GodMode"-style strings, then disassemble `.text` for
`lea reg,[rip+str]` references) and an automatic "is there a newer table?" step
before remapping. Those are the next two pieces.

## Status — 2026-11-06: table drift on Far Cry 5
The Steam-launch route works (attach readable, module-owner switch to the engine
process). The next failure is a real table/build mismatch, proven offline:
`FC_m64.dll` RVA `0x114B512` holds `de 74 0d f0` in the installed build, while the
table asserts `0f b6 f0 e8` — and that pattern does exist, at RVA `0x114C562`
(delta `+0x1050`). So the address math is correct and the table was written for a
different FC5 build.

`ctaa.apply_inplace` now re-anchors on assert mismatch: it searches ±0x8000 for the
exact byte sequence, accepts only a unique match (ambiguous ⇒ refuse), applies the
patch at the shifted address, records the shift in the handle (logged as
"table drift +0x1050"), and restores correctly. Tested in
tests/test_inplace_patch.py (patch / restore / drift / bad assert / empty script).

## Status — 2026-11-05 (late): stuck wine trees
Steam reported Far Cry 5 as running while unable to stop it. Cause: a wineserver
answers `-k` only for the exact `WINEPREFIX` string Proton used —
`…/compatdata/<appid>/pfx/` **with the trailing slash**; with any other string the
request silently does nothing (this also bit my first manual cleanup). Added
`ctlaunch.force_stop()` — signals every process whose `/proc/<pid>/environ`
carries that prefix (SIGTERM, then SIGKILL), runs `wineserver -k` and `-k9` with
the exact string, and reports survivors — plus a **Force stop** button, and the
launch timeout now force-stops the whole tree instead of only the proton parent.
Verified in `tests/test_force_stop.py`: a 10-process wine tree under a fresh prefix
is fully removed.

## Status — 2026-11-05 (late): ptrace permission settled without root
Tried the "narrow setcap'd helper" (`native/ctptrace.c`): **it cannot work**.
`PR_CAP_AMBIENT_RAISE` needs the cap in the process *inheritable* set, which only
root/CAP_SETPCAP can populate; a file capability (`+eip`) fills permitted+effective
and the exec'd child comes back unprivileged. Verified: `getcap` showed
`cap_sys_ptrace=eip` and the raise still failed with EPERM.

What works instead, with no privilege at all — put this in the game's Steam Launch
Options so a *Steam-launched* process loads the shim (correct runtime container,
correct seat):

```
LD_PRELOAD=/home/geo/wemod-linux/native/ptrace_allow.so CT_TRACER_PID_FILE=/home/geo/.cache/ct_tracer.pid %command%
```

The shim now reads the tracer pid from `~/.cache/ct_tracer.pid` (written by the
trainer on startup; the watchdog re-reads it every second) because Steam cannot
substitute a live pid. Verified with a real wine process: with `CT_TRACER_PID`
unset and only the file present, `process_vm_readv` succeeded on the wine PE
process. This also removes the 32-bit-shim problem for the *launcher* stub: the
real game process loads the shim, so the stub's `wrong ELF class` warning is moot.

Remaining privileged fallbacks, in order of least damage: `setcap cap_sys_ptrace+ep`
on the interpreter that runs the trainer (privileges every script under it), then
`sysctl kernel.yama.ptrace_scope=0` (machine-wide).

## Status — 2026-11-05 (night)
**Launch debugging added, and the Far Cry 5 blocker identified.** Every launch now
logs the plan (game/appid/install/proton/exe size), the exact command, cwd, the
relevant env (`CT_TRACER_PID`, `STEAM_COMPAT_DATA_PATH`, `SteamAppId`, LD_PRELOAD),
a poll tick every 5 s, and a diagnosis from the transcript tail — plus a full
transcript file per attempt. `ctlaunch.diagnose()` maps GLX BadAlloc / wrong ELF
class / "Failed to connect to Steam" / missing redistributable to plain-English
hints (tests/test_diagnose.py).

What the log then proved about Far Cry 5:
- a second instance hands off to an existing one, so launching while the game is
  already up looks like a hang → `launch()` now detects a running instance first
  and attaches instead;
- `bin/FarCry5.exe` is a 32-bit stub: `LD_PRELOAD … wrong ELF class: ELFCLASS64`
  (needs a `-m32` shim for that process);
- direct Proton launch dies on `X Error … BadAlloc (GLX X_GLXMakeCurrent)` —
  i.e. it needs Steam's runtime container. **So for titles like this the trainer
  cannot both launch and trace**: use **Launch via Steam** (added) and grant ptrace
  via `setcap cap_sys_ptrace+ep` or `ptrace_scope=0`, which is now the documented
  path rather than a footnote.

## Status — 2026-11-05 (evening)
**English labels added** (`cttranslate.py`). Only 335 unique non-English names
exist in the whole library; 213 are already bilingual ("Act pt. / 行動力") and
215/335 are handled by the bilingual split, 93 by a curated cheat glossary +
formula patterns ("弹匣不减" → "Unlimited Magazine", "喷射能量不减（火星DLC）" →
"Unlimited Jetpack Energy (Mars DLC)"), 21 are 2–4 Han-character proper names
left as-is on purpose. 327/335 (98%) resolved, cached in
`clean/translations.json` so a manual fix is never re-run. Remaining 8 are
Japanese source strings and two mixed-language notes — the documented `--llm`
hook is where those go if wanted.

## Status — 2026-11-05 (later)
**Clean list + WeMod-style rows are in.** `ctclean.py` builds sidecars for all 169
games with tables: 16,119 rows out of 57,849 raw entries (2,075 `high` script
cheats, 5,612 `medium`, 8,432 `low` struct junk). The GUI's cheat pane is now
grouped rows with a pill switch per standing cheat, an Apply button for one-shot
actions ("Refill …"), and an inline value box for numbers — no listbox, no
select-then-press-a-button dance.

Two defects found and fixed on the way:
- **CJK was tofu, not mojibake.** Far Cry 5's table is valid UTF-8 Chinese
  (`不被探测`, `弹匣不减`); the boxes were the Latin-only Roboto face. The UI now
  loads Noto Sans CJK and DPG 2.x builds the ranges automatically
  (`add_font_range_hint` is a deprecated no-op).
- **`ctclean` must recognise CJK keywords**, or every row in the Ubisoft tables
  scores as junk. Script presence carries most rows there anyway.

Verified: `tests/test_toggle.py` drives the same handler the switch calls and
confirms a 1/0 write into a live process plus a refusal for an unresolvable
symbol; `tests/test_gui_render.py "Far Cry 5"` renders the CJK rows and switches.
Still open: value rows whose address is an AA symbol cannot resolve (the 88%
problem below) — their switches log the reason and stay off.

## Status — 2026-11-05
**Option 1 (trainer launches the game) is built and verified.** Files: `ctlaunch.py`,
`native/ptrace_allow.c` (+ built `native/ptrace_allow.so`), `cttheme.py`,
`tests/test_ptrace_allow.py`, `tests/test_wine_preload.py`, `tests/test_theme.py`,
`tests/test_gui_render.py`; GUI restyled Material Design dark, with a
**Launch & attach** button that starts the game through Proton, waits for the PE
process, verifies memory access, then attaches.

What was measured on this box (`ptrace_scope=1`, no capabilities):

| Probe | Result |
|---|---|
| `process_vm_readv` / `/proc/pid/mem` on an unrelated process | EPERM / denied |
| same on our own child | OK — **so the old "readv needs no ptrace" note was wrong; Yama gates it** |
| same on a child that double-forked (ppid=1) | EPERM — "be the parent" is not enough |
| shim with only `PR_SET_PTRACER` in a real Wine process | **still EPERM** |
| shim with `PR_SET_PTRACER` + `PR_SET_DUMPABLE=1` + 1 Hz re-apply | **readable, 64-bit Wine processes** |

Two traps worth remembering: the Yama ptracer relation is per-task and is **not
inherited across fork** (needs `pthread_atfork`, and Wine re-marks processes
non-dumpable after our constructor runs, hence the watchdog thread); and a 64-bit
`.so` `LD_PRELOAD`ed into a 32-bit Wine process is ignored ("wrong ELF class"),
so 32-bit-only games stay untraceable until a `-m32` shim is built.

## Goal
Two workstreams, in this order:

- **A — clean tables.** A normalized sidecar JSON per game so a WeMod-style UI can show a flat, human-readable cheat list: on/off toggles for scripts and 0/1 bytes, value editors for numerics. Original `.CT` files are never modified.
- **B — address resolution (the gateway).** A value editor is worthless unless the address it writes actually belongs to the cheat. Today 88% of entries cannot be resolved at all. This is the blocking work; scheduling it second would ship a UI full of dead controls.

## Measured reality (re-derive with the probe at the bottom — do not trust this table blindly)
| Metric | Value |
|---|---|
| game dirs / `.ct` files | 172 / 268 (`148 .ct` + `120 .CT` — glob case-sensitively and you lose half) |
| `<CheatEntry>` blocks | 57,849 |
| unique (game, normalized description) | 12,198 |
| entries with `AssemblerScript` | 2,344 |
| `GroupHeader` entries (categories, not cheats) | 10,749 |
| keeps under a keyword-or-script filter | 28% (16,234) — rest are struct dumps |

An older draft of this doc said "268 games, 66,785 entries, ~13,369 unique". Wrong: 268 is the *file* count, and the 66,785 figure counted description nodes without dedupe. The planning numbers above are the real ones.

## Address taxonomy — this is the whole problem
| `<Address>` shape | entries | share | resolvable today? |
|---|---|---|---|
| symbol / label (`cheat4_store1`, `i_base_dreason_addr+[i_endings_offset]+3C`) | 50,835 | 87.9% | **no** |
| `module+offset` | 4,486 | 7.8% | yes, if the module name matches under Wine |
| none (group header, or script-only entry) | 2,272 | 3.9% | n/a |
| bare absolute hex | 256 | 0.4% | only by luck — wrong under Wine |

Pointer-path depth (`<Offsets>` child count): 36,369 single-offset, 3,450 two, 1,677 three, 502 four, 1,055 five, then a long tail up to 20. Max chain is 20 hops; assume every step can be wrong.

`ctapply.resolve()` currently does `deref(base)`, then `+off` / `deref` for `offs[:-1]`, and adds `offs[-1]` without a final deref. **That order is an assumption, not a verified fact.** It agrees with CE's convention as usually described (last-listed offset applied first), but a chain that resolves to a plausible-looking wrong value will silently write garbage. Verify against a table where the expected value is known before relying on it (Task B2 below).

### Why symbol-addressed entries fail
`<Address>cheat4_store1</Address>` is not memory — it is a name the table's own Auto Assembler script creates via `alloc(...)` + `registersymbol(...)` / `label(...)`. The address only exists once that script has run, and each run gets a fresh cave. So:
- Resolution is a *stateful* operation: run the script, remember what it allocated, keep it alive while the user edits values, free it on disable.
- A value entry can depend on an ancestor script being enabled. WeMod hides this; we must at minimum enable the parent automatically and say so in the log.
- `ctaa.py` already resolves `label`/`alloc` names and label arithmetic (`slot+0x10`, `slot-8`) internally during `run_aa_script` / `auto_enable`, but it does not expose them afterwards. Nothing to invent — just to publish.

## Workstream B — solving address resolution

**B1. Symbol registry in `ctaa.py`** (do this first; everything else is cosmetic without it)
A per-PID registry `{pid: {symbol: (addr, size, script_id, cave_alive)}}` populated as scripts run, keyed on the script that created it. Public API: `symbols(pid) -> dict`, `lookup(pid, name) -> int|None`, `free_script_symbols(pid, script_id)` on disable. Expose from `ctgui.App` through one accessor so the UI never touches ctaa internals.

**B2. Expression evaluator for `<Address>` strings**
Grammar actually seen in the corpus: `NAME`, `NAME+0x1C`, `NAME+[OTHER]+3C`, `module+0x1234`, `module+0x10+0x20`, bare hex. Nested `[...]` means "read pointer stored at OTHER". Reuse `ctaa`'s existing two-pass label arithmetic rather than writing a second parser; add the bracket form and refuse (with a reason string) anything else. Lock the deref-order question here with a test.

**B3. Module matching + PE rebasing**
`find_module()` already case-folds and prefix-matches. Add: (a) fall back to the main image (the executable's own mapping) when the named module is absent but exactly one PE image exists — Proton renames wrappers, and a 1-of-1 match is safe; (b) for bare-hex addresses, read the PE `OptionalHeader.ImageBase` from the mapped file, compute `offset = addr - ImageBase`, rebase onto the live module base. Never treat a bare hex address as absolute.

**B4. Guarded pointer walk**
Replace the bare deref loop with a walk that validates each step and returns a diagnosis instead of an integer or `None`:
- target must be in a readable mapping from `parse_maps(pid)` (region start/end),
- canonical 64-bit range, 8-byte alignment for pointer reads, non-zero,
- refuse region types that are never heap/static (guard pages, `[vvar]`, `[vsyscall]`),
- record every hop so a failure reads `step 3: 0x7f... reads 0x12 -> unmapped` in the log pane.
**Hard rule: a value editor may never write to an address that failed validation, or that resolved differently between the read and the write.** Re-walk immediately before `write_value` and compare with the address shown in the UI.

**B5. Resolution cache + build fingerprint**
`~/.local/share/ctfetch/clean/resolved/<Game>.json`: per cheat, the resolved address, the hop chain, the module base, and a fingerprint (module size + PE `TimeDateStamp` + PID start time). Invalidate wholesale when the fingerprint changes. Cuts repeated AA runs to one per game/session and makes "this table is for a different build" a reportable state rather than a mystery.

**B6. Fallback preference: AOB beats chains**
2,344 entries have scripts, and `aobscanmodule` signatures are byte-identical under Wine/Proton (only the module base differs) — that path is already proven end-to-end in `ctaa.auto_enable`. When a cheat has both a pointer chain and an AOB script, prefer the AOB/trampoline path for anything that must *stick* (god mode, no reload) and use the chain only for reading/editing displayed numbers. Document this in the UI as "script-backed" vs "chain-backed" so failures are debuggable.

**B7. Test harness**
Extend the synthetic target (`test_target.py`) to allocate: a static struct, a 3-level pointer chain, a 6-level chain with deliberately stale offsets, and an `alloc`-style cave. Tests: correct chain resolves; stale chain reports the failing hop; offset order asserted both directions (so the test fails loudly if someone flips it); 32-bit vs 64-bit pointer width; unmapped read refused; write-to-garbage impossible by construction. This is the acceptance gate for B1–B5 — no real game needed, no root, no ptrace.

## Workstream A — clean tables (only after B1–B4 land)

**A1. `ctclean.py`** — build on `ctapply.parse_ct`/`parse_entry`/`flatten` (recursive, keeps parent groups, address, offsets, script) rather than the scratch regex parser at `~/.hermes/cache/scratch/clean.py`. Output per game:

```json
{"description": "Infinite Ammo", "kind": "script", "type": "auto_assemble",
 "category": "Weapons", "source_file": "…/X.CT", "entry_id": "42",
 "address": "cheat4_store1", "offsets": ["0x10","0x28"],
 "script_lang": "aa", "confidence": "high", "resolver": "symbol"}
```

Drop `GroupHeader` entries from the cheat list but keep them as `category`. Normalize: `html.unescape`, strip leading `[hex]`, strip `#012` / `-->` / trailing arrows, collapse whitespace, discard empty / `No description` / pure-number. Dedupe by `(category, description)`, merging duplicate addresses into `addresses: []`. Keep originals untouched.

**A2. Three-tier confidence, never a hard delete**
- `high` — has `AssemblerScript`, or the description is not struct-field shaped (struct dumps are camelCase, hex-prefixed, and repeat >20× across groups in one file; `RagdollScale` alone repeats 1,373×).
- `medium` — keyword hit (`infin|unlimit|god|one.?hit|no.?reload|speed|money|credit|ammo|health|hp|damage|multiplier|max|…`) but no script.
- `low` — everything else. Still emitted, shown only behind a "Show raw entries" checkbox. A hard keyword filter loses real cheats: it dropped `God`, `Pest Control`, `Fancy Mathematics` in the sample.

**A3. LLM pass — optional, later, cheap.** Only over the `low` bucket of a couple of games, after eyeballing whether it contains real cheats. Free model ⇒ $0. If it ever needs the paid flash model, ~3k short prompts is well under $1. Nothing in A or B itself costs money.

**A4. `ctui_clean.py` (new DPG tab in `ctgui.py`)** — game list left (reuse `ctdetect.scan_games` for ✓ installed / ▶ running); right pane: attach status banner (`Attached to DOOM.exe (pid 1234)` or "Not attached — press Auto-attach"), live search, collapsible category headers, rows of `label … control`. `script` and 0/1 `Byte` ⇒ toggle; numeric ⇒ input + set; unresolvable ⇒ greyed read-only with the reason on hover. Lua (`{$lua}`) scripts get a disabled toggle and the reason inline (`ctaa` has no CE Lua engine). Reuse the existing worker-thread + queue-polled-by-frame-callback pattern; keep attach single-threaded. Per-game `state.json` so reattach re-applies previously-enabled toggles.

## Order of work
0. ~~Trace permission~~ **done** — trainer is the launcher, shim declares it as
   tracer; verified with `tests/test_ptrace_allow.py` and
   `tests/test_wine_preload.py`.
1. **B7 harness + B2 evaluator** — makes the rest testable.
2. **B1 symbol registry, B3 module/PE rebasing, B4 guarded walk** — resolution actually works.
3. **B5 cache** — keeps the UI responsive.
4. **A1/A2 `ctclean.py`**, run on BioShock 2 + DOOM + one junk-heavy table, review JSON with the user before the mass run.
5. **A4 UI on top of the resolved data**, then wire toggles/values.

Each step is its own commit; `HANDOFF.md` stays current as the resume point.

## Open questions
- Does any real game here expose the module names the tables expect? Probe one installed game under Proton and print `/proc/pid/maps` basenames vs the `aobscanmodule` argument before promising "module+offset works".
- Is `offs[-1]` really not dereferenced? Confirm with a table whose value is visible in-game (health, ammo).
- `reassemble`, CE Lua, and `@f`/`@b` anonymous labels remain unimplemented — how many of the 2,344 scripts depend on them? Cheap to count, worth counting before the UI ships.
- Should enabling a script implicitly to display a child value be opt-in (safer) or automatic (WeMod-like)? Default: automatic, logged.

## Constraints
- No passwordless sudo needed any more for the normal flow: the trainer launches the game and the shim grants trace access with `ptrace_scope` left at 1. `ptrace_scope=0` / `setcap cap_sys_ptrace+ep` are fallbacks only for attaching to a game started elsewhere. AA injection under default Yama is blocked only for those externally-started games — the UI says so in a banner, not a stack trace. `process_vm_readv`/`writev` needs the same permission (verified), so the shim gates value edits too.
- 32-bit Wine processes do not load the 64-bit shim ("wrong ELF class"); needs a `-m32` build (gcc-multilib) before 32-bit games can be traced.
- DPG 2.x installed via `--break-system-packages`. Never call `dpg.get_dearpygui_version()` — it segfaults.
- CET shadow-stack: never `setrip`; injected code runs only through a jmp hook (`ctinject` pitfall, already documented).
- Keep originals under `~/.local/share/ctfetch/tables/` byte-identical. Verify with `git status` in the repo and directory mtimes, not by eyeballing.

## Probe — regenerate every number above
```python
import re, glob, os, collections
root = os.path.expanduser('~/.local/share/ctfetch/tables')
files = [p for p in glob.glob(root + '/*/*') if p.lower().endswith('.ct')]
k = collections.Counter(); depths = collections.Counter(); tot = groups = 0
for f in files:
    for e in re.findall(r'<CheatEntry>(.*?)</CheatEntry>',
                        open(f, encoding='utf-8', errors='replace').read(), re.S):
        tot += 1
        groups += '<GroupHeader>1</GroupHeader>' in e
        a = re.search(r'<Address>(.*?)</Address>', e, re.S)
        a = a.group(1).strip() if a else None
        if a is None: k['none'] += 1
        elif re.fullmatch(r'[^+\s]+[+][0-9A-Fa-fx+]+', a): k['module+offset'] += 1
        elif re.fullmatch(r'[0-9A-Fa-f]+', a): k['bare hex'] += 1
        else: k['symbol'] += 1
        if '<Offsets>' in e: depths[len(re.findall(r'<Offset>', e))] += 1
print(len(files), tot, groups, k, dict(sorted(depths.items())))
```
