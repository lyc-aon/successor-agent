"""Frontend-backed session reviewer shell generation.

This module hosts the built reviewer app inside generated HTML shells.
The Python side owns bundle discovery, payload shaping, and theme export.
The frontend side owns the actual UI.
"""

from __future__ import annotations

import json
from html import escape
from pathlib import Path

from .config import load_chat_config
from .loader import builtin_root
from .render.theme import ThemeVariant, all_themes, normalize_theme_name


def _rgb_hex(value: int) -> str:
    return f"#{value:06x}"


def _variant_payload(variant: ThemeVariant) -> dict[str, str]:
    return {
        "bg": _rgb_hex(variant.bg),
        "bg_input": _rgb_hex(variant.bg_input),
        "bg_footer": _rgb_hex(variant.bg_footer),
        "fg": _rgb_hex(variant.fg),
        "fg_dim": _rgb_hex(variant.fg_dim),
        "fg_subtle": _rgb_hex(variant.fg_subtle),
        "accent": _rgb_hex(variant.accent),
        "accent_warm": _rgb_hex(variant.accent_warm),
        "accent_warn": _rgb_hex(variant.accent_warn),
    }


def theme_catalog_payload() -> list[dict[str, object]]:
    catalog: list[dict[str, object]] = []
    for theme in all_themes():
        catalog.append(
            {
                "name": theme.name,
                "icon": theme.icon,
                "description": theme.description,
                "dark": _variant_payload(theme.dark),
                "light": _variant_payload(theme.light),
            }
        )
    return catalog


def viewer_defaults() -> tuple[str, str]:
    cfg = load_chat_config()
    theme = normalize_theme_name(cfg.get("theme")) or "steel"
    mode = str(cfg.get("display_mode") or "light")
    if mode not in {"light", "dark"}:
        mode = "light"
    return theme, mode


def _reviewer_asset_root() -> Path:
    return builtin_root() / "reviewer_app"


def _load_asset(name: str) -> str:
    path = _reviewer_asset_root() / name
    if not path.exists():
        raise FileNotFoundError(
            f"missing reviewer asset {path}; run `npm --prefix reviewer-app install` "
            "and `npm --prefix reviewer-app run build` from the repo root"
        )
    return path.read_text(encoding="utf-8")


def _escape_inline_script(text: str) -> str:
    return text.replace("</script>", "<\\/script>")


def render_reviewer_html(payload: dict[str, object], *, title: str) -> str:
    css = _load_asset("reviewer-app.css")
    js = _load_asset("reviewer-app.js")
    payload_json = (
        json.dumps(payload)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    safe_title = escape(title)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title}</title>
  <style>{css}</style>
</head>
<body>
  <div id="root"></div>
  <script>window.__SUCCESSOR_REVIEWER_BOOTSTRAP__ = {payload_json};</script>
  <script type="module">{_escape_inline_script(js)}</script>
</body>
</html>
"""


def write_reviewer_html(
    output: str | Path,
    payload: dict[str, object],
    *,
    title: str,
) -> Path:
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_reviewer_html(payload, title=title), encoding="utf-8")
    return output_path
