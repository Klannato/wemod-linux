# HANDOFF — WeMod-style clean UI over cheat tables

Repo `~/wemod-linux` (branch master, remote `Klannato/wemod-linux`). Skill: `cheat-table-scraping`.
Scope: single-player/offline only.

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
- No passwordless sudo; ptrace needs `ptrace_scope=0` or root. AA injection is blocked under default Yama — the UI must say `AA injection blocked: sudo sysctl -w kernel.yama.ptrace_scope=0` as a banner, not a stack trace. `process_vm_readv`/`writev` (value edits) needs no ptrace.
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
