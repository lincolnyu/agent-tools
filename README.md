# Agent Loop

A tiny, manual **human-in-the-loop** helper for iterating with an external LLM
chat agent you can't script — a web UI like Grok, ChatGPT, or Claude. Instead of
an API, you shuttle a single Markdown document back and forth by hand, keeping a
growing record of the conversation and packaging your code repos alongside it.

`agent_loop.py` has no third-party dependencies (standard library only) and uses
`git` when available. `loop.ps1` is a thin PowerShell wrapper that forwards all
arguments to the script.

## The loop

The whole workflow revolves around one evolving document, `topic.md`:

```
  1. python agent_loop.py --template topic.md      # scaffold the working doc
        edit topic.md — describe the task and list your repos

  2. python agent_loop.py topic.md                 # build the packet + zips
        paste topic.out.md into the chat, attach the zips,
        save the agent's reply as topic.diff.md

  3. python agent_loop.py topic.md topic.diff.md   # fold the reply back in
        review topic.patch, apply changes to the repo yourself,
        revise topic.md, then go back to step 2
```

```
   topic.md ──(2)──> topic.out.md ──you──> chat ──you──> topic.diff.md
      ^               + topic-files/                          │
      └──────────────────────(3)──────────────────────────────┘
                     (+ topic.patch, topic.bak.md)
```

`topic.md` is the single source of narrative (task → answers → feedback). **Code
never lives in it** — your repos are the source of truth and are re-zipped fresh
every round, so any change you apply is carried forward automatically.

## Commands

| Invocation | Action |
|---|---|
| `python agent_loop.py --template FILE` | Write a starter working doc to `FILE`. |
| `python agent_loop.py A.md` | Build the packet `A.out.md` and zip the referenced repos. |
| `python agent_loop.py A.md REPLY.md` | Fold the agent's reply in `REPLY.md` back into `A.md`. |
| `python agent_loop.py` | Print help. |

`loop.ps1` mirrors these: `.\loop.ps1 --template topic.md`, `.\loop.ps1 topic.md`,
`.\loop.ps1 topic.md topic.diff.md`.

## The working document (`topic.md`)

Plain Markdown with `<<<Section>>>` markers. The template gives you:

```
<<<Original State>>>
Describe your question / task here in detail.
Be specific about goals, constraints, and what success looks like.
Reference code repos using code1, code2, etc.

<<<Code>>>
- code1: path/to/a/repo
- somefile: path/to/a/file
```

- **`<<<Original State>>>`** — your task/question, goals and constraints.
- **`<<<Code>>>`** — one `label: path` per line, each pointing at a local repo
  **or** file to attach:
  - a **directory** is packaged as a zip named `<label>_<dirname>.zip`;
  - a **file** is symlinked flat into the outbox by its basename (falling back
    to a copy where symlinks aren't permitted). If a file of that name is
    already in the folder, it's left as-is.

  The list is flat — no subfolders are created in the outbox.

As rounds progress, the tool appends `<<<Agent Answer>>>` and a blank
`<<<Feedback>>>` stub for each round, growing the document into a full thread.

## Step 2 — building the packet (`topic.out.md`)

Running `agent_loop.py topic.md` produces `topic.out.md`, a single self-contained
prompt you paste into the chat. It contains:

- **`<<<INSTRUCTIONS>>>`** — tells the agent to reply in exactly two sections,
  `<<<Agent Answer>>>` and `<<<Code diff>>>` (a `git diff -U3`-style unified diff,
  or "No code changes this turn.").
- **`<<<ATTACHED FILES>>>`** — lists everything in the `topic-files/` folder
  (repo zips + anything you added), annotating which entries are repo archives.
- **The current working document**, embedded verbatim so the agent sees all prior
  answers and your feedback.

The attachments live in a folder named after the doc — `topic.md` → **`topic-files/`**
— created next to it. This folder is a **persistent outbox**: the tool refreshes
the repo zips there every round but never deletes anything, so files you drop in
manually (and prior ones) survive across rounds. You're responsible for referencing
any manual files in the doc where it matters; the tool just lists them so nothing
is silently attached. `topic.out.md` itself stays at top level — it's the text you
paste, the folder is what you attach.

Directory entries in `<<<Code>>>` are zipped into `topic-files/` (file entries
are symlinked, as described above):

- If the path is a **git repo**, the zip contains exactly what git does *not*
  ignore — tracked **and** untracked files, honoring every `.gitignore` layer,
  `.git/info/exclude`, and global excludes. The `.git` folder itself is excluded.
- If the path is **not a git repo**, it falls back to a plain directory walk that
  skips common noise (`.git`, `node_modules`, `__pycache__`, `venv`, `.venv`,
  `env`, `dist`, `build`, `.DS_Store`) and prints a warning that `.gitignore` is
  not being respected.

On Windows the packet is also copied to the clipboard for convenience. The tool
prints the exact files to attach from `topic-files/`.

## Step 3 — folding the reply back in

Save the agent's reply as a file (e.g. `topic.diff.md`) and run
`agent_loop.py topic.md topic.diff.md`. The tool:

1. Extracts `<<<Agent Answer>>>` and `<<<Code diff>>>` from the reply (fenced code
   blocks around the diff are stripped). If neither is found, it aborts and
   **touches nothing**.
2. Backs up the current `topic.md` to `topic.bak.md` (one-deep undo).
3. Appends the answer and a fresh `<<<Feedback>>>` stub to `topic.md`.
4. Writes any real code diff to **`topic.patch`** — a **review aid only**. The
   tool never applies it; you inspect it and apply changes to the repo yourself
   (agent-produced patches are not trusted). A round with no changes clears any
   stale `topic.patch`.
5. Deletes the consumed `topic.diff.md`.

Then edit `topic.md`: add your feedback in the `<<<Feedback>>>` stub, and loop.

## Files at a glance

| File | Role |
|---|---|
| `topic.md` | The evolving working document (you own and edit it). |
| `topic.out.md` | Generated packet to paste into the chat. |
| `topic-files/` | Persistent outbox to attach: repo zips (refreshed each round) + any files you add. |
| `topic.diff.md` | The agent's raw reply you save (consumed in step 3). |
| `topic.patch` | Proposed code changes to review and apply by hand. |
| `topic.bak.md` | Backup of `topic.md` from before the last merge. |

## Notes & limitations

- The tool **never runs `git apply`** — patch correctness is never something the
  loop depends on. You stay in control of what lands in your repos.
- Merging is intentionally "dumb": it only ever appends prose and stages a file,
  which keeps the mechanical step robust.
- Feedback stubs repeat each round (no numbering); the newest is always at the
  bottom of `topic.md`.
- Only a single-deep backup is kept; each merge overwrites `topic.bak.md`.
