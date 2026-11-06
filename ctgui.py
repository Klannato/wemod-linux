#!/usr/bin/env python3
"""
ctgui — WeCheat: the Dear PyGui front end (Material Design, dark).

Left: game list (installed / running markers). Right: the game's **clean** cheat
list — one row per cheat, a pill switch for anything that is a standing toggle,
an Apply button for one-shot actions, and an inline value box for numbers. Rows
come from ctclean.py's sidecar JSON, not from the raw table, so struct dumps
(`RagdollScale` ×1373) never reach the UI.

"Launch & attach" starts the game through Proton with the tracer shim loaded
(see ctlaunch.py / native/ptrace_allow.c). That is what makes the process
traceable on a stock Ubuntu kernel: yama ptrace_scope=1 only allows tracing a
process that named us with PR_SET_PTRACER — otherwise both process_vm_readv and
PTRACE_ATTACH are refused with EPERM.

Run:  python3 ctgui.py
"""
import json
import queue
import sys
import threading
import time
from pathlib import Path

import dearpygui.dearpygui as dpg

sys.path.insert(0, str(Path(__file__).parent))
import ctapply as C
import ctaa as AA
import ctagent as G
import ctclean as CL
import ctcompat as CC
import ctdetect as D
import ctfind as F
import ctlaunch as L
import cttheme as T
import ctwidgets as W

TABLES = D.TABLES
TRAINER_LOG = L.LOG_DIR / "trainer.log"
CONF_ORDER = {"high": 0, "medium": 1, "low": 2}


def list_games() -> list[str]:
    if not TABLES.exists():
        return []
    return sorted(d.name for d in TABLES.iterdir() if d.is_dir())


def value_size(vtype: str) -> int:
    t = (vtype or "").lower()
    if "byte" in t:
        return 2 if t.startswith("2") else 1
    if "8 byte" in t or "double" in t:
        return 8
    return 4


class App:
    def __init__(self, ui: dict | None = None) -> None:
        self.ui = ui or {}
        self.pid: int | None = None
        self.bases: dict[str, int] = {}
        self.cheats: list[dict] = []
        self.enabled: dict[str, dict] = {}     # state key -> AA handle
        self.q: queue.Queue = queue.Queue()
        self.log_lines: list[str] = []
        self.installed: set[str] = set()
        self.procs: dict[str, list[int]] = {}
        self.current_game: str | None = None
        self.switches: dict[str, object] = {}
        self.launched: dict | None = None
        self.find_hits: list[int] = []
        self.find_patch: dict | None = None
        self.compat: dict | None = None
        self._suspend_select = False    # configure_item(items=...) fires the callback

    # ------------------------------------------------------------- widget tree
    def build(self) -> None:
        with dpg.window(tag="main", label="WeCheat"):
            with dpg.child_window(height=58, tag="appbar", no_scrollbar=True):
                with dpg.group(horizontal=True):
                    dpg.add_text("WeCheat", tag="app_title")
                    dpg.add_spacer(width=16)
                    dpg.add_button(label="Launch & attach", tag="btn_launch",
                                   callback=self.launch_and_attach)
                    dpg.add_button(label="Launch via Steam", tag="btn_viasteam",
                                   callback=self.launch_via_steam_click)
                    dpg.add_button(label="Auto-attach", tag="btn_auto",
                                   callback=self.auto_attach)
                    dpg.add_button(label="Refresh", tag="btn_refresh", callback=self.refresh)
                    dpg.add_button(label="Detach", tag="btn_detach", callback=self.detach)
                    dpg.add_button(label="Force stop", tag="btn_kill",
                                   callback=self.force_stop_click)
                    dpg.add_button(label="Troubleshoot", tag="btn_trouble",
                                   callback=self.trouble_open)
                    dpg.add_spacer(width=16)
                    self.status = dpg.add_text(default_value="not attached", tag="status")

            dpg.add_spacer(height=8)

            with dpg.child_window(height=470, width=-1, tag="body"):
                with dpg.group(horizontal=True):
                    with dpg.child_window(width=320, height=-1, tag="left_pane"):
                        dpg.add_text("GAMES", tag="lbl_games")
                        dpg.add_input_text(tag="game_filter", hint="filter games",
                                           width=-1, callback=self.refresh_games)
                        with dpg.group(horizontal=True):
                            dpg.add_checkbox(label="Installed", tag="installed_only",
                                             callback=self.refresh_games)
                            dpg.add_checkbox(label="Running", tag="running_only",
                                             callback=self.refresh_games)
                        dpg.add_listbox([], tag="game_list", num_items=18, width=-1,
                                        callback=self.on_select_game)
                    with dpg.child_window(width=-1, height=-1, tag="right_pane"):
                        with dpg.group(horizontal=True):
                            dpg.add_text("CHEATS", tag="lbl_cheats")
                            dpg.add_spacer(width=10)
                            self.cheat_count = dpg.add_text("", tag="cheat_count")
                            dpg.add_spacer(width=12)
                            self.compat_text = dpg.add_text("", tag="compat_text")
                            dpg.add_button(label="Check table", tag="btn_compat",
                                           callback=self.check_compat)
                        dpg.add_input_text(tag="cheat_filter", hint="search cheats",
                                           width=-1, callback=self.populate_cheats)
                        with dpg.group(horizontal=True):
                            dpg.add_checkbox(label="Values", tag="show_values",
                                             default_value=True, callback=self.populate_cheats)
                            dpg.add_checkbox(label="English", tag="show_english",
                                             default_value=True, callback=self.populate_cheats)
                            dpg.add_checkbox(label="Show raw entries", tag="show_raw",
                                             callback=self.populate_cheats)
                        with dpg.child_window(height=-1, tag="cheat_scroll"):
                            pass

            with dpg.collapsing_header(label="Log", default_open=True):
                with dpg.child_window(height=130, tag="log_pane"):
                    self.log_text = dpg.add_text(default_value="", wrap=0)

            with dpg.collapsing_header(label="Find cheat", default_open=False, tag="find_header"):
                with dpg.child_window(height=190, tag="find_pane"):
                    with dpg.group(horizontal=True):
                        dpg.add_text("Type")
                        dpg.add_combo(["int32", "int64", "float", "double", "byte"],
                                      tag="find_kind", width=90, default_value="int32")
                        dpg.add_text("Value in game")
                        dpg.add_input_text(tag="find_value", width=110, decimal=True)
                        dpg.add_button(label="Scan", tag="btn_scan", callback=self.find_scan)
                    with dpg.group(horizontal=True):
                        dpg.add_text("After changing it in game, new value")
                        dpg.add_input_text(tag="find_new", width=110, decimal=True)
                        dpg.add_button(label="Refine", tag="btn_refine", callback=self.find_refine)
                        dpg.add_button(label="Watch writes", tag="btn_watch", callback=self.find_watch)
                    dpg.add_text("", tag="find_status")
                    with dpg.group(horizontal=True):
                        dpg.add_listbox([], tag="find_list", num_items=4, width=260)
                        with dpg.group():
                            dpg.add_text("", tag="find_writer", wrap=320)
                            with dpg.group(horizontal=True):
                                dpg.add_button(label="Apply patch", tag="btn_apply_patch",
                                               callback=self.find_apply)
                                dpg.add_button(label="Undo", tag="btn_undo_patch",
                                               callback=self.find_undo)

            with dpg.collapsing_header(label="Agent", default_open=False):
                with dpg.group(horizontal=True):
                    dpg.add_input_text(tag="agent_goal", width=-1, on_enter=True,
                                       hint="e.g. find the value 100 and set it to 9999",
                                       callback=self.agent_send)
                    dpg.add_button(label="Send", tag="btn_send", callback=self.agent_send)
                with dpg.child_window(height=150, tag="chat_pane"):
                    self.chat_text = dpg.add_text(default_value="", wrap=0)

        self._style()

    def _style(self) -> None:
        if not self.ui:
            return
        for item, level in (("appbar", 1), ("left_pane", 2), ("right_pane", 2),
                            ("log_pane", 2), ("chat_pane", 2)):
            dpg.bind_item_theme(item, T.elevation(level))
        if self.ui.get("primary_button"):
            dpg.bind_item_theme("btn_launch", self.ui["primary_button"])
        for item in ("btn_auto", "btn_refresh", "btn_detach", "btn_send",
                     "btn_viasteam", "btn_kill", "btn_compat", "btn_trouble"):
            if self.ui.get("text_button"):
                dpg.bind_item_theme(item, self.ui["text_button"])
        for item, role in (("app_title", "headline"), ("lbl_games", "caption"),
                           ("lbl_cheats", "caption"), ("cheat_count", "caption")):
            font = T.font_for(role)
            if font:
                dpg.bind_item_font(item, font)
        self._chip("warn")

    def _chip(self, kind: str) -> None:
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
            prefix = "▶ " if pids else ("✓ " if is_inst else "")
            items.append(prefix + game)
        self._suspend_select = True
        try:
            dpg.configure_item("game_list", items=items)
        finally:
            self._suspend_select = False

    def on_select_game(self, sender, app_data, user_data) -> None:
        if self._suspend_select or not app_data:
            return
        game = app_data.lstrip("▶✓ ")
        if game == self.current_game:
            return
        self.current_game = game
        self.cheats = CL.load_clean(game)["cheats"]
        self.enabled.clear()
        self.log(f"{game}: {len(self.cheats)} cleaned cheats")
        self.populate_cheats()
        self.compat = None
        dpg.set_value("compat_text", "checking table…")
        threading.Thread(target=self._compat_worker, args=(game,), daemon=True).start()

    # ------------------------------------------------------------- cheat list
    def _visible_cheats(self) -> list[dict]:
        q = (dpg.get_value("cheat_filter") or "").strip().lower()
        show_raw = dpg.get_value("show_raw")
        show_values = dpg.get_value("show_values")
        out = []
        for c in self.cheats:
            if not show_raw and c["confidence"] == "low":
                continue
            if not show_values and not c.get("toggle") and not c.get("one_shot"):
                continue
            if q and q not in c["description"].lower() and q not in c["category"].lower():
                continue
            out.append(c)
        return out

    def populate_cheats(self, *_) -> None:
        """(Re)build the grouped cheat rows inside the scrolling pane."""
        W.clear()
        self.switches.clear()
        if dpg.does_item_exist("cheat_scroll"):
            dpg.delete_item("cheat_scroll", children_only=True)
        if not self.current_game:
            dpg.set_value(self.cheat_count, "")
            return

        cheats = self._visible_cheats()
        dpg.set_value(self.cheat_count, f"{len(cheats)} of {len(self.cheats)}")
        if not cheats:
            dpg.add_text("nothing matches — clear the filter or tick Show raw entries",
                         parent="cheat_scroll")
            return

        groups: dict[str, list[dict]] = {}
        for c in cheats:
            groups.setdefault(c["category"] or "General", []).append(c)

        # Items are added into an existing container: push it rather than passing
        # parent= to every widget, because a collapsing_header only becomes a
        # usable container while it is on the container stack.
        dpg.push_container_stack("cheat_scroll")
        try:
            for category in sorted(groups, key=lambda k: (-len(groups[k]), k.lower())):
                rows = sorted(groups[category],
                              key=lambda c: (CONF_ORDER[c["confidence"]],
                                             c["description"].lower()))
                try:
                    with dpg.collapsing_header(label=f"{category}  ({len(rows)})",
                                               default_open=True):
                        with dpg.table(header_row=False, policy=dpg.mvTable_SizingStretchProp,
                                       borders_innerH=False, borders_outerH=False,
                                       borders_innerV=False, borders_outerV=False):
                            dpg.add_table_column(width_stretch=True)
                            dpg.add_table_column(init_width_or_weight=64, width_fixed=True)
                            for c in rows:
                                self._cheat_row(c)
                except Exception as exc:      # one bad row must not blank the list
                    print(f"row build failed ({category}): {exc!r}", file=sys.stderr)
        finally:
            try:
                dpg.pop_container_stack()
            except Exception:
                pass

    def _cheat_row(self, c: dict) -> None:
        key = f"{c['category']}/{c['description']}"
        with dpg.table_row():
            english = c.get("description_en") if dpg.get_value("show_english") else None
            label = (english or c["description"]) + ("   (raw)" if c["confidence"] == "low" else "")
            text_tag = dpg.add_text(label)
            if english and english != c["description"]:
                with dpg.tooltip(text_tag):
                    dpg.add_text(c["description"])
            cell = dpg.generate_uuid()
            with dpg.group(horizontal=True, tag=cell):
                if c.get("toggle") and c["kind"] == "script":
                    sw = W.add_switch(cell, value=key in self.enabled,
                                      on_change=lambda v, c=c, k=key: self._on_script_toggle(c, k, v))
                    self.switches[key] = sw
                elif c.get("one_shot"):
                    dpg.add_button(label="Apply", callback=self._apply_once, user_data=c)
                elif c["kind"] == "value" and c.get("toggle"):
                    sw = W.add_switch(cell, value=False,
                                      on_change=lambda v, c=c, k=key: self._on_value_toggle(c, k, v))
                    self.switches[key] = sw
                elif c["kind"] == "value":
                    input_tag = f"val_{abs(hash(key))}"
                    dpg.add_input_text(tag=input_tag, width=70, decimal=True, hint=c["type"])
                    dpg.add_button(label="Set", width=42, callback=self._on_set_value,
                                   user_data=(c, input_tag))
                else:
                    dpg.add_text(c["type"] or "—", color=(120, 120, 120))

    # ------------------------------------------------------------- actions
    def _address_of(self, c: dict) -> int | None:
        if self.pid is None:
            return None
        addr = c.get("address") or (c.get("addresses") or [None])[0]
        if not addr:
            return None
        entry = {"address": addr, "offsets": c.get("offsets") or []}
        try:
            return C.resolve(self.pid, entry, self.bases)
        except Exception:
            return C._base_addr(addr, self.bases)

    def _on_script_toggle(self, c: dict, key: str, value: bool) -> None:
        sw = self.switches.get(key)
        if self.pid is None:
            self.log("attach to the game first")
            if sw:
                sw.set(False)
            return
        if not value:
            handle = self.enabled.pop(key, None)
            if handle:
                try:
                    AA.auto_disable(self.pid, handle)
                    self.log(f"disabled: {c['description']}")
                except Exception as exc:
                    self.log(f"disable failed: {exc}")
            return
        self.log(f"enabling {c['description']}…")
        threading.Thread(target=self._enable_worker, args=(c, key), daemon=True).start()

    def _enable_worker(self, c: dict, key: str) -> None:
        """Enable a script, switching to whichever process owns its modules.

        A game's launcher stub and its engine are separate processes; attaching to
        the stub and then toggling `FC_m64.dll+114B512` fails with "cannot resolve"
        while the game is perfectly alive one pid over.
        """
        try:
            pid = self.pid
            modules = L.script_modules(c["script"])
            if modules:
                owner = L.pid_for_modules(modules, pid, log=lambda m: self.q.put(("log", m)))
                if owner and owner != pid:
                    ok, detail = L.trace_ok(owner)
                    if not ok:
                        raise ValueError(f"{modules[0]} lives in pid {owner} but it is "
                                         f"not readable ({detail})")
                    self.pid = owner
                    self.bases = C.module_bases(owner)
                    pid = owner
                    self.q.put(("reattach", owner))
            handle = AA.auto_enable(pid, c["script"], self.bases)
            shift = (handle or {}).get("shift") or 0
            remap = (handle or {}).get("remap")
            if remap:
                self.q.put(("log", f"  re-mapped with a {remap['signature_len']}-byte "
                                   f"{remap['source']} signature (unique match)"))
            if shift:
                self.q.put(("log", f"  table drift {shift:+#x}: patched at the shifted "
                                   f"address (this table was made for another build)"))
            self.q.put(("aa_ok", (key, handle, c["description"])))
        except ValueError as exc:
            if "Confirm it explicitly" in str(exc):
                self.q.put(("aa_confirm", (key, c, str(exc), pid)))
                return
            self.q.put(("aa_err", (key, c["description"], f"{type(exc).__name__}: {exc}")))
        except Exception as exc:
            self.q.put(("aa_err", (key, c["description"], f"{type(exc).__name__}: {exc}")))

    def _on_value_toggle(self, c: dict, key: str, value: bool) -> None:
        sw = self.switches.get(key)
        addr = self._address_of(c)
        if addr is None:
            self.log(f"{c['description']}: address unavailable "
                     f"(symbol '{c.get('address')}' needs the table's own script)")
            if sw:
                sw.set(False)
            return
        try:
            C.write_value(self.pid, addr, 1 if value else 0, value_size(c["type"]))
            self.log(f"{c['description']} -> {1 if value else 0}")
        except Exception as exc:
            self.log(f"write failed: {exc}")
            if sw:
                sw.set(not value)

    def _on_set_value(self, sender=None, app_data=None, user_data=None) -> None:
        c, input_tag = user_data
        addr = self._address_of(c)
        if addr is None:
            self.log(f"{c['description']}: address unavailable")
            return
        try:
            val = int(float(dpg.get_value(input_tag)))
        except (TypeError, ValueError):
            self.log(f"{c['description']}: not a number")
            return
        try:
            C.write_value(self.pid, addr, val, value_size(c["type"]))
            self.log(f"{c['description']} -> {val}")
        except Exception as exc:
            self.log(f"write failed: {exc}")

    def _apply_once(self, sender=None, app_data=None, user_data=None) -> None:
        c = user_data
        if self.pid is None:
            self.log("attach to the game first")
            return
        if c["kind"] == "script" and c.get("script"):
            self.log(f"applying {c['description']}…")
            threading.Thread(target=self._apply_worker, args=(c,), daemon=True).start()
        else:
            self._on_value_toggle(c, "", True)

    def _confirm_shifted(self, payload) -> None:
        """A drifted patch may not be the same instruction — ask, never assume."""
        key, c, reason, pid = payload
        if dpg.does_item_exist("shift_modal"):
            dpg.delete_item("shift_modal")
        with dpg.window(label="Patch may be for another build", modal=True, width=560,
                        height=200, tag="shift_modal", pos=(330, 260)):
            if self.compat:
                dpg.add_text(f"Table check: {self.compat['matched']}/"
                             f"{self.compat['checked']} asserts match this build "
                             f"({self.compat['verdict']})", wrap=520)
            dpg.add_text(reason, wrap=520)
            dpg.add_text("The game can crash if these bytes are not the same "
                         "instruction. Apply anyway?", wrap=520)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Apply anyway", tag="btn_shift_yes",
                               callback=lambda: self._confirm_shifted_apply(key, c, pid))
                dpg.add_button(label="Cancel", tag="btn_shift_no",
                               callback=lambda: self._confirm_shifted_cancel(key))
        if self.ui.get("primary_button"):
            dpg.bind_item_theme("btn_shift_yes", self.ui["primary_button"])
        if self.ui.get("text_button"):
            dpg.bind_item_theme("btn_shift_no", self.ui["text_button"])

    def _confirm_shifted_apply(self, key: str, c: dict, pid: int) -> None:
        dpg.delete_item("shift_modal")
        self.log(f"applying shifted patch for {c['description']} (confirmed)…")
        threading.Thread(target=self._confirm_worker, args=(key, c, pid), daemon=True).start()

    def _confirm_shifted_cancel(self, key: str) -> None:
        dpg.delete_item("shift_modal")
        sw = self.switches.get(key)
        if sw:
            sw.set(False)
        self.log("shifted patch cancelled")

    def _confirm_worker(self, key: str, c: dict, pid: int) -> None:
        try:
            handle = AA.auto_enable(pid, c["script"], self.bases, allow_shifted=True)
            self.q.put(("aa_ok", (key, handle, c["description"])))
        except Exception as exc:
            self.q.put(("aa_err", (key, c["description"], f"{type(exc).__name__}: {exc}")))

    def _watch_for_crash(self, game: str, cheat: str, pid: int) -> None:
        """If the game dies within a few seconds of a patch, say so and remember it."""
        import time as _t
        for _ in range(6):
            _t.sleep(1.0)
            if not Path(f"/proc/{pid}").exists():
                self.q.put(("crash", (game, cheat)))
                return

    def _apply_worker(self, c: dict) -> None:
        try:
            AA.auto_enable(self.pid, c["script"], self.bases)
            self.q.put(("chat", f"applied: {c['description']}"))
        except Exception as exc:
            self.q.put(("chat", f"apply failed ({c['description']}): {exc}"))

    # ------------------------------------------------------- troubleshoot wizard
    def trouble_open(self) -> None:
        """A guided path for a table that does not fit this game build."""
        game = self.current_game
        if not game:
            self.log("select a game first")
            return
        if dpg.does_item_exist("trouble"):
            dpg.delete_item("trouble")
        with dpg.window(label=f"Troubleshoot — {game}", tag="trouble", width=720,
                        height=470, pos=(240, 140)):
            dpg.add_text(f"Game: {game}", tag="trouble_title")
            dpg.add_separator()
            dpg.add_text("Step 1 — does this table match the installed build?",
                         tag="trouble_step1")
            dpg.add_text("Reads each assert's bytes out of the table and compares "
                         "them with the DLL/EXE on disk. No game launch needed.",
                         wrap=680)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Check table", tag="btn_t_step1",
                               callback=self.trouble_check)
                dpg.add_text("", tag="trouble_step1_result")
            dpg.add_separator()
            dpg.add_text("Step 2 — is there a newer table for this build?")
            with dpg.group(horizontal=True):
                dpg.add_button(label="Search online", tag="btn_t_step2",
                               callback=self.trouble_online)
                dpg.add_button(label="Use best matching table", tag="btn_t_use",
                               callback=self.trouble_use_best)
            dpg.add_text("", tag="trouble_step2_result", wrap=680)
            dpg.add_separator()
            dpg.add_text("Step 3 — if no table fits: rebuild the cheat by observing "
                         "the value (Find cheat panel).")
            with dpg.group(horizontal=True):
                dpg.add_button(label="Open Find cheat", tag="btn_t_step3",
                               callback=self.trouble_open_find)
            dpg.add_separator()
            with dpg.child_window(height=140, tag="trouble_log"):
                dpg.add_text("", tag="trouble_log_text", wrap=680)
            dpg.add_button(label="Close", tag="btn_t_close",
                           callback=lambda: dpg.delete_item("trouble"))
        if self.ui.get("primary_button"):
            dpg.bind_item_theme("btn_t_step1", self.ui["primary_button"])
        for item in ("btn_t_step2", "btn_t_use", "btn_t_step3", "btn_t_close"):
            if self.ui.get("text_button"):
                dpg.bind_item_theme(item, self.ui["text_button"])

    def _tlog(self, msg: str) -> None:
        if not dpg.does_item_exist("trouble_log_text"):
            return
        current = dpg.get_value("trouble_log_text") or ""
        dpg.set_value("trouble_log_text", (current + "\n" + msg).strip())
        self.log(msg)

    def trouble_open_find(self) -> None:
        dpg.set_value("find_header", True)
        self.log("Find cheat panel opened — scan for the value you see in game")

    def trouble_check(self) -> None:
        game = self.current_game
        if not game:
            return
        dpg.set_value("trouble_step1_result", "checking…")
        threading.Thread(target=lambda g=game: self.q.put(
            ("trouble_check", CC.verify_game(g))), daemon=True).start()

    def trouble_online(self) -> None:
        game = self.current_game
        if not game:
            return
        dpg.set_value("trouble_step2_result", "searching GitHub…")
        threading.Thread(target=lambda g=game: self.q.put(
            ("trouble_online", self._trouble_online_worker(g))), daemon=True).start()

    def _trouble_online_worker(self, game: str) -> dict:
        out = {"game": game, "candidates": [], "best": None}
        try:
            res = CC.verify_game(game, include_online=True)
        except Exception as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"
            return out
        roots = res.get("roots") or []
        out["candidates"] = res.get("tables") or []
        scored = [t for t in out["candidates"] if t.get("checked") and t["matched"]]
        if scored:
            out["best"] = max(scored, key=lambda t: t["matched"])
        return out

    def trouble_use_best(self) -> None:
        best = getattr(self, "trouble_best", None)
        if not best or not best.get("file"):
            self._tlog("no scored candidate yet — press Search online first")
            return
        game = self.current_game
        dest = L.LOG_DIR.parent / "tables" / game / f"new_{Path(best['file']).name}"
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(Path(best["file"]).read_bytes())
            data = CL.write_game(dest.parent)
            self.cheats = data["cheats"]
            self.populate_cheats()
            self._tlog(f"installed {dest.name} ({best['matched']}/{best['checked']} "
                       f"asserts match) — cheat list rebuilt")
        except OSError as exc:
            self._tlog(f"could not install the table: {exc}")

    # ------------------------------------------------------------- table compat
    def _compat_worker(self, game: str) -> None:
        try:
            res = CC.verify_game(game)
        except Exception as exc:
            self.q.put(("compat", {"game": game, "tables": [], "best": None,
                                   "error": f"{type(exc).__name__}: {exc}"}))
            return
        self.q.put(("compat", res))

    def check_compat(self) -> None:
        game = self.current_game
        if not game:
            self.log("select a game first")
            return
        dpg.set_value("compat_text", "checking table…")
        threading.Thread(target=self._compat_worker, args=(game,), daemon=True).start()

    def _on_compat(self, res: dict) -> None:
        game = res.get("game", "?")
        if res.get("error"):
            dpg.set_value("compat_text", "compat check failed")
            self.log(f"{game}: compat check failed — {res['error']}")
            return
        tables = res.get("tables") or []
        if not tables:
            dpg.set_value("compat_text", "no tables to check")
            return
        best = res.get("best") or tables[0]
        self.compat = best
        summary = (f"{best['matched']}/{best['checked']} asserts match this build"
                   if best["checked"] else "no direct-address asserts")
        dpg.set_value("compat_text", summary)
        detail = "" if best["verdict"] == summary else f" ({best['verdict']})"
        self.log(f"{game}: {best['table']} — {summary}{detail}")
        if best.get("drifts"):
            self.log(f"  observed drifts: {', '.join(best['drifts'][:6])}"
                     + ("  (inconsistent — remapping cannot be trusted)"
                        if not best.get("remap_safe") else ""))
        for sample in (best.get("samples") or [])[:3]:
            if "rva" in sample:
                self.log(f"  {sample['module']}+{sample['rva']}: expected "
                         f"{sample['expected']}, found {sample['actual']}"
                         + (f", drift {sample['drift']}" if sample.get("drift") else ""))

    # ------------------------------------------------------------- find cheat
    def _find_value(self, tag: str):
        kind = dpg.get_value("find_kind")
        raw = (dpg.get_value(tag) or "").strip()
        if not raw:
            return None
        try:
            return float(raw) if kind in ("float", "double") else int(float(raw))
        except ValueError:
            self.log(f"'{raw}' is not a number")
            return None

    def _find_ready(self) -> bool:
        if self.pid is None:
            self.log("attach to the game first — a watchpoint needs ptrace")
            return False
        return True

    def find_scan(self) -> None:
        if not self._find_ready():
            return
        kind = dpg.get_value("find_kind")
        value = self._find_value("find_value")
        if value is None:
            self.log("enter the value you see in game")
            return
        self.log(f"scanning for {kind} {value}…")
        threading.Thread(target=self._find_scan_worker, args=(kind, value), daemon=True).start()

    def _find_scan_worker(self, kind: str, value) -> None:
        try:
            hits = F.scan_value(self.pid, value, kind)
            self.q.put(("find_hits", (hits, f"{len(hits)} address(es) hold {value}")))
        except Exception as exc:
            self.q.put(("log", f"scan failed: {type(exc).__name__}: {exc}"))

    def find_refine(self) -> None:
        if not self._find_ready():
            return
        if not self.find_hits:
            self.log("scan first")
            return
        kind = dpg.get_value("find_kind")
        value = self._find_value("find_new")
        if value is None:
            self.log("change the value in game, then enter the new number")
            return
        self.log(f"refining {len(self.find_hits)} candidates against {value}…")
        threading.Thread(target=self._find_refine_worker, args=(kind, value), daemon=True).start()

    def _find_refine_worker(self, kind: str, value) -> None:
        try:
            hits = F.refine(self.pid, self.find_hits, value, kind)
            self.q.put(("find_hits", (hits, f"{len(hits)} candidate(s) left after {value}")))
        except Exception as exc:
            self.q.put(("log", f"refine failed: {type(exc).__name__}: {exc}"))

    def find_watch(self) -> None:
        if not self._find_ready():
            return
        if not self.find_hits:
            self.log("scan first")
            return
        choice = dpg.get_value("find_list")
        addr = int(choice, 16) if choice else self.find_hits[0]
        kind = dpg.get_value("find_kind")
        size = F.FORMATS.get(kind, ("", 4))[1]
        self.log(f"watching writes to {addr:#x} — change the value in game now…")
        threading.Thread(target=self._find_watch_worker, args=(addr, size), daemon=True).start()

    def _find_watch_worker(self, addr: int, size: int) -> None:
        try:
            res = F.watch_writes(self.pid, addr, size, timeout=30.0, hits=1)
        except Exception as exc:
            res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self.q.put(("find_watch", res))

    def find_apply(self) -> None:
        insn = getattr(self, "find_insn", None)
        if not insn or not self._find_ready():
            self.log("watch a write first")
            return
        threading.Thread(target=self._find_apply_worker, args=(insn,), daemon=True).start()

    def _find_apply_worker(self, insn: dict) -> None:
        try:
            patch = F.apply_nop_patch(self.pid, insn, log=lambda m: self.q.put(("log", m)))
            self.q.put(("find_patched", patch))
        except Exception as exc:
            self.q.put(("log", f"patch failed: {type(exc).__name__}: {exc}"))

    def find_undo(self) -> None:
        if not self.find_patch or not self._find_ready():
            self.log("nothing to undo")
            return
        try:
            F.undo_nop_patch(self.pid, self.find_patch, log=lambda m: self.q.put(("log", m)))
            self.find_patch = None
        except Exception as exc:
            self.log(f"undo failed: {exc}")

    # ------------------------------------------------------------- attach
    def auto_attach(self) -> None:
        """Find the running game and attach.

        Cheat tables only name the exe when they contain an aobscanmodule (Far
        Cry 5's is symbol-based), so fall back to the launched executable and to
        the ProcessMonitor-free /proc scan in ctlaunch.
        """
        pids: list[int] = []
        if self.current_game:
            pids = D.running_pids(self.current_game, self.procs)
            if not pids:
                app = L.find_app(self.current_game)
                if app is not None:
                    found = L.game_pid(L.find_exe(app, self.current_game), self.current_game)
                    if found:
                        self.log(f"found {self.current_game} by {found[1]} match (pid {found[0]})")
                        self.attach(found[0])
                        return
        if not pids:
            for g in list_games():
                pids.extend(D.running_pids(g, self.procs))
                if pids:
                    self.current_game = g
                    break
        if not pids:
            self.log("no running game found — press Launch & attach "
                     "(a game started from Steam cannot be traced on this kernel)")
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
            self.log(f"attached to pid {pid} but memory access is denied ({detail}).")
            self.log("  fix: launch the game from Steam with these Launch Options "
                     "(right-click game → Properties → Launch Options):")
            self.log(f"  {L.steam_launch_options()}")
            self.log("  the shim then reads this trainer's pid from "
                     f"{L.TRACER_PID_FILE}; no sudo, no ptrace_scope change.")
        self.populate_cheats()

    def detach(self) -> None:
        self.pid = None
        self.bases = {}
        self.enabled.clear()
        dpg.set_value(self.status, "not attached")
        self._chip("warn")
        self.log("detached")
        self.populate_cheats()

    # ------------------------------------------------------------- launch
    def launch_and_attach(self) -> None:
        game = self.current_game
        if not game:
            label = dpg.get_value("game_list")
            if label:
                game = label.lstrip("▶✓ ")
        if not game:
            self.log("select a game first")
            return
        if L.find_app(game) is None:
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
            res = L.launch(game, wait=True, timeout=120.0,
                           log=lambda msg: self.q.put(("log", msg)))
        except Exception as exc:
            res = {"ok": False, "error": repr(exc)}
        res["game"] = game
        self.q.put(("launch", res))

    def force_stop_click(self) -> None:
        """Kill the game's wine tree — for the state Steam cannot stop."""
        game = self.current_game or (dpg.get_value("game_list") or "").lstrip("▶✓ ")
        if not game:
            self.log("select a game first")
            return
        self.log(f"force-stopping {game} (SIGTERM → SIGKILL → wineserver -k/-k9)")
        threading.Thread(target=lambda g=game: self.q.put(
            ("forcestop", dict(L.force_stop(g, log=lambda m: self.q.put(("log", m))), game=g))),
            daemon=True).start()

    def _on_force_stop(self, res: dict) -> None:
        game = res.get("game", "?")
        if res.get("ok"):
            self.log(f"{game}: stopped ({len(res.get('signalled') or [])} signals, no processes left)")
            self.detach()
        else:
            self.log(f"{game}: {res.get('error')} (prefix {res.get('prefix')})")

    def launch_via_steam_click(self) -> None:
        game = self.current_game or (dpg.get_value("game_list") or "").lstrip("▶✓ ")
        if not game:
            self.log("select a game first")
            return
        self.log(f"asking Steam to launch {game} (Steam's runtime, no tracer shim)")
        self._chip("warn")
        dpg.set_value(self.status, f"Steam launching {game}…")
        threading.Thread(target=lambda g=game: self.q.put(
            ("steamlaunch", dict(L.launch_via_steam(
                g, log=lambda m: self.q.put(("log", m))), game=g))), daemon=True).start()

    def _on_steam_launch(self, res: dict) -> None:
        game = res.get("game", "?")
        if not res.get("ok"):
            self.log(f"{game}: Steam launch failed — {res.get('error')}")
            dpg.set_value(self.status, "launch failed")
            self._chip("error")
            return
        self.log(f"{game}: running as pid {res['pid']} (launched by Steam)")
        self.attach(int(res["pid"]))

    def _on_launch(self, res: dict) -> None:
        game = res.get("game", "?")
        for warning in res.get("warnings") or []:
            self.log(f"note: {warning}")
        if not res.get("ok"):
            self.log(f"launch failed: {res.get('error')}")
            for hint in res.get("diagnosis") or []:
                self.log(f"diagnosis: {hint}")
            if res.get("log_file"):
                self.log(f"launch log: {res['log_file']}")
            dpg.set_value(self.status, "launch failed")
            self._chip("error")
            return
        self.launched = res
        pid = res.get("pid")
        trace = (res.get("trace") or {}).get("detail", "?")
        if res.get("already_running"):
            self.log(f"{game} was already running (pid {pid}); attaching — tracer access {trace}")
        else:
            self.log(f"{game} is running (pid {pid}); tracer access {trace}")
        for hint in res.get("diagnosis") or []:
            self.log(f"diagnosis: {hint}")
        if res.get("log_file"):
            self.log(f"launch log: {res['log_file']}")
        if game != self.current_game:
            self.current_game = game
            self.cheats = CL.load_clean(game)["cheats"]
        self.attach(int(pid))

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
        """Append to the pane AND to the on-disk log.

        On-disk is the point: when something is reported as "error", the answer
        has to be readable after the fact (LAUNCH_LOG below), not only while the
        window is open.
        """
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        self.log_lines.append(msg)
        self.log_lines = self.log_lines[-500:]
        dpg.set_value(self.log_text, "\n".join(self.log_lines))
        try:
            L.LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(TRAINER_LOG, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    def _poll(self, *_) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "chat":
                    dpg.set_value(self.chat_text,
                                  dpg.get_value(self.chat_text) + "\n" + payload + "\n")
                elif kind == "aa_ok":
                    key, handle, name = payload
                    self.enabled[key] = handle
                    if isinstance(handle, dict) and handle.get("type") == "inplace":
                        self.log(f"enabled: {name} ({len(handle.get('patches', []))} patch(es))")
                    else:
                        self.log(f"enabled: {name} (cave {handle.get('cave', 0):#x})")
                    if self.current_game and self.pid:
                        threading.Thread(target=self._watch_for_crash,
                                         args=(self.current_game, name, self.pid),
                                         daemon=True).start()
                elif kind == "trouble_check":
                    res = payload
                    tables = res.get("tables") or []
                    best = res.get("best") or (tables[0] if tables else None)
                    if not best:
                        msg = "no table found for this game"
                    else:
                        msg = (f"{best['matched']}/{best['checked']} asserts match "
                               f"— {best['verdict']}")
                        self._tlog(f"step 1: {best['table']} — {msg}")
                        for s_ in (best.get("samples") or [])[:3]:
                            if "rva" in s_:
                                self._tlog(f"   {s_['module']}+{s_['rva']}: expected "
                                           f"{s_['expected']}, found {s_['actual']}"
                                           + (f", drift {s_['drift']}" if s_.get("drift") else ""))
                    dpg.set_value("trouble_step1_result", msg)
                elif kind == "trouble_online":
                    res = payload
                    if res.get("error"):
                        dpg.set_value("trouble_step2_result", res["error"])
                        self._tlog(f"step 2 failed: {res['error']}")
                    else:
                        cands = res.get("candidates") or []
                        self.trouble_best = res.get("best")
                        good = [c for c in cands if c.get("checked") and c["matched"]]
                        dpg.set_value("trouble_step2_result",
                                      f"{len(cands)} candidate(s); "
                                      f"{len(good)} fit this build")
                        for c in cands[:6]:
                            self._tlog(f"   {c['table'][:44]:44} "
                                       f"{c['matched']}/{c['checked']} {c['verdict']}")
                        if self.trouble_best:
                            self._tlog("   best candidate can be installed with "
                                       "Use best matching table")
                elif kind == "compat":
                    self._on_compat(payload)
                elif kind == "aa_confirm":
                    self._confirm_shifted(payload)
                elif kind == "crash":
                    game, cheat = payload
                    self.log(f"!! the game exited right after patching '{cheat}' — that "
                             f"patch is unsafe on this build (not retrying automatically)")
                    try:
                        unsafe_path = L.LOG_DIR.parent / "clean" / "unsafe.json"
                        unsafe = json.loads(unsafe_path.read_text()) if unsafe_path.exists() else {}
                        unsafe.setdefault(game, {})[cheat] = time.strftime("%Y-%m-%d %H:%M")
                        unsafe_path.parent.mkdir(parents=True, exist_ok=True)
                        unsafe_path.write_text(json.dumps(unsafe, indent=1, ensure_ascii=False))
                    except OSError:
                        pass
                elif kind == "find_hits":
                    hits, msg = payload
                    self.find_hits = hits
                    dpg.set_value("find_status", msg)
                    dpg.configure_item("find_list", items=[f"{h:#x}" for h in hits[:20]])
                    dpg.set_value("find_writer", "")
                    self.log(msg)
                elif kind == "find_watch":
                    if payload.get("ok"):
                        self.find_insn = payload.get("instruction")
                        insn = self.find_insn
                        text = (f"writer at {payload['rip']:#x}: "
                                f"{insn['text'] if insn else '?'}")
                        dpg.set_value("find_writer", text)
                        self.log(text)
                        self.log(F.suggest_patch(insn))
                    else:
                        dpg.set_value("find_writer", str(payload.get("error")))
                        self.log(str(payload.get("error")))
                elif kind == "find_patched":
                    self.find_patch = payload
                    self.log(f"patched the writer at {payload['addr']:#x} "
                             f"(Undo restores {payload['orig'].hex(' ')})")
                elif kind == "reattach":
                    dpg.set_value(self.status, f"attached to {payload} (module owner)")
                    self.log(f"switched to pid {payload} — it owns the script's module")
                elif kind == "aa_err":
                    key, name, err = payload
                    self.log(f"enable failed ({name}): {err}")
                    sw = self.switches.get(key)
                    if sw:
                        sw.set(False)
                elif kind == "log":
                    self.log(payload)
                elif kind == "launch":
                    self._on_launch(payload)
                elif kind == "steamlaunch":
                    self._on_steam_launch(payload)
                elif kind == "forcestop":
                    self._on_force_stop(payload)
        except queue.Empty:
            pass
        dpg.set_frame_callback(dpg.get_frame_count() + 1, self._poll)


def main() -> None:
    L.write_tracer_pid()        # for shims loaded into Steam-launched games
    TRAINER_LOG.parent.mkdir(parents=True, exist_ok=True)
    TRAINER_LOG.write_text(f"=== trainer started {time.strftime('%Y-%m-%d %H:%M:%S')} "
                           f"(pid {__import__('os').getpid()}) ===\n", encoding="utf-8")
    dpg.create_context()
    ui = T.apply()                      # Material theme + type scale, bound globally
    app = App(ui)
    app.build()
    dpg.create_viewport(title="WeCheat", width=1250, height=900)
    dpg.setup_dearpygui()
    dpg.show_viewport()
    dpg.set_primary_window("main", True)
    app.refresh()
    dpg.set_frame_callback(1, app._poll)
    dpg.start_dearpygui()
    dpg.destroy_context()


if __name__ == "__main__":
    main()
