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
    assert.match(b.text, /step 3 was cut off/);
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
