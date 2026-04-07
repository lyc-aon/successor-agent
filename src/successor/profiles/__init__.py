"""Profiles — named bundles of (theme, mode, density, prompt, provider).

A profile is the user's persona unit. Switching profiles atomically
swaps everything that defines "how this chat feels": the visual theme,
the display mode, the layout density, the system prompt sent to the
model, the provider and model configuration, the active skill list,
the active tool list (deferred — slot exists but not yet wired into
the agent loop), and an optional intro animation that plays before
the chat opens.

Profiles are JSON files loaded via the same Registry pattern themes
use. Built-ins ship in `src/successor/builtin/profiles/`; user files live
in `~/.config/successor/profiles/`. User files override built-ins by name.

Public surface:
    Profile             dataclass with all profile fields
    parse_profile_file  Path → Profile (used by Registry)
    PROFILE_REGISTRY    the Registry[Profile] singleton
    get_profile(name)   convenience lookup
    all_profiles()      list of every loaded profile
    next_profile(p)     cycle to the next profile in registry order
    get_active_profile  resolve the active profile from chat config
    set_active_profile  persist a new active profile name
"""

from .profile import (
    PROFILE_REGISTRY,
    Profile,
    all_profiles,
    get_active_profile,
    get_profile,
    next_profile,
    parse_profile_file,
    set_active_profile,
)

__all__ = [
    "PROFILE_REGISTRY",
    "Profile",
    "all_profiles",
    "get_active_profile",
    "get_profile",
    "next_profile",
    "parse_profile_file",
    "set_active_profile",
]
