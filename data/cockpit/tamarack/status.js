// SPDX-License-Identifier: GPL-3.0-or-later
//
// What the Overview says, worked out from the engine's D-Bus properties.
// Pure functions, no DOM and no Cockpit: tests/cockpit/test_status.js runs
// them under node. The page (tamarack.js) only draws what these return.
//
// Rules from docs/design/cockpit.md: one banner, one sentence, one action;
// never a storage-engineering question; only states the engine confirmed.

"use strict";

const TB = 1e12;
const GB = 1e9;

// Decimal units, the way drives are sold (tamarack.units.format_size).
function formatSize(bytes) {
    if (bytes >= TB) return trim(bytes / TB) + " TB";
    if (bytes >= GB) return trim(bytes / GB) + " GB";
    return Math.round(bytes / 1e6) + " MB";
}

function trim(n) {
    const s = n >= 10 ? n.toFixed(0) : n.toFixed(1);
    return s.endsWith(".0") ? s.slice(0, -2) : s;
}

function parse(text, fallback) {
    if (!text) return fallback;
    try {
        return JSON.parse(text);
    } catch (e) {
        return fallback;
    }
}

// The engine's properties, decoded once.
function read(props) {
    const layout = parse(props.Layout, null);
    return {
        layout,
        health: parse(props.Health, { status: "none", degraded_layers: [], drives: [] }),
        space: parse(props.Space, {}),
        tolerated: props.DriveFailuresTolerated || 0,
        usable: Number(props.UsableBytes || 0),
        state: props.State || "idle",
        onBattery: !!props.OnBattery,
        lastBackup: Number(props.LastBackupConfirmed || 0),
        schedule: props.SnapshotSchedule || {},
    };
}

// One row per drive, in the layout's order. Bays come with hardware
// bring-up; until then a drive is "Drive N" with its device name.
function drives(model) {
    if (!model.layout) return [];
    const byDevice = {};
    for (const d of model.health.drives || []) byDevice[d.device] = d;
    return model.layout.drives.map((d, i) => {
        const h = byDevice[d.device] || { state: "unknown", detail: "" };
        return {
            label: "Drive " + (i + 1),
            device: d.device,
            size: formatSize(d.size),
            state: h.state,
            stateText: {
                ok: "Healthy",
                failing: "Wearing out: replace soon",
                missing: "Missing: replace this drive",
                unknown: "Health unknown",
            }[h.state] || "Health unknown",
            tone: { ok: "ok", failing: "warning", missing: "danger" }[h.state] || "neutral",
            detail: h.detail || "",
        };
    });
}

// Where the drives' bytes go: files, free, protection, and space waiting
// for a partner drive. Sizes are decimal bytes.
function capacity(model) {
    if (!model.layout) return null;
    const raw = model.layout.drives.reduce((n, d) => n + d.size, 0);
    const allocated = model.layout.layers.reduce(
        (n, l) => n + l.slice_bytes * l.members.length, 0);
    const protection = Math.max(0, allocated - model.usable);
    const waiting = Math.max(0, raw - allocated);
    const used = model.space.used || 0;
    const free = model.space.free !== undefined ? model.space.free : Math.max(0, model.usable - used);
    const total = model.space.size || model.usable;
    return {
        raw,
        total,
        used,
        free,
        protection,
        // Partition reserves and metadata leave a little unallocated on
        // every drive; only call it "waiting" when it is real space.
        waiting: waiting > raw * 0.01 ? waiting : 0,
        fullness: total ? used / total : 0,
    };
}

function protectedText(tolerated) {
    if (tolerated >= 2) return "If any two drives fail, every file stays readable and nothing is lost.";
    return "If any one drive fails, every file stays readable and nothing is lost.";
}

// The banner: tone (ok, warning, danger, working, neutral), a title, one
// sentence, and at most one action.
function banner(model, job) {
    if (!model.layout) {
        return {
            tone: "neutral",
            title: "No storage pool yet",
            text: "Create one from a terminal for now: tamarack engine create, with the drives to use.",
        };
    }
    if (model.state === "running" && job) {
        return {
            tone: "working",
            title: "Working: " + (job.StepTitle || "a storage change"),
            text: "Step " + job.Step + " of " + job.StepCount + ". Your files stay available. " +
                "Keep the power on and don't remove any drive.",
            progress: job.StepCount ? Math.max(0, job.Step - 1) / job.StepCount : 0,
        };
    }
    if (model.state === "paused") {
        return {
            tone: "warning",
            title: "A storage change is waiting for you",
            text: (job && job.Message ? job.Message + " " : "") +
                "For now, continue it from a terminal: tamarack engine status says what it needs.",
        };
    }
    const rows = drives(model);
    const missing = rows.filter(d => d.state === "missing");
    const failing = rows.filter(d => d.state === "failing");
    if (model.health.status === "degraded") {
        return {
            tone: "danger",
            title: missing.length === 1 ? missing[0].label + " has failed" : "A drive has failed",
            text: "Every file is still readable, but another failure now would lose data. " +
                (missing.length === 1
                    ? "Replace it with a drive of " + missing[0].size + " or more."
                    : "Replace the failed drive."),
            action: { label: "Replace the drive", href: "#drives" },
        };
    }
    if (failing.length) {
        const d = failing[0];
        return {
            tone: "warning",
            title: d.label + " is wearing out",
            text: "It still works and your data is protected. Replace it soon, while every copy is intact.",
            action: { label: "Replace it", href: "#drives" },
        };
    }
    if (model.tolerated === 0) {
        const size = rows.length ? rows[0].size : "";
        return {
            tone: "warning",
            title: "Not protected against a drive failure",
            text: "Two copies are kept on one drive, which guards against bad spots but not a dead drive. " +
                "Add a second drive of " + size + " or more.",
        };
    }
    const cap = capacity(model);
    if (cap && cap.fullness >= 0.85) {
        const pct = Math.round(cap.fullness * 100);
        return {
            tone: "warning",
            title: "Your pool is " + pct + "% full",
            text: "Your data is protected. " +
                (cap.fullness >= 0.95 ? "Add space now: " : "Add space soon: ") +
                "put in another drive or replace a smaller one.",
            action: { label: "Ways to add space", href: "#drives" },
        };
    }
    return {
        tone: "ok",
        title: "Your data is protected",
        text: protectedText(model.tolerated) + " Nothing needs doing.",
    };
}

// The "Looking after your data" lines.
function care(model, now) {
    const s = model.schedule;
    const counts = [s.hourly, s.daily, s.weekly, s.monthly].map(n => n || 0);
    const snapshots = counts.every(n => n === 0)
        ? "off"
        : "every hour · kept " + [
            counts[0] && counts[0] + " hours",
            counts[1] && counts[1] + " days",
            counts[2] && counts[2] + " weeks",
            counts[3] && counts[3] + " months",
        ].filter(Boolean).join(", ");
    let backup = "none confirmed yet";
    if (model.lastBackup) {
        const days = Math.floor((now - model.lastBackup) / 86400);
        backup = days <= 0 ? "confirmed today" : days === 1 ? "confirmed yesterday" : "confirmed " + days + " days ago";
    }
    return [
        { label: "Snapshots", value: snapshots },
        { label: "Backup", value: backup },
        { label: "Power", value: model.onBattery ? "on battery now" : "mains power" },
    ];
}

const api = { formatSize, read, drives, capacity, banner, care };
if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
} else {
    window.TamarackStatus = api;
}
