#!/usr/bin/env python3
"""ctlaunch — start a Steam/Proton game *from the trainer* so it can be traced.

The trainer is the launcher, and the launcher is the point: on this machine
kernel.yama.ptrace_scope=1 lets a process trace only its own descendants, and
``process_vm_readv`` is gated by the same check. Wine's wineserver promptly
daemonises the game out of our subtree, so being the parent is not enough.
What works (verified in tests/test_ptrace_allow.py) is the game declaring us as
its tracer: LD_PRELOAD a tiny shim whose constructor calls
``prctl(PR_SET_PTRACER, our_pid)``. The game then stays traceable however Wine
reparented it, with no root, no setcap, and no change to ptrace_scope.

Env the shim needs:
    LD_PRELOAD=<abs>/native/ptrace_allow.so
    CT_TRACER_PID=<our pid>

CLI:
    python3 ctlaunch.py --list
    python3 ctlaunch.py --launch "Helldivers 2"
    python3 ctlaunch.py --self-test
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import ctdetect as D

HERE = Path(__file__).resolve().parent
SHIM = HERE / "native" / "ptrace_allow.so"
# The shim can learn the tracer pid from this file, which is what makes the
# Steam-launch-options route work: Steam cannot substitute a pid at launch time.
TRACER_PID_FILE = Path.home() / ".cache" / "ct_tracer.pid"


def write_tracer_pid(pid: int | None = None) -> Path:
    """Publish our pid for shims loaded into processes we did not spawn."""
    TRACER_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    TRACER_PID_FILE.write_text(str(pid or os.getpid()))
    return TRACER_PID_FILE


def steam_launch_options() -> str:
    """The string to paste into a Steam game's Launch Options (no root needed)."""
    return (f"LD_PRELOAD={SHIM} CT_TRACER_PID_FILE={TRACER_PID_FILE} %command%")


# ---------------------------------------------------------------- Steam layout
def steam_libraries() -> list[Path]:
    """Every steamapps dir reachable from the known Steam roots."""
    out: list[Path] = []
    for root in D._STEAM_ROOTS:
        sa = root / "steamapps"
        if sa.exists():
            out.append(sa)
        vdf = sa / "libraryfolders.vdf"
        if vdf.exists():
            try:
                for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(errors="replace")):
                    other = Path(m.group(1)) / "steamapps"
                    if other.exists():
                        out.append(other)
            except OSError:
                pass
    seen, unique = set(), []
    for sa in out:
        if sa not in seen:
            seen.add(sa)
            unique.append(sa)
    return unique


def apps() -> list[dict]:
    """Installed Steam apps: {appid, name, installdir, path, steamapps}."""
    found = []
    for sa in steam_libraries():
        for acf in sa.glob("appmanifest_*.acf"):
            try:
                txt = acf.read_text(errors="replace")
            except OSError:
                continue
            appid = re.search(r'"appid"\s+"(\d+)"', txt)
            name = re.search(r'"name"\s+"([^"]+)"', txt)
            installdir = re.search(r'"installdir"\s+"([^"]+)"', txt)
            if not (appid and name and installdir):
                continue
            path = sa / "common" / installdir.group(1)
            found.append({
                "appid": appid.group(1),
                "name": name.group(1),
                "installdir": installdir.group(1),
                "path": path,
                "steamapps": sa,
            })
    return found


def find_app(game: str) -> dict | None:
    """Match a table's game name to an installed app: exact, then prefix, then tokens."""
    target = D._norm(game)
    catalogue = apps()
    for app in catalogue:
        if D._norm(app["name"]) == target:
            return app
    for app in catalogue:
        if D._norm(app["name"]).startswith(target) or target.startswith(D._norm(app["name"])):
            return app
    words = [w for w in re.split(r"[^a-z0-9]+", game.lower()) if len(w) > 3]
    for app in catalogue:
        norm = D._norm(app["name"])
        if words and all(w in norm for w in words):
            return app
    return None


# ---------------------------------------------------------------- Proton & exe
def find_proton() -> Path | None:
    """Newest usable Proton under any library (Experimental preferred)."""
    candidates: list[Path] = []
    for sa in steam_libraries():
        for d in (sa / "common").glob("Proton*"):
            exe = d / "proton"
            if exe.is_file() and os.access(exe, os.X_OK):
                candidates.append(exe)
    if not candidates:
        return None

    def rank(p: Path) -> tuple:
        name = p.parent.name.lower()
        version = re.findall(r"\d+", name)
        return ("experimental" in name, [int(v) for v in version], str(p))

    return max(candidates, key=rank)


EXE_BLACKLIST = re.compile(
    r"installer|install|setup|redist|vcredist|vc_redist|dxsetup|dxwebsetup|dotnet|"
    r"unins|crashreport|crashpad|breakpad|ubisoftconnect|uplay|eac|easyanticheat|"
    r"battleye|steamworks|touchup|cleanup|activate", re.I)


def _usable(p: Path) -> bool:
    return p.is_file() and not EXE_BLACKLIST.search(p.stem)


def find_exe(app: dict, game: str) -> Path | None:
    """The game's main executable.

    "Largest .exe under the install dir" is wrong and was how Far Cry 5 got
    launched as its 262 MB Ubisoft Connect *installer* instead of the 215 KB
    `bin/FarCry5.exe` stub. Order: the name the cheat tables target → the name
    matching the install directory → anything under bin/ → largest remaining
    non-blacklisted. Installers/redistributables are excluded outright.
    """
    if not app["path"].exists():
        return None
    exes = []
    for p in app["path"].rglob("*.exe"):
        try:
            if p.stat().st_size > 20_000 and _usable(p):
                exes.append(p)
        except OSError:
            continue
    if not exes:
        return None

    wanted = {n.lower() for n in D.game_exe_names(game)}
    for pool in (
        [p for p in exes if p.stem.lower() in wanted or p.name.lower() in wanted],
        [p for p in exes if p.stem.lower() == app["installdir"].lower()],
        [p for p in exes if any(w and w in p.stem.lower() for w in wanted)],
        [p for p in exes if p.parent.name.lower() in ("bin", "binaries", "game", "win64")],
        exes,
    ):
        if pool:
            return max(pool, key=lambda p: p.stat().st_size)
    return None


def blacklisted_exes(app: dict) -> list[str]:
    """What got skipped — shown when a launch target cannot be found."""
    if not app["path"].exists():
        return []
    return sorted(p.name for p in app["path"].rglob("*.exe")
                  if p.is_file() and EXE_BLACKLIST.search(p.stem))


# ---------------------------------------------------------------- environment
def launch_env(app: dict | None, tracer_pid: int | None = None) -> dict:
    """Proton + tracer environment for one app (pure, so it can be tested)."""
    env = dict(os.environ)
    if app is not None:
        steam_root = app["steamapps"].parent
        compat = app["steamapps"] / "compatdata" / app["appid"]
        compat.mkdir(parents=True, exist_ok=True)
        env.update({
            "STEAM_COMPAT_CLIENT_INSTALL_PATH": str(steam_root),
            "STEAM_COMPAT_DATA_PATH": str(compat),
            "SteamAppId": app["appid"],
            "SteamGameId": app["appid"],
            "SteamClientLaunch": "1",
        })
    if SHIM.exists():
        old = env.get("LD_PRELOAD", "")
        env["LD_PRELOAD"] = f"{SHIM} {old}".strip()
        env["CT_TRACER_PID"] = str(tracer_pid or os.getpid())
    return env


def trace_ok(pid: int) -> tuple[bool, str]:
    """Can we actually read the target's memory? (the only proof that matters)"""
    import ctapply as C
    try:
        maps = C.parse_maps(pid)
        readable = [m for m in maps if m["perms"].startswith("r")]
        if not readable:
            return False, "no readable mappings"
        C.read_mem(pid, readable[0]["start"], 4)
        return True, "readable"
    except OSError as exc:
        return False, f"{exc.strerror} (errno {exc.errno})"
    except Exception as exc:  # malformed maps, dead pid
        return False, str(exc)


def pids_for_exe(exe_name: str) -> list[int]:
    """PIDs that have this executable *mapped* — i.e. the process running it.

    Matching the cmdline is not enough and actively misleading: our own shell
    commands mention the exe path, and Proton's `steam.exe` wrapper carries it as
    an argument while the game binary has not loaded yet. A mapped-file hit means
    the PE image is actually loaded (wine maps it into the host process).
    """
    needle = exe_name.lower()
    out = []
    me = os.getpid()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == me:
            continue
        try:
            maps = (entry / "maps").read_text(errors="replace")
        except OSError:
            continue
        low = maps.lower()
        if needle in low and ("wine" in low or "/steamapps/" in low):
            out.append(pid)
    return out


def _proc_cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return ""


def _proc_comm(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text(errors="replace").strip()
    except OSError:
        return ""


def game_pid(exe_hint: Path | None, game: str, exclude: int | None = None) -> tuple[int, str] | None:
    """Find the running instance: (pid, how) where how says which signal matched.

    Three signals, cheapest first:
      *table*   — the cheat table names the game's exe (needs an aobscanmodule)
      *mapped*  — a process has the launched exe mapped (wine maps the PE image)
      *cmdline* — a wine process was started with that exe as its command; the
                  PE process shows up as `Z:\\…\\Game.exe`, while Proton's own
                  `steam.exe` wrapper is excluded by name
    """
    pids = [p for p in D.running_pids(game) if p != exclude]
    if pids:
        return pids[0], "table"
    if exe_hint is None:
        return None
    pids = [p for p in pids_for_exe(exe_hint.name) if p != exclude]
    if pids:
        return pids[0], "mapped"
    needle = exe_hint.name.lower()
    for pid in pids_for_cmdline(needle, exclude=exclude):
        return pid, "cmdline"
    return None


def pids_for_cmdline(needle: str, exclude: int | None = None) -> list[int]:
    """Wine PE processes started with this executable (skips Proton's wrapper)."""
    out = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == exclude:
            continue
        cmd = _proc_cmdline(pid).lower()
        if needle not in cmd:
            continue
        comm = _proc_comm(pid).lower()
        if comm.startswith("steam.exe") or comm.startswith("python"):
            continue
        out.append(pid)
    return out


def wait_for_game(game: str, child: subprocess.Popen, timeout: float = 180.0,
                  exe_hint: Path | None = None, log=None) -> int | None:
    """Poll for the game's PE process; its tree may be reparented by wineserver.

    Cheat-table exe names only exist when a table has an `aobscanmodule` (Far
    Cry 5's is symbol-based, so it has none) — hence the exe_hint fallbacks.
    Logs a progress line every 5 s so a slow boot (or a modal launcher dialog)
    is visible instead of looking like a hang.
    """
    deadline = time.time() + timeout
    started = time.time()
    tick = 0
    while time.time() < deadline:
        found = game_pid(exe_hint, game, exclude=child.pid)
        if found:
            if log:
                log(f"  game process found: pid {found[0]} (matched by {found[1]}) "
                    f"after {time.time() - started:.1f}s")
            return found[0]
        if child.poll() is not None:
            if log:
                log(f"  launcher exited after {time.time() - started:.1f}s "
                    f"(no game process appeared)")
            return None
        waited = time.time() - started
        if log and int(waited) // 5 > tick:
            tick = int(waited) // 5
            log(f"  waiting for {exe_hint.name if exe_hint else game}… {waited:.0f}s "
                f"(wine procs: {sum(1 for p in Path('/proc').iterdir() if p.name.isdigit() and 'wine' in _proc_cmdline(int(p.name)).lower())})")
        time.sleep(1.0)
    if log:
        log(f"  timed out after {timeout:.0f}s waiting for the game process")
    return None


def steam_running() -> bool:
    """Is the Steam client up? Steamworks titles abort without it.

    Measured on Far Cry 5: launched straight through Proton it exits with
    `err:steamclient:steamclient_init_registry Failed to connect to Steam`, with
    or without the right executable. The client supplies the IPC the game's
    steam_api shim needs, so this is a precondition, not a nicety.
    """
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text(errors="replace").strip()
        except OSError:
            continue
        if comm in ("steam", "steamwebhelper", "steam.sh"):
            return True
    return False


LOG_DIR = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "ctfetch" / "logs"


DIAGNOSES = [
    (re.compile(r"X Error.*BadAlloc|GLXMakeCurrent", re.I),
     "graphics context creation failed (GLX BadAlloc): the game was started outside "
     "Steam's runtime container. Launch it from Steam, then attach."),
    (re.compile(r"wrong ELF class", re.I),
     "some processes are 32-bit and ignored the 64-bit tracer shim (needs a -m32 build)"),
    (re.compile(r"steamclient_init_registry Failed to connect to Steam", re.I),
     "the Steam client was not reachable; start Steam first"),
    (re.compile(r"(d3d|d3dx|msvcp|vcruntime|ucrtbase|xinput).{0,140}(not found|missing|failed)", re.I),
     "a redistributable is missing in this prefix; one launch from Steam installs it"),
]


def diagnose(log_path: Path | str | None, tail: int = 200) -> list[str]:
    """Turn the tail of a launch transcript into actionable hints."""
    if not log_path:
        return []
    try:
        lines = Path(log_path).read_text(errors="replace").splitlines()[-tail:]
    except OSError:
        return []
    text = "\n".join(lines)
    return [hint for pattern, hint in DIAGNOSES if pattern.search(text)]


def wine_prefix(app: dict) -> Path:
    """Proton's WINEPREFIX for this app: `<compatdata>/<appid>/pfx/` (with the
    trailing slash Proton itself uses — a wineserver started with a different
    prefix string will not answer `-k`, which is why Steam sometimes "cannot"
    stop a game)."""
    return app["steamapps"] / "compatdata" / app["appid"] / "pfx"


def _env_prefix(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    for chunk in raw.split(b"\0"):
        if chunk.startswith(b"WINEPREFIX="):
            return chunk[len(b"WINEPREFIX="):].decode(errors="replace").rstrip("/")
    return None


def _pids_with_prefix(prefix: Path) -> list[int]:
    want = str(prefix).rstrip("/")
    out = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if _env_prefix(pid) == want:
            out.append(pid)
    return out


def force_stop(game: str, log=None, app: dict | None = None) -> dict:
    """Kill a wine tree the normal way won't: game processes + its wineserver.

    `steam -applaunch` can leave Far Cry 5 running with Steam unable to stop it,
    because the wineserver answers only to the exact WINEPREFIX string Proton
    started it with. This kills every process carrying that prefix and asks the
    wineserver to quit, escalating to SIGKILL.
    """
    def say(msg: str) -> None:
        if log:
            log(msg)

    app = app or find_app(game)
    if app is None:
        return {"ok": False, "error": f"'{game}' is not an installed Steam app"}
    prefix = wine_prefix(app)
    exe = find_exe(app, game)
    killed: list[int] = []

    targets = set(_pids_with_prefix(prefix))
    found = game_pid(exe, game)
    if found:
        targets.add(found[0])

    for sig in (15, 9):
        alive = [p for p in targets if Path(f"/proc/{p}").exists()]
        if not alive:
            break
        for pid in alive:
            try:
                os.kill(pid, sig)
                killed.append(pid)
                say(f"  sent SIG{'TERM' if sig == 15 else 'KILL'} to {pid}")
            except ProcessLookupError:
                pass
            except PermissionError:
                say(f"  no permission to signal {pid} (not ours?)")
        time.sleep(2.0)

    wineserver = None
    proton = find_proton()
    if proton is not None:
        candidates = list(proton.parent.glob("files/bin/wineserver"))
        wineserver = candidates[0] if candidates else None
    if wineserver and wineserver.exists():
        env = dict(os.environ, WINEPREFIX=str(prefix) + "/")
        for flag in ("-k", "-k9"):
            try:
                subprocess.run([str(wineserver), flag], env=env, timeout=10,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                say(f"  wineserver {flag} on {prefix}")
            except (OSError, subprocess.SubprocessError):
                pass

    time.sleep(1.0)
    left = _pids_with_prefix(prefix)
    say(f"  {len(killed)} signal(s) sent; {len(left)} process(es) still carry the prefix")
    return {"ok": not left, "prefix": str(prefix), "signalled": killed, "remaining": left,
            "error": None if not left else f"{len(left)} process(es) survived"}


def launch(game: str, exe: Path | None = None, timeout: float = 120.0,
           wait: bool = True, log=None) -> dict:
    """Start `game` under Proton with the tracer shim; return what happened.

    `log(msg)` receives a running commentary — every command, env key, poll tick
    and the tail of the game's own output — so a slow or failing launch is
    visible in the GUI instead of looking like a frozen window. The full
    transcript (including proton's stdout/stderr) is also written to
    `~/.local/share/ctfetch/logs/launch-<game>-<timestamp>.log`.
    """
    def say(msg: str) -> None:
        if log:
            log(msg)

    app = find_app(game)
    if app is None:
        say(f"{game}: not an installed Steam app")
        return {"ok": False, "error": f"'{game}' is not an installed Steam app"}
    proton = find_proton()
    if proton is None:
        say("no Proton installation found")
        return {"ok": False, "error": "no Proton installation found"}

    target = exe or find_exe(app, game)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"launch-{re.sub(r'[^A-Za-z0-9]+', '-', game)}-{stamp}.log"
    transcript = open(log_path, "w", buffering=1, encoding="utf-8", errors="replace")

    def say2(msg: str) -> None:
        say(msg)
        transcript.write(f"{time.strftime('%H:%M:%S')} {msg}\n")

    say2(f"launch plan: game={game} appid={app['appid']} install={app['path']}")
    say2(f"  proton: {proton}  ({proton.parent.name})")
    if target is None:
        skipped = blacklisted_exes(app)
        hint = f" (only installers found: {', '.join(skipped[:3])})" if skipped else ""
        say2("  no usable executable" + hint)
        transcript.close()
        return {"ok": False, "error": f"no game executable found under {app['path']}{hint}"}
    say2(f"  exe: {target}  ({target.stat().st_size / 1e6:.1f} MB)")

    # Already running? Launching a second instance is the usual "hung launch":
    # the new process hands off to the old one and exits, and we then wait.
    running = game_pid(target, game)
    if running:
        say2(f"  already running: pid {running[0]} (matched by {running[1]}) — "
             f"attaching instead of launching")
        ok, detail = trace_ok(running[0])
        say2(f"  memory access: {detail}")
        transcript.close()
        return {"ok": ok, "app": app, "exe": str(target), "pid": running[0],
                "already_running": True, "trace": {"ok": ok, "detail": detail},
                "log_file": str(log_path), "warnings": [],
                "error": None if ok else f"attach refused ({detail}) — the running "
                                        f"game was not launched by this trainer"}

    warnings = []
    prefix = app["steamapps"] / "compatdata" / app["appid"] / "pfx"
    if not prefix.exists():
        warnings.append("fresh Proton prefix: first run creates it and installs "
                        "redistributables; titles with a third-party launcher "
                        "(Ubisoft Connect, EA App) usually need one launch from "
                        "Steam before direct launching works")
    if not steam_running():
        warnings.append("the Steam client is not running: Steamworks games exit with "
                        "'Failed to connect to Steam' — start Steam first")
    for warning in warnings:
        say2(f"  warning: {warning}")
    if not SHIM.exists():
        transcript.close()
        return {"ok": False, "error": f"tracer shim missing: build {SHIM.with_suffix('.c')}"}

    env = launch_env(app)
    cmd = [str(proton), "run", str(target)]
    say2(f"  command: {' '.join(cmd)}")
    say2(f"  cwd: {target.parent}")
    say2(f"  env: CT_TRACER_PID={env.get('CT_TRACER_PID')} "
         f"STEAM_COMPAT_DATA_PATH={env.get('STEAM_COMPAT_DATA_PATH')} "
         f"SteamAppId={env.get('SteamAppId')} "
         f"LD_PRELOAD={env.get('LD_PRELOAD', '').split()[0] if env.get('LD_PRELOAD') else '-'}"
         f" (+{max(0, len(env.get('LD_PRELOAD', '').split()) - 1)} inherited)")
    try:
        child = subprocess.Popen(cmd, cwd=str(target.parent), env=env,
                                 stdout=transcript, stderr=subprocess.STDOUT)
    except OSError as exc:
        say2(f"  spawn failed: {exc}")
        transcript.close()
        return {"ok": False, "error": f"spawn failed: {exc}", "log_file": str(log_path)}
    say2(f"  launcher pid {child.pid}; waiting up to {timeout:.0f}s for the game process")

    result = {"ok": True, "app": app, "proton": str(proton), "exe": str(target),
              "launcher_pid": child.pid, "pid": None, "trace": None, "warnings": warnings,
              "log_file": str(log_path), "already_running": False,
              "shim": str(SHIM), "env_keys": sorted(k for k in env if k.startswith(("STEAM_COMPAT", "CT_")) or k == "LD_PRELOAD")}
    if not wait:
        transcript.close()
        return result

    pid = wait_for_game(game, child, timeout, exe_hint=target, log=say2)
    if pid is None:
        # Nothing appeared: do not leave a hung Proton/wine tree behind (this is
        # exactly how a failed Far Cry 5 launch ended up "still running borked").
        killed = force_stop(game, log=say2, app=app)
        cleaned = bool(killed.get("signalled")) or killed.get("ok")
        say2(f"  cleanup: {len(killed.get('signalled') or [])} process(es) signalled; "
             f"{len(killed.get('remaining') or [])} left holding the prefix")
        result.update(ok=False, error="game process never appeared "
                                     f"({'launcher process stopped' if cleaned else 'launcher already gone'})")
        result["diagnosis"] = diagnose(log_path)
        for hint in result["diagnosis"]:
            say2(f"  diagnosis: {hint}")
        transcript.close()
        return result
    ok, detail = trace_ok(pid)
    say2(f"  memory access: {detail}")
    result.update(pid=pid, trace={"ok": ok, "detail": detail})
    if not ok:
        result["ok"] = False
        result["error"] = f"attached refused ({detail}) — shim did not take effect"
    result["diagnosis"] = diagnose(log_path)
    for hint in result["diagnosis"]:
        say2(f"  diagnosis: {hint}")
    transcript.close()
    return result


# ---------------------------------------------------------------- self test
def self_test() -> int:
    """Exercise the launcher's env plumbing against a stand-in that double-forks."""
    if not SHIM.exists():
        print(f"missing {SHIM}")
        return 2
    stub = ("import os,sys,time\n"
            "if os.fork()>0: sys.exit(0)\n"
            "os.setsid()\n"
            "if os.fork()>0: sys.exit(0)\n"
            "open(sys.argv[1],'w').write(str(os.getpid()))\n"
            "time.sleep(20)\n")
    pidfile = HERE / "native" / ".selftest.pid"
    if pidfile.exists():
        pidfile.unlink()
    env = launch_env(None)
    child = subprocess.Popen([sys.executable, "-c", stub, str(pidfile)], env=env)
    pid = None
    for _ in range(100):
        try:
            pid = int(pidfile.read_text())
            break
        except (OSError, ValueError):
            time.sleep(0.05)
    if pid is None:
        print("FAIL stub never started")
        return 1
    ok, detail = trace_ok(pid)
    os.kill(pid, 9)
    pidfile.unlink(missing_ok=True)
    child.wait(timeout=5)
    print(f"self-test: pid={pid} trace={detail} preload={env['LD_PRELOAD'].split()[0]}")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def steam_command() -> list[str] | None:
    """How to invoke Steam on this machine.

    `steam` is not always on PATH — on this box the Debian install only provides
    `~/.steam/steam/steam.sh`, so a bare subprocess of "steam" fails with
    FileNotFoundError ("steam no exist"). Flatpak installs need `flatpak run`.
    """
    candidates = [
        os.path.expanduser("~/.steam/steam/steam.sh"),
        os.path.expanduser("~/.local/share/Steam/steam.sh"),
        "/usr/bin/steam",
        "/usr/games/steam",
    ]
    on_path = shutil.which("steam")
    if on_path:
        candidates.insert(0, on_path)
    for cand in candidates:
        path = Path(cand)
        if path.is_file() and os.access(path, os.X_OK):
            return [str(path)]
    flatpak = shutil.which("flatpak")
    if flatpak:
        try:
            info = subprocess.run([flatpak, "info", "com.valvesoftware.Steam"],
                                  capture_output=True, timeout=10)
            if info.returncode == 0:
                return [flatpak, "run", "com.valvesoftware.Steam"]
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def start_steam_client(timeout: float = 60.0, log=None) -> bool:
    """Bring the Steam client up so -applaunch has somewhere to talk to."""
    if steam_running():
        return True
    cmd = steam_command()
    if cmd is None:
        return False
    if log:
        log(f"starting the Steam client: {' '.join(cmd)} -silent")
    try:
        subprocess.Popen(cmd + ["-silent"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if steam_running():
            if log:
                log("  Steam client is up")
            return True
        time.sleep(2.0)
    return False


def fossilize_workers() -> int:
    """Steam pre-caches Vulkan pipelines with `fossilize_replay` before launching.

    While these are running the game process does not exist yet, so a "game did
    not start" timeout during this phase means "still compiling shaders", not
    "broken launch". Far Cry 5 can spend minutes here on a cold cache.
    """
    count = 0
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text(errors="replace").strip()
        except OSError:
            continue
        if comm.startswith("fossilize"):
            count += 1
    return count


def launch_via_steam(game: str, timeout: float = 300.0, log=None) -> dict:
    """Start the game through the Steam client instead of Proton directly.

    Direct launches bypass Steam's runtime container, and titles that need it die
    with `X Error … BadAlloc (GLX)` — Far Cry 5 does exactly that. Letting Steam
    start the game fixes the runtime but costs the tracer shim: the process is
    Steam's child, so attaching needs CAP_SYS_PTRACE or ptrace_scope=0. The
    return value says which of those is in play.
    """
    def say(msg: str) -> None:
        if log:
            log(msg)

    app = find_app(game)
    if app is None:
        return {"ok": False, "error": f"'{game}' is not an installed Steam app"}
    exe = find_exe(app, game)
    cmd = steam_command()
    if cmd is None:
        return {"ok": False, "error": "no Steam launcher found (checked PATH, "
                                      "~/.steam/steam/steam.sh, /usr/bin/steam, "
                                      "/usr/games/steam, flatpak com.valvesoftware.Steam)"}
    say(f"asking Steam to launch {game} (appid {app['appid']}) via {cmd[0]}")
    if not start_steam_client(log=say):
        return {"ok": False, "error": "the Steam client is not running and would not start"}
    try:
        subprocess.Popen(cmd + ["-applaunch", app["appid"]],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as exc:
        return {"ok": False, "error": f"could not run {cmd[0]}: {exc}"}

    deadline = time.time() + timeout
    started = time.time()
    tick = 0
    while time.time() < deadline:
        found = game_pid(exe, game)
        if found:
            ok, detail = trace_ok(found[0])
            say(f"  running as pid {found[0]} (Steam's child); memory access: {detail}")
            if not ok:
                say("  this game was not launched by the trainer, so the tracer shim "
                    "is absent: grant ptrace with `sudo setcap cap_sys_ptrace+ep` on a "
                    "helper, or `sudo sysctl -w kernel.yama.ptrace_scope=0`")
            return {"ok": ok, "pid": found[0], "app": app, "via": "steam",
                    "trace": {"ok": ok, "detail": detail},
                    "error": None if ok else f"attach refused ({detail}) — Steam launched "
                                            f"this game, so it has no tracer declaration"}
        waited = time.time() - started
        if int(waited) // 10 > tick:
            tick = int(waited) // 10
            workers = fossilize_workers()
            phase = (f"Steam is still pre-caching Vulkan shaders ({workers} workers)"
                     if workers else "waiting for the game process")
            say(f"  {phase}… {waited:.0f}s")
        time.sleep(2.0)
    workers = fossilize_workers()
    hint = (" (shader pre-caching was still running — press Play again once it "
            "finishes)" if workers else "")
    return {"ok": False, "app": app, "via": "steam",
            "error": f"Steam did not start {game} within {timeout:.0f}s{hint}"}


def main() -> None:
    args = sys.argv[1:]
    if "--self-test" in args:
        sys.exit(self_test())
    if "--list" in args:
        for app in sorted(apps(), key=lambda a: a["name"].lower()):
            print(f"{app['appid']:>8}  {app['name']}")
        return
    if "--launch" in args:
        i = args.index("--launch")
        if i + 1 >= len(args):
            sys.exit("--launch needs a game name")
        game = args[i + 1]
        res = launch(game, wait="--no-wait" not in args)
        for k in ("ok", "error", "proton", "exe", "launcher_pid", "pid", "trace"):
            if res.get(k) is not None:
                print(f"{k}: {res[k]}")
        sys.exit(0 if res["ok"] else 1)
    print(__doc__)


if __name__ == "__main__":
    main()
