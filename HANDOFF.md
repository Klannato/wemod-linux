# HANDOFF — WeCheat

Repo `~/wemod-linux` (branch **main**, remote `Klannato/wemod-linux`, collaborator Sam-GG).
UI name is **WeCheat**; modules keep the old `ct*` prefix. Scope: single-player/offline only.
Skill: `cheat-table-scraping`.

Resume:

```bash
cd ~/wemod-linux && git pull
python3 ctgui.py                                    # the trainer
python3 tests/test_gui_render.py 900 "Far Cry 5"    # headless render, no game needed
```

## What this is

A Linux WeMod equivalent over scraped Cheat Engine tables: read a game's `.CT` files,
normalise them into a per-game sidecar with human names, render as toggles and value
editors, write into the live process (ptrace; `process_vm_readv/writev`, in-place
byte patches, AOB scans, hardware watchpoints).

Original `.CT` files are never modified — all derived state lives under `~/.local/share/ctfetch/`.

## Measured reality (do not re-derive)

| Fact | Value |
|---|---|
| Game dirs / `.ct` files | 172 / 268 (148 `.ct` + 120 `.CT`) |
| `<CheatEntry>` total | 57,849 → 12,198 unique (game, description) |
| Entries with `AssemblerScript` | 2,344 |
| `GroupHeader` rows | 10,749 |
| Address shapes | 7.8% `module+offset`, **87.9% AA-script symbols**, 3.9% none, 0.4% bare hex |
| Pointer depth | 36,369 ×1 offset … tail to 20 hops |
| Non-English names | 335 unique → 327 translated offline (98%) |
| Sidecars | 169 in `clean/` |

The 87.9% figure is the core constraint: most entries name a symbol that only exists
after the table's own AA script has allocated it.

## Architecture

| File | Role |
|---|---|
| `ctgui.py` | Dear PyGui app: game list, cheat rows, log pane, Find cheat panel, Troubleshoot wizard |
| `cttheme.py` / `ctwidgets.py` | Material dark theme, type scale, fonts; `Switch` pill toggle |
| `ctdetect.py` | Running games (Steam app list × `/proc`) |
| `ctlaunch.py` | Proton/Steam launch, `find_exe`, diagnosis, transcripts, tracer-pid file, `force_stop` |
| `ctapply.py` | Attach, module bases, `process_vm_readv/writev`, value/pointer writes, maps parsing |
| `ctaa.py` | AA enable/disable: `aobscanmodule` **and** in-place `assert` patches |
| `ctremap.py` | Evidence-driven re-anchor when the table's address is gone (hex-comment signature → DISABLE bytes → assert) |
| `ctclean.py` | Table → sidecar (`confidence`, `kind`, `description_en`); re-applies translation cache |
| `cttranslate.py` | 335 non-English names → English (bilingual split + glossary), cached |
| `ctcompat.py` | **Table vs installed build**, offline: scores every assert against the DLL on disk |
| `ctcheck.py` | Is a newer table published? (`gh search code` + commit dates) |
| `ctfind.py` | Rebuild a cheat by observation: scan → refine → watchpoint → NOP patch |
| `native/ptrace_allow.c` | `LD_PRELOAD` shim so the tracer needs no root |

## Verified working

- **Attach without root** — shim sets `PR_SET_PTRACER` + `PR_SET_DUMPABLE`, re-applied
  from a 1 Hz watchdog and a `pthread_atfork` hook. Far Cry 5 via Steam: pid 1254249,
  `shim=True trace=(True,'readable')`, `module_bases → 242`. `ptrace_scope` stays 1,
  `getcap` clean, no privilege on disk.
- **In-place AA patches** — `assert(MODULE+OFF, bytes)` + `MODULE+OFF:` block; asserts
  verified before any write; originals remembered; patch must begin one instruction of
  exactly the patch length (capstone) or it refuses.
- **Watchpoint → NOP patch** — freeze/unfreeze proven in `tests/test_ctfind.py`.
- **Offline build check** — `ctcompat.py "Far Cry 5"` → `0/21 asserts, different build
  (inconsistent drift)`, drifts `+0x1050` **and** `+0x12f0`.
- **Troubleshoot wizard** — check build → search+score online candidates → install best → or
  hand off to Find cheat.
- **Translations survive rebuilds** — `ctclean.apply_translations` re-attaches `description_en`
  on every sidecar write (losing them once made Chinese names reappear).

## Far Cry 5, the worked example

- Local table (`ragnaroks…FarCry5…ct`) was built for **another build**: 0/21 asserts match
  and the drifts are non-uniform, so no single shift is trustworthy. The game crashed at
  20:00:43 because an automatic `+0x1050` patch was applied anyway.
- Guards added: confirmation modal for any shift, instruction-alignment check, and a 6 s
  watchdog that records the offending cheat to `clean/unsafe.json`.
- Online candidates (`new_FarCry5.CT`, `atul1503`, `grasmanek94`) are **AOB-based** — zero
  fixed addresses, therefore build-independent — and are what the list now uses:
  68 cheats, 22 high confidence, e.g. `Unlimited Health`, `No Recoil`, `No Fall From Jump`.

## Known gaps (ordered by value)

1. **Symbol registry** (`ctaa`): publish `{pid: {symbol: (addr, size, script_id)}}` so value
   rows addressed by AA symbols can resolve. Unlocks the 87.9%. Nothing to invent — `ctaa`
   already resolves labels during a run, it just never exposes them.
2. **Pointer chains** at runtime: `pointerPaths` are parsed, not followed; value editors need
   a guarded walk (validate each hop against `parse_maps`, re-walk before writing).
3. **Cached resolution per build** (`clean/resolved/<Game>.json`, fingerprint = module size +
   PE `TimeDateStamp` + pid start) so AA runs happen once per session.
4. **`createthread` / CE Lua scripts** unsupported (guarded with a clear message).
5. **Watchpoints are per-thread** — a writer on a worker thread reports "no write within 30 s".
6. **32-bit Wine games** can't load the 64-bit shim (needs `-m32` gcc-multilib).
7. **Cheat list noise** — e.g. 29 meaningless `Health Pointer N` rows; collapse or hide by default.

## Conventions that must not regress

- "if i say error you should just check the log" — read
  `~/.local/share/ctfetch/logs/trainer.log` first and quote the line before theorising.
- **Never** auto-apply a shifted patch; confirmation is mandatory.
- Originals untouched; only sidecars/caches are written under `ctfetch/`.
- No sudo, no capabilities, `ptrace_scope` stays 1.
- DPG 2.x traps: `get_dearpygui_version()` segfaults; `add_font_range_hint` and
  `set_font_scale` are deprecated no-ops (themes must set `FontSize`); `add_font(tag=None)`
  raises `SystemError`; build widgets with explicit `parent=` (nesting `with` inside a pushed
  container corrupts the stack → error 1003/1009).
- CJK needs the Noto CJK face — a Latin-only font turns every Chinese cheat name into boxes.

## Environment

- DPG 2.x via `pip --break-system-packages`; `DISPLAY=:1`, `WAYLAND_DISPLAY=wayland-1`.
- Steam launch options, so a Steam-launched game can be traced:
  `LD_PRELOAD=/home/geo/wemod-linux/native/ptrace_allow.so CT_TRACER_PID_FILE=/home/geo/.cache/ct_tracer.pid %command%`
- Shim build: `gcc -shared -fPIC -O2 -Wall -Wextra -o native/ptrace_allow.so native/ptrace_allow.c -lpthread`
- Tests (11): ptrace, wine preload, theme, gui render, toggle, diagnose, launch exe,
  inplace patch, ctfind, ctcheck, ctcompat.
- Runtime artifacts: `clean/*.json` (+ `translations.json`, `unsafe.json`),
  `candidates/<Game>/*.CT` (scored downloads), `logs/trainer.log`, `~/.cache/ct_tracer.pid`.

## CLI quick reference

```bash
python3 ctclean.py                        # rebuild every sidecar (--game "Far Cry 5" for one)
python3 cttranslate.py --apply            # refresh English labels from the cache
python3 ctcompat.py "Far Cry 5"           # table vs installed build, offline
python3 ctcompat.py "Far Cry 5" --online  # + score online candidates
python3 ctcheck.py "Far Cry 5"            # is a newer table published?
```

## Probe — regenerate the table above

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
