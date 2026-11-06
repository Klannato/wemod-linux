#!/usr/bin/env python3
"""
ctgui — Dear PyGui front end for wemod-linux, styled Material Design (dark).

Left: game list (filterable to installed / running, with auto-attach that finds
the game's PID by its table's target executable). Right: that game's cheats
(toggle AA scripts, edit value cheats). Bottom: a log pane and an agent chat
that drives the free LLM (OpenRouter) -> scanmem.

"Launch & attach" starts the game through Proton with the tracer shim loaded
(see ctlaunch.py / native/ptrace_allow.c). That is what makes the process
traceable on a stock Ubuntu kernel: yama ptrace_scope=1 only allows tracing a
process that named us with PR_SET_PTRACER — otherwise both process_vm_readv and
PTRACE_ATTACH are refused with EPERM.

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
import ctlaunch as L
import cttheme as T

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
    def __init__(self, ui: dict | None = None) -> None:
        self.ui = ui or {}
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
        self.launched: dict | None = None

    # ------------------------------------------------------------- widget tree
    def build(self) -> None:
        with dpg.window(tag="main", label="wemod-linux"):
            # --- app bar: title, primary action, secondary actions, status chip
            with dpg.child_window(height=58, tag="appbar", no_scrollbar=True):
                with dpg.group(horizontal=True):
                    dpg.add_text("wemod-linux", tag="app_title")
                    dpg.add_spacer(width=16)
                    dpg.add_button(label="Launch & attach", tag="btn_launch",
                                   callback=self.launch_and_attach)
                    dpg.add_button(label="Auto-attach", tag="btn_auto",
                                   callback=self.auto_attach)
                    dpg.add_button(label="Refresh", tag="btn_refresh", callback=self.refresh)
                    dpg.add_button(label="Detach", tag="btn_detach", callback=self.detach)
                    dpg.add_spacer(width=16)
                    self.status = dpg.add_text(default_value="not attached", tag="status")

            dpg.add_spacer(height=8)

            with dpg.child_window(height=430, tag="body"):
                with dpg.group(horizontal=True):
                    with dpg.child_window(width=340, height=-1, tag="left_pane"):
                        dpg.add_text("GAMES", tag="lbl_games")
                        dpg.add_input_text(tag="game_filter", hint="filter games",
                                           width=-1, callback=self.refresh_games)
                        with dpg.group(horizontal=True):
                            dpg.add_checkbox(label="Installed", tag="installed_only",
                                             callback=self.refresh_games)
                            dpg.add_checkbox(label="Running", tag="running_only",
                                             callback=self.refresh_games)
                        dpg.add_listbox([], tag="game_list", num_items=17, width=-1,
                                        callback=self.on_select_game)
                    with dpg.child_window(width=-1, height=-1, tag="right_pane"):
                        dpg.add_text("CHEATS", tag="lbl_cheats")
                        dpg.add_listbox([], tag="cheat_list", num_items=17, width=-1)
                        with dpg.group(horizontal=True):
                            dpg.add_button(label="Toggle (script)", tag="btn_toggle",
                                           callback=self.toggle_cheat)
                            dpg.add_button(label="Edit value", tag="btn_edit",
                                           callback=self.edit_value)

            with dpg.collapsing_header(label="Log", default_open=True):
                with dpg.child_window(height=140, tag="log_pane"):
                    self.log_text = dpg.add_text(default_value="", wrap=0)

            with dpg.collapsing_header(label="Agent", default_open=True):
                with dpg.group(horizontal=True):
                    dpg.add_input_text(tag="agent_goal", hint="e.g. find the value 100 and set it to 9999",
                                       width=-1, on_enter=True, callback=self.agent_send)
                    dpg.add_button(label="Send", tag="btn_send", callback=self.agent_send)
                with dpg.child_window(height=150, tag="chat_pane"):
                    self.chat_text = dpg.add_text(default_value="", wrap=0)

        self._style()

    def _style(self) -> None:
        """Apply Material surfaces and type scale to the items just built."""
        if not self.ui:
            return
        for item, level in (("appbar", 1), ("left_pane", 2), ("right_pane", 2),
                            ("log_pane", 2), ("chat_pane", 2)):
            dpg.bind_item_theme(item, T.elevation(level))
        if self.ui.get("primary_button"):
            dpg.bind_item_theme("btn_launch", self.ui["primary_button"])
        for item in ("btn_auto", "btn_refresh", "btn_detach", "btn_toggle",
                     "btn_edit", "btn_send"):
            if self.ui.get("text_button"):
                dpg.bind_item_theme(item, self.ui["text_button"])
        fonts = {
            "app_title": "headline",
            "lbl_games": "caption",
            "lbl_cheats": "caption",
        }
        for item, role in fonts.items():
            font = T.font_for(role)
            if font:
                dpg.bind_item_font(item, font)
        self._chip("warn")

    def _chip(self, kind: str) -> None:
        """Recolour the status text (ok / warn / error)."""
        theme = self.ui.get(kind)
        if theme:
            dpg.bind_item_theme("status", theme)

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
            self.log("no running game found — press Launch & attach (a game started "
                     "from Steam cannot be traced on this kernel)")
            return
        self.attach(pids[0])

    def attach(self, pid: int) -> None:
        try:
            self.bases = C.module_bases(pid)
        except Exception as exc:
            self.log(f"attach {pid} failed: {exc}")
            return
        ok, detail = L.trace_ok(pid)
        self.pid = pid
        dpg.set_value(self.status, f"attached to {pid} ({len(self.bases)} modules)")
        if ok:
            self._chip("ok")
            self.log(f"attached to pid {pid} — memory access {detail}")
        else:
            self._chip("error")
            self.log(f"attached to pid {pid} but memory access is denied ({detail}). "
                     f"Launch the game with Launch & attach so it declares this "
                     f"trainer as its tracer.")

    def detach(self) -> None:
        self.pid = None
        self.bases = {}
        dpg.set_value(self.status, "not attached")
        self._chip("warn")
        self.log("detached")

    # ------------------------------------------------------------- launch
    def launch_and_attach(self) -> None:
        """Start the selected game under Proton with the tracer shim, then attach."""
        game = self.current_game
        if not game:
            label = dpg.get_value("game_list")
            if label:
                game = label.lstrip("▶✓ ")
        if not game:
            self.log("select a game first")
            return
        app = L.find_app(game)
        if app is None:
            self.log(f"'{game}' is not installed through Steam — cannot launch it")
            return
        proton = L.find_proton()
        if proton is None:
            self.log("no Proton installation found")
            return
        self.log(f"launching {game} via {proton.parent.name} "
                 f"(tracer shim: {L.SHIM.name})")
        self._chip("warn")
        dpg.set_value(self.status, f"launching {game}…")
        threading.Thread(target=self._launch_worker, args=(game,), daemon=True).start()

    def _launch_worker(self, game: str) -> None:
        try:
            res = L.launch(game, wait=True, timeout=240.0)
        except Exception as exc:
            self.q.put(("launch", {"ok": False, "error": repr(exc), "game": game}))
            return
        res["game"] = game
        self.q.put(("launch", res))

    def _on_launch(self, res: dict) -> None:
        game = res.get("game", "?")
        if not res.get("ok"):
            self.log(f"launch failed: {res.get('error')}")
            dpg.set_value(self.status, "launch failed")
            self._chip("error")
            return
        self.launched = res
        pid = res.get("pid")
        trace = (res.get("trace") or {}).get("detail", "?")
        self.log(f"{game} is running (pid {pid}); tracer access {trace}")
        self.current_game = game
        self.attach(int(pid))

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
        if dpg.does_item_exist("edit_modal"):
            dpg.delete_item("edit_modal")
        with dpg.window(label=f"Edit value — {r['path']}", modal=True, width=360,
                        height=150, tag="edit_modal", pos=(420, 240)):
            dpg.add_text(default_value=f"address {addr:#x}, current {cur}")
            dpg.add_input_text(tag="edit_input", default_value=str(cur), width=-1)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Write", tag="btn_write", callback=self.write_value)
                dpg.add_button(label="Cancel", tag="btn_cancel",
                               callback=lambda: dpg.delete_item("edit_modal"))
        if self.ui.get("primary_button"):
            dpg.bind_item_theme("btn_write", self.ui["primary_button"])
        if self.ui.get("text_button"):
            dpg.bind_item_theme("btn_cancel", self.ui["text_button"])

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
                elif kind == "launch":
                    self._on_launch(payload)
        except queue.Empty:
            pass
        dpg.set_frame_callback(dpg.get_frame_count() + 1, self._poll)


def main() -> None:
    dpg.create_context()
    ui = T.apply()                      # Material theme + type scale, bound globally
    app = App(ui)
    app.build()
    dpg.create_viewport(title="wemod-linux", width=1250, height=860)
    dpg.setup_dearpygui()
    dpg.show_viewport()
    dpg.set_primary_window("main", True)
    app.refresh()
    dpg.set_frame_callback(1, app._poll)
    dpg.start_dearpygui()
    dpg.destroy_context()


if __name__ == "__main__":
    main()
