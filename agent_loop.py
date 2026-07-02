#!/usr/bin/env python3
"""
agent_loop.py - a manual, human-in-the-loop "agent loop".

A file-shuttling helper for iterating with an external LLM chat agent you
cannot script (a web UI like Grok/ChatGPT). Instead of an API you pass one
Markdown document back and forth, in place:

  1. python agent_loop.py --template topic.md   # scaffold the working doc
     -> edit topic.md: <<<Original State>>> (task) + <<<Code>>> (repo refs)
  2. python agent_loop.py topic.md              # -> topic.out.md (+ repo zips)
     -> paste topic.out.md into the chat, attach the zips, save the reply as
        topic.diff.md
  3. python agent_loop.py topic.md topic.diff.md # fold the reply into topic.md
     -> the agent's answer + a feedback stub are appended to topic.md; any
        proposed code diff is written to topic.patch for you to review and
        apply to the repo BY HAND (agent patches are not trusted/auto-applied)
     -> apply the changes, revise topic.md, then go back to step 2

topic.md is the single evolving narrative (task -> answers -> feedback); code
never lives in it. The repo is the source of truth and is re-zipped fresh every
round, so whatever you apply is carried forward. A one-deep topic.bak.md is
kept for undo.
"""

import argparse
import os
import re
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


def generate_template(output_path: Path):
    template = """<<<Original State>>>
Describe your question / task here in detail.
Be specific about goals, constraints, and what success looks like.
Reference code repos using code1, code2, etc.

<<<Code>>>
- code1: path/to/first/repo
- code2: path/to/second/repo (if needed)
"""
    output_path.write_text(template, encoding="utf-8")
    print(f"✅ Template generated: {output_path}")
    print(f"Edit it, then run: python agent_loop.py {output_path}")


def extract_section(text: str, name: str) -> str:
    m = re.search(rf'<<<{name}>>>(.*?)(?=<<<|$)', text, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""


def strip_outer_fence(text: str) -> str:
    text = text.strip()
    m = re.match(r'^```[A-Za-z0-9_-]*\s*\n(.*?)\n```$', text, re.DOTALL)
    return m.group(1).strip() if m else text


def parse_code_refs(code_section: str):
    """Yield (label, repo_path) pairs from the <<<Code>>> section lines."""
    refs = []
    for line in code_section.splitlines():
        if ':' not in line:
            continue
        label, repo = line.split(':', 1)
        label = label.strip().strip('-* ')
        repo = repo.strip().strip('-* ')
        if repo:
            refs.append((label or repo, repo))
    return refs


def repo_files(path: Path):
    """Files to zip for a repo: everything git does not ignore (the .git folder
    itself is excluded). Returns (list_of_paths, used_git). Falls back to a
    plain walk with EXCLUDE_DIRS when the path is not a git repo (git can't
    honor .gitignore there)."""
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

    files = [path / rel for rel in out.split('\0') if rel]
    return files, True


def zip_repo(repo_path: str, zip_name: str):
    path = Path(repo_path).resolve()
    if not path.exists() or not path.is_dir():
        print(f"⚠️  Repo path {repo_path} not found or not a directory")
        return False

    files, used_git = repo_files(path)
    zip_path = Path(zip_name)
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
        for full in files:
            if not full.is_file():
                continue  # skip staged-deleted / vanished entries
            arc = full.relative_to(path.parent) if full.is_relative_to(path.parent) else full.name
            z.write(full, arc)

    if used_git:
        print(f"✅ Zipped {path} → {zip_path}  (git-tracked + untracked, .gitignore respected)")
    else:
        print(f"⚠️  {path} is not a git repo → zipped with default excludes (.gitignore NOT respected): {zip_path}")
    return True


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
    refs = parse_code_refs(extract_section(a_content, "Code"))

    # Zip referenced repos first so we can list them in the packet.
    attached = []
    for label, repo in refs:
        if not Path(repo).exists():
            print(f"⚠️  Repo path {repo} not found; skipping")
            continue
        base = re.sub(r'[^a-zA-Z0-9_.-]', '_', Path(repo).name) or "repo"
        zip_name = f"{label}_{base}.zip"
        if zip_repo(repo, zip_name):
            attached.append((label, repo, zip_name))

    out = f"""# AGENT LOOP PACKET for {a_path.name}
Generated: {datetime.now().isoformat()}

<<<INSTRUCTIONS>>>
You are participating in an iterative agentic problem-solving loop.
The user will feed your response back into a local tool, so format matters.

Respond **exactly** in this format (do not add extra sections):

<<<Agent Answer>>>
Concise, high-value contribution: new insights, decisions, or changes to merge
into the working document with minimal redundancy. Take into account any prior
<<<Agent Answer>>> and <<<Feedback>>> already present in the document below.

<<<Code diff>>>
Unified git diff (`git diff -U3` style), applicable with `git apply` from the
root of the relevant repo. One diff block per file when possible. If there are
no code changes, write "No code changes this turn."
"""

    if attached:
        out += "\n<<<ATTACHED REPOS>>>\n"
        out += ("The referenced repositories are attached to this message as zip files "
                "(directories like .git/node_modules are excluded). Unzip and inspect "
                "them as needed; diffs should apply from each repo's root.\n")
        for label, repo, zip_name in attached:
            out += f"- {label}: {zip_name}  (from {repo})\n"

    out += "\n--- WORKING DOCUMENT (current state) ---\n\n" + a_content.rstrip() + "\n"

    out_path = a_path.with_suffix('.out.md')
    out_path.write_text(out, encoding="utf-8")
    print(f"✅ Wrote {out_path}")
    if copy_to_clipboard(out):
        print("📋 Copied packet to clipboard")
    if attached:
        print("Attach these zips to your message: " + ", ".join(z for _, _, z in attached))


def integrate(a_path: Path, diff_path: Path):
    a_content = a_path.read_text(encoding="utf-8")
    reply = diff_path.read_text(encoding="utf-8")

    answer = extract_section(reply, "Agent Answer")
    code_diff = strip_outer_fence(extract_section(reply, "Code diff"))
    if not answer and not code_diff:
        print(f"❌ No <<<Agent Answer>>> / <<<Code diff>>> found in {diff_path}; "
              f"leaving files untouched.")
        return

    # Only the narrative thread goes into the working doc. Code lives in the
    # repo (re-zipped fresh every round), never in A.
    merged = a_content.rstrip()
    merged += "\n\n<<<Agent Answer>>>\n" + (answer or "(none)") + "\n"
    merged += "\n<<<Feedback>>>\n(Add your feedback / next instructions here, then run the next round.)\n"

    # Keep one-deep undo, then overwrite the working doc in place.
    a_path.with_suffix('.bak.md').write_text(a_content, encoding="utf-8")
    a_path.write_text(merged, encoding="utf-8")

    # The code diff is a per-round review aid only, never applied by this tool.
    patch_path = a_path.with_suffix('.patch')
    has_patch = bool(code_diff) and not re.match(r'^\s*no code changes', code_diff, re.IGNORECASE)
    if has_patch:
        patch_path.write_text(code_diff.rstrip() + "\n", encoding="utf-8")
    elif patch_path.exists():
        patch_path.unlink()  # clear a stale patch from a previous round

    diff_path.unlink()

    print(f"✅ Merged answer from {diff_path.name} into {a_path.name}  "
          f"(backup: {a_path.with_suffix('.bak.md').name})")
    if has_patch:
        print(f"📄 Proposed code changes written to {patch_path.name} — review and apply "
              f"to the repo yourself (do not trust it blindly).")
    print(f"Then fill in <<<Feedback>>> in {a_path.name} and run: "
          f"python agent_loop.py {a_path.name}  (re-zips the repo fresh)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--template", type=Path, help="Generate the initial working doc")
    parser.add_argument("a_file", type=Path, nargs="?", help="working doc (topic.md)")
    parser.add_argument("diff_file", type=Path, nargs="?", help="agent reply to fold in (topic.diff.md)")
    args = parser.parse_args()

    if args.template:
        generate_template(args.template)
    elif args.a_file and not args.diff_file:
        produce_out(args.a_file)
    elif args.a_file and args.diff_file:
        integrate(args.a_file, args.diff_file)
    else:
        parser.print_help()
