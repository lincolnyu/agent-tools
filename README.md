# Agent Loop

A tiny, manual **human-in-the-loop** helper for iterating with an external LLM
chat agent you can't script — a web UI like Grok, ChatGPT, or Claude. Instead of
an API, you keep one Markdown document (the **A document**) and shuttle
information through the chat by hand, accumulating the agent's answers over time
and packaging your files/repos alongside each round.

`agent_loop.py` has no third-party dependencies (standard library only) and uses
`git` when available. `loop.ps1` is a thin PowerShell wrapper that forwards all
arguments to the script.

## The loop

```
  1. python agent_loop.py --new [sometopic.md]     # scaffold the A document
        edit sometopic.md — fill in <<<Problem>>> and list inputs under <<<Files>>>

  2. python agent_loop.py sometopic.md             # build the B doc + files
        paste sometopic.out.md into the chat, attach everything in
        sometopic-files/, save the agent's reply as sometopic.diff.md

  3. review sometopic.diff.md; apply its code changes to your repos by hand

  4. python agent_loop.py sometopic.md sometopic.diff.md   # fold the answer in
        the agent's answer is appended to sometopic.md; the previous version is
        backed up as sometopic.<timestamp>.md — then go back to step 2
```

```
   sometopic.md ──(2)──> sometopic.out.md ──you──> chat ──you──> sometopic.diff.md
       ^                  + sometopic-files/                            │
       └──────────────────────────(4)───────────────────────────────────┘
                      (+ sometopic.<timestamp>.md backup)
```

`sometopic.md` is the single evolving narrative — the problem plus every agent
answer, in order. **Code never lives in it**: referenced files/repos are
re-packaged fresh every round, and the agent's proposed code changes are yours
to apply by hand. `sometopic.out.md` and `sometopic.diff.md` are transient and
overwritten each round.

## Commands

| Invocation | Action |
|---|---|
| `python agent_loop.py --new [FILE]` | Create a new A document. With no name, uses `<cwd-name>.md`. Won't overwrite an existing file. |
| `python agent_loop.py A.md` | Build the B document `A.out.md` and the `A-files/` attachment folder. |
| `python agent_loop.py A.md REPLY.md` | Append the agent's answer from `REPLY.md` into `A.md` (with a timestamped backup). |
| `python agent_loop.py` | Print help. |

`loop.ps1` mirrors these: `.\loop.ps1 --new sometopic.md`, `.\loop.ps1 sometopic.md`,
`.\loop.ps1 sometopic.md sometopic.diff.md`.

## The A document (`sometopic.md`)

Sections are delimited by `<<<Marker>>>` lines (the same convention as the
agent's reply). `--new` scaffolds:

```
<<<Problem>>>

Describe the problem / task in detail here.
Be specific about goals, constraints, and what success looks like.
Refer to inputs by their reference name (defined under <<<Files>>>).

<<<Files>>>

- ref1: path/to/a/file
- ref2: path/to/a/folder
```

- **`<<<Problem>>>`** — your task/question, goals, constraints. Refer to inputs by
  their reference name so the agent can connect prose to attachments.
- **`<<<Files>>>`** — one `refname: path` per line, each pointing at a local
  **file** or **folder**. Relative paths are resolved against the **A document's
  own directory** (not the cwd). The list is flat; reference names (and the
  resulting attachment names) must be distinct. An entry whose path doesn't exist
  (a broken link, or a deliberate `refname: n/a`) is simply ignored — not attached
  and not listed in the B document.

As rounds progress, the tool appends, for each round, an
`<<<Agent Answer <timestamp>>>>` section followed by an empty
`<<<User Response <timestamp>>>>` stub (matching timestamps). Fill the stub with
your feedback on that answer, or leave it empty. Both accumulate into the growing
document — your running dialogue with the agent — and you can edit any part of it
between rounds.

## Step 2 — building the B document (`sometopic.out.md`)

`agent_loop.py sometopic.md` produces `sometopic.out.md`, a single self-contained
prompt you paste into the chat, laid out in reading order:

1. **Instructions** — how to respond, pointing at the format at the very end.
2. **Problem** — from `<<<Problem>>>`.
3. **History** — every accumulated `<<<Agent Answer T>>>` interleaved with your
   `<<<User Response T>>>` feedback (matching timestamps); empty responses are
   dropped. Empty on round 1.
4. **File References** — each `refname: path → attached-name`, plus the list of
   attachments (ignored/missing refs don't appear).
5. **Required Response Format** — placed last so it's the final, unambiguous
   instruction: reply with exactly `<<<Agent Answer>>>` and `<<<Code diff>>>`.

Alongside it, the `sometopic-files/` folder is **rebuilt from scratch** to mirror
the current `<<<Files>>>` list:

- A **file** entry is symlinked in by its basename (copied where symlinks aren't
  permitted, e.g. Windows without Developer Mode / admin).
- A **folder** entry is zipped in as `<name>.zip`. For a **git repo** the zip
  holds exactly what git does *not* ignore — tracked **and** untracked files,
  honoring every `.gitignore` layer (`.git` itself is excluded). A non-git folder
  falls back to a plain walk skipping common noise (`node_modules`, `__pycache__`,
  `venv`, `dist`, `build`, …) with a warning that `.gitignore` isn't respected.

If two entries would produce the same attachment name, the duplicate is skipped
with a warning. On Windows the packet is also copied to the clipboard.

## Step 3 — the agent's reply (`sometopic.diff.md`)

Save the agent's response to a file. It should contain exactly two sections:

- **`<<<Agent Answer>>>`** — a concise, self-contained contribution for this
  round. This is what gets recorded in the A document.
- **`<<<Code diff>>>`** — a `git apply`-able unified diff (or "No code changes
  this turn."). This is **yours to review and apply by hand** — the tool never
  applies it and never stores it in the A document.

You may edit the reply before folding it in, but usually won't.

## Step 4 — folding the answer in

`agent_loop.py sometopic.md sometopic.diff.md`:

1. Extracts `<<<Agent Answer>>>` from the reply. If it's missing, the tool aborts
   and touches nothing.
2. Backs up the current `sometopic.md` to `sometopic.<timestamp>.md` (timestamp
   `YYYYMMDDHHMM`, e.g. `202607041940` = 2026-07-04 19:40; extends to seconds only
   if a same-minute backup already exists, so nothing is clobbered).
3. Appends the answer as a new `<<<Agent Answer <timestamp>>>>` section, followed
   by an empty `<<<User Response <timestamp>>>>` stub for your feedback.

The code diff is not touched — apply it to your repos yourself, then loop.

## Files at a glance

| File | Role |
|---|---|
| `sometopic.md` | The evolving A document (you own and edit it). |
| `sometopic.out.md` | Generated B document to paste into the chat (transient). |
| `sometopic-files/` | Attachment folder, rebuilt each round: file symlinks + folder zips. |
| `sometopic.diff.md` | The agent's raw reply you save (transient). |
| `sometopic.<timestamp>.md` | Timestamped backup of the A document before each merge. |

## Notes & limitations

- The tool **never runs `git apply`** and never stores code in the A document —
  you stay in control of what lands in your repos.
- Folding in a reply is intentionally mechanical: it only extracts the answer and
  appends it, which keeps the step robust.
- `sometopic-files/` is wiped and rebuilt every round, so it always reflects the
  current `<<<Files>>>` list (removed references don't linger).
- Reference names must map to distinct attachment basenames; colliding entries
  are skipped with a warning.
