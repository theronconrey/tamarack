// SPDX-License-Identifier: GPL-3.0-or-later
//
// What the Cockpit Overview says for each state of the pool. Runs with
// node's built-in test runner (node --test tests/cockpit/test_status.js), no
// packages.

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const S = require("../../data/cockpit/tamarack/status.js");

const TB = 1e12;

// 8 + 8 + 4 + 4 TB: a 4-drive parity layer of 4 TB slices (12 TB usable)
// and a 2-drive layer on the 8 TB drives' top halves (4 TB usable).
function layout() {
    return JSON.stringify({
        format: "tamarack-layout",
        version: 1,
        drives: [
            { device: "/dev/sda", size: 8 * TB },
            { device: "/dev/sdb", size: 8 * TB },
            { device: "/dev/sdc", size: 4 * TB },
            { device: "/dev/sdd", size: 4 * TB },
        ],
        layers: [
            { index: 1, level: "raid5", slice_bytes: 4 * TB, members: [0, 1, 2, 3].map(d => ({ drive: d, partition: 2 })) },
            { index: 2, level: "raid5", slice_bytes: 4 * TB, members: [0, 1].map(d => ({ drive: d, partition: 3 })) },
        ],
    });
}

function health(status, states = {}) {
    const drives = ["/dev/sda", "/dev/sdb", "/dev/sdc", "/dev/sdd"]
        .map(device => ({ device, state: states[device] || "ok", detail: "" }));
    return JSON.stringify({ status, degraded_layers: status === "degraded" ? [1] : [], drives });
}

function props(extra = {}) {
    return {
        State: "idle",
        Layout: layout(),
        DriveFailuresTolerated: 1,
        UsableBytes: 16 * TB,
        Health: health("ok"),
        Space: JSON.stringify({ size: 16 * TB, used: 6.2 * TB, free: 9.8 * TB }),
        OnBattery: false,
        LastBackupConfirmed: 0,
        SnapshotSchedule: { hourly: 24, daily: 7, weekly: 4, monthly: 6 },
        ...extra,
    };
}

test("sizes read the way drives are sold", () => {
    assert.equal(S.formatSize(16 * TB), "16 TB");
    assert.equal(S.formatSize(9.8 * TB), "9.8 TB");
    assert.equal(S.formatSize(500e9), "500 GB");
    assert.equal(S.formatSize(4.2e6), "4 MB");
    assert.equal(S.formatSize(1200), "1 KB");
    assert.equal(S.formatSize(5), "5 bytes");
});

test("a healthy pool says it is protected and asks for nothing", () => {
    const b = S.banner(S.read(props()));
    assert.equal(b.tone, "ok");
    assert.equal(b.title, "Your data is protected");
    assert.equal(b.action, undefined);
});

test("a missing drive names it, says what to buy and offers one action", () => {
    const b = S.banner(S.read(props({ Health: health("degraded", { "/dev/sdb": "missing" }) })));
    assert.equal(b.tone, "danger");
    assert.equal(b.title, "Drive 2 has failed");
    assert.match(b.text, /8 TB or more/);
    assert.equal(b.action.label, "Replace the drive");
});

test("a failing drive is a warning, not an emergency", () => {
    const b = S.banner(S.read(props({ Health: health("warning", { "/dev/sdc": "failing" }) })));
    assert.equal(b.tone, "warning");
    assert.equal(b.title, "Drive 3 is wearing out");
});

test("a nearly full pool asks for space, soon and then now", () => {
    const soon = S.banner(S.read(props({ Space: JSON.stringify({ size: 100, used: 88, free: 12 }) })));
    assert.equal(soon.title, "Your pool is 88% full");
    assert.match(soon.text, /Add space soon/);
    const now = S.banner(S.read(props({ Space: JSON.stringify({ size: 100, used: 96, free: 4 }) })));
    assert.match(now.text, /Add space now/);
});

test("a single drive is not protected against its own failure", () => {
    const b = S.banner(S.read(props({ DriveFailuresTolerated: 0 })));
    assert.equal(b.tone, "warning");
    assert.equal(b.title, "Not protected against a drive failure");
});

test("a running job shows its step and how far it is", () => {
    const job = { StepTitle: "Layer 1: widen single parity to 4 drives", Step: 3, StepCount: 6 };
    const b = S.banner(S.read(props({ State: "running" })), job);
    assert.equal(b.tone, "working");
    assert.match(b.text, /Step 3 of 6/);
    assert.match(b.text, /Keep the power on/);
    assert.ok(b.progress > 0 && b.progress < 1);
});

test("a job waiting for a person is never shown as healthy", () => {
    const b = S.banner(S.read(props({ State: "paused" })), { Message: "step 3 was cut off." });
    assert.equal(b.tone, "warning");
    assert.match(b.text, /step 3 was cut off/i);
});

test("no layout means no pool, not an error", () => {
    const b = S.banner(S.read(props({ Layout: "" })));
    assert.equal(b.tone, "neutral");
    assert.deepEqual(S.drives(S.read(props({ Layout: "" }))), []);
});

test("drives come in layout order with their health in words", () => {
    const rows = S.drives(S.read(props({ Health: health("degraded", { "/dev/sdb": "missing" }) })));
    assert.deepEqual(rows.map(r => [r.label, r.size, r.stateText]), [
        ["Drive 1", "8 TB", "Healthy"],
        ["Drive 2", "8 TB", "Missing: replace this drive"],
        ["Drive 3", "4 TB", "Healthy"],
        ["Drive 4", "4 TB", "Healthy"],
    ]);
});

test("capacity splits the drives into files, free and protection", () => {
    const cap = S.capacity(S.read(props()));
    assert.equal(cap.raw, 24 * TB);
    assert.equal(cap.protection, 8 * TB);  // 24 TB allocated, 16 TB usable
    assert.equal(cap.waiting, 0);
    assert.equal(cap.free, 9.8 * TB);
});

test("space a drive can't pair yet is shown as waiting", () => {
    const l = JSON.parse(layout());
    l.layers.pop();  // the 8 TB drives' top halves not yet paired
    const cap = S.capacity(S.read(props({ Layout: JSON.stringify(l), UsableBytes: 12 * TB })));
    assert.equal(cap.waiting, 8 * TB);
});

test("the care lines say what is looked after, in words", () => {
    const now = 1_800_000_000;
    const lines = S.care(S.read(props({ LastBackupConfirmed: now - 2 * 86400 })), now);
    assert.deepEqual(lines.map(l => l.value), [
        "every hour · kept 24 hours, 7 days, 4 weeks, 6 months",
        "confirmed 2 days ago",
        "mains power",
    ]);
    const off = S.care(S.read(props({ SnapshotSchedule: { hourly: 0, daily: 0, weekly: 0, monthly: 0 } })), now);
    assert.equal(off[0].value, "off");
});

// --- the Drives tab ----------------------------------------------------------

test("drive cards add what UDisks2 knows and what each drive holds", () => {
    const details = JSON.stringify([
        { device: "/dev/sda", model: "WDC WD80EFZZ", serial: "WD-A", size: 8 * TB, kind: "hdd",
          temperature_c: 38, power_on_hours: 3 * 8766 },
        { device: "/dev/sdc", model: "Fast SSD", serial: "S-C", size: 4 * TB, kind: "ssd",
          temperature_c: null, power_on_hours: 100 },
    ]);
    const cards = S.driveCards(S.read(props({ Drives: details })));
    assert.equal(cards.length, 4);
    assert.equal(cards[0].kind, "Hard drive");
    assert.equal(cards[0].temperature, "38 °C");
    assert.equal(cards[0].age, "about 3 years of use");
    assert.equal(cards[0].holds, "Holds 8 TB of your pool");
    assert.equal(cards[0].waiting, "");
    assert.equal(cards[2].kind, "SSD");
    assert.equal(cards[2].temperature, "");
    assert.equal(cards[2].age, "less than a month of use");
    assert.equal(cards[1].model, "");  // no details for /dev/sdb: the card still shows
});

test("a drive with unpaired space says so", () => {
    const l = JSON.parse(layout());
    l.layers.pop();
    const cards = S.driveCards(S.read(props({ Layout: JSON.stringify(l) })));
    assert.equal(cards[0].waiting, "4 TB waiting for a partner drive");
    assert.equal(cards[2].waiting, "");
});

test("hot drives are called out, by kind", () => {
    assert.deepEqual(S.temperature(56, "hdd"), { text: "56 °C, running hot", hot: true });
    assert.deepEqual(S.temperature(56, "ssd"), { text: "56 °C", hot: false });
    assert.equal(S.age(20 * 24 * 30.44).startsWith("20 months"), true);
});

test("the upgrade advisor puts the biggest gain first", () => {
    const upgrades = JSON.stringify({
        usable: 16 * TB,
        add: { size: 8 * TB, usable_after: 20 * TB },
        replace: [{ device: "/dev/sdc", size: 8 * TB, usable_after: 24 * TB }],
    });
    const advice = S.upgradeAdvice(S.read(props({ Upgrades: upgrades })));
    assert.equal(advice.best.text, "Replace Drive 3 (4 TB) with a drive of 8 TB");
    assert.equal(advice.best.gainText, "+8 TB for your files");
    assert.equal(advice.others[0].text, "Add a drive of 8 TB, if there is a free bay");
});

test("with every drive the same size, the advisor explains why one isn't enough", () => {
    const advice = S.upgradeAdvice(S.read(props({ Upgrades: JSON.stringify({ usable: 16 * TB, add: null, replace: [] }) })));
    assert.equal(advice.best, null);
    assert.match(advice.text, /second larger drive/);
    assert.equal(S.upgradeAdvice(S.read(props({ Upgrades: "{}" }))), null);
});

// --- the Shares tab ------------------------------------------------------------

test("share cards say who can connect and who is trusted", () => {
    const shares = JSON.stringify([{
        name: "photos", path: "/srv/tamarack/shares/photos", clients: ["192.168.1.0/24"],
        read_only: false, squash: "root", uid: 0, gid: 0,
        security: ["sec=sys trusts the user IDs each client sends", "clients can change files"],
    }, {
        name: "open", path: "/srv/tamarack/shares/open", clients: ["*"], read_only: true,
        security: ["open to a wide range of addresses; consider narrowing it"],
    }]);
    const cards = S.shareCards(S.read(props({ Shares: shares, HostName: "nas" })));
    assert.equal(cards[0].address, "nas:/srv/tamarack/shares/photos");
    assert.equal(cards[0].access, "Can change files");
    assert.equal(cards[0].who, "192.168.1.0/24");
    assert.equal(cards[0].trust[1], "Clients can change files.");
    assert.deepEqual(cards[0].ways.map(w => w.label), ["Linux computers"]);
    assert.equal(cards[0].tone, "ok");
    assert.equal(cards[1].access, "Read only");
    assert.equal(cards[1].tone, "warning");
});

// --- the Activity tab ------------------------------------------------------------

test("activity lists jobs in words, newest first, with what changed", () => {
    const now = Date.parse("2026-11-23T12:00:00Z") / 1000;
    const history = JSON.stringify([
        { id: "2", kind: "add", started: "2026-11-23T11:00:00Z", ended: "", result: "paused",
          message: "a command was cut off", step: 3, steps: 7, usable_before: 4 * TB, usable_after: 8 * TB },
        { id: "1", kind: "create", started: "2026-11-20T09:00:00Z", ended: "2026-11-20T09:05:00Z",
          result: "completed", message: "", step: 7, steps: 7, usable_before: 0, usable_after: 4 * TB },
    ]);
    const act = S.activity(S.read(props({ JobHistory: history })), now);
    assert.deepEqual(act.entries.map(e => [e.title, e.when, e.resultText]), [
        ["Added a drive", "1 hour ago", "Waiting for you"],
        ["Set up the pool", "3 days ago", "Done"],
    ]);
    assert.deepEqual(act.entries[0].lines, ["Step 3 of 7", "a command was cut off"]);
    assert.deepEqual(act.entries[1].lines, ["Space for your files: 4 TB"]);
    assert.equal(act.waiting.length, 1);
    assert.equal(S.ago("2026-11-22T11:00:00Z", now), "yesterday");
    assert.equal(S.ago("", now), "");
});

// --- changing shares ---------------------------------------------------------------

test("the share form checks the name and the clients before asking the engine", () => {
    const model = S.read(props({ Shares: JSON.stringify([{ name: "photos", clients: ["192.0.2.0/24"] }]) }));
    assert.deepEqual(Object.keys(S.checkShare({ name: "Photos!", clients: "" }, model, null)), ["name", "clients"]);
    assert.match(S.checkShare({ name: "photos", clients: "nas" }, model, null).name, /already a share/);
    assert.deepEqual(S.checkShare({ name: "docs", clients: "192.0.2.0/24" }, model, null), {});
    // An edit keeps its name, so only the clients are checked.
    assert.deepEqual(S.checkShare({ name: "", clients: "laptop" }, model, { name: "photos" }), {});
});

test("clients split on lines, commas and spaces; wide ones are flagged", () => {
    assert.deepEqual(S.splitClients(" 192.0.2.1,\n laptop  192.0.2.0/24\n"), ["192.0.2.1", "laptop", "192.0.2.0/24"]);
    for (const c of ["*", "0.0.0.0/0", "10.0.0.0/8", "*.lan", "2001:db8::/32"]) assert.equal(S.isWide(c), true, c);
    for (const c of ["192.168.1.0/24", "192.168.0.0/16", "laptop", "2001:db8::/64"]) assert.equal(S.isWide(c), false, c);
});

test("the preview says who is trusted, like the share card will", () => {
    const narrow = S.trustPreview({ clients: "192.168.1.0/24", readOnly: true }, null);
    assert.equal(narrow.tone, "ok");
    assert.deepEqual(narrow.notes.slice(1), [
        "Read-only for every client.",
        "Root on a client is treated as the anonymous user nobody.",
    ]);
    const wide = S.trustPreview({ clients: "*", readOnly: false }, null);
    assert.equal(wide.tone, "warning");
    assert.equal(wide.notes[3], "Open to a wide range of addresses; consider narrowing it.");
});

test("a new share belongs to the signed-in user; an edit keeps its owner", () => {
    const fresh = S.shareRequest({ name: "docs", clients: "192.0.2.0/24 laptop", readOnly: false }, { id: 1000, gid: 1000 }, null);
    assert.deepEqual(fresh, {
        name: "docs",
        options: {
            clients: { t: "as", v: ["192.0.2.0/24", "laptop"] },
            "read-only": { t: "b", v: false },
            squash: { t: "s", v: "root" },
            uid: { t: "u", v: 1000 },
            gid: { t: "u", v: 1000 },
        },
    });
    const existing = { name: "media", squash: "all", uid: 2000, gid: 3000, clients: ["*"], read_only: true };
    const edit = S.shareRequest({ name: "ignored", clients: "192.0.2.9", readOnly: true }, { id: 1000 }, existing);
    assert.equal(edit.name, "media");
    assert.deepEqual([edit.options.squash.v, edit.options.uid.v, edit.options.gid.v], ["all", 2000, 3000]);
});

test("engine errors come back in words", () => {
    assert.match(S.changeError({ name: "org.freedesktop.DBus.Error.AccessDenied", message: "not authorized" }),
                 /administrative access/);
    assert.equal(S.changeError({ name: "x.InvalidArgument", message: "not an address, network or host name: 'a b'" }),
                 "Not an address, network or host name: 'a b'.");
});

// --- a paused job -------------------------------------------------------------------

test("a paused job between steps offers Continue, and Cancel when allowed", () => {
    const job = { Message: "the UPS is on battery; resume when power is back", NeedsCheck: false, CanCancel: true };
    const b = S.banner(S.read(props({ State: "paused" })), job);
    assert.equal(b.title, "A storage change is waiting for you");
    assert.match(b.text, /^The UPS is on battery; resume when power is back\. Your files are available/);
    assert.deepEqual(b.action, { label: "Continue", op: "resume" });
    assert.deepEqual(b.secondary, { label: "Cancel this change", op: "cancel" });
    const noCancel = S.banner(S.read(props({ State: "paused" })), { ...job, CanCancel: false });
    assert.equal(noCancel.secondary, undefined);
});

test("a command nobody can check sends a person to a terminal, with no buttons", () => {
    const job = { Message: "step 3, command 2 was cut off and cannot be checked automatically", NeedsCheck: true, CanCancel: false };
    const b = S.banner(S.read(props({ State: "paused" })), job);
    assert.equal(b.title, "A storage change needs a check before it can continue");
    assert.match(b.text, /tamarack engine status/);
    assert.equal(b.action, undefined);
});

// --- the Snapshots tab ----------------------------------------------------------------

test("the snapshot schedule reads as one sentence", () => {
    assert.equal(S.scheduleSentence({ hourly: 24, daily: 7, weekly: 4, monthly: 6 }),
        "A snapshot of your shares every hour, thinned out as it ages: kept for 24 hours, 7 days, 4 weeks and 6 months.");
    assert.equal(S.scheduleSentence({ hourly: 1, daily: 0, weekly: 0, monthly: 0 }),
        "A snapshot of your shares every hour, thinned out as it ages: kept for 1 hour.");
    assert.match(S.scheduleSentence({ hourly: 0, daily: 0, weekly: 0, monthly: 0 }), /^Snapshots are off/);
});

test("snapshots are listed newest first, in words", () => {
    const now = Date.parse("2026-11-24T04:30:00Z") / 1000;
    const snaps = JSON.stringify([
        { number: 12, date: "2026-11-24T04:00:01Z", kind: "single", cleanup: "timeline", description: "timeline" },
        { number: 3, date: "2026-11-21T10:00:00Z", kind: "single", cleanup: "", description: "before moving photos" },
    ]);
    const model = S.read(props({ Snapshots: snaps }));
    assert.equal(S.snapshotSummary(model, now).count, "2 snapshots · newest 29 minutes ago · oldest 2 days ago");
    assert.deepEqual(S.snapshotRows(model, now).map(r => [r.when, r.label]), [
        ["29 minutes ago", "Scheduled"],
        ["2 days ago", "before moving photos"],
    ]);
    assert.match(S.snapshotSummary(S.read(props()), now).count, /^No snapshots yet/);
});

test("the schedule form takes whole numbers from 0 to 1000", () => {
    assert.deepEqual(S.checkSchedule({ hourly: "24", daily: "7", weekly: "4", monthly: "6" }), {});
    assert.deepEqual(Object.keys(S.checkSchedule({ hourly: "-1", daily: "1.5", weekly: "", monthly: "1001" })),
        ["hourly", "daily", "weekly", "monthly"]);
    assert.deepEqual(S.scheduleRequest({ hourly: " 48 ", daily: "7", weekly: "0", monthly: "12" }),
        { hourly: 48, daily: 7, weekly: 0, monthly: 12 });
});

// --- browsing a snapshot -------------------------------------------------------------

test("the breadcrumb leads back up to the top of the shares", () => {
    assert.deepEqual(S.breadcrumb("photos/2025"), [
        { label: "Shares", path: "" },
        { label: "photos", path: "photos" },
        { label: "2025", path: "photos/2025" },
    ]);
    assert.deepEqual(S.breadcrumb(""), [{ label: "Shares", path: "" }]);
});

test("a snapshot folder lists what can be opened and restored", () => {
    const listing = JSON.stringify({ entries: [
        { name: "2025", kind: "folder", size: 0, modified: 0 },
        { name: "latest", kind: "link", size: 0, modified: 0 },
        { name: "notes.txt", kind: "file", size: 2.5e9, modified: 0 },
    ], truncated: true });
    const view = S.browseRows(listing, "photos");
    assert.deepEqual(view.rows.map(r => [r.path, r.detail, r.canOpen, r.canRestore]), [
        ["photos/2025", "Folder", true, true],
        ["photos/latest", "Link (not restored on its own)", false, false],
        ["photos/notes.txt", "2.5 GB", false, true],
    ]);
    assert.match(view.truncated, /5000/);
    assert.equal(S.browseRows(JSON.stringify({ entries: [] }), "").empty, "This folder was empty.");
});

test("a restore says where the copy went", () => {
    assert.equal(S.restoredMessage("photos/notes (from 2026-11-20 14.00).txt"),
        "Restored as “notes (from 2026-11-20 14.00).txt” in photos, next to the original. Nothing was overwritten.");
    assert.match(S.restoredMessage("top (from 2026-11-20 14.00).txt"), /at the top of your shares/);
});

// --- drives coming and going ----------------------------------------------------------

function candidates(extra = []) {
    const pool = ["/dev/sda", "/dev/sdb", "/dev/sdc", "/dev/sdd"].map(device => ({ device, verdict: "pool" }));
    return JSON.stringify(pool.concat(extra));
}

test("a new drive that may join gets the banner when nothing else is wrong", () => {
    const fresh = { device: "/dev/sde", verdict: "suggest", size: 8 * TB, model: "WDC WD80", serial: "WD-ABCD1234", warnings: [], reasons: [] };
    const b = S.banner(S.read(props({ Candidates: candidates([fresh]) })));
    assert.equal(b.title, "A new 8 TB drive is ready");
    assert.match(b.text, /tamarack engine add \/dev\/sde/);
    const usb = { device: "/dev/sdf", verdict: "refuse", size: 1 * TB, reasons: ["it is connected by USB, which can drop out"] };
    assert.equal(S.banner(S.read(props({ Candidates: candidates([usb]) }))).title, "Your data is protected");
});

test("a failed drive with a big enough new one says how to rebuild onto it", () => {
    const fresh = { device: "/dev/sde", verdict: "suggest", size: 8 * TB };
    const b = S.banner(S.read(props({
        Health: health("degraded", { "/dev/sdc": "missing" }),
        Candidates: candidates([fresh]),
    })));
    assert.equal(b.tone, "danger");
    assert.match(b.text, /tamarack engine rebuild \/dev\/sdc \/dev\/sde/);
    const small = { device: "/dev/sde", verdict: "suggest", size: 2 * TB };
    const c = S.banner(S.read(props({
        Health: health("degraded", { "/dev/sdc": "missing" }),
        Candidates: candidates([small]),
    })));
    assert.match(c.text, /Replace it with a drive of 4 TB or more/);
});

test("other drives say whether they can be used, and why not", () => {
    const rows = S.otherDrives(S.read(props({ Candidates: candidates([
        { device: "/dev/sde", verdict: "warn", size: 2e12, model: "ST2000", serial: "Z1Z2Z3Z4", warnings: ["/dev/sde holds a 2.00 TB NTFS drive; joining the pool erases it"] },
        { device: "/dev/sdf", verdict: "refuse", size: 64e9, model: "Flash", serial: "", reasons: ["it is connected by USB"] },
    ]) })));
    assert.deepEqual(rows.map(r => [r.stateText, r.name, r.lines[0]]), [
        ["Can be added", "ST2000, serial …Z3Z4", "/dev/sde holds a 2.00 TB NTFS drive; joining the pool erases it"],
        ["Can't be used", "Flash", "It is connected by USB."],
    ]);
});
