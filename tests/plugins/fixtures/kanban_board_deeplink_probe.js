// Behavioral probe for ``?board=<slug>`` on card deep links and the drawer's not-found text.
// Extracts the real functions from the shipped dashboard bundle (the bundle IS the source)
// and drives them against a stub window. Exits 0 and prints "PASS" when the URL board wins
// over the localStorage pin (and ?profile= is never read as a board), an explicit ?board=
// follows board switches without ever being added, and a task 404 renders
// "Card <id> not found on board <slug>" while other errors pass through unchanged.
// Run via: node kanban_board_deeplink_probe.js <path-to-bundle>
const fs = require("fs");

const src = fs.readFileSync(process.argv[2], "utf8");

function extract(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start === -1) { console.error(`${name} not found in bundle`); process.exit(1); }
  const bodyStart = src.indexOf("{", src.indexOf(")", start));
  let depth = 0, end = bodyStart;
  for (; end < src.length; end++) {
    if (src[end] === "{") depth++;
    else if (src[end] === "}") { depth--; if (depth === 0) break; }
  }
  return src.slice(start, end + 1);
}

function constDecl(name) {
  const m = src.match(new RegExp(`const ${name} = "[^"]+";`));
  if (!m) { console.error(`${name} not found in bundle`); process.exit(1); }
  return m[0];
}

const replaced = [];
let stored = null;
function setUrl(href) {
  const u = new URL(href);
  global.window = {
    location: { href: u.href, search: u.search },
    localStorage: {
      getItem() { return stored; },
      setItem(_k, v) { stored = v; },
      removeItem() { stored = null; },
    },
    history: {
      state: null,
      replaceState(_state, _title, url) {
        replaced.push(url);
        const next = new URL(url, global.window.location.href);
        global.window.location = { href: next.href, search: next.search };
      },
    },
  };
}

const names = [
  "readSelectedBoard", "readBoardParam", "initialBoard", "syncBoardParam",
  "parseApiErrorMessage", "taskLoadErrorMessage",
];
const mod = new Function(
  [constDecl("LS_BOARD_KEY"), constDecl("URL_BOARD_PARAM"), ...names.map(extract)].join("\n") +
  `\nreturn { ${names.join(", ")} };`,
)();

function assert(cond, msg) { if (!cond) { console.error("FAIL: " + msg); process.exit(1); } }

const base = "http://host:9119/kanban?profile=default";

// --- initial board: URL ?board= beats the localStorage pin -------------------
stored = "pinned";
setUrl(base + "&task=t_a&board=ops");
assert(mod.initialBoard() === "ops", "?board= wins over the localStorage pin");
assert(stored === "pinned", "?board= is not persisted over the pin");

setUrl(base + "&task=t_a");
assert(mod.initialBoard() === "pinned", "no ?board= -> localStorage pin");
setUrl(base + "&task=t_a&board=");
assert(mod.initialBoard() === "pinned", "empty ?board= -> localStorage pin");

stored = null;
setUrl("http://host:9119/kanban?profile=ops&task=t_a");
assert(mod.initialBoard() === null, "?profile= is never read as the board");

// --- explicit ?board= follows board switches, never added --------------------
setUrl(base + "&task=t_a&board=ops");
mod.syncBoardParam("ops");
assert(replaced.length === 0, "same board does not touch history");
mod.syncBoardParam("default");
assert(replaced.pop() === "/kanban?profile=default&task=t_a&board=default",
  "switching boards rewrites an explicit ?board=");
setUrl(base + "&task=t_a");
mod.syncBoardParam("ops");
assert(replaced.length === 0, "?board= is not added when the link had none");

// --- drawer error text -------------------------------------------------------
function apiErr(status, message) { const e = new Error(message); e.status = status; return e; }

const nf = mod.taskLoadErrorMessage(apiErr(404, "task t_x not found"), "t_x", "ops");
assert(/^Card t_x not found on board ops\b/.test(nf), "task 404 names card + board: " + nf);
assert(/switch to the board/i.test(nf), "task 404 hints at switching board: " + nf);

const legacy = mod.taskLoadErrorMessage(new Error('404: {"detail":"task t_x not found"}'), "t_x", null);
assert(/^Card t_x not found on board default\b/.test(legacy), "legacy '404:' error + no board -> default: " + legacy);

const boardGone = mod.taskLoadErrorMessage(apiErr(404, "board 'ops' does not exist"), "t_x", "ops");
assert(boardGone === "board 'ops' does not exist", "unknown-board 404 keeps its own message");

const other = mod.taskLoadErrorMessage(apiErr(500, "Internal server error"), "t_x", "ops");
assert(other === "Internal server error", "non-404 errors pass through unchanged");

console.log("PASS");
