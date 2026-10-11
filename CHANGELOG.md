# Changelog

## 0.2.0 (2026-10-11)

- **Pending changes:** in the answer, mark done, delegate and drop boxes, Tab keeps the change pending instead of sending it. The card shows it, for example *done · pending*. `P` reviews every pending change, moves included, and sends each coordinator one message.
- **Cancelling:** Backspace on a card cancels its pending change; in the review, ↑/↓ picks a change and Backspace cancels it. Discard all is now `X`, since Backspace cancels one change.

## 0.1.0 (2026-10-11)

First release.

- **Board:**
  - a kanban popup over herdr-projects, with five columns: needs you, to do, in progress, in review, shipped;
  - an All projects tab, then one tab per project, each with a needs-you count;
  - how long each thread has been untouched, turning amber after a day.
- **Needs you in groups:** Decisions, Your tasks, Waiting on you.
- **Folding:** every section and column folds; folds are kept between runs; Backlog starts folded.
- **Arranging To do:** move sections (the board's own order, saved) and tasks (a priority change, so each project's moves go to its coordinator as one sentence after you review them, across projects at once).
- **Talking to the coordinator:** answer a decision, or send a note about any card. Mark a task done, delegate it or drop it, with an optional note. The board never edits your project files: each change goes to the coordinator as one sentence, shown before it is sent.
- **Card menus:** grouped as Open, Go to, Talk to the coordinator, Arrange and Copy, with each shortcut shown.
- **Keys and mouse:**
  - `?` help, `/` search, `1`–`9` to jump to a project, `g` to go to a thread or the coordinator;
  - click, scroll, and right-click menus.
- **Colours:** follows herdr's theme, including custom colours and auto light/dark, using exact colours where the terminal allows. It can also use its own soft palette or the terminal's 16 colours.
- **Settings panel:** grouped as General, Cards and Layout, with values aligned. Includes the editor for TASKS.md and reports, closing after a send, and resetting folds and section order. Changes are saved to the plugin's config dir.
- **Text mode:** `board --once` prints the board as plain text.
- **Requirements:** runs on Python 3.9 or later with the standard library only, on Linux and macOS.
- **Responsive:** data refreshes in the background; new data waits while a menu, a move or a box is open.
- **Robustness:** a project that fails to load shows its error instead of crashing the board, and keeps its pending moves; bad settings keep their defaults; pasted text never sends or acts as keys.
