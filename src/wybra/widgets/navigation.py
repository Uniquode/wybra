from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MenuAlignment = Literal["start", "centre", "end"]
MenuLayout = Literal["vertical", "horizontal"]
MenuSpacing = Literal["compact", "normal", "relaxed"]


@dataclass(frozen=True, slots=True)
class KeyboardShortcut:
    key: str
    label: str | None = None
    modifiers: tuple[str, ...] = ()

    @property
    def display_label(self) -> str:
        parts = (*self.modifiers, self.label or self.key)
        return " ".join(part for part in parts if part)


@dataclass(frozen=True, slots=True)
class NavigationItem:
    label: str
    path: str
    description: str | None = None
    icon_token: str | None = None
    shortcut: KeyboardShortcut | None = None
    active: bool = False
    disabled: bool = False
    css_class: str | None = None


@dataclass(frozen=True, slots=True)
class NavigationMenu:
    items: tuple[NavigationItem, ...]
    label: str = "Navigation"
    layout: MenuLayout = "vertical"
    spacing: MenuSpacing = "normal"
    alignment: MenuAlignment = "start"
    item_alignment: MenuAlignment = "start"
    shortcut_scope: str | None = None
    css_class: str | None = None

    @property
    def has_items(self) -> bool:
        return bool(self.items)


@dataclass(frozen=True, slots=True)
class DropdownPanel:
    label: str
    menu: NavigationMenu
    id: str
    alignment: MenuAlignment = "end"
    css_class: str | None = None

    @property
    def has_items(self) -> bool:
        return self.menu.has_items


__all__ = (
    "DropdownPanel",
    "KeyboardShortcut",
    "NavigationItem",
    "NavigationMenu",
)
