"""Successor command-line interface.

Subcommands:

    successor                — show help
    successor chat           — chat interface (real llama.cpp streaming)
    successor setup          — profile creation wizard with live preview
    successor config         — three-pane profile config menu
    successor doctor         — terminal capability check
    successor login          — OAuth device flow for Kimi Code
    successor skills         — list loaded skills
    successor tools          — list registered tools
    successor record         — record a session (bundle or raw input JSONL)
    successor replay         — replay a recorded input stream
    successor playback       — inspect or open a recording bundle reviewer
    successor review         — alias for playback
    successor snapshot       — headless render of a chat scenario
    successor bench          — renderer benchmark
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__


# ─── subcommands ───


def cmd_chat(args: argparse.Namespace) -> int:
    """Open the chat with re-entry support for the config menu.

    Plays the active profile's intro animation on first launch.
    Subsequent re-entries (after exiting the config menu) skip the
    intro to avoid being annoying. The chat's `_pending_action` flag
    is checked after each chat.run() to decide whether to open the
    config menu and then resume the chat.
    """
    from .chat import SuccessorChat
    from .profiles import get_active_profile
    from .wizard import run_config_menu

    profile = get_active_profile()
    first_launch = True

    while True:
        initial_input = ""
        # Intro only on the very first launch in this session — re-entry
        # after the config menu skips it.
        if first_launch and profile.intro_animation:
            initial_input = _play_intro_animation(profile.intro_animation)
        first_launch = False

        chat = SuccessorChat(profile=profile, initial_input=initial_input)
        chat.run()

        if getattr(chat, "_pending_action", None) == "config":
            # User opened the config menu from inside the chat. Run it,
            # then resume the chat with whichever profile they want
            # active when they exit.
            requested_name = run_config_menu()
            if requested_name:
                from .profiles import get_profile
                next_profile = get_profile(requested_name)
                if next_profile is not None:
                    profile = next_profile
                else:
                    profile = get_active_profile()
            else:
                # Cancelled — resume with whatever active_profile says
                profile = get_active_profile()
            continue

        # Normal exit — leave the loop
        break

    return 0


def cmd_config(args: argparse.Namespace) -> int:
    """Run the config menu standalone (not from inside the chat).

    On exit, drops into the chat with the user's selected active
    profile. If they cancel, drops into the chat with the previously-
    active profile.
    """
    from .chat import SuccessorChat
    from .profiles import get_active_profile, get_profile
    from .wizard import run_config_menu

    requested_name = run_config_menu()
    if requested_name:
        profile = get_profile(requested_name) or get_active_profile()
    else:
        profile = get_active_profile()

    SuccessorChat(profile=profile).run()
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """Run the profile creation wizard.

    Multi-step interactive wizard with a live preview pane that uses
    the actual chat renderer to show the user's choices in real time.
    On save, transitions directly into the chat with the new profile
    active.

    First-launch flourish: the SUCCESSOR emergence animation plays
    before the wizard opens, so a user typing `successor setup` for
    the first time sees the brand portrait morph in. Skippable with
    any keypress (the intro App handles that).
    """
    from .chat import SuccessorChat
    from .wizard import run_setup_wizard

    # Play the bundled SUCCESSOR intro animation BEFORE the wizard
    # opens. Always plays — first-time users will see this; repeat
    # users can skip with any key. If the bundled frames are missing
    # (broken install) the helper exits cleanly.
    _play_intro_animation("successor")

    saved_profile = run_setup_wizard()
    if saved_profile is None:
        # User cancelled — exit cleanly without launching the chat
        return 0

    # Drop straight into the chat with the new profile active. We
    # do NOT replay the intro here — the user already saw it before
    # the wizard, and watching it twice is annoying.
    SuccessorChat(profile=saved_profile).run()
    return 0


def _play_intro_animation(name: str) -> str:
    """Play a registered intro animation, blocking until it finishes.

    For v0, only "successor" is supported — it plays the bundled
    11-frame numbered emergence sequence ending on the held oracle
    frame, held for a couple of seconds. `hero.txt` is separate
    empty-state art for the chat and is not part of this animation.
    `10-title.txt` remains the legacy filename for the final frame.
    Any keypress skips ahead. Unknown intro names are silently ignored
    so a profile that references a future intro doesn't break the chat.
    """
    if name != "successor":
        # Future: walk ~/.config/successor/intros/<name>/ for user intros.
        return ""
    from .intros import run_successor_intro

    try:
        return run_successor_intro()
    except RuntimeError:
        # Frames dir missing on this install — skip silently.
        return ""


def cmd_skills(args: argparse.Namespace) -> int:
    """List every loaded skill (built-in + user) with size + source.

    Skills are selected per profile and loaded on demand by the chat's
    internal `skill` tool. This command is the inventory view: it shows
    what is installed, where it came from, and the approximate prompt
    budget once a skill is actually loaded.
    """
    from .skills import SKILL_REGISTRY

    SKILL_REGISTRY.reload()
    skills = SKILL_REGISTRY.all()
    if not skills:
        print("successor: no skills loaded")
        print(f"  drop *.md files into {SKILL_REGISTRY.kind}/ to add them:")
        from .loader import builtin_root, config_dir

        print(f"    builtin: {builtin_root() / 'skills'}")
        print(f"    user:    {config_dir() / 'skills'}")
        return 0

    print(f"successor · skills ({len(skills)} loaded)")
    print()
    name_w = max(len(s.name) for s in skills)
    total_tokens = sum(s.estimated_tokens for s in skills)
    for skill in skills:
        source = SKILL_REGISTRY.source_of(skill.name) or "?"
        tokens = skill.estimated_tokens
        print(f"  {skill.name:<{name_w}}  {source:>7}  ~{tokens:>5} tokens")
        if skill.description:
            # Soft-wrap the description to a comfortable column width
            desc = skill.description
            indent = " " * (4 + name_w)
            max_w = 80 - len(indent)
            while desc:
                if len(desc) <= max_w:
                    print(f"{indent}{desc}")
                    break
                # Break at the last space before max_w
                cut = desc.rfind(" ", 0, max_w)
                if cut < 0:
                    cut = max_w
                print(f"{indent}{desc[:cut]}")
                desc = desc[cut:].lstrip()
        indent = " " * (4 + name_w)
        if skill.when_to_use:
            print(f"{indent}when:  {skill.when_to_use}")
        if skill.allowed_tools:
            print(f"{indent}tools: {', '.join(skill.allowed_tools)}")
        print()
    print(f"  {len(skills)} skills · ~{total_tokens:,} tokens total")
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    """List both native chat tools and Python-import plugin tools."""
    from .tools import TOOL_REGISTRY
    from .tools_registry import AVAILABLE_TOOLS, selectable_tool_names

    TOOL_REGISTRY.reload()
    plugin_tools = TOOL_REGISTRY.all()

    print("successor · tools")
    print()
    native_names = selectable_tool_names()
    print(f"  native chat tools ({len(native_names)})")
    print()
    native_label_w = max(len(AVAILABLE_TOOLS[name].label) for name in native_names)
    native_name_w = max(len(name) for name in native_names)
    for name in native_names:
        descriptor = AVAILABLE_TOOLS[name]
        print(
            f"  {descriptor.label:<{native_label_w}}  "
            f"{name:<{native_name_w}}  native",
        )
        if descriptor.description:
            indent = " " * (4 + native_label_w + 2 + native_name_w + 8)
            print(f"{indent}{descriptor.description}")
        print()

    print(f"  plugin tools ({len(plugin_tools)})")
    print()
    if not plugin_tools:
        print("  (none)")
    else:
        name_w = max(len(t.name) for t in plugin_tools)
        for tool in plugin_tools:
            source = TOOL_REGISTRY.source_of(tool.name) or "?"
            print(f"  {tool.name:<{name_w}}  {source:>7}")
            if tool.description:
                indent = " " * (4 + name_w)
                print(f"{indent}{tool.description}")
            print()
    print("  plugin tools are loaded from the Python-import registry and are not dispatched by the native chat loop unless wired in explicitly.")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    import fcntl
    import struct
    import termios

    print(f"successor {__version__}")
    print(f"  python      {sys.version.split()[0]}")
    print(f"  platform    {sys.platform}")
    print()

    is_tty = sys.stdout.isatty()
    print(f"  stdout      {'tty' if is_tty else 'not a tty'}")
    if is_tty:
        try:
            data = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\x00" * 8)
            rows, cols, _, _ = struct.unpack("hhhh", data)
            print(f"  size        {cols} cols × {rows} rows")
        except OSError as e:
            print(f"  size        unknown ({e})")

    print(f"  TERM        {os.environ.get('TERM', '<unset>')}")
    print(f"  COLORTERM   {os.environ.get('COLORTERM', '<unset>')}")
    truecolor = os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")
    print(f"  truecolor   {'yes' if truecolor else 'no (renderer still emits 24-bit SGR)'}")
    print()

    from .render.measure import char_width, text_width

    samples = [
        ("ascii a",   "a"),
        ("CJK 中",    "中"),
        ("braille ⠿", "⠿"),
        ("emoji 🦊",  "🦊"),
        ("combine ñ", "n\u0303"),
    ]
    print("  measure cell widths:")
    for label, ch in samples:
        print(f"    {label:12s}  char_width={char_width(ch[0])}  text_width={text_width(ch)}")
    print()

    from .intros import successor_intro_frame_paths
    from .loader import builtin_root
    intro_dir = builtin_root() / "intros" / "successor"
    if intro_dir.exists():
        intro_frames = successor_intro_frame_paths(intro_dir)
        print(f"  intro       {len(intro_frames)} successor emergence frames")
        if (intro_dir / "hero.txt").exists():
            print("  hero        chat empty-state hero art present")
        print(f"  intro_dir   {intro_dir}")

    # ─── Active profile + provider connectivity check ───
    #
    # Useful when the user runs `successor doctor` to debug "why
    # isn't my chat working?" — the terminal capability dump alone
    # doesn't help. This section probes the active profile's
    # provider, reports whether the endpoint is reachable, and
    # surfaces the resolved context window so the user can verify
    # everything's wired before launching the chat.
    print()
    print("  active profile:")
    try:
        from .profiles import get_active_profile
        from .providers import make_provider
        from .config import load_chat_config
        from .playback import recordings_dir
        from .web import (
            available_provider_status,
            browser_runtime_status,
            resolve_browser_config,
            resolve_holonet_config,
            resolve_vision_config,
            vision_runtime_status,
        )
        profile = get_active_profile()
        print(f"    name        {profile.name}")
        provider_cfg = profile.provider or {}
        provider_type = provider_cfg.get("type", "<unset>")
        provider_type_norm = str(provider_type).strip().lower()
        base_url = provider_cfg.get("base_url", "<unset>")
        model = provider_cfg.get("model", "<unset>")
        api_key = provider_cfg.get("api_key", "")
        api_key_status = (
            "set"
            if isinstance(api_key, str) and api_key.strip()
            else "(none — local server)"
            if provider_type == "llamacpp"
            else "MISSING"
        )
        print(f"    provider    {provider_type}")
        print(f"    base_url    {base_url}")
        print(f"    model       {model}")
        print(f"    api_key     {api_key_status}")

        # Construct the client and probe it. Both probes are short
        # and tolerant of failure — if the server is down or the
        # auth is wrong we want a friendly status line, not a stack
        # trace.
        try:
            client = make_provider(provider_cfg)
        except Exception as exc:  # noqa: BLE001
            print(f"    status      provider construction failed: {exc}")
            return 0

        # Health check (HTTP reachability)
        health_ok: bool | None = None
        try:
            health_ok = bool(getattr(client, "health", lambda: False)())
        except Exception:  # noqa: BLE001
            health_ok = False
        if health_ok:
            print("    status      reachable")
        elif health_ok is False:
            print("    status      UNREACHABLE — is the server running?")
        else:
            print("    status      unknown (no health check on this provider)")

        # Context window detection
        ctx: int | None = None
        detect = getattr(client, "detect_context_window", None)
        if callable(detect):
            try:
                ctx = detect()
            except Exception:  # noqa: BLE001
                ctx = None
            override = provider_cfg.get("context_window")
            if isinstance(override, int) and override > 0:
                print(f"    ctx window  {override} tokens (profile override)")
            elif isinstance(ctx, int) and ctx > 0:
                print(f"    ctx window  {ctx} tokens (auto-detected)")
            else:
                print("    ctx window  unknown — falls back to 262144 default")

        raw_max_tokens = provider_cfg.get("max_tokens")
        gen_budget: int | None = None
        gen_note: str | None = None
        if isinstance(raw_max_tokens, int) and raw_max_tokens > 0:
            gen_budget = raw_max_tokens
            gen_note = "profile"
        elif provider_type_norm in {"llamacpp", "llama", "llama.cpp"}:
            gen_budget = ctx if isinstance(ctx, int) and ctx > 0 else 262144
            gen_note = "auto"
        if isinstance(gen_budget, int) and gen_budget > 0:
            note = f" ({gen_note})" if gen_note else ""
            print(f"    gen budget  {gen_budget} tokens{note}")

        detect_caps = getattr(client, "detect_runtime_capabilities", None)
        if callable(detect_caps):
            try:
                caps = detect_caps()
            except Exception:  # noqa: BLE001
                caps = None
            total_slots = getattr(caps, "total_slots", None)
            endpoint_slots = bool(getattr(caps, "endpoint_slots", False))
            parallel_tools = bool(
                getattr(caps, "supports_parallel_tool_calls", False)
            )
            if isinstance(total_slots, int) and total_slots > 0:
                slot_note = "/slots on" if endpoint_slots else "/slots off"
                print(f"    slots       {total_slots} total ({slot_note})")
            if caps is not None:
                print(
                    "    tool calls  "
                    + ("parallel supported" if parallel_tools else "serial only")
                )

        tools = tuple(profile.tools or ())
        if tools:
            from .tools_registry import tool_label

            print("    tools       " + ", ".join(tool_label(name) for name in tools))
        if "holonet" in tools:
            holo_cfg = resolve_holonet_config(profile)
            holo_status = available_provider_status(holo_cfg)
            enabled = [name for name, ok in holo_status.items() if ok]
            disabled = [name for name, ok in holo_status.items() if not ok]
            print(f"    holonet     default={holo_cfg.default_provider}")
            print(
                "    holonet ok  "
                + (", ".join(enabled) if enabled else "none")
            )
            if disabled:
                print(
                    "    holonet off "
                    + ", ".join(disabled)
                )
        if "browser" in tools:
            browser_cfg = resolve_browser_config(profile)
            browser_status = browser_runtime_status(profile.name, browser_cfg)
            print(
                "    browser     "
                + ("playwright ready" if browser_status.package_available else "playwright missing")
            )
            runtime_note = " (external runtime)" if browser_status.using_external_runtime else ""
            print(f"    browser py  {browser_status.python_executable}{runtime_note}")
            print(f"    browser ch  {browser_status.channel or '(default chromium)'}")
            if browser_status.executable_path:
                print(f"    browser exe {browser_status.executable_path}")
            print(f"    browser dir {browser_status.user_data_dir}")
        if "vision" in tools:
            vision_cfg = resolve_vision_config(profile)
            vision_client = make_provider(profile.provider or {"type": "llamacpp"})
            vision_status = vision_runtime_status(vision_cfg, client=vision_client)
            print(
                "    vision      "
                + ("ready" if vision_status.tool_available else "unavailable")
                + f" ({vision_status.mode})"
            )
            if vision_status.provider_type:
                print(f"    vision type {vision_status.provider_type}")
            if vision_status.base_url:
                print(f"    vision url  {vision_status.base_url}")
            if vision_status.model:
                print(f"    vision mod  {vision_status.model}")
            print(f"    vision note {vision_status.reason}")
        chat_cfg = load_chat_config()
        autorecord = bool(chat_cfg.get("autorecord", True))
        print(
            "    recording   "
            + ("auto-record on" if autorecord else "auto-record off")
        )
        if autorecord:
            print(f"    record dir  {recordings_dir()}")
    except Exception as exc:  # noqa: BLE001
        print(f"    error: could not load active profile ({exc})")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    """Run the Kimi Code OAuth device flow and create a profile.

    1. Request device authorization from auth.kimi.com
    2. Print verification URL + user code
    3. Poll until the user authorizes
    4. Save the token to credentials storage
    5. Fetch available models
    6. Write a kimi-code profile
    """
    from .oauth import (
        KIMI_CODE_CLIENT_ID,
        DEFAULT_OAUTH_HOST,
        OAuthToken,
        request_device_authorization,
        request_device_token,
    )
    from .oauth.storage import save_token
    import urllib.request
    import webbrowser

    print("Kimi Code — OAuth device authorization")
    print()

    # Step 1: request device code
    try:
        auth = request_device_authorization()
    except Exception as exc:
        print(f"error: device authorization failed: {exc}")
        return 1

    # Step 2: show URL + code
    print("Please visit the following URL to finish authorization:")
    print()
    print(f"  {auth.verification_uri_complete}")
    print()
    print(f"  User code: {auth.user_code}")
    print()
    try:
        webbrowser.open(auth.verification_uri_complete)
    except Exception:
        pass

    # Step 3: poll for token
    print("Waiting for authorization", end="", flush=True)
    deadline = time.time() + (auth.expires_in or 600)
    token = None
    while time.time() < deadline:
        time.sleep(max(auth.interval, 1))
        print(".", end="", flush=True)
        try:
            status, data = request_device_token(
                auth.device_code,
                client_id=KIMI_CODE_CLIENT_ID,
                oauth_host=DEFAULT_OAUTH_HOST,
            )
        except Exception as exc:
            print(f"\nerror: token request failed: {exc}")
            return 1
        if status == 200 and "access_token" in data:
            token = OAuthToken.from_response(data)
            break
        error = str(data.get("error") or "")
        if error == "expired_token":
            print("\nerror: device code expired. Please try again.")
            return 1
        if error == "slow_down":
            time.sleep(5)
    print()

    if token is None:
        print("error: authorization timed out. Please try again.")
        return 1

    # Step 4: save token
    save_token("oauth/kimi-code", token)
    print("Token saved.")

    # Step 5: fetch models
    model_name = "kimi-k2-5"
    try:
        req = urllib.request.Request(
            "https://api.kimi.com/coding/v1/models",
            headers={
                "Authorization": f"Bearer {token.access_token}",
                "User-Agent": "successor/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            models_data = json.loads(resp.read().decode("utf-8"))
        model_ids = [
            m.get("id", "")
            for m in models_data.get("data", [])
            if isinstance(m, dict) and m.get("id")
        ]
        if "kimi-k2-5" in model_ids:
            model_name = "kimi-k2-5"
        elif model_ids:
            model_name = model_ids[0]
    except Exception:
        pass  # model list is optional — use default

    # Step 6: write profile
    profiles_dir = Path.home() / ".config" / "successor" / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    profile_path = profiles_dir / "kimi-code.json"
    profile_data = {
        "name": "kimi-code",
        "description": "Kimi Code platform via OAuth",
        "provider": {
            "type": "openai_compat",
            "base_url": "https://api.kimi.com/coding/v1",
            "model": model_name,
        },
        "oauth": {"storage": "file", "key": "oauth/kimi-code"},
        "tools": [
            "read_file", "write_file", "edit_file",
            "bash", "subagent", "holonet", "browser", "vision",
        ],
    }
    profile_path.write_text(
        json.dumps(profile_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Profile written: {profile_path}")
    print(f"  model: {model_name}")
    print()
    print("Run `successor chat --profile kimi-code` to start chatting.")
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    """Run the chat with recording enabled.

    Default behavior writes a full bundle directory with raw input,
    frame timeline, trace events, and a self-contained playback viewer.
    Passing a `.jsonl` output path or `--input-only` keeps the legacy
    raw-input-only mode for minimal repro captures.
    """
    from .chat import SuccessorChat
    from .playback import (
        RecordingBundle,
        bundle_path_from_input,
        is_bundle_path,
    )
    from .recorder import Recorder

    path = bundle_path_from_input(args.output)
    if args.input_only or not is_bundle_path(path):
        if path.suffix.lower() != ".jsonl":
            path = path.with_suffix(".jsonl")
        print(f"recording input to {path} (Ctrl+C to stop)")
        with Recorder(path) as rec:
            chat = SuccessorChat(recorder=rec)
            chat.run()
        print(f"\nrecording saved: {path}")
        print("note: this is input-only. Use a bundle directory for playback.html.")
        return 0

    bundle = RecordingBundle(
        path,
        title="Successor session playback",
        description="Recorded via `successor record`.",
        frame_interval_s=args.frame_interval,
    )
    print(f"recording local-only bundle to {bundle.root} (Ctrl+C to stop)")
    with bundle as rec:
        chat = SuccessorChat(recorder=rec)
        chat.run()
    summary = bundle.finalize(trace_path=chat.session_trace_path)
    print(f"\nrecording saved: {bundle.root}")
    print(f"  input     {bundle.input_path}")
    print(f"  viewer    {bundle.viewer_path}")
    print(f"  trace     {bundle.trace_jsonl_path}")
    print("  privacy   local-only bundle; if saved inside a git repo it is added to .git/info/exclude")
    print(
        "  summary   "
        f"{summary['frame_count']} frames, {summary['trace_event_count']} trace events, "
        f"{summary['duration_s']:.1f}s"
    )
    print(f"  reopen    successor playback {bundle.root}")
    if args.open:
        import webbrowser

        opened = webbrowser.open(bundle.viewer_path.resolve().as_uri())
        print(f"  browser   {'opened' if opened else 'failed to auto-open'}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """Replay a recording into a fresh chat instance.

    Feeds the recorded bytes into the chat at original timing (or
    faster with --speed). Useful for bug repro and demos.

    NOTE: replay does NOT talk to the model — the recorded bytes are
    INPUT only (keystrokes), not the model's responses. The model
    will be re-queried during replay if the user submits messages,
    so the responses may differ. For deterministic playback, the
    consumer would need to also record the model output, which is
    deferred to a future commit.
    """
    from .chat import SuccessorChat
    from .recorder import Player

    path = Path(args.input)
    if not path.exists():
        print(f"successor: no such file: {path}", file=sys.stderr)
        return 1

    player = Player(path, speed=args.speed)
    chat = SuccessorChat()

    # Drive the chat by feeding bytes through on_key. We have to run
    # the chat's main loop ourselves so the renderer ticks during
    # playback. We use a simple thread to feed bytes while the chat's
    # main loop runs.
    import threading
    import time as time_mod

    def _feeder():
        # Wait for the chat to enter its terminal context
        time_mod.sleep(0.5)
        player.play_into(chat.on_key)
        # When playback is done, leave a short tail so the user can
        # see the final state, then call stop() to break the run loop.
        # stop() sets _running = False which the App.run() loop checks
        # at the next select wakeup.
        time_mod.sleep(1.0)
        chat.stop()

    feeder_thread = threading.Thread(target=_feeder, daemon=True)
    feeder_thread.start()
    chat.run()
    return 0


def cmd_playback(args: argparse.Namespace) -> int:
    """Inspect or open a recording bundle reviewer."""
    from .playback import prepare_recording_viewer

    requested = args.input.strip() if isinstance(args.input, str) else ""
    want_library = bool(getattr(args, "library", False)) or requested == "recordings"
    try:
        viewer_path, bundle_root, is_library = prepare_recording_viewer(
            requested or None,
            library=want_library,
        )
    except FileNotFoundError as exc:
        print(f"successor: {exc}", file=sys.stderr)
        return 1
    if is_library:
        print(f"library   {viewer_path}")
    print(f"bundle    {bundle_root}")
    print(f"viewer    {viewer_path}")
    summary_path = bundle_root / "summary.json"
    if not is_library and summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            summary = {}
        if isinstance(summary, dict):
            frame_count = int(summary.get("frame_count", 0) or 0)
            trace_count = int(summary.get("trace_event_count", 0) or 0)
            duration_s = float(summary.get("duration_s", 0.0) or 0.0)
            print(f"summary   {frame_count} frames, {trace_count} trace events, {duration_s:.1f}s")

    if args.open:
        import webbrowser

        opened = webbrowser.open(viewer_path.resolve().as_uri())
        print(f"browser   {'opened' if opened else 'failed to auto-open'}")
    else:
        print("tip       pass --open to launch the viewer in your browser")
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    """Render a chat scenario to ANSI / plain text without a TTY.

    Useful for marketing material, documentation, bug-repro screenshots,
    and verifying the renderer in CI without needing a real terminal.
    """
    from .snapshot import (
        chat_demo_snapshot,
        render_grid_to_ansi,
        render_grid_to_plain,
    )

    grid = chat_demo_snapshot(
        rows=args.rows,
        cols=args.cols,
        theme_name=args.theme,
        display_mode=args.display_mode,
        density_name=args.density,
        scenario=args.scenario,
    )

    if args.format == "ansi":
        output = render_grid_to_ansi(grid)
    else:
        output = render_grid_to_plain(grid) + "\n"

    if args.output == "-":
        sys.stdout.write(output)
    else:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output, encoding="utf-8")
        print(f"wrote {len(output):,} bytes to {path}")

    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    from .intros import successor_intro_frame_paths
    from .loader import builtin_root
    from .render.braille import interpolate_frame, load_frame
    from .render.cells import Grid, Style
    from .render.diff import diff_frames
    from .render.paint import fill_region, paint_lines

    rows = args.rows
    cols = args.cols
    n = args.frames

    print(f"successor bench: {n} frames at {cols}×{rows}")

    intro_dir = builtin_root() / "intros" / "successor"
    fr = successor_intro_frame_paths(intro_dir) if intro_dir.exists() else []
    if len(fr) < 2:
        print("successor: need at least 2 intro frames to bench", file=sys.stderr)
        return 1
    fa = load_frame(fr[0])
    fb = load_frame(fr[-1])

    front = Grid(rows, cols)
    back = Grid(rows, cols)
    bg = Style(bg=0x101010)
    fg = Style(fg=0xFF3030, bg=0x101010)

    total_bytes = 0
    t0 = time.perf_counter()
    for i in range(n):
        back.clear()
        fill_region(back, 0, 0, cols, rows, style=bg)
        t = (i % 60) / 60.0
        morphed = interpolate_frame(fa, fb, t)
        if morphed:
            x = max(0, (cols - len(morphed[0])) // 2)
            y = max(0, (rows - len(morphed)) // 2)
            paint_lines(back, morphed, x, y, style=fg)
        prev = None if i == 0 else front
        delta = diff_frames(prev, back)
        total_bytes += len(delta.encode("utf-8"))
        front, back = back, front
    elapsed = time.perf_counter() - t0

    fps = n / elapsed if elapsed > 0 else float("inf")
    print(f"  elapsed     {elapsed * 1000:.1f} ms")
    print(f"  fps         {fps:.0f}")
    print(f"  per-frame   {(elapsed / n) * 1000:.2f} ms")
    print(f"  bytes       {total_bytes:,} total ({total_bytes // n:,} avg)")
    print(f"  wire        {(total_bytes / elapsed) / 1024:.1f} KiB/s at full speed")
    return 0


# ─── argparse plumbing ───


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="successor",
        description=(
            "successor — terminal chat harness for local llama.cpp and "
            "OpenAI-compatible endpoints (OpenAI, OpenRouter, etc.)"
        ),
        epilog=(
            "First time? Run `successor setup` to create a profile with "
            "the wizard, or `successor chat` to chat against the default "
            "(local llama.cpp on http://localhost:8080)."
        ),
    )
    p.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"successor (successor) {__version__}",
    )

    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    p_chat = sub.add_parser(
        "chat",
        help="streaming chat with the active profile (default command for daily use)",
    )
    p_chat.set_defaults(func=cmd_chat)

    p_setup = sub.add_parser(
        "setup",
        help="10-step profile creation wizard with live preview pane",
    )
    p_setup.set_defaults(func=cmd_setup)

    p_config = sub.add_parser(
        "config",
        help="three-pane profile config menu (browse + edit + live preview)",
    )
    p_config.set_defaults(func=cmd_config)

    p_doctor = sub.add_parser(
        "doctor",
        help="terminal + active profile health check",
    )
    p_doctor.set_defaults(func=cmd_doctor)

    p_login = sub.add_parser(
        "login",
        help="OAuth device flow for Kimi Code (creates kimi-code profile)",
    )
    p_login.set_defaults(func=cmd_login)

    p_skills = sub.add_parser(
        "skills",
        help="list loaded skills (markdown frontmatter format)",
    )
    p_skills.set_defaults(func=cmd_skills)

    p_tools = sub.add_parser(
        "tools",
        help="list Python-import tools in the registry (separate from bash)",
    )
    p_tools.set_defaults(func=cmd_tools)

    p_bench = sub.add_parser("bench", help="renderer benchmark (no TTY required)")
    p_bench.add_argument("--frames", type=int, default=300, help="number of frames")
    p_bench.add_argument("--rows", type=int, default=40, help="grid rows")
    p_bench.add_argument("--cols", type=int, default=120, help="grid cols")
    p_bench.set_defaults(func=cmd_bench)

    p_snapshot = sub.add_parser(
        "snapshot",
        help="render a chat scenario to text/ANSI without a TTY",
    )
    p_snapshot.add_argument("--rows", type=int, default=30, help="grid rows")
    p_snapshot.add_argument("--cols", type=int, default=100, help="grid cols")
    # Theme is intentionally NOT a choices= list — it's resolved against
    # the live registry at run time so user-installed themes work without
    # editing argparse. Validation happens inside chat_demo_snapshot,
    # which falls back to the default theme if the name is unknown.
    p_snapshot.add_argument(
        "--theme", default="steel",
        help="color theme name (any registered theme — see `successor doctor`)",
    )
    p_snapshot.add_argument(
        "--display-mode", default="dark",
        choices=["dark", "light"],
        help="display mode within the chosen theme",
    )
    p_snapshot.add_argument(
        "--density", default="normal",
        choices=["compact", "normal", "spacious"],
        help="layout density",
    )
    p_snapshot.add_argument(
        "--scenario", default="showcase",
        choices=["blank", "showcase", "thinking", "search", "help", "autocomplete", "tool_card"],
        help="which chat state to render",
    )
    p_snapshot.add_argument(
        "--format", default="ansi",
        choices=["ansi", "text"],
        help="output format (ansi for `cat`, text for plain)",
    )
    p_snapshot.add_argument(
        "--output", "-o", default="-",
        help="output file or '-' for stdout",
    )
    p_snapshot.set_defaults(func=cmd_snapshot)

    p_record = sub.add_parser(
        "record",
        help="record a session to a playback bundle (or raw JSONL with --input-only)",
    )
    p_record.add_argument(
        "output",
        nargs="?",
        help="bundle directory (default) or raw JSONL file",
    )
    p_record.add_argument(
        "--input-only",
        action="store_true",
        help="write only the raw input JSONL instead of a playback bundle",
    )
    p_record.add_argument(
        "--frame-interval",
        type=float,
        default=0.15,
        help="minimum seconds between captured viewer frames in bundle mode",
    )
    p_record.add_argument(
        "--open",
        action="store_true",
        help="open playback.html in the default browser after recording ends",
    )
    p_record.set_defaults(func=cmd_record)

    p_replay = sub.add_parser(
        "replay",
        help="play back a recording into a fresh chat",
    )
    p_replay.add_argument("input", help="recording file to play back")
    p_replay.add_argument(
        "--speed", type=float, default=1.0,
        help="playback speed multiplier (1.0=real time, 2.0=2x, 0=instant)",
    )
    p_replay.set_defaults(func=cmd_replay)

    p_playback = sub.add_parser(
        "playback",
        help="inspect or open a recording bundle reviewer",
    )
    p_playback.add_argument(
        "input",
        nargs="?",
        help="bundle directory or playback.html (defaults to the latest bundle)",
    )
    p_playback.add_argument(
        "--open",
        action="store_true",
        help="open the reviewer in the default browser",
    )
    p_playback.add_argument(
        "--library",
        action="store_true",
        help="open the recordings manager for the configured recordings root",
    )
    p_playback.set_defaults(func=cmd_playback)

    p_review = sub.add_parser(
        "review",
        help="alias for playback; open a recording bundle reviewer",
    )
    p_review.add_argument(
        "input",
        nargs="?",
        default="",
        help="bundle directory or playback.html (defaults to the latest bundle)",
    )
    p_review.add_argument(
        "--open",
        action="store_true",
        help="launch the reviewer in the default browser after regeneration",
    )
    p_review.add_argument(
        "--library",
        action="store_true",
        help="open the recordings manager for the configured recordings root",
    )
    p_review.set_defaults(func=cmd_playback)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
