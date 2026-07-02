#!/usr/bin/env python3
"""
agent_loop.py - a manual, human-in-the-loop "agent loop".

A file-shuttling helper for iterating with an external LLM chat agent you
cannot script (a web UI like Grok/ChatGPT). Instead of an API you pass
Markdown files back and forth. The naming convention is A -> B -> C -> A(n+1):

  A(n)  Your human-readable prompt doc: <<<Original State>>> (the task) and
        <<<Code>>> (references to repos, e.g. `- code1: path/to/repo`).
  B(n)  A compiled packet you paste into the external agent. It embeds the
        instructions, the required response format and the current A, and
        zips each referenced repo so you can attach them.
  C(n)  The agent's raw reply, saved to a file. It must contain
        <<<Agent Answer>>> and <<<Code diff>>> (git-apply-able unified diff).
  A(n+1)  Produced by folding C's answer + diff back into A, numbered per
        round, so you can review, add <<<Feedback n>>>, and loop again.

Usage:
  python agent_loop.py --template some.A.md   # scaffold a fresh A
  python agent_loop.py some.A.md              # produce B (+ repo zips)
  python agent_loop.py some.A.md some.C.md    # integrate C -> next A
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


def parse_a_file(a_path: Path):
    content = a_path.read_text(encoding="utf-8")
    sections = {}
    current = None
    for line in content.splitlines():
        m = re.match(r'<<<(.+?)>>>', line.strip())
        if m:
            current = m.group(1).strip()
            sections[current] = []
        elif current:
            sections[current].append(line)
    return {k: '\n'.join(v).strip() for k, v in sections.items() if v}, content


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


def zip_repo(repo_path: str, zip_name: str):
    path = Path(repo_path).resolve()
    if not path.exists() or not path.is_dir():
        print(f"⚠️  Repo path {repo_path} not found or not a directory")
        return False
    zip_path = Path(zip_name)
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            for file in files:
                if file in EXCLUDE_FILES:
                    continue
                full = Path(root) / file
                arc = full.relative_to(path.parent) if full.is_relative_to(path.parent) else full.name
                z.write(full, arc)
    print(f"✅ Zipped {path} → {zip_path}")
    return True


def produce_b(a_path: Path):
    sections, full_a = parse_a_file(a_path)
    code_section = sections.get("Code", "")
    refs = parse_code_refs(code_section)

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

    b_content = f"""# AGENT LOOP B PACKET for {a_path.name}
Generated: {datetime.now().isoformat()}

<<<INSTRUCTIONS>>>
You are participating in an iterative agentic problem-solving loop.
The user will feed your response back into a local tool, so format matters.

Respond **exactly** in this format (do not add extra sections):

<<<Agent Answer>>>
Concise, high-value contribution. Focus on new insights, decisions, or changes
that can be merged into the original state with minimal redundancy. Take into
account any prior <<<Agent Answer n>>> and <<<Feedback n>>> already in CURRENT A.

<<<Code diff>>>
Unified git diffs (`git diff -U3` style) that can be applied with `git apply`
from the root of the relevant repo. Use one diff block per file when possible.
If no code changes, write "No code changes this turn."
"""

    if attached:
        b_content += "\n<<<ATTACHED REPOS>>>\n"
        b_content += ("The referenced repositories are attached to this message as zip files "
                      "(directories like .git/node_modules are excluded). Unzip and inspect "
                      "them as needed; diffs should apply from each repo's root.\n")
        for label, repo, zip_name in attached:
            b_content += f"- {label}: {zip_name}  (from {repo})\n"

    b_content += "\n<<<CURRENT A>>>\n" + full_a + "\n"

    b_path = a_path.with_suffix('.B.md')
    b_path.write_text(b_content, encoding="utf-8")
    print(f"✅ Wrote {b_path}")
    if copy_to_clipboard(b_content):
        print("📋 Copied B packet to clipboard")
    if attached:
        print("Attach these zips to your message: " + ", ".join(z for _, _, z in attached))


def extract_section(text: str, name: str) -> str:
    m = re.search(rf'<<<{name}>>>(.*?)(?=<<<|$)', text, re.DOTALL | re.IGNORECASE)
    return (m.group(1).strip() if m else f"(No {name} section found)").strip()


def strip_outer_fence(text: str) -> str:
    text = text.strip()
    m = re.match(r'^```[A-Za-z0-9_-]*\s*\n(.*?)\n```$', text, re.DOTALL)
    return m.group(1).strip() if m else text


def next_round_index(a_content: str) -> int:
    """Next round number = highest existing <<<Agent Answer n>>> + 1."""
    nums = [int(n) for n in re.findall(r'<<<Agent Answer (\d+)>>>', a_content, re.IGNORECASE)]
    return (max(nums) + 1) if nums else 1


def next_a_path(a_path: Path, out_number: int) -> Path:
    """Strip any trailing round marker on the stem and re-apply the new one."""
    base = re.sub(r'[._]A\d+$', '', a_path.stem)
    # also drop a bare trailing ".A" left by the template naming (some.A.md)
    base = re.sub(r'\.A$', '', base)
    return a_path.with_name(f"{base}.A{out_number}.md")


def produce_a2(a_path: Path, c_path: Path):
    a_content = a_path.read_text(encoding="utf-8")
    c_content = c_path.read_text(encoding="utf-8")

    agent_answer = extract_section(c_content, "Agent Answer")
    code_diff = strip_outer_fence(extract_section(c_content, "Code diff"))

    idx = next_round_index(a_content)

    a2 = a_content.rstrip()
    a2 += f"\n\n<<<Agent Answer {idx}>>>\n{agent_answer}\n"
    a2 += f"\n<<<Code diff {idx}>>>\n{code_diff}\n"
    a2 += (f"\n<<<Feedback {idx}>>>\n"
           f"(Add your feedback / next instructions here, then run the next round.)\n")

    a2_path = next_a_path(a_path, idx + 1)
    a2_path.write_text(a2, encoding="utf-8")
    print(f"✅ Wrote draft round {idx + 1}: {a2_path}")

    # Sidecar patch for easy `git apply`, when there is a real diff.
    if code_diff and not re.match(r'^\s*no code changes', code_diff, re.IGNORECASE):
        patch_path = a2_path.with_name(f"{a2_path.stem}.diff{idx}.patch")
        patch_path.write_text(code_diff.rstrip() + "\n", encoding="utf-8")
        print(f"✅ Wrote patch: {patch_path}  (apply with: git apply {patch_path.name})")

    print(f"Review, fill in <<<Feedback {idx}>>>, then run: "
          f"python agent_loop.py {a2_path.name} <next C>.md")


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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--template", type=Path, help="Generate initial A template")
    parser.add_argument("a_file", type=Path, nargs="?", help="A.md file")
    parser.add_argument("c_file", type=Path, nargs="?", help="Optional C.md to generate next A")
    args = parser.parse_args()

    if args.template:
        generate_template(args.template)
    elif args.a_file and not args.c_file:
        produce_b(args.a_file)
    elif args.a_file and args.c_file:
        produce_a2(args.a_file, args.c_file)
    else:
        parser.print_help()
