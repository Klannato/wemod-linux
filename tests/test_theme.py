#!/usr/bin/env python3
"""Headless smoke test for cttheme: themes and fonts build without a viewport.

Catches the two ways this breaks: a Dear PyGui constant that doesn't exist in
this build (themes silently lose a colour), and a font file the loader refuses.

Run: python3 tests/test_theme.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import dearpygui.dearpygui as dpg
import cttheme


def main() -> int:
    dpg.create_context()
    themes = cttheme.apply()

    missing = [n for n in ("mvThemeCol_Button", "mvThemeCol_FrameBg", "mvStyleVar_FrameRounding")
               if getattr(dpg, n, None) is None]
    fonts = themes.pop("fonts", {})
    print(f"theme ids: { {k: v for k, v in themes.items()} }")
    print(f"fonts: {sorted(fonts)} (family: {cttheme._font_file()})")

    with dpg.window(tag="main"):
        dpg.add_text("Header", tag="hdr")
        dpg.add_input_text(hint="filter", width=-1)
        dpg.add_button(label="Launch & attach")
        dpg.add_listbox(["a", "b"])
        with dpg.child_window(height=120):
            dpg.add_text("card")

    dpg.bind_item_theme("main", cttheme.elevation(1))
    ok = not missing
    if missing:
        print(f"MISSING CONSTANTS: {missing}")
    print("PASS" if ok else "FAIL")
    dpg.destroy_context()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
