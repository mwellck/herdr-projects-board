import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
import board  # noqa: E402
import theme  # noqa: E402

FIXTURE = json.loads((HERE / "fixtures" / "thread-list.json").read_text())
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
CFG = dict(board.DEFAULTS)

TASKS = """# Tasks

## Decisions

- [ ] DECIDE: Pick a database. Recommendation: Postgres (me)
- [x] DECIDE: Already decided (me)

## Backlog

- [ ] Someday: a dark mode for the docs

## Stage 1

- [ ] Offline sync (claude-complex) · t-0001
- [ ] Rename settings keys (codex-fast)
- [ ] Look into Safari logouts
- [ ] Write the release notes (me)
- [ ] Old task whose thread is gone (claude) · t-0099
"""


def load_bin_board():
    loader = importlib.machinery.SourceFileLoader("bin_board", str(ROOT / "bin" / "board"))
    spec = importlib.util.spec_from_loader("bin_board", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def fake_herdr_projects(tmp, threads, list_out="alpha\tactive\tWorking: 1\nold\tarchived\t-\n", thread_err=""):
    """A stand-in herdr-projects: `list`, `thread list --json`, and `coordinator prompt` (recorded to a file)."""
    fake = Path(tmp) / "herdr-projects"
    (Path(tmp) / "threads.json").write_text(json.dumps(threads))
    (Path(tmp) / "list.txt").write_text(list_out)
    thread_cmd = f'echo "{thread_err}" >&2; exit 2' if thread_err else f'cat "{tmp}/threads.json"'
    fake.write_text(f"""#!/bin/sh
case "$3" in
  list) cat "{tmp}/list.txt" ;;
  thread) {thread_cmd} ;;
  coordinator) echo "$5" > "{tmp}/sent-to.txt"; cat > "{tmp}/sent.txt" ;;
esac
""")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return fake


def make_root(tmp):
    root = Path(tmp) / "root"
    (root / "alpha" / "threads").mkdir(parents=True)
    (root / "alpha" / "PROJECT.md").write_text('+++\nname = "Alpha"\n\n[[repos]]\npath = "/r"\n+++\n# Instructions\n')
    (root / "alpha" / "TASKS.md").write_text(TASKS)
    return root


class Tasks(unittest.TestCase):
    def test_parses_open_tasks_owners_threads_and_decisions(self):
        tasks = board.read_tasks(TASKS)
        self.assertEqual([t["text"] for t in tasks][:3],
                         ["Pick a database. Recommendation: Postgres", "Someday: a dark mode for the docs", "Offline sync"])
        self.assertTrue(tasks[0]["decision"])
        self.assertEqual((tasks[2]["owner"], tasks[2]["thread"]), ("claude-complex", "t-0001"))
        self.assertEqual(len(tasks), 7, "checked tasks are left out")


class Columns(unittest.TestCase):
    def cols(self, cfg=CFG, threads=FIXTURE):
        return board.build_columns(board.read_tasks(TASKS), threads, cfg, "alpha", now=NOW)

    def test_every_card_lands_in_its_column(self):
        cols = self.cols()
        self.assertEqual([(i.kind, i.text) for i in cols["Needs you"]],
                         [("decision", "Pick a database. Recommendation: Postgres"),
                          ("task", "Write the release notes"), ("thread", "Upgrade the build tool")])
        self.assertEqual(cols["Needs you"][2].note, "blocked")
        todo = [i.text for i in cols["To do"]]
        self.assertNotIn("Offline sync", todo, "a task its thread is running shows as the thread")
        self.assertIn("Old task whose thread is gone", todo)
        self.assertEqual(cols["To do"][0].section, "Backlog", "TASKS.md's order")
        self.assertEqual([i.group for i in cols["Needs you"]], ["Decisions", "Your tasks", "Waiting on you"])
        self.assertEqual([i.shown() for i in cols["In progress"]], ["Offline sync · Running tests"],
                         "reviewer tabs are hidden by default")
        self.assertEqual([i.link for i in cols["In review"]], ["https://github.com/example/app/pull/2"])
        self.assertEqual([i.label for i in cols["Shipped"]], ["09 Oct"])
        self.assertTrue(all(i.project == "alpha" for c in cols.values() for i in c))

    def test_ages_and_stale_threads(self):
        cols = self.cols()
        thread = cols["In progress"][0]
        self.assertEqual(board.age_text(thread.age), "1d")
        self.assertTrue(thread.stale, "26 hours untouched is past the 24-hour default")
        self.assertFalse(self.cols(dict(CFG, stale_hours=48))["In progress"][0].stale)

    def test_old_shipped_and_unmerged_are_left_out(self):
        threads = FIXTURE + [dict(FIXTURE[2], id="t-0006", last_state_change="2026-08-01T00:00:00Z"),
                             dict(FIXTURE[2], id="t-0007", pr_state="CLOSED")]
        self.assertEqual(len(self.cols(threads=threads)["Shipped"]), 1)

    def test_review_threads_only_when_asked(self):
        cols = self.cols(dict(CFG, show_review_threads=True))
        self.assertEqual(len(cols["In progress"]), 2)


class Views(unittest.TestCase):
    def projects(self):
        a = board.Project("alpha", "Alpha", Path("/a"), board.build_columns(board.read_tasks(TASKS), FIXTURE, CFG, "alpha", NOW))
        b = board.Project("beta", "Beta", Path("/b"), board.build_columns([], [], CFG, "beta", NOW))
        return [a, b]

    def test_all_projects_merges_and_names_the_project(self):
        merged = board.all_projects(self.projects())
        self.assertEqual(merged.name, "All projects")
        self.assertEqual((merged.columns["Needs you"][0].section, merged.columns["Needs you"][0].label), ("Decisions", "Alpha"),
                         "Needs you stays grouped by what to do, each card naming its project")
        self.assertEqual(merged.columns["To do"][0].section, "Alpha · Backlog")
        self.assertEqual(merged.columns["To do"][0].label, "", "the section already names the project")

    def test_search_matches_every_word_anywhere_on_the_card(self):
        cols = self.projects()[0].columns
        found = board.search(cols, "sync tests")
        self.assertEqual([i.text for c in found.values() for i in c], ["Offline sync"])
        self.assertEqual(sum(len(c) for c in board.search(cols, "alpha").values()),
                         sum(len(c) for c in cols.values()), "the project slug is searchable")
        self.assertIs(board.search(cols, "  "), cols)

    def test_plain_text_board(self):
        text = board.as_text(self.projects()[0])
        self.assertIn("Alpha  (3 need you)", text)
        self.assertIn("- Offline sync · Running tests  [t-0001 · 1d · untouched]", text)
        self.assertIn("  Decisions\n  - Pick a database", text)


class Actions(unittest.TestCase):
    def project(self, tmp):
        path = Path(tmp)
        (path / "threads").mkdir()
        (path / "threads" / "t-0002.md").write_text("report")
        (path / "TASKS.md").write_text(TASKS)
        (path / "STATUS.md").write_text("status")
        return board.Project("alpha", "Alpha", path)

    def labels(self, pr, item=None):
        return [a[0] for a in board.actions_for(pr, item)]

    def test_a_decision_goes_to_the_coordinator_and_can_be_answered(self):
        with tempfile.TemporaryDirectory() as tmp:
            pr = self.project(tmp)
            item = board.Item("Pick a database", "decision", "Decisions", group="Decisions")
            self.assertEqual(board.go_to(pr, item)[1:3], ("hp", ["open", "alpha"]))
            labels = self.labels(pr, item)
            self.assertIn("Answer the decision…", labels)
            self.assertNotIn("Mark done…", labels, "decisions are answered, not ticked off")

    def test_a_task_can_be_done_delegated_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            acts = {a[0]: a for a in board.actions_for(self.project(tmp), board.Item("Rename keys", "task"))}
            self.assertEqual(acts["Mark done…"][1:], ("task", "mark as done", "Talk to the coordinator", "x"))
            self.assertEqual(acts["Delegate…"][4], "d")
            self.assertEqual(acts["Drop…"][4], "D")

    def test_a_thread_menu_is_grouped_with_shortcuts(self):
        with tempfile.TemporaryDirectory() as tmp:
            pr = self.project(tmp)
            item = board.Item("Dark mode", "thread", label="t-0002", link="https://x/pull/2", pane="w1:p2", thread="t-0002")
            self.assertEqual(board.go_to(pr, item)[1:3], ("herdr", ["agent", "focus", "w1:p2"]))
            acts = board.actions_for(pr, item)
            groups = [a[3] for a in acts]
            self.assertEqual(groups, sorted(groups, key=["Open", "Go to", "Talk to the coordinator", "Copy"].index),
                             "entries come in group order, so each group gets one heading")
            keys = {a[0]: a[4] for a in acts}
            self.assertEqual((keys["Open the pull request"], keys["Jump to the thread"], keys["Message the coordinator…"]),
                             ("⏎", "g", "a"))
            self.assertEqual(keys["Jump to the coordinator"], "", "g goes to the thread here, so the coordinator has no key")
            self.assertIn("Read the thread's report", keys)

    def test_the_project_menu_lists_its_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.labels(self.project(tmp)), ["Open STATUS.md", "Open TASKS.md", "Jump to the coordinator"])

    def test_sentences(self):
        decision = board.Item("Pick a database", "decision")
        self.assertEqual(board.answer_sentence(decision, "  Postgres,\n as recommended "),
                         '(from the projects board) My answer to the decision "Pick a database" in TASKS.md: Postgres, as recommended')
        thread = board.Item("Dark mode", "thread", thread="t-0002")
        self.assertEqual(board.answer_sentence(thread, "hold the merge"),
                         '(from the projects board) About thread t-0002 "Dark mode": hold the merge')
        task = board.Item("Rename keys", "task")
        self.assertEqual(board.task_sentence("mark as done", task),
                         '(from the projects board) Please mark as done the task "Rename keys" in TASKS.md.')
        self.assertEqual(board.task_sentence("mark as done", task, " done in PR #12 "),
                         '(from the projects board) Please mark as done the task "Rename keys" in TASKS.md. Note: done in PR #12')

    def test_send_hands_the_sentence_to_the_coordinator(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = fake_herdr_projects(tmp, [])
            board.send(dict(CFG, root=tmp, herdr_projects_bin=str(fake)), "alpha", "hello coordinator")
            self.assertEqual((Path(tmp) / "sent.txt").read_text(), "hello coordinator")
            self.assertEqual((Path(tmp) / "sent-to.txt").read_text().strip(), "alpha")


class Folding(unittest.TestCase):
    def items(self):
        return [board.Item("later", section="Backlog", group="Backlog", project="p"),
                board.Item("one", section="Stage 1", group="Stage 1", project="p"),
                board.Item("two", section="Stage 1", group="Stage 1", project="p"),
                board.Item("a thread", "thread", label="t-0001", project="p")]

    def test_backlog_starts_folded_and_folds_hide_cards_not_headings(self):
        rows = board.rows(self.items(), "To do", {})
        self.assertEqual(rows, [("section", "To do|p|Backlog", "Backlog", 1, True),
                                ("section", "To do|p|Stage 1", "Stage 1", 2, False), ("card", 1), ("card", 2), ("card", 3)])
        rows = board.rows(self.items(), "To do", {"To do|p|Backlog": False, "To do|p|Stage 1": True})
        self.assertEqual([r[0] if r[0] == "section" else r[1] for r in rows], ["section", 0, "section", 3])

    def test_needs_you_groups_fold_across_projects(self):
        a = board.Item("x", "decision", "Decisions", group="Decisions", project="a")
        b = board.Item("y", "decision", "Decisions", group="Decisions", project="b")
        self.assertEqual(board.fold_key("Needs you", a), board.fold_key("Needs you", b))
        self.assertNotEqual(board.fold_key("To do", a), board.fold_key("To do", b))

    def test_state_round_trip_and_bad_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(board.load_state(tmp), board.EMPTY_STATE)
            state = dict(board.EMPTY_STATE, order={"p": ["Backlog"]}, moves=[{"project": "p", "text": "a", "section": "S", "before": None}])
            board.save_state(tmp, state)
            self.assertEqual(board.load_state(tmp), state)
            (Path(tmp) / "state.json").write_text('{"sections": "wrong type", "moves": []}')
            self.assertEqual(board.load_state(tmp), board.EMPTY_STATE)


class Moving(unittest.TestCase):
    def todo(self, project="p"):
        def item(text, section):
            return board.Item(text, "task", section, group=section, project=project)
        return [item("a", "Stage 1"), item("b", "Stage 1"), item("c", "Stage 2"), item("d", "Stage 2"), item("z", "Backlog")]

    def project(self, todo=None):
        pr = board.Project("p", "P", Path("/p"))
        pr.columns["To do"] = todo or self.todo()
        return pr

    def test_steps_cross_section_edges(self):
        todo = self.todo()
        self.assertIsNone(board.move_for(todo, todo[0], -1), "already at the top")
        self.assertEqual(board.move_for(todo, todo[0], 1), ("Stage 1", None), "below b, the end of Stage 1")
        self.assertEqual(board.move_for(todo, todo[1], 1), ("Stage 2", "c"), "into the top of the next section")
        self.assertEqual(board.move_for(todo, todo[2], -1), ("Stage 1", None), "into the end of the section above")
        other = self.todo("q")
        self.assertIsNone(board.move_for(todo + other, todo[4], 1), "never into another project")

    def test_moves_and_section_order_shape_the_column(self):
        state = dict(board.EMPTY_STATE, order={"p": ["Backlog"]},
                     moves=[{"project": "p", "text": "d", "section": "Stage 1", "before": "b"},
                            {"project": "p", "text": "gone", "section": "Stage 1", "before": None}])
        fresh = self.todo()
        pr = board.arrange(self.project(fresh), state)
        self.assertEqual([(i.group, i.text, i.moved) for i in pr.columns["To do"]],
                         [("Backlog", "z", ""), ("Stage 1", "a", ""), ("Stage 1", "d", "pending"),
                          ("Stage 1", "b", ""), ("Stage 2", "c", "")])
        self.assertEqual([m["text"] for m in state["moves"]], ["d"], "a move whose task left TASKS.md is dropped")
        self.assertEqual([i.group for i in fresh], ["Stage 1", "Stage 1", "Stage 2", "Stage 2", "Backlog"],
                         "the loaded tasks are not changed")

    def test_sent_moves_stay_until_applied_or_too_old(self):
        sent = {"project": "p", "text": "d", "section": "Stage 1", "before": "b", "status": "sent", "sent_at": 1000}
        state = dict(board.EMPTY_STATE, moves=[dict(sent)])
        board.arrange(self.project(), state, now=1000 + 60)
        self.assertEqual(len(state["moves"]), 1, "not applied yet: still shown")
        board.arrange(self.project(), state, now=1000 + board.SENT_MOVES_EXPIRE + 1)
        self.assertEqual(state["moves"], [], "too old")
        applied = self.todo()
        applied.insert(1, applied.pop(3))
        applied[1].group = applied[1].section = "Stage 1"
        state = dict(board.EMPTY_STATE, moves=[dict(sent)])
        board.arrange(self.project(applied), state, now=1000 + 60)
        self.assertEqual(state["moves"], [], "TASKS.md has it now")

    def test_a_task_next_to_a_moved_one_still_moves(self):
        # TASKS.md has r, f, o; r is moved down to just above o, so the board shows f, r, o. Moving f down
        # puts it just above o: where TASKS.md alone has it, but not with r's move, so it is a real move.
        # Together the two moves restore TASKS.md's order, so neither is left to send.
        def item(text):
            return board.Item(text, "task", "M2", group="M2", project="p")
        raw = board.Project("p", "P", Path("/p"))
        raw.columns["To do"] = [item("r"), item("f"), item("o")]
        state = dict(board.EMPTY_STATE, moves=[{"project": "p", "text": "r", "section": "M2", "before": "o", "status": "pending"}])

        def shown():
            copy = board.Project("p", "P", Path("/p"), dict(raw.columns))
            return [i.text for i in board.arrange(copy, json.loads(json.dumps(state))).columns["To do"]]

        self.assertEqual(shown(), ["f", "r", "o"])
        current = board.arrange(board.Project("p", "P", Path("/p"), dict(raw.columns)), json.loads(json.dumps(state))).columns["To do"]
        f = next(i for i in current if i.text == "f")
        section, before = board.move_for(current, f, 1)
        self.assertEqual((section, before), ("M2", "o"))
        board.record_move(state, raw, {"project": "p", "text": "f", "section": section, "before": before, "status": "pending"})
        self.assertEqual(shown(), ["r", "f", "o"], "f moved below r")
        self.assertEqual(state["moves"], [], "r, f, o is TASKS.md's own order: the two moves cancel out")

    def test_moves_that_still_change_the_order_are_kept(self):
        raw = self.project()
        state = dict(board.EMPTY_STATE)
        state["moves"] = []
        board.record_move(state, raw, {"project": "p", "text": "a", "section": "Stage 1", "before": None, "status": "pending"})
        board.record_move(state, raw, {"project": "p", "text": "c", "section": "Stage 2", "before": None, "status": "pending"})
        self.assertEqual([m["text"] for m in state["moves"]], ["a", "c"])

    def test_moving_back_to_the_file_order_drops_the_move(self):
        raw = self.project()
        state = dict(board.EMPTY_STATE, moves=[{"project": "p", "text": "a", "section": "Stage 1", "before": None, "status": "pending"}])
        board.record_move(state, raw, {"project": "p", "text": "a", "section": "Stage 1", "before": "b", "status": "pending"})
        self.assertEqual(state["moves"], [], "a is back above b, as in TASKS.md: nothing to send")

    def test_cancelling_a_move_also_drops_moves_left_with_nothing_to_do(self):
        raw = self.project()
        state = dict(board.EMPTY_STATE, moves=[{"project": "p", "text": "a", "section": "Stage 1", "before": None, "status": "pending"},
                                                {"project": "p", "text": "c", "section": "Stage 2", "before": None, "status": "pending"}])
        board.cancel_move(state, raw, "a")
        self.assertEqual([m["text"] for m in state["moves"]], ["c"])

    def test_one_sentence_per_project(self):
        one = [{"text": "d", "section": "Stage 1", "before": "b"}]
        self.assertEqual(board.move_sentence(one),
                         '(from the projects board) Please move the task "d" to just above "b" in the "Stage 1" section of TASKS.md.')
        two = one + [{"text": "z", "section": "Stage 2", "before": None}]
        self.assertEqual(board.move_sentence(two),
                         '(from the projects board) Please reorder TASKS.md: 1) move the task "d" to just above "b" in the '
                         '"Stage 1" section; 2) move the task "z" to the end of the "Stage 2" section.')


class Pending(unittest.TestCase):
    def test_a_single_change_reads_as_it_would_alone(self):
        done = {"project": "p", "text": "Rename keys", "kind": "task", "thread": "", "verb": "mark as done", "typed": "in #12"}
        self.assertEqual(board.batch_sentence([done], []), board.task_sentence("mark as done", board.Item("Rename keys"), "in #12"))
        move = {"text": "d", "section": "Stage 1", "before": "b"}
        self.assertEqual(board.batch_sentence([], [move]), board.move_sentence([move]))
        note = {"project": "p", "text": "Dark mode", "kind": "thread", "thread": "t-0002", "verb": "", "typed": "hold it."}
        self.assertEqual(board.batch_sentence([done, note], [move]),
                         '(from the projects board) Please go through these in order: '
                         '1) Please mark as done the task "Rename keys" in TASKS.md. Note: in #12; '
                         '2) About thread t-0002 "Dark mode": hold it; '
                         '3) Please move the task "d" to just above "b" in the "Stage 1" section of TASKS.md.')

    def test_actions_mark_their_card_and_go_with_it(self):
        pr = board.Project("p", "P", Path("/p"))
        pr.columns["To do"] = [board.Item("a", "task", "S", group="S", project="p")]
        pr.columns["In progress"] = [board.Item("t", "thread", thread="t-0001", project="p")]
        state = dict(board.EMPTY_STATE, actions=[
            {"project": "p", "text": "a", "kind": "task", "thread": "", "verb": "drop", "typed": ""},
            {"project": "p", "text": "t", "kind": "thread", "thread": "t-0001", "verb": "", "typed": "hi"},
            {"project": "p", "text": "gone", "kind": "task", "thread": "", "verb": "drop", "typed": ""},
            {"project": "q", "text": "other project", "kind": "task", "thread": "", "verb": "drop", "typed": ""}])
        shown = board.arrange(pr, state)
        self.assertEqual((shown.columns["To do"][0].pending, shown.columns["In progress"][0].pending), ("drop", "message"))
        self.assertEqual([a["text"] for a in state["actions"]], ["a", "t", "other project"], "a card that is gone takes its action")

    def test_a_new_action_replaces_the_cards_earlier_one(self):
        state = dict(board.EMPTY_STATE, moves=[{"project": "p", "text": "a", "section": "S", "before": None, "status": "pending"}])
        state["actions"] = []
        note = {"project": "p", "text": "a", "kind": "task", "thread": "", "verb": "", "typed": "hi"}
        board.record_action(state, note)
        self.assertEqual(len(state["moves"]), 1, "a message keeps the move")
        board.record_action(state, dict(note, verb="drop", typed=""))
        self.assertEqual([board.pending_label(a) for a in state["actions"]], ["drop"])
        self.assertEqual(state["moves"], [], "a dropped task's move is moot")
        board.record_action(state, dict(note, verb="delegate"), queue=False)
        self.assertEqual(state["actions"], [], "sent at once: it replaces the pending one")

    def test_the_menu_offers_to_cancel(self):
        pr = board.Project("p", "P", Path("/nonexistent"))
        acts = {a[0]: a for a in board.actions_for(pr, board.Item("a", "task", "S", group="S", pending="done"))}
        self.assertEqual(acts["Cancel pending: done"][1:], ("cancel", None, "Pending", "⌫"))
        acts = {a[0] for a in board.actions_for(pr, board.Item("a", "task", "S", group="S", moved="pending"))}
        self.assertIn("Cancel pending: move", acts)
        acts = {a[0] for a in board.actions_for(pr, board.Item("a", "task", "S", group="S", moved="sent"))}
        self.assertFalse(any(a.startswith("Cancel") for a in acts), "a sent move is the coordinator's now")


class Loading(unittest.TestCase):
    def test_root_order(self):
        with tempfile.TemporaryDirectory() as home:
            home = Path(home)
            self.assertEqual(board.resolve_root({}, env={}, home=home), home / ".herdr-projects")
            (home / ".config/herdr-projects").mkdir(parents=True)
            (home / ".config/herdr-projects/config.toml").write_text('root = "/from-config"\n\n[profiles.x]\nmodel = "m"\n')
            self.assertEqual(board.resolve_root({}, env={}, home=home), Path("/from-config"))
            self.assertEqual(board.resolve_root({}, env={"HERDR_PROJECTS_ROOT": "/from-env"}, home=home), Path("/from-env"))
            self.assertEqual(board.resolve_root({"root": "/from-board"}, env={"HERDR_PROJECTS_ROOT": "/x"}, home=home),
                             Path("/from-board"))

    def test_archived_projects_are_left_out(self):
        self.assertEqual(board.active_projects("alpha\tactive\tx\nold\tarchived\tx\nbeta\tpaused\tx\n"), ["alpha", "beta"])

    def test_load_all_with_a_fake_herdr_projects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            fake = fake_herdr_projects(tmp, FIXTURE)
            projects, found_root, _ = board.load_all(dict(CFG, root=str(root), herdr_projects_bin=str(fake)))
            self.assertEqual(found_root, root)
            self.assertEqual([p.name for p in projects], ["Alpha"])
            self.assertEqual(len(projects[0].columns["In progress"]), 1)
            self.assertEqual(projects[0].error, "")

    def test_an_old_herdr_projects_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            fake = fake_herdr_projects(tmp, [], thread_err="error: unexpected argument '--json' found")
            projects, _, _ = board.load_all(dict(CFG, root=str(root), herdr_projects_bin=str(fake)))
            self.assertIn("too old", projects[0].error)

    def test_a_missing_binary_names_the_setting(self):
        with self.assertRaises(FileNotFoundError) as e:
            board.load_all(dict(CFG, herdr_projects_bin="/nonexistent/herdr-projects"))
        self.assertIn("herdr_projects_bin", str(e.exception))

    def test_once_prints_the_board(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            fake = fake_herdr_projects(tmp, FIXTURE)
            cfg_dir = Path(tmp) / "cfg"
            board.save_config(cfg_dir, dict(CFG, root=str(root), herdr_projects_bin=str(fake)))
            out = subprocess.run([sys.executable, str(ROOT / "bin" / "board"), "--once"], capture_output=True, text=True,
                                 env=dict(os.environ, HERDR_PLUGIN_CONFIG_DIR=str(cfg_dir)), timeout=30)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertTrue(out.stdout.startswith("All projects  (3 need you)"))
            self.assertIn("\n\nAlpha  (3 need you)", out.stdout)


class Settings(unittest.TestCase):
    def test_saved_settings_load_back_and_defaults_are_left_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = dict(board.DEFAULTS, shipped_days=30, show_review_threads=True, palette="soft",
                       editor="nano", close_after_send=True)
            path = board.save_config(tmp, cfg)
            self.assertNotIn("refresh_seconds", path.read_text())
            self.assertEqual(board.load_config(tmp), cfg)


class Text(unittest.TestCase):
    def test_wide_characters_never_overflow_the_column(self):
        for text in ("修复登录页面的错误并添加测试 和文档", "Ship 🚀 the 🚀 rocket 🚀 release 🚀 today", "x" * 50):
            for line in board.wrap(text, 12, 5):
                self.assertLessEqual(board.cells(line), 12, line)
        self.assertEqual(board.cells("a修b"), 4)

    def test_blocks_name_sections_once_and_put_labels_first(self):
        items = [board.Item("Short task", section="Stage 1"), board.Item("A much longer task " * 6, section="Stage 1"),
                 board.Item("A thread", "thread", label="t-0004", link="https://github.com/o/r/pull/4")]
        out = board.blocks(items, width=20, max_lines=2)
        self.assertEqual([b[0] for b in out], ["section", "card", "card", "card"])
        self.assertEqual(out[0], ("section", "Stage 1"))
        self.assertTrue(out[2][2][-1].endswith("…"))
        self.assertEqual(out[3][2][0], "t-0004")
        self.assertTrue(out[3][3])

    def test_styled_runs_flow_together_and_fit(self):
        runs = [("(from the projects board) My answer to the decision \"Pick a database\":", "fixed"),
                ("Postgres as recommended " + "x" * 40, "typed")]
        lines = board.wrap_runs(runs, 24)
        for line in lines:
            self.assertLessEqual(sum(board.cells(w) for w, _ in line), 24)
        flat = [(w, s) for line in lines for w, s in line if w != " "]
        self.assertEqual([w for w, s in flat if s == "typed"][:3], ["Postgres", "as", "recommended"])
        self.assertTrue(any(s == "fixed" for _, s in lines[0]))
        self.assertEqual(board.wrap_runs([("", "typed")], 10), [[]])

    def test_editor_choices_start_with_herdrs_default(self):
        self.assertEqual(board.editor_choices()[0], "")

    def test_labels_keep_their_thread_id(self):
        self.assertEqual(board.fit_label("Website · t-0009", 30), "Website · t-0009")
        self.assertEqual(board.fit_label("Website · t-0009", 14), "Webs… · t-0009")
        self.assertEqual(board.fit_label("Website · t-0009", 7), "t-0009")
        self.assertEqual(board.fit_label("Decide", 3), "Dec")

    def test_fallback_toml_reader(self):
        real, board.tomllib = board.tomllib, None
        try:
            text = ('name = "Alpha"\ndays = 30 # note\nflag = true\npath = \'~/x\'\n'
                    '[theme]\nname = "gruvbox"\n[theme.custom]\naccent = "#ff0000"\n[[keys.command]]\nkey = "prefix+t"\n')
            self.assertEqual(board.read_toml(text), {"name": "Alpha", "days": 30, "flag": True, "path": "~/x",
                                                     "theme": {"name": "gruvbox", "custom": {"accent": "#ff0000"}}})
        finally:
            board.tomllib = real

    def test_mouse_reports(self):
        bb = load_bin_board()
        self.assertEqual(bb.parse_sgr("[<0;12;3M"), (0, 11, 2))
        self.assertEqual(bb.parse_sgr("[<65;1;1M"), (65, 0, 0))
        self.assertIsNone(bb.parse_sgr("[<0;12;3m"), "releases are ignored")
        self.assertIsNone(bb.parse_sgr("[A"))


class Theme(unittest.TestCase):
    def config(self, tmp, text):
        path = Path(tmp) / "config.toml"
        path.write_text(text)
        return path

    def test_builtin_theme_with_custom_colours(self):
        with tempfile.TemporaryDirectory() as tmp:
            pal = theme.herdr_palette(self.config(tmp, '[theme]\nname = "gruvbox"\n[theme.custom]\naccent = "rgb(1, 2, 3)"\nred = "reset"\n'))
            self.assertEqual(pal["accent"], (1, 2, 3))
            self.assertEqual(pal["red"], theme.rgb(theme.PALETTES["gruvbox"]["red"]), "reset keeps the theme's colour")

    def test_auto_switch_picks_the_light_theme_and_its_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.config(tmp, '[theme]\nname = "catppuccin"\nauto_switch = true\nlight_name = "tokyo-night-day"\n'
                                    '[theme.custom.light]\nblue = "#000080"\n')
            light = theme.herdr_palette(path, light=True)
            self.assertTrue(theme.is_light(light["base"]))
            self.assertEqual(light["blue"], (0, 0, 128))
            self.assertFalse(theme.is_light(theme.herdr_palette(path, light=False)["base"]))

    def test_terminal_and_unknown_themes_fall_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(theme.herdr_palette(self.config(tmp, '[theme]\nname = "terminal"\n')))
            self.assertIsNone(theme.herdr_palette(self.config(tmp, '[theme]\nname = "my-own"\n')))
            self.assertIsNotNone(theme.herdr_palette(Path(tmp) / "missing.toml"), "no config means herdr's default theme")

    def test_every_builtin_palette_is_complete_and_readable(self):
        for name, pal in theme.PALETTES.items():
            self.assertEqual(set(pal), set(theme.TOKENS), name)
            self.assertTrue(all(theme.rgb(v) for v in pal.values()), name)

    def test_herdr_config_path_follows_herdr(self):
        self.assertEqual(theme.herdr_config_path({"HERDR_CONFIG_PATH": "/x/c.toml", "XDG_CONFIG_HOME": "/xdg"}), Path("/x/c.toml"))
        self.assertEqual(theme.herdr_config_path({"XDG_CONFIG_HOME": "/xdg"}), Path("/xdg/herdr/config.toml"))
        self.assertEqual(theme.herdr_config_path({}, home="/h"), Path("/h/.config/herdr/config.toml"))

    def test_nearest_256(self):
        self.assertEqual(theme.nearest_256((0, 0, 0)), 16)
        self.assertEqual(theme.nearest_256((255, 255, 255)), 231)
        self.assertEqual(theme.nearest_256((128, 128, 128)), 244)


if __name__ == "__main__":
    unittest.main()


class Robustness(unittest.TestCase):
    def test_config_keeps_defaults_for_wrong_types_and_bad_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config.toml").write_text('stale_hours = "24"\nrefresh_seconds = 0\nshipped_days = 30\npalette = 3\n')
            cfg = board.load_config(tmp)
            self.assertEqual((cfg["stale_hours"], cfg["refresh_seconds"], cfg["shipped_days"], cfg["palette"]), (24, 5, 30, "herdr"))
            (Path(tmp) / "config.toml").write_text("this is [not toml")
            self.assertEqual(board.load_config(tmp), board.DEFAULTS)

    def test_save_config_keeps_comments_and_unknown_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config.toml").write_text("# mine\nshipped_days = 30\nfuture_key = 1\n")
            board.save_config(tmp, dict(board.DEFAULTS, stale_hours=12))
            text = (Path(tmp) / "config.toml").read_text()
            self.assertIn("# mine", text)
            self.assertIn("future_key = 1", text)
            self.assertIn("stale_hours = 12", text)
            self.assertNotIn("shipped_days", text, "back at its default, the line goes")

    def test_fallback_toml_reads_a_quoted_value_with_a_comment(self):
        real, board.tomllib = board.tomllib, None
        try:
            self.assertEqual(board.read_toml('name = "tokyo-night" # mine\nother = \'x\' # c\n'),
                             {"name": "tokyo-night", "other": "x"})
        finally:
            board.tomllib = real

    def test_odd_thread_data(self):
        threads = [{"id": "t-1", "group_token": "working", "kind": "worktree", "last_state_change": "2026-10-09T10:00:00"},
                   {"title": "no id"}, "not a dict"]
        cols = board.build_columns([], threads, CFG, "p", now=NOW)
        self.assertEqual([i.text for i in cols["In progress"]], ["t-1"], "no title: the id; no id or not a dict: skipped")
        self.assertIsNotNone(cols["In progress"][0].age, "a timestamp without a zone is read as UTC")

    def test_a_broken_project_becomes_its_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            (root / "alpha" / "PROJECT.md").write_text("+++\nname = [unclosed\n+++\n")
            fake = fake_herdr_projects(tmp, FIXTURE)
            projects, _, _ = board.load_all(dict(CFG, root=str(root), herdr_projects_bin=str(fake)))
            self.assertEqual(len(projects), 1, "a broken PROJECT.md never crashes the board")
            self.assertTrue(projects[0].error or projects[0].name == "alpha",
                            "an error (tomllib), or the slug as its name (the lenient reader on Python < 3.11)")
            (root / "alpha" / "PROJECT.md").write_text('+++\nname = "Alpha"\n+++\n')
            (Path(tmp) / "threads.json").write_text("not json")
            projects, _, _ = board.load_all(dict(CFG, root=str(root), herdr_projects_bin=str(fake)))
            self.assertIn("JSONDecodeError", projects[0].error)

    def test_moves_survive_a_project_that_failed_to_load(self):
        state = dict(board.EMPTY_STATE, moves=[{"project": "p", "text": "a", "section": "S", "before": None}])
        broken = board.Project("p", "P", Path("/p"), error="timed out")
        board.arrange(broken, state)
        self.assertEqual(len(state["moves"]), 1)

    def test_only_web_links_open(self):
        self.assertTrue(board.web_link("https://github.com/o/r/pull/1"))
        self.assertFalse(board.web_link("file:///etc/passwd"))
        self.assertFalse(board.web_link("-a Calculator"))

    def test_plain_text_drops_control_characters(self):
        pr = board.Project("p", "P\x1b[31m", Path("/p"))
        self.assertNotIn("\x1b", board.as_text(pr))

    def test_once_without_herdr_projects_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            board.save_config(tmp, dict(CFG, herdr_projects_bin="/nonexistent/herdr-projects"))
            out = subprocess.run([sys.executable, str(ROOT / "bin" / "board"), "--once"], capture_output=True, text=True,
                                 env=dict(os.environ, HERDR_PLUGIN_CONFIG_DIR=tmp), timeout=30)
            self.assertEqual(out.returncode, 1)
            self.assertIn("herdr-projects not found", out.stderr)
            self.assertNotIn("Traceback", out.stderr)


class FakeScreen:
    """Enough of a curses window for the board's key handling and drawing, without a terminal."""

    def __init__(self, keys=()):
        self.keys = list(keys)

    def getmaxyx(self):
        return 40, 160

    def get_wch(self):
        if not self.keys:
            import curses
            raise curses.error("no input")
        return self.keys.pop(0)

    def timeout(self, _):
        pass

    def nodelay(self, _):
        pass

    def __getattr__(self, name):  # drawing calls (erase, addstr, refresh…) do nothing
        return lambda *args, **kwargs: None


class Interactive(unittest.TestCase):
    """The board's keys and state, driven without a terminal."""

    def setUp(self):
        self.bb = load_bin_board()
        self.sent = []
        self.fail_for = set()

        def send(cfg, slug, sentence):
            if slug in self.fail_for:
                raise RuntimeError("coordinator not running")
            self.sent.append((slug, sentence))
        self._send, board.send = board.send, send
        self.projects = [self.project("alpha", "Alpha"), self.project("beta", "Beta")]
        self._load, board.load_all = board.load_all, lambda cfg: ([self.copy(p) for p in self.projects], Path("/r"), 0)

    def tearDown(self):
        board.send, board.load_all = self._send, self._load

    def project(self, slug, name):
        tasks = board.read_tasks(TASKS + "- [ ] One more task\n")
        pr = board.Project(slug, name, Path("/nonexistent") / slug, board.build_columns(tasks, [], CFG, slug, NOW))
        return pr

    def copy(self, pr):
        return board.Project(pr.slug, pr.name, pr.path, {c: list(v) for c, v in pr.columns.items()}, pr.error)

    def board(self, keys=(), **cfg):
        b = self.bb.Board(FakeScreen(keys), dict(CFG, **cfg), None, None)
        b.unget = lambda ch: b.s.keys.insert(0, ch)
        b.refresh_data()
        b.first_item()
        return b

    def select(self, b, project, text):
        b.tab = [p.slug for p in b.projects].index(project) + 1
        b.rebuild()
        for c in range(len(board.COLUMNS)):
            b.col = c
            for i, r in enumerate(b.rows()):
                if r[0] == "card" and b.items()[r[1]].text == text:
                    b.row = i
                    return
        self.fail(f"{text} not found")

    def test_enter_in_pasted_text_is_a_space_not_a_send(self):
        b = self.board(keys=["D", "o", "o", "p", "s", "\n"])
        self.select(b, "alpha", "Look into Safari logouts")
        b.ask_answer(b.selected())
        for ch in "hello":
            b.key(ch)
        b.key("\n")  # more keys are waiting: a paste
        self.assertEqual(self.sent, [])
        self.assertTrue(b.prompt)
        self.assertTrue(b.prompt["text"].startswith("hello "))
        b.pasting = True
        b.key("\n")
        self.assertEqual(self.sent, [], "inside a bracketed paste, Enter never sends")
        b.pasting, b.s.keys = False, []
        b.key("\n")
        self.assertEqual(len(self.sent), 1, "a typed Enter sends")

    def test_pasted_text_never_reaches_the_board_keys(self):
        b = self.board()
        self.select(b, "alpha", "Look into Safari logouts")
        b.pasting = True
        self.assertIsNone(b.key("q"))
        self.assertIsNone(b.prompt)
        self.assertIsNone(b.key("x"))
        self.assertIsNone(b.prompt, "x in a paste must not open the mark-done box")

    def test_moves_send_per_project_and_failures_stay_pending(self):
        b = self.board()
        for slug in ("alpha", "beta"):
            self.select(b, slug, "Rename settings keys")
            b.pick_up()
            b.step(1)
            b.drop()
        self.assertEqual(len(b.pending()), 2)
        self.fail_for = {"beta"}
        b.review = "open"
        b.key("\n")
        self.assertEqual([s for s, _ in self.sent], ["alpha"])
        self.assertEqual([m["project"] for m in b.pending()], ["beta"], "the failed project's move stays pending")
        self.assertIn("Not sent to Beta", b.flash)

    def test_discarding_moves_asks_first(self):
        b = self.board()
        self.select(b, "alpha", "Rename settings keys")
        b.pick_up(); b.step(1); b.drop()
        b.key("x"); b.key("\t")
        b.review = "open"
        b.key("X")
        self.assertEqual(len(b.changes()), 1, "X only asks")
        b.key("n")
        self.assertEqual((b.review, len(b.changes())), ("open", 1))
        b.key("X"); b.key("y")
        self.assertEqual((b.pending(), b.state["actions"]), ([], []))

    def test_tab_adds_to_pending_and_p_sends_one_sentence_per_project(self):
        b = self.board()
        self.select(b, "alpha", "Pick a database. Recommendation: Postgres")
        b.key("a")
        b.key("\t")
        self.assertEqual(b.state["actions"], [], "an empty answer is not added")
        for ch in "Postgres":
            b.key(ch)
        b.key("\t")
        self.select(b, "alpha", "Look into Safari logouts")
        b.key("x"); b.key("\t")
        self.select(b, "beta", "Look into Safari logouts")
        b.key("D"); b.key("\t")
        self.assertEqual(self.sent, [], "Tab never sends")
        self.assertEqual(b.selected().pending, "drop")
        self.assertEqual(b.card_label(b.selected()), "drop · pending")
        b.key("P")
        self.assertEqual(len(b.changes()), 3)
        b.key("\n")
        self.assertEqual([s for s, _ in self.sent], ["alpha", "beta"])
        self.assertEqual(self.sent[0][1], '(from the projects board) Please go through these in order: '
                         '1) My answer to the decision "Pick a database. Recommendation: Postgres" in TASKS.md: Postgres; '
                         '2) Please mark as done the task "Look into Safari logouts" in TASKS.md.')
        self.assertEqual(self.sent[1][1], '(from the projects board) Please drop the task "Look into Safari logouts" in TASKS.md.')
        self.assertEqual(b.state["actions"], [])

    def test_backspace_cancels_the_cards_action_then_its_move(self):
        b = self.board()
        self.select(b, "alpha", "Rename settings keys")
        b.pick_up(); b.step(1); b.drop()
        b.key("a")
        for ch in "after the beta":
            b.key(ch)
        b.key("\t")
        self.assertEqual(b.card_label(b.selected()), "moved · pending · message · pending")
        b.key("a")
        self.assertEqual(b.prompt["text"], "after the beta", "reopening the box edits what was typed")
        b.escape()
        b.key("\x7f")
        self.assertEqual((b.state["actions"], len(b.pending())), ([], 1), "the action goes first")
        b.key("\x7f")
        self.assertEqual(b.pending(), [])
        self.assertEqual(b.selected().moved, "")
        b.key("\x7f")
        self.assertIn("Nothing pending", b.flash)

    def test_done_replaces_the_move_and_blocks_moving(self):
        b = self.board()
        self.select(b, "alpha", "Rename settings keys")
        b.pick_up(); b.step(1); b.drop()
        b.key("x"); b.key("\t")
        self.assertEqual((b.pending(), b.selected().pending), ([], "done"), "a done task's move is moot")
        b.pick_up()
        self.assertIsNone(b.moving)
        self.assertIn("Backspace cancels it", b.flash)

    def test_sending_now_clears_the_cards_pending_action(self):
        b = self.board()
        self.select(b, "alpha", "Rename settings keys")
        b.key("x"); b.key("\t")
        b.key("d"); b.key("\n")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(b.state["actions"], [])

    def test_backspace_in_the_review_cancels_the_selected_change(self):
        b = self.board()
        self.plain_theme(b)
        for text in ("Rename settings keys", "Look into Safari logouts"):
            self.select(b, "alpha", text)
            b.key("x"); b.key("\t")
        self.select(b, "alpha", "One more task")
        b.pick_up(); b.step(-1); b.drop()
        b.key("P")
        b.draw()
        b.key("j"); b.key("\x7f")
        self.assertEqual([a["text"] for a in b.state["actions"]], ["Rename settings keys"])
        self.assertEqual(len(b.pending()), 1)
        b.key("j"); b.key("\x7f")
        self.assertEqual((b.review, len(b.pending())), ("open", 0), "the last line was selected: the move")
        b.key("\x7f")
        self.assertIsNone(b.review, "nothing left: the review closes")

    def test_close_after_send(self):
        b = self.board(close_after_send=True)
        self.select(b, "alpha", "Rename settings keys")
        b.key("x")
        self.assertEqual(b.key("\n"), "quit")
        self.assertEqual(len(self.sent), 1)

    def test_selection_follows_its_card_across_a_refresh(self):
        b = self.board()
        self.select(b, "alpha", "Look into Safari logouts")
        alpha = self.projects[0]
        alpha.columns["To do"].insert(0, board.Item("A brand new task", "task", "Stage 1", group="Stage 1", project="alpha"))
        b.refresh_data()
        self.assertEqual(b.selected().text, "Look into Safari logouts")

    def test_a_dropped_card_in_a_folded_section_selects_its_heading(self):
        b = self.board()
        self.select(b, "alpha", "Offline sync")  # the first task of Stage 1
        b.state["sections"]["To do|alpha|Backlog"] = True
        b.pick_up()
        b.step(-1)  # up past the top of Stage 1: into the end of Backlog, folded
        b.drop()
        r = b.current_row()
        self.assertEqual((r[0], r[1]), ("section", "To do|alpha|Backlog"))

    def plain_theme(self, b):
        b.theme = self.bb.Theme.__new__(self.bb.Theme)  # the no-colour theme: no terminal needed
        b.theme.name, b.theme.slots = "terminal", []
        b.theme.plain()

    def test_pending_moves_show_beside_the_tabs(self):
        b = self.board()
        self.plain_theme(b)
        self.select(b, "alpha", "Rename settings keys")
        b.pick_up(); b.step(1); b.drop()
        b.draw()
        self.assertIn((0, "review"), [(hit[0], hit[4]) for hit in b.hits], "the note is on the tabs row, and clickable")

    def test_the_settings_panel_draws(self):
        b = self.board()
        self.plain_theme(b)
        b.key("s")
        b.draw()  # Reset is an action with no value: sizing the value pills must skip it
        self.assertEqual(b.settings, 0)

    def test_a_folded_section_can_be_picked_up(self):
        b = self.board()
        self.select(b, "alpha", "Offline sync")
        b.row = next(i for i, r in enumerate(b.rows()) if r[0] == "section" and r[1] == "To do|alpha|Backlog")
        self.assertTrue(b.current_row()[4], "Backlog starts folded")
        b.pick_up()
        self.assertEqual((b.moving["kind"], b.moving["group"]), ("section", "Backlog"))

    def test_folded_columns_are_passed_over_and_inert(self):
        b = self.board()
        self.select(b, "alpha", "Look into Safari logouts")
        b.state["columns"] = ["In progress", "In review"]
        b.col = board.COLUMNS.index("To do")
        b.move(dcol=1)
        self.assertEqual(board.COLUMNS[b.col], "Shipped")
        b.col = board.COLUMNS.index("In progress")
        self.assertIsNone(b.selected())

    def test_what_cannot_be_moved_says_why(self):
        b = self.board()
        self.select(b, "alpha", "Write the release notes")  # a task of yours, in Needs you
        b.pick_up()
        self.assertIsNone(b.moving)
        self.select(b, "alpha", "Look into Safari logouts")
        b.query = "safari"
        b.pick_up()
        self.assertIsNone(b.moving)
        self.assertIn("search", b.flash)
        b.query = ""
        self.projects[0].columns["To do"].append(board.Item("Look into Safari logouts", "task", "Backlog", group="Backlog", project="alpha"))
        b.refresh_data()
        self.select(b, "alpha", "Look into Safari logouts")
        b.pick_up()
        self.assertIsNone(b.moving)
        self.assertIn("share this text", b.flash)

    def test_moves_of_a_project_no_longer_listed_are_dropped(self):
        b = self.board()
        self.select(b, "beta", "Rename settings keys")
        b.pick_up(); b.step(1); b.drop()
        self.projects.pop()  # beta archived
        b.refresh_data()
        self.assertEqual(b.pending(), [])

    def test_a_failed_refresh_keeps_moves(self):
        b = self.board()
        self.select(b, "alpha", "Rename settings keys")
        b.pick_up(); b.step(1); b.drop()
        board.load_all = lambda cfg: (_ for _ in ()).throw(subprocess.TimeoutExpired("herdr-projects", 20))
        b.refresh_data()
        self.assertIn("TimeoutExpired", b.error)
        self.assertEqual(len(b.pending()), 1)

    def wait_for_refresh(self, b):
        for _ in range(100):
            if not b.results.empty():
                return
            time.sleep(0.02)
        self.fail("the background refresh never finished")

    def test_background_refresh_waits_while_something_is_open(self):
        b = self.board()
        self.select(b, "alpha", "Look into Safari logouts")
        self.projects[0].columns["To do"].append(board.Item("Arrived later", "task", "Stage 1", group="Stage 1", project="alpha"))
        b.help = 0  # something open
        b.start_refresh(announce=True)
        self.wait_for_refresh(b)
        b.take_results()
        self.assertNotIn("Arrived later", [i.text for i in b.items()], "held while help is open")
        b.help = None
        b.take_results()
        self.assertIn("Arrived later", [i.text for i in b.items()])
        self.assertEqual(b.selected().text, "Look into Safari logouts", "the selection stays put")
        self.assertEqual(b.flash, "Refreshed")
        self.assertFalse(b.loading)

    def test_a_failed_background_refresh_shows_its_error(self):
        b = self.board()
        board.load_all = lambda cfg: (_ for _ in ()).throw(RuntimeError("herdr-projects crashed"))
        b.start_refresh()
        self.wait_for_refresh(b)
        b.take_results()
        self.assertEqual(b.error, "herdr-projects crashed")
        self.assertTrue(b.raw, "the last good data is kept")

