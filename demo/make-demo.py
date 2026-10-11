#!/usr/bin/env python3
"""Build a made-up herdr-projects setup for recording the README's GIF and screenshot.

  python3 demo/make-demo.py DIR    writes DIR/root (three projects), DIR/herdr-projects (a stand-in
                                   that answers the board's reads), and DIR/config (board settings)
Then run the board on it:  HERDR_PLUGIN_CONFIG_DIR=DIR/config HERDR_PLUGIN_STATE_DIR=DIR/state python3 bin/board
Nothing in it is real, and the stand-in never sends anything anywhere.
"""
import json
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

NOW = datetime.now(timezone.utc)


def ago(**kw):
    return (NOW - timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%SZ")


def thread(n, title, group, pr_state="", activity="", minutes=30, note=""):
    return {"id": f"t-{n:04d}", "title": title, "kind": "worktree", "group_token": group,
            "pr": f"https://github.com/example/app/pull/{n}" if pr_state else "", "pr_state": pr_state,
            "activity": activity, "note": note, "pane_id": f"w1:p{n}", "last_state_change": ago(minutes=minutes)}


PROJECTS = {
    "website": ("Website", """# Tasks

## Decisions

- [ ] DECIDE: Pricing page: monthly and yearly side by side, or a toggle? Recommendation: a toggle (me)

## Launch

- [ ] Hero section with the new illustration (claude-routine) · t-0012
- [ ] Pricing page with the plan comparison table (claude-complex)
- [ ] Customer logos strip, greyscale on hover
- [ ] Newsletter sign-up with double opt-in

## Polish

- [ ] Lighthouse score above 95 on mobile
- [ ] Dark mode for the blog

## Backlog

- [ ] Localise the landing page into French and German
- [ ] Animated product tour
""", [thread(12, "Hero section with the new illustration", "working", activity="Running visual checks", minutes=12),
      thread(9, "Contact form with spam protection", "ready-for-review", "OPEN", minutes=50),
      thread(7, "Blog index with tags", "resolved", "MERGED", minutes=60 * 20),
      thread(5, "New footer and sitemap", "resolved", "MERGED", minutes=60 * 50)]),
    "mobile": ("Mobile app", """# Tasks

## Decisions

- [ ] DECIDE: Offline mode: sync on launch only, or in the background? Recommendation: background, every 15 minutes (me)
- [ ] DECIDE: Minimum iOS version: 16 or 17? Recommendation: 16 (me)

## Release 2.0

- [ ] Offline sync for saved items (claude-complex) · t-0021
- [ ] Push notifications for replies
- [ ] Haptics on the main actions

## Backlog

- [ ] iPad layout
- [ ] Widgets for the home screen
""", [thread(21, "Offline sync for saved items", "working", activity="Writing conflict tests", minutes=60 * 30),
      thread(19, "Sign in with Apple", "waiting-on-you", note="needs the team ID"),
      thread(18, "Settings screen redesign", "ready-for-review", "OPEN", minutes=90),
      thread(15, "Faster cold start", "resolved", "MERGED", minutes=60 * 8)]),
    "api": ("API", """# Tasks

## Version 3

- [ ] Cursor pagination on every list endpoint (claude-routine) · t-0033
- [ ] Rate limits per API key
- [ ] Webhooks with signed payloads
- [ ] Write the release notes (me)

## Backlog

- [ ] GraphQL gateway
""", [thread(33, "Cursor pagination on every list endpoint", "working", activity="Updating clients", minutes=8),
      thread(31, "Idempotency keys for payments", "resolved", "MERGED", minutes=60 * 3),
      thread(29, "OpenAPI spec published", "resolved", "MERGED", minutes=60 * 26)]),
}


def main(out):
    out = Path(out)
    root = out / "root"
    threads = {}
    for slug, (name, tasks, project_threads) in PROJECTS.items():
        (root / slug / "threads").mkdir(parents=True, exist_ok=True)
        (root / slug / "PROJECT.md").write_text(f'+++\nname = "{name}"\n+++\n# Instructions\n')
        (root / slug / "TASKS.md").write_text(tasks)
        (root / slug / "STATUS.md").write_text(f"# {name}\n\nAll on track.\n")
        threads[slug] = project_threads
    (out / "threads.json").write_text(json.dumps(threads))
    fake = out / "herdr-projects"
    fake.write_text(f"""#!/bin/sh
# Answers the board's reads with the made-up projects; ignores everything else (nothing is sent).
case "$3" in
  list) printf 'website\\tactive\\t-\\nmobile\\tactive\\t-\\napi\\tactive\\t-\\n' ;;
  thread) python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))[sys.argv[2]]))' "{out}/threads.json" "$5" ;;
esac
""")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    (out / "config").mkdir(exist_ok=True)
    (out / "config" / "config.toml").write_text(f'root = "{root}"\nherdr_projects_bin = "{fake}"\npalette = "soft"\nrefresh_seconds = 30\n')
    print(f"demo ready in {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/projects-board-demo")
