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

## User-Visible Surfaces

The same native file-tool surface now appears in:

- setup wizard defaults
- `/config` tool picker
- chat intro panel
- `successor doctor`
- `successor tools` native-tools section
- tool cards, progress summaries, traces, and playback

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
