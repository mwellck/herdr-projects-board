#!/usr/bin/env python3
"""Record the README's visuals: a real herdr session, the board opening as its popup, a short captioned tour.

  python3 demo/record.py    writes docs/images/demo.gif and the stills beside it (needs herdr, agg, ffmpeg)

Nothing of yours shows: herdr runs with a throwaway HOME (no config, plugins or sessions of yours) and
without any HERDR_* variable of the session you run this from, so it cannot reach your herdr. Its
workspaces and the board's projects are made up (demo/make-demo.py), the panes run a bare shell with a
neutral prompt, and this checkout is linked as the throwaway herdr's only plugin, with the demo's settings.
Sends go to the demo's stand-in for herdr-projects, which ignores them. The herdr server is stopped and the
throwaway folders removed at the end.

The captions are terminal rows under herdr's, redrawn after each frame, so agg renders them in the same
font. DEMO_TMP sets where the throwaway folders go (default /tmp): herdr's socket path must stay short.
"""
import codecs
import fcntl
import importlib.util
import json
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_ID = "herdr-projects-board"
IMAGES = HERE.parent / "docs" / "images"
COLS, ROWS = 178, 46       # herdr's terminal; the captions add CAPTION_ROWS below it
CAPTION_ROWS = 3
AGG = ["agg", "--font-family", "Menlo", "--font-size", "15", "--line-height", "1.3",
       "--idle-time-limit", "60", "--last-frame-duration", "2"]  # pauses are capped here, so stills line up
MAX_PAUSE = 2.5
STILL_CROP = "crop=1212:708:322:107"  # the popup's inside, in pixels for the agg settings above
CTRL_B = "\x02"
KEY = {"tab": "\t", "enter": "\r", "esc": "\x1b", "space": " ", "down": "\x1b[B", "right": "\x1b[C"}
PANE = ("$ git log --oneline -3\n"  # what the website workspace's pane shows: made up
        "4f2a9c1 Hero section with the new illustration (wip)\n"
        "b81e07d Contact form with spam protection\n"
        "9c3d552 Blog index with tags\n"
        "$ npm run dev\n\n  ready on http://localhost:3000\n")
WORKSPACES = {"website": "website", "mobile": "mobile-app", "api": "api"}  # demo project slug: herdr workspace
AGENT_STATE = {"working": "working", "waiting-on-you": "blocked", "ready-for-review": "idle"}  # resolved: no pane
CAPTIONS = ["Every project, one popup", "Answer a decision", "Reprioritise, review, send", "Every action in one menu"]


def steps():
    """(seconds to wait first, what to do): keys, "text:" typed, "wait:TEXT" until it is drawn, "caption:N",
    "still:NAME" (a screenshot now), or "start" (the GIF's first frame)."""
    return [
        (0.3, "prefix:t"), (0.0, "wait:NEEDS YOU"), (0.0, "start"), (0.0, "caption:0"),  # opens on the board over herdr
        (2.2, "still:all-projects"),
        (0.0, "caption:1"), (0.5, "a"), (0.0, "wait:type your answer"), (0.4, "text:A toggle, as recommended"),
        (1.2, "still:answer"), (0.4, "tab"), (0.0, "wait:Added"),   # typed text is redrawn letter by letter: no wait
        (0.8, "caption:2"), (0.0, "tab"), (0.4, "tab"),     # Mobile app
        (0.5, "right"), (0.3, "down"), (0.3, "space"), (0.3, "down"), (0.3, "down"), (0.3, "space"),
        (0.5, "P"), (0.0, "wait:Pending changes"), (1.2, "still:review"), (0.0, "enter"), (0.0, "wait:Sent"),
        (0.8, "caption:3"), (0.0, "4"), (0.5, "m"), (0.0, "wait:TALK TO THE COORDINATOR"), (0.3, "j"), (0.3, "j"),
        (1.2, "still:menu"), (0.6, "esc"), (1.0, ""),   # ends on the board, so the loop never shows an empty herdr
    ]


def demo_threads():
    """(workspace, thread) for every thread of the demo projects that still has an agent pane."""
    spec = importlib.util.spec_from_file_location("make_demo", HERE / "make-demo.py")
    make_demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(make_demo)
    return [(WORKSPACES[slug], t) for slug, project in make_demo.PROJECTS.items() for t in project[2]
            if t["group_token"] in AGENT_STATE]


def caption_rows(current):
    """The caption strip: progress dots and the current caption, centred, in herdr's tokyo-night colours."""
    bg, accent, text = "31;35;53", "122;162;247", "192;202;245"
    blank = f"\x1b[48;2;{bg}m{' ' * COLS}"
    if current < 0:  # before the tour starts: an empty strip
        return "\x1b7" + "".join(f"\x1b[{ROWS + 1 + i};1H{blank}" for i in range(CAPTION_ROWS)) + "\x1b[0m\x1b8"
    dots = "  ".join("●" if i <= current else "○" for i in range(len(CAPTIONS)))
    label = CAPTIONS[current]
    pad = (COLS - len(dots) - 4 - len(label)) // 2
    line = (f"\x1b[48;2;{bg}m{' ' * pad}\x1b[38;2;{accent}m{dots}    "
            f"\x1b[1;38;2;{text}m{label}\x1b[22m{' ' * (COLS - pad - len(dots) - 4 - len(label))}")
    return "\x1b7" + "".join(f"\x1b[{ROWS + 1 + i};1H{r}" for i, r in enumerate([blank, line, blank])) + "\x1b[0m\x1b8"


def record(events, marks):
    """Run herdr on made-up workspaces in a pseudo-terminal, play steps(), and fill events and marks."""
    herdr = shutil.which("herdr") or sys.exit("herdr is not on PATH")
    base = os.environ.get("DEMO_TMP", "/tmp")
    home = Path(tempfile.mkdtemp(prefix="pbh-", dir=base))
    demo = Path(tempfile.mkdtemp(prefix="pbd-", dir=base))
    subprocess.run([sys.executable, str(HERE / "make-demo.py"), str(demo)], check=True, stdout=subprocess.DEVNULL)
    shell = home / "shell"  # a bare bash: no rc files, a neutral prompt, no macOS zsh notice
    shell.write_text("#!/bin/bash\nexport PS1='$ ' BASH_SILENCE_DEPRECATION_WARNING=1\nexec /bin/bash --noprofile --norc -i\n")
    shell.chmod(0o755)
    plugin_config = home / ".config" / "herdr" / "plugins" / "config" / PLUGIN_ID  # where herdr points the board
    plugin_config.mkdir(parents=True)
    shutil.copy(demo / "config" / "config.toml", plugin_config / "config.toml")
    (home / ".config" / "herdr" / "config.toml").write_text(f'''onboarding = false

[theme]
name = "tokyo-night"

[keys]
prefix = "ctrl+b"

[[keys.command]]
key = "prefix+t"
type = "plugin_action"
command = "{PLUGIN_ID}.open"
description = "projects board"
''')
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TERM": "xterm-256color",
           "SHELL": str(shell), "LANG": "en_US.UTF-8", "BASH_SILENCE_DEPRECATION_WARNING": "1"}

    def cli(*args):
        return subprocess.run([herdr, *args], env=env, capture_output=True, text=True, timeout=20)

    pid, fd = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
        os.chdir(home)
        os.execve(herdr, [herdr], env)
    start, shown = time.time(), 0.0
    decode = codecs.getincrementaldecoder("utf-8")("replace").decode  # a glyph can span two reads

    def pump(seconds):
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([fd], [], [], max(0, end - time.time()))
            if ready:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    return
                events.append([time.time() - start, decode(data)])

    try:
        pump(2.0)
        linked = cli("plugin", "link", str(HERE.parent))
        if linked.returncode:
            sys.exit(f"herdr plugin link failed: {linked.stderr.strip()}")
        for name in WORKSPACES.values():
            (home / "code" / name).mkdir(parents=True)
            cli("workspace", "create", "--cwd", str(home / "code" / name), "--label", name, "--no-focus")
        workspaces = json.loads(cli("workspace", "list").stdout)["result"]["workspaces"]
        website = next(w for w in workspaces if w["label"] == "website")
        first = next(w for w in workspaces if w["label"] not in WORKSPACES.values())
        for name, t in demo_threads():  # the board's open threads, as agents in herdr's sidebar
            space = next(w for w in workspaces if w["label"] == name)
            tab = json.loads(cli("tab", "create", "--workspace", space["workspace_id"], "--cwd",
                                 str(home / "code" / name), "--label", t["id"], "--no-focus").stdout)
            agent_pane = tab["result"]["root_pane"]["pane_id"]
            message = t["activity"] or t["note"] or "Ready for review"
            cli("pane", "report-agent", "--source", "demo", "--agent", "claude", "--state",
                AGENT_STATE[t["group_token"]], "--message", message, agent_pane)
            cli("pane", "report-metadata", "--source", "demo", "--agent", "claude", "--title",
                f'{t["id"]} · {t["title"]}', agent_pane)
        cli("workspace", "focus", website["workspace_id"])
        cli("workspace", "close", first["workspace_id"])
        panes = json.loads(cli("pane", "list").stdout)["result"]["panes"]
        target = next(p["pane_id"] for p in panes if p["workspace_id"] == website["workspace_id"])
        (home / "website.txt").write_text(PANE)
        cli("pane", "send-text", target, f"clear; cat {home / 'website.txt'}")
        cli("pane", "send-keys", target, "Enter")
        pump(1.0)
        for wait, action in steps():
            pump(wait)
            if action == "start":
                shown = time.time() - start  # everything before is folded into the GIF's first frame
            elif action.startswith("wait:"):
                seen, deadline = len(events), time.time() + 15
                while time.time() < deadline and action[5:] not in drawn(events[seen:]):
                    pump(0.1)
                if time.time() >= deadline:
                    sys.exit(f"the demo stopped: {action[5:]!r} never appeared")
            elif action.startswith(("caption:", "still:")):
                marks.append((time.time() - start, action))
            elif action.startswith("prefix:"):
                os.write(fd, (CTRL_B + action[7:]).encode())
            elif action.startswith("text:"):
                for ch in action[5:]:
                    os.write(fd, ch.encode())
                    pump(0.05)
            elif action:
                os.write(fd, KEY.get(action, action).encode())
        pump(0.3)
    finally:
        cli("server", "stop")
        try:
            os.kill(pid, 15)
        except OSError:
            pass
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(demo, ignore_errors=True)
    squeeze = squeezer([max(0.0, e[0] - shown) for e in events])
    for e in events:
        e[0] = squeeze(max(0.0, e[0] - shown))
    marks[:] = [(squeeze(max(0.0, t - shown)) - 0.01, m) for t, m in marks]  # before any frame drawn at the same time


def drawn(events):
    """The text in a run of outputs, without escape sequences: herdr can split a word across two writes."""
    return re.sub(r"\x1b(\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)|.)", "", "".join(e[1] for e in events))


def squeezer(times):
    """A time map that shortens every pause between outputs to MAX_PAUSE, for the outputs and the marks alike."""
    cuts, last = [], 0.0
    for t in sorted(times):
        if t - last > MAX_PAUSE:
            cuts.append((last + MAX_PAUSE, t - last - MAX_PAUSE))
        last = t
    return lambda t: t - sum(min(cut, max(0.0, t - at)) for at, cut in cuts)


COMPLETE = re.compile(r"\x1b(\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)|P[^\x1b]*\x1b\\|[^\[\]P])")


def write_cast(path, events, marks):
    """herdr's output with the caption strip redrawn after every frame, and whenever the caption changes."""
    captions = sorted((t, int(m[8:])) for t, m in marks if m.startswith("caption:"))
    frames = [(t, data) for t, data in events] + [(t, "") for t, _ in captions]
    lines, held = [json.dumps({"version": 2, "width": COLS, "height": ROWS + CAPTION_ROWS})], ""
    for t, data in sorted(frames, key=lambda f: f[0]):
        data, esc = held + data, (held + data).rfind("\x1b")
        held = ""
        if esc >= 0 and not COMPLETE.match(data, esc):  # a read can end inside an escape sequence: finish it first
            data, held = data[:esc], data[esc:]
        current = max([c for at, c in captions if at <= t], default=-1)
        lines.append(json.dumps([round(t, 4), "o", data + caption_rows(current)]))
    path.write_text("\n".join(lines) + "\n")


def main():
    for tool in ("agg", "ffmpeg", "ffprobe"):
        shutil.which(tool) or sys.exit(f"{tool} is not on PATH")
    events, marks = [], []
    record(events, marks)
    work = Path(tempfile.mkdtemp(prefix="pbr-"))
    try:
        cast, gif, frames = work / "board.cast", IMAGES / "demo.gif", work / "frames"
        write_cast(cast, events, marks)
        subprocess.run([*AGG, str(cast), str(gif)], check=True, capture_output=True)
        frames.mkdir()
        subprocess.run(["ffmpeg", "-loglevel", "fatal", "-i", str(gif), "-fps_mode", "passthrough",  # agg's GIFs repeat timestamps: harmless warnings
                        str(frames / "%04d.png")], check=True)
        times = [float(t) for t in subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "frame=pts_time", "-of", "csv=p=0",
             str(gif)], check=True, capture_output=True, text=True).stdout.split()]
        IMAGES.mkdir(parents=True, exist_ok=True)
        stills = [(at, mark[6:]) for at, mark in marks if mark.startswith("still:")]
        for at, name in stills:
            frame = max(i for i, t in enumerate(times) if t <= at) + 1  # the frame on screen at that moment
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(frames / f"{frame:04d}.png"),
                            "-vf", STILL_CROP, str(IMAGES / f"{name}.png")], check=True)
        print(f"wrote {gif} ({times[-1] + 2:.1f} s) and {len(stills)} stills beside it")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
