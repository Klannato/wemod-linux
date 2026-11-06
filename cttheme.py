#!/usr/bin/env python3
"""cttheme — Material Design (dark) styling for the Dear PyGui front end.

Two layers, the way Material does it:

* **global theme** — palette, corner radii, paddings, state-layer hover/active
  colours. Bound once with ``dpg.bind_theme``.
* **elevation themes** — surface colours for stacked panels (0 = background,
  1 = window, 2 = card, 3 = menu/dialog). Bound per item with
  ``dpg.bind_item_theme``, which is how Material fakes depth without shadows
  (Dear PyGui has no shadow pass).

Fonts: Roboto if ``assets/Roboto-Regular.ttf`` is present, else Open Sans, else
Dear PyGui's built-in. Sizes follow Material's type scale.

Caveat worth knowing: Dear PyGui has no font fallback chain, and neither Roboto
nor Open Sans carries CJK glyphs, so Chinese/Japanese cheat names render as
boxes. Swapping ``assets/Roboto-Regular.ttf`` for a Noto Sans CJK build fixes it
at the cost of the Material typeface.
"""
from __future__ import annotations

from pathlib import Path

import dearpygui.dearpygui as dpg

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"

# ---------------------------------------------------------------- palette
# Material Design 2 dark scheme, with the state layers Material uses instead of
# per-widget hover colours.
PRIMARY = (187, 134, 252, 255)          # purple 200
PRIMARY_VARIANT = (55, 0, 179, 255)
SECONDARY = (3, 218, 198, 255)          # teal A200
ERROR = (207, 102, 121, 255)
ON_PRIMARY = (0, 0, 0, 255)
ON_SECONDARY = (0, 0, 0, 255)
ON_SURFACE_HIGH = (235, 235, 235, 255)  # 87% white, flattened (DPG has no alpha on text)
ON_SURFACE_MEDIUM = (168, 168, 168, 255)
ON_SURFACE_DISABLED = (92, 92, 92, 255)
OUTLINE = (66, 66, 66, 255)

SURFACES = {
    0: (18, 18, 18, 255),    # background
    1: (30, 30, 30, 255),    # window / app bar
    2: (38, 38, 38, 255),    # card, input frame
    3: (46, 46, 46, 255),    # menu, dialog, modal
}
HOVER_LAYER = (255, 255, 255, 26)        # ~10% white state layer
ACTIVE_LAYER = (255, 255, 255, 46)       # ~18%
SELECT_LAYER = (187, 134, 252, 60)       # primary tint for selected rows

TYPE_SCALE = {          # Material type scale -> Dear PyGui point sizes
    "headline": 26,
    "title": 19,
    "subtitle": 16,
    "body": 15,
    "caption": 13,
}


def _font_file() -> Path | None:
    """Family for the whole UI.

    Noto Sans CJK comes first on purpose: cheat tables are frequently Chinese
    (the Far Cry / Ubisoft ones are Chinese-only), and a Latin-only face renders
    every one of those names as boxes. Noto's Latin is metrically close to
    Roboto, so the Material look survives.
    """
    for cand in (ASSETS / "NotoSansCJK-Regular.ttc",
                 Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
                 ASSETS / "Roboto-Regular.ttf",
                 Path("/usr/share/fonts/truetype/open-sans/OpenSans-Regular.ttf")):
        if cand.is_file():
            return cand
    return None


_CJK_HINTS = ("mvFontRangeHint_Chinese_Simplified_Common",
              "mvFontRangeHint_Chinese_Traditional_Common",
              "mvFontRangeHint_Japanese")


_fonts: dict[int, int] | None = None


def load_fonts() -> dict[int, int]:
    """Register the type scale; returns {size: font_id}. Empty dict on failure.

    Cached: a second font registry with the same files would double the atlas
    for no reason.
    """
    global _fonts
    if _fonts is not None:
        return _fonts
    path = _font_file()
    if path is None:
        _fonts = {}
        return _fonts
    fonts: dict[int, int] = {}
    try:
        tag = dpg.generate_uuid()
        with dpg.font_registry(tag=tag):
            for size in sorted(set(TYPE_SCALE.values())):
                # Dear PyGui 2.x builds the glyph ranges automatically; the old
                # add_font_range_hint() calls are no-ops now.
                fonts[size] = dpg.add_font(str(path), size)
    except Exception:
        _fonts = {}
        return _fonts
    _fonts = fonts
    return fonts


def font_for(role: str) -> int | None:
    """Font id for a type-scale role, or None when fonts are unavailable."""
    return load_fonts().get(TYPE_SCALE.get(role, TYPE_SCALE["body"]))


def bind_font(role: str) -> None:
    """Bind a type-scale role ('title', 'body', …) as the default font."""
    fonts = load_fonts()
    if not fonts:
        return
    size = TYPE_SCALE.get(role, TYPE_SCALE["body"])
    font_id = fonts.get(size)
    if font_id:
        dpg.bind_font(font_id)


def _theme_items(colors: dict, styles: dict) -> int:
    """Create a theme, skipping constants this Dear PyGui build doesn't have.

    Dear PyGui 2.x requires colours and styles to live inside a theme
    *component*; adding them directly to the theme raises 1003 "Incompatible
    parent". mvAll targets every item type.
    """
    tag = dpg.generate_uuid()
    with dpg.theme(tag=tag):
        with dpg.theme_component(dpg.mvAll, parent=tag):
            for name, value in colors.items():
                const = getattr(dpg, name, None)
                if const is not None:
                    dpg.add_theme_color(const, value)
            for name, value in styles.items():
                const = getattr(dpg, name, None)
                if const is not None:
                    dpg.add_theme_style(const, *value if isinstance(value, tuple) else (value,))
    return tag


BASE_COLORS = {
    "mvThemeCol_Text": ON_SURFACE_HIGH,
    "mvThemeCol_TextDisabled": ON_SURFACE_DISABLED,
    "mvThemeCol_WindowBg": SURFACES[1],
    "mvThemeCol_ChildBg": (0, 0, 0, 0),
    "mvThemeCol_PopupBg": SURFACES[2],
    "mvThemeCol_Border": OUTLINE,
    "mvThemeCol_BorderShadow": (0, 0, 0, 0),
    "mvThemeCol_FrameBg": SURFACES[2],
    "mvThemeCol_FrameBgHovered": SURFACES[3],
    "mvThemeCol_FrameBgActive": SURFACES[3],
    "mvThemeCol_TitleBg": SURFACES[1],
    "mvThemeCol_TitleBgActive": SURFACES[1],
    "mvThemeCol_TitleBgCollapsed": SURFACES[1],
    "mvThemeCol_MenuBarBg": SURFACES[1],
    "mvThemeCol_ScrollbarBg": (0, 0, 0, 0),
    "mvThemeCol_ScrollbarGrab": (90, 90, 90, 255),
    "mvThemeCol_ScrollbarGrabHovered": (120, 120, 120, 255),
    "mvThemeCol_ScrollbarGrabActive": PRIMARY,
    "mvThemeCol_CheckMark": PRIMARY,
    "mvThemeCol_SliderGrab": PRIMARY,
    "mvThemeCol_SliderGrabActive": PRIMARY,
    "mvThemeCol_Button": SURFACES[2],
    "mvThemeCol_ButtonHovered": SURFACES[3],
    "mvThemeCol_ButtonActive": PRIMARY_VARIANT,
    "mvThemeCol_Header": SURFACES[2],
    "mvThemeCol_HeaderHovered": SURFACES[3],
    "mvThemeCol_HeaderActive": PRIMARY_VARIANT,
    "mvThemeCol_Separator": OUTLINE,
    "mvThemeCol_SeparatorHovered": PRIMARY,
    "mvThemeCol_SeparatorActive": PRIMARY,
    "mvThemeCol_ResizeGrip": (0, 0, 0, 0),
    "mvThemeCol_ResizeGripHovered": PRIMARY,
    "mvThemeCol_ResizeGripActive": PRIMARY,
    "mvThemeCol_Tab": SURFACES[1],
    "mvThemeCol_TabHovered": SURFACES[3],
    "mvThemeCol_TabActive": SURFACES[2],
    "mvThemeCol_TabUnfocused": SURFACES[1],
    "mvThemeCol_TabUnfocusedActive": SURFACES[2],
    "mvThemeCol_PlotBg": SURFACES[0],
    "mvThemeCol_TableHeaderBg": SURFACES[2],
    "mvThemeCol_TableBorderStrong": OUTLINE,
    "mvThemeCol_TableBorderLight": (48, 48, 48, 255),
    "mvThemeCol_TableRowBg": (0, 0, 0, 0),
    "mvThemeCol_TableRowBgAlt": (255, 255, 255, 8),
    "mvThemeCol_TextSelectedBg": SELECT_LAYER,
    "mvThemeCol_NavHighlight": PRIMARY,
    "mvThemeCol_ModalWindowDimBg": (0, 0, 0, 150),
    "mvThemeCol_DragDropTarget": SECONDARY,
}

BASE_STYLES = {
    "mvStyleVar_WindowPadding": (16, 16),
    "mvStyleVar_FramePadding": (12, 7),
    "mvStyleVar_CellPadding": (8, 4),
    "mvStyleVar_ItemSpacing": (8, 8),
    "mvStyleVar_ItemInnerSpacing": (8, 4),
    "mvStyleVar_IndentSpacing": 20,
    "mvStyleVar_ScrollbarSize": 12,
    "mvStyleVar_GrabMinSize": 12,
    "mvStyleVar_WindowRounding": 8,
    "mvStyleVar_ChildRounding": 6,
    "mvStyleVar_FrameRounding": 4,      # Material's 4dp corners
    "mvStyleVar_PopupRounding": 6,
    "mvStyleVar_ScrollbarRounding": 12,
    "mvStyleVar_GrabRounding": 4,
    "mvStyleVar_TabRounding": 4,
    "mvStyleVar_WindowBorderSize": 0,   # depth comes from surface colour, not borders
    "mvStyleVar_ChildBorderSize": 0,
    "mvStyleVar_PopupBorderSize": 1,
    "mvStyleVar_FrameBorderSize": 0,
    "mvStyleVar_WindowTitleAlign": (0.0, 0.5),
    "mvStyleVar_ButtonTextAlign": (0.5, 0.5),
}


def build_global() -> int:
    """The one theme bound application-wide."""
    return _theme_items(BASE_COLORS, BASE_STYLES)


_elevation_cache: dict[int, int] = {}


def elevation(level: int) -> int:
    """Surface theme for a stacked panel (0..3). Cached — DPG themes are cheap but not free."""
    level = max(0, min(3, level))
    if level in _elevation_cache:
        return _elevation_cache[level]
    colors = {k: v for k, v in BASE_COLORS.items()}
    colors["mvThemeCol_ChildBg"] = SURFACES[level]
    colors["mvThemeCol_WindowBg"] = SURFACES[level]
    _elevation_cache[level] = _theme_items(colors, BASE_STYLES)
    return _elevation_cache[level]


def contained_button() -> int:
    """Filled button — one per screen, for the primary action."""
    colors = {k: v for k, v in BASE_COLORS.items()}
    colors.update({
        "mvThemeCol_Button": PRIMARY,
        "mvThemeCol_ButtonHovered": (203, 158, 255, 255),
        "mvThemeCol_ButtonActive": (167, 110, 238, 255),
        "mvThemeCol_Text": ON_PRIMARY,
    })
    return _theme_items(colors, dict(BASE_STYLES, mvStyleVar_FrameRounding=(4,)))


def text_button() -> int:
    """Borderless button — the Material default for anything secondary."""
    colors = {k: v for k, v in BASE_COLORS.items()}
    colors.update({
        "mvThemeCol_Button": (0, 0, 0, 0),
        "mvThemeCol_ButtonHovered": HOVER_LAYER,
        "mvThemeCol_ButtonActive": ACTIVE_LAYER,
        "mvThemeCol_Text": PRIMARY,
    })
    return _theme_items(colors, BASE_STYLES)


def status_chip(color: tuple[int, int, int, int]) -> int:
    """Coloured caption text — attached / detached / error."""
    colors = {k: v for k, v in BASE_COLORS.items()}
    colors["mvThemeCol_Text"] = color
    return _theme_items(colors, BASE_STYLES)


# Semantic roles for the app.
GREEN = (129, 199, 132, 255)
AMBER = (255, 213, 79, 255)
RED = ERROR


def apply() -> dict[str, int]:
    """Build and bind everything; returns the named themes the GUI reuses."""
    global _global_theme
    fonts = load_fonts()
    _global_theme = build_global()
    dpg.bind_theme(_global_theme)
    if fonts:
        dpg.bind_font(fonts[TYPE_SCALE["body"]])
    return {
        "global": _global_theme,
        "primary_button": contained_button(),
        "text_button": text_button(),
        "ok": status_chip(GREEN),
        "warn": status_chip(AMBER),
        "error": status_chip(RED),
        "fonts": fonts,
    }


_global_theme: int | None = None
