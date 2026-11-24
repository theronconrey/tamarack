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
    if (bytes >= 1e6) return Math.round(bytes / 1e6) + " MB";
    if (bytes >= 1e3) return Math.round(bytes / 1e3) + " KB";
    return bytes === 1 ? "1 byte" : bytes + " bytes";
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
        snapshots: parse(props.Snapshots, []),
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
        const why = job && job.Message ? sentence(job.Message.replace(/\.$/, "")) + " " : "";
        if (!job || job.NeedsCheck) {
            // A command was cut off and nothing can tell whether it took
            // effect: a person has to look. Never ask that on this page.
            return {
                tone: "warning",
                title: "A storage change needs a check before it can continue",
                text: why + "Your files are available. Someone needs to look from a terminal: " +
                    "tamarack engine status says what to check.",
            };
        }
        return {
            tone: "warning",
            title: "A storage change is waiting for you",
            text: why + "Your files are available. Continue when you're ready.",
            action: { label: "Continue", op: "resume" },
            secondary: job.CanCancel ? { label: "Cancel this change", op: "cancel" } : undefined,
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
            share: s,
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

// --- changing shares ------------------------------------------------------
//
// The form only gathers three things: a name, who can connect, and whether
// they can change files. Everything else has a safe default: root on a
// client counts as nobody, and a new share's folder belongs to the person
// signed in to Cockpit. The engine validates everything again.

const SHARE_NAME = /^[a-z0-9][a-z0-9._-]{0,63}$/;

// The same notes as tamarack.shares.Share.security(); tests/test_shares.py
// checks the wording still matches.
const SEC_SYS_NOTE = "sec=sys trusts the user IDs each client sends: anyone in control of an " +
    "allowed client can act as any user on this share";
const WIDE_NOTE = "open to a wide range of addresses; consider narrowing it";

function splitClients(text) {
    return String(text || "").split(/[\s,]+/).map(c => c.trim()).filter(Boolean);
}

function isWide(client) {
    if (client === "*" || client === "0.0.0.0/0" || client === "::/0") return true;
    const slash = client.indexOf("/");
    if (slash > 0) {
        const prefix = Number(client.slice(slash + 1));
        if (!Number.isInteger(prefix)) return false;
        return prefix < (client.indexOf(":") >= 0 ? 48 : 16);
    }
    return client.indexOf("*") >= 0;
}

// What the form's answers mean, as the share card will say it.
function trustPreview(form, existing) {
    const squash = existing ? existing.squash : "root";
    const notes = [SEC_SYS_NOTE, form.readOnly ? "read-only for every client" : "clients can change files"];
    if (squash === "root") notes.push("root on a client is treated as the anonymous user nobody");
    else if (squash === "all") notes.push("every client user is treated as uid " + existing.uid + ", gid " + existing.gid);
    else notes.push("root on a client is root on this share (no squashing)");
    const wide = splitClients(form.clients).some(isWide);
    if (wide) notes.push(WIDE_NOTE);
    return { notes: notes.map(sentence), tone: wide ? "warning" : "ok" };
}

// Problems to fix before asking the engine, keyed by field.
function checkShare(form, model, existing) {
    const errors = {};
    const name = String(form.name || "");
    if (!existing) {
        if (!SHARE_NAME.test(name)) {
            errors.name = "Use lowercase letters, digits, dots, dashes and underscores, " +
                "starting with a letter or digit (up to 64).";
        } else if ((model.shares || []).some(s => s.name === name)) {
            errors.name = "There is already a share called " + name + ".";
        }
    }
    if (!splitClients(form.clients).length) {
        errors.clients = "List at least one computer or network: an address like 192.168.1.20, " +
            "a network like 192.168.1.0/24, or a computer's name.";
    }
    return errors;
}

// The SetShare call: its name and its a{sv} options, each value tagged with
// its D-Bus type as cockpit.dbus expects. An edit keeps the share's owner and
// squashing; a new share's folder belongs to the signed-in user.
function shareRequest(form, user, existing) {
    const owner = existing || { squash: "root", uid: user.id, gid: user.gid !== undefined ? user.gid : user.id };
    return {
        name: existing ? existing.name : String(form.name),
        options: {
            clients: { t: "as", v: splitClients(form.clients) },
            "read-only": { t: "b", v: !!form.readOnly },
            squash: { t: "s", v: owner.squash },
            uid: { t: "u", v: owner.uid },
            gid: { t: "u", v: owner.gid },
        },
    };
}

// An engine error in words. polkit refusing is the common one.
function changeError(error) {
    const name = (error && error.name) || "";
    if (name === "org.freedesktop.DBus.Error.AccessDenied") {
        return "Not allowed. Turn on administrative access at the top of Cockpit, then try again.";
    }
    const message = (error && (error.message || String(error))) || "The change didn't go through.";
    return sentence(message.replace(/\.$/, ""));
}

// --- the Snapshots tab ----------------------------------------------------

const PERIODS = [
    { key: "hourly", unit: "hour", label: "Hourly snapshots" },
    { key: "daily", unit: "day", label: "Daily snapshots" },
    { key: "weekly", unit: "week", label: "Weekly snapshots" },
    { key: "monthly", unit: "month", label: "Monthly snapshots" },
];

function listWords(items) {
    if (items.length <= 1) return items.join("");
    return items.slice(0, -1).join(", ") + " and " + items[items.length - 1];
}

// The schedule in one sentence.
function scheduleSentence(schedule) {
    const kept = PERIODS.filter(p => (schedule[p.key] || 0) > 0)
        .map(p => plural(schedule[p.key], p.unit));
    if (!kept.length) return "Snapshots are off: nothing is kept to go back to.";
    return "A snapshot of your shares every hour, thinned out as it ages: kept for " + listWords(kept) + ".";
}

function snapshotSummary(model, now) {
    const list = model.snapshots || [];
    return {
        sentence: scheduleSentence(model.schedule || {}),
        count: !list.length
            ? "No snapshots yet. The first comes on the hour."
            : plural(list.length, "snapshot") + " · newest " + ago(list[0].date, now) +
              " · oldest " + ago(list[list.length - 1].date, now),
    };
}

function snapshotRows(model, now, limit) {
    return (model.snapshots || []).slice(0, limit || 50).map(s => ({
        number: s.number,
        when: ago(s.date, now),
        exact: s.date,
        label: s.cleanup === "timeline" ? "Scheduled" : (s.description || "Taken by hand"),
    }));
}

// Problems with the schedule form, keyed by period.
function checkSchedule(values) {
    const errors = {};
    for (const p of PERIODS) {
        const text = String(values[p.key] === undefined ? "" : values[p.key]).trim();
        if (!/^\d{1,4}$/.test(text) || Number(text) > 1000) errors[p.key] = "A whole number from 0 to 1000.";
    }
    return errors;
}

// SetSnapshotSchedule's a{su} argument.
function scheduleRequest(values) {
    const out = {};
    for (const p of PERIODS) out[p.key] = Number(String(values[p.key]).trim());
    return out;
}

// --- browsing a snapshot --------------------------------------------------

// Where you are in a snapshot, as links back up: "Shares › photos › 2025".
function breadcrumb(path) {
    const parts = String(path || "").split("/").filter(Boolean);
    const crumbs = [{ label: "Shares", path: "" }];
    parts.forEach((part, i) => crumbs.push({ label: part, path: parts.slice(0, i + 1).join("/") }));
    return crumbs;
}

function joinPath(folder, name) {
    return folder ? folder + "/" + name : name;
}

// One folder of a snapshot, from BrowseSnapshot's JSON.
function browseRows(listing, folder) {
    const data = parse(listing, { entries: [], truncated: false });
    const rows = (data.entries || []).map(e => ({
        name: e.name,
        path: joinPath(folder, e.name),
        kind: e.kind,
        detail: e.kind === "folder" ? "Folder"
            : e.kind === "file" ? formatSize(e.size)
                : e.kind === "link" ? "Link (not restored on its own)" : "Not a file",
        canOpen: e.kind === "folder",
        canRestore: e.kind === "folder" || e.kind === "file",
    }));
    return {
        rows,
        empty: rows.length ? "" : "This folder was empty.",
        truncated: data.truncated ? "Only the first 5000 entries are shown." : "",
    };
}

function restoredMessage(path) {
    const parts = String(path).split("/");
    const name = parts.pop();
    return "Restored as “" + name + "”" +
        (parts.length ? " in " + parts.join("/") : " at the top of your shares") +
        ", next to the original. Nothing was overwritten.";
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
    splitClients, isWide, trustPreview, checkShare, shareRequest, changeError,
    SEC_SYS_NOTE, WIDE_NOTE,
    PERIODS, scheduleSentence, snapshotSummary, snapshotRows, checkSchedule, scheduleRequest,
    breadcrumb, browseRows, restoredMessage,
};
if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
} else {
    window.TamarackStatus = api;
}
