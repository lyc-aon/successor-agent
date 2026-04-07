"""Wizard package — multi-region Apps that share the chat renderer.

Two Apps live here:

  SuccessorSetup    — `successor setup` profile creation wizard. Linear, eight
                  steps, ends in a save action that drops you into the
                  chat with the new profile active. See `setup.py`.

  SuccessorConfig   — `successor config` profile config menu. Three panes
                  (profiles list / settings tree / live preview),
                  non-linear, dirty-tracking save/revert. Stays open
                  until you hit Esc. See `config.py`.

Both reuse the chat's renderer + theme transition machinery and don't
introduce any new primitives. The wizard is for first-time setup;
the config menu is for ongoing tweaks.
"""

from .config import SuccessorConfig, run_config_menu
from .setup import SuccessorSetup, run_setup_wizard

__all__ = [
    "SuccessorConfig",
    "SuccessorSetup",
    "run_config_menu",
    "run_setup_wizard",
]
