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
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import ctdetect as D

HERE = Path(__file__).resolve().parent
SHIM = HERE / "native" / "ptrace_allow.so"


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


def find_exe(app: dict, game: str) -> Path | None:
    """The game's main executable: prefer the name the cheat tables target."""
    if not app["path"].exists():
        return None
    exes = []
    for p in app["path"].rglob("*.exe"):
        try:
            if p.stat().st_size > 100_000:
                exes.append(p)
        except OSError:
            continue
    if not exes:
        return None
    wanted = {n.lower() for n in D.game_exe_names(game)}
    exact = [p for p in exes if p.stem.lower() in wanted or p.name.lower() in wanted]
    if exact:
        return max(exact, key=lambda p: p.stat().st_size)
    partial = [p for p in exes if any(w and w in p.stem.lower() for w in wanted)]
    if partial:
        return max(partial, key=lambda p: p.stat().st_size)
    # No table hint: the biggest exe is usually the game, not the redistributable.
    return max(exes, key=lambda p: p.stat().st_size)


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


def wait_for_game(game: str, child: subprocess.Popen, timeout: float = 180.0) -> int | None:
    """Poll for the game's PE process; its tree may be reparented by wineserver."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        procs = D.find_processes()
        pids = D.running_pids(game, procs)
        if pids:
            return pids[0]
        if child.poll() is not None:
            return None
        time.sleep(1.0)
    return None


def launch(game: str, exe: Path | None = None, timeout: float = 180.0,
           wait: bool = True) -> dict:
    """Start `game` under Proton with the tracer shim; return what happened."""
    app = find_app(game)
    if app is None:
        return {"ok": False, "error": f"'{game}' is not an installed Steam app"}
    proton = find_proton()
    if proton is None:
        return {"ok": False, "error": "no Proton installation found"}
    target = exe or find_exe(app, game)
    if target is None:
        return {"ok": False, "error": f"no executable found under {app['path']}"}
    if not SHIM.exists():
        return {"ok": False, "error": f"tracer shim missing: build {SHIM.with_suffix('.c')}"}

    env = launch_env(app)
    cmd = [str(proton), "run", str(target)]
    try:
        child = subprocess.Popen(cmd, cwd=str(target.parent), env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        return {"ok": False, "error": f"spawn failed: {exc}"}

    result = {"ok": True, "app": app, "proton": str(proton), "exe": str(target),
              "launcher_pid": child.pid, "pid": None, "trace": None,
              "shim": str(SHIM), "env_keys": sorted(k for k in env if k.startswith(("STEAM_COMPAT", "CT_")) or k == "LD_PRELOAD")}
    if not wait:
        return result

    pid = wait_for_game(game, child, timeout)
    if pid is None:
        result.update(ok=False, error="game process never appeared (check Proton log)")
        return result
    ok, detail = trace_ok(pid)
    result.update(pid=pid, trace={"ok": ok, "detail": detail})
    if not ok:
        result["ok"] = False
        result["error"] = f"attached refused ({detail}) — shim did not take effect"
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
