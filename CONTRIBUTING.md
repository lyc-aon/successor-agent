# Contributing to Successor

Thanks for considering a contribution. The repo is small enough that
you can read the whole thing in an afternoon, and the architecture
is documented well enough that you can ship a meaningful PR without
me holding your hand. Here is what you need to know.

## Dev setup

```bash
git clone https://github.com/lyc-aon/successor-agent
cd successor-agent
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pip install pytest
```

That's it. There are no third-party runtime dependencies. The only
dev dependency is `pytest`.

## Running the tests

```bash
.venv/bin/python -m pytest -q          # all tests, dot view
.venv/bin/python -m pytest -xvs        # follow individual tests, fail fast
.venv/bin/python -m pytest tests/test_chat_compaction_e2e.py  # one file
```

The full suite is hermetic. Each test gets its own
`SUCCESSOR_CONFIG_DIR` via the `temp_config_dir` fixture in
`tests/conftest.py`. Bash dispatch tests use real shell builtins,
no mocks. Visual snapshot tests run headless against the renderer
without spawning a TTY.

There are 1025+ tests at the time of writing. Please add tests for
any new feature you ship. Visual features should have a snapshot
test that asserts grid contents directly via the `wizard_demo_snapshot`
or `chat_demo_snapshot` helpers in `src/successor/snapshot.py`.

## Read this first

Before touching the renderer, read
[`docs/rendering-superpowers.md`](docs/rendering-superpowers.md). It
covers the One Rule (only `src/successor/render/diff.py` writes to
stdout) and explains why every feature so far has fit through that
hole as a pure paint function. If you find yourself wanting to
import Rich, prompt_toolkit, Textual, or call `print()` from
anywhere outside `diff.py`, the answer is no.

Other docs worth reading depending on what you're touching:

- [`docs/concepts.md`](docs/concepts.md) for the architecture's
  feature list and the categories of capability the renderer enables
- [`docs/compaction.md`](docs/compaction.md) for the autocompactor
  schema, threshold math, and the post-compact assertion
- [`docs/llamacpp-protocol.md`](docs/llamacpp-protocol.md) for the
  llama.cpp HTTP surface and Qwen tool-call format
- [`CLAUDE.md`](CLAUDE.md) for the per-subsystem orientation note
  that auto-loads in Claude Code sessions

## Style

The codebase favors:

- Frozen dataclasses for value types, mutable dataclasses for state
- Type hints everywhere, including private helpers
- Docstrings that explain *why*, not what (the type signature is the what)
- Sentence-case comments, no `# TODO` without a name attached
- One module per concept, no kitchen-sink files

Prose in docs and READMEs follows a deliberate anti-slop discipline:

- No em dashes in prose (use commas, periods, or parentheses)
- No tricolons (rule-of-three lists for rhetorical effect)
- No "It's not X, it's Y" reframes
- No magic adverbs (`quietly`, `deeply`, `fundamentally`)
- Specific claims with concrete numbers beat abstract praise

If your PR adds documentation, give it a slop pass before opening the
PR. Run `grep -c '—' your-new-file.md` and aim for zero in prose
sections.

## Profile, theme, skill, and tool contributions

You can ship a new theme by dropping a JSON file at
`src/successor/builtin/themes/<name>.json`. Copy `steel.json` or
`forge.json` as a starting point. The format is documented inline in
the existing files.

New profiles, skills, and tools follow the same loader pattern.
See `src/successor/loader.py` for the `Registry[T]` implementation
they all reuse.

## PR process

1. Open an issue first if you're proposing a meaningful change. A
   one-line "what are you trying to do" thread saves both of us
   from sunk-cost arguments later.
2. Branch from `master`. Keep PRs focused. One feature per PR.
3. Make sure `pytest -q` passes locally. CI will run it again.
4. Reference the issue in your PR description if there is one.
5. Ship.

I read every PR and aim to respond within a day. If you hear nothing
in a week, ping me on the issue or DM
[@lyc_aon](https://twitter.com/lyc_aon) on twitter. Sometimes
notifications get buried.

## Code of conduct

Be the kind of person you would want to collaborate with. Be precise
about technical disagreements, charitable about misunderstandings,
and direct about credit. The repo is small enough that there is
room for everyone who shows up to help.

## License

Apache 2.0. Contributions are accepted under the same license as the
project. By submitting a PR you agree that your changes can ship
under Apache 2.0 with the existing NOTICE attribution.
