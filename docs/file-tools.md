# Native File Tools

Successor's default authoring path is now native file IO, not shell
redirection.

## Tool Surface

- `read_file`
  Reads a local UTF-8 text file and returns deterministic line-numbered
  output. Supports optional `offset` and `limit` for targeted range
  reads.
- `write_file`
  Creates a new file or fully replaces an existing file with the
  provided content.
- `edit_file`
  Replaces one exact string with another in an existing file. Supports
  `replace_all=true` when the match is intentionally non-unique.
- `bash`
  Reserved for shell and system work: tests, builds, package managers,
  git, servers, process inspection, and one-off shell commands.

## Contract

- Prefer `read_file` over `cat`, `head`, `tail`, or `sed` for reading files.
- Prefer `edit_file` over `sed`, `awk`, `perl`, or inline Python for targeted edits.
- Prefer `write_file` over heredocs, `echo >`, or shell redirection for file creation and full rewrites.
- Existing files must be fully read before `write_file` or `edit_file`.
- `write_file` and `edit_file` fail cleanly if the file changed since the last full read.
- `edit_file` fails when `old_string` matches multiple locations unless `replace_all=true`.
- `edit_file` preserves the file's existing line-ending style.
- New files can be created directly with `write_file`.
- Re-reading the same unchanged file with the same full-range request returns a short unchanged stub instead of re-sending the entire file.
- Repeating the same exact `read_file` request too many times in a row without any intervening non-read tool call first warns, then hard-fails to break read loops.

## Guard Recovery

When a native write is refused, Successor now tries to recover inside
the same authoring path instead of silently letting the model drift back
to bash mutation.

- failed `write_file` / `edit_file` calls can emit one deterministic
  recovery reminder in the next turn's system prompt
- the reminder is only for common guard failures, not arbitrary errors
- current recovery cases:
  - file has not been fully read in this chat yet
  - file was only read partially
  - file changed since the last full read
  - `edit_file` target is ambiguous
  - `edit_file` target text is not present anymore

Recovery posture:

- re-read the exact file with `read_file`
- use a full-file read when the refusal says the earlier read was partial
- retry `write_file` / `edit_file` with the native tool
- do not bypass the guard with heredocs, `sed`, `awk`, inline Python, or shell redirection

## Fast Post-Write Checks

Native file mutation does not stop at "bytes changed on disk".
`write_file` and `edit_file` now try one fast deterministic
post-write sanity check when the file type and local workspace make that
possible.

- Python:
  - prefer `ruff check <path>` when the nearest workspace advertises
    Ruff
  - otherwise fall back to `python -m py_compile <path>`
- JSON:
  - `python -m json.tool <path>`
- JavaScript:
  - `node --check <path>` when Node is available

These fast checks are advisory, not hidden retries. The file mutation
still completes, but the tool output, progress summary, trace, and
playback all surface whether the fast check passed or failed so the
model can react in the next turn.

## User-Visible Surfaces

The same native file-tool surface now appears in:

- setup wizard defaults
- `/config` tool picker
- chat intro panel
- `successor doctor`
- `successor tools` native-tools section
- tool cards, progress summaries, traces, and playback

## Verification Pairing

File tools solve authoring. Verification is a separate control problem.

- For broad or long-horizon requests, the runtime now nudges the model
  to adopt the session task ledger before the first substantive
  mutation, process-management step, or browser loop.
- Successor now keeps a compact internal verification contract (`verify`)
  alongside the task ledger during tool-enabled runs.
- Use it to track concrete claims plus the exact evidence that should
  prove them: browser interaction, screenshots plus vision, console
  output, runtime logs, or a small verifier/player script.
- For stateful or realtime work, the runtime now nudges the model to
  make that proof path explicit early: name the deterministic driver or
  autoplay harness when hand-play is weak, and pair it with an
  observable debug surface such as a HUD value, runtime log, or state
  accessor.
- Recording bundles persist the latest contract as `assertions.json`
  when the run produced explicit proof state.
- When the model needs a fresh checker, use `subagent` with
  `role="verification"`. That worker is read-only by construction:
  no `write_file`, no `edit_file`, no nested subagents, and
  non-mutating bash only.
- Verification workers should start with the repo contract when
  available, then try to prove the changed behavior directly with
  runtime evidence rather than source inspection alone.

## Notes

- Paths are normalized to absolute paths at execution time.
- Relative paths resolve from the same working directory the chat uses
  for bash tool execution.
- The Python-import plugin registry under `src/successor/tools/` is a
  separate surface. Those plugin tools are listed by `successor tools`
  under the plugin section and are not part of the native chat loop
  unless explicitly integrated.
- Browser verification is intentionally separate from file tools. If
  you need to inspect live UI state, use `browser` plus `vision` rather
  than stretching `read_file` into a visual-debug path.
- When serving a local preview app, prefer another free high port if
  your first choice is occupied. Do not kill an unknown process just to
  reclaim a port, and never reclaim the active local provider endpoint
  unless the user explicitly told you to replace it.
