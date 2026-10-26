#!/usr/bin/env python3
"""
ctgui — Dear PyGui front end for wemod-linux.

Left: game list (filterable to installed / running, with auto-attach that finds
the game's PID by its table's target executable). Right: that game's cheats
(toggle AA scripts, edit value cheats). Bottom: a log pane and an agent chat
that drives the free LLM (OpenRouter) -> scanmem.

Run:  python3 ctgui.py
"""
import queue
import sys
import threading
from pathlib import Path

import dearpygui.dearpygui as dpg

sys.path.insert(0, str(Path(__file__).parent))
import ctapply as C
import ctaa as AA
import ctagent as G
import ctdetect as D

TABLES = D.TABLES


# ---------------------------------------------------------------- data helpers
def list_games() -> list[str]:
    if not TABLES.exists():
        return []
    return sorted(d.name for d in TABLES.iterdir() if d.is_dir())


def game_cheats(game: str) -> list[dict]:
    """Flatten every cheat across a game's tables into rows."""
    rows = []
    for f in D.game_tables(game):
        try:
            entries = C.parse_ct(str(f))
        except Exception:
            continue
        for path, e in C.flatten(entries):
            is_script = "script" in e["type"].lower() or e["script"] is not None
            rows.append({
                "path": "  " * (len(path) - 1) + path[-1],
                "address": e["address"] or "",
                "offsets": e["offsets"],
                "type": e["type"] or ("script" if is_script else ""),
                "is_script": is_script,
                "script": e["script"],
                "entry": e,
            })
    return rows


class App:
    def __init__(self) -> None:
        self.pid: int | None = None
        self.bases: dict[str, int] = {}
        self.rows: list[dict] = []
        self.cheat_labels: list[str] = []
        self.enabled: dict[str, dict] = {}   # cheat index -> AA handle
        self.edit_row: tuple | None = None
        self.q: queue.Queue = queue.Queue()
        self.log_lines: list[str] = []
        self.installed: set[str] = set()
        self.procs: dict[str, list[int]] = {}
        self.current_game: str | None = None

    # ------------------------------------------------------------- widget tree
    def build(self) -> None:
        with dpg.window(tag="main"):
            with dpg.group(horizontal=True):
                dpg.add_button(label="Auto-attach", callback=self.auto_attach)
                dpg.add_button(label="Refresh", callback=self.refresh)
                dpg.add_button(label="Detach", callback=self.detach)
                self.status = dpg.add_text(default_value="not attached")

            with dpg.child_window(height=400):
                with dpg.group(horizontal=True):
                    with dpg.child_window(width=340, height=-1):
                        dpg.add_text(default_value="Games")
                        dpg.add_input_text(tag="game_filter", hint="filter",
                                           callback=self.refresh_games, width=-1)
                        dpg.add_checkbox(label="Installed only", tag="installed_only",
                                         callback=self.refresh_games)
                        dpg.add_checkbox(label="Running only", tag="running_only",
                                         callback=self.refresh_games)
                        dpg.add_listbox([], tag="game_list", num_items=20, width=-1,
                                        callback=self.on_select_game)
                    with dpg.child_window(width=-1, height=-1):
                        dpg.add_text(default_value="Cheats")
                        dpg.add_listbox([], tag="cheat_list", num_items=20, width=-1)
                        with dpg.group(horizontal=True):
                            dpg.add_button(label="Toggle (script)", callback=self.toggle_cheat)
                            dpg.add_button(label="Edit value", callback=self.edit_value)

            with dpg.collapsing_header(label="Log", default_open=True):
                with dpg.child_window(height=140):
                    self.log_text = dpg.add_text(default_value="", wrap=0)

            with dpg.collapsing_header(label="Agent", default_open=True):
                with dpg.group(horizontal=True):
                    dpg.add_input_text(tag="agent_goal", hint="e.g. find the value 100 and set it to 9999",
                                       width=-1, on_enter=True, callback=self.agent_send)
                    dpg.add_button(label="Send", callback=self.agent_send)
                with dpg.child_window(height=150):
                    self.chat_text = dpg.add_text(default_value="", wrap=0)

    # ------------------------------------------------------------- game browser
    def refresh(self) -> None:
        self.installed = D.steam_installed_games()
        self.procs = D.find_processes()
        self.log(f"refreshed: {len(self.installed)} Steam apps installed, "
                 f"{sum(len(v) for v in self.procs.values())} processes seen")
        self.refresh_games()

    def refresh_games(self, *_) -> None:
        q = dpg.get_value("game_filter").lower()
        inst_only = dpg.get_value("installed_only")
        run_only = dpg.get_value("running_only")
        items = []
        for game in list_games():
            is_inst = D._norm(game) in self.installed
            pids = D.running_pids(game, self.procs)
            if inst_only and not is_inst:
                continue
            if run_only and not pids:
                continue
            if q and q not in game.lower():
                continue
            prefix = ""
            if pids:
                prefix += "▶ "
            elif is_inst:
                prefix += "✓ "
            items.append(prefix + game)
        dpg.configure_item("game_list", items=items)

    def on_select_game(self, sender, app_data, user_data) -> None:
        game = app_data
        if not game:
            return
        self.current_game = game.lstrip("▶✓ ")
        self.rows = game_cheats(self.current_game)
        self.cheat_labels = [r["path"] for r in self.rows]
        dpg.configure_item("cheat_list", items=self.cheat_labels)
        self.log(f"loaded {len(self.rows)} cheats for {self.current_game}")

    # ------------------------------------------------------------- attach
    def auto_attach(self) -> None:
        pids: list[int] = []
        if self.current_game:
            pids = D.running_pids(self.current_game, self.procs)
        if not pids:  # fall back to any running game in the library
            for g in list_games():
                pids.extend(D.running_pids(g, self.procs))
                if pids:
                    self.current_game = g
                    break
        if not pids:
            self.log("no running game found — launch the game, then Refresh")
            return
        self.attach(pids[0])

    def attach(self, pid: int) -> None:
        try:
            self.bases = C.module_bases(pid)
        except Exception as exc:
            self.log(f"attach {pid} failed: {exc}")
            return
        self.pid = pid
        dpg.set_value(self.status, f"attached to {pid} ({len(self.bases)} modules)")
        self.log(f"attached to pid {pid}")

    def detach(self) -> None:
        self.pid = None
        self.bases = {}
        dpg.set_value(self.status, "not attached")
        self.log("detached")

    # ------------------------------------------------------------- cheats
    def _selected_index(self) -> int | None:
        label = dpg.get_value("cheat_list")
        if not label:
            return None
        try:
            return self.cheat_labels.index(label)
        except ValueError:
            return None

    def toggle_cheat(self) -> None:
        i = self._selected_index()
        if i is None:
            self.log("select a cheat first")
            return
        r = self.rows[i]
        if not r["is_script"]:
            self.log(f"'{r['path']}' is a value cheat — use Edit value")
            return
        if self.pid is None:
            self.log("attach to a running game first")
            return
        key = str(i)
        if key in self.enabled:
            try:
                AA.aa_disable(self.pid, self.enabled.pop(key))
                self.log(f"disabled script: {r['path']}")
            except Exception as exc:
                self.log(f"disable failed: {exc}")
            return
        if not r["script"]:
            self.log("no AA script body")
            return
        self.log(f"enabling script (AOB scan + inject): {r['path']}")
        threading.Thread(target=self._aa_worker, args=(key, r["script"], r["path"]), daemon=True).start()

    def _aa_worker(self, key: str, script: str, name: str) -> None:
        try:
            h = AA.auto_enable(self.pid, script)
            self.q.put(("aa_ok", (key, h, name)))
        except Exception as exc:
            self.q.put(("aa_err", (key, name, str(exc))))

    def edit_value(self) -> None:
        i = self._selected_index()
        if i is None:
            self.log("select a value cheat first")
            return
        r = self.rows[i]
        if r["is_script"] or not r["address"]:
            self.log("not a value cheat")
            return
        if self.pid is None:
            self.log("attach to a running game first")
            return
        addr = C._base_addr(r["address"], self.bases)
        if addr is None:
            self.log("could not resolve address")
            return
        if r["offsets"]:
            addr = C.resolve(self.pid, r["entry"], self.bases)
            if addr is None:
                self.log("pointer path failed to resolve")
                return
        size = 8 if "double" in r["type"].lower() else 4 if "float" in r["type"].lower() or "4 byte" in r["type"].lower() else 8
        try:
            cur = C.read_value(self.pid, addr, size)
        except Exception as exc:
            self.log(f"read failed: {exc}")
            return
        self.edit_row = (addr, size)
        with dpg.window(label=f"Edit value — {r['path']}", modal=True, width=340, height=130, tag="edit_modal"):
            dpg.add_text(default_value=f"address {addr:#x}, current {cur}")
            dpg.add_input_text(tag="edit_input", default_value=str(cur), width=-1)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Write", callback=self.write_value)
                dpg.add_button(label="Cancel", callback=lambda: dpg.delete_item("edit_modal"))

    def write_value(self) -> None:
        try:
            val = int(dpg.get_value("edit_input"), 0)
        except ValueError:
            self.log("invalid value")
            return
        addr, size = self.edit_row
        try:
            C.write_value(self.pid, addr, val, size)
            self.log(f"wrote {val} to {addr:#x}")
        except Exception as exc:
            self.log(f"write failed: {exc}")
        dpg.delete_item("edit_modal")

    # ------------------------------------------------------------- agent
    def agent_send(self, *_) -> None:
        if self.pid is None:
            self.log("attach to a running game first")
            return
        goal = dpg.get_value("agent_goal").strip()
        if not goal:
            return
        self.log(f"[agent] {goal}")
        dpg.set_value(self.chat_text, dpg.get_value(self.chat_text) + f"\n> {goal}\n(thinking…)\n")
        threading.Thread(target=self._agent_worker, args=(goal,), daemon=True).start()

    def _agent_worker(self, goal: str) -> None:
        try:
            plan, out = G.scan(self.pid, goal)
            self.q.put(("chat", f"plan: {plan}\n{out}"))
        except Exception as exc:
            self.q.put(("chat", f"error: {exc}"))

    # ------------------------------------------------------------- plumbing
    def log(self, msg: str) -> None:
        self.log_lines.append(msg)
        self.log_lines = self.log_lines[-500:]
        dpg.set_value(self.log_text, "\n".join(self.log_lines))

    def _poll(self, *_) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "chat":
                    dpg.set_value(self.chat_text, dpg.get_value(self.chat_text) + "\n" + payload + "\n")
                elif kind == "aa_ok":
                    key, h, name = payload
                    self.enabled[key] = h
                    self.log(f"script enabled: {name} (cave {h['cave']:#x})")
                elif kind == "aa_err":
                    key, name, err = payload
                    self.log(f"script enable failed ({name}): {err}")
        except queue.Empty:
            pass
        dpg.set_frame_callback(dpg.get_frame_count() + 1, self._poll)


def main() -> None:
    dpg.create_context()
    app = App()
    app.build()
    dpg.create_viewport(title="wemod-linux", width=1250, height=820)
    dpg.setup_dearpygui()
    dpg.show_viewport()
    dpg.set_primary_window("main", True)
    app.refresh()
    dpg.set_frame_callback(1, app._poll)
    dpg.start_dearpygui()
    dpg.destroy_context()


if __name__ == "__main__":
    main()
