"""Data for the herdr-projects board: what each project needs, has queued, runs, reviews and shipped.

Reads what herdr-projects documents: `herdr-projects list`, `herdr-projects thread list --json`, each
project's TASKS.md, STATUS.md and PROJECT.md. It never writes a project's files: task changes and answers go to the project's coordinator as one sentence, through
`herdr-projects coordinator prompt`, the same channel herdr-projects' own popup uses. It writes only its
own settings (config dir) and state (state dir).
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import time
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python < 3.11, e.g. the python3 macOS ships
    tomllib = None


def read_toml(text):
    """TOML as a dict. shortcut: without tomllib, only `key = value` lines (strings, numbers, booleans)
    under top-level keys and `[table]` headers are read; arrays of tables are skipped. That covers the
    files this board reads; upgrade if it ever needs arrays."""
    if tomllib:
        return tomllib.loads(text)
    out, table, skip = {}, None, False
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("[["):
            skip = True
            continue
        if s.startswith("["):
            skip, table = False, out
            for part in s.strip("[]").split("."):
                table = table.setdefault(part.strip().strip('"'), {})
            continue
        m = re.match(r"^([A-Za-z0-9_-]+)\s*=\s*(.+?)\s*$", s)
        if skip or not m:
            continue
        key, raw = m.groups()
        target = out if table is None else table
        basic = re.match(r'^("(?:[^"\\]|\\.)*")\s*(?:#.*)?$', raw)
        literal = re.match(r"^'([^']*)'\s*(?:#.*)?$", raw)
        try:
            if basic:
                target[key] = json.loads(basic.group(1))
            elif literal:
                target[key] = literal.group(1)
            else:
                target[key] = json.loads(re.sub(r"\s+#.*$", "", raw))
        except ValueError:
            pass
    return out


COLUMNS = ("Needs you", "To do", "In progress", "In review", "Shipped")
DEFAULTS = {
    "root": "",                  # empty: herdr-projects' own order ($HERDR_PROJECTS_ROOT, its config.toml, ~/.herdr-projects)
    "herdr_projects_bin": "",    # empty: on PATH, else the plugin's own build
    "shipped_days": 14,          # how far back Shipped looks
    "refresh_seconds": 5,
    "stale_hours": 24,           # a running or reviewed thread untouched this long turns amber
    "show_review_threads": False,  # threads without a worktree (reviewers) are usually the coordinator's sub-work
    "palette": "herdr",          # herdr: follow herdr's theme · soft: the board's own · terminal: the terminal's 16 colours
    "editor": "",                # for TASKS.md and reports; empty: herdr's $EDITOR (vi when unset)
    "close_after_send": False,   # close the board once a message to a coordinator is sent
}
# Editors the settings panel offers when installed; the GUI ones open in their own window, not a herdr tab.
TERMINAL_EDITORS = ("nano", "micro", "vim", "nvim", "hx", "emacs")
GUI_EDITORS = ("code", "cursor", "zed", "subl")


def editor_choices():
    return ("",) + tuple(e for e in TERMINAL_EDITORS + GUI_EDITORS if shutil.which(e))

# Settings the board can change itself: group, key, label, choices, how to show a value.
# A setting with no choices is an action: Enter runs it.
EDITABLE = (
    ("General", "refresh_seconds", "Refresh every", (2, 5, 10, 30), "{} s"),
    ("General", "close_after_send", "Close after sending", (False, True), None),
    ("General", "palette", "Colours", ("herdr", "soft", "terminal"), "{}"),
    ("General", "editor", "Open files with", editor_choices, "{}"),  # choices computed when shown: what is installed
    ("Cards", "show_review_threads", "Show reviewer threads", (False, True), None),
    ("Cards", "stale_hours", "Flag untouched threads after", (4, 12, 24, 48, 72), "{} h"),
    ("Cards", "shipped_days", "Shipped looks back", (7, 14, 30, 90), "{} days"),
    ("Layout", "reset_layout", "Reset folds and section order", None, None),
)


def setting_text(key, fmt, value):
    """How a setting's value reads in the panel."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if key == "editor" and value == "":
        return "herdr's $EDITOR"
    return fmt.format(value)
NEEDS_GROUPS = ("Decisions", "Your tasks", "Waiting on you")


@dataclass
class Item:
    text: str
    kind: str = "task"   # task (in TASKS.md), decision (a DECIDE: task), thread, shipped
    section: str = ""    # heading the card sits under, as shown (on All projects, with the project's name)
    group: str = ""      # the heading itself: a TASKS.md section, or a Needs you group; folds are keyed on it
    label: str = ""      # small line on the card: a thread id, a date, "Decide"
    note: str = ""       # shown after the text: a thread's activity
    link: str = ""       # a pull request
    pane: str = ""       # a thread's pane
    thread: str = ""     # a thread's id
    project: str = ""    # the project's slug
    age: float | None = None   # seconds since a thread last changed
    stale: bool = False
    moved: str = ""      # a move waiting for you to send ("pending") or for the coordinator to apply ("sent")
    pending: str = ""    # an action waiting for you to send: done, delegate, drop, answer or message

    def shown(self):
        return f"{self.text} · {self.note}" if self.note else self.text


@dataclass
class Project:
    slug: str
    name: str
    path: Path
    columns: dict = field(default_factory=lambda: {c: [] for c in COLUMNS})
    error: str = ""


# configuration ---------------------------------------------------------------
MINIMUMS = {"refresh_seconds": 1, "shipped_days": 1, "stale_hours": 1}


def load_config(config_dir):
    """Settings from config.toml; a value of the wrong type (or below its minimum) keeps its default,
    and a file that does not parse leaves every default."""
    cfg = dict(DEFAULTS)
    path = Path(config_dir or "") / "config.toml"
    if not (config_dir and path.exists()):
        return cfg
    try:
        data = read_toml(path.read_text())
    except (ValueError, OSError):
        return cfg
    for key, value in data.items():
        default = DEFAULTS.get(key)
        if key not in DEFAULTS or type(value) is not type(default):
            continue
        if key in MINIMUMS and value < MINIMUMS[key]:
            continue
        cfg[key] = value
    return cfg


def save_config(config_dir, cfg):
    """Update config.toml in place: each setting's line is rewritten, added, or (back at its default)
    removed; comments and keys the board does not know are kept."""
    path = Path(config_dir) / "config.toml"
    lines = path.read_text().splitlines() if path.exists() else \
        ["# Projects board settings. Every key is optional; see config.example.toml."]
    for key, default in DEFAULTS.items():
        value = cfg.get(key, default)
        line = f"{key} = {json.dumps(value)}"
        at = next((i for i, l in enumerate(lines) if re.match(rf"^\s*{key}\s*=", l)), None)
        if value == default:
            if at is not None:
                del lines[at]
        elif at is None:
            lines.append(line)
        else:
            lines[at] = line
    write_atomically(path, "\n".join(lines) + "\n")
    return path


def write_atomically(path, text):
    """Write through a temporary file, so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def resolve_root(cfg, env=os.environ, home=Path.home()):
    """The projects root, in herdr-projects' own order."""
    if cfg.get("root"):
        return Path(cfg["root"]).expanduser()
    if env.get("HERDR_PROJECTS_ROOT"):
        return Path(env["HERDR_PROJECTS_ROOT"]).expanduser()
    hp_config = home / ".config" / "herdr-projects" / "config.toml"
    if hp_config.exists():
        root = read_toml(hp_config.read_text()).get("root")
        if root:
            return Path(root).expanduser()
    return home / ".herdr-projects"


def find_bin(cfg, home=Path.home()):
    if cfg.get("herdr_projects_bin"):
        return cfg["herdr_projects_bin"]
    found = shutil.which("herdr-projects")
    if found:
        return found
    builds = sorted(glob.glob(str(home / ".config/herdr/plugins/github/*herdr-projects*/target/release/herdr-projects")))
    if builds:
        return builds[-1]
    raise FileNotFoundError("herdr-projects not found: install it, or set herdr_projects_bin in the board's settings file")


def run(binary, root, *args, stdin=None):
    try:
        out = subprocess.run([binary, "--root", str(root), *args], input=stdin, capture_output=True, text=True, timeout=20)
    except FileNotFoundError:
        raise FileNotFoundError(f"herdr-projects not found at {binary}: fix herdr_projects_bin in the board's settings file")
    if out.returncode:
        raise RuntimeError((out.stderr or out.stdout).strip() or f"herdr-projects {' '.join(args)} failed")
    return out.stdout


# reading ---------------------------------------------------------------------
def active_projects(listing):
    """Slugs from `herdr-projects list` (tab-separated: slug, status, summary), archived ones left out."""
    slugs = []
    for line in listing.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1].strip() != "archived":
            slugs.append(parts[0].strip())
    return slugs


TASK = re.compile(r"^- \[ \] (?P<text>.*?)(?:\s+\((?P<owner>[^()]*)\))?(?:\s+·\s+(?P<thread>t-\d+))?\s*$")


def read_tasks(text):
    """Open tasks from TASKS.md as dicts: section, text, owner, thread, decision."""
    tasks, section = [], ""
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        m = TASK.match(line)
        if not m:
            continue
        body = m["text"].strip()
        decision = body.startswith("DECIDE:")
        tasks.append({"section": section, "text": body[7:].strip() if decision else body,
                      "owner": m["owner"] or "", "thread": m["thread"] or "", "decision": decision})
    return tasks


def _when(value):
    """A timestamp as an aware datetime (one without a zone is taken as UTC), or None."""
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError, TypeError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def build_columns(tasks, threads, cfg, slug="", now=None):
    now = now or datetime.now(timezone.utc)
    cols = {c: [] for c in COLUMNS}
    threads = [t for t in threads if isinstance(t, dict) and t.get("id")]
    shown = [t for t in threads if cfg["show_review_threads"] or t.get("kind") == "worktree"]
    open_ids = {t["id"] for t in threads if t.get("group_token") != "resolved"}

    for task in tasks:
        if task["decision"]:
            cols["Needs you"].append(Item(task["text"], "decision", "Decisions", group="Decisions", project=slug))
        elif task["owner"] == "me":
            cols["Needs you"].append(Item(task["text"], "task", "Your tasks", group="Your tasks", project=slug))
        elif task["thread"] not in open_ids:
            cols["To do"].append(Item(task["text"], "task", task["section"], group=task["section"], project=slug))

    shipped = []
    for t in shown:
        group, link = t.get("group_token", ""), t.get("pr") or ""
        title = str(t.get("title") or t["id"])
        when = _when(t.get("last_state_change") or t.get("updated") or "")
        age = (now - when).total_seconds() if when else None
        if group == "resolved":
            if t.get("pr_state") == "MERGED" and when and (now - when).days <= cfg["shipped_days"]:
                shipped.append((when, Item(title, "shipped", label=when.strftime("%d %b"), link=link,
                                           thread=t["id"], project=slug, age=age)))
            continue
        item = Item(title, "thread", label=t["id"], link=link, pane=t.get("pane_id") or "",
                    thread=t["id"], project=slug, age=age)
        if group == "waiting-on-you":
            item.note, item.section, item.group = t.get("note") or "", "Waiting on you", "Waiting on you"
            cols["Needs you"].append(item)
            continue
        column = "In review" if group == "ready-for-review" or t.get("pr_state") == "OPEN" else "In progress"
        if column == "In progress" and t.get("activity") not in (None, "", "Done"):
            item.note = str(t["activity"])
        item.stale = age is not None and age > cfg["stale_hours"] * 3600
        cols[column].append(item)
    cols["Shipped"] = [item for _, item in sorted(shipped, key=lambda s: s[0], reverse=True)]
    cols["Needs you"].sort(key=lambda i: NEEDS_GROUPS.index(i.group))
    return cols


def is_backlog(section):
    return section.strip().lower() == "backlog"


def too_old(message):
    return "--json" in message or "unexpected argument" in message


def load_project(binary, root, slug, cfg):
    """One project; anything that goes wrong becomes the project's error message, never a crash."""
    path = root / slug
    project = Project(slug, slug, path)
    try:
        project_md = path / "PROJECT.md"
        if project_md.exists():
            parts = project_md.read_text().split("+++")
            if len(parts) >= 3:
                project.name = str(read_toml(parts[1]).get("name") or slug)
        threads = json.loads(run(binary, root, "thread", "list", slug, "--json") or "[]")
        if not isinstance(threads, list):
            raise ValueError("herdr-projects thread list --json did not return a list")
        tasks_md = path / "TASKS.md"
        tasks = read_tasks(tasks_md.read_text()) if tasks_md.exists() else []
        project.columns = build_columns(tasks, threads, cfg, slug)
    except RuntimeError as e:
        project.error = ("This herdr-projects is too old for the board, which needs `thread list --json`: update herdr-projects."
                         if too_old(str(e)) else str(e))
    except Exception as e:  # a plugin must show a broken project, not crash the board
        project.error = f"{type(e).__name__}: {e}"
    return project


def load_all(cfg):
    binary, root = find_bin(cfg), resolve_root(cfg)
    return [load_project(binary, root, slug, cfg) for slug in active_projects(run(binary, root, "list"))], root, time.time()


def all_projects(projects):
    """One view over every project: each column merged, every card and section naming its project."""
    merged = Project("", "All projects", Path("."))
    for pr in projects:
        for col in COLUMNS:
            for item in pr.columns[col]:
                if col == "Needs you":  # grouped by what you do, across projects; each card names its project
                    section, label = item.section, f"{pr.name} · {item.label}" if item.label else pr.name
                else:
                    section = f"{pr.name} · {item.section}" if item.section else pr.name if col == "To do" else ""
                    label = f"{pr.name} · {item.label}" if item.label else "" if section else pr.name
                merged.columns[col].append(replace(item, section=section, label=label))
        if pr.error:
            merged.error = merged.error or f"{pr.name}: {pr.error}"
    merged.columns["Shipped"].sort(key=lambda i: i.age if i.age is not None else float("inf"))
    merged.columns["Needs you"].sort(key=lambda i: NEEDS_GROUPS.index(i.group))
    return merged


def search(columns, query):
    """Columns keeping only cards whose text, note, label, section or project mention every word of query."""
    words = query.lower().split()
    if not words:
        return columns
    def hit(item):
        hay = " ".join((item.text, item.note, item.label, item.section, item.project)).lower()
        return all(w in hay for w in words)
    return {col: [i for i in items if hit(i)] for col, items in columns.items()}


# folding ---------------------------------------------------------------------
def fold_key(column, item):
    """Needs you groups fold everywhere at once; other sections fold per project and column."""
    if column == "Needs you":
        return f"Needs you|{item.group}"
    return f"{column}|{item.project}|{item.group}"


def is_folded(folds, key):
    """A section is open unless folded; Backlog sections start folded until you open one."""
    return folds.get(key, is_backlog(key.rsplit("|", 1)[-1]))


def rows(items, column, folds):
    """A column as rows you can select: ('section', key, heading, count, folded) above each section's cards,
    then ('card', index) for every card whose section is open."""
    out, last, hidden = [], None, False
    for i, item in enumerate(items):
        if item.section != last:
            last, hidden = item.section, False
            if item.section:
                key = fold_key(column, item)
                count = sum(1 for other in items if other.section == item.section)
                hidden = is_folded(folds, key)
                out.append(("section", key, item.section, count, hidden))
        if not hidden:
            out.append(("card", i))
    return out


EMPTY_STATE = {"sections": {}, "columns": [], "order": {}, "moves": [], "actions": []}
SENT_MOVES_EXPIRE = 15 * 60  # a sent move the coordinator has not applied by then stops being shown


def load_state(directory):
    """Folds, the board's own section order, the moves not yet applied and the actions not yet sent, kept between runs."""
    state = copy_state(EMPTY_STATE)
    try:
        data = json.loads((Path(directory) / "state.json").read_text())
    except (OSError, ValueError):
        return state
    if isinstance(data, dict):
        for key, empty in EMPTY_STATE.items():
            if isinstance(data.get(key), type(empty)):
                state[key] = data[key]
    return state


def save_state(directory, state):
    write_atomically(Path(directory) / "state.json", json.dumps(state, indent=1, sort_keys=True))


def copy_state(state):
    return json.loads(json.dumps(state))


SOURCE = "(from the projects board)"


# arranging -------------------------------------------------------------------
def sections_of(todo):
    """A To do column's sections, in the order they appear."""
    seen = []
    for item in todo:
        if item.group not in seen:
            seen.append(item.group)
    return seen


def order_sections(todo, order):
    """To do with sections in the board's own order (sections not in order keep their place after it)."""
    rank = {s: i for i, s in enumerate(order)}
    first = {s: i for i, s in enumerate(sections_of(todo))}
    return sorted(todo, key=lambda i: (rank.get(i.group, len(order)), first[i.group]))


def apply_move(todo, move):
    """Put the moved task where the move says: above its `before` task in `section`, or at the section's end.
    Returns False when the task is no longer in To do."""
    src = next((i for i, it in enumerate(todo) if it.text == move["text"]), None)
    if src is None:
        return False
    item = todo.pop(src)
    item.group = item.section = move["section"]
    item.moved = move.get("status", "pending")
    at = next((i for i, it in enumerate(todo) if move.get("before") and it.text == move["before"]
               and it.group == move["section"]), None)
    if at is None:
        last = max((i for i, it in enumerate(todo) if it.group == move["section"]), default=None)
        at = len(todo) if last is None else last + 1
    todo.insert(at, item)
    return True


def is_applied(todo, move):
    """Whether TASKS.md already has the task where the move puts it."""
    for i, it in enumerate(todo):
        if it.text == move["text"]:
            if it.group != move["section"]:
                return False
            nxt = next((o for o in todo[i + 1:] if o.group == move["section"]), None)
            return (nxt.text if nxt else None) == move.get("before")
    return False


def arrange(project, state, now=None):
    """The project as the board shows it: its sections in the board's order, the moves still waiting, and each
    pending action marked on its card. Sent moves the coordinator has applied (or that are too old), and actions
    whose card is gone, are dropped from state."""
    now = now or time.time()
    if project.error:  # nothing loaded: keep its moves for when it loads again
        return project
    fresh = list(project.columns["To do"])
    todo = [replace(i) for i in order_sections(fresh, state["order"].get(project.slug, []))]  # copies: moves edit them
    keep = []
    for move in state["moves"]:
        if move["project"] != project.slug:
            keep.append(move)
            continue
        if move.get("status") == "sent" and (is_applied(fresh, move) or now - move.get("sent_at", 0) > SENT_MOVES_EXPIRE):
            continue
        if apply_move(todo, move):
            keep.append(move)
    state["moves"] = keep
    columns, found = {}, []
    for col, items in dict(project.columns, **{"To do": todo}).items():
        columns[col] = []
        for item in items:
            action = find_action(state, item)
            if action:
                found.append(action)
                item = replace(item, pending=pending_label(action))
            columns[col].append(item)
    state["actions"] = [a for a in state["actions"] if a["project"] != project.slug or any(a is f for f in found)]
    project.columns = columns
    return project


def movable(project_todo, item):
    """Why a task cannot be moved, or '' when it can. Moves name tasks by their text, so it must be unique."""
    if item is None or item.kind != "task" or not item.group or item.group in NEEDS_GROUPS:
        return "Only tasks in a To do section can be moved"
    if sum(1 for it in project_todo if it.text == item.text) > 1:
        return "Two tasks share this text: rename one in TASKS.md to move it"
    return ""


def move_for(todo, item, step):
    """Where one step up (-1) or down (+1) takes item in its project's To do: (section, before), or None at an end.
    Up from a section's first task goes to the end of the section above; down from its last, to the top of the next."""
    mine = [it for it in todo if it.project == item.project]
    i = next(n for n, it in enumerate(mine) if it.text == item.text)
    if step < 0:
        if i == 0:
            return None
        prev = mine[i - 1]
        if prev.group == item.group:
            return item.group, prev.text
        return prev.group, None
    if i == len(mine) - 1:
        return None
    nxt = mine[i + 1]
    if nxt.group == item.group:
        after = mine[i + 2] if i + 2 < len(mine) and mine[i + 2].group == item.group else None
        return item.group, after.text if after else None
    return nxt.group, nxt.text


def record_move(state, project, move):
    """Store a task's move (replacing its earlier one), unless the other pending moves already leave the task
    exactly there: then TASKS.md needs nothing for it. Compared with TASKS.md plus the other moves, not
    TASKS.md alone, or a move next to another moved task could look like no move at all."""
    others = [m for m in state["moves"] if not (m["project"] == move["project"] and m["text"] == move["text"])]
    base_state = copy_state(dict(state, moves=others))
    base = arrange(Project(project.slug, project.name, project.path, dict(project.columns)), base_state)
    state["moves"] = others + ([move] if not is_applied(base.columns["To do"], move) else [])
    settle_moves(state, project)


def cancel_move(state, project, text):
    """Take back a task's pending move."""
    state["moves"] = [m for m in state["moves"] if not (m["project"] == project.slug and m["text"] == text
                                                         and m.get("status", "pending") == "pending")]
    settle_moves(state, project)


def settle_moves(state, project):
    """Moves that together put every task back where TASKS.md has it would ask the coordinator for nothing."""
    after = arrange(Project(project.slug, project.name, project.path, dict(project.columns)), copy_state(state))
    if task_order(after.columns["To do"]) == task_order(project.columns["To do"]):
        state["moves"] = [m for m in state["moves"] if m["project"] != project.slug or m.get("status") == "sent"]


def task_order(todo):
    """Each section's tasks in order; the sections' own order is the board's, so it does not count."""
    out = {}
    for item in todo:
        out.setdefault(item.group, []).append(item.text)
    return out


def move_sentence(moves):
    """One sentence for all of a project's pending moves."""
    def part(m):
        if m.get("before"):
            return f'move the task "{m["text"]}" to just above "{m["before"]}" in the "{m["section"]}" section'
        return f'move the task "{m["text"]}" to the end of the "{m["section"]}" section'
    parts = [part(m) for m in moves]
    if len(parts) == 1:
        return f"{SOURCE} Please {parts[0]} of TASKS.md."
    return f"{SOURCE} Please reorder TASKS.md: " + "; ".join(f"{n}) {p}" for n, p in enumerate(parts, 1)) + "."


# pending actions -------------------------------------------------------------
# An action waiting to be sent: {"project", "text", "kind", "thread"} name its card, "verb" is empty for an
# answer or a message, "typed" is the answer, message or note.
ACTION_LABELS = {"mark as done": "done", "delegate": "delegate", "drop": "drop"}


def pending_label(action):
    """How a pending action reads on its card."""
    return ACTION_LABELS.get(action.get("verb"), "answer" if action.get("kind") == "decision" else "message")


def find_action(state, item):
    """The action waiting for a card, or None."""
    return next((a for a in state["actions"] if (a["project"], a["text"], a.get("thread", ""))
                 == (item.project, item.text, item.thread)), None)


def record_action(state, action, queue=True):
    """A card's new action replaces its earlier one. Done, delegate and drop make the task's pending move moot,
    so it goes. With queue False the action was sent at once: it only clears what it replaces."""
    card = (action["project"], action["text"], action.get("thread", ""))
    state["actions"] = [a for a in state["actions"] if (a["project"], a["text"], a.get("thread", "")) != card]
    if action.get("verb"):
        state["moves"] = [m for m in state["moves"] if not (m["project"] == action["project"] and m["text"] == action["text"]
                                                             and m.get("status", "pending") == "pending")]
    if queue:
        state["actions"].append(action)


def action_sentence(action):
    item = Item(action["text"], action.get("kind", "task"), thread=action.get("thread", ""))
    if action.get("verb"):
        return task_sentence(action["verb"], item, action.get("typed", ""))
    return answer_sentence(item, action.get("typed", ""))


def batch_sentence(actions, moves):
    """One sentence for everything waiting for a project: its actions, then its moves. A single change reads
    exactly as it would sent on its own."""
    if not actions:
        return move_sentence(moves)
    parts = [action_sentence(a) for a in actions] + [move_sentence([m]) for m in moves]
    if len(parts) == 1:
        return parts[0]
    bare = [p[len(SOURCE) + 1:].rstrip(".") for p in parts]
    return f"{SOURCE} Please go through these in order: " + "; ".join(f"{n}) {p}" for n, p in enumerate(bare, 1)) + "."


def pending_line(kind, entry):
    """One line for an action or a move in the review."""
    if kind == "move":
        return f"move · {entry['text']}"
    typed = entry.get("typed", "")
    return f"{pending_label(entry)} · {entry['text']}" + (f": {typed}" if typed else "")


# actions ---------------------------------------------------------------------
def actions_for(project, item=None):
    """Menu entries for a card (or for the project when item is None): (label, kind, value, group, key).
    kind: 'link' opens a URL, 'copy' copies text, 'herdr'/'hp' run herdr / herdr-projects with value as args,
    'task' asks to send the task sentence for verb value (with an optional note), 'answer' asks for text first,
    'cancel' takes back the card's pending action (or else its pending move).
    group puts entries under a divider; key is the shortcut shown beside the entry."""
    acts = []
    if item and item.link:
        acts.append(("Open the pull request", "link", item.link, "Open", "⏎"))
    if item and item.thread and (project.path / "threads" / f"{item.thread}.md").exists():
        acts.append(("Read the thread's report", "hp", ["open-file", str(project.path / "threads" / f"{item.thread}.md")], "Open", ""))
    for label, rel in (("Open STATUS.md", "STATUS.md"), ("Open TASKS.md", "TASKS.md")):
        if (project.path / rel).exists() and (item is None or rel == "TASKS.md"):
            acts.append((label, "hp", ["open-file", str(project.path / rel)], "Open", "t" if rel == "TASKS.md" else ""))
    if item and item.pane:
        acts.append(("Jump to the thread", "herdr", ["agent", "focus", item.pane], "Go to", "g"))
    acts.append(("Jump to the coordinator", "hp", ["open", project.slug], "Go to", "" if item and item.pane else "g"))
    if item:
        acts.append(("Answer the decision…" if item.kind == "decision" else "Message the coordinator…", "answer", None,
                     "Talk to the coordinator", "a"))
    if item and item.kind == "task":
        acts += [("Mark done…", "task", "mark as done", "Talk to the coordinator", "x"),
                 ("Delegate…", "task", "delegate", "Talk to the coordinator", "d"),
                 ("Drop…", "task", "drop", "Talk to the coordinator", "D")]
    if item and item.kind == "task" and item.group and item.group not in NEEDS_GROUPS:
        acts.append(("Move…", "move", None, "Arrange", "␣"))
    if item and (item.pending or item.moved == "pending"):
        acts.append((f"Cancel pending: {item.pending or 'move'}", "cancel", None, "Pending", "⌫"))
    if item:
        acts.append(("Copy the text", "copy", item.text, "Copy", ""))
    if item and item.link:
        acts.append(("Copy the pull request link", "copy", item.link, "Copy", ""))
    return acts


def web_link(url):
    """Only http(s) links are handed to the system opener."""
    return isinstance(url, str) and re.match(r"^https?://[^\s]+$", url) is not None


def go_to(project, item):
    """What `g` does: the thread's pane when there is one, else the project's coordinator."""
    return next(a for a in actions_for(project, item) if a[3] == "Go to")


def task_sentence(verb, item, note=""):
    note = " ".join(note.split())
    return f'{SOURCE} Please {verb} the task "{item.text}" in TASKS.md.' + (f" Note: {note}" if note else "")


def answer_sentence(item, text):
    text = " ".join(text.split())
    if item.kind == "decision":
        return f'{SOURCE} My answer to the decision "{item.text}" in TASKS.md: {text}'
    if item.thread:
        return f'{SOURCE} About thread {item.thread} "{item.text}": {text}'
    return f'{SOURCE} About the task "{item.text}" in TASKS.md: {text}'


def send(cfg, slug, sentence):
    """Hand one sentence to a project's coordinator; herdr-projects delivers it when the coordinator is free."""
    run(find_bin(cfg), resolve_root(cfg), "coordinator", "prompt", slug, "--text-file", "-", stdin=sentence)


# text layout -----------------------------------------------------------------
def cells(text):
    """Terminal cells a string takes: wide characters (CJK, most emoji) take two."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 0 if unicodedata.combining(ch) else 1 for ch in text)


def clip(text, width):
    """The longest prefix of text that fits in width cells."""
    out, used = "", 0
    for ch in text:
        w = cells(ch)
        if used + w > width:
            break
        out, used = out + ch, used + w
    return out


def fit_label(label, width):
    """A card's label in width cells: when too long, the project name before " · " is shortened first, so the
    thread id or date after it stays whole."""
    if cells(label) <= width or " · " not in label:
        return clip(label, width)
    head, tail = label.rsplit(" · ", 1)
    room = width - cells(tail) - 4  # " · " and "…"
    return clip(head, room) + "… · " + tail if room >= 2 else clip(tail, width)


def wrap(text, width, max_lines):
    """Text as at most max_lines lines of at most width cells, the last one ending in … when cut."""
    width = max(4, width)
    lines, line = [], ""
    for word in text.split():
        while cells(word) > width:  # a word longer than the line is split
            room = width - cells(line) - (1 if line else 0)
            if room < 2:
                lines.append(line)
                line, room = "", width
            head = clip(word, room)
            lines.append((line + " " + head).strip())
            line, word = "", word[len(head):]
        candidate = (line + " " + word).strip()
        if cells(candidate) <= width:
            line = candidate
        else:
            lines.append(line)
            line = word
    lines.append(line)
    lines = [l for l in lines if l] or [""]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = clip(lines[-1], width - 1).rstrip() + "…"
    return lines


def wrap_runs(runs, width):
    """Styled text as lines: runs is [(text, style)], each line [(word-or-space, style)], at most width cells.
    Lets the answer box show the fixed part of a sentence and what was typed in one flowing paragraph."""
    words = [(w, style) for text, style in runs for w in text.split()]
    lines, line, used = [], [], 0
    for word, style in words:
        while cells(word) > width:
            room = width - used - (1 if line else 0)
            if room < 2:
                lines.append(line)
                line, used, room = [], 0, width
            head = clip(word, room)
            line += ([(" ", style)] if line else []) + [(head, style)]
            lines.append(line)
            line, used, word = [], 0, word[len(head):]
        need = cells(word) + (1 if line else 0)
        if line and used + need > width:
            lines.append(line)
            line, used, need = [], 0, cells(word)
        line += ([(" ", style)] if line else []) + [(word, style)]
        used += need
    lines.append(line)
    return lines


def blocks(items, width, max_lines=3):
    """A column as drawable blocks: ('section', text) above the first card of each section, then
    ('card', index, lines, has_label) where the card's label, when it has one, is the first line."""
    out, last = [], None
    for i, item in enumerate(items):
        if item.section and item.section != last:
            out.append(("section", item.section))
        last = item.section
        lines = ([item.label] if item.label else []) + wrap(item.shown(), width, max_lines)
        out.append(("card", i, lines, bool(item.label)))
    return out


def age_text(seconds):
    if seconds is None:
        return ""
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def printable(text):
    """Text without control characters (a title could carry terminal escapes)."""
    return "".join(ch for ch in str(text) if unicodedata.category(ch) != "Cc")


def as_text(project, width=100):
    """The board as plain text: for `board --once`, bug reports and tests."""
    out = [f"{project.name}" + (f"  ({len(project.columns['Needs you'])} need you)" if project.columns["Needs you"] else "")]
    if project.error:
        out.append(f"error: {project.error}")
    for col in COLUMNS:
        items = project.columns[col]
        out.append(f"\n{col.upper()} ({len(items)})")
        for b in blocks(items, width - 4, 2):
            if b[0] == "section":
                out.append(f"  {b[1]}")
                continue
            item = items[b[1]]
            age = age_text(item.age) if item.kind == "thread" else ""
            label = " · ".join(x for x in (item.label, age, "untouched" if item.stale else "") if x)
            out.append(f"  - {item.shown()}" + (f"  [{label}]" if label else "") + (f"  {item.link}" if item.link else ""))
    return "\n".join(printable(line) for line in out)
