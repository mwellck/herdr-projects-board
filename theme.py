"""herdr's colours, so the board matches the theme herdr draws itself with.

herdr has no API for its palette, so this reads its config (theme name, auto_switch, [theme.custom] and
its light/dark variants) and starts from the published palette of each built-in theme. Custom colours
are exact; built-in palettes are the themes' upstream colours, which herdr's may differ from by a shade.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from board import read_toml

TOKENS = ("base", "surface0", "surface1", "overlay0", "subtext0", "text", "accent",
          "red", "green", "yellow", "blue", "mauve", "teal", "peach")


def _p(base, surface0, surface1, overlay0, subtext0, text, accent, red, green, yellow, blue, mauve, teal, peach):
    return dict(zip(TOKENS, (base, surface0, surface1, overlay0, subtext0, text, accent,
                             red, green, yellow, blue, mauve, teal, peach)))


# Upstream palettes of herdr's built-in themes, mapped to herdr's token names.
PALETTES = {
    "catppuccin": _p("#1e1e2e", "#313244", "#45475a", "#6c7086", "#a6adc8", "#cdd6f4", "#cba6f7",
                     "#f38ba8", "#a6e3a1", "#f9e2af", "#89b4fa", "#cba6f7", "#94e2d5", "#fab387"),
    "catppuccin-latte": _p("#eff1f5", "#ccd0da", "#bcc0cc", "#9ca0b0", "#6c6f85", "#4c4f69", "#8839ef",
                           "#d20f39", "#40a02b", "#df8e1d", "#1e66f5", "#8839ef", "#179299", "#fe640b"),
    "tokyo-night": _p("#1a1b26", "#292e42", "#414868", "#565f89", "#a9b1d6", "#c0caf5", "#7aa2f7",
                      "#f7768e", "#9ece6a", "#e0af68", "#7aa2f7", "#bb9af7", "#73daca", "#ff9e64"),
    "tokyo-night-day": _p("#e1e2e7", "#c4c8da", "#a8aecb", "#848cb5", "#6172b0", "#3760bf", "#2e7de9",
                          "#f52a65", "#587539", "#8c6c3e", "#2e7de9", "#9854f1", "#118c74", "#b15c00"),
    "gruvbox": _p("#282828", "#3c3836", "#504945", "#928374", "#a89984", "#ebdbb2", "#fabd2f",
                  "#fb4934", "#b8bb26", "#fabd2f", "#83a598", "#d3869b", "#8ec07c", "#fe8019"),
    "gruvbox-light": _p("#fbf1c7", "#ebdbb2", "#d5c4a1", "#928374", "#7c6f64", "#3c3836", "#b57614",
                        "#9d0006", "#79740e", "#b57614", "#076678", "#8f3f71", "#427b58", "#af3a03"),
    "one-dark": _p("#282c34", "#3e4451", "#4b5263", "#5c6370", "#828997", "#abb2bf", "#61afef",
                   "#e06c75", "#98c379", "#e5c07b", "#61afef", "#c678dd", "#56b6c2", "#d19a66"),
    "one-light": _p("#fafafa", "#e5e5e6", "#d4d4d4", "#a0a1a7", "#696c77", "#383a42", "#4078f2",
                    "#e45649", "#50a14f", "#c18401", "#4078f2", "#a626a4", "#0184bc", "#986801"),
    "solarized": _p("#002b36", "#073642", "#0d4654", "#586e75", "#839496", "#93a1a1", "#268bd2",
                    "#dc322f", "#859900", "#b58900", "#268bd2", "#6c71c4", "#2aa198", "#cb4b16"),
    "solarized-light": _p("#fdf6e3", "#eee8d5", "#ddd6c1", "#93a1a1", "#657b83", "#586e75", "#268bd2",
                          "#dc322f", "#859900", "#b58900", "#268bd2", "#6c71c4", "#2aa198", "#cb4b16"),
    "kanagawa": _p("#1f1f28", "#2a2a37", "#363646", "#727169", "#c8c093", "#dcd7ba", "#7e9cd8",
                   "#e46876", "#98bb6c", "#e6c384", "#7e9cd8", "#957fb8", "#7aa89f", "#ffa066"),
    "kanagawa-lotus": _p("#f2ecbc", "#e5ddb0", "#dcd5ac", "#8a8980", "#716e61", "#545464", "#4d699b",
                         "#c84053", "#6f894e", "#de9800", "#4d699b", "#624c83", "#597b75", "#cc6d00"),
    "rose-pine": _p("#191724", "#26233a", "#403d52", "#6e6a86", "#908caa", "#e0def4", "#c4a7e7",
                    "#eb6f92", "#9ccfd8", "#f6c177", "#31748f", "#c4a7e7", "#9ccfd8", "#ebbcba"),
    "rose-pine-dawn": _p("#faf4ed", "#f2e9e1", "#dfdad9", "#9893a5", "#797593", "#575279", "#907aa9",
                         "#b4637a", "#56949f", "#ea9d34", "#286983", "#907aa9", "#56949f", "#d7827e"),
}
NAMED = {"black": "#000000", "white": "#ffffff", "red": "#ff0000", "green": "#00ff00", "blue": "#0000ff",
         "yellow": "#ffff00", "magenta": "#ff00ff", "cyan": "#00ffff", "gray": "#808080", "grey": "#808080"}


def rgb(value):
    """'#1e1e2e', 'rgb(30,30,46)' or a basic colour name as (r, g, b); None for anything else (e.g. reset)."""
    value = str(value).strip().lower()
    if re.fullmatch(r"#[0-9a-f]{6}", value):
        return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))
    m = re.fullmatch(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", value)
    if m:
        return tuple(min(255, int(v)) for v in m.groups())
    return rgb(NAMED[value]) if value in NAMED else None


def is_light(colour):
    r, g, b = colour
    return 0.2126 * r + 0.7152 * g + 0.0722 * b > 140


def host_is_light():
    """macOS's appearance, for herdr's auto_switch; elsewhere dark."""
    if sys.platform != "darwin":
        return False
    out = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True)
    return "Dark" not in out.stdout


def herdr_config_path(env=os.environ, home=None):
    """Where herdr reads its config: HERDR_CONFIG_PATH, else $XDG_CONFIG_HOME/herdr, else ~/.config/herdr."""
    if env.get("HERDR_CONFIG_PATH"):
        return Path(env["HERDR_CONFIG_PATH"]).expanduser()
    base = Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else Path(home or Path.home()) / ".config"
    return base / "herdr" / "config.toml"


def herdr_palette(config_path=None, light=None):
    """herdr's current palette as {token: (r, g, b)}, or None when herdr uses the terminal's colours
    or a theme this board does not know (the caller falls back)."""
    path = Path(config_path or herdr_config_path())
    theme = read_toml(path.read_text()).get("theme", {}) if path.exists() else {}
    name = theme.get("name", "catppuccin")
    mode = None
    if theme.get("auto_switch"):
        light = host_is_light() if light is None else light
        mode = "light" if light else "dark"
        name = theme.get(f"{mode}_name") or name
    if name not in PALETTES:
        return None
    palette = {k: rgb(v) for k, v in PALETTES[name].items()}
    custom = theme.get("custom", {})
    for layer in (custom, custom.get(mode, {}) if mode else {}):
        for key, value in layer.items():
            if key in TOKENS and not isinstance(value, dict) and rgb(value):
                palette[key] = rgb(value)
    return palette


# The xterm 256-colour cube and grey ramp, for terminals curses drives through 256 indexes.
_STEPS = (0, 95, 135, 175, 215, 255)


def nearest_256(colour):
    def dist(a, b):
        return sum((x - y) ** 2 for x, y in zip(a, b))
    r, g, b = colour
    cube = min(range(6), key=lambda i: abs(_STEPS[i] - r)), min(range(6), key=lambda i: abs(_STEPS[i] - g)), \
        min(range(6), key=lambda i: abs(_STEPS[i] - b))
    cube_rgb = tuple(_STEPS[i] for i in cube)
    grey_i = max(0, min(23, round((sum(colour) / 3 - 8) / 10)))
    grey_rgb = (8 + 10 * grey_i,) * 3
    if dist(colour, grey_rgb) < dist(colour, cube_rgb):
        return 232 + grey_i
    return 16 + 36 * cube[0] + 6 * cube[1] + cube[2]
