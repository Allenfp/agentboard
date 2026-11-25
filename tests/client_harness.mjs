/* Loads the real shipped app.js in a stubbed DOM and prints one JSON object of
 * observed behaviour, for tests/test_client.py to assert on.
 *
 * Nothing here is a re-typed copy of the logic under test: app.js is read from
 * disk and evaluated as-is, so a change to the file changes these answers. The
 * stub is the minimum that survives app.js's bootstrap (theme, a poll and a
 * ticker whose timers are inert, a failed first fetch it recovers from by
 * showing a "Try again" button). If app.js starts touching a browser global the
 * stub does not have, this exits 3 with the ReferenceError rather than quietly
 * answering less.
 *
 * Usage: node client_harness.mjs path/to/app.js
 */
"use strict";
import fs from "node:fs";
import vm from "node:vm";

const target = process.argv[2];
if (!target) { console.error("usage: client_harness.mjs path/to/app.js"); process.exit(2); }

/* A tree real enough that reparenting behaves: the coordination block rebuilds
   itself in place, so `firstChild`/`remove`/`append` and an `#id` lookup have to
   agree with each other, or a test of "the paths box survived the repaint"
   passes on a stub that never moved anything. Deliberately tiny: no layout, no
   events bubbling, CSS matching limited to "#id", ".class" and [data-x="y"].

   Focus is real, though: detaching a node blurs whatever it held, the way a
   browser moves focus to <body> when the element under the caret disappears.
   Without that, a test of "the tile is still focused after a rebuild" passes on
   a stub where the old detached tile quietly keeps focus forever. */
let focused = null;
class FakeNode {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.nodeType = this.tagName ? 1 : 3;
    this.childNodes = [];
    this.parentNode = null;
    this.dataset = {};
    this.attrs = {};
    this.style = { setProperty(k, v) { this[k] = v; }, removeProperty(k) { delete this[k]; } };
    this.classList = {
      add: (...c) => { this._classes.add(...c); },
      remove: (...c) => { c.forEach((x) => this._classes.delete(x)); },
      toggle: (c, on) => { if (on === undefined) on = !this._classes.has(c); on ? this._classes.add(c) : this._classes.delete(c); },
      contains: (c) => this._classes.has(c),
    };
    this._classes = new Set();
  }
  get className() { return [...this._classes].join(" "); }
  set className(v) { this._classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get firstChild() { return this.childNodes[0] || null; }
  get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
  get textContent() {
    return this.nodeType === 3 ? String(this.data ?? "") : this.childNodes.map((n) => n.textContent).join("");
  }
  _blurIfInside() { if (focused && this.contains(focused)) { focused = null; document.activeElement = document.body; } }
  set textContent(v) { this._blurIfInside(); this.childNodes.forEach((n) => { n.parentNode = null; }); this.childNodes = []; if (v !== "") this.append(document.createTextNode(String(v))); }
  /* Strings are legal children: `paintGit` appends "git off" straight onto a
     button. Coerced to text nodes here, or a full render() throws on a stub
     limitation that looks exactly like a production crash. */
  append(...kids) { for (let k of kids.flat(9)) { if (typeof k !== "object" || !k) k = text(String(k)); if (k.parentNode) k.parentNode.childNodes.splice(k.parentNode.childNodes.indexOf(k), 1); k.parentNode = this; this.childNodes.push(k); } return this; }
  appendChild(k) { this.append(k); return k; }
  remove() { this._blurIfInside(); if (this.parentNode) this.parentNode.childNodes.splice(this.parentNode.childNodes.indexOf(this), 1); this.parentNode = null; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k.startsWith("data-")) this.dataset[k.slice(5)] = String(v); }
  getAttribute(k) { return this.attrs[k] ?? null; }
  contains(n) { for (let p = n; p; p = p.parentNode) if (p === this) return true; return false; }
  closest(sel) { for (let p = this; p; p = p.parentNode) if (p.matches?.(sel)) return p; return null; }
  /* One matcher behind `matches`/`closest` and `querySelectorAll`; they used to
     disagree. querySelectorAll grew a [data-id="WEB-2"] branch and matches never
     learned it, so `closest("[data-id]")` answered null inside a tree this same
     stub had just searched by that selector — and `render()` restores focus
     through exactly that call, so the gap reads as app.js losing the place.
     A bare [attr] means "has it", as in a browser. */
  matches(sel) { return this._matchOne(sel); }
  _matchOne(part) {
    if (part.startsWith("#")) return this.attrs.id === part.slice(1);
    if (part.startsWith(".")) return this._classes.has(part.slice(1));
    const attr = part.match(/^\[([\w-]+)(?:="(.*)")?\]$/);
    if (!attr) return this.tagName === part.toUpperCase();
    if (!attr[1].startsWith("data-"))
      return attr[2] === undefined ? attr[1] in this.attrs : this.attrs[attr[1]] === attr[2];
    const key = attr[1].slice(5);
    return attr[2] === undefined ? key in this.dataset : this.dataset[key] === attr[2];
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    const parts = String(sel).trim().split(/\s+/);
    let level = [this];
    for (const part of parts) {
      const next = [];
      const walk = (n) => { for (const c of n.childNodes) { if (c.nodeType === 1 && c._matchOne?.(part)) next.push(c); walk(c); } };
      level.forEach(walk);
      level = next;
    }
    return level;
  }
  addEventListener(type, fn) { (this._events ??= {})[type] = fn; if (type.startsWith("on")) this["on" + type] = fn; }
  removeEventListener() {}
  focus() { focused = this; document.activeElement = this; }
  blur() { if (focused === this) { focused = null; document.activeElement = document.body; } }
}

function el(tag = "div", attrs = {}, ...kids) {
  const node = new FakeNode(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(node.style, v);
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (v === true) node.setAttribute(k, "");
    else if (v !== null && v !== undefined && v !== false) node.setAttribute(k, String(v));
  }
  node.append(...kids);
  return node;
}

const text = (t) => Object.assign(new FakeNode(""), { nodeType: 3, data: String(t) });
const document = {
  documentElement: el("html"), body: el("body"), head: el("head"),
  activeElement: null, visibilityState: "visible",
  createElement: (t) => el(t), createElementNS: (ns, t) => el(t), createTextNode: text,
  querySelector: (sel) => document.documentElement.querySelectorAll(sel)[0]
    || document.body.querySelectorAll(sel)[0] || null,
  querySelectorAll: (sel) => [...document.documentElement.querySelectorAll(sel),
    ...document.body.querySelectorAll(sel)],
  getElementById: (id) => document.querySelector("#" + id),
  addEventListener() {}, removeEventListener() {},
  contains: (n) => document.documentElement.contains(n) || document.body.contains(n),
};
/* index.html's mount points, or buildShell() has nothing to build into. */
for (const id of ["root", "modal-root", "toast-root", "boot"]) document.body.append(el("div", { id }));
document.getElementById("boot").append(el("p", { class: "boot-note" }));

const prefs = new Map();
const ctx = vm.createContext({
  Node: FakeNode, document,
  window: { addEventListener() {}, matchMedia: () => ({ matches: false, addEventListener() {} }),
    location: { href: "http://127.0.0.1:1/" } },
  localStorage: {
    getItem: (k) => (prefs.has(k) ? prefs.get(k) : null),
    setItem: (k, v) => prefs.set(k, String(v)),
    removeItem: (k) => prefs.delete(k),
  },
  matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
  navigator: { clipboard: { writeText: async () => {} } },
  fetch: async () => ({ ok: true, status: 200, text: async () => "", json: async () => ({
    state: { doc: { people: [], labels: [], boards: [] }, tokens: {}, ready: [],
      conflicts: [], agents: {}, file: "/tmp/.agentboard", read_only: false,
      git: { is_repo: false } } }) }),
  requestAnimationFrame: () => 0,
  /* Inert on purpose: the poll is a self-rescheduling for(;;), so a setTimeout
     that ever ran its callback would spin the microtask queue forever. The
     harness drives adopt()/noteExpiries() directly instead of waiting on a poll. */
  setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  console, JSON, Math, Date, Number, String, Object, Array, RegExp, Set, Map, Promise, Error, Intl,
  CSS: { escape: (s) => String(s).replace(/[^\w-]/g, (c) => "\\" + c) },
});

try {
  vm.runInContext(fs.readFileSync(target, "utf8"), ctx, { filename: target });
} catch (error) {
  if (!/could not load the board/.test(String(error.message))) throw error;
}

const observed = vm.runInContext(`(function () {
  const PEOPLE = [{ id: "ada", name: "Ada", color: "#2f6f4f" },
                  { id: "grigory", name: "Grigory", color: "#d1495b" }];
  const board = (cards) => ({ doc: { people: PEOPLE, boards: [{ id: "web", key: "WEB",
    title: "Web", columns: [{ id: "todo", name: "To do" },
    { id: "done", name: "Done", is_done: true }], cards }] },
    people: PEOPLE, tokens: { board: "b1" }, conflicts: [], ready: [] });

  const r = {};

  /* assignee moves that a lease caused are not evidence of a human edit. */
  r.claim_sets_assignee = assigneeChange(
    { assignee: null, claim: null }, { assignee: "ada", claim: { agent: "ada" } });
  r.release_clears_assignee = assigneeChange(
    { assignee: "ada", claim: { agent: "ada" } }, { assignee: null, claim: null });
  r.handedit_under_a_lease = assigneeChange(
    { assignee: "ada", claim: { agent: "ada" } }, { assignee: "grigory", claim: { agent: "ada" } });
  r.unchanged_assignee = assigneeChange(
    { assignee: "ada", claim: { agent: "ada" } }, { assignee: "ada", claim: { agent: "ada" } });

  /* movedFiles: a claim-only poll must not read as "changed on disk". */
  const before = board([{ id: "WEB-1", title: "x", assignee: null, state: "ready" }]);
  const claimed = board([{ id: "WEB-1", title: "x", assignee: "ada", state: "taken",
    live: true, claim: { agent: "ada", left: 900, span: 1800 } }]);
  claimed.tokens = { board: "b1", "WEB-1": "t2" };
  const claimOnly = movedFiles(before, claimed);
  r.claim_only = { claims: claimOnly.claims, content: claimOnly.content,
                   rebuild: claimOnly.rebuild };
  const edited = JSON.parse(JSON.stringify(claimed));
  edited.doc.boards[0].cards[0].title = "a human renamed it";
  const withEdit = movedFiles(before, edited);
  r.human_edit = { claims: withEdit.claims, content: withEdit.content,
                   rebuild: withEdit.rebuild };
  const moved_ = board([{ id: "WEB-2", title: "new", state: "ready" }]);
  moved_.tokens = { board: "b1", "WEB-2": "t9" };
  r.card_appeared = movedFiles(before, moved_).added;

  /* the countdown's spelling, and its refusal to go negative */
  r.human_left = [-90, 0, 59, 60, 125, 3600, 3661, 7325].map(humanLeft);

  /* stamps the writer actually emits */
  r.stamps = {
    micros: parseStamp("2026-11-23T23:10:04.123456") !== null,
    naive: parseStamp("2026-11-23T23:10:04") !== null,
    space: parseStamp("2026-11-23 23:10:04") !== null,
    junk: parseStamp("tomorrow"),
    date_only: parseStamp("2026-11-23"),
  };

  /* A browser whose clock disagrees with the machine that owns the lease. The
     countdown must be the server's remaining time, so after syncing, leaseNow()
     has to land exactly on expires - left: the local clock cancels out rather
     than leaking into "26m left" for a 30m lease. */
  leaseSkew = 0;
  const until = parseStamp("2026-11-24T00:10:04");
  const skewed = board([{ id: "X", live: true,
    claim: { agent: "ada", expires: "2026-11-24T00:10:04", left: 1800 } }]);
  syncLeaseClock(skewed);
  r.skew_zeroed_to_server_frame = Math.abs(leaseNow() - (until - 1800 * 1000)) < 2;
  /* with no live claim to measure against, the last answer must stand: zeroing
     would make a board where everyone just finished tick a stale countdown wrong */
  const kept = leaseSkew;
  syncLeaseClock(board([]));
  r.skew_kept_without_a_claim = leaseSkew === kept;

  /* the amber threshold, on the first frame, per claim. S.state has to hold
     these cards' people: person() reads doc().people, and with a state from the
     bootstrap stub every chip silently takes the unknown-agent fallback. */
  S.state = board([
    { id: "WEB-9", state: "taken" }, { id: "WEB-8", state: "taken" },
    { id: "WEB-7", state: "taken" }]);
  const nearly = { id: "WEB-9", live: true,
    claim: { agent: "ada", expires: "2026-11-24T00:00:00", left: 449, span: 1800 }, overlaps: false };
  const healthy = { id: "WEB-8", live: true,
    claim: { agent: "ada", expires: "2026-11-24T00:00:00", left: 451, span: 1800 }, overlaps: false };
  const ghost = { id: "WEB-7", live: true,
    claim: { agent: "nobody", expires: "2026-11-24T00:00:00", left: 100, span: 1800 }, overlaps: true };
  const expired = { id: "WEB-6", live: false, claim: { agent: "ada" }, overlaps: false };
  const overridden = { id: "WEB-5", live: false, claim: { agent: "ada", overridden_by: "grigory" } };
  /* classList, the --claim colour and the tooltip are the observable surface;
     returning the node itself would serialise prototype getters as absent. */
  const look = (n) => n && ({
    classes: [...n._classes], claim: n.style["--claim"] || null,
    title: n.attrs.title || null, text: n.textContent });
  r.nearly = look(claimNode(nearly)); r.healthy = look(claimNode(healthy));
  r.ghost = look(claimNode(ghost)); r.expired = look(claimNode(expired));
  r.overridden = look(claimNode(overridden));
  r.no_claim_agent = claimNode({ id: "W", live: false, claim: {} }) === null;

  /* collision narration names both agents and both patterns */
  const clashState = { conflicts: [{ kind: "overlap",
    winner: { card: "PROD-2", agent: "ada" }, loser: { card: "PROD-3", agent: "grigory" },
    patterns: [{ a: "src/api/**", b: "src/api/routes.py" }] }], ready: [],
    doc: { people: PEOPLE, boards: [] } };
  S.state = clashState;
  r.clash_both = clashFor("PROD-3").join(" | ");
  r.clash_uninvolved = clashFor("PROD-9").length;
  r.title_shape = claimTitle({ id: "PROD-3", live: true, overlaps: true,
    claim: { agent: "grigory", expires: "2026-11-24T00:00:00", paths: ["src/api/routes.py"] } });

  /* coordination classes are on the tile at birth, and are classes */
  r.coord_classes = {
    plain: coordClasses({ state: "ready" }),
    claimed: coordClasses({ state: "taken", live: true, claim: { agent: "ada" } }),
    clashing: coordClasses({ state: "taken", live: true, claim: { agent: "ada" }, overlaps: true }),
    blocked: coordClasses({ state: "blocked", open_blockers: ["WEB-1"] }),
  };
  /* the toolbar's identity picker: built by the real shell, filled by the real
     syncAgent, so the aria-label and the default come from the shipped code */
  S.state = board([]);
  buildShell();
  syncAgent();
  const picker = els.agentSelect;
  const opts = [];
  const walk = (n) => {
    if (!n || typeof n !== "object") return;
    if (n.tagName === "OPTION") opts.push([n.value, n.textContent]);
    (n.children || []).forEach(walk);
  };
  walk(picker);
  r.select = { aria: picker.attrs["aria-label"], value: S.agent, options: opts,
    stored: localStorage.getItem("ab.agent"), isSelect: picker.tagName === "SELECT" };

  /* expiry announcements: one per expiry, one again after a renew, never a
     backlog on first load. Driven by the real noteExpiries + real toast(). */
  const shown = [];
  const realToast = toast;
  const NL = String.fromCharCode(10);   // "\\n" inside this template is a real newline
  toast = (msg) => shown.push(String(msg).split(NL)[0]);
  const alive = (id, at) => board([{ id, live: true,
    claim: { agent: "ada", at, expires: "2026-11-24T00:00:00", left: 900, span: 1800 } }]);
  const dead = (id, at) => board([{ id, live: false,
    claim: { agent: "ada", at, expires: "2026-11-24T00:00:00" } }]);
  document.visibilityState = "visible";
  noteExpiries(alive("WEB-1", "2026-11-23T23:00:00"));   // seeds: must say nothing
  r.backlog_on_first_load = shown.length;
  noteExpiries(dead("WEB-1", "2026-11-23T23:00:00"));    // the expiry itself
  r.first_expiry_toasts = shown.length;
  noteExpiries(dead("WEB-1", "2026-11-23T23:00:00"));    // still dead: silence
  r.repeat_same_expiry = shown.length;
  // A live renew cannot re-arm this card: api.claim_renew only restamps at
  // when the claim was already dead, so the key cannot survive a death here.
  // Asserted at the server level instead (TestLeaseLiveness); what the browser
  // guarantees is that one death toasts once and stays quiet after that.
  noteExpiries(dead("WEB-1", "2026-11-23T23:00:00"));
  noteExpiries(dead("WEB-1", "2026-11-23T23:00:00"));
  r.stays_quiet_after_one_death = shown.length;
  noteExpiries(alive("WEB-1", "2026-11-24T00:00:00"));   // retaken: at moved
  noteExpiries(dead("WEB-1", "2026-11-24T00:00:00"));    // a genuinely new expiry
  r.after_retake_new_at = shown.length;
  r.expiry_messages = shown;
  toast = realToast;

  /* The modal's own agent picker is the only place a person chooses who to act
     as, and every claim/renew/release button reads currentAgent() — so a pick
     that changes nothing means the card is claimed as somebody else. */
  const all = (root, tag) => {
    const found = [];
    const walk = (n) => {
      if (!n || typeof n !== "object") return;
      if (n.tagName === tag) found.push(n);
      (n.children || []).forEach(walk);
    };
    walk(root);
    return found;
  };
  const labels = (root) => all(root, "BUTTON").map((b) => b.textContent);
  const hasHandler = (s) => typeof (s._events?.change || s.onchange) === "function";
  const pick = (select, id) => (select._events?.change || select.onchange)?.({ target: { value: id } });

  r.modal_free_text = (() => {
    S.state = board([{ id: "WEB-1", title: "t", state: "ready", claim: {} }]);
    S.agent = "ada";
    const section = coordSection(cardById("WEB-1"));
    const select = all(section, "SELECT")[0];
    const typed = all(section, "INPUT")[0];
    typed.value = "src/api/**";                  // paths typed before switching
    if (!hasHandler(select)) return { handled: false };
    pick(select, "grigory");
    const survivors = all(section, "INPUT");
    return { handled: true, afterPick: currentAgent(),
      stored: localStorage.getItem("ab.agent"),
      pathsKept: survivors.length === 1 && survivors[0] === typed,
      pathsValue: survivors[0] ? survivors[0].value : null,
      buttons: labels(section) };
  })();

  r.modal_renew_follows_identity = (() => {
    S.state = board([{ id: "WEB-2", title: "t", state: "taken", live: true,
      claim: { agent: "grigory", at: "2026-11-23T23:00:00",
        expires: "2026-11-24T00:00:00", left: 900, span: 1800 } }]);
    S.agent = "grigory";                         // grigory really holds it
    const section = coordSection(cardById("WEB-2"));
    const asHolder = labels(section);
    pick(all(section, "SELECT")[0], "ada");      // now wearing ada
    return { asHolder, asStranger: labels(section), who: currentAgent() };
  })();

  /* A poll that rebuilds the board must not move the keyboard. This drives the
     real render(), so it fails if the restore is dropped -- and it can only pass
     at all because the stub blurs on detach, the way a browser does. */
  r.focus_survives_rebuild = (() => {
    S.state = board([{ id: "WEB-1", title: "a", column: "todo", state: "ready" },
                     { id: "WEB-2", title: "b", column: "todo", state: "ready" }]);
    S.state.ready = ["WEB-1", "WEB-2"];
    S.boardId = "web";
    buildShell();
    render();
    const tile = document.querySelector('[data-id="WEB-2"]');
    if (!tile) return { error: "the board rendered no tiles" };
    tile.focus();
    const before = document.activeElement?.dataset?.id || null;
    render();                                  // another writer; the poll rebuilds
    return { before,
             after: document.activeElement?.dataset?.id || document.activeElement?.tagName };
  })();

  /* A finished card is not offered as takeable. state_of has no word for it,
     so the tile reads the ready list -- the same source the filter reads. */
  r.finished_is_not_accented = (() => {
    S.showArchived = false;
    S.state = board([{ id: "WEB-1", title: "open", column: "todo", state: "ready" },
                     { id: "WEB-2", title: "shipped", column: "done", state: "ready" }]);
    S.state.ready = ["WEB-1"];                 // the server leaves the done card out
    S.boardId = "web";
    buildShell();
    render();
    const tile = (id) => document.querySelector('[data-id="' + id + '"]');
    return { open: tile("WEB-1")?._classes.has("ready-look"),
             done: tile("WEB-2")?._classes.has("ready-look") };
  })();
  const seen = new WeakSet();
  return JSON.stringify(r, (k, v) => {
    if (k === "parentNode") return undefined;
    if (v && typeof v === "object") { if (seen.has(v)) return "[circular]"; seen.add(v); }
    return v;
  });
})()`, ctx);

console.log(observed);
/* Stop here. bootstrap()'s async chain resumes in a microtask after this print
   and drives the full render() against a stub with no layout; the answers above
   are already taken, so running it would only risk a stub failure after a good
   result, which a caller cannot tell apart from a real one. */
process.exit(0);
