#!/usr/bin/env python3
"""
agent_loop.py - standalone prompt packet compiler and response integrator.

Usage:
  python agent_loop.py A1.md
      Produce B1: a pasteable prompt packet for an external agent.

  python agent_loop.py A1.md C1.txt
      Produce A2.md: the next human-readable prompt document from the
      external agent response.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

try:
    import pyperclip

    CLIPBOARD_AVAILABLE = True
except ImportError:
    pyperclip = None
    CLIPBOARD_AVAILABLE = False


MAX_INLINE_CHARS = 12000
DEFAULT_INCLUDE_GLOBS = (
    "*.cs",
    "*.csproj",
    "*.sln",
    "*.py",
    "*.ps1",
    "*.js",
    "*.jsx",
    "*.ts",
    "*.tsx",
    "*.json",
    "*.yml",
    "*.yaml",
    "*.toml",
    "*.xml",
    "*.html",
    "*.css",
    "*.md",
    "*.txt",
    "*.sql",
    "*.sh",
    "*.bat",
)

BEGIN_MARKER = "<<<AGENT_LOOP_RESPONSE_BEGIN>>>"
END_MARKER = "<<<AGENT_LOOP_RESPONSE_END>>>"
NEXT_A_BEGIN = "<<<NEXT_A_MARKDOWN_BEGIN>>>"
NEXT_A_END = "<<<NEXT_A_MARKDOWN_END>>>"
DIFF_BEGIN = "<<<GIT_DIFF_BEGIN>>>"
DIFF_END = "<<<GIT_DIFF_END>>>"
NOTES_BEGIN = "<<<NOTES_BEGIN>>>"
NOTES_END = "<<<NOTES_END>>>"


@dataclass
class FileReference:
    raw: str
    resolved: Path
    exists: bool
    size: int = 0
    inline: bool = False
    reason: str = ""
    content: str = ""


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def is_probably_text(path: Path) -> bool:
    mime, _ = mimetypes.guess_type(str(path))
    if mime and (mime.startswith("text/") or mime in {"application/json", "application/xml"}):
        return True
    if path.suffix.lower() in {glob[1:] for glob in DEFAULT_INCLUDE_GLOBS if glob.startswith("*")}:
        return True
    try:
        sample = path.read_bytes()[:2048]
    except OSError:
        return False
    return b"\x00" not in sample


def looks_like_file_token(token: str) -> bool:
    if not token or token.startswith(("http://", "https://", "mailto:")):
        return False
    if any(ch in token for ch in ("/", "\\")):
        return True
    suffix = Path(token).suffix.lower()
    known_suffixes = {glob[1:] for glob in DEFAULT_INCLUDE_GLOBS if glob.startswith("*")}
    known_suffixes.update({".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".docx", ".xlsx"})
    return suffix in known_suffixes


def strip_line_noise(token: str) -> str:
    token = token.strip().strip("`'\"")
    token = token.rstrip(".,;:)")
    token = token.lstrip("(")
    if token.startswith("./") or token.startswith(".\\"):
        token = token[2:]
    return token


def extract_frontmatter_files(content: str) -> list[str]:
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not match:
        return []

    files: list[str] = []
    lines = match.group(1).splitlines()
    in_files = False
    for line in lines:
        stripped = line.strip()
        if re.match(r"^(files|file_refs|references)\s*:\s*$", stripped, re.I):
            in_files = True
            continue
        if in_files and re.match(r"^[A-Za-z_][\w-]*\s*:", stripped) and not stripped.startswith("-"):
            in_files = False
        if in_files and stripped.startswith("-"):
            files.append(strip_line_noise(stripped[1:].strip()))
        else:
            inline = re.match(r"^(file|path)\s*:\s*(.+)$", stripped, re.I)
            if inline:
                files.append(strip_line_noise(inline.group(2)))
    return files


def extract_file_references(content: str, base_dir: Path) -> list[FileReference]:
    candidates: list[str] = []
    candidates.extend(extract_frontmatter_files(content))

    for match in re.finditer(r"`([^`\n]+)`", content):
        token = strip_line_noise(match.group(1))
        if looks_like_file_token(token):
            candidates.append(token)

    for match in re.finditer(r"\[[^\]\n]+\]\(([^)\n]+)\)", content):
        token = strip_line_noise(match.group(1).split("#", 1)[0])
        if looks_like_file_token(token):
            candidates.append(token)

    for line in content.splitlines():
        stripped = line.strip()
        list_match = re.match(r"^(?:[-*]|\d+[.)])\s+(?:file|files|path|ref|reference)s?\s*:\s*(.+)$", stripped, re.I)
        if list_match:
            candidates.append(strip_line_noise(list_match.group(1)))

    seen: set[str] = set()
    refs: list[FileReference] = []
    for raw in candidates:
        if not raw or raw in seen:
            continue
        seen.add(raw)
        candidate = Path(raw).expanduser()
        resolved = candidate if candidate.is_absolute() else (base_dir / candidate)
        resolved = resolved.resolve()
        if not resolved.exists():
            refs.append(FileReference(raw=raw, resolved=resolved, exists=False, reason="not found"))
            continue
        if resolved.is_dir():
            refs.append(FileReference(raw=raw, resolved=resolved, exists=True, reason="directory"))
            continue

        size = resolved.stat().st_size
        inline = size <= MAX_INLINE_CHARS and is_probably_text(resolved)
        reason = "included inline" if inline else ("too large" if size > MAX_INLINE_CHARS else "binary or unknown text")
        content_text = ""
        if inline:
            try:
                content_text = read_text(resolved)
            except UnicodeDecodeError:
                inline = False
                reason = "not utf-8 text"
        refs.append(
            FileReference(
                raw=raw,
                resolved=resolved,
                exists=True,
                size=size,
                inline=inline,
                reason=reason,
                content=content_text,
            )
        )
    return refs


def language_for(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    return {
        "csproj": "xml",
        "props": "xml",
        "targets": "xml",
        "ps1": "powershell",
        "py": "python",
        "js": "javascript",
        "jsx": "jsx",
        "ts": "typescript",
        "tsx": "tsx",
        "yml": "yaml",
        "yaml": "yaml",
        "md": "markdown",
    }.get(suffix, suffix or "text")


def compile_prompt(a_path: Path) -> tuple[str, list[FileReference]]:
    a_content = read_text(a_path)
    refs = extract_file_references(a_content, a_path.parent)

    lines: list[str] = []
    lines.append("# Agent Loop Packet")
    lines.append("")
    lines.append(f"Generated: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"Source A: `{a_path}`")
    lines.append("")
    lines.append("## Your Task")
    lines.append("")
    lines.append(
        "You are an external agent in an iterative prompt-improvement loop. "
        "Answer the user's current request, and also return a revised next prompt document "
        "that can be reviewed by a human and used for the next iteration."
    )
    lines.append("")
    lines.append("## Required Response Format")
    lines.append("")
    lines.append("Return exactly one structured response using these markers:")
    lines.append("")
    lines.append(BEGIN_MARKER)
    lines.append(f"{NOTES_BEGIN}")
    lines.append("Human-readable answer, review notes, assumptions, risks, and next-step suggestions.")
    lines.append(f"{NOTES_END}")
    lines.append(f"{NEXT_A_BEGIN}")
    lines.append(
        "A complete revised Markdown document for the next iteration. "
        "It should preserve still-relevant content from the below Current Question section, incorporate your new findings, "
        "and remain readable/editable by a human."
    )
    lines.append(f"{NEXT_A_END}")
    lines.append(f"{DIFF_BEGIN}")
    lines.append(
        "Optional unified git diff only if code changes are proposed. "
        "Leave this section empty if no code diff is needed."
    )
    lines.append(f"{DIFF_END}")
    lines.append(END_MARKER)
    lines.append("")
    lines.append("Important constraints:")
    lines.append("- Do not include hidden reasoning. Use concise rationale and actionable notes instead.")
    lines.append("- If proposing code changes, make the diff suitable for `git apply`.")
    lines.append("- The NEXT_A_MARKDOWN block must be complete; the local tool may write it verbatim as A(n+1).")
    lines.append("- If a referenced file is listed but not included inline, ask the user for it only if it is necessary.")
    lines.append("")
    lines.append("## Current A")
    lines.append("")
    lines.append("```markdown")
    lines.append(a_content.rstrip())
    lines.append("```")
    lines.append("")

    if refs:
        lines.append("## Referenced Files")
        lines.append("")
        listed = [ref for ref in refs if not ref.inline]
        inlined = [ref for ref in refs if ref.inline]
        if listed:
            lines.append("### File Links / Attach Separately")
            lines.append("")
            for ref in listed:
                status = ref.reason if ref.exists else "not found locally"
                size = f", {ref.size} bytes" if ref.exists and ref.size else ""
                lines.append(f"- `{ref.raw}` -> `{ref.resolved}` ({status}{size})")
            lines.append("")
        if inlined:
            lines.append("### Inline File Contents")
            lines.append("")
            for ref in inlined:
                lines.append(f"#### `{ref.raw}`")
                lines.append("")
                lines.append(f"```{language_for(ref.resolved)}")
                lines.append(ref.content.rstrip())
                lines.append("```")
                lines.append("")

    lines.append("Now produce the structured response.")
    return "\n".join(lines).rstrip() + "\n", refs


def copy_to_clipboard(text: str) -> bool:
    if CLIPBOARD_AVAILABLE:
        try:
            pyperclip.copy(text)
            return True
        except Exception:
            pass

    if sys.platform.startswith("win"):
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
                input=text,
                text=True,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:
            return False
    return False


def next_a_path(a_path: Path, explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit)

    match = re.search(r"(\d+)$", a_path.stem)
    if match:
        number = int(match.group(1))
        stem = f"{a_path.stem[:match.start(1)]}{number + 1}"
    else:
        stem = f"{a_path.stem}_next"
    return a_path.with_name(stem + a_path.suffix)


def sidecar_path(a_path: Path, label: str, suffix: str) -> Path:
    return a_path.with_name(f"{a_path.stem}.{label}{suffix}")


def extract_between(text: str, begin: str, end: str) -> str:
    pattern = re.escape(begin) + r"\s*(.*?)\s*" + re.escape(end)
    match = re.search(pattern, text, re.DOTALL)
    return match.group(1).strip() if match else ""


def strip_outer_fence(text: str) -> str:
    text = text.strip()
    match = re.match(r"^```[A-Za-z0-9_-]*\s*\n(.*?)\n```$", text, re.DOTALL)
    return match.group(1).strip() if match else text


def build_fallback_next_a(a_path: Path, c_text: str) -> str:
    original = read_text(a_path).rstrip()
    notes = extract_between(c_text, NOTES_BEGIN, NOTES_END)
    if not notes:
        notes = c_text.strip()

    return "\n\n".join(
        [
            original,
            "---",
            f"## Agent Loop Update ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})",
            strip_outer_fence(notes),
            (
                "Note: The external agent did not provide a valid "
                f"{NEXT_A_BEGIN} / {NEXT_A_END} block, so this update was appended instead."
            ),
        ]
    )


def integrate_response(a_path: Path, c_path: Path, output_path: Path | None = None) -> tuple[Path, Path | None]:
    c_text = read_text(c_path)
    next_a = extract_between(c_text, NEXT_A_BEGIN, NEXT_A_END)
    if next_a:
        next_a = strip_outer_fence(next_a)
    else:
        next_a = build_fallback_next_a(a_path, c_text)

    out_path = output_path or next_a_path(a_path)
    write_text(out_path, next_a)

    diff_text = extract_between(c_text, DIFF_BEGIN, DIFF_END)
    diff_path = None
    if diff_text.strip():
        diff_path = sidecar_path(out_path, "diff", ".patch")
        write_text(diff_path, strip_outer_fence(diff_text))

    notes = extract_between(c_text, NOTES_BEGIN, NOTES_END)
    if notes.strip():
        notes_path = sidecar_path(out_path, "notes", ".md")
        write_text(notes_path, strip_outer_fence(notes))

    return out_path, diff_path


def write_refs_file(a_path: Path, refs: Iterable[FileReference]) -> Path | None:
    refs = list(refs)
    attachable = [ref for ref in refs if not ref.inline]
    if not attachable:
        return None

    path = sidecar_path(a_path, "files", ".txt")
    lines = []
    for ref in attachable:
        lines.append(str(ref.resolved))
    write_text(path, "\n".join(lines))
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compile an A(n) Markdown prompt into B(n), or integrate C(n) into A(n+1)."
    )
    parser.add_argument("a_file", help="A(n) Markdown prompt file")
    parser.add_argument("c_file", nargs="?", help="C(n) response file from the external agent")
    parser.add_argument("-o", "--output", help="Output file for B(n) or A(n+1)")
    parser.add_argument("--no-clipboard", action="store_true", help="Do not copy B(n) to the clipboard")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    a_path = Path(args.a_file).resolve()
    if not a_path.exists():
        print(f"ERROR: A file does not exist: {a_path}", file=sys.stderr)
        return 1

    if args.c_file:
        c_path = Path(args.c_file).resolve()
        if not c_path.exists():
            print(f"ERROR: C file does not exist: {c_path}", file=sys.stderr)
            return 1
        output_path = Path(args.output).resolve() if args.output else None
        out_path, diff_path = integrate_response(a_path, c_path, output_path)
        print(f"Wrote next A: {out_path}")
        if diff_path:
            print(f"Wrote proposed git diff: {diff_path}")
        return 0

    packet, refs = compile_prompt(a_path)
    output_path = Path(args.output).resolve() if args.output else sidecar_path(a_path, "B", ".md")
    write_text(output_path, packet)
    refs_path = write_refs_file(a_path, refs)

    copied = False if args.no_clipboard else copy_to_clipboard(packet)
    print(packet)
    print(f"\nWrote B packet: {output_path}", file=sys.stderr)
    if copied:
        print("Copied B packet to clipboard", file=sys.stderr)
    elif not args.no_clipboard:
        print("Clipboard copy unavailable; use stdout or the B packet file", file=sys.stderr)
    if refs_path:
        print(f"Wrote separate file list: {refs_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
