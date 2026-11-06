#!/usr/bin/env python3
"""Render the GUI for a bounded number of frames, then exit.

Used to verify the Material theme actually renders (and to grab a screenshot)
without leaving a window open. Needs a display; on this box DISPLAY=:1.

Run: python3 tests/test_gui_render.py [frames]
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import dearpygui.dearpygui as dpg
import cttheme as T
from ctgui import App


def main() -> int:
    frames = int(sys.argv[1]) if len(sys.argv) > 1 else 240
    preselect = sys.argv[2] if len(sys.argv) > 2 else None
    dpg.create_context()
    ui = T.apply()
    app = App(ui)
    app.build()
    dpg.create_viewport(title="wemod-linux", width=1250, height=900)
    dpg.setup_dearpygui()
    dpg.show_viewport()
    dpg.set_primary_window("main", True)
    app.refresh()
    if preselect:
        app.on_select_game(None, preselect, None)
    dpg.set_frame_callback(1, app._poll)

    n = 0
    while dpg.is_dearpygui_running() and n < frames:
        dpg.render_dearpygui_frame()
        n += 1
        time.sleep(0.02)
    print(f"rendered {n} frames, items in main: {len(dpg.get_item_children('main', 1) or [])}")
    dpg.destroy_context()
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
