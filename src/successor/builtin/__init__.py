"""Built-in registry data shipped with the package.

Subdirectories follow the kind names used by `loader.Registry`:

  themes/    *.json — Theme files
  profiles/  *.json — Profile files (added in phase 3)
  skills/    *.md   — Skill markdown files (added in phase 5)
  tools/     *.py   — Tool Python modules (added in phase 6)

The loader walks both these built-in directories AND the matching
~/.config/successor/<kind>/ user directories. User files override built-ins
with the same name. Nothing in core has a privileged path — copying a
built-in to ~/.config/successor/<kind>/ and editing it is the supported
way to customize.
"""
