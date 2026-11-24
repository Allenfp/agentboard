/* yamlboard -- the whole client.
 *
 * One rule: the browser never decides where a card lands. Every interaction
 * posts an op to /api/apply, and the returned document replaces ours. So drag
 * order, id allocation and validation live in exactly one place (Python), and a
 * hand-edited file can never disagree with what the UI thinks it wrote.
 */
"use strict";

const $ = (tag, attrs, ...kids) => {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "style" && typeof value === "object") applyStyle(node, value);
    else if (key.startsWith("on") && typeof value === "function")
      node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, String(value));
  }
  for (const kid of kids.flat(9)) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return node;
};

/**
 * `style.setProperty` is required for custom properties: assigning `--col`
 * straight onto a CSSStyleDeclaration is a no-op, and nearly every colour on a
 * card (column, label, priority, avatar) is passed that way. The sanctioned set
 * JS may set is `--col`, `--prio`, `--c`, `--who` and `--claim`; anything else a
 * component wants belongs in `styles.css` as a token, not on a node.
 */
function applyStyle(node, declarations) {
  for (const [prop, value] of Object.entries(declarations)) {
    if (value === null || value === undefined) continue;
    if (prop.startsWith("--")) node.style.setProperty(prop, value);
    else node.style[prop] = value;
  }
}

const PRIORITIES = ["urgent", "high", "med", "low", "none"];
const PRIORITY_RANK = { urgent: 0, high: 1, med: 2, low: 3, none: 4 };
const FALLBACK_COLOR = "#7c8797";
const DENSITIES = [["minimal", "Minimal"], ["compact", "Compact"], ["full", "Full"]];
const VIEWS = ["board", "list"];
const THEMES = ["light", "dark"];

/**
 * Browser-only prefs (P24). ``localStorage`` can be switched off entirely, and
 * whatever is already in it is a suggestion rather than a fact: a hand-edited
 * entry must never reach the DOM as ``data-density="banana"``, and a getter
 * that throws must never stop the app booting.
 */
function pref(key, fallback, allowed) {
  let value = null;
  try { value = localStorage.getItem(key); } catch { /* storage disabled: no prefs this session */ }
  return allowed.includes(value) ? value : fallback;
}

function savePref(key, value) {
  try { localStorage.setItem(key, value); } catch { /* storage disabled: prefs are forgotten */ }
}

const S = {
  state: null,          // the whole payload: {file, read_only, doc, ready, agents, conflicts, tokens, git}
  boardId: null,
  view: pref("yb.view", "board", VIEWS),
  swim: "none",
  density: pref("yb.density", "compact", DENSITIES.map(([value]) => value)),
  showArchived: false,
  filters: { q: "", label: "", assignee: "", priority: "" },
  busy: 0,
  openCardId: null,      // set while the card modal is open, so ops can repaint it
  pollFails: 0,          // consecutive polls the server did not answer -> offline cue
  agent: "",             // who this browser claims as; persisted, validated against people
  readyOnly: false,      // the `r` filter: only cards the server called ready
};

/* ---------------------------------------------------------------- icons */

const ICONS = {
  search: '<circle cx="7" cy="7" r="4.6"/><path d="M10.6 10.6 14 14"/>',
  filter: '<path d="M2.2 3h11.6L9.4 8.4v4.2L6.6 11V8.4z"/>',
  rows: '<rect x="2" y="3" width="12" height="10" rx="1.6"/><path d="M2 6.3h12M2 9.7h12"/>',
  display: '<path d="M2.5 3.2h11M2.5 6.4h11M2.5 9.6h7.6M2.5 12.8h4.4"/>',
  archive: '<rect x="2" y="3" width="12" height="3.4" rx="1"/><path d="M3.4 6.4v6a1 1 0 0 0 1 1h7.2a1 1 0 0 0 1-1v-6M6.4 9.4h3.2"/>',
  gear: '<path d="M2.4 5.4h4.4M11 5.4h2.6M2.4 10.6h2.6M8 10.6h5.6"/><circle cx="8.7" cy="5.4" r="1.9"/><circle cx="6.1" cy="10.6" r="1.9"/>',
  help: '<circle cx="8" cy="8" r="6.2"/><path d="M6.4 6.3a1.7 1.7 0 1 1 2.5 1.5c-.6.3-.9.7-.9 1.3"/><path d="M8 11.6h.01"/>',
  plus: '<path d="M8 3.2v9.6M3.2 8h9.6"/>',
  clock: '<circle cx="8" cy="8" r="6.2"/><path d="M8 4.6V8l2.4 1.6"/>',
  flag: '<path d="M4 2.2v11.6M4 3h7.4l-1.3 2.6L11.4 8H4"/>',
  link: '<path d="M6.6 9.4a2.8 2.8 0 0 0 4 0l2-2a2.83 2.83 0 1 0-4-4l-.9.9"/><path d="M9.4 6.6a2.8 2.8 0 0 0-4 0l-2 2a2.83 2.83 0 1 0 4 4l.9-.9"/>',
  check: '<path d="M3.2 8.4 6.4 11.6l6.4-7"/>',
  chat: '<path d="M13.6 9.2a1.8 1.8 0 0 1-1.8 1.8H5.4L2.4 13.6V4.2a1.8 1.8 0 0 1 1.8-1.8h7.6a1.8 1.8 0 0 1 1.8 1.8z"/>',
  board: '<rect x="2" y="3" width="3.6" height="10" rx="1.1"/><rect x="6.2" y="3" width="3.6" height="7" rx="1.1"/><rect x="10.4" y="3" width="3.6" height="4.6" rx="1.1"/>',
  list: '<path d="M5.4 4h8.4M5.4 8h8.4M5.4 12h8.4M2.4 4h.01M2.4 8h.01M2.4 12h.01"/>',
  moon: '<path d="M13.4 9.8A5.9 5.9 0 0 1 5.9 2.4a5.6 5.6 0 1 0 7.5 7.4z"/>',
  sun: '<circle cx="8" cy="8" r="3.1"/><path d="M8 1.7v1.7M8 12.6v1.7M1.7 8h1.7M12.6 8h1.7M3.5 3.5l1.2 1.2M11.3 11.3l1.2 1.2M12.5 3.5l-1.2 1.2M4.7 11.3l-1.2 1.2"/>',
};

/** Inline stroke icon; inherits currentColor, so themes come free. */
function icon(name, extra) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("class", "i" + (extra ? " " + extra : ""));
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML = ICONS[name] || "";
  return svg;
}

/* ------------------------------------------------------------------ transport */

/**
 * Fetch with a deadline. A wedged server (a git status inside the payload that
 * never returns) otherwise leaves the request pending forever, the busy dot lit
 * forever, and `sendQueue` jammed behind it - the tab looks alive and is not.
 */
const FETCH_TIMEOUT = 10000;

async function request(method, path, body) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT);
  try {
    const response = await fetch(path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });
    const data = await response.json().catch(() => ({ ok: false, error: "unreadable response" }));
    return { status: response.status, data };
  } catch (error) {
    if (error.name === "AbortError") throw new Error(`no answer within ${FETCH_TIMEOUT / 1000}s`);
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Ops queue instead of overlapping. The `tokens` map is read only once the
 * previous op has been answered, so two drops inside one second cannot both post
 * the same view of a card and blame the loser for the winner's edit.
 */
let sendQueue = Promise.resolve();

/** Send one op; adopt the returned document, or roll back to the truth on 409. */
async function send(op, args, label) {
  // null, not the toast node: callers ask `if (await send(...))` whether the
  // write landed, and a truthy node there would make them report a save the
  // read-only server refused.
  if (S.state?.read_only) { toast("This server is read-only.", "warn"); return null; }
  const turn = sendQueue.then(async () => {
    S.busy++;
    paintBusy();
    try {
      const { status, data } = await request("POST", "/api/apply",
        { op, args, tokens: S.state.tokens || {} });
      // A 200 without a document is not a success: a proxy or a half-written
      // reply would otherwise replace the board with undefined.
      if (status === 200 && data?.state) {
        adopt(data.state, { repaintCard: true });
        return data.result;
      }
      if (data.claim_denied) {
        // Deliberately not a reload: nothing moved under us, another agent simply
        // holds what we asked for. The reply carries no document on purpose, and
        // its own lines already end with the next command worth running.
        denyToast(data, label);
      } else if (data.conflict && data.state) {
        // The server's document is ours now, cards it deleted included, so the
        // open modal has to be reconciled here rather than left on a ghost.
        adopt(data.state, { repaintCard: true });
        toast(`${label || op} was not applied: ${(data.paths || []).join(", ") || "the board"} `
          + "changed on disk, so it reloaded.", "warn", 8000);
      } else if (data.errors) {
        toast(`${label || op} rejected: ${data.errors.join("; ")}`, "error", 8000);
      } else {
        toast(`${label || op} failed: ${data.error || status}`, "error", 6000);
      }
    } catch (error) {
      toast(`Could not reach the server (${error.message}).`, "error", 6000);
    } finally {
      S.busy--;
      paintBusy();
    }
    return null;
  });
  sendQueue = turn.catch(() => null);   // a failed op must never jam the queue
  return turn;
}

/**
 * Replace our document with the server's; reports what actually moved, per file.
 *
 * There is no board-level stamp any more, and that is the point: with several
 * agents renewing leases, "the file changed" is almost always true and therefore
 * worthless. So the diff is per card and split by kind — a claim moving on a card
 * nobody has open repaints that one tile and says nothing, while a card changing
 * column, appearing, vanishing, or having its text edited still earns a rebuild.
 * A blanket rebuild on every poll would throw away the sentence the person is
 * partway through selecting, every three seconds, forever.
 */
function adopt(state, { repaintCard = false } = {}) {
  const before = S.state;
  // Before any chip is built: a countdown measured against an unmeasured skew
  // renders a wrong number for a frame and then jumps.
  syncLeaseClock(state);
  S.state = state;
  if (!S.boardId || !findBoard(S.boardId)) S.boardId = boardList()[0]?.id || null;

  const news = movedFiles(before, state);
  if (!before || news.layout || news.added.length || news.gone.length
      || news.content.length || news.rebuild || repaintCard) render();
  else if (news.claims.length && !paintClaimed(news.claims)) render();
  else paintGit();
  // An op run from inside the card modal (comment, subtask) must repaint it from
  // the server's document, otherwise the modal shows a card that no longer exists.
  if (repaintCard) reloadOpenCard();
  noteExpiries(state);
  return news;
}

/**
 * Poll the store. A token that moved while no op of ours was in flight is
 * somebody else's change — the CLI, a hand edit in Vim, a `git pull` — and the
 * toast names the cards, because with one file per card "the file" no longer
 * identifies anything the reader could act on.
 */
async function refresh({ silent = true } = {}) {
  let reached = false;
  try {
    const { status, data } = await request("GET", "/api/state");
    reached = status === 200;
    if (!reached) {
      if (!silent) toast(`Could not load the board: ${data.error}`, "error");
      return;
    }
    const news = adopt(data);
    if (modalOpen()) return;
    const churn = [...news.added, ...news.gone];
    const named = (ids, verb) => {
      const rest = ids.length - 2;
      return `${ids.slice(0, 2).join(", ")}${rest > 0 ? ` and ${rest} more` : ""} ${verb}`;
    };
    if (news.layout) toast("board.yaml changed on disk — board reloaded.", "info", 5000);
    // A card appearing or vanishing is somebody running the CLI or a `git pull`;
    // naming the ids says which. "the board reloaded" alone hides that a card the
    // human was reading is simply no longer there.
    else if (churn.length) toast(named(churn, "were added or removed — board reloaded."), "info", 5000);
    else if (news.content.length) toast(named(news.content, "changed on disk — reloaded."), "info", 5000);
    // A claim-only move stays silent here: noteExpiries says the one thing about
    // it worth interrupting for, and N agents' renewals would otherwise put a
    // toast on this screen every three seconds.
  } catch (error) {
    if (!silent) toast(`Could not reach the server (${error.message}).`, "error");
  } finally {
    S.pollFails = reached ? 0 : S.pollFails + 1;
    paintOffline();
  }
}

const cardsById = (state) => new Map(payloadCards(state).map((card) => [card.id, card]));

/**
 * A card's own bytes, minus its lease and minus everything the server derives.
 *
 * `state`, `live`, `overlaps` and `open_blockers` are computed per payload, so
 * one lease expiring moves them on the card that expired *and* on every card that
 * was waiting on it. Compared verbatim they read as four cards' worth of human
 * edits, which is four "changed on disk" toasts for an event that already gets a
 * better one. What is left here is the part a person typed.
 */
const DERIVED = ["claim", "state", "live", "overlaps", "open_blockers"];
function ownBytes(card) {
  if (!card) return card;
  const rest = { ...card };
  for (const key of DERIVED) delete rest[key];
  // `assignee` is a content field a person can set, but claiming writes it too,
  // so a difference there is not evidence of an edit on its own: movedFiles
  // consults assigneeChange() and counts only a genuinely hand-edited name.
  delete rest.assignee;
  return rest;
}

/**
 * Which way `assignee` moved between two payloads of one card, if it moved.
 *
 * `claim` writes `assignee` and `release` clears it, so a lease moving also moves
 * that field: coordination traffic wearing a content field's clothes. Compared
 * verbatim it reads as a person editing the card, and N agents' claims would put
 * "changed on disk" on this screen every poll. The move is lease-driven only if
 * `assignee` agrees with each side's own claim on BOTH sides -- claim sets it to
 * the holder, release clears it with the claim gone. A name that matches nobody's
 * lease (assignee grigory while ada holds it) is a hand-edit and still counts.
 */
function assigneeChange(before, now) {
  const was = before?.assignee ?? null;
  const is = now?.assignee ?? null;
  if (was === is) return "same";
  const agrees = (c) => (c?.assignee ?? null) === (c?.claim?.agent ?? null);
  return agrees(before) && agrees(now) ? "lease" : "edit";
}

/**
 * Which files moved, and what kind of moving it was.
 *
 * `tokens` is one entry per file (`"board"` plus every card id), which is the
 * only cheap signal that bytes changed; the payload's cards say whether the
 * change was a lease or something the human wrote. A token can move with no card
 * difference and vice versa — a `renew` that rewrote one line, or a card that
 * moved board and so changed its position without changing its own bytes — so
 * both halves are consulted and the coarser answer wins.
 */
function movedFiles(before, state) {
  const news = { layout: false, content: [], claims: [], added: [], gone: [], rebuild: false };
  if (!before) return { ...news, layout: true };
  const was = cardsById(before);
  const now = cardsById(state);
  // Columns, labels, people, board list: anything that restructures the grid.
  news.layout = (before.tokens?.board || "") !== (state.tokens?.board || "");
  for (const [id, card] of now) {
    const old = was.get(id);
    if (!old) { news.added.push(id); continue; }        // a card appeared
    const tokenMoved = (before.tokens?.[id] || "") !== (state.tokens?.[id] || "");
    if (!tokenMoved) continue;
    const restSame = JSON.stringify(ownBytes(old)) === JSON.stringify(ownBytes(card));
    const who = assigneeChange(old, card);
    if (who === "lease") { news.claims.push(id); news.rebuild = true; continue; }
    (restSame && who === "same" ? news.claims : news.content).push(id);
  }
  for (const id of was.keys()) if (!now.has(id)) news.gone.push(id);   // deleted or archived
  /* Nothing on this path wrote a card: `ready` and `conflicts` are computed per
     payload, so a lease simply running out moves them with no byte changed. That
     changes which tiles belong on screen — a freed card needs its ready border,
     a blocked one loses its note — which a per-chip patch cannot express, so it
     is a rebuild. It stays a rebuild and not "content": expiry is coordination
     traffic, and it earns "ada's lease expired", never "changed on disk". */
  const reshaped = JSON.stringify(before.conflicts) !== JSON.stringify(state.conflicts)
    || JSON.stringify(before.ready) !== JSON.stringify(state.ready);
  if (reshaped) news.rebuild = true;
  news.content = [...new Set(news.content)];
  return news;
}

/* -------------------------------------------------------------------- lookups */

const doc = () => S.state?.doc || { boards: [], people: [], labels: [] };
const boardList = () => doc().boards || [];
const findBoard = (id) => boardList().find((b) => b.id === id) || null;
const board = () => findBoard(S.boardId);
const people = () => doc().people || [];
const labels = () => doc().labels || [];
const person = (id) => people().find((p) => p.id === id) || null;
const label = (id) => labels().find((l) => l.id === id) || null;
const columns = (b) => (b && b.columns) || [];
const column = (b, id) => columns(b).find((c) => c.id === id) || null;
const allCards = (b) => (b && b.cards) || [];
const cardById = (id) => {
  for (const b of boardList()) {
    const found = allCards(b).find((c) => c.id === id);
    if (found) return found;
  }
  return null;
};
const customFields = (b) => (b && b.custom_fields) || [];

function visibleCards(b, columnId) {
  const { q, label: labelId, assignee, priority } = S.filters;
  const needle = q.trim().toLowerCase();
  return allCards(b).filter((card) => {
    if (card.column !== columnId) return false;
    if (!!card.archived !== !!S.showArchived) return false;
    // The server's list, not a local guess: a card can look unclaimed here and
    // still be blocked or colliding, and offering it is how two agents end up in
    // one file. `state === "ready"` and membership of `ready` are the same fact.
    if (S.readyOnly && !readyIds().has(card.id)) return false;
    if (labelId && !(card.labels || []).includes(labelId)) return false;
    if (assignee && (card.assignee || "") !== assignee) return false;
    if (priority && (card.priority || "none") !== priority) return false;
    if (needle) {
      const haystack = [
        card.id, card.title, card.desc, (card.labels || []).join(" "),
        (card.subtasks || []).map((s) => s.text).join(" "),
        (card.comments || []).map((c) => c.text).join(" "),
        Object.values(card.fields || {}).join(" "),
      ].join(" ").toLowerCase();
      if (!haystack.includes(needle)) return false;
    }
    return true;
  });
}

const filtersActive = () =>
  !!S.filters.q || !!S.filters.label || !!S.filters.assignee || !!S.filters.priority || S.readyOnly;

const readOnly = () => !!S.state?.read_only;

/* ---------------------------------------------------------------------- theme */

/** Paint the theme; ``persist`` is off for the boot guess, which is not a choice. */
function applyTheme(name, { persist = true } = {}) {
  document.documentElement.dataset.theme = name;
  if (persist) savePref("yb.theme", name);
  if (els.theme) {
    els.theme.textContent = "";
    els.theme.append(icon(name === "dark" ? "sun" : "moon"));
    els.theme.title = name === "dark" ? "Switch to light (t)" : "Switch to dark (t)";
  }
}

/** "" while the person never picked a theme, which is the only case worth following. */
const storedTheme = () => pref("yb.theme", "", THEMES);

/** Follow the OS until the person picks a theme; a stored choice is final. */
function followSystemTheme() {
  const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
  mq?.addEventListener?.("change", (event) => {
    if (storedTheme() === "") applyTheme(event.matches ? "dark" : "light", { persist: false });
  });
  return mq?.matches ? "dark" : "light";
}

/* ---------------------------------------------------------------------- chrome */

let els = {};

function buildShell() {
  const root = document.getElementById("root");
  root.textContent = "";
  root.hidden = false;   // index.html hides it so nothing shows before the shell exists

  els.boardSelect = $("select", {
    class: "board-picker", title: "Switch board", "aria-label": "Board",
    onchange: (event) => {
      const value = event.target.value;
      if (value === "__new") { openSettings("boards"); els.boardSelect.value = S.boardId || ""; return; }
      S.boardId = value;
      S.showArchived = false;
      render();
    },
  });

  els.search = $("input", {
    type: "search", placeholder: "Search cards", "aria-label": "Search cards",
    value: S.filters.q,
    oninput: (event) => { S.filters.q = event.target.value; renderBoard(); paintFilterState(); },
  });

  els.labelFilter = filterSelect("Label", (value) => { S.filters.label = value; renderBoard(); paintFilterState(); });
  els.assigneeFilter = filterSelect("Who", (value) => { S.filters.assignee = value; renderBoard(); paintFilterState(); });
  els.priorityFilter = filterSelect("Priority", (value) => { S.filters.priority = value; renderBoard(); paintFilterState(); });

  els.filterBadge = $("span", { class: "badge", hidden: "" });
  els.filterButton = $("button", {
    class: "ghost icon", title: "Filter by label, person or priority (f)", "aria-label": "Filters",
    onclick: (event) => togglePop(event.currentTarget, filterPop()),
  }, icon("filter"), els.filterBadge);

  els.rowsButton = $("button", {
    class: "ghost icon", title: "Group the board into swimlane rows", "aria-label": "Swimlane rows",
    onclick: (event) => togglePop(event.currentTarget, choicePop("Rows",
      [["none", "No rows"], ["assignee", "Assignee"], ["label", "Label"], ["priority", "Priority"]],
      () => S.swim,
      (value) => { S.swim = value; renderBoard(); paintViewButtons(); })),
  }, icon("rows"));

  els.displayButton = $("button", {
    class: "ghost icon", title: "How much each card shows (d)", "aria-label": "Card detail",
    onclick: (event) => togglePop(event.currentTarget, choicePop("Cards show", DENSITIES,
      () => S.density,
      setDensity,
      "Minimal: title, labels, who.  Compact adds points and the due date.  Full adds the start of the notes.")),
  }, icon("display"));

  els.archiveToggle = $("button", {
    class: "ghost icon", title: "Show archived cards instead (a)", "aria-label": "Archive",
    onclick: () => { S.showArchived = !S.showArchived; render(); },
  }, icon("archive"));

  /* Overlaps git cannot see. Built hidden: paintCoordination() puts it in the
     DOM only when there is something to report, because a greyed-out button
     invites a click to find out why while an absent one states the board is
     clean — which is the only thing this control is for. */
  els.conflictBadge = $("span", { class: "badge", id: "conflicts-badge", hidden: "" });
  els.conflictButton = $("button", {
    class: "ghost conflicts", "aria-label": "Path conflicts between agents", hidden: "",
    onclick: openConflicts,
  }, icon("link"), $("span", {}, "Conflicts"), els.conflictBadge);

  /* Which name `c` claims as. A select over `people` and never a text box: an
     identity the file has never heard of is a hard error downstream, so the
     control cannot offer an answer the server will refuse. */
  els.agentSelect = $("select", {
    id: "agent-select", class: "agent-picker",
    title: "Who this browser claims cards as — press c on a focused card",
    "aria-label": "Claim cards as (the agent c will use)",
    onchange: (event) => { S.agent = event.target.value; savePref("yb.agent", S.agent); },
  });
  els.agentWrap = $("label", { class: "agent-wrap" },
    $("span", { class: "agent-key" }, "as"), els.agentSelect);

  els.commitButton = $("button", {
    class: "commit", title: "Commit the board files to git",
    onclick: openCommitDialog,
  }, "Commit");

  els.theme = $("button", {
    class: "ghost icon", "aria-label": "Theme",
    onclick: () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"),
  }, icon(document.documentElement.dataset.theme === "dark" ? "sun" : "moon"));

  els.newBoard = $("button", { class: "ghost icon", title: "New board", "aria-label": "New board", onclick: openNewBoardDialog }, icon("plus"));
  els.settings = $("button", { class: "ghost icon", title: "Columns, labels, people, fields (s)", "aria-label": "Settings", onclick: () => openSettings() }, icon("gear"));
  els.help = $("button", { class: "ghost icon", title: "Keyboard shortcuts (?)", "aria-label": "Shortcuts", onclick: openHelp }, icon("help"));
  els.busy = $("span", { class: "busy", title: "Waiting for the server", role: "status" });
  els.offline = $("span", { class: "offline", hidden: "", title: "The last polls did not reach the server" }, "offline");
  els.file = $("span", { class: "file", title: "The file being edited" });
  els.subtitle = $("p", { class: "subtitle" });
  els.main = $("main", { class: "main", id: "main", "data-density": S.density });
  els.viewToggle = segmented(
    [["board", "Board"], ["list", "List"]],
    () => S.view,
    (value) => { S.view = value; savePref("yb.view", value); render(); },
    (value) => icon(value === "board" ? "board" : "list"),
  );

  els.subtitleTail = $("span", { class: "note" });
  /* Owns the footer counts, so paintCoordination() can repaint them without
     touching els.file, which render() rewrites wholesale on every pass. */
  els.footStats = $("span", { class: "note foot-stats" });
  els.tools = $("div", { class: "tools" },
    els.filterButton, els.rowsButton, els.displayButton, els.conflictButton, els.archiveToggle);

  root.append(
    $("header", { class: "top" },
      $("div", { class: "brand" },
        $("span", { class: "mark", "aria-hidden": "true" }, icon("board")),
        $("div", {}, $("h1", {}, "yamlboard"), els.subtitle, els.subtitleTail),
      ),
      els.boardSelect,
      els.viewToggle.node,
      $("div", { class: "searchwrap" }, icon("search"), els.search, $("kbd", { "aria-hidden": "true" }, "/")),
      els.tools,
      $("div", { class: "tools right" },
        els.busy, els.offline, els.agentWrap, els.commitButton, els.newBoard, els.settings, els.help, els.theme,
      ),
    ),
    els.main,
    $("footer", { class: "foot" }, els.file, els.footStats),
  );
  paintViewButtons();
}

/**
 * A toolbar dropdown whose blank value means "no filter". Options are rebuilt by
 * syncFilters() on every render so a label or person added in Settings appears
 * without a reload, and so the control can never show a filter that is not
 * actually applied.
 */
function filterSelect(title, onchange) {
  return $("select", { title, "aria-label": title, onchange: (event) => onchange(event.target.value) });
}

/** Board/list control. Returns the repaint hook so paintViewButtons() can keep
 * the highlight honest when the view changes from the keyboard. */
function segmented(items, current, onPick, iconFor) {
  const wrap = $("div", { class: "segmented", role: "group" });
  const repaint = () => [...wrap.children].forEach((b, i) => b.classList.toggle("on", items[i][0] === current()));
  for (const [value, text] of items) {
    wrap.append($("button", {
      class: "seg", title: text, "aria-label": text,
      onclick: () => { onPick(value); repaint(); },
    }, iconFor ? iconFor(value) : text));
  }
  repaint();
  return { node: wrap, repaint };
}

/* ---- transient layers: one open at a time, dismissed by click-away or Escape ---- */

let activePop = null;
let popSeq = 0;

function closePop() {
  if (!activePop) return;
  const { anchor, node, scrim } = activePop;
  // Only hand focus back when it was inside the panel (or nowhere at all), so
  // clicking a control elsewhere is not dragged back to the toolbar.
  const ours = node.contains(document.activeElement) || document.activeElement === document.body;
  activePop = null;
  node.remove();
  scrim.remove();
  anchor.removeAttribute("aria-expanded");
  anchor.removeAttribute("aria-controls");
  if (ours && anchor.isConnected) anchor.focus();
}

function togglePop(anchor, contents) {
  const was = activePop;
  closePop();
  if (was && was.anchor === anchor) return;   // same button: this click closes it
  const id = `pop-${++popSeq}`;
  const title = contents.querySelector?.(".pop-title")?.textContent || "";
  const node = $("div", { class: "pop open", role: "dialog", id, "aria-label": title }, contents);
  const scrim = $("div", { class: "menu-scrim", onclick: closePop });
  document.body.append(scrim, node);
  const box = anchor.getBoundingClientRect();
  node.style.top = `${box.bottom + 6}px`;
  node.style.left = `${Math.max(8, Math.min(box.left, window.innerWidth - node.offsetWidth - 10))}px`;
  anchor.setAttribute("aria-expanded", "true");
  anchor.setAttribute("aria-controls", id);
  node.querySelector("button, select, input")?.focus();
  activePop = { anchor, node, scrim };
}

/** The filter panel: three dropdowns plus a clear, so "what is filtering me"
 * has one answer instead of three loose boxes in the toolbar. */
function filterPop() {
  return $("div", {},
    $("div", { class: "pop-group" },
      $("div", { class: "pop-title" }, "Filter cards"),
      $("label", { class: "pop-row" }, $("span", {}, "Label"), els.labelFilter),
      $("label", { class: "pop-row" }, $("span", {}, "Person"), els.assigneeFilter),
      $("label", { class: "pop-row" }, $("span", {}, "Priority"), els.priorityFilter),
    ),
    $("div", { class: "pop-foot" }, $("button", { class: "ghost", onclick: clearFilters }, "Clear all")),
  );
}

function clearFilters() {
  S.filters = { q: "", label: "", assignee: "", priority: "" };
  els.search.value = "";
  syncFilters();
  paintFilterState();
  renderBoard();
  closePop();
}

/** A titled either/or list, used by Rows and Cards-show. */
function choicePop(title, options, get, set, hint) {
  const buttons = options.map(([value, text]) => $("button", {
    class: "menu-item", onclick: () => { set(value); closePop(); },
  }, text));
  const wrap = $("div", {}, $("div", { class: "pop-group" },
    $("div", { class: "pop-title" }, title), ...buttons));
  if (hint) wrap.append($("p", { class: "pop-hint" }, hint));
  activeRadio = { buttons, options, get };
  paintRadio();
  return wrap;
}

let activeRadio = null;

function paintRadio() {
  if (!activeRadio) return;
  const { buttons, options, get } = activeRadio;
  const value = get();
  buttons.forEach((button, i) => button.classList.toggle("on", options[i][0] === value));
}

/**
 * Repopulate the dropdowns from S.filters; the single place values are set.
 *
 * Each write-back is the value the select actually settled on: a label deleted
 * in Settings is no longer an option, and a filter the control cannot show must
 * not stay applied behind it — otherwise the board empties and the dropdown
 * says "Any label" at the same time.
 */
function syncFilters() {
  S.filters.label = fillSelect(els.labelFilter, [["", "Any label"], ...labels().map((l) => [l.id, l.name])], S.filters.label);
  S.filters.assignee = fillSelect(els.assigneeFilter, [["", "Anyone"], ...people().map((p) => [p.id, p.name])], S.filters.assignee);
  S.filters.priority = fillSelect(els.priorityFilter, [["", "Any priority"], ...PRIORITIES.map((p) => [p, p])], S.filters.priority);
}

/** Fill a select and return the value it ended up with. */
function fillSelect(select, options, value) {
  select.textContent = "";
  for (const [optionValue, text] of options) select.append($("option", { value: optionValue }, text));
  select.value = options.some(([v]) => v === value) ? value : options[0][0];
  return select.value;
}

/**
 * Which cards the server called ready — its `ready` array and nothing else.
 * "Ready" means unclaimed *and* unblocked *and* path-free, three files' worth of
 * judgement; a browser that computed its own would offer work an agent cannot
 * start, which is the exact collision this whole system exists to prevent.
 */
const readyIds = () => new Set(S.state?.ready || []);

/** The toolbar reads as state: which controls are doing something right now. */
function paintFilterState() {
  if (!els.filterBadge) return;
  const n = Object.values(S.filters).filter(Boolean).length;
  els.filterBadge.hidden = n === 0;
  els.filterBadge.textContent = String(n);
  els.filterButton.classList.toggle("on", n > 0);
}

/** The toolbar is state made visible, so every path through render() repaints it. */
function paintViewButtons() {
  if (!els.rowsButton) return;
  els.rowsButton.classList.toggle("on", S.swim !== "none");
  els.displayButton.classList.toggle("on", S.density !== "compact");
  els.archiveToggle.classList.toggle("on", S.showArchived);
  els.viewToggle?.repaint();
  paintRadio();
}

/**
 * Repaint the tiles whose only change was somebody else's lease, without a rebuild.
 *
 * Returns false when a tile is not on screen, so the caller falls back to
 * render(). It has to be allowed to fail: an expired claim moves a card into the
 * `ready` set, and under the ready filter a card that just became claimable has to
 * appear, which patching one chip cannot do.
 */
function paintClaimed(ids) {
  let all = true;
  for (const id of ids) {
    const tile = els.main?.querySelector(`.card[data-id="${CSS.escape(id)}"]`);
    const card = cardById(id);
    if (!tile || !card) { all = false; continue; }
    tile.classList.toggle("claimed", !!card.live);
    tile.classList.toggle("overlaps", !!card.overlaps);
    tile.classList.toggle("ready-look", readyIds().has(id));
    const foot = tile.querySelector(".card-foot");
    const fresh = claimNode(card);
    if (!foot || !fresh) { all = false; continue; }
    const chip = foot.querySelector(".claim");
    if (chip) chip.replaceWith(fresh);
    else foot.append(fresh);
    if (claimColor(card)) tile.style.setProperty("--claim", claimColor(card));
    else tile.style.removeProperty("--claim");
  }
  paintCoordination();
  return all;
}

function setDensity(value) {
  S.density = value;
  savePref("yb.density", value);
  if (els.main) els.main.dataset.density = value;
  paintViewButtons();
}

/** A quiet board and an unreachable server look the same; this is the difference. */
function paintOffline() {
  if (!els.offline) return;
  els.offline.hidden = S.pollFails < 2;
}

function paintBusy() {
  if (!els.busy) return;
  els.busy.classList.toggle("on", S.busy > 0);
}

/* ---------------------------------------------------------------------- render */

function render() {
  if (!S.state) return;
  document.getElementById("boot")?.remove();
  if (!els.main) buildShell();

  els.boardSelect.textContent = "";
  if (!boardList().length) {
    els.boardSelect.append($("option", { value: "" }, "no boards yet"));
  } else {
    for (const b of boardList()) els.boardSelect.append($("option", { value: b.id }, b.title || b.id));
    els.boardSelect.value = S.boardId || "";
  }
  els.boardSelect.append($("option", { value: "__new" }, "+ New board…"));

  // A label or person deleted in Settings is no longer a filterable option, so
  // the dropdowns are reconciled before anything counts cards or paints itself.
  syncFilters();
  syncAgent();
  paintFilterState();
  paintViewButtons();

  const current = board();
  els.subtitle.textContent = "";
  if (current) {
    const live = allCards(current).filter((c) => !c.archived).length;
    els.subtitle.append(`${current.key} · ${live} card(s)`);
    if (filtersActive() || S.showArchived) {
      const shown = columns(current).reduce((sum, c) => sum + visibleCards(current, c.id).length, 0);
      els.subtitle.append($("span", { class: "note" }, ` · ${shown} shown`));
    }
  } else {
    els.subtitle.textContent = "no boards";
  }
  // With a dozen tabs open, the board name is what tells them apart.
  document.title = current ? `${current.title || current.id} · yamlboard` : "yamlboard";

  els.file.textContent = S.state.file + (readOnly() ? "  (read-only)" : "");
  paintGit();
  renderBoard();
  // Last: it counts cards and conflicts, and until the board is on screen the
  // counts it prints are the previous board's.
  paintCoordination();
}

function paintGit() {
  const git = S.state?.git;
  els.commitButton.textContent = "";
  els.commitButton.disabled = readOnly() || !git || !git.is_repo || !git.is_dirty;
  els.commitButton.classList.toggle("dirty", !!(git && git.is_dirty));
  if (!git) { els.commitButton.append("git off"); return; }
  if (!git.is_repo) { els.commitButton.append("not a repo"); return; }
  if (git.error) { els.commitButton.append("git: " + git.error); return; }
  els.commitButton.append(git.is_dirty ? `Commit (${git.changed.length})` : "Committed");
  if (git.ahead) els.commitButton.append($("span", { class: "ahead" }, ` ↑${git.ahead}`));
}

function renderBoard() {
  const main = els.main;
  if (!main) return;
  const scroll = { x: main.scrollLeft, y: main.scrollTop };
  main.textContent = "";

  const current = board();
  if (!current) { main.append(emptyFile()); return; }

  if (S.view === "list") main.append(renderList(current));
  else main.append(renderGrid(current));

  main.scrollLeft = scroll.x;
  main.scrollTop = scroll.y;
}

function emptyFile() {
  return $("div", { class: "empty" },
    $("h2", {}, "This board has no cards yet"),
    $("p", {}, "Two jobs, one directory. Make a card: press ", $("kbd", {}, "n"),
      " here, or run ", $("code", {}, "yamlboard create \"Ship the report\" --as you"),
      " anywhere in the repo."),
    $("p", {}, "Let agents work beside you: run ", $("code", {}, "yamlboard skill install"),
      " in the repo, and the next agent that opens it claims a card before it edits a file."),
    readOnly() ? $("p", { class: "note" }, "This server is running read-only.")
      : $("button", { class: "primary", onclick: openNewBoardDialog }, "Create the first board"),
  );
}

/* ------------------------------------------------------------------ board grid */

function renderGrid(current) {
  const cols = columns(current);
  const grid = $("div", { class: "grid", style: { gridTemplateColumns: `repeat(${cols.length + 1}, minmax(230px, 1fr))` } });

  const laneLabels = swimGroups(current);
  if (!laneLabels.length) {
    // flat board: one column per status
    for (const col of cols) grid.append(columnHeader(current, col));
    grid.append(addColumnHeader());
    for (const col of cols) grid.append(columnBody(current, col, null));
    grid.append($("div", { class: "add-col" },
      $("button", { class: "ghost add-column", onclick: () => openSettings("columns") }, "+ column")));
    return grid;
  }

  // swimlanes: lane label + one cell per column, one row per lane. No trailing
  // "+ column" cell here, or the header row would overflow the track count.
  grid.style.gridTemplateColumns = `var(--lane) repeat(${cols.length}, minmax(210px, 1fr))`;
  grid.append($("div", { class: "lane-corner" }));
  for (const col of cols) grid.append(columnHeader(current, col));

  for (const group of laneGroups) {
    grid.append($("div", { class: "lane-label" },
      group.color ? $("span", { class: "dot", style: { background: group.color } }) : null,
      $("span", {}, group.title)));
    for (const col of cols) grid.append(columnBody(current, col, group));
  }
  return grid;
}

let laneGroups = [];

function swimGroups(current) {
  laneGroups = [];
  if (S.swim === "none") return laneGroups;
  const seen = new Map();
  const push = (key, title, color, sortKey) => {
    if (!seen.has(key)) {
      seen.set(key, { key, title, color, sort: sortKey ?? 99, cards: 0 });
    }
  };
  // A lane is only worth a row if it can show something. The row list used to
  // come from allCards while the cells came from visibleCards, so any filter
  // left lanes whose every cell read "no cards match"; visibleCards has also
  // already applied the archive toggle, hence no archived test here.
  const shown = columns(current).flatMap((c) => visibleCards(current, c.id));
  for (const card of shown) {
    if (S.swim === "assignee") {
      const p = person(card.assignee);
      push(card.assignee || "__none", p ? p.name : "Unassigned", p?.color, p ? 0 : 1);
    } else if (S.swim === "priority") {
      const p = card.priority || "none";
      push(p, p, null, PRIORITY_RANK[p] ?? 9);
    } else if (S.swim === "label") {
      for (const id of card.labels && card.labels.length ? card.labels : ["__none"]) {
        const l = label(id);
        push(id, l ? l.name : "No label", l?.color, l ? 0 : 1);
      }
    }
  }
  for (const card of shown) {
    const keys = S.swim === "label"
      ? (card.labels && card.labels.length ? card.labels : ["__none"])
      : [S.swim === "assignee" ? card.assignee || "__none" : card.priority || "none"];
    for (const key of keys) if (seen.has(key)) seen.get(key).cards++;
  }
  laneGroups = [...seen.values()].sort((a, b) => a.sort - b.sort || a.title.localeCompare(b.title));
  if (S.swim === "priority") {
    laneGroups = laneGroups.filter((g) => g.cards > 0);
  }
  return laneGroups;
}

function addColumnHeader() {
  return $("div", { class: "col-head invisible" });
}

function columnHeader(current, col) {
  // Archive view is a different population: the live count would sit above a
  // column full of archived cards and describe none of them, and a WIP limit
  // says nothing about an archive, so the over flag goes quiet there.
  const archived = !!S.showArchived;
  const total = allCards(current).filter((c) => c.column === col.id && !!c.archived === archived).length;
  const over = !archived && col.wip_limit && total > col.wip_limit;
  return $("div", { class: "col-head", style: { "--col": col.color || FALLBACK_COLOR } },
    $("span", { class: "col-dot" }),
    $("span", { class: "col-name", title: col.name }, col.name),
    col.is_done ? $("span", { class: "tag done" }, "done") : null,
    $("span", { class: ["col-count", over ? "over" : ""].filter(Boolean).join(" ") },
      !archived && col.wip_limit ? `${total}/${col.wip_limit}` : String(total)),
    $("button", {
      class: "col-menu", title: "Column options",
      onclick: (event) => openColumnMenu(event, current, col),
    }, "⋮"),
  );
}

/** A column emptied by a filter and a column with nothing in it are different news. */
function emptyColumnText() {
  if (filtersActive()) return "No cards match.";
  if (S.showArchived) return "Nothing archived.";
  return "Empty.";
}

function columnBody(current, col, group) {
  const all = visibleCards(current, col.id);
  const cards = group ? all.filter((card) => laneKeysFor(card).includes(group.key)) : all;
  const body = $("div", {
    class: "col-body", "data-column": col.id, "data-lane": group ? group.key : "",
  });
  for (const card of cards) body.append(cardTile(current, card));
  if (!cards.length) body.append($("p", { class: "col-empty" }, emptyColumnText()));
  if (!readOnly()) {
    body.append($("button", { class: "ghost quick-add", onclick: () => openNewCard(col.id, group?.key) }, "+ card"));
    // clicking the dead space of a column is the quickest way to add a card to it
    body.addEventListener("click", (event) => {
      if (event.target === body) openNewCard(col.id, group?.key);
    });
    wireDropTarget(body, current, col);
  }
  return body;
}

function laneKeysFor(card) {
  if (S.swim === "assignee") return [card.assignee || "__none"];
  if (S.swim === "priority") return [card.priority || "none"];
  if (S.swim === "label") return card.labels && card.labels.length ? card.labels : ["__none"];
  return [""];
}

/* --------------------------------------------------------------------- card ui */

function cardTile(current, card) {
  const who = person(card.assignee);
  const subtasks = card.subtasks || [];
  const openSubs = subtasks.filter((s) => !s.done).length;
  const overdue = card.due && !card.archived && dueState(card.due) === "over";

  const tile = $("article", {
    class: ["card", card.archived ? "archived" : "", overdue ? "overdue" : ""]
      .concat(coordClasses(card)).filter(Boolean).join(" "),
    tabindex: "0", "data-id": card.id,
    draggable: readOnly() ? null : "true",
    style: { "--prio": priorityColor(card.priority), "--claim": claimColor(card) },
    onclick: () => openCard(card.id),
    onkeydown: onEnter(() => openCard(card.id)),
    ondragstart: (event) => {
      event.dataTransfer.setData("text/plain", card.id);
      event.dataTransfer.effectAllowed = "move";
      tile.classList.add("dragging");
      draggingCard = card.id;
    },
    ondragend: () => {
      tile.classList.remove("dragging");
      draggingCard = null;
      clearHints();
    },
  },
    $("div", { class: "card-top" },
      $("span", { class: "card-id" }, card.id),
      (card.links || []).length ? $("a", {
        class: "card-link", href: safeHref((card.links[0] || {}).url), target: "_blank",
        rel: "noreferrer noopener", title: (card.links[0] || {}).title || (card.links[0] || {}).url,
        onclick: (event) => event.stopPropagation(),
      }, icon("link")) : null,
      $("span", { class: "grow" }),
      subtasks.length ? $("span", {
        class: ["meta", openSubs ? "" : "meta-done"].filter(Boolean).join(" "),
        title: openSubs ? `${openSubs} subtasks left` : "all subtasks done",
      }, icon("check"), `${subtasks.length - openSubs}/${subtasks.length}`) : null,
      card.comments?.length ? $("span", { class: "meta", title: `${card.comments.length} comments` },
        icon("chat"), String(card.comments.length)) : null,
    ),
    $("h3", { class: "card-title" }, card.title),
    card.desc ? $("p", { class: "card-desc" }, preview(card.desc)) : null,
    (card.labels || []).length
      ? $("div", { class: "labels" }, ...card.labels.map((id) => chip(label(id), true)))
      : null,
    $("div", { class: "card-foot" },
      card.priority && card.priority !== "none" ? $("span", { class: "prio" }, card.priority) : null,
      card.due ? $("span", { class: ["due", dueState(card.due)].filter(Boolean).join(" "), title: "Due " + card.due },
        icon("clock"), shortDate(card.due)) : null,
      card.estimate !== undefined && card.estimate !== null ? $("span", { class: "est", title: "Estimate" },
        icon("flag"), String(card.estimate)) : null,
      $("span", { class: "grow" }),
      who ? avatar(who) : null,
      // Who holds the card outranks who it is assigned to: the assignee is a
      // plan, the claim is a lock, and a stale claim is exactly what makes two
      // agents edit one file — so it sits at the reading end, after the avatar.
      ...coordNodes(card),
    ),
  );
  return tile;
}

/** First ~40 words, for the Full density preview. */
function preview(text) {
  const words = String(text).replace(/\s+/g, " ").trim().split(" ");
  return words.length > 40 ? words.slice(0, 40).join(" ") + "…" : words.join(" ");
}

/**
 * The file is hand-editable, so a link's url is somebody's text rather than a
 * URL we chose: `javascript:` and friends must never reach an href attribute.
 */
const safeHref = (u) => /^(https?:|mailto:)/i.test(String(u || "").trim()) ? String(u).trim() : "#";

/**
 * Enter, once, per intent. An IME consumes the Enter that confirms a composed
 * character; without the guard that same keystroke also fires the row's action,
 * so a CJK subtask landed half-typed and a commit message went out early.
 */
const onEnter = (fn) => (event) => {
  if (event.key === "Enter" && !event.isComposing) { event.preventDefault(); fn(); }
};

/* A token name rather than a hex: the stripe is set as an inline custom
   property, so a var() reference re-resolves when the theme flips without the
   cards having to be rebuilt. */
function priorityColor(priority) {
  return { urgent: "var(--prio-urgent)", high: "var(--prio-high)", med: "var(--prio-med)",
    low: "var(--prio-low)", none: "transparent" }[priority] || "transparent";
}

function chip(def, small) {
  if (!def) return $("span", { class: "chip", style: { "--c": FALLBACK_COLOR } }, "?");
  return $("span", {
    class: ["chip", small ? "chip-sm" : ""].join(" "),
    style: { "--c": def.color || FALLBACK_COLOR },
    title: def.name,
  }, def.name);
}

/**
 * Whether initials read better in ink than in white on this swatch.
 *
 * People pick their own colour, and white initials fall under 4.5:1 on anything
 * pale - the shipped fallback #7c8797 measures 3.64:1. The break-even sits near
 * a relative luminance of 0.19, which keeps deep colours white (the intended
 * look) and puts ink on everything pale.
 */
function isPaleColor(hex) {
  const digits = String(hex || "").replace("#", "");
  if (!/^([0-9a-f]{3}|[0-9a-f]{6})$/i.test(digits)) return false;
  const full = digits.length === 3 ? digits.replace(/./g, (c) => c + c) : digits;
  const channel = (i) => {
    const raw = parseInt(full.slice(i, i + 2), 16) / 255;
    return raw <= 0.03928 ? raw / 12.92 : ((raw + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(0) + 0.7152 * channel(2) + 0.0722 * channel(4) > 0.19;
}

function avatar(who) {
  const initials = (who.name || "?").trim().split(/\s+/).map((w) => w[0]).slice(0, 2).join("").toUpperCase();
  const color = who.color || FALLBACK_COLOR;
  const klass = "avatar" + (isPaleColor(color) ? " on-light" : "");
  return $("span", { class: klass, title: who.name, style: { background: color } }, initials);
}

/** Y-m-d -> local midnight, or null. A hand-written `due: soon` must not pass. */
function parseYmd(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value || "").trim());
  if (!match) return null;
  const when = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return Number.isNaN(when.getTime()) ? null : when;
}

function dueState(due) {
  const when = parseYmd(due);
  if (!when) return "";
  const today = new Date(); today.setHours(0, 0, 0, 0);
  if (when < today) return "over";
  // Rounded, not truncated: across a fall-back the gap is 29 hours to the day,
  // and 2.04 dropped a due-tomorrow card out of the "soon" band entirely.
  return Math.round((when - today) / 86400000) <= 2 ? "soon" : "";
}

/** 11 / 03 for a real date; an unparseable one is shown whole rather than sliced. */
function shortDate(value) {
  const text = String(value || "").trim();
  return parseYmd(text) ? text.slice(5, 10).replace("-", "/") : text;
}

/* ------------------------------------------------------------------ drag+drop */

/**
 * Which card is under the cursor, or null. Two things need to know: the poll
 * must not rebuild a grid that has a card lifted out of it (the drop target
 * vanishes mid-gesture), and the drop handler must resolve the card against the
 * live document rather than the one this closure was rendered from.
 */
let draggingCard = null;

function wireDropTarget(body, current, col) {
  body.addEventListener("dragover", (event) => {
    if (!event.dataTransfer.types.includes("text/plain")) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    body.classList.add("drag-over");
    const after = cardAfter(body, event.clientY);
    const quickAdd = body.querySelector(".quick-add");
    body.insertBefore(dropHint(), after || quickAdd || null);
  });
  body.addEventListener("dragleave", (event) => {
    if (!body.contains(event.relatedTarget)) {
      body.classList.remove("drag-over");
      dropHint().remove();
    }
  });
  body.addEventListener("drop", async (event) => {
    event.preventDefault();
    body.classList.remove("drag-over");
    const id = event.dataTransfer.getData("text/plain");
    const card = cardById(id);
    if (!card || !!card.archived !== !!S.showArchived) return clearHints();

    const hint = dropHint();
    const beforeId = hint.nextElementSibling?.dataset?.id || null;
    clearHints();

    // Re-read the board: a poll, or another tab's commit, can have rewritten the
    // file since this column was rendered, and ordering against that stale list
    // would silently move the card to a position nobody dragged it to.
    current = board() || current;
    // Both indexes are counted in the list *without* the dragged card, so
    // equality means the drop would reproduce the order the file already has.
    const shown = visibleCards(current, col.id);
    const siblings = shown.filter((other) => other.id !== id).map((c) => c.id);
    const wasIndex = shown.findIndex((c) => c.id === id);
    const willIndex = beforeId ? siblings.indexOf(beforeId) : siblings.length;
    const samePlace = col.id === card.column && wasIndex === willIndex;
    const crossedInto = col.id !== card.column;

    if (!samePlace) {
      const moved = await send("card.move",
        { id, board: current.id, column: col.id, before: beforeId }, "Move");
      if (!moved) return;
    }
    // In swimlane mode a row *is* a field value, so honour the row that was
    // dropped into: dropping into Alice's row assigns the card to Alice.
    if (body.dataset.lane) await applyLane(id, body.dataset.lane);

    if (crossedInto && col.is_done) offerArchive(id, col.name);
  });
}

/** Make a card belong to the swimlane row it was dropped into. */
async function applyLane(cardId, laneKey) {
  if (laneKey === "") return;
  const card = cardById(cardId);
  if (!card) return;
  if (S.swim === "assignee") {
    const wanted = laneKey === "__none" ? null : laneKey;
    if ((card.assignee || null) !== wanted) {
      await send("card.update", { id: cardId, patch: { assignee: wanted } }, "Assign");
    }
  } else if (S.swim === "priority" && (card.priority || "none") !== laneKey) {
    await send("card.update", { id: cardId, patch: { priority: laneKey } }, "Priority");
  } else if (S.swim === "label") {
    // Set semantics: the row is the lane, so dropping into "bug" means "this is
    // a bug card", not "this card is also a bug". Appending here left a card
    // rendered in three rows at once, and the no-label row was a silent no-op.
    const wanted = laneKey === "__none" ? [] : [laneKey];
    const now = card.labels || [];
    if (now.length !== wanted.length || now[0] !== wanted[0]) {
      await send("card.update", { id: cardId, patch: { labels: wanted } }, "Label");
    }
  }
}

function cardAfter(container, y) {
  const tiles = [...container.querySelectorAll(".card:not(.dragging)")];
  for (const tile of tiles) {
    const box = tile.getBoundingClientRect();
    if (y < box.top + box.height / 2) return tile;
  }
  return null;
}

let hint = null;
function dropHint() {
  if (!hint) hint = $("div", { class: "drop-hint" });
  return hint;
}
function clearHints() { if (hint) hint.remove(); }

function offerArchive(cardId, columnName) {
  toast(`${cardId} landed in “${columnName}”. Archive it?`, "info", 9000, {
    label: "Archive",
    action: () => send("card.archive", { id: cardId, archived: true }, "Archive"),
  });
}

/* --------------------------------------------------------------------- list ui */

function renderList(current) {
  const rows = $("tbody");
  const ordered = columns(current).flatMap((col) =>
    visibleCards(current, col.id).map((card) => [col, card]));
  for (const [col, card] of ordered) {
    const who = person(card.assignee);
    rows.append($("tr", {
      tabindex: "0", "data-id": card.id,
      onclick: () => openCard(card.id),
      onkeydown: onEnter(() => openCard(card.id)),
      class: card.archived ? "archived" : "",
    },
      $("td", { class: "mono" }, card.id),
      $("td", {}, card.title),
      $("td", {}, $("span", { class: "col-tag", style: { "--col": col.color || FALLBACK_COLOR } }, col.name)),
      $("td", {}, who ? avatar(who) : $("span", { class: "note" }, card.assignee || "—")),
      $("td", {}, card.priority && card.priority !== "none" ? $("span", { class: "prio" }, card.priority) : ""),
      // The state colours live on .due.soon / .due.over, so the cell needs the
      // .due class too - a bare "over" on the <td> matched nothing and painted
      // the date in body colour. Same pill as the card foot, minus the icon.
      $("td", {}, card.due ? $("span", {
        class: ["due", dueState(card.due)].filter(Boolean).join(" "),
        title: "Due " + card.due,
      }, shortDate(card.due)) : ""),
      $("td", {}, ...(card.labels || []).map((id) => chip(label(id), true))),
      $("td", { class: "mono" }, card.estimate ?? ""),
    ));
  }
  if (!ordered.length) return $("div", { class: "empty" }, $("p", {}, "No cards match this view."));
  return $("table", { class: "list" },
    $("thead", {}, $("tr", {},
      ...["ID", "Title", "Column", "Who", "Priority", "Due", "Labels", "Est"].map((h) => $("th", {}, h)))),
    rows);
}

/* ---------------------------------------------------------------------- toasts */

function toast(message, kind = "info", ms = 4200, withAction = null) {
  const root = document.getElementById("toast-root");
  let timer = null;
  const kill = () => { clearTimeout(timer); node.remove(); };
  const node = $("div", { class: "toast " + kind }, $("span", {}, message),
    withAction ? $("button", {
      class: "primary toast-action",
      onclick: async () => { kill(); await withAction.action(); },
    }, withAction.label) : null,
    $("button", { class: "toast-x", "aria-label": "Dismiss", onclick: kill }, "×"));
  root.append(node);
  if (ms) timer = setTimeout(kill, ms);
  /* A burst of failures used to stack toasts down the whole viewport. Stepping
     off the oldest one has to go through its kill() too, or the timer of a node
     nobody can see any more keeps the detached element alive. */
  while (root.children.length > 4) {
    const oldest = root.firstElementChild;
    if (oldest.dismiss) oldest.dismiss(); else oldest.remove();
  }
  node.dismiss = kill;
  return node;
}

/* ---------------------------------------------------------------------- modals */

let dialog = null;
const modalOpen = () => !!dialog;

function openModal(title, bodyNode, buttons, wide, focusId, headExtra) {
  // Replacing a panel is not closing one: no resync poll here, or rebuilding a
  // card modal to repaint a subtask would also refetch the board.
  if (dialog) closePanel();
  // Under the modal's scrim a popover is unreachable, so it must not survive:
  // one Escape has to end the whole transient layer, not just the top of it.
  closePop();
  const root = document.getElementById("modal-root");
  root.hidden = false;
  const panel = $("div", { class: "panel" + (wide ? " wide" : ""), role: "dialog", "aria-modal": "true", "aria-label": title },
    $("div", { class: "panel-head" }, $("h2", {}, title), headExtra || null,
      $("button", { class: "ghost", "aria-label": "Close", onclick: closeModal }, "×")),
    $("div", { class: "panel-body" }, bodyNode),
    buttons ? $("div", { class: "panel-foot" }, ...buttons) : null,
  );
  dialog = $("div", {
    class: "scrim",
    onclick: (event) => { if (event.target === dialog) closeModal(); },
  }, panel);
  root.append(dialog);
  // Where the person was standing before the dialog took the screen; Tab is
  // trapped inside the panel, so this is the only way back out.
  dialog.opener = document.activeElement;
  // aria-modal is a promise to a screen reader that the rest of the page is
  // gone. `inert` is the version the browser enforces: without it, Tab walks
  // straight out of the dialog and into the board behind the scrim.
  document.getElementById("root").inert = true;
  const target = (focusId && panel.querySelector("#" + CSS.escape(focusId)))
    || panel.querySelector("input, textarea, select, button.primary");
  if (target) target.focus();
  document.addEventListener("keydown", modalKeys, true);
  return panel;
}

/** Escape ends the dialog; Tab stays inside it. */
function modalKeys(event) {
  if (event.key === "Escape") { event.stopPropagation(); event.preventDefault(); closeModal(); return; }
  if (event.key !== "Tab" || !dialog) return;
  const focusable = [...dialog.querySelectorAll(
    "a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled),"
    + " textarea:not(:disabled), [tabindex]:not([tabindex='-1'])")]
    .filter((node) => node.offsetParent !== null);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const active = document.activeElement;
  if (event.shiftKey && (active === first || !dialog.contains(active))) {
    event.preventDefault(); last.focus();
  } else if (!event.shiftKey && (active === last || !dialog.contains(active))) {
    event.preventDefault(); first.focus();
  }
}

/** Tear the panel down without asking the server anything. */
function closePanel() {
  const opener = dialog.opener;
  dialog.remove();
  dialog = null;
  S.openCardId = null;
  document.removeEventListener("keydown", modalKeys, true);
  document.getElementById("root").inert = false;
  // #modal-root exists only to carry the scrim. Left shown with no children it
  // is a full-screen empty layer, and it eats clicks meant for cards.
  document.getElementById("modal-root").hidden = true;
  return opener;
}

function closeModal() {
  if (!dialog) return;
  const opener = closePanel();
  if (opener && opener.isConnected) opener.focus();
  // The poll pauses while a modal is open, so whatever happened meanwhile - a
  // Vim edit, a pull, another tab's commit - is picked up here rather than
  // whenever the next op collides with it. Cheap when nothing moved: adopt()
  // does not repaint unless the file's stamp actually changed.
  if (els.main) refresh();
}

/**
 * Redraw an open card modal from the freshly adopted document.
 *
 * Almost every edit inside the modal replaces the document, so the modal has to
 * be rebuilt from the server's copy -- otherwise a posted comment or a ticked
 * subtask would silently not appear. The one edit that must *not* trigger a
 * rebuild is the title/notes autosave: its own DOM text is newer than anything
 * the server had a moment ago, and rebuilding mid-word would restore older text
 * and drop the caret. `textFlushInFlight` is set only around that send.
 *
 * Rebuilds keep the caret: the focused control's id and selection are carried
 * across, so typing in the notes survives a subtask tick next to it.
 */
async function reloadOpenCard() {
  if (!dialog || !S.openCardId) return;
  if (!cardById(S.openCardId)) return closeModal();   // deleted
  // The one rebuild that must wait. Skipping it outright would drop the repaint
  // for good: a subtask ticked during the autosave window would never appear.
  if (textFlushInFlight) { pendingReload = true; return; }
  pendingReload = false;
  reloadingCard = true;
  const where = dialog.contains(document.activeElement) ? document.activeElement : null;
  // title and notes are recreated each rebuild without an id, so match on class
  const key = where?.id
    ? "#" + CSS.escape(where.id)
    : where?.classList.contains("doc-title") ? ".doc-title"
    : where?.classList.contains("doc-desc") ? ".doc-desc"
    : null;
  const caret = where && "selectionStart" in where ? [where.selectionStart, where.selectionEnd] : null;
  // Writing a subtask or posting a comment does not touch title/notes, so any
  // text still inside the debounce window has to be committed before the
  // rebuild -- otherwise the new DOM is built from the server's older text and
  // whatever was being typed disappears.
  if (dialog.flushPending) await dialog.flushPending();
  await openCard(S.openCardId);
  reloadingCard = false;
  if (!dialog || !key) return;
  const again = dialog.querySelector(key);
  if (!again) return;
  again.focus();
  if (caret) { try { again.setSelectionRange(caret[0], caret[1]); } catch { /* not a text field */ } }
}

/** True only while the title/notes autosave request is in flight. */
let textFlushInFlight = false;
/** A repaint asked for during that window; replayed once the text is safe. */
let pendingReload = false;
/** Set while a rebuild is already running, so it never recurses into itself. */
let reloadingCard = false;

/**
 * Card editor. `focus` is a selector to re-focus after the modal is rebuilt, so
 * posting a comment or ticking a subtask (both of which replace the document
 * with the server's) does not throw away where the person was typing.
 */
async function openCard(cardId, focus) {
  const card = cardById(cardId);
  if (!card) return closeModal();
  const current = boardList().find((b) => allCards(b).includes(card)) || board();
  const fields = customFields(current);
  const editable = !readOnly();

  const title = $("input", { class: "doc-title", value: card.title || "", placeholder: "Untitled card", disabled: !editable });
  const desc = $("textarea", { class: "doc-desc", rows: "5", placeholder: "Notes — stored exactly as you write it", disabled: !editable }, card.desc || "");

  const colSelect = $("select", { disabled: readOnly() },
    ...columns(current).map((c) => $("option", { value: c.id, selected: c.id === card.column }, c.name)));
  const whoSelect = $("select", { disabled: readOnly() },
    $("option", { value: "", selected: !card.assignee }, "Unassigned"),
    ...people().map((p) => $("option", { value: p.id, selected: p.id === card.assignee }, p.name)));
  const prioSelect = $("select", { disabled: readOnly() },
    ...PRIORITIES.map((p) => $("option", { value: p, selected: (card.priority || "none") === p }, p)));
  const due = $("input", { type: "date", value: (card.due || "").slice(0, 10), disabled: readOnly() });
  const estimate = $("input", { type: "number", min: "0", step: "1", value: card.estimate ?? "", disabled: readOnly() });

  const labelPicker = $("div", { class: "pick" }, ...labels().map((def) => {
    const box = $("input", { type: "checkbox", disabled: readOnly() });
    box.checked = (card.labels || []).includes(def.id);
    return $("label", { class: "pick-item" }, box, chip(def));
  }));
  if (!labels().length) labelPicker.append($("span", { class: "note" }, "no labels defined yet"));

  /* Custom fields are declared per board, so they render as the same pills as
   * the built-ins instead of a second grid of labelled boxes. */
  const customInputs = new Map();
  const customWrap = $("div", { class: "doc-pills" });
  for (const spec of fields) {
    const value = (card.fields || {})[spec.id];
    let input;
    if (spec.type === "select") {
      input = $("select", { disabled: readOnly() },
        $("option", { value: "", selected: !value }, "—"),
        ...(spec.options || []).map((o) => $("option", { value: o, selected: o === value }, o)));
    } else if (spec.type === "bool") {
      input = $("input", { type: "checkbox", disabled: readOnly() });
      input.checked = !!value;
    } else if (spec.type === "number") {
      input = $("input", { type: "number", value: value ?? "", disabled: readOnly() });
    } else if (spec.type === "date") {
      input = $("input", { type: "date", value: String(value || "").slice(0, 10), disabled: readOnly() });
    } else {
      input = $("input", { value: value ?? "", placeholder: "—", size: "12", disabled: readOnly() });
    }
    customInputs.set(spec, input);
    customWrap.append(pill(spec.name + (spec.required ? " *" : ""), input, "custom"));
  }
  /** The server replaces the whole map, so every field travels together. */
  const collectFields = () => Object.fromEntries([...customInputs].map(([spec, control]) => [
    spec.id,
    spec.type === "bool" ? control.checked
      : spec.type === "number" ? (control.value === "" ? "" : Number(control.value))
      : control.value,
  ]));

  /* Links are a list you add to and take away from, so each structural change
   * (add a row, delete a row, leave a filled-in row) writes the whole list
   * back. Nothing here can be left silently unsaved by closing the modal. */
  const linkRows = $("div", { class: "rows" });
  const collectLinks = () => [...linkRows.children]
    .map((row) => { const [url, title] = row.querySelectorAll("input"); return { url: url.value.trim(), title: title.value.trim() }; })
    .filter((link) => link.url);
  const saveLinks = () => patch({ links: collectLinks() }, "Links");
  /**
   * A link row is a list you add to and take rows from, so every structural
   * change writes the whole list back and nothing can be left unsaved by
   * closing the modal. Each row carries its own leading open-link affordance,
   * which is why there is no separate read-only list of links up top.
   */
  const addLinkRow = (url = "", text = "", isNew = false) => {
    // The row's position is its identity in the saved list, so the ids ride on
    // it: a blur writes the whole list, the document is re-adopted, the modal is
    // rebuilt - and without an id to find its way back to, the caret lands on
    // the title field and the next keystrokes become the card's title.
    const at = linkRows.children.length;
    const urlInput = $("input", { id: `link-url-${at}`, value: url, placeholder: "https://…", class: "grow", disabled: readOnly() });
    const titleInput = $("input", { id: `link-title-${at}`, value: text, placeholder: "label", disabled: readOnly() });
    const open = $("a", {
      class: "row-open", href: safeHref(url), target: "_blank", rel: "noreferrer noopener",
      title: "Open link", "aria-label": "Open link",
    }, icon("link"));
    const row = $("div", { class: "row" }, open, urlInput, titleInput,
      readOnly() ? null : $("button", {
        class: "ghost", title: "Remove link",
        onclick: () => { row.remove(); saveLinks(); },
      }, "×"));
    const sync = () => { open.href = safeHref(urlInput.value); };
    for (const box of [urlInput, titleInput]) {
      box.addEventListener("input", sync);
      if (!readOnly()) box.addEventListener("blur", () => {
        const before = JSON.stringify((cardById(card.id)?.links || []).map((l) => [l.url, l.title || ""]));
        const after = JSON.stringify(collectLinks().map((l) => [l.url, l.title]));
        if (before !== after) saveLinks();   // never write a file-changing no-op
      });
    }
    linkRows.append(row);
    if (isNew) urlInput.focus();
  };
  for (const link of card.links || []) addLinkRow(link.url, link.title || "");
  const linksWrap = $("div", { class: "doc-rows" }, linkRows,
    readOnly() ? null : $("button", { class: "ghost", onclick: () => addLinkRow("", "", true) }, "+ link"));

  // subtasks
  const subList = $("div", { class: "subs" });
  const addSubRow = (text = "", done = false, index = null) => {
    const box = $("input", { type: "checkbox", disabled: readOnly() });
    box.checked = !!done;
    const row = $("div", { class: "sub" }, box, $("span", { class: "sub-text" }, text));
    if (index !== null && !readOnly()) {
      box.addEventListener("change", async () => {
        await send("card.subtask.toggle", { id: card.id, index, done: box.checked }, "Subtask");
      });
      row.append($("button", {
        class: "ghost", title: "Remove subtask",
        // Index-addressed like comment.delete: check the row still holds the
        // text it was built from before writing the list without it.
        onclick: async () => {
          const list = cardById(card.id)?.subtasks || [];
          if (!list[index] || String(list[index].text || "") !== String(text || "")) {
            toast("The subtasks changed while this card was open — the list reloaded.", "warn");
            await reloadOpenCard();
            return;
          }
          await send("card.update", { id: card.id, patch: { subtasks: list.filter((_, i) => i !== index) } }, "Remove subtask");
        },
      }, "×"));
    }
    subList.append(row);
  };
  (card.subtasks || []).forEach((s, i) => addSubRow(s.text, !!s.done, i));
  const newSub = $("input", { id: "new-sub", placeholder: "Add a subtask, press Enter", disabled: readOnly() });
  newSub.addEventListener("keydown", onEnter(async () => {
    const text = newSub.value.trim();
    if (!text) return;
    // A hand-written `text: ""` row is not a subtask, and it survives every
    // later write, so the next write drops the blanks along with its add.
    const subtasks = [...(cardById(card.id)?.subtasks || []), { text, done: false }]
      .filter((s) => String(s.text || "").trim());
    newSub.value = "";
    const ok = await send("card.update", { id: card.id, patch: { subtasks } }, "Add subtask");
    if (!ok) newSub.value = text;   // the op never landed: give the text back
  }));

  // comments
  const commentList = $("div", { class: "comments" });
  (card.comments || []).forEach((entry, index) => {
    commentList.append($("div", { class: "comment" },
      $("div", { class: "comment-head" },
        entry.by ? avatar(person(entry.by)) : $("span", { class: "avatar anon" }, "·"),
        $("span", { class: "comment-who" }, person(entry.by)?.name || "someone"),
        $("time", { datetime: String(entry.at || ""), title: String(entry.at || "") }, humanTime(entry.at)),
        readOnly() ? null : $("button", {
          class: "ghost", title: "Delete comment",
          // The op addresses a comment by position, so if another tab posted or
          // deleted one since this row was rendered the index names somebody
          // else's note. The durable fix is an identity-keyed card.comment.delete.
          onclick: async () => {
            const now = (cardById(card.id)?.comments || [])[index];
            if (!now || now.at !== entry.at || now.text !== entry.text) {
              toast("The comments changed while this card was open — the list reloaded.", "warn");
              await reloadOpenCard();
              return;
            }
            await send("card.comment.delete", { id: card.id, index }, "Delete comment");
          },
        }, "×"),
      ),
      $("p", {}, entry.text)));
  });
  if (!(card.comments || []).length) commentList.append($("p", { class: "note" }, "no comments"));
  const commentBy = $("select", { title: "Comment author", "aria-label": "Comment author" },
    $("option", { value: "", selected: true }, "No author"),
    ...people().map((p) => $("option", { value: p.id }, p.name)));
  const commentText = $("input", { id: "comment-text", placeholder: "Add a comment, press Enter", disabled: readOnly() });
  const postComment = async () => {
    const text = commentText.value.trim();
    if (!text) return;
    commentText.value = "";
    const ok = await send("card.comment", { id: card.id, text, by: commentBy.value || null }, "Comment");
    // A refused post must not swallow the paragraph that was just typed.
    if (!ok) commentText.value = text;
  };
  commentText.addEventListener("keydown", onEnter(postComment));

  /* The editor reads as a document, not a form: the title and notes are
   * borderless text you click and type into, and everything discrete is a pill
   * that applies the moment you pick it. There is no Save button, because every
   * edit is an op and ops are already the unit of the file. */
  const saved = $("span", { class: "saved" });
  const showSaved = (text, kind) => {
    saved.textContent = text;
    saved.className = "saved" + (kind ? " " + kind : "");
  };
  showSaved(`saved to ${card.id}`);

  let saveTimer = null;
  let dirty = false;
  const flushText = async () => {
    clearTimeout(saveTimer);
    saveTimer = null;
    // Escape closes mid-debounce; a detached indicator means this editor is gone,
    // so the keystrokes it was about to write are discarded, as Escape promises.
    if (!saved.isConnected) { dirty = false; return false; }
    if (!dirty) return true;
    const changes = { title: title.value.trim(), desc: desc.value };
    if (!changes.title) { showSaved("a card needs a title", "warn"); return false; }
    dirty = false;
    showSaved("saving…", "busy");
    // The adopted document still holds pre-keystroke text for a moment; letting
    // the normal rebuild run here would flash it back into the field in use.
    textFlushInFlight = true;
    const ok = !!(await send("card.update", { id: card.id, patch: changes }, "Save card"));
    textFlushInFlight = false;
    // Whatever the window swallowed (a subtask tick, a comment from a second
    // tab's poll) is rebuilt now that our own text is safely on disk. When this
    // flush is itself part of a rebuild, that rebuild is the replay.
    if (pendingReload && !reloadingCard) { pendingReload = false; await reloadOpenCard(); }
    showSaved(ok ? `saved to ${card.id}` : "not saved", ok ? "" : "warn");
    return ok;
  };
  /* Closing the tab inside the debounce window used to lose the last few words
     with no warning. keepalive is the only write a page is still allowed to make
     while it unloads, and it is fire-and-forget: no adopt, no toast - which is
     fine, since the poll picks the truth up when the tab comes back. */
  window.addEventListener("pagehide", () => {
    if (!dirty || !saved.isConnected || readOnly()) return;
    const changes = { title: title.value.trim(), desc: desc.value };
    if (!changes.title) return;
    const tokens = { ...(S.state?.tokens || {}) };
    delete tokens.board;   // a card edit must not stake a claim on board.yaml
    fetch("/api/apply", {
      method: "POST", keepalive: true, headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ op: "card.update", tokens,
                             args: { id: card.id, patch: changes } }),
    }).catch(() => {});
  });

  const markEdited = () => {
    dirty = true;
    showSaved("unsaved", "busy");
    clearTimeout(saveTimer);
    saveTimer = setTimeout(flushText, 600);
  };
  /* Notes have no visible frame, so they must grow instead of scrolling: a
   * clipped, unscrollable paragraph of notes reads as broken. */
  const grow = () => { desc.style.height = "auto"; desc.style.height = `${desc.scrollHeight + 2}px`; };
  desc.addEventListener("input", grow);
  requestAnimationFrame(grow);

  for (const box of [title, desc]) {
    box.addEventListener("input", markEdited);
    box.addEventListener("blur", flushText);
  }
  title.addEventListener("keydown", onEnter(flushText));

  /** One discrete edit: patch, and let the adopted document repaint the pill. */
  const patch = async (changes, label) => {
    // A refused text flush (an emptied title, say) means the pill the person
    // just moved never reached the file: rebuild from the document so it shows
    // what the file holds rather than the edit that was declined.
    if (!(await flushText())) { await reloadOpenCard(); return; }
    await send("card.update", { id: card.id, patch: changes }, label);
  };
  colSelect.addEventListener("change", () => patch({ column: colSelect.value }, "Move"));
  whoSelect.addEventListener("change", () => patch({ assignee: whoSelect.value || null }, "Assign"));
  prioSelect.addEventListener("change", () => patch({ priority: prioSelect.value }, "Priority"));
  due.addEventListener("change", () => patch({ due: due.value || "" }, "Due date"));
  estimate.addEventListener("change", () =>
    patch({ estimate: estimate.value === "" ? "" : Number(estimate.value) }, "Estimate"));

  const setPanelTitle = () => {
    const heading = dialog?.querySelector(".panel-head h2");
    if (heading) heading.textContent = `${card.id} · ${title.value.trim() || "untitled"}`;
  };
  title.addEventListener("input", setPanelTitle);

  const colStyle = { "--col": column(current, card.column)?.color || FALLBACK_COLOR };
  const who = person(card.assignee);

  const body = $("div", { class: "doc" },
    title,
    $("div", { class: "doc-pills" },
      pill("status", colSelect, "col", colStyle),
      pill("who", whoSelect, "who", who && who.color ? { "--who": who.color } : null),
      pill("priority", prioSelect, "prio"),
      pill("due", due, "date"),
      pill("points", estimate, "pts"),
    ),
    desc,
    coordSection(card),
    labels().length ? section("Labels", labelPicker) : null,
    fields.length ? section("Custom fields", customWrap) : null,
    /* Empty is only noise when there is nothing to do there. In edit mode the
     * section stays even when empty, because that is where "+ add" lives. */
    (card.subtasks || []).length || !readOnly()
      ? section("Subtasks", $("div", {}, subList, readOnly() ? null : newSub)) : null,
    (card.links || []).length || !readOnly() ? section("Links", linksWrap) : null,
    section("Activity", $("div", {}, commentList,
      readOnly() ? null : $("div", { class: "composer" }, commentBy, commentText,
        $("button", { class: "primary", onclick: postComment }, "Post")))),
  );

  /* Label chips and custom fields are sets rather than single values: the
   * server replaces the whole list or map, so one change writes all of it. */
  if (!readOnly()) {
    labelPicker.addEventListener("change", () => {
      const ids = [...labelPicker.querySelectorAll("input")]
        .map((box, i) => (box.checked ? labels()[i].id : null)).filter(Boolean);
      patch({ labels: ids }, "Labels");
    });
    for (const [spec, control] of customInputs) {
      control.addEventListener("change", () => patch({ fields: collectFields() }, spec.name));
    }
  }

  const buttons = [];
  if (!readOnly()) {
    buttons.push($("button", {
      class: "danger", title: "Delete this card",
      onclick: async () => {
        if (!confirm(`Delete ${card.id} “${card.title}”? Its file goes away in .yamlboard/cards/`)) return;
        await send("card.delete", { id: card.id }, "Delete card");
        closeModal();
      },
    }, "Delete"));
    buttons.push($("button", {
      class: "ghost",
      onclick: async () => {
        await send("card.archive", { id: card.id, archived: !card.archived }, "Archive");
        closeModal();
      },
    }, card.archived ? "Unarchive" : "Archive"));
  }
  buttons.push($("span", { class: "grow" }));
  buttons.push($("button", {
    class: "primary", onclick: async () => { await flushText(); closeModal(); },
  }, "Done"));

  // The save state belongs in the header: the body scrolls, the header does not.
  // And openCardId goes last: openModal closes whatever modal is already up, and
  // closeModal clears it - set it first and every repaint after this point is
  // dead, the auto-close of a card another tab deleted included.
  openModal(`${card.id} · ${card.title}`, body, buttons, true, focus, saved);
  S.openCardId = card.id;
  dialog.flushPending = flushText;   // reloadOpenCard commits text before rebuilding
}

/**
 * A value you can change: a bare control dressed as a chip with a leading
 * label. The control stays a real <select>/<input>, so the keyboard and the
 * native pickers keep working -- only the chrome changes, and that is what
 * stops the editor from reading as a form.
 */
function pill(labelText, control, kind, style) {
  return $("label", { class: "pill" + (kind ? " pill-" + kind : ""), style: style || null },
    $("span", { class: "pill-key" }, labelText), control);
}

/** A titled block inside the card document. */
function section(title, contents) {
  if (!contents) return null;
  return $("section", { class: "doc-section" }, title ? $("h4", {}, title) : null, contents);
}

function field(labelText, control) {
  return $("label", { class: "field" }, $("span", {}, labelText), control);
}

function humanTime(value) {
  const text = String(value || "").replace("T", " ").slice(0, 16);
  return text || "";
}

/* ------------------------------------------------------------------- new card */

function openNewCard(columnId, laneKey) {
  const current = board();
  if (!current) return;
  const title = $("input", { placeholder: "What needs doing?", autofocus: "" });
  const colSelect = $("select", {}, ...columns(current).map((c) => $("option", { value: c.id, selected: c.id === columnId }, c.name)));
  const desc = $("textarea", { rows: "3", placeholder: "Notes (optional)" });
  const create = async () => {
    if (!title.value.trim()) return toast("Type a title first.", "warn");
    const result = await send("card.create", {
      board: current.id, column: colSelect.value, title: title.value.trim(), desc: desc.value,
    }, "Create card");
    closeModal();
    if (!result) return;
    // A lane row is a field value, so a card added from inside one belongs to
    // it; applyLane already turns a lane key into the right patch.
    if (laneKey) await applyLane(result.id, laneKey);
    openCard(result.id);
  };
  title.addEventListener("keydown", onEnter(create));
  openModal("New card", $("div", {}, field("Title", title), field("Column", colSelect), field("Notes", desc)),
    [$("button", { class: "ghost", onclick: closeModal }, "Cancel"),
     $("button", { class: "primary", onclick: create }, "Create")]);
}

/* ------------------------------------------------------------------- settings */

function openSettings(tab = "columns") {
  const wrap = $("div", { class: "settings" });
  const tabs = [["columns", "Columns"], ["boards", "Boards"], ["labels", "Labels"], ["people", "People"], ["file", "File"]];
  const body = $("div", { class: "settings-body", role: "tabpanel" });
  const nav = $("div", { class: "settings-nav", role: "tablist", "aria-label": "Settings sections" });
  const show = (name) => {
    [...nav.children].forEach((button, i) => {
      const on = tabs[i][0] === name;
      button.classList.toggle("on", on);
      button.setAttribute("aria-selected", String(on));
    });
    body.textContent = "";
    if (name === "columns") renderColumnSettings(body);
    if (name === "boards") renderBoardSettings(body);
    if (name === "labels") renderMetaSettings(body, "label");
    if (name === "people") renderMetaSettings(body, "person");
    if (name === "file") renderFileSettings(body);
  };
  tabs.forEach(([name, text]) => nav.append($("button", {
    class: "ghost", role: "tab", "aria-selected": "false", onclick: () => show(name),
  }, text)));
  wrap.append(nav, body);
  // Filled before the modal opens: openModal focuses the first control it can
  // find, and in an empty panel that is the Done button rather than a setting.
  show(tab);
  openModal("Settings", wrap, [$("button", { class: "ghost", onclick: closeModal }, "Done")], true);
}

function renderColumnSettings(body) {
  const current = board();
  if (!current) return body.append($("p", { class: "note" }, "create a board first"));
  body.append($("p", { class: "note" },
    "Column order here is the board order. Deleting a column moves its cards into the column you pick."));
  for (const col of columns(current)) {
    const name = $("input", { value: col.name, title: "Column name", "aria-label": "Column name", disabled: readOnly() });
    const hadColor = !!col.color;
    const color = $("input", { type: "color", value: col.color || FALLBACK_COLOR, title: "Column colour", "aria-label": "Column colour", disabled: readOnly() });
    const limit = $("input", { type: "number", min: "1", value: col.wip_limit ?? "", placeholder: "none", title: "WIP limit", "aria-label": "WIP limit", disabled: readOnly() });
    const done = $("input", { type: "checkbox", disabled: readOnly() });
    done.checked = !!col.is_done;
    body.append($("div", { class: "setting-row" },
      $("div", { class: "order" },
        $("button", { class: "ghost", title: "Move left", disabled: readOnly(), onclick: () => moveColumn(current, col, "left") }, "←"),
        $("button", { class: "ghost", title: "Move right", disabled: readOnly(), onclick: () => moveColumn(current, col, "right") }, "→")),
      name, color, limit,
      $("label", { class: "inline" }, done, "done"),
      $("button", { class: "ghost", title: "Save", disabled: readOnly(), onclick: async () => {
        const raw = limit.value.trim();
        const n = Number(raw);
        if (raw !== "" && !(Number.isInteger(n) && n >= 1)) return toast("A WIP limit is a whole number of 1 or more.", "warn");
        await send("column.update", {
          board: current.id, id: col.id, name: name.value.trim(),
          // A column that never had a colour keeps none: the picker always shows
          // the fallback grey, and writing it out turns a widget default into
          // board data that survives every later round trip.
          color: hadColor || color.value.toLowerCase() !== FALLBACK_COLOR.toLowerCase() ? color.value : "",
          wip_limit: raw === "" ? 0 : n, is_done: done.checked,
        }, "Save column");
      } }, "Save"),
      $("button", { class: "danger", disabled: readOnly(), onclick: () => deleteColumn(current, col) }, "Delete"),
    ));
  }
  body.append($("div", { class: "setting-row add" },
    $("input", { id: "new-col-name", placeholder: "New column name", title: "New column name", "aria-label": "New column name" }),
    $("button", { class: "primary", disabled: readOnly(), onclick: async (event) => {
      const input = body.querySelector("#new-col-name");
      if (!input.value.trim()) return;
      await send("column.create", { board: current.id, name: input.value.trim(), after: columns(current).at(-1)?.id }, "Add column");
    } }, "Add column")));

  const fields = customFields(current);
  body.append($("h3", {}, "Custom fields"));
  for (const spec of fields) {
    const name = $("input", { value: spec.name, title: "Field name", "aria-label": "Field name", disabled: readOnly() });
    const options = $("input", { value: (spec.options || []).join(", "), placeholder: "option, option", title: "Field options", "aria-label": "Field options", disabled: readOnly() });
    body.append($("div", { class: "setting-row" },
      $("span", { class: "mono note" }, spec.id), name,
      $("span", { class: "note" }, spec.type),
      spec.type === "select" ? options : null,
      $("button", { class: "ghost", disabled: readOnly(), onclick: () => {
        // Only a select has options; sending an empty list for a text field
        // reads as "replace its options with nothing" and wipes card values.
        const payload = { board: current.id, id: spec.id, name: name.value.trim() };
        if (spec.type === "select")
          payload.options = options.value.split(",").map((s) => s.trim()).filter(Boolean);
        send("field.update", payload, "Save field");
      } }, "Save"),
      $("button", { class: "danger", disabled: readOnly(), onclick: () => send("field.delete", { board: current.id, id: spec.id }, "Delete field") }, "Delete"),
    ));
  }
  const newName = $("input", { placeholder: "Field name", title: "New field name", "aria-label": "New field name" });
  const newType = $("select", { title: "New field type", "aria-label": "New field type" }, ...["text", "select", "date", "number", "bool"].map((t) => $("option", { value: t }, t)));
  const newOptions = $("input", { placeholder: "options (select only), comma separated", title: "New field options", "aria-label": "New field options" });
  body.append($("div", { class: "setting-row add" }, newName, newType, newOptions,
    $("button", { class: "primary", disabled: readOnly(), onclick: async () => {
      if (!newName.value.trim()) return;
      // Only a select has options; a text field given a list stores it and the
      // card editor then has nothing to render it with.
      await send("field.create", {
        board: current.id, name: newName.value.trim(), type: newType.value,
        options: newType.value === "select"
          ? newOptions.value.split(",").map((s) => s.trim()).filter(Boolean) : [],
      }, "Add field");
      openSettings("columns");
    } }, "Add field")));
}

function moveColumn(current, col, direction) {
  const ids = columns(current).map((c) => c.id);
  const index = ids.indexOf(col.id);
  const before = direction === "left" ? ids[index - 1] : ids[index + 1];
  if (!before) return;
  send("column.reorder", { board: current.id, id: col.id, before }, "Reorder column");
}

function deleteColumn(current, col) {
  const others = columns(current).filter((c) => c.id !== col.id);
  if (!others.length) return toast("A board needs at least one column.", "warn");
  const target = $("select", { title: "Move its cards to", "aria-label": "Move its cards to" },
    ...others.map((c) => $("option", { value: c.id }, c.name)));
  const count = allCards(current).filter((c) => c.column === col.id).length;
  openModal(`Delete “${col.name}”?`, $("div", {},
    $("p", {}, count ? `${count} card(s) will move to:` : "This column is empty."),
    target),
    [$("button", { class: "ghost", onclick: () => { closeModal(); openSettings("columns"); } }, "Cancel"),
     $("button", { class: "danger", onclick: async () => {
       await send("column.delete", { board: current.id, id: col.id, to: target.value }, "Delete column");
       openSettings("columns");
     } }, "Delete column")]);
}

function renderBoardSettings(body) {
  for (const b of boardList()) {
    const title = $("input", { value: b.title, title: "Board title", "aria-label": "Board title", disabled: readOnly() });
    const key = $("input", { value: b.key, maxlength: "8", title: "Card key prefix", "aria-label": "Card key prefix", style: { textTransform: "uppercase" }, disabled: readOnly() });
    body.append($("div", { class: "setting-row" },
      $("span", { class: "mono note" }, b.id), title,
      $("label", { class: "inline" }, $("span", { class: "note" }, "key"), key),
      $("span", { class: "note" }, `${allCards(b).length} card(s)`),
      $("button", { class: "ghost", disabled: readOnly(), onclick: () => send("board.update", { board: b.id, title: title.value.trim(), key: key.value.toUpperCase() }, "Save board") }, "Save"),
      b.id === S.boardId ? $("button", { class: "primary", onclick: () => { S.boardId = b.id; closeModal(); render(); } }, "Open") : null,
      $("button", { class: "danger", disabled: readOnly(), onclick: async () => {
        const cards = allCards(b).length;
        if (!confirm(`Delete board “${b.title}” and its ${cards} card(s)?`)) return;
        await send("board.delete", { board: b.id }, "Delete board");
        openSettings("boards");
      } }, "Delete"),
    ));
  }
  body.append($("h3", {}, "New board"));
  const title = $("input", { placeholder: "Board title", title: "New board title", "aria-label": "New board title" });
  const key = $("input", { placeholder: "auto", title: "New board card key prefix", "aria-label": "New board card key prefix", style: { textTransform: "uppercase" } });
  const cols = $("input", { placeholder: "Backlog, In Progress, Done", title: "New board columns", "aria-label": "New board columns" });
  body.append($("div", { class: "setting-row add" }, title, key, cols,
    $("button", { class: "primary", disabled: readOnly(), onclick: async () => {
      if (!title.value.trim()) return;
      const columnsArg = cols.value.split(",").map((s) => s.trim()).filter(Boolean);
      const result = await send("board.create", {
        title: title.value.trim(), key: key.value.trim().toUpperCase() || undefined,
        columns: columnsArg.length ? columnsArg : undefined,
      }, "Create board");
      if (result) { S.boardId = result.id; openSettings("boards"); }
    } }, "Create board")));
}

function renderMetaSettings(body, kind) {
  const isLabel = kind === "label";
  const items = isLabel ? labels() : people();
  const op = isLabel ? "label" : "person";
  body.append($("p", { class: "note" },
    isLabel ? "Labels are global: any board can use them." : "People are global; deleting one unassigns their cards."));
  for (const item of items) {
    const name = $("input", { value: item.name, title: isLabel ? "Label name" : "Person name", "aria-label": isLabel ? "Label name" : "Person name", disabled: readOnly() });
    const hadColor = !!item.color;
    const color = $("input", { type: "color", value: item.color || FALLBACK_COLOR, title: isLabel ? "Label colour" : "Person colour", "aria-label": isLabel ? "Label colour" : "Person colour", disabled: readOnly() });
    const used = isLabel
      ? boardList().reduce((n, b) => n + allCards(b).filter((c) => (c.labels || []).includes(item.id)).length, 0)
      : boardList().reduce((n, b) => n + allCards(b).filter((c) => c.assignee === item.id).length, 0);
    body.append($("div", { class: "setting-row" },
      $("span", { class: "mono note" }, item.id), name, color,
      $("span", { class: "note" }, `${used} card(s)`),
      $("button", { class: "ghost", disabled: readOnly(), onclick: () => send(`${op}.update`, {
        id: item.id, name: name.value.trim(),
        // Same round trip as columns: the picker shows the fallback grey even
        // when the file says nothing, and writing it back is invented data.
        color: hadColor || color.value.toLowerCase() !== FALLBACK_COLOR.toLowerCase() ? color.value : "",
      }, "Save") }, "Save"),
      $("button", { class: "danger", disabled: readOnly(), onclick: () => send(`${op}.delete`, { id: item.id }, "Delete") }, "Delete"),
    ));
  }
  const name = $("input", { placeholder: isLabel ? "New label" : "New person", title: isLabel ? "New label name" : "New person name", "aria-label": isLabel ? "New label name" : "New person name" });
  const color = $("input", { type: "color", value: "#2f6f4f", title: isLabel ? "New label colour" : "New person colour", "aria-label": isLabel ? "New label colour" : "New person colour" });
  body.append($("div", { class: "setting-row add" }, name, color,
    $("button", { class: "primary", disabled: readOnly(), onclick: async () => {
      if (!name.value.trim()) return;
      await send(`${op}.create`, { name: name.value.trim(), color: color.value }, isLabel ? "Add label" : "Add person");
      openSettings(isLabel ? "labels" : "people");
    } }, "Add")));
}

function renderFileSettings(body) {
  const git = S.state.git;
  body.append(
    $("p", {}, $("code", {}, S.state.file)),
    $("p", { class: "note" }, `schema v${S.state.schema_version}, one file per card, `
      + `${(S.state.ready || []).length} ready, ${(S.state.conflicts || []).length} conflict(s)`
      + `${S.state.read_only ? ", read-only" : ""}`),
    $("h3", {}, "Git"),
    $("p", { class: "note" }, git ? (git.is_repo
      ? `branch ${git.branch || "?"} · ${git.is_dirty ? `${git.changed.length} uncommitted file(s)` : "clean"}${git.ahead ? ` · ${git.ahead} ahead of upstream` : ""}`
      : "this file is not inside a git repository, so committing is unavailable")
      : "git integration is switched off (--no-git)"),
    $("p", {}, "Editing the YAML in your editor is supported: the board reloads when the file changes."),
  );
}

/** Column options ride the popover machinery, so Escape and click-away have one owner. */
function openColumnMenu(event, current, col) {
  event.stopPropagation();
  const anchor = event.currentTarget;
  const item = (text, handler, extra) => $("button", {
    class: "menu-item" + (extra ? " " + extra : ""), disabled: readOnly(),
    onclick: () => { closePop(); handler(); },
  }, text);
  const menu = $("div", { class: "pop-group" },
    $("div", { class: "pop-title" }, col.name),
    item("Rename…", () => openRenameColumn(current, col)),
    item("Add card here", () => openNewCard(col.id)),
    item("Move left", () => moveColumn(current, col, "left")),
    item("Move right", () => moveColumn(current, col, "right")),
    item(col.is_done ? "Unmark as done" : "Mark as done column", () =>
      send("column.update", { board: current.id, id: col.id, is_done: !col.is_done }, "Column")),
    item(col.wip_limit ? `Clear WIP limit (${col.wip_limit})` : "Set WIP limit…", () =>
      col.wip_limit
        ? send("column.update", { board: current.id, id: col.id, wip_limit: 0 }, "Column")
        : openWipLimit(current, col)),
    item("Delete column…", () => deleteColumn(current, col), "danger"),
  );
  togglePop(anchor, menu);
}

function openRenameColumn(current, col) {
  const name = $("input", { value: col.name, title: "Column name", "aria-label": "Column name" });
  const hadColor = !!col.color;
  const color = $("input", { type: "color", value: col.color || FALLBACK_COLOR, title: "Column colour", "aria-label": "Column colour" });
  const save = async () => {
    await send("column.update", {
      board: current.id, id: col.id, name: name.value.trim(),
      color: hadColor || color.value.toLowerCase() !== FALLBACK_COLOR.toLowerCase() ? color.value : "",
    }, "Rename column");
    closeModal();
  };
  name.addEventListener("keydown", onEnter(save));
  openModal(`Column “${col.name}”`, $("div", {}, field("Name", name), field("Colour", color)),
    [$("button", { class: "ghost", onclick: closeModal }, "Cancel"), $("button", { class: "primary", onclick: save }, "Save")]);
}

function openWipLimit(current, col) {
  // The limit the column actually has, not a hard-coded 3: opening the dialog
  // on a column limited to 8 used to offer 3, and taking it set a real limit.
  const limit = $("input", { type: "number", min: "1", value: col.wip_limit ?? "", placeholder: "none" });
  const save = async () => {
    const raw = limit.value.trim();
    const n = Number(raw);
    if (raw !== "" && !(Number.isInteger(n) && n >= 1)) return toast("A WIP limit is a whole number of 1 or more.", "warn");
    await send("column.update", { board: current.id, id: col.id, wip_limit: raw === "" ? 0 : n }, "WIP limit");
    closeModal();
  };
  limit.addEventListener("keydown", onEnter(save));
  openModal(`WIP limit for “${col.name}”`,
    $("div", {}, field("Cards allowed before the column is flagged", limit)),
    [$("button", { class: "ghost", onclick: closeModal }, "Cancel"), $("button", { class: "primary", onclick: save }, "Set")]);
}

function openNewBoardDialog() {
  const title = $("input", { placeholder: "e.g. Personal", title: "Board title", "aria-label": "Board title" });
  const key = $("input", { placeholder: "auto (PER)", title: "Card key prefix", "aria-label": "Card key prefix", style: { textTransform: "uppercase" } });
  const cols = $("input", { value: "Backlog, In Progress, Done", title: "Columns", "aria-label": "Columns" });
  const create = async () => {
    if (!title.value.trim()) return toast("Give the board a title.", "warn");
    const result = await send("board.create", {
      title: title.value.trim(), key: key.value.trim().toUpperCase() || undefined,
      columns: cols.value.split(",").map((s) => s.trim()).filter(Boolean),
    }, "Create board");
    if (result) { S.boardId = result.id; closeModal(); render(); }
  };
  title.addEventListener("keydown", onEnter(create));
  openModal("New board", $("div", {},
    field("Title", title),
    field("Card key", key),
    field("Columns (comma separated)", cols),
    $("p", { class: "note" }, "Cards get ids like ", $("code", {}, "PER-1"), " using that key.")),
    [$("button", { class: "ghost", onclick: closeModal }, "Cancel"), $("button", { class: "primary", onclick: create }, "Create board")]);
}

function openCommitDialog() {
  const message = $("input", {
    title: "Commit message", "aria-label": "Commit message",
    value: `boards: update ${new Date().toISOString().slice(0, 10)}`,
  });
  const sendCommit = async () => {
    S.busy++; paintBusy();
    const { status, data } = await request("POST", "/api/commit", { message: message.value.trim() });
    S.busy--; paintBusy();
    if (status !== 200) return toast(data.error || "git commit failed", "error", 7000);
    closeModal();
    if (data.committed) toast(`Committed ${data.sha || ""} to ${data.branch || "the repo"}.`, "success", 5000);
    else toast("Nothing to commit — the file already matches HEAD.", "info");
    refresh({ silent: false });
  };
  message.addEventListener("keydown", onEnter(sendCommit));
  openModal("Commit the board", $("div", {},
    field("Commit message", message),
    S.state.git?.changed?.length ? $("p", { class: "note" }, "will stage: " + S.state.git.changed.join(", ")) : null,
    $("p", { class: "note" }, "Only .yamlboard/ is staged; code an agent is working on is left alone.")),
    [$("button", { class: "ghost", onclick: closeModal }, "Cancel"),
     $("button", { class: "primary", onclick: sendCommit }, "Commit")]);
}

function openHelp() {
  const rows = [
    ["n", "new card"], ["/", "search"], ["Enter on a card", "open it"],
    ["c", "claim the focused card as the agent named in the toolbar"],
    ["r", "show only ready cards — unclaimed, unblocked, nothing colliding"],
    ["a", "toggle archive view"], ["b / l", "board or list view"], ["d", "cards show less / more"],
    ["f", "filters"], ["t", "light / dark"], ["s", "settings"], ["?", "this panel"], ["Esc", "close"],
    ["drag", "move a card; drop order is written to the file"],
    ["click a pill", "change status, owner, priority, due or points — it saves at once"],
  ];
  openModal("Shortcuts", $("table", { class: "keys" },
    $("tbody", {}, ...rows.map(([key, text]) => $("tr", {}, $("td", {}, $("kbd", {}, key)), $("td", {}, text))))),
    [$("button", { class: "ghost", onclick: closeModal }, "Close")]);
}

/* ------------------------------------------------------------------ shortcuts */

document.addEventListener("keydown", (event) => {
  if (modalOpen()) return;
  const where = event.target.tagName;
  if (where === "INPUT" || where === "TEXTAREA" || where === "SELECT") {
    // Escape inside a popover's own dropdown closes the popover and hands focus
    // back to the toolbar button. Blurring alone strands focus on <body> and
    // leaves the panel open, which costs two Escape presses per opening.
    if (event.key === "Escape" && activePop?.node.contains(event.target)) {
      event.preventDefault();
      closePop();
      return;
    }
    if (event.key === "Escape") event.target.blur();
    return;
  }
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  const key = event.key.toLowerCase();
  if (key === "enter") {
    // cards are focusable, so Tab can land on one; Enter opens it
    const focused = document.activeElement?.closest?.(".card");
    if (focused) { event.preventDefault(); openCard(focused.dataset.id); }
    return;
  }
  if (key === "escape") { closePop(); return; }
  if (key === "/") { event.preventDefault(); els.search?.focus(); }
  else if (key === "n") { event.preventDefault(); openNewCard(columns(board())[0]?.id); }
  else if (key === "a") { S.showArchived = !S.showArchived; render(); }
  else if (key === "b") { S.view = "board"; savePref("yb.view", "board"); render(); }
  else if (key === "l") { S.view = "list"; savePref("yb.view", "list"); render(); }
  else if (key === "d") {
    const [low, mid, high] = DENSITIES.map(([value]) => value);
    setDensity({ [low]: mid, [mid]: high, [high]: low }[S.density]);
  }
  else if (key === "f") { event.preventDefault(); togglePop(els.filterButton, filterPop()); }
  else if (key === "s") { event.preventDefault(); openSettings(); }
  else if (key === "t") { applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"); }
  else if (key === "?") openHelp();
  // `r` and `c` are the two coordination verbs. Both also live in the help table
  // above and the README keyboard line — a shortcut nobody can discover is not one.
  else if (key === "r") toggleReadyFilter();
  else if (key === "c") { event.preventDefault(); claimFocusedCard(); }
});

/* ---------------------------------------------------------------- coordination */

/* Nothing below decides anything: it renders what the payload already said
 * (`state`, `live`, `overlaps`, `open_blockers`, `claim.left`) and posts intent.
 * The one thing computed here is the countdown between polls, from the absolute
 * expiry the server sent — a clock the browser is otherwise free to have wrong. */

/**
 * Milliseconds this browser's clock is ahead of the machine that owns leases.
 *
 * `claim.left` is measured over there, so pairing it with the absolute `expires`
 * it was computed against pins our local tick to it. Without this, a laptop four
 * minutes fast renders "26m left" for a 30m lease and the human frees work an
 * agent still believes it holds. Re-measured on every payload, which is also why
 * the countdown does not jump when a poll lands.
 */
let leaseSkew = 0;

/** Every card in a payload, whichever board it lives on. */
const payloadCards = (state) => ((state || {}).doc?.boards || []).flatMap((b) => b.cards || []);

/**
 * A YAML stamp as epoch milliseconds, or null for a value that is not one.
 *
 * Naive local time is this app's own convention, so a bare
 * `2026-11-23T23:10:04` reads as local. The fraction is sliced to three digits
 * because `Date.parse` need not accept the microseconds the writer emits, and a
 * silent NaN here would render every countdown as expired.
 */
function parseStamp(value) {
  const found = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?\.?(\d{0,3})/
    .exec(String(value || ""));
  if (!found) return null;
  const when = new Date(Number(found[1]), Number(found[2]) - 1, Number(found[3]),
    Number(found[4]), Number(found[5]), Number(found[6] || 0),
    Number(String(found[7] || "0").padEnd(3, "0")));
  return Number.isNaN(when.getTime()) ? null : when.getTime();
}

/** This clock, translated into the lease owner's frame. */
const leaseNow = () => Date.now() - leaseSkew;

/** Re-pin `leaseSkew` from the first live claim that carries both numbers. */
function syncLeaseClock(state) {
  for (const card of payloadCards(state)) {
    if (!card.live || !card.claim || typeof card.claim.left !== "number") continue;
    const until = parseStamp(card.claim.expires);
    if (until === null) continue;
    leaseSkew = Date.now() - (until - card.claim.left * 1000);
    return;
  }
  // No live claim to measure against, so the last answer stands: zeroing it here
  // would make a board where every agent just finished tick a stale countdown
  // further wrong instead of leaving nothing on screen at all.
}

/** `7300 -> "2h"`, `125 -> "2m"`, `59 -> "59s"` — the CLI's spelling. */
function humanLeft(seconds) {
  const left = Math.max(0, Math.round(seconds));
  if (left >= 3600) return `${Math.floor(left / 3600)}h${left % 3600 >= 60 ? `${Math.floor(left % 3600 / 60)}m` : ""}`;
  return left >= 60 ? `${Math.floor(left / 60)}m` : `${left}s`;
}

const clockTime = (ms) => new Date(ms).toLocaleTimeString([], {
  hour: "2-digit", minute: "2-digit", second: "2-digit",
});

/** Overlap reports naming this card, one sentence each, for a tooltip. */
function clashFor(cardId) {
  const lines = [];
  for (const report of (S.state?.conflicts || []).filter((r) => r.kind === "overlap")) {
    const { winner, loser } = report;
    if (winner.card !== cardId && loser.card !== cardId) continue;
    const other = winner.card === cardId ? loser : winner;
    const pairs = (report.patterns || []).map((p) => `${p.a} ↔ ${p.b}`).join(", ");
    lines.push(`${other.agent} on ${other.card} collides here (${pairs}) — ${winner.agent} claimed first and keeps it`);
  }
  return lines;
}

/** `ada · until 12:04:31 · src/api/**`, plus the collision if there is one. */
function claimTitle(card) {
  const claim = card.claim || {};
  const bits = [claim.agent || "someone"];
  const until = parseStamp(claim.expires);
  if (card.live) bits.push(`until ${until === null ? claim.expires : clockTime(until)}`);
  else if (claim.overridden_by) bits.push(`overridden by ${claim.overridden_by}`);
  else bits.push("expired");
  if ((claim.paths || []).length) bits.push(claim.paths.join(", "));
  return bits.concat(clashFor(card.id)).join(" · ");
}

/** `<avatar> 2m left`, or the `?` fallback for an agent the board never heard of. */
function claimNode(card) {
  const claim = card.claim || {};
  if (!claim.agent) return null;
  const who = person(claim.agent);
  const left = card.live && typeof claim.left === "number" ? claim.left : 0;
  const until = parseStamp(claim.expires);
  // Baked once per payload: the ticker needs one number to subtract, forever.
  const deadline = until === null ? leaseNow() + left * 1000 : until;
  const text = document.createTextNode(
    card.live ? `${humanLeft(left)} left` : (claim.overridden_by ? "overridden" : "expired"));
  const span = typeof claim.span === "number" ? claim.span : 0;
  // Amber from the first frame, not one tick later: a payload that arrives with
  // the lease already under a quarter of its own TTL must not render healthy and
  // then change colour while the reader is looking at it.
  const nearly = card.live && span > 0 && left <= span * 0.25;
  const node = $("span", {
    class: ["claim", card.live ? "" : "gone", card.overlaps ? "clashing" : "",
            !card.live || nearly ? "low" : ""].filter(Boolean).join(" "),
    style: { "--claim": who?.color || FALLBACK_COLOR },
    title: claimTitle(card),
    "data-deadline": String(deadline),
    "data-span": String(span),
  }, who ? avatar(who) : chip(null), text);
  node.$text = text;
  return node;
}

/** `blocked by WEB-1`, from ids the server already resolved. */
function blockerNode(card) {
  const open = (card.open_blockers || []).filter(Boolean);
  if (!open.length) return null;
  return $("span", {
    class: "blockers",
    title: `Waiting on ${open.join(", ")} to reach a done column`,
  }, icon("link"), `blocked by ${open.join(", ")}`);
}

const coordNodes = (card) => [blockerNode(card), claimNode(card)].filter(Boolean);

/**
 * Coordination classes a tile is born with, not painted on afterwards.
 *
 * Building them in beats toggling them in a second pass: the alternative is a
 * render that shows an unclaimed card for one frame before the claim appears on
 * it, which is precisely the lie the board exists to prevent.
 */
const coordClasses = (card) => [
  card.live ? "claimed" : "",
  card.state === "blocked" ? "blocked" : "",
  card.state === "ready" && !S.showArchived ? "ready-look" : "",
  card.overlaps ? "overlaps" : "",
];

/**
 * The agent's own colour as `--claim`, so a card being held reads as that
 * person at a glance and the chip, its tint and the tile's left edge cannot
 * disagree. The token falls back to `--muted` in CSS for a card nobody holds,
 * which is why an absent claim here is `null` rather than a guess.
 */
const claimColor = (card) => (card.claim && card.claim.agent ? person(card.claim.agent)?.color || FALLBACK_COLOR : null);

/**
 * One interval for every countdown on screen, patching text nodes only.
 *
 * `render()` here would rebuild the board once a second to change one number: it
 * throws away whatever text the human is partway through selecting, resets
 * scroll, and buys nothing the payload did not already say. The tick pauses with
 * the tab hidden for the same reason the poll does — a number that is wrong
 * while nobody can see it is a number that lies the moment they come back.
 */
let leaseTimer = null;

function startLeaseTicker() {
  if (leaseTimer) return;   // "Try again" must not stack tickers
  leaseTimer = setInterval(paintLeases, 1000);
}

function paintLeases() {
  if (document.visibilityState !== "visible") return;
  const now = leaseNow();
  for (const node of document.querySelectorAll(".claim")) {
    const deadline = Number(node.dataset.deadline);
    if (!Number.isFinite(deadline)) continue;
    const left = Math.max(0, (deadline - now) / 1000);   // clamped: never negative
    const span = Number(node.dataset.span) || 0;
    if (node.$text) node.$text.nodeValue = left <= 0 ? "expired" : `${humanLeft(left)} left`;
    node.classList.toggle("gone", left <= 0);
    // 25% of *this* claim's own TTL, not a hard-coded minute count: a 30s lease
    // and a 24h refactor card do not run out the same way.
    node.classList.toggle("low", left <= 0 || (span > 0 && left <= span * 0.25));
  }
}

/* Which expiries the human has already been told about, keyed card + claim.at so
   one expiry toasts exactly once and a retaken claim (which moves `at`) can be
   news again. Deliberately outside S.state: this is a memory of what was shown,
   not a reading of the files, and adopt() replaces that wholesale. */
const expirySeen = new Set();
/* Claims this tab watched the server call live. Release and expiry look identical
   once the claim key leaves the file, so only a claim still on disk and now inert
   counts as an expiry — the only one worth announcing, because a released card
   was freed on purpose. */
const seenAlive = new Set();
let expirySeeded = false;

const claimKey = (card) => `${card.id}|${(card.claim || {}).at ?? ""}`;

/**
 * Toast each lease that just ran out, and only the ones that just ran out.
 *
 * Opening a board that sat there for a week must dump nothing, so the first
 * payload seeds the sets silently. After that a transition is only believed once
 * the *server* stops calling the claim live — the local countdown reaching zero
 * first would toast a lease its owner renewed two seconds ago.
 */
function noteExpiries(state) {
  const seeding = !expirySeeded;
  expirySeeded = true;
  for (const card of payloadCards(state)) {
    const claim = card.claim;
    if (!claim || !claim.agent || claim.overridden_by) continue;
    const key = claimKey(card);
    if (card.live) {
      seenAlive.add(key);
      expirySeen.delete(key);      // renewed or retaken: it may be news again
      continue;
    }
    // Seeding, or a claim this tab never saw alive, was already dead when the
    // board opened: record it as told and say nothing.
    if (seeding || !seenAlive.has(key) || expirySeen.has(key)) { expirySeen.add(key); continue; }
    expirySeen.add(key);
    if (document.visibilityState !== "visible") continue;
    seenAlive.delete(key);
    toast(`${claim.agent}'s lease on ${card.id} expired — it is free again.`, "info", 6000);
  }
}

/* ---- who this browser is ---- */

const currentAgent = () => S.agent || people()[0]?.id || "";

/**
 * Refill the identity picker. Agent ids come from the file, so the stored choice
 * is validated rather than trusted: a person deleted in Settings must fall back
 * to the first entry, never to a name `claim.create` would then refuse.
 */
function syncAgent() {
  if (!els.agentSelect) return;
  const names = people().map((p) => [p.id, p.name]);
  if (!names.length) {
    els.agentSelect.textContent = "";
    els.agentSelect.append($("option", { value: "" }, "no people yet"));
    els.agentSelect.disabled = true;
    S.agent = "";
    return;
  }
  els.agentSelect.disabled = readOnly();
  S.agent = fillSelect(els.agentSelect, names, pref("yb.agent", names[0][0], names.map(([id]) => id)));
  savePref("yb.agent", S.agent);
}

/** A refused claim: the server's own lines, whose last one is the next move. */
function denyToast(data, label) {
  const lines = data.lines && data.lines.length ? data.lines : [data.error || `${label} was refused`];
  toast(lines.join("\n"), "warn", 9000);
}

async function claimCard(cardId, paths) {
  const card = cardById(cardId);
  if (!card) return null;
  const agent = currentAgent();
  if (!agent) {
    toast("Nobody is in this board yet — add a person in Settings (s) before claiming.", "warn", 7000);
    return null;
  }
  const result = await send("claim.create", { id: cardId, agent, paths: paths || [] }, `Claim ${cardId}`);
  if (result && result.unchanged) toast(`${cardId} is already yours.`, "info", 4000);
  else if (result) {
    const until = parseStamp(result.expires);
    const extra = (result.paths || []).length ? ` Holding ${result.paths.join(", ")}.` : "";
    toast(`${cardId} is yours${until === null ? "" : ` until ${clockTime(until)}`}.${extra}`, "success", 5000);
  }
  return result;
}

/**
 * The card modal's coordination block: who holds it, and the three moves a human
 * has on it — claim, release, finish.
 *
 * The agent is a `<select>` over `people` and never a text box, because an
 * identity the file has not heard of is a hard refusal downstream and a control
 * whose only output is an error is not a control. Paths are free text because
 * globs *are* the data; the server's `--paths` parser is the sole authority and
 * answers a loose directory with a refusal worded as instructions.
 */
function coordSection(card) {
  const claim = card.claim || {};
  const agent = currentAgent();
  // The payload's `state` is computed with no caller, so it says `taken` where
  // this browser would say `yours`; comparing ids here is the one judgement the
  // client is allowed to make, and only about which name it is wearing.
  const mine = !!card.live && claim.agent === agent;
  const status = $("p", { class: "claim-state" + (card.overlaps ? " overlaps" : "") },
    mine ? `You hold this card as ${agent}${(claim.paths || []).length ? `, claiming ${(claim.paths || []).join(", ")}` : ""}.`
      : card.live ? `${claim.agent} holds it until ${clockTime(parseStamp(claim.expires) ?? 0)}`
        + `${(claim.paths || []).length ? `, claiming ${(claim.paths || []).join(", ")}` : ""}.`
      : claim.overridden_by ? `${claim.agent}'s claim was overridden by ${claim.overridden_by}; the card is free again.`
      : card.state === "blocked" ? `Blocked by ${(card.open_blockers || []).join(", ")} — finish those first.`
      : "Nobody holds this card. Claim it before editing anything it covers.");
  const clashes = clashFor(card.id);
  const who = $("select", { "aria-label": "Claim this card as" },
    ...people().map((p) => $("option", { value: p.id, selected: p.id === agent }, p.name)));
  const paths = $("input", {
    type: "text", placeholder: "src/api/**, docs/notes.md", size: "26",
    "aria-label": "Paths this work will touch",
    title: "Comma-separated globs. A directory pattern needs /** on the end.",
  });
  const split = () => paths.value.split(",").map((p) => p.trim()).filter(Boolean);
  const button = (label, kind, onclick, title) => $(
    "button", { class: kind, disabled: readOnly() || !people().length, title, onclick }, label);
  const nobody = !people().length;
  const row = $("div", { class: "claim-row" },
    nobody ? $("span", { class: "note" }, "add a person in Settings (s) before claiming") : who,
    card.live ? null : paths,
    card.live || nobody ? null : button("Claim", "primary",
      () => claimCard(card.id, split()),
      `claim.create as ${agent} — the same op the CLI runs`),
    mine ? button("Renew", "ghost", async () => {
      const done = await send("claim.renew", { id: card.id, agent }, "Renew");
      if (done) toast(`${card.id} extended to ${clockTime(parseStamp(done.expires) ?? Date.now())}.`, "success", 5000);
    }, `Extend your lease on ${card.id} by one full TTL`) : null,
    card.live ? button("Release", "ghost", async () => {
      const done = await send("claim.release", { id: card.id, agent, force: !mine }, "Release");
      if (done && done.released === false) toast(`${card.id} was not claimed, so there was nothing to release.`, "info");
    }, mine ? "Give the card up now"
      : `Force-release ${claim.agent}'s live claim — recorded on the card as released_by ${agent}`) : null,
    nobody ? null : button(card.archived ? "Unarchive" : "Mark done", "ghost", async () => {
      if (card.archived) { await send("card.unarchive", { id: card.id }, "Unarchive"); return; }
      const done = await send("card.done", { id: card.id }, "Mark done");
      if (done) toast(`${card.id} finished${card.live ? " and its lease was released" : ""}.`, "success", 5000);
    }, card.archived ? "Restore it to the board" : "Move it to the done column and release any lease"));
  return section("Coordination", $("div", {}, status,
    clashes.length ? $("ul", { class: "claim-clash" }, ...clashes.map((line) => $("li", {}, line))) : null,
    readOnly() ? null : row));
}

function claimFocusedCard() {
  const tile = document.activeElement?.closest?.(".card");
  if (!tile) { toast("Focus a card first (Tab), then press c to claim it.", "info", 5000); return null; }
  return claimCard(tile.dataset.id, []);
}

function toggleReadyFilter() {
  S.readyOnly = !S.readyOnly;
  if (S.readyOnly && !(S.state?.ready || []).length) {
    toast("Nothing is ready — every card is claimed, blocked, or colliding.", "info", 5000);
  }
  renderBoard();
  paintCoordination();
}

/**
 * The toolbar's coordination readout: collisions git could not see, agents
 * holding something, and whether the ready filter is on.
 *
 * The conflict control leaves the DOM at zero instead of greying out. A disabled
 * button invites a click to find out why; an absent one says the board is clean,
 * which is the only thing the control is for.
 */
function paintCoordination() {
  if (!els.tools) return;
  const reports = S.state?.conflicts || [];
  const overlaps = reports.filter((r) => r.kind === "overlap");
  if (!reports.length) els.conflictButton.remove();
  else {
    if (!els.conflictButton.isConnected) els.tools.insertBefore(els.conflictButton, els.archiveToggle);
    // Presence is the signal: buildShell() hands this over with `hidden` set, so
    // the only way it ever reaches the screen is this branch clearing it.
    els.conflictButton.hidden = false;
    els.conflictBadge.textContent = String(reports.length);
    els.conflictBadge.hidden = false;
    els.conflictButton.title = overlaps.length
      ? `${overlaps.length} path overlap(s): whoever claimed first keeps it — click to resolve`
      : `${reports.length} board problem(s) git could not see`;
  }
  const agents = Object.keys(S.state?.agents || {}).length;
  els.footStats.textContent = [
    `${(S.state?.ready || []).length} ready`,
    agents ? `${agents} agent${agents === 1 ? "" : "s"} holding cards` : "",
    reports.length ? `${reports.length} conflict${reports.length === 1 ? "" : "s"}` : "no conflicts",
    S.readyOnly ? "showing ready only (r)" : "",
  ].map((bit) => (bit ? ` · ${bit}` : "")).join("");
  // The filter is a state of the board, not of a control, so it says itself on
  // the search row too: without this a colleague's screen just looks empty.
  els.search.placeholder = S.readyOnly ? "Search ready cards only" : "Search cards";
}

/**
 * The collision ledger: every report, who wins, and one Resolve per pair.
 *
 * Nothing is decided here. The winner is whichever claim started first, which the
 * server already worked out so that two clones agree without talking; Resolve is
 * the human telling it to write that down.
 */
function openConflicts() {
  const reports = S.state?.conflicts || [];
  const body = $("div", {});
  if (!reports.length) body.append($("p", { class: "note" }, "Nothing is colliding right now."));
  for (const report of reports) {
    if (report.kind === "overlap") { body.append(overlapRow(report)); continue; }
    body.append($("div", { class: "clash note-row" },
      $("strong", {}, report.kind === "duplicate"
        ? `${report.card} exists ${report.times} times`
        : `${report.card} — ${report.agent}`),
      $("p", { class: "note" }, report.detail
        || (report.kind === "duplicate"
          ? "Two clones minted the same id; delete or renumber one of them."
          : "Claims paths the working tree already has changed."))));
  }
  openModal(`Conflicts (${reports.length})`, body,
    [$("span", { class: "grow" }), $("button", { class: "ghost", onclick: closeModal }, "Close")], true);
}

function overlapRow(report) {
  const { winner, loser } = report;
  const side = (who, role) => $("div", { class: "clash-side " + role },
    $("div", { class: "clash-who" },
      person(who.agent) ? avatar(person(who.agent)) : chip(null),
      $("strong", {}, who.agent), $("span", { class: "mono note" }, who.card)),
    $("p", { class: "note" }, (who.paths || []).length ? who.paths.join(", ") : "no paths declared"),
    $("p", { class: "note" }, role === "win" ? `claimed first, ${humanTime(who.at)}` : `claimed ${humanTime(who.at)}`));
  const resolve = $("button", {
    class: "primary", disabled: readOnly(),
    title: `Mark ${loser.agent}'s claim on ${loser.card} overridden by ${winner.agent}, which frees the card`,
    onclick: async () => {
      const done = await send("conflicts.resolve", { card: loser.card }, "Resolve");
      if (!done) return;
      toast(`${loser.card} is free again — ${loser.agent}'s claim is marked overridden by ${winner.agent}.`,
        "success", 6000);
      // Rebuild from the fresh document so the remaining pairs stay addressable;
      // when the last one goes the control leaves the DOM and so does the dialog.
      if ((S.state?.conflicts || []).length) openConflicts(); else closeModal();
    },
  }, `Release ${loser.card}`);
  return $("div", { class: "clash" },
    $("div", { class: "clash-pair" },
      side(winner, "win"), $("span", { class: "clash-vs", "aria-hidden": "true" }, "vs"), side(loser, "lose")),
    $("p", { class: "clash-paths" }, (report.patterns || []).map((p) => $("code", {}, `${p.a} ↔ ${p.b}`))),
    $("p", { class: "note" }, "Earliest claim wins, so two clones that merged cleanly still name the same holder. Nothing resolves itself: until you release it, both agents believe they hold these files."),
    $("div", { class: "clash-foot" }, resolve));
}

/* ------------------------------------------------------------------ bootstrap */

/* A stored choice wins; the OS answer is a guess, so it is never written back. */
const chosenTheme = storedTheme();
applyTheme(chosenTheme || followSystemTheme(), { persist: !!chosenTheme });

/* Cheap external-edit detection: the server compares mtime/size on every poll.
   Self-rescheduling, because a poll that outlives its interval (git status runs
   inside every payload) would otherwise stack up and answer out of order.
   Started before the first fetch: the poll is also what notices the server
   coming back, so a boot that gave up first would never recover. */
let polling = false;
function startPolling() {
  if (polling) return;   // "Try again" must not double the traffic
  polling = true;
  (async function loop() {
    for (;;) {
      await new Promise((done) => setTimeout(done, 3000));
      if (modalOpen() || S.busy > 0 || draggingCard) continue;
      if (document.visibilityState !== "visible") continue;
      // The dot covers background traffic too, so a quiet dot is a claim about
      // the polls as well as about the person's own ops.
      S.busy++;
      paintBusy();
      try { await refresh(); } finally { S.busy--; paintBusy(); }
    }
  })();
}

async function start() {
  startPolling();
  // Its own interval, not a clause in the poll: three seconds is far too coarse
  // for "2m left" to tick, and a countdown that only moved on a poll would be a
  // strobe — while between polls is exactly where the human is deciding whether
  // to free the card.
  startLeaseTicker();
  try {
    const { status, data } = await request("GET", "/api/state");
    if (status !== 200) throw new Error(data.error || String(status));
    adopt(data);   // render() removes #boot
  } catch (error) {
    const note = document.querySelector("#boot .boot-note");
    note.textContent = "";
    note.append("could not load the board: " + error.message + " ");
    // The CLI opens this tab ~0.35 s before the socket is ready, and refuses to
    // serve a file that does not validate, so an early failure is often "not up
    // yet" rather than "broken". A dead-end screen means reloading by hand.
    note.append($("button", { class: "ghost", onclick: start }, "Try again"));
  }
}

start();
