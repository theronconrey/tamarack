// SPDX-License-Identifier: GPL-3.0-or-later
//
// What the page says, worked out from the engine's D-Bus properties.
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
        details: parse(props.Drives, []),
        upgrades: parse(props.Upgrades, {}),
        history: parse(props.JobHistory, []),
        shares: parse(props.Shares, []),
        hostName: props.HostName || "",
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

// --- the Drives tab -------------------------------------------------------

function plural(n, word) {
    return n + " " + word + (n === 1 ? "" : "s");
}

// How long a drive has been powered on, in words.
function age(hours) {
    if (hours === null || hours === undefined) return "";
    const days = hours / 24;
    if (days < 30) return "less than a month of use";
    const months = Math.floor(days / 30.44);
    if (months < 24) return plural(months, "month") + " of use";
    return "about " + plural(Math.round(months / 12), "year") + " of use";
}

// Hard drives are uncomfortable above about 55 °C, SSDs above about 70 °C.
function temperature(celsius, kind) {
    if (celsius === null || celsius === undefined) return { text: "", hot: false };
    const hot = celsius >= (kind === "ssd" ? 70 : 55);
    return { text: celsius + " °C" + (hot ? ", running hot" : ""), hot };
}

function allocatedOn(layout, index) {
    let bytes = 0;
    for (const layer of layout.layers) {
        for (const m of layer.members) if (m.drive === index) bytes += layer.slice_bytes;
    }
    return bytes;
}

// One card per drive: the Overview's row plus what UDisks2 knows and what
// the drive holds.
function driveCards(model) {
    if (!model.layout) return [];
    const byDevice = {};
    for (const d of model.details || []) byDevice[d.device] = d;
    return drives(model).map((row, i) => {
        const info = byDevice[row.device] || {};
        const size = model.layout.drives[i].size;
        const used = allocatedOn(model.layout, i);
        const spare = size - used;
        const heat = temperature(info.temperature_c, info.kind);
        return {
            ...row,
            model: info.model || "",
            serial: info.serial || "",
            kind: { hdd: "Hard drive", ssd: "SSD" }[info.kind] || "",
            temperature: heat.text,
            hot: heat.hot,
            age: age(info.power_on_hours),
            holds: "Holds " + formatSize(used) + " of your pool",
            waiting: spare > size * 0.01 ? formatSize(spare) + " waiting for a partner drive" : "",
        };
    });
}

// The upgrade advisor: the best single change, then the others, each with
// the space it adds. The engine works the numbers out with its planners.
function upgradeAdvice(model) {
    const up = model.upgrades;
    if (!model.layout || !up || up.usable === undefined) return null;
    const labels = {};
    for (const r of drives(model)) labels[r.device] = r;
    const options = [];
    for (const r of up.replace || []) {
        const row = labels[r.device];
        if (!row) continue;
        options.push({
            text: "Replace " + row.label + " (" + row.size + ") with a drive of " + formatSize(r.size),
            gain: r.usable_after - up.usable,
        });
    }
    if (up.add) {
        options.push({
            text: "Add a drive of " + formatSize(up.add.size) + ", if there is a free bay",
            gain: up.add.usable_after - up.usable,
        });
    }
    options.sort((a, b) => b.gain - a.gain);
    for (const o of options) o.gainText = "+" + formatSize(o.gain) + " for your files";
    if (!options.length) {
        return {
            best: null,
            others: [],
            text: "Every drive is the same size, so one larger drive adds no space by itself: " +
                "its extra space waits until a second larger drive joins it.",
        };
    }
    return {
        best: options[0],
        others: options.slice(1),
        text: "A larger drive than this adds no more by itself; the extra waits for a partner.",
    };
}

// --- the Shares tab -------------------------------------------------------

// The engine's notes as sentences; a leading option name (sec=sys) keeps its case.
function sentence(text) {
    if (!text) return "";
    const first = /^\w+=/.test(text) ? text.charAt(0) : text.charAt(0).toUpperCase();
    return first + text.slice(1) + ".";
}

function shareCards(model) {
    const host = model.hostName || "this machine";
    return (model.shares || []).map(s => {
        const security = s.security || [];
        return {
            name: s.name,
            address: host + ":" + s.path,
            access: s.read_only ? "Read only" : "Can change files",
            who: (s.clients || []).join(", "),
            ways: [{ label: "Linux computers", detail: "Connect over NFS from the addresses listed" }],
            trust: security.map(sentence),
            tone: security.some(t => t.indexOf("wide range") >= 0) ? "warning" : "ok",
        };
    });
}

// --- the Activity tab -----------------------------------------------------

const JOB_TITLES = {
    create: "Set up the pool",
    add: "Added a drive",
    replace: "Replaced a drive",
    rebuild: "Rebuilt onto a new drive",
};

const RESULTS = {
    completed: ["Done", "ok"],
    failed: ["Stopped with an error", "danger"],
    cancelled: ["Cancelled", "neutral"],
    running: ["Working", "working"],
    waiting: ["Working", "working"],
    paused: ["Waiting for you", "warning"],
    unreadable: ["Its record can't be read", "danger"],
};

// "5 minutes ago", "3 hours ago", "yesterday", "12 days ago".
function ago(iso, now) {
    const then = Date.parse(iso) / 1000;
    if (!iso || Number.isNaN(then)) return "";
    const seconds = Math.max(0, now - then);
    if (seconds < 60) return "just now";
    if (seconds < 3600) return plural(Math.floor(seconds / 60), "minute") + " ago";
    if (seconds < 86400) return plural(Math.floor(seconds / 3600), "hour") + " ago";
    const days = Math.floor(seconds / 86400);
    return days === 1 ? "yesterday" : days + " days ago";
}

function activity(model, now) {
    const entries = (model.history || []).map(h => {
        const [resultText, tone] = RESULTS[h.result] || ["Unknown", "neutral"];
        const lines = [];
        if (!h.ended && h.steps) lines.push("Step " + h.step + " of " + h.steps);
        if (h.result === "completed" && h.usable_after && h.usable_after !== h.usable_before) {
            lines.push(h.usable_before
                ? "Space for your files: " + formatSize(h.usable_before) + " → " + formatSize(h.usable_after)
                : "Space for your files: " + formatSize(h.usable_after));
        }
        if (h.message) lines.push(h.message);
        return {
            id: h.id,
            title: JOB_TITLES[h.kind] || "A storage change",
            when: ago(h.ended || h.started, now),
            exact: h.ended || h.started || "",
            resultText,
            tone,
            lines,
        };
    });
    return {
        waiting: entries.filter(e => e.tone === "warning"),
        entries,
        alerts: "Alerts (a drive wearing out, failing or missing) are written to the " +
            "system log as they happen. Email alerts come later.",
    };
}

const api = {
    formatSize, read, drives, capacity, banner, care,
    age, temperature, driveCards, upgradeAdvice, shareCards, ago, activity,
};
if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
} else {
    window.TamarackStatus = api;
}
