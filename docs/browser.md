# The board in a browser

```bash
yamlboard serve
```

```
yamlboard 0.1.0 on http://localhost:7474
  board /home/you/my-repo/.yamlboard
  ctrl-c to stop
```

It binds `127.0.0.1:7474` and does not open a tab. Flags: `--host`, `--port`,
`--read-only`, `--no-git`, `--allow-public`, `-v`, `--json`. Binding a
non-loopback address is refused unless you pass `--read-only` or `--allow-public`,
because there is no authentication.

The server is a dashboard over the files, not their owner. Every write it makes
goes through the same Python layer the CLI does, so an agent claiming from a
terminal and a human dragging a card are the same code with different transports.
`--read-only` is honest end to end: the server refuses every POST, the payload
tells the client, and every write control renders disabled.

## What coordination looks like

The landing view is the classic board — same columns, same drag and drop.
Coordination arrives as state on cards, not as a new screen:

- **A claim chip** on each held card: who, and a countdown that ticks locally
  against the absolute `expires` the server sent, so it moves every second without
  a request every second. Under a quarter of its own TTL it goes amber; at zero it
  reads `expired` and stays there.
- **Overlap is loud.** A card whose claim collides with another live claim gets a
  red left edge, and a `Conflicts (N)` control appears in the toolbar listing each
  pair, the winner and a Resolve button. At zero conflicts it is gone entirely —
  not greyed, because a disabled control invites clicking.
- **Blocked cards** render dimmed with the ids they wait on, and a ready card gets
  an accent border, so "go get me" reads at a glance.
- **You claim as whoever the toolbar's agent select says** (`c` uses it too). It is
  a select over `people`, never free text, and it lives in `localStorage` — a pref,
  so two people can read one board as different agents.
- **Commit** stages `.yamlboard/` and nothing else, and refuses outright if your
  index already holds someone else's files — a human clicking Commit must not ship
  an agent's half-finished source edit.

## Keyboard

`n` new card · `/` search · `Enter` open the focused card · `c` claim the focused
card as the selected agent · `r` ready filter · `a` archive view · `b`/`l` board
or list view · `d` card density · `f` filters · `s` settings · `t` theme · `?` all
shortcuts · `Esc` close. Drag a card to move it; drop order is written to the file.

That list exists in three formats — the key handler in `app.js`, the help panel
(`?`) in the same file, and this line — and `test_every_shortcut_exists_in_the_handler_the_help_panel_and_the_readme`
fails if they drift. A shortcut nobody can discover is not one.

## There are no screenshots

The v1 board's PNGs were deleted rather than left as a picture of something that
no longer exists, and nothing in this repo generates them, so the demo store is
the tour: `cp -r examples/demo/.yamlboard ./ && yamlboard serve` shows the claims,
the ticking leases and the conflict control in a browser in about ten seconds.
CONVENTIONS states what happens if anyone adds images again.
