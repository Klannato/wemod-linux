#!/usr/bin/env python3
"""ctwidgets — small custom controls Dear PyGui doesn't ship.

Only one so far: `Switch`, the pill toggle the WeMod layout is built around.
Dear PyGui has no switch widget, so it is drawn in a drawlist (rounded track +
knob) and clicks are delivered through an item-clicked handler.

Everything here is built with explicit `parent=` instead of `with` blocks: these
widgets are created inside an already-pushed container stack (a scrolling child
window of table rows), and stacking another context manager mid-list corrupts
that stack — the visible symptom is the *next* item failing with error 1003/1009.
"""
from __future__ import annotations

import dearpygui.dearpygui as dpg

import cttheme as T

TRACK_OFF = (58, 58, 58, 255)
TRACK_ON = T.PRIMARY
KNOB_OFF = (158, 158, 158, 255)
KNOB_ON = (255, 255, 255, 255)


class Switch:
    """A 44×22 pill toggle bound to a callback.

    `on_change(new_value)` fires after the visual state has already flipped, so
    a failing backend can call `switch.set(old_value)` to roll the UI back.
    """

    def __init__(self, parent: int | str, value: bool = False, on_change=None,
                 tag: int | str | None = None, width: int = 46, height: int = 24):
        self.value = bool(value)
        self.on_change = on_change
        self.width = width
        self.height = height

        # Pass tag only when given: add_drawlist(tag=None) is rejected by the
        # C++ side (SystemError with no message), which is easy to misread as a
        # parenting problem.
        kwargs: dict = {"width": width, "height": height, "parent": parent}
        if tag is not None:
            kwargs["tag"] = tag
        self.tag = dpg.add_drawlist(**kwargs)
        self._track = dpg.draw_rectangle(
            (0, 2), (width, height - 2), parent=self.tag,
            color=(0, 0, 0, 0), fill=TRACK_OFF, rounding=(height - 4) / 2)
        self._knob = dpg.draw_circle(
            (height / 2, height / 2), (height - 8) / 2, parent=self.tag,
            color=(0, 0, 0, 0), fill=KNOB_OFF)

        reg = dpg.add_item_handler_registry()
        dpg.add_item_clicked_handler(parent=reg, callback=self._clicked)
        dpg.bind_item_handler_registry(self.tag, reg)
        self._paint()

    def _paint(self) -> None:
        dpg.configure_item(self._track, fill=TRACK_ON if self.value else TRACK_OFF)
        x = self.width - self.height / 2 if self.value else self.height / 2
        dpg.configure_item(self._knob, center=(x, self.height / 2),
                           fill=KNOB_ON if self.value else KNOB_OFF)

    def _clicked(self, sender=None, app_data=None, user_data=None) -> None:
        self.toggle()

    def toggle(self) -> None:
        self.set(not self.value, notify=True)

    def set(self, value: bool, notify: bool = False) -> None:
        self.value = bool(value)
        self._paint()
        if notify and self.on_change:
            self.on_change(self.value)


_switches: dict[int | str, Switch] = {}


def add_switch(parent: int | str, value: bool = False, on_change=None, tag=None) -> Switch:
    sw = Switch(parent, value=value, on_change=on_change, tag=tag)
    _switches[sw.tag] = sw
    return sw


def get_switch(tag) -> Switch | None:
    return _switches.get(tag)


def clear() -> None:
    """Drop references to switches whose parent container was deleted."""
    _switches.clear()
