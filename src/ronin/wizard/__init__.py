"""rn setup — the profile creation wizard.

Multi-region App with a live preview pane that shows the user's
in-progress profile choices in real time. The preview is a real
RoninChat instance that the wizard mutates as the user picks options;
the wizard renders it into a sub-grid via on_tick and copies the
cells into its own main content area. When the user arrows between
themes, the preview's existing _set_theme machinery runs the smooth
transition for free — same code path as the live chat.

The wizard is the showcase — every screen exercises a renderer
capability that conventional TUI stacks either can't or have to fight
their stack to attempt. See `setup.py` docstring for the per-screen
breakdown.
"""

from .setup import RoninSetup, run_setup_wizard

__all__ = ["RoninSetup", "run_setup_wizard"]
