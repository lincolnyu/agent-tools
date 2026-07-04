#!/usr/bin/env python3
"""
agent_loop.py - a manual, human-in-the-loop "agent loop".

A file-shuttling helper for iterating with an external LLM chat agent you
cannot script (a web UI like Grok/ChatGPT/Claude). You keep one Markdown
document, the "A" document, and shuttle information through the chat by hand:

  1. python agent_loop.py --new [sometopic.md]
     Scaffold a fresh A document (defaults to <cwd-name>.md). Fill in its
     `<<<Problem>>>` section and list inputs under `<<<Files>>>` as
     `- refname: path/to/file/or/folder`.

  2. python agent_loop.py sometopic.md
     Build the "B" document sometopic.out.md (instructions + problem + prior
     answers + file references + the required response format) and a
     sometopic-files/ folder: referenced files are symlinked in (copied if
     symlinks aren't permitted), referenced folders are zipped in. Paste the
     .out.md into the chat and attach everything in sometopic-files/.

  3. Save the agent's reply (must use <<<Agent Answer>>> / <<<Code diff>>>) as
     sometopic.diff.md, review it, apply its code changes to your repos by hand.

  4. python agent_loop.py sometopic.md sometopic.diff.md
     Append the agent's answer to sometopic.md as `<<<Agent Answer <timestamp>>>>`,
     backing up the previous version as sometopic.<timestamp>.md, then loop.

sometopic.md is the single evolving narrative (problem + accumulated answers).
Code never lives in it. sometopic.out.md and sometopic.diff.md are transient
and overwritten each round.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from datetime import datetime

# Windows consoles default to cp1252, which can't encode the ✅/📋 status glyphs.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

EXCLUDE_DIRS = {'.git', 'node_modules', '__pycache__', 'venv', 'env', 'dist', 'build', '.venv'}
EXCLUDE_FILES = {'.DS_Store'}

INSTRUCTIONS = (
    "You are an agent in an iterative, human-in-the-loop problem-solving loop. "
    "A human will paste your reply back into a local tool, so **format matters**. "
    "Read the problem, the prior answers, and the attached files below, then reply "
    "in EXACTLY the format given under \"Required Response Format\" at the END of "
    "this document — those two sections and nothing else. Be concise and additive: "
    "the prior answers are already recorded, so contribute what is new rather than "
    "restating them."
)

RESPONSE_FORMAT = """Reply with these two sections only, using these exact markers:

<<<Agent Answer>>>
A clear, concise, self-contained contribution — the key insight, decision, or
change from this round. It is appended verbatim to the working document, so
write it as a direct addition (no need to restate prior answers).

<<<Code diff>>>
A unified git diff (`git diff -U3` style) for any code changes, one block per
file, applyable with `git apply` from the repo root. If there are none, write:
No code changes this turn."""


def timestamp() -> str:
    """Compact YYYYMMDDHHMM, e.g. 202607041940 -> 2026-07-04 19:40."""
    return datetime.now().strftime("%Y%m%d%H%M")


def generate_new_template(output_path: Path):
    if output_path.exists():
        print(f"⚠️  {output_path} already exists; not overwriting.")
        return
    template = """<<<Problem>>>

Describe the problem / task in detail here.
Be specific about goals, constraints, and what success looks like.
Refer to inputs by their reference name (defined under <<<Files>>>).

<<<Files>>>

- ref1: path/to/a/file
- ref2: path/to/a/folder
"""
    output_path.write_text(template, encoding="utf-8")
    print(f"✅ New A document: {output_path}")
    print(f"   Fill in <<<Problem>>> and <<<Files>>>, then run: python agent_loop.py {output_path}")


def extract_section(text: str, name: str) -> str:
    """Content of a `<<<name>>>` section, up to the next `<<<...>>>` marker or EOF.
    Tolerates optional spaces inside the markers (<<< name >>>)."""
    m = re.search(rf'<<<\s*{re.escape(name)}\s*>>>\s*(.*?)(?=\n<<<|\Z)', text, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""


def extract_agent_answers(a_content: str) -> str:
    """All accumulated `<<<Agent Answer <ts>>>>` blocks, from the first one to EOF."""
    m = re.search(r'(?im)^<<<\s*Agent Answer\b', a_content)
    return a_content[m.start():].strip() if m else ""


def parse_file_refs(files_section: str):
    """Parse `- refname: path` lines into (refname, path) pairs."""
    refs = []
    for line in files_section.splitlines():
        line = re.sub(r'^[-*\s]+', '', line.strip())
        if not line or line.startswith('#') or ':' not in line:
            continue
        label, path_str = line.split(':', 1)
        label, path_str = label.strip(), path_str.strip()
        if label and path_str:
            refs.append((label, path_str))
    return refs


def repo_files(path: Path):
    """Files to zip for a folder: everything git does not ignore (.git excluded).
    Returns (paths, used_git); falls back to a plain walk with EXCLUDE_DIRS when
    the path is not a git repo (git can't honor .gitignore there)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        files = []
        for root, dirs, names in os.walk(path):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            files += [Path(root) / n for n in names if n not in EXCLUDE_FILES]
        return files, False
    return [path / rel for rel in out.split('\0') if rel], True


def zip_folder(src: Path, zip_path: Path) -> bool:
    files, used_git = repo_files(src)
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
        for full in files:
            if not full.is_file():
                continue  # skip staged-deleted / vanished entries
            arc = full.relative_to(src.parent) if full.is_relative_to(src.parent) else full.name
            z.write(full, arc)
    note = "git-tracked + untracked, .gitignore respected" if used_git \
        else "not a git repo, basic excludes only (.gitignore NOT respected)"
    print(f"✅ Zipped {src} → {zip_path.name}  ({note})")
    return True


def link_file(src: Path, dest: Path) -> bool:
    """Symlink src into the outbox as dest; copy where symlinks aren't permitted
    (e.g. Windows without Developer Mode / admin)."""
    try:
        dest.symlink_to(src.resolve())
        print(f"🔗 Linked {src} → {dest.name}")
        return True
    except OSError as e:
        try:
            shutil.copy2(src, dest)
            print(f"✅ Copied {src} → {dest.name}  (symlink unavailable: {e.strerror or e})")
            return True
        except OSError as e2:
            print(f"⚠️  Could not link or copy {src}: {e2}")
            return False


def copy_to_clipboard(text: str) -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
            input=text, text=True, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def produce_out(a_path: Path):
    a_content = a_path.read_text(encoding="utf-8")
    problem = extract_section(a_content, "Problem")
    files_section = extract_section(a_content, "Files")
    answers = extract_agent_answers(a_content)
    refs = parse_file_refs(files_section)

    # Rebuild the outbox from scratch so it mirrors the current # Files list.
    files_dir = a_path.with_name(a_path.stem + "-files")
    if files_dir.exists():
        shutil.rmtree(files_dir)
    files_dir.mkdir()

    used = set()          # attached basenames, to detect collisions
    ref_rows = []         # (refname, path, attached-name) — successful attachments only
    for label, ref_str in refs:
        src = Path(ref_str).expanduser()
        if not src.exists():
            print(f"↪️  {label}: {ref_str} — not found, ignoring")
            continue
        safe = re.sub(r'[^a-zA-Z0-9_.-]', '_', src.name) or "item"
        name = f"{safe}.zip" if src.is_dir() else safe
        if name in used:
            print(f"⚠️  Duplicate attachment name '{name}' for {ref_str}; skipping "
                  f"(reference names must map to distinct file names)")
            continue
        ok = zip_folder(src, files_dir / name) if src.is_dir() else link_file(src, files_dir / name)
        if ok:
            used.add(name)
            ref_rows.append((label, ref_str, name))
        else:
            print(f"⚠️  {label}: could not attach {ref_str}")

    if ref_rows:
        refs_block = "\n".join(f"- {label}: {path}  →  {att}" for label, path, att in ref_rows)
    else:
        refs_block = "(no file references)"
    attached = sorted(used)
    attach_note = (f"Attached alongside this document (in `{files_dir.name}/`): "
                   + (", ".join(attached) if attached else "none"))

    out = "\n".join([
        f"# AGENT LOOP PACKET — {a_path.name}",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "## Instructions",
        INSTRUCTIONS,
        "",
        "## Problem",
        problem or "(no problem section)",
        "",
        "## Prior Agent Answers",
        answers or "(none yet)",
        "",
        "## File References",
        refs_block,
        "",
        attach_note,
        "",
        "## Required Response Format",
        RESPONSE_FORMAT,
    ]).rstrip() + "\n"

    out_path = a_path.with_suffix('.out.md')
    out_path.write_text(out, encoding="utf-8")
    print(f"✅ Wrote {out_path}")
    if copy_to_clipboard(out):
        print("📋 Copied packet to clipboard")
    if attached:
        print(f"📎 Attach the files in {files_dir.name}/ : " + ", ".join(attached))


def integrate(a_path: Path, diff_path: Path):
    if not diff_path.exists():
        print(f"❌ {diff_path} not found")
        return
    a_content = a_path.read_text(encoding="utf-8")
    answer = extract_section(diff_path.read_text(encoding="utf-8"), "Agent Answer")
    if not answer:
        print(f"❌ No <<<Agent Answer>>> found in {diff_path}; leaving files untouched.")
        return

    ts = timestamp()
    backup_path = a_path.with_suffix(f'.{ts}.md')
    if backup_path.exists():  # rare same-minute rerun: extend precision, don't clobber
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        backup_path = a_path.with_suffix(f'.{ts}.md')
    backup_path.write_text(a_content, encoding="utf-8")

    merged = a_content.rstrip() + f"\n\n<<<Agent Answer {ts}>>>\n\n{answer}\n"
    a_path.write_text(merged, encoding="utf-8")

    print(f"✅ Appended '<<<Agent Answer {ts}>>>' to {a_path.name}  (backup: {backup_path.name})")
    print(f"   Apply the reply's <<<Code diff>>> to your code by hand if needed, then run: "
          f"python agent_loop.py {a_path.name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--new", nargs="?", const="", default=None,
                        help="Create a new A document (defaults to <cwd-name>.md)")
    parser.add_argument("a_file", type=Path, nargs="?", help="the A document (sometopic.md)")
    parser.add_argument("diff_file", type=Path, nargs="?", help="agent reply (sometopic.diff.md)")
    args = parser.parse_args()

    if args.new is not None:
        target = Path(args.new) if args.new else Path(f"{Path.cwd().name}.md")
        generate_new_template(target)
    elif args.a_file and not args.diff_file:
        produce_out(args.a_file)
    elif args.a_file and args.diff_file:
        integrate(args.a_file, args.diff_file)
    else:
        parser.print_help()
