#!/usr/bin/env python3
"""
agent_relay.py - Markdown conversation relay with optional GUI automation.

Completed <isay> blocks are sent to a linked side-by-side agent client when
available, then the latest agent answer is copied from the client and appended
after the sent prompt.
"""

import argparse
import copy
import fnmatch
import hashlib
from html import parser
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    import pyperclip

    CLIPBOARD_AVAILABLE = True
except ImportError:
    pyperclip = None
    CLIPBOARD_AVAILABLE = False
    print("WARNING: pyperclip not installed. Clipboard feature disabled.")
    print("Install with: pip install pyperclip")


MARKER_PREFIX = "agent_relay"
PROMPT_BEGIN = f"<!-- {MARKER_PREFIX}:prompt-begin -->"
PROMPT_END = f"<!-- {MARKER_PREFIX}:prompt-end -->"
RESPONSE_PENDING = f"<!-- {MARKER_PREFIX}:response-pending:{{action_id}} -->"
CONFIG_FILE_NAME = "agent_relay.config.json"
SUPPORTED_AGENTS = ("grok", "chatgpt", "gemini", "claude", "deepseek")


DEFAULT_AGENT_SETTINGS = {
    "input_hotkey": "",
    "send_hotkey": "enter",
    "safety_delay": 0.8,
    "activation_delay": 1.1,
    "initial_capture_delay": 3.0,
    "confidence": 0.78,
    "max_retries": 3,
    "retry_delay": 0.7,
    "stable_delay": 1.0,
    "click_duration": 0.3,
    "post_copy_delay": 1.1,
}


DEFAULT_CONFIG = {
    "poll_interval": 1.5,
    "agents": {
        agent: {
            **DEFAULT_AGENT_SETTINGS,
            "image_dir": f"resources/{agent}",
        }
        for agent in SUPPORTED_AGENTS
    },
}


def has_title(title: str | None) -> bool:
    return bool(title and title.strip() and title.strip().lower() != "no title")


def get_file_hash(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.md5(path.read_bytes()).hexdigest()


def slugify_title(title: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9\s-]", "", title.strip())
    slug = re.sub(r"[\s_-]+", "-", slug).strip("-").lower()
    return f"{slug}.md" if slug else "conversation.md"

def write_new_md(md_path: Path, title: str | None = None) -> None:
    lines = []
    if has_title(title):
        lines.append(f"**Title:** {title.strip()}")
    lines.append(f"**Started:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ")
    lines.append("---")
    lines.append("")
    template = "\n".join(lines)
    md_path.write_text(template, encoding="utf-8")
    print(f"Created new conversation: {md_path}")
    if has_title(title):
        print(f"Title : {title.strip()}")

def markdown_quote(text: str) -> str:
    quoted = []
    for line in text.splitlines():
        quoted.append("> " + line.rstrip() if line.strip() else ">")
    return "\n".join(quoted)


def append_agent_answer(answer: str | None) -> str:
    if answer and answer.strip():
        return f"\n\n{answer.strip()}\n"

    return "\n\n> failed to capture agent response.\n"


@dataclass
class PendingAction:
    kind: str
    action_id: str
    prompt: str = ""
    window_pattern: str = ""
    agent_name: str = ""


def has_processed_prompt(content: str) -> bool:
    return (
        PROMPT_BEGIN in content
        or re.search(r"^\s*## Initial Prompt\s*$", content, re.MULTILINE) is not None
    )


def needs_initial_rule_before(content_before: str) -> bool:
    meaningful_lines = [line.strip() for line in content_before.splitlines() if line.strip()]
    return not meaningful_lines or meaningful_lines[-1] != "---"


def format_initial_prompt(prompt: str, content_before: str) -> str:
    prefix = "\n---\n" if needs_initial_rule_before(content_before) else "\n"
    return f"{prefix}## Initial Prompt\n{prompt.strip()}\n---"


def format_followup_prompt(prompt: str) -> str:
    return f"\n{PROMPT_BEGIN}\n{markdown_quote(prompt)}\n{PROMPT_END}"


def send_and_capture(gui_bridge, prompt: str, md_path: Path | None = None, expected_hash: str | None = None) -> str | None:
    if gui_bridge:
        return gui_bridge.send_and_capture(prompt, md_path=md_path, expected_hash=expected_hash)

    copy_to_clipboard(prompt, "human response")
    return None


def make_action_id(kind: str, index: int) -> str:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    return f"{kind}-{stamp}-{index}"


def response_pending_marker(action_id: str) -> str:
    return RESPONSE_PENDING.format(action_id=action_id)


def process_say_tags(content: str, action_start_index: int = 0) -> tuple[str, bool, str, list[PendingAction]]:
    """Process only complete <isay> blocks."""
    modified = False
    last_response_content = ""
    actions = []
    lines = content.splitlines()
    result = []
    i = 0

    while i < len(lines):
        stripped = lines[i].strip()

        if not stripped.startswith("<isay>"):
            result.append(lines[i])
            i += 1
            continue

        closing_index = None
        for j in range(i, len(lines)):
            if lines[j].strip().endswith("</isay>"):
                closing_index = j
                break

        if closing_index is None:
            result.append(lines[i])
            i += 1
            continue

        modified = True

        lines[i] = lines[i].replace("<isay>", "", 1)
        lines[closing_index] = lines[closing_index].replace("</isay>", "", 1)
        
        # repsonse_content to include the above if they are not empty
        if lines[i].strip():
            response_content = [lines[i].rstrip()]
        else:
            response_content = []
        response_content += [lines[k].rstrip() for k in range(i + 1, closing_index)]
        if closing_index != i and lines[closing_index].strip():
            response_content.append(lines[closing_index].rstrip())

        response_text = "\n".join(response_content).strip()

        content_before = "\n".join(result)
        if has_processed_prompt(content_before):
            prompt_markdown = format_followup_prompt(response_text)
        else:
            prompt_markdown = format_initial_prompt(response_text, content_before)

        if response_text:
            action_id = make_action_id("say", action_start_index + len(actions))
            actions.append(PendingAction(kind="say", action_id=action_id, prompt=response_text))
            prompt_markdown = f"{prompt_markdown}\n{response_pending_marker(action_id)}"

        result.extend(prompt_markdown.splitlines())

        i = closing_index + 1

    return "\n".join(result), modified, actions


def extract_prompt_from_marked_block(block: str) -> str:
    lines = []
    for line in block.splitlines():
        stripped = line.rstrip()
        if stripped.startswith("> "):
            lines.append(stripped[2:])
        elif stripped == ">":
            lines.append("")
    return "\n".join(lines).strip()


def retry_last_prompt(content: str, action_start_index: int = 0) -> tuple[str, bool, list[PendingAction]]:
    if "<iretry/>" not in content:
        return content, False, []

    retry_index = content.find("<iretry/>")
    search_area = content[:retry_index]
    begin_marker, end_marker, begin = latest_prompt_marker(search_area)
    end = search_area.find(end_marker, begin) if begin != -1 else -1

    if begin == -1 or end == -1 or end > retry_index:
        return content.replace("<iretry/>", "> unable to retry: no previous prompt found.", 1), True, []

    prompt_block_start = begin + len(begin_marker)
    prompt = extract_prompt_from_marked_block(search_area[prompt_block_start:end])
    if not prompt:
        return content[:end + len(end_marker)] + "\n\n> unable to retry: previous prompt was empty.\n", True, []

    kept = content[: end + len(end_marker)].rstrip()
    action_id = make_action_id("retry", action_start_index)
    action = PendingAction(kind="retry", action_id=action_id, prompt=prompt)
    return kept + "\n" + response_pending_marker(action_id), True, [action]


def latest_prompt_marker(content: str) -> tuple[str, str, int]:
    current_begin = content.rfind(PROMPT_BEGIN)
    return PROMPT_BEGIN, PROMPT_END, current_begin

def parse_tag_attrs(attr_text: str) -> dict[str, str]:
    return {key: value for key, value in re.findall(r"(\w+)=\"([^\"]*)\"", attr_text)}


def process_link_tags(content: str, action_start_index: int = 0) -> tuple[str, bool, list[PendingAction]]:
    pattern = re.compile(r"<ilink\b([^>]*)/>")
    changed = False
    actions = []

    def replace(match: re.Match) -> str:
        nonlocal changed
        changed = True
        attrs = parse_tag_attrs(match.group(1))
        window_pattern = attrs.get("window") or attrs.get("windowName") or ""
        agent_name = attrs.get("agent", "").strip().lower()
        action_id = make_action_id("link", action_start_index + len(actions))
        actions.append(
            PendingAction(
                kind="link",
                action_id=action_id,
                window_pattern=window_pattern.strip(),
                agent_name=agent_name,
            )
        )
        return f'<!-- linking to client "{window_pattern}"... {MARKER_PREFIX}:link-pending:{action_id} -->'

    return pattern.sub(replace, content), changed, actions


def process_special_prompt_tag(content: str) -> tuple[str, bool, str]:
    if "<iwrapup/>" in content:
        wrapup_prompt = (
            "Please consolidate the most current, internally consistent, and well-supported conclusions, findings, "
            "and status from this conversation into a clear and coherent summary. Exclude information that was later "
            "found to be incorrect, speculative, misleading, outdated, irrelevant, or superseded. Structure the summary "
            "so another agent or a new thread can continue seamlessly without losing context. Include references to any "
            "relevant artifacts, files, images, outputs, or external resources discussed. Exclude code blocks, "
            "implementation diffs, or iterative edits unless they are essential to understanding the discussion."
        )
        return content.replace("<iwrapup/>", markdown_quote(wrapup_prompt)), True, wrapup_prompt
    
    if "<istudy/>" in content:
        study_prompt = (
            "Please provide a concise but comprehensive summary of the conversation so far, including the main ideas "
            "explored, key reasoning steps, unresolved questions, corrections, dead ends, and changes in understanding "
            "over time. Structure the summary so another agent or a new thread can continue seamlessly without losing "
            "context. Include references to any relevant artifacts, files, images, outputs, or external resources "
            "discussed. Exclude code blocks, implementation diffs, or iterative edits unless they are essential to "
            "understanding the discussion."
        )
        return content.replace("<istudy/>", markdown_quote(study_prompt)), True, study_prompt
    
    if "<ireprompt/>" in content:
        reprompt_prompt = (
            "Please create a single, high-quality, self-contained prompt that a fresh new agent can use as its initial prompt."
            "The new prompt must capture the original goal, all key insights, corrections, evolved requirements, and essential artifacts (files, images, outputs, etc.) from this conversation, so the new agent can reach the current state or go further with its own reasoning."
            "Make it clear, focused, and optimized for best results. Instruct the new agent to think step-by-step and aim for superior outcomes where possible."
            "Return only the final improved prompt enclosed in triple backticks (```). No extra text."
        )
        return content.replace("<ireprompt/>", markdown_quote(reprompt_prompt)), True, reprompt_prompt

    return content, False, ""


def replace_first(text: str, old: str, new: str) -> str:
    index = text.find(old)
    if index == -1:
        return text
    return text[:index] + new + text[index + len(old):]


def script_dir() -> Path:
    return Path(__file__).resolve().parent


def config_path() -> Path:
    return script_dir() / CONFIG_FILE_NAME


def write_default_config() -> Path:
    path = config_path()
    path.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
    return path


def deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_app_config() -> dict:
    path = config_path()
    if not path.exists():
        return copy.deepcopy(DEFAULT_CONFIG)

    loaded = json.loads(path.read_text(encoding="utf-8"))
    return deep_merge(DEFAULT_CONFIG, loaded)


def resolve_config_path(value: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    return (script_dir() / candidate).resolve()


def enumerate_images(directory: Path, prefix: str) -> list[Path]:
    if not directory.exists() or not directory.is_dir():
        return []

    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file()
        and path.suffix.lower() == ".png"
        and path.stem.lower().startswith(prefix.lower())
    )


def image_click_offset(path: Path) -> tuple[int, int]:
    match = re.search(r"-(-?\d+),(-?\d+)$", path.stem)
    if not match:
        return 0, 0
    return int(match.group(1)), int(match.group(2))


def build_agent_config(app_config: dict, agent_name: str) -> "GuiConfig":
    agent_key = agent_name.lower()
    if agent_key not in app_config["agents"]:
        raise ValueError(f'unsupported agent "{agent_name}"')

    settings = app_config["agents"][agent_key]
    image_dir = resolve_config_path(settings.get("image_dir", agent_key))
    input_boxes = enumerate_images(image_dir, "inputbox")
    copy_buttons = enumerate_images(image_dir, "copybutton")
    scroll_down_buttons = enumerate_images(image_dir, "scrolldown")

    missing = []
    if not input_boxes:
        missing.append("inputbox*.png")
    if not copy_buttons:
        missing.append("copybutton*.png")
    if not scroll_down_buttons:
        missing.append("scrolldown*.png")
    if missing:
        raise ValueError(f'{agent_key} missing image assets in {image_dir}: {", ".join(missing)}')

    return GuiConfig(
        agent_name=agent_key,
        input_hotkey=settings.get("input_hotkey", ""),
        input_boxes=input_boxes,
        send_hotkey=settings.get("send_hotkey", "enter"),
        safety_delay=float(settings.get("safety_delay", 0.8)),
        activation_delay=float(settings.get("activation_delay", 1.1)),
        initial_capture_delay=float(settings.get("initial_capture_delay", 3.0)),
        scroll_down_buttons=scroll_down_buttons,
        copy_buttons=copy_buttons,
        confidence=float(settings.get("confidence", 0.78)),
        max_retries=int(settings.get("max_retries", 3)),
        retry_delay=float(settings.get("retry_delay", 0.7)),
        stable_delay=float(settings.get("stable_delay", 1.0)),
        click_duration=float(settings.get("click_duration", 0.3)),
        post_copy_delay=float(settings.get("post_copy_delay", 1.1)),
    )


def get_window_title(win) -> str:
    return getattr(win, "title", "") or ""


def window_pattern_matches(title: str, pattern: str) -> bool:
    if not pattern:
        return False

    title_lower = title.lower()
    pattern_lower = pattern.lower()
    if "*" in pattern_lower or "?" in pattern_lower:
        return fnmatch.fnmatchcase(title_lower, pattern_lower)

    return pattern_lower in title_lower


def find_window_by_pattern(gw, pattern: str):
    try:
        windows = gw.getAllWindows()
    except Exception:
        windows = gw.getWindowsWithTitle(pattern)

    for win in windows:
        title = get_window_title(win)
        if title and window_pattern_matches(title, pattern):
            return win

    return None


def guess_agent_name(pattern: str, actual_title: str) -> tuple[str | None, str | None]:
    text = f"{pattern} {actual_title}".lower()
    matches = [agent for agent in SUPPORTED_AGENTS if agent in text]
    unique_matches = sorted(set(matches))

    if len(unique_matches) == 1:
        return unique_matches[0], None
    if not unique_matches:
        return None, "unable to guess agent type"
    return None, f"ambiguous agent type: {', '.join(unique_matches)}"


def complete_send_action(md_path: Path, action: PendingAction, gui_bridge, expected_hash: str) -> None:
    answer = send_and_capture(gui_bridge, action.prompt, md_path=md_path, expected_hash=expected_hash)
    if get_file_hash(md_path) != expected_hash:
        return

    content = md_path.read_text(encoding="utf-8")
    marker = response_pending_marker(action.action_id)
    md_path.write_text(replace_first(content, marker, append_agent_answer(answer)), encoding="utf-8")


def complete_link_action(md_path: Path, action: PendingAction, app_config: dict) -> object | None:
    pending_line = f'<!-- linking to client "{action.window_pattern}"... {MARKER_PREFIX}:link-pending:{action.action_id} -->'
    result_line = f'<!-- unable to link to client "{action.window_pattern}". -->'
    gui_bridge = None

    try:
        import pygetwindow as gw

        win = find_window_by_pattern(gw, action.window_pattern)
        if not win:
            raise RuntimeError(f'window not found: "{action.window_pattern}"')

        actual_title = get_window_title(win)
        agent_name = action.agent_name
        if agent_name:
            if agent_name not in SUPPORTED_AGENTS:
                raise RuntimeError(f'unsupported agent type: "{agent_name}"')
        else:
            guessed_agent, guess_error = guess_agent_name(action.window_pattern, actual_title)
            if guess_error:
                raise RuntimeError(guess_error)
            agent_name = guessed_agent

        config = build_agent_config(app_config, agent_name)
        candidate = GuiBridge(config, action.window_pattern)
        if candidate.find_and_activate_window():
            gui_bridge = candidate
            result_line = f'<!-- connected to {agent_name} client "{actual_title}". -->'
    except Exception as exc:
        print(f"Link failed: {exc}")
        result_line = f'<!-- unable to link to client "{action.window_pattern}": {exc}. -->'

    content = md_path.read_text(encoding="utf-8")
    md_path.write_text(replace_first(content, pending_line, result_line), encoding="utf-8")
    return gui_bridge


def complete_pending_actions(md_path: Path, actions: list[PendingAction], gui_bridge, app_config: dict, expected_hash: str):
    current_expected_hash = expected_hash
    for action in actions:
        if action.kind == "link":
            linked_bridge = complete_link_action(md_path, action, app_config)
            if linked_bridge:
                gui_bridge = linked_bridge
            current_expected_hash = get_file_hash(md_path)
        elif action.kind in {"say", "retry"}:
            complete_send_action(md_path, action, gui_bridge, current_expected_hash)
            current_expected_hash = get_file_hash(md_path)
    return gui_bridge


def copy_to_clipboard(text: str, label: str) -> None:
    if not CLIPBOARD_AVAILABLE or not text.strip():
        return
    try:
        pyperclip.copy(text)
        print(f"Copied {label} to clipboard")
    except Exception:
        pass


@dataclass
class GuiConfig:
    agent_name: str
    input_hotkey: str
    input_boxes: list[Path]
    send_hotkey: str
    safety_delay: float
    activation_delay: float
    initial_capture_delay: float
    scroll_down_buttons: list[Path]
    copy_buttons: list[Path]
    confidence: float
    max_retries: int
    retry_delay: float
    stable_delay: float
    click_duration: float
    post_copy_delay: float


class GuiBridge:
    def __init__(self, config: GuiConfig, window_pattern: str):
        self.config = config
        self.window_pattern = window_pattern
        self.pyautogui = None
        self.gw = None
        self.win32gui = None
        self.win32con = None
        self.pywin32_available = False
        self._load_modules()

    def _load_modules(self) -> None:
        try:
            import pyautogui
            import pygetwindow as gw
        except ImportError as exc:
            raise RuntimeError(
                "GUI mode requires pyautogui and pygetwindow. Install with: pip install pyautogui pygetwindow"
            ) from exc

        self.pyautogui = pyautogui
        self.gw = gw
        self.pyautogui.FAILSAFE = True
        self.pyautogui.PAUSE = 0.4

        try:
            import win32con
            import win32gui

            self.win32con = win32con
            self.win32gui = win32gui
            self.pywin32_available = True
        except ImportError:
            self.pywin32_available = False
            self.log("WARNING: pywin32 not installed; falling back to pygetwindow activation.")

    def log(self, msg: str) -> None:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    def find_and_activate_window(self):
        win = find_window_by_pattern(self.gw, self.window_pattern)
        if not win:
            self.log(f"Window not found: {self.window_pattern}")
            return None

        try:
            if self.pywin32_available:
                hwnd = win._hWnd
                #self.win32gui.ShowWindow(hwnd, self.win32con.SW_RESTORE)
                self.win32gui.ShowWindow(hwnd, self.win32con.SW_SHOW)
                time.sleep(0.5)
                self.win32gui.SetForegroundWindow(hwnd)
                time.sleep(self.config.activation_delay)
            else:
                #win.restore()
                win.activate()
                time.sleep(self.config.activation_delay)

            if win.left is not None and win.top is not None:
                self.pyautogui.click(win.left + 180, win.top + 35)
                time.sleep(0.6)

            return win
        except Exception:
            return win

    def press_key_or_hotkey(self, key_spec: str) -> None:
        keys = [key.strip() for key in key_spec.split("+") if key.strip()]
        if len(keys) > 1:
            self.pyautogui.hotkey(*keys)
        elif keys:
            self.pyautogui.press(keys[0])

    def click_point_for_match(self, template_path: Path, location):
        center = self.pyautogui.center(location)
        dx, dy = image_click_offset(template_path)
        return center.x + dx, center.y + dy

    def locate_first_existing_input_box(self, win):
        if win.left is None or win.top is None or win.width is None or win.height is None:
            self.log("Window geometry unavailable; cannot locate prompt input")
            return None

        region = (win.left, win.top, win.width, win.height)
        found_input_templates = [path for path in self.config.input_boxes if path.exists()]
        missing_input_templates = [path for path in self.config.input_boxes if not path.exists()]

        for path in missing_input_templates:
            self.log(f"Input box image not found on disk: {path}")

        for path in found_input_templates:
            for _ in range(self.config.max_retries):
                try:
                    location = self.pyautogui.locateOnScreen(
                        str(path),
                        confidence=self.config.confidence,
                        region=region,
                    )
                    if location:
                        self.log(f"Located prompt input box ({path.name})")
                        return location, path
                except Exception as exc:
                    self.log(f"Locate error (Prompt input box {path.name}): {exc}")
                time.sleep(self.config.retry_delay)

        if found_input_templates:
            names = ", ".join(path.name for path in found_input_templates)
            self.log(f"No input box image matched on screen ({names})")
        else:
            self.log("No input box images available")

        return None

    def focus_prompt_input(self, win) -> bool:
        if self.config.input_hotkey:
            self.pyautogui.hotkey(*self.config.input_hotkey.split("+"))
            self.log(f"Input hotkey pressed: {self.config.input_hotkey}")
            return True

        matched = self.locate_first_existing_input_box(win)
        if not matched:
            return False

        location, path = matched
        click_point = self.click_point_for_match(path, location)
        self.pyautogui.moveTo(*click_point, duration=self.config.click_duration)
        self.pyautogui.click(*click_point)
        self.log(f"Clicked prompt input box ({path.name}) at {click_point}")
        return True

    def locate_and_click(self, template_path: Path, action_name: str, region=None) -> bool:
        for _ in range(self.config.max_retries):
            try:
                location = self.pyautogui.locateOnScreen(
                    str(template_path),
                    confidence=self.config.confidence,
                    region=region,
                )
                if location:
                    click_point = self.click_point_for_match(template_path, location)
                    self.pyautogui.moveTo(*click_point, duration=self.config.click_duration)
                    self.pyautogui.click(*click_point)
                    self.log(f"Clicked {action_name}")
                    time.sleep(self.config.retry_delay)
                    return True
            except Exception as exc:
                self.log(f"Locate error ({action_name}): {exc}")
            time.sleep(self.config.retry_delay)
        return False

    def locate_and_click_any(self, template_paths: list[Path], action_name: str, region=None) -> bool:
        for path in template_paths:
            if self.locate_and_click(path, f"{action_name} ({path.name})", region=region):
                return True
        return False

    def find_stable_copy_above_input(self, win):
        if not self.config.copy_buttons:
            self.log("No copy button images configured")
            return None

        input_match = self.locate_first_existing_input_box(win)
        if not input_match:
            self.log("Cannot use input box cue for response capture")
            return None

        input_box, _ = input_match
        input_top = input_box.top
        region_height = max(1, input_top - win.top)
        region = (win.left, win.top, win.width, region_height)

        def choose_candidate():
            try:
                matches = []
                for path in self.config.copy_buttons:
                    for box in self.pyautogui.locateAllOnScreen(
                        str(path),
                        confidence=self.config.confidence,
                        grayscale=False,
                        region=region,
                    ):
                        matches.append((box, path))
            except Exception as exc:
                self.log(f"Locate error (Copy above input): {exc}")
                return None

            # The button normally should be also on the left side of the input box. There are distracting ones on the right and they should definitely be excluded.
            # Review and revise this when an exception has been found.
            above = [
                (box, path)
                for box, path in matches
                if box.top + box.height <= input_top and box.left <= input_box.left + input_box.width
            ]
            if not above:
                return None

            return min(above, key=lambda match: (input_top - (match[0].top + match[0].height), match[0].left))

        first = choose_candidate()
        if not first:
            self.log("No copy button found above prompt input")
            return None

        first_box, first_path = first
        first_center = self.pyautogui.center(first_box)
        self.log(f"Candidate copy button above input ({first_path.name}) at {first_center}; checking stability")
        time.sleep(self.config.stable_delay)

        second = choose_candidate()
        if not second:
            self.log("Copy button disappeared during stability check")
            return None

        second_box, second_path = second
        second_center = self.pyautogui.center(second_box)
        if abs(first_center.x - second_center.x) > 3 or abs(first_center.y - second_center.y) > 3:
            self.log(f"Copy button moved from {first_center} to {second_center}; not clicking")
            return None

        return self.click_point_for_match(second_path, second_box)

    def capture_latest_response(self) -> str | None:
        self.log("Starting agent response capture")
        win = self.find_and_activate_window()
        if not win:
            return None

        try:
            if self.config.scroll_down_buttons:
                region_bottom = None
                if win.left is not None and win.top is not None and win.width is not None and win.height is not None:
                    region_bottom = (
                        win.left,
                        win.top + int(win.height * 0.5),
                        win.width,
                        int(win.height * 0.55),
                    )
                self.locate_and_click_any(self.config.scroll_down_buttons, "Scroll-to-bottom", region=region_bottom)

            center = self.find_stable_copy_above_input(win)
            if not center:
                self.log("No stable copy button could be located above input")
                return None

            self.pyautogui.moveTo(center, duration=self.config.click_duration)
            self.pyautogui.click(center)
            self.log("Clicked selected Copy button")
            time.sleep(self.config.post_copy_delay)

            if not CLIPBOARD_AVAILABLE:
                self.log("Clipboard is unavailable")
                return None

            text = pyperclip.paste().strip()
            # Normalize line breaks and trim excessive blank lines
            text = re.sub(r"\r\n?", "\n", text)
            text = re.sub(r"(?<!\n)\n\n(?!\n)", "\n", text)
            text = re.sub(r"\n{3,}", "\n\n", text)
            
            self.log(f"Captured {len(text)} characters")
            return text if text else None
        except Exception as exc:
            self.log(f"Capture error: {exc}")
            return None

    def capture_latest_response_until_file_change(self, md_path: Path, expected_hash: str) -> str | None:
        self.log("Starting agent response capture; will keep trying until the Markdown file changes")

        while get_file_hash(md_path) == expected_hash:
            response = self.capture_latest_response()
            if response:
                return response

            if get_file_hash(md_path) != expected_hash:
                self.log("Markdown file changed; stopping response capture")
                return None

            self.log("No stable copy button yet; retrying")
            time.sleep(self.config.retry_delay)

        self.log("Markdown file changed; stopping response capture")
        return None

    def send_prompt_to_agent(self, prompt: str) -> bool:
        self.log(f"Sending prompt: {prompt[:70]}{'...' if len(prompt) > 70 else ''}")
        win = self.find_and_activate_window()
        if not win:
            return False

        try:
            if not self.focus_prompt_input(win):
                self.log("Prompt input box could not be focused")
                return False
            time.sleep(self.config.safety_delay)
            pyperclip.copy(prompt.strip())
            self.pyautogui.hotkey("ctrl", "v")
            self.log("Prompt pasted; pressing send key")
            time.sleep(self.config.safety_delay)
            self.press_key_or_hotkey(self.config.send_hotkey)
            self.log("Send key pressed; delivery is not verified")
            return True
        except Exception as exc:
            self.log(f"Send error: {exc}")
            return False

    def send_and_capture(self, prompt: str, md_path: Path | None = None, expected_hash: str | None = None) -> str | None:
        if not self.send_prompt_to_agent(prompt):
            return None
        self.log(f"Waiting {self.config.initial_capture_delay:.1f}s before looking for agent response")
        time.sleep(self.config.initial_capture_delay)

        if md_path is not None and expected_hash is not None:
            return self.capture_latest_response_until_file_change(md_path, expected_hash)

        return self.capture_latest_response()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Watch a Markdown conversation file and optionally bridge replies to a GUI agent client."
    )

    parser.add_argument(
        "--create",
        metavar="TITLE",
        nargs=1,                    # Take exactly one argument (title)
        help='Create a new markdown file. Title should be quoted if it contains spaces.'
    )

    parser.add_argument("--write-config", action="store_true", help="Write default configuration file")

    parser.add_argument(
        "target",
        nargs="?",                  # Optional
        default=None,
        help="Markdown file to process (default mode)"
    )

    return parser

def main() -> None:
    args = build_parser().parse_args()

    if args.write_config:
        path = write_default_config()
        print(f"Wrote default config: {path}")
        return
    
    md_path = Path(args.target) if args.target else (script_dir() / slugify_title(args.create[0]) if args.create else script_dir() / "conversation.md")

    if args.create:   # create only
        title = args.create[0].strip('"') if args.create[0] else None   # Since nargs=1, it's a list with one item
        write_new_md(md_path, title)
        # return the name of file to the console so it can be captured by a parent process or script
        print(md_path)
        return

    app_config = load_app_config()

    if not md_path.exists():
        print(f"File does not exist: {md_path}")
        sys.exit(1)

    # If the file is empty or trivially containing only whitespace, also call write_new_md() to add the initial template with title and timestamp
    if md_path.stat().st_size == 0 or md_path.read_text(encoding="utf-8").strip() == "":
        write_new_md(md_path, '')
    gui_bridge = None

    print(f"agent_relay started - Monitoring: {md_path}")
    print(f"Config: {config_path()}")
    print(f"Supported agents: {', '.join(SUPPORTED_AGENTS)}")
    print('GUI linking: use <ilink window="Grok *" agent="grok"/> in the Markdown file')
    print("\nSupported tags:")
    print('  <ilink window="..." agent="..."/>')
    print("  <isay> ... </isay>")
    print("  <iretry/>")
    print("  <iwrapup/>")
    print("  <ireprompt/>")
    print("  <istudy/>")
    print("Press Ctrl+C to stop.\n")

    last_hash = get_file_hash(md_path)

    try:
        while True:
            current_hash = get_file_hash(md_path)

            if current_hash != last_hash:
                print(f"Change detected at {datetime.now().strftime('%H:%M:%S')}")
                content = md_path.read_text(encoding="utf-8")
                actions = []

                content, link_changed, link_actions = process_link_tags(content, action_start_index=len(actions))
                actions.extend(link_actions)

                content, retry_changed, retry_actions = retry_last_prompt(content, action_start_index=len(actions))
                actions.extend(retry_actions)

                content, resp_changed, say_actions = process_say_tags(
                    content,
                    action_start_index=len(actions),
                )
                actions.extend(say_actions)
                content, special_prompt_changed, special_prompt_text = process_special_prompt_tag(content)

                if link_changed or retry_changed or resp_changed or special_prompt_changed:
                    md_path.write_text(content, encoding="utf-8")
                    action_hash = get_file_hash(md_path)
                    print("Processed tags")

                    if special_prompt_text:
                        copy_to_clipboard(special_prompt_text, "special prompt")

                    if actions:
                        gui_bridge = complete_pending_actions(md_path, actions, gui_bridge, app_config, action_hash)

                last_hash = get_file_hash(md_path)

            time.sleep(float(app_config.get("poll_interval", 1.5)))

    except KeyboardInterrupt:
        print("\nagent_relay stopped.")
    except Exception as exc:
        print(f"Error: {exc}")


if __name__ == "__main__":
    main()
