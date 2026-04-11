"""Display/render-model orchestration extracted from SuccessorChat.

This helper owns the chat-area row building, empty-state rendering, live
stream previews, and static footer paint logic. The chat remains the owner
of state, timing, and higher-level coordination; this module only consumes
that state to produce the same visuals through stable wrapper entrypoints.
"""

from __future__ import annotations

import math
import time
from typing import Any

from .bash.runner import BashRunner
from .chat_agent_loop import _message_has_tool_artifact
from .render.cells import (
    ATTR_BOLD,
    ATTR_DIM,
    ATTR_ITALIC,
    Grid,
    Style,
)
from .render.chat_intro import paint_empty_state as paint_empty_state_surface
from .render.chat_rows import (
    RenderedRow,
    fade_prepainted_rows as fade_prepainted_chat_rows,
    highlight_spans as highlight_row_spans,
    paint_chat_row as paint_chat_scene_row,
    render_md_lines_with_search as render_markdown_rows_with_search,
    render_running_tool_card_rows as render_running_chat_card_rows,
    render_subagent_card_rows as render_subagent_chat_card_rows,
    render_tool_card_rows as render_tool_chat_card_rows,
)
from .render.chat_viewport import compute_viewport_decision
from .render.markdown import (
    LaidOutLine,
    LaidOutSpan,
    PreparedMarkdown,
)
from .render.paint import fill_region, paint_text
from .render.text import ease_out_cubic, lerp_rgb
from .render.theme import ThemeVariant
from .tools_registry import tool_label
from .streaming_tool_preview import build_streaming_tool_preview


def _trim_trailing_empty_markdown_lines(lines: list[LaidOutLine]) -> list[LaidOutLine]:
    """Drop trailing empty paragraph rows that only create dead air.

    This is intentionally conservative: code blocks, headers, quotes,
    and any other tagged rows stay intact. We only strip plain empty
    lines at the tail so in-flight tool previews can sit directly
    beneath the last real streamed line.
    """
    trimmed = list(lines)
    while trimmed:
        tail = trimmed[-1]
        if tail.line_tag:
            break
        if tail.indent:
            break
        if any(span.text for span in tail.spans):
            break
        trimmed.pop()
    return trimmed


class ChatDisplayRuntime:
    """Owns render-model assembly while the chat owns state."""

    def __init__(
        self,
        host: Any,
        *,
        rendered_row_cls: type[RenderedRow],
        user_prefix: str,
        successor_prefix: str,
        prefix_width: int,
        fade_in_s: float,
        spinner_fps: float,
        spinner_frames: str,
        reasoning_preview_chars: int,
    ) -> None:
        self._host = host
        self._rendered_row_cls = rendered_row_cls
        self._user_prefix = user_prefix
        self._successor_prefix = successor_prefix
        self._prefix_width = prefix_width
        self._fade_in_s = fade_in_s
        self._spinner_fps = spinner_fps
        self._spinner_frames = spinner_frames
        self._reasoning_preview_chars = reasoning_preview_chars

    def _row(self, **kwargs: Any) -> RenderedRow:
        return self._rendered_row_cls(**kwargs)

    def paint_chat_area(
        self,
        grid: Grid,
        top: int,
        bottom: int,
        width: int,
        theme: ThemeVariant,
    ) -> None:
        host = self._host
        if bottom <= top or width <= 2:
            return

        if self.is_empty_chat() and self.has_intro_art():
            self.paint_empty_state(grid, top, bottom, width, theme)
            return

        density = host._current_density()
        geometry = compute_viewport_decision(
            width=width,
            top=top,
            bottom=bottom,
            density=density,
            committed_height=0,
            scroll_offset=host.scroll_offset,
            auto_scroll=host._auto_scroll,
            last_total_height=host._last_total_height,
        )

        committed = self.build_message_lines(geometry.body_width, theme)
        stream_lines = (
            self.build_streaming_lines(geometry.body_width, theme)
            if host._stream is not None
            else []
        )
        all_rows = committed + stream_lines
        committed_h = len(all_rows)
        viewport = compute_viewport_decision(
            width=width,
            top=top,
            bottom=bottom,
            density=density,
            committed_height=committed_h,
            scroll_offset=host.scroll_offset,
            auto_scroll=host._auto_scroll,
            last_total_height=host._last_total_height,
        )
        host.scroll_offset = viewport.scroll_offset
        host._auto_scroll = viewport.auto_scroll
        host._last_chat_h = viewport.last_chat_h
        host._last_chat_w = viewport.last_chat_w
        host._last_total_height = viewport.last_total_height

        combined = all_rows[viewport.start:viewport.end]

        paint_y = max(top, bottom - len(combined))
        for i, row in enumerate(combined):
            y = paint_y + i
            if y >= bottom:
                break
            self.paint_chat_row(grid, viewport.body_x, y, viewport.body_width, row, theme)

    def is_empty_chat(self) -> bool:
        host = self._host
        if host._stream is not None:
            return False
        for msg in host.messages:
            if _message_has_tool_artifact(msg):
                return False
            if getattr(msg, "is_boundary", False):
                return False
            if getattr(msg, "is_summary", False):
                return False
            if not msg.synthetic:
                return False
        return True

    def has_intro_art(self) -> bool:
        return self.resolve_intro_art() is not None

    def resolve_intro_art(self):
        host = self._host
        if host._intro_art_resolved:
            return host._intro_art
        from .render.intro_art import load_intro_art

        if host.profile is None:
            host._intro_art = None
        else:
            host._intro_art = load_intro_art(host.profile.chat_intro_art)
        host._intro_art_resolved = True
        return host._intro_art

    def paint_empty_state(
        self,
        grid: Grid,
        top: int,
        bottom: int,
        width: int,
        theme: ThemeVariant,
    ) -> None:
        paint_empty_state_surface(
            grid,
            top,
            bottom,
            width,
            theme,
            panel_lines=self.build_intro_panel_lines(),
            resolve_intro_art=self.resolve_intro_art,
        )

    def build_intro_panel_lines(self) -> list[tuple[str, str, bool, bool]]:
        host = self._host
        rows: list[tuple[str, str, bool, bool]] = []

        rows.append(("profile", "", True, False))
        rows.append(("", host.profile.name if host.profile else "(none)", False, False))
        rows.append(("", "", False, False))

        provider_cfg = (host.profile.provider or {}) if host.profile else {}
        provider_type = provider_cfg.get("type") or "llamacpp"
        model = provider_cfg.get("model") or host.client.model
        if provider_type == "openai_compat":
            base_url = provider_cfg.get("base_url", "")
            if "openrouter" in base_url:
                provider_label = "openrouter"
            elif "openai.com" in base_url:
                provider_label = "openai"
            else:
                provider_label = "openai-compat"
        else:
            provider_label = provider_type
        rows.append(("provider", "", True, False))
        rows.append(("", provider_label, False, False))
        rows.append(("", model, False, False))
        try:
            window = host._resolve_context_window()
            rows.append(("", f"{window:,} tokens", False, False))
        except Exception:
            pass
        if host._server_health_ok is True:
            rows.append(("", "● reachable", False, False))
        elif host._server_health_ok is False:
            rows.append(("", "○ unreachable", False, False))
        rows.append(("", "", False, False))

        tools = list(host.profile.tools) if host.profile and host.profile.tools else []
        rows.append(("tools", "", True, False))
        if tools:
            for tool in tools:
                rows.append(("", tool_label(tool), False, False))
        else:
            rows.append(("", "(none enabled)", False, False))
        rows.append(("", "", False, False))

        theme_name = host.theme.name if host.theme else "steel"
        mode = host.display_mode if host.display_mode else "dark"
        density = host.density.name if host.density else "normal"
        rows.append(("appearance", "", True, False))
        rows.append(("", f"{theme_name} · {mode} · {density}", False, False))
        rows.append(("", "", False, False))

        rows.append(("type / for commands · press ? for help", "", False, True))
        return rows

    def paint_chat_row(
        self,
        grid: Grid,
        x: int,
        y: int,
        body_width: int,
        row: RenderedRow,
        theme: ThemeVariant,
    ) -> None:
        paint_chat_scene_row(
            grid,
            x,
            y,
            body_width,
            row,
            theme,
            prefix_width=self._prefix_width,
            elapsed=self._host.elapsed,
        )

    def build_message_lines(
        self,
        body_width: int,
        theme: ThemeVariant,
    ) -> list[RenderedRow]:
        host = self._host
        if host._compaction_anim is not None:
            now = time.monotonic()
            phase, phase_t = host._compaction_anim.phase_at(now)
            if phase == "done":
                host._compaction_anim = None
            elif phase in ("anticipation", "fold", "waiting"):
                return self.build_rows_from_messages(
                    host._compaction_anim.pre_compact_snapshot,
                    body_width,
                    theme,
                )
            else:
                return self.build_rows_from_messages(
                    host.messages,
                    body_width,
                    theme,
                    anim_phase=phase,
                    anim_t=phase_t,
                )

        return self.build_rows_from_messages(host.messages, body_width, theme)

    def build_rows_from_messages(
        self,
        messages: list[Any],
        body_width: int,
        theme: ThemeVariant,
        *,
        global_fade_alpha: float = 1.0,
        anticipation_glow: bool = False,
        anim_phase: str = "",
        anim_t: float = 1.0,
    ) -> list[RenderedRow]:
        host = self._host
        out: list[RenderedRow] = []
        now = time.monotonic()
        n = len(messages)
        spacing = host._current_density().message_spacing
        md_width = max(1, body_width - self._prefix_width)

        for i, msg in enumerate(messages):
            age = now - msg.created_at
            fade_t = (
                ease_out_cubic(min(1.0, age / self._fade_in_s))
                if age < self._fade_in_s
                else 1.0
            )
            base_color = theme.fg if msg.role == "user" else theme.accent
            if msg.synthetic:
                base_color = theme.fg_dim
            if anticipation_glow:
                base_color = lerp_rgb(base_color, theme.accent_warm, 0.35)
            if fade_t < 1.0:
                base_color = lerp_rgb(theme.fg_subtle, base_color, fade_t)

            if msg.is_boundary:
                materialize_t = 1.0
                if anim_phase == "materialize":
                    materialize_t = ease_out_cubic(anim_t)
                elif anim_phase in ("", "reveal", "toast"):
                    materialize_t = 1.0
                out.append(
                    self._row(
                        is_boundary=True,
                        boundary_meta=msg.boundary_meta,
                        materialize_t=materialize_t,
                        base_color=base_color,
                        fade_alpha=global_fade_alpha,
                    )
                )
                if i < n - 1:
                    for _ in range(spacing):
                        out.append(self._row(base_color=base_color))
                continue

            if msg.is_summary:
                summary_alpha = global_fade_alpha
                materialize_t = 1.0
                if anim_phase == "materialize":
                    summary_alpha = 0.0
                    materialize_t = ease_out_cubic(anim_t)
                elif anim_phase == "reveal":
                    summary_alpha = ease_out_cubic(anim_t)
                    materialize_t = 1.0
                elif anim_phase in ("toast", ""):
                    materialize_t = 1.0
                    if not anim_phase:
                        summary_alpha = global_fade_alpha

                if msg.boundary_meta is not None:
                    out.append(
                        self._row(
                            is_boundary=True,
                            boundary_meta=msg.boundary_meta,
                            materialize_t=materialize_t,
                            base_color=base_color,
                            fade_alpha=global_fade_alpha,
                        )
                    )

                prefix = "▼ "
                md_lines = msg.body.lines(md_width)
                summary_color = lerp_rgb(theme.fg_subtle, theme.fg_dim, 0.6)
                rendered_rows = self.render_md_lines_with_search(
                    md_lines,
                    msg.display_text,
                    [],
                    prefix,
                    summary_color,
                )
                for row in rendered_rows:
                    row.is_summary = True
                    row.fade_alpha = summary_alpha
                out.extend(rendered_rows)
                if i < n - 1:
                    for _ in range(spacing):
                        out.append(self._row(base_color=base_color))
                continue

            if _message_has_tool_artifact(msg):
                card_rows = self.render_tool_card_rows(msg, body_width, theme)
                if global_fade_alpha < 1.0:
                    card_rows = self.fade_prepainted_rows(
                        card_rows,
                        theme.bg,
                        1.0 - global_fade_alpha,
                    )
                out.extend(card_rows)
                if i < n - 1:
                    for _ in range(spacing):
                        out.append(self._row(base_color=base_color))
                continue

            if msg.role != "user" and not msg.display_text.strip():
                if i < n - 1:
                    for _ in range(spacing):
                        out.append(self._row(base_color=base_color))
                continue

            prefix = self._user_prefix if msg.role == "user" else self._successor_prefix
            md_lines = msg.body.lines(md_width)

            msg_matches: list[tuple[int, int, int]] = []
            if host._search_active and host._search_matches:
                for mi_focused, start, end in host._search_matches:
                    if mi_focused != i:
                        continue
                    is_focused = (
                        host._search_matches.index((mi_focused, start, end))
                        == host._search_focused
                    )
                    msg_matches.append((start, end, 2 if is_focused else 1))

            if not md_lines:
                out.append(
                    self._row(
                        leading_text=prefix,
                        leading_attrs=ATTR_BOLD,
                        leading_color_kind="accent",
                        base_color=base_color,
                        fade_alpha=global_fade_alpha,
                    )
                )
            else:
                rendered_rows = self.render_md_lines_with_search(
                    md_lines,
                    msg.display_text,
                    msg_matches,
                    prefix,
                    base_color,
                )
                if global_fade_alpha < 1.0:
                    for row in rendered_rows:
                        row.fade_alpha = global_fade_alpha
                out.extend(rendered_rows)

            if i < n - 1:
                for _ in range(spacing):
                    out.append(self._row(base_color=base_color))
        return out

    @staticmethod
    def fade_prepainted_rows(
        rows: list[RenderedRow],
        bg_color: int,
        toward_bg_amount: float,
    ) -> list[RenderedRow]:
        return fade_prepainted_chat_rows(rows, bg_color, toward_bg_amount)

    def render_tool_card_rows(
        self,
        msg: Any,
        body_width: int,
        theme: ThemeVariant,
    ) -> list[RenderedRow]:
        return render_tool_chat_card_rows(msg, body_width, theme)

    def render_running_tool_card_rows(
        self,
        msg: Any,
        body_width: int,
        theme: ThemeVariant,
        runner: BashRunner,
    ) -> list[RenderedRow]:
        return render_running_chat_card_rows(msg, body_width, theme, runner)

    def render_subagent_card_rows(
        self,
        msg: Any,
        body_width: int,
        theme: ThemeVariant,
    ) -> list[RenderedRow]:
        return render_subagent_chat_card_rows(msg, body_width, theme)

    def render_md_lines_with_search(
        self,
        md_lines: list[LaidOutLine],
        msg_raw_text: str,
        matches: list[tuple[int, int, int]],
        prefix: str,
        base_color: int,
    ) -> list[RenderedRow]:
        query = self._host._search_query.lower() if self._host._search_active else ""
        return render_markdown_rows_with_search(
            md_lines,
            query,
            matches,
            prefix,
            base_color,
            prefix_width=self._prefix_width,
        )

    def highlight_spans(
        self,
        spans: list[LaidOutSpan],
        query: str,
    ) -> list[LaidOutSpan]:
        return highlight_row_spans(spans, query)

    def build_streaming_lines(
        self,
        body_width: int,
        theme: ThemeVariant,
    ) -> list[RenderedRow]:
        host = self._host
        if host._stream is None:
            return []
        now = time.monotonic()
        spinner_idx = int(now * self._spinner_fps) % len(self._spinner_frames)
        spinner = self._spinner_frames[spinner_idx]

        block_in_flight = False
        if host._stream_bash_detector is not None:
            content_so_far = host._stream_bash_detector.cleaned_text()
            block_in_flight = host._stream_bash_detector.is_inside_block()
        else:
            content_so_far = "".join(host._stream_content)

        tool_calls_in_flight = getattr(host._stream, "tool_calls_so_far", None) or []
        out: list[RenderedRow] = []

        if not content_so_far:
            text = (
                f"{spinner} thinking… ({host._stream_reasoning_chars} chars)"
                if host._stream_reasoning_chars > 0
                else f"{spinner} thinking…"
            )
            out.append(
                self._row(
                    leading_text=self._successor_prefix,
                    leading_attrs=ATTR_BOLD,
                    leading_color_kind="accent",
                    body_spans=(LaidOutSpan(text=text),),
                    base_color=theme.accent,
                )
            )

            reasoning_text = host._stream.reasoning_so_far
            if reasoning_text:
                tail = reasoning_text[-self._reasoning_preview_chars:]
                tail = " ".join(tail.split())
                if tail:
                    avail_w = max(1, body_width - self._prefix_width - 4)
                    if len(tail) > avail_w:
                        tail = "…" + tail[-(avail_w - 1):]
                    out.append(
                        self._row(
                            leading_text=" " * self._prefix_width + "  ↳ ",
                            leading_color_kind="fg_dim",
                            leading_attrs=ATTR_DIM,
                            body_spans=(
                                LaidOutSpan(
                                    text=tail,
                                    attrs=ATTR_DIM | ATTR_ITALIC,
                                ),
                            ),
                            base_color=theme.fg_subtle,
                        )
                    )
            for tc in tool_calls_in_flight:
                raw_args = tc.get("raw_arguments", "")
                if not raw_args:
                    continue
                out.extend(
                    self.streaming_tool_call_preview_rows(
                        name=tc.get("name") or "",
                        raw_arguments=raw_args,
                        call_index=tc.get("index", 0),
                        body_width=body_width,
                        theme=theme,
                        spinner=spinner,
                    )
                )
            return out

        live_md = PreparedMarkdown(content_so_far)
        md_width = max(1, body_width - self._prefix_width)
        md_lines = live_md.lines(md_width)
        if block_in_flight or tool_calls_in_flight:
            md_lines = _trim_trailing_empty_markdown_lines(md_lines)
        if md_lines:
            last_line = md_lines[-1]
            last_line.spans.append(LaidOutSpan(text="▌"))
        else:
            md_lines = [LaidOutLine(spans=[LaidOutSpan(text="▌")])]
        for line_idx, md_line in enumerate(md_lines):
            leading = (
                self._successor_prefix if line_idx == 0 else " " * self._prefix_width
            )
            out.append(
                self._row(
                    leading_text=leading,
                    leading_attrs=ATTR_BOLD if line_idx == 0 else 0,
                    leading_color_kind="accent",
                    body_spans=tuple(md_line.spans),
                    base_color=theme.accent,
                    line_tag=md_line.line_tag,
                    body_indent=md_line.indent,
                )
            )

        if block_in_flight:
            out.append(
                self._row(
                    leading_text=" " * self._prefix_width + "  ↳ ",
                    leading_color_kind="fg_dim",
                    leading_attrs=ATTR_DIM,
                    body_spans=(
                        LaidOutSpan(
                            text=f"{spinner} queuing bash command…",
                            attrs=ATTR_DIM | ATTR_ITALIC,
                        ),
                    ),
                    base_color=theme.fg_subtle,
                )
            )

        for tc in tool_calls_in_flight:
            raw_args = tc.get("raw_arguments", "")
            if not raw_args:
                continue
            out.extend(
                self.streaming_tool_call_preview_rows(
                    name=tc.get("name") or "",
                    raw_arguments=raw_args,
                    call_index=tc.get("index", 0),
                    body_width=body_width,
                    theme=theme,
                    spinner=spinner,
                )
            )
        return out

    def streaming_tool_call_preview_rows(
        self,
        *,
        name: str,
        raw_arguments: str,
        call_index: int,
        body_width: int,
        theme: ThemeVariant,
        spinner: str,
    ) -> list[RenderedRow]:
        host = self._host
        cache_key = (id(host._stream), call_index)
        prior = host._streaming_preview_cache.get(cache_key)
        preview = build_streaming_tool_preview(
            name=name,
            raw_arguments=raw_arguments,
            prior=prior,
        )
        if preview.sticky:
            host._streaming_preview_cache[cache_key] = preview
        display_text = preview.display_text or raw_arguments
        max_preview_lines = 5
        avail_w = max(10, body_width - self._prefix_width - 6)

        raw_lines = display_text.replace("\\n", "\n").split("\n")
        wrapped: list[str] = []
        for raw_line in raw_lines:
            if not raw_line:
                wrapped.append("")
                continue
            offset = 0
            while offset < len(raw_line):
                wrapped.append(raw_line[offset:offset + avail_w])
                offset += avail_w
        tail_lines = wrapped[-max_preview_lines:] or [""]
        tail_lines[-1] = tail_lines[-1] + "▌"

        rows: list[RenderedRow] = []
        header_text = f"{spinner} {preview.glyph} {preview.label}"
        if preview.hint:
            header_text += f"  {preview.hint}"
        elif preview.status:
            header_text += f" — {preview.status}"
        rows.append(
            self._row(
                leading_text=" " * self._prefix_width + "  ↳ ",
                leading_color_kind="fg_dim",
                leading_attrs=ATTR_DIM,
                body_spans=(
                    LaidOutSpan(
                        text=header_text,
                        attrs=ATTR_DIM | ATTR_BOLD,
                    ),
                ),
                base_color=theme.accent_warm,
            )
        )
        for line in tail_lines:
            rows.append(
                self._row(
                    leading_text=" " * self._prefix_width + "    ",
                    leading_color_kind="fg_dim",
                    leading_attrs=ATTR_DIM,
                    body_spans=(
                        LaidOutSpan(
                            text=line,
                            attrs=ATTR_DIM | ATTR_ITALIC,
                        ),
                    ),
                    base_color=theme.fg_subtle,
                )
            )
        return rows

    def paint_static_footer(
        self,
        grid: Grid,
        y: int,
        width: int,
        theme: ThemeVariant,
    ) -> None:
        host = self._host
        try:
            snapshot = host._context_usage_snapshot()
            used = snapshot.used_tokens
            window = snapshot.window
            pct = snapshot.fill_pct
            state = snapshot.state
        except Exception:
            used = host._fallback_token_count()
            window = host._resolve_context_window()
            pct = used / window if window > 0 else 0.0
            budget = host._agent_budget()
            state = budget.state(used)

        fill_region(grid, 0, y, width, 1, style=Style(bg=theme.bg_footer))

        pulse = 0.0
        if state in ("autocompact", "blocking"):
            pulse = 0.5 + 0.5 * math.sin(host.elapsed * 0.5 * 2 * math.pi)

        label = f" ctx {used:>7}/{window:>7} "
        state_badge = ""
        if state == "autocompact":
            state_badge = " ◉ COMPACT "
        elif state == "blocking":
            state_badge = " ⚠ BLOCKED "

        compacting_badge = ""
        if host._compaction_worker is not None and host._compaction_anim is not None:
            spinner_idx = int(host.elapsed * 10) % len(self._spinner_frames)
            elapsed_compact = host._compaction_worker.elapsed()
            minutes, seconds = divmod(int(elapsed_compact), 60)
            n_rounds = host._compaction_anim.rounds_summarized
            compacting_badge = (
                f" {self._spinner_frames[spinner_idx]} compacting "
                f"{n_rounds}r · {minutes:02d}:{seconds:02d} "
            )

        warming_badge = ""
        if host._cache_warmer is not None and host._cache_warmer.is_running():
            spinner_idx = int(host.elapsed * 10) % len(self._spinner_frames)
            warming_badge = f" {self._spinner_frames[spinner_idx]} warming "

        right_label = (
            f"{compacting_badge}{warming_badge}{state_badge} "
            f"{pct * 100:5.2f}%  {host.client.model[:20]} "
        )
        label_style = Style(fg=theme.fg_dim, bg=theme.bg_footer, attrs=ATTR_DIM)

        if state == "blocking":
            right_fg = theme.accent_warn
        elif state == "autocompact":
            right_fg = lerp_rgb(theme.accent_warm, theme.accent_warn, pulse)
        elif compacting_badge or warming_badge:
            right_fg = theme.accent_warm
        elif state == "warning":
            right_fg = theme.accent_warm
        else:
            right_fg = theme.fg
        right_style = Style(fg=right_fg, bg=theme.bg_footer, attrs=ATTR_BOLD)

        paint_text(grid, label, 0, y, style=label_style)

        bar_x = len(label) + 1
        right_x = max(0, width - len(right_label))
        bar_w = max(0, right_x - bar_x - 1)

        if bar_w > 0:
            filled = int(round(bar_w * pct))
            empty = bar_w - filled
            if state == "ok":
                bar_fg = lerp_rgb(theme.accent, theme.accent_warm, pct / 0.85)
            elif state == "warning":
                bar_fg = theme.accent_warm
            elif state == "autocompact":
                bar_fg = lerp_rgb(theme.accent_warm, theme.accent_warn, pulse)
            else:
                bar_fg = theme.accent_warn
            if filled > 0:
                paint_text(
                    grid,
                    "█" * filled,
                    bar_x,
                    y,
                    style=Style(fg=bar_fg, bg=theme.bg_footer),
                )
            if empty > 0:
                paint_text(
                    grid,
                    "░" * empty,
                    bar_x + filled,
                    y,
                    style=Style(fg=theme.fg_subtle, bg=theme.bg_footer),
                )

        paint_text(grid, right_label, right_x, y, style=right_style)
