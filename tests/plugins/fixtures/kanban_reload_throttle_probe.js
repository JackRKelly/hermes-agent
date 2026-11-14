// Behavioral probe for the board's live-event reloads: extracts eventsNeedBoardReload()
// and createReloadThrottle() from the shipped dashboard bundle (no build step: the bundle
// IS the source) and drives them on a fake clock. Exits 0 and prints "PASS" when a
// heartbeat-only batch does not reload, the first reload after a quiet spell fires within
// 250 ms, a steady event stream reloads at most once per 3 s (trailing, never dropping the
// last change), and cancel() drops a pending reload.
// Run via: node kanban_reload_throttle_probe.js <path-to-bundle>
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

const names = ["eventsNeedBoardReload", "createReloadThrottle"];
const mod = new Function(names.map(extract).join("\n") + `\nreturn { ${names.join(", ")} };`)();

// Fake clock: timers fire in due order as time advances.
let now = 100000;
let seq = 0;
let timers = [];
const clock = {
  now() { return now; },
  setTimeout(fn, ms) { const id = ++seq; timers.push({ id, at: now + ms, fn }); return id; },
  clearTimeout(id) { timers = timers.filter((t) => t.id !== id); },
};
function advance(ms) {
  const end = now + ms;
  for (;;) {
    timers.sort((a, b) => a.at - b.at);
    const next = timers[0];
    if (!next || next.at > end) break;
    timers.shift();
    now = next.at;
    next.fn();
  }
  now = end;
}

const failures = [];
function check(cond, msg) { if (!cond) failures.push(msg); }

// 1. Heartbeats alone never reload; any other kind in the batch does.
check(!mod.eventsNeedBoardReload([{ kind: "heartbeat" }, { kind: "heartbeat" }]), "heartbeat-only batch reloads");
check(mod.eventsNeedBoardReload([{ kind: "heartbeat" }, { kind: "status_changed" }]), "mixed batch does not reload");
check(mod.eventsNeedBoardReload([{ kind: "commented" }]), "comment batch does not reload");

// 2. First reload after quiet: <= 250 ms.
const loads = [];
const throttle = mod.createReloadThrottle(clock, 250, 3000);
const load = () => loads.push(now);
throttle.schedule(load);
advance(249);
check(loads.length === 0, "reload fired before the 250 ms quiet delay");
advance(1);
check(loads.length === 1, `first reload did not fire at 250 ms (loads=${loads.length})`);

// 3. Steady stream: one event every 200 ms for 12 s => at most one load per 3 s.
const streamStart = now;
for (let t = 0; t < 12000; t += 200) { throttle.schedule(load); advance(200); }
advance(3000);  // let the trailing reload land
const streamLoads = loads.filter((at) => at > streamStart);
for (let i = 1; i < loads.length; i++) {
  check(loads[i] - loads[i - 1] >= 3000, `two reloads ${loads[i] - loads[i - 1]} ms apart`);
}
check(streamLoads.length >= 4 && streamLoads.length <= 5, `stream produced ${streamLoads.length} reloads in 12 s`);
check(loads[loads.length - 1] >= streamStart + 12000 - 200, "the last change in the stream was dropped");

// 4. After a quiet spell longer than the interval, the next reload is fast again.
advance(10000);
const before = loads.length;
throttle.schedule(load);
advance(250);
check(loads.length === before + 1, "reload after a quiet spell was not fast");

// 5. The newest load function wins (filters changed while a reload was pending).
const seen = [];
throttle.schedule(() => seen.push("old"));
throttle.schedule(() => seen.push("new"));
advance(3000);
check(seen.join(",") === "new", `pending reload ran ${seen.join(",")}`);

// 6. cancel() drops a pending reload.
const afterCancel = loads.length;
advance(10000);
throttle.schedule(load);
throttle.cancel();
advance(5000);
check(loads.length === afterCancel, "cancelled reload still fired");

if (failures.length) {
  console.log("FAIL\n" + failures.join("\n"));
  process.exit(1);
}
console.log("PASS");
