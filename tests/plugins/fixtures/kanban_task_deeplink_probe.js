// Behavioral probe for the ``?task=t_xxx`` deep link: extracts readTaskParam() and
// writeTaskParam() from the shipped dashboard bundle (no build step — the bundle IS the
// source) and drives them against a stub window.location/history. Exits 0 and prints "PASS"
// when a ?task= URL yields that id (so KanbanPage opens its drawer on load), other params
// survive, and opening/closing the drawer writes the param back via replaceState.
// Run via: node kanban_task_deeplink_probe.js <path-to-bundle>
const fs = require("fs");

const src = fs.readFileSync(process.argv[2], "utf8");

function extract(name) {
  const start = src.indexOf(`function ${name}`);
  if (start === -1) { console.error(`${name} not found in bundle`); process.exit(1); }
  const bodyStart = src.indexOf("{", start);
  let depth = 0, end = bodyStart;
  for (; end < src.length; end++) {
    if (src[end] === "{") depth++;
    else if (src[end] === "}") { depth--; if (depth === 0) break; }
  }
  return src.slice(start, end + 1);
}

const constMatch = src.match(/const URL_TASK_PARAM = "[^"]+";/);
if (!constMatch) { console.error("URL_TASK_PARAM not found in bundle"); process.exit(1); }

const replaced = [];
function setUrl(href) {
  const u = new URL(href);
  global.window = {
    location: { href: u.href, search: u.search },
    history: {
      state: { k: 1 },
      replaceState(_state, _title, url) {
        replaced.push(url);
        const next = new URL(url, global.window.location.href);
        global.window.location = { href: next.href, search: next.search };
      },
    },
  };
}

const mod = new Function(
  `${constMatch[0]}\n${extract("readTaskParam")}\n${extract("writeTaskParam")}\n` +
  "return { readTaskParam, writeTaskParam };",
)();

function assert(cond, msg) { if (!cond) { console.error("FAIL: " + msg); process.exit(1); } }

const base = "http://host:9119/kanban?profile=default";

setUrl(base + "&task=t_2be807d3");
assert(mod.readTaskParam() === "t_2be807d3", "reads ?task= id");

setUrl(base);
assert(mod.readTaskParam() === null, "no param -> null (drawer stays closed)");
setUrl(base + "&task=");
assert(mod.readTaskParam() === null, "empty param -> null");

// Mount with a deep link: writing the same id back is a no-op.
setUrl(base + "&task=t_a");
mod.writeTaskParam("t_a");
assert(replaced.length === 0, "same id does not touch history");

// Clicking another card rewrites the param, keeping profile.
mod.writeTaskParam("t_b");
assert(replaced.pop() === "/kanban?profile=default&task=t_b", "open writes task param");

// Closing the drawer removes it, keeping profile.
mod.writeTaskParam(null);
assert(replaced.pop() === "/kanban?profile=default", "close removes task param");
mod.writeTaskParam(null);
assert(replaced.length === 0, "null with no param is a no-op");

console.log("PASS");
