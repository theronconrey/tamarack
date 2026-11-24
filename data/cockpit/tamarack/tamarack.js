// SPDX-License-Identifier: GPL-3.0-or-later
//
// The Tamarack page in Cockpit: the Overview and the Drives, Shares, Snapshots and
// Activity tabs. The changes it makes so far: shares, the snapshot schedule,
// restoring from a snapshot, and continuing or cancelling a paused job. It talks only to
// the engine (com.borealislogistics.Tamarack1 on the system bus) and draws
// what status.js works out. Every text from the engine goes in with
// textContent, never as HTML.

/* global cockpit, TamarackStatus */
"use strict";

const BUS_NAME = "com.borealislogistics.Tamarack1";
const ROOT = "/com/borealislogistics/Tamarack1";
const MANAGER = BUS_NAME + ".Manager";
const JOB = BUS_NAME + ".Job";

const S = TamarackStatus;

function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
}

function replace(id, ...children) {
    const target = document.getElementById(id);
    target.replaceChildren(...children);
}

const ICONS = {
    ok: "M5 12l5 5 9-10",
    warning: "M12 8v5 M12 16.5v.01",
    danger: "M9.5 9.5l5 5 M14.5 9.5l-5 5",
    working: "M12 8v5l3 2",
    neutral: "M12 8v.01 M12 11v5",
};

function shieldIcon(tone) {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("class", "tk-banner-icon");
    for (const d of ["M12 3l7 3v5c0 4.5-3 8.5-7 10-4-1.5-7-5.5-7-10V6z", ICONS[tone] || ICONS.neutral]) {
        const path = document.createElementNS(ns, "path");
        path.setAttribute("d", d);
        svg.appendChild(path);
    }
    return svg;
}

function drawBanner(b) {
    const section = el("section", "tk-banner tk-tone-" + b.tone);
    section.setAttribute("aria-live", "polite");
    const words = el("div", "tk-banner-words");
    words.append(el("strong", "tk-banner-title", b.title), el("span", "tk-banner-text", b.text));
    section.append(shieldIcon(b.tone), words);
    if (b.action && b.action.op) {
        section.append(jobControls(b));
    } else if (b.action) {
        const a = el("a", "tk-button", b.action.label);
        a.href = b.action.href;
        link(a, b.action.href);
        section.append(a);
    }
    if (b.progress !== undefined) {
        const bar = el("div", "tk-progress");
        bar.setAttribute("role", "progressbar");
        bar.setAttribute("aria-valuemin", "0");
        bar.setAttribute("aria-valuemax", "100");
        bar.setAttribute("aria-valuenow", String(Math.round(b.progress * 100)));
        const fill = el("div", "tk-progress-fill");
        fill.style.width = (b.progress * 100).toFixed(1) + "%";
        bar.append(fill);
        section.append(bar);
    }
    replace("tk-banner", section);
}

// --- a paused job: continue or cancel -------------------------------------------

let confirmingCancel = false;
let jobError = "";
let jobBusy = false;

function jobCall(method, args, signature) {
    if (!job) return;
    jobBusy = true;
    jobError = "";
    render();
    admin.call(job.path, JOB, method, args, { type: signature })
        .then(() => { confirmingCancel = false; })
        .catch(error => { jobError = S.changeError(error); })
        .finally(() => {
            jobBusy = false;
            render();
        });
}

function jobControls(b) {
    const box = el("div", "tk-banner-actions");
    if (confirmingCancel) {
        box.append(el("p", "", "Cancel this change? It stops here and won't continue; " +
            "the steps already done stay done."));
    }
    const buttons = el("div", "tk-actions");
    const primary = el("button", "tk-button", confirmingCancel ? "Keep it" : b.action.label);
    primary.type = "button";
    primary.disabled = jobBusy;
    primary.addEventListener("click", () => {
        if (confirmingCancel) {
            confirmingCancel = false;
            render();
        } else {
            jobCall("Resume", [{}], "a{sv}");
        }
    });
    buttons.append(primary);
    if (b.secondary) {
        const cancel = el("button", "tk-button " + (confirmingCancel ? "tk-danger" : "tk-secondary"),
                          confirmingCancel ? "Cancel the change" : b.secondary.label);
        cancel.type = "button";
        cancel.disabled = jobBusy;
        cancel.addEventListener("click", () => {
            if (confirmingCancel) {
                jobCall("Cancel", [], "");
            } else {
                confirmingCancel = true;
                render();
            }
        });
        buttons.append(cancel);
    }
    box.append(buttons);
    if (jobBusy) box.append(el("p", "tk-small", "Asking the engine…"));
    if (jobError) box.append(el("p", "tk-error", jobError));
    return box;
}

function drawDrives(rows) {
    const list = el("ul", "tk-drives");
    for (const d of rows) {
        const item = el("li", "tk-drive tk-drive-" + d.tone);
        item.append(el("span", "tk-drive-label", d.label), el("span", "tk-drive-size", d.size),
                    el("span", "tk-pill tk-tone-" + d.tone, d.stateText));
        item.title = d.device + (d.detail ? " · " + d.detail : "");
        list.append(item);
    }
    replace("tk-drives", list);
    document.getElementById("tk-drive-count").textContent =
        rows.length === 1 ? "1 drive" : rows.length + " drives";
}

function drawCapacity(cap) {
    if (!cap) {
        replace("tk-capacity");
        return;
    }
    const head = el("div", "tk-free");
    head.append(el("span", "tk-free-number", S.formatSize(cap.free) + " free"),
                el("span", "tk-muted", "of " + S.formatSize(cap.total) + " for your files"));
    const parts = [
        { cls: "used", label: "Your files", bytes: cap.used },
        { cls: "free", label: "Free", bytes: cap.free },
        { cls: "protection", label: "Keeps your data safe", bytes: cap.protection },
        { cls: "waiting", label: "Waiting for a partner drive", bytes: cap.waiting },
    ];
    const whole = cap.used + cap.free + cap.protection + cap.waiting || 1;
    const bar = el("div", "tk-bar");
    bar.setAttribute("role", "img");
    bar.setAttribute("aria-label", parts.filter(p => p.bytes)
        .map(p => p.label + ": " + S.formatSize(p.bytes)).join(", "));
    const legend = el("div", "tk-legend");
    for (const p of parts) {
        if (!p.bytes && p.cls === "waiting") continue;
        const seg = el("div", "tk-seg tk-seg-" + p.cls);
        seg.style.width = (100 * p.bytes / whole).toFixed(2) + "%";
        bar.append(seg);
        const key = el("span", "tk-key");
        key.append(el("span", "tk-swatch tk-seg-" + p.cls), document.createTextNode(p.label + ": " + S.formatSize(p.bytes)));
        legend.append(key);
    }
    replace("tk-capacity", head, bar, legend);
}

// A link to another Cockpit page moves the whole shell, not this frame; a
// "#name" link opens one of this page's tabs.
function link(a, href) {
    if (href.startsWith("/")) {
        a.addEventListener("click", event => {
            event.preventDefault();
            cockpit.jump(href);
        });
    } else if (href.startsWith("#")) {
        a.addEventListener("click", event => {
            event.preventDefault();
            goTab(href.replace(/^#\/?/, ""));
        });
    }
}

function drawDriveCards(cards, advice) {
    const list = [];
    for (const c of cards) {
        const card = el("section", "tk-card tk-drive-card tk-drive-" + c.tone);
        const head = el("div", "tk-card-head");
        const title = el("h3", "", c.label + " · " + c.size);
        head.append(title, el("span", "tk-pill tk-tone-" + c.tone, c.stateText));
        card.append(head);
        const facts = el("dl", "tk-facts");
        const add = (label, value) => {
            if (value) facts.append(el("dt", "", label), el("dd", "", value));
        };
        add("Model", [c.kind, c.model].filter(Boolean).join(" · "));
        add("Serial number", c.serial);
        add("Temperature", c.temperature);
        add("Age", c.age);
        add("Holds", c.holds.replace(/^Holds /, ""));
        add("Device", c.device);
        card.append(facts);
        if (c.waiting) card.append(el("p", "tk-muted tk-small", c.waiting));
        if (c.detail) card.append(el("p", "tk-small", c.detail));
        list.push(card);
    }
    replace("tk-drive-cards", ...list);

    if (!advice) {
        replace("tk-upgrade");
        return;
    }
    const parts = [];
    if (advice.best) {
        const best = el("div", "tk-upgrade-best");
        best.append(el("strong", "", advice.best.text), el("span", "tk-gain", advice.best.gainText));
        parts.push(best);
        if (advice.others.length) {
            const others = el("ul", "tk-upgrade-others");
            for (const o of advice.others) {
                const item = el("li");
                item.append(el("span", "", o.text), el("span", "tk-muted", " · " + o.gainText));
                others.append(item);
            }
            parts.push(others);
        }
    }
    parts.push(el("p", "tk-muted tk-small", advice.text));
    replace("tk-upgrade", ...parts);
}

function drawShareCards(cards) {
    if (!cards.length) {
        const empty = el("section", "tk-card");
        empty.append(el("h2", "", "No shares yet"),
                     el("p", "tk-muted", "Make a folder other computers can open: choose New share."));
        replace("tk-share-cards", empty);
        return;
    }
    const list = [];
    for (const c of cards) {
        const card = el("section", "tk-card");
        const head = el("div", "tk-card-head");
        head.append(el("h3", "", c.name), el("span", "tk-pill tk-tone-neutral", c.access));
        const chips = el("div", "tk-chips");
        for (const w of c.ways) {
            const chip = el("span", "tk-chip", w.label);
            chip.title = w.detail;
            chips.append(chip);
        }
        const facts = el("dl", "tk-facts");
        facts.append(el("dt", "", "Connect to"), el("dd", "tk-mono", c.address),
                     el("dt", "", "Who can connect"), el("dd", "", c.who));
        card.append(head, chips, facts, trustBox(c.trust, c.tone));
        if (removing === c.name) {
            card.append(confirmRemove(c.name));
        } else {
            const actions = el("div", "tk-actions");
            const edit = el("button", "tk-button tk-secondary", "Edit");
            edit.type = "button";
            edit.addEventListener("click", () => openShareForm(c.share));
            const stop = el("button", "tk-button tk-secondary", "Stop sharing");
            stop.type = "button";
            stop.addEventListener("click", () => {
                removing = c.name;
                render();
            });
            actions.append(edit, stop);
            card.append(actions);
        }
        list.push(card);
    }
    replace("tk-share-cards", ...list);
}

function trustBox(notes, tone) {
    const trust = el("div", "tk-trust tk-tone-" + tone);
    trust.append(el("strong", "", "Who you are trusting"));
    const list = el("ul");
    for (const t of notes) list.append(el("li", "", t));
    trust.append(list);
    return trust;
}

// --- changing shares ----------------------------------------------------------

let removing = "";  // the share whose "Stop sharing" is being confirmed
let removeError = "";
let user = { id: 0, gid: 0 };
cockpit.user().then(u => { user = u; });

function confirmRemove(name) {
    const box = el("div", "tk-confirm");
    box.append(el("p", "", "Stop sharing " + name + "? Its files stay in your pool; " +
        "other computers just can't open it any more."));
    if (removeError) box.append(el("p", "tk-error", removeError));
    const actions = el("div", "tk-actions");
    const yes = el("button", "tk-button tk-danger", "Stop sharing");
    yes.type = "button";
    const no = el("button", "tk-button tk-secondary", "Keep sharing");
    no.type = "button";
    no.addEventListener("click", () => {
        removing = "";
        removeError = "";
        render();
    });
    yes.addEventListener("click", () => {
        yes.disabled = no.disabled = true;
        admin.call(ROOT, MANAGER, "RemoveShare", [name], { type: "s" })
            .then(() => {
                removing = "";
                removeError = "";
                render();
            })
            .catch(error => {
                removeError = S.changeError(error);
                render();
            });
    });
    actions.append(yes, no);
    box.append(actions);
    return box;
}

function field(labelText, control, help) {
    const wrap = el("div", "tk-field");
    const label = el("label", "", labelText);
    label.htmlFor = control.id;
    wrap.append(label, control);
    if (help) wrap.append(el("p", "tk-muted tk-small", help));
    const error = el("p", "tk-error");
    error.hidden = true;
    wrap.append(error);
    return { wrap, error };
}

function openShareForm(existing) {
    const form = document.getElementById("tk-share-form");
    form.hidden = false;
    removing = "";

    const heading = el("h2", "", existing ? "Change " + existing.name : "New share");
    heading.id = "share-form-h";

    const name = el("input");
    name.id = "tk-share-name";
    name.autocomplete = "off";
    name.spellcheck = false;
    if (existing) {
        name.value = existing.name;
        name.disabled = true;
    }
    const nameField = field("Name", name, existing ? "" : "Lowercase, like photos or family-docs. It becomes the folder's name.");

    const clients = el("textarea");
    clients.id = "tk-share-clients";
    clients.rows = 2;
    clients.spellcheck = false;
    clients.value = existing ? existing.clients.join("\n") : "";
    const clientsField = field("Who can connect", clients,
        "One per line: a computer's address (192.168.1.20), your home network (192.168.1.0/24) or a computer's name.");

    const access = el("fieldset", "tk-choice");
    access.append(el("legend", "", "What they can do"));
    const choices = {};
    for (const [value, text] of [["rw", "Open and change files"], ["ro", "Only open files"]]) {
        const option = el("label", "tk-radio");
        const radio = el("input");
        radio.type = "radio";
        radio.name = "tk-share-access";
        radio.value = value;
        radio.checked = existing ? (value === "ro") === existing.read_only : value === "rw";
        choices[value] = radio;
        option.append(radio, document.createTextNode(" " + text));
        access.append(option);
    }

    const preview = el("div");
    const failure = el("p", "tk-error");
    failure.hidden = true;

    const values = () => ({ name: name.value.trim(), clients: clients.value, readOnly: choices.ro.checked });
    const update = () => {
        const p = S.trustPreview(values(), existing);
        preview.replaceChildren(trustBox(p.notes, p.tone));
    };
    for (const control of [name, clients, choices.rw, choices.ro]) control.addEventListener("input", update);
    update();

    const save = el("button", "tk-button", existing ? "Save changes" : "Share it");
    save.type = "submit";
    const cancel = el("button", "tk-button tk-secondary", "Cancel");
    cancel.type = "button";
    cancel.addEventListener("click", closeShareForm);
    const actions = el("div", "tk-actions");
    actions.append(save, cancel,
                   el("span", "tk-muted tk-small", "Cockpit asks for the admin password if it needs to."));

    const body = el("form");
    body.noValidate = true;
    body.append(nameField.wrap, clientsField.wrap, access, preview, failure, actions);
    body.addEventListener("submit", event => {
        event.preventDefault();
        const draft = values();
        const errors = S.checkShare(draft, S.read(properties(manager)), existing);
        nameField.error.hidden = !errors.name;
        nameField.error.textContent = errors.name || "";
        clientsField.error.hidden = !errors.clients;
        clientsField.error.textContent = errors.clients || "";
        failure.hidden = true;
        if (errors.name || errors.clients) return;
        const request = S.shareRequest(draft, user, existing);
        save.disabled = cancel.disabled = true;
        admin.call(ROOT, MANAGER, "SetShare", [request.name, request.options], { type: "sa{sv}" })
            .then(closeShareForm)
            .catch(error => {
                failure.textContent = S.changeError(error);
                failure.hidden = false;
            })
            .finally(() => { save.disabled = cancel.disabled = false; });
    });

    form.replaceChildren(heading, body);
    (existing ? clients : name).focus();
}

function closeShareForm() {
    const form = document.getElementById("tk-share-form");
    form.hidden = true;
    form.replaceChildren();
}

document.getElementById("tk-share-new").addEventListener("click", () => openShareForm(null));

function drawSnapshots(summary, rows) {
    document.getElementById("tk-schedule-sentence").textContent = summary.sentence;
    document.getElementById("tk-snapshot-count").textContent = summary.count;
    if (!rows.length) {
        replace("tk-snapshot-list", el("p", "tk-muted", summary.count));
        return;
    }
    const list = el("ol", "tk-history");
    for (const r of rows) {
        const item = el("li", "tk-history-item");
        const head = el("div", "tk-history-head");
        const when = el("strong", "", r.when);
        when.title = r.exact;
        head.append(when, el("span", "tk-muted tk-small", r.label + " · #" + r.number));
        const look = el("button", "tk-button tk-secondary tk-small-button", "Look inside");
        look.type = "button";
        look.addEventListener("click", () => openSnapshot(r, ""));
        head.append(look);
        item.append(head);
        list.append(item);
    }
    replace("tk-snapshot-list", list);
}

// --- browsing a snapshot and restoring from it -----------------------------------

let browsing = null;  // {snapshot: row, folder, listing, message, error, busy}

function openSnapshot(row, folder) {
    browsing = { snapshot: row, folder, listing: null, message: "", error: "", busy: true };
    drawBrowser();
    document.getElementById("tk-browser").scrollIntoView({ block: "start" });
    admin.call(ROOT, MANAGER, "BrowseSnapshot", [row.number, folder], { type: "us" })
        .then(([listing]) => {
            if (!browsing || browsing.snapshot !== row || browsing.folder !== folder) return;
            browsing.listing = listing;
        })
        .catch(error => {
            if (browsing) browsing.error = S.changeError(error);
        })
        .finally(() => {
            if (browsing) browsing.busy = false;
            drawBrowser();
        });
}

function restoreItem(path) {
    const b = browsing;
    b.busy = true;
    b.message = "";
    b.error = "";
    drawBrowser();
    admin.call(ROOT, MANAGER, "RestoreFromSnapshot", [b.snapshot.number, path], { type: "us" })
        .then(([restored]) => { b.message = S.restoredMessage(restored); })
        .catch(error => { b.error = S.changeError(error); })
        .finally(() => {
            b.busy = false;
            drawBrowser();
        });
}

function drawBrowser() {
    const box = document.getElementById("tk-browser");
    if (!browsing) {
        box.hidden = true;
        box.replaceChildren();
        return;
    }
    const b = browsing;
    box.hidden = false;
    const head = el("div", "tk-card-head");
    const title = el("h2", "", "Snapshot from " + b.snapshot.when);
    title.id = "browser-h";
    const close = el("button", "tk-button tk-secondary", "Close");
    close.type = "button";
    close.addEventListener("click", () => {
        browsing = null;
        drawBrowser();
    });
    head.append(title, close);

    const crumbs = el("nav", "tk-crumbs");
    crumbs.setAttribute("aria-label", "Folder");
    for (const c of S.breadcrumb(b.folder)) {
        if (c.path === b.folder) {
            crumbs.append(el("span", "tk-crumb-here", c.label));
        } else {
            const a = el("button", "tk-crumb", c.label);
            a.type = "button";
            a.addEventListener("click", () => openSnapshot(b.snapshot, c.path));
            crumbs.append(a, el("span", "tk-muted", " › "));
        }
    }

    const parts = [head, el("p", "tk-muted tk-small",
        "Restoring puts a copy next to the original, named with the snapshot's time. " +
        "Nothing is overwritten."), crumbs];
    if (b.message) parts.push(el("p", "tk-ok-text", b.message));
    if (b.error) parts.push(el("p", "tk-error", b.error));
    if (b.busy) parts.push(el("p", "tk-muted", b.listing === null ? "Opening…" : "Restoring…"));
    if (b.listing !== null) {
        const view = S.browseRows(b.listing, b.folder);
        const list = el("ul", "tk-files");
        for (const r of view.rows) {
            const item = el("li", "tk-file");
            let name;
            if (r.canOpen) {
                name = el("button", "tk-file-name tk-crumb", r.name);
                name.type = "button";
                name.addEventListener("click", () => openSnapshot(b.snapshot, r.path));
            } else {
                name = el("span", "tk-file-name", r.name);
            }
            item.append(name, el("span", "tk-muted tk-small", r.detail));
            if (r.canRestore) {
                const restore = el("button", "tk-button tk-secondary tk-small-button", "Restore");
                restore.type = "button";
                restore.disabled = b.busy;
                restore.addEventListener("click", () => restoreItem(r.path));
                item.append(restore);
            }
            list.append(item);
        }
        parts.push(list);
        if (view.empty) parts.push(el("p", "tk-muted", view.empty));
        if (view.truncated) parts.push(el("p", "tk-muted tk-small", view.truncated));
    }
    box.replaceChildren(...parts);
}

function openScheduleForm() {
    const box = document.getElementById("tk-schedule-form");
    const current = S.read(properties(manager)).schedule;
    box.hidden = false;
    document.getElementById("tk-schedule-change").hidden = true;
    const inputs = {};
    const errors = {};
    const form = el("form", "tk-schedule");
    form.noValidate = true;
    for (const p of S.PERIODS) {
        const row = el("div", "tk-field tk-field-inline");
        const input = el("input");
        input.id = "tk-keep-" + p.key;
        input.inputMode = "numeric";
        input.value = String(current[p.key] || 0);
        const label = el("label", "", p.label + ", kept for");
        label.htmlFor = input.id;
        row.append(label, input, el("span", "tk-muted", p.unit + "s"));
        const error = el("p", "tk-error");
        error.hidden = true;
        inputs[p.key] = input;
        errors[p.key] = error;
        form.append(row, error);
    }
    form.append(el("p", "tk-muted tk-small", "0 keeps none of that kind. All 0 turns snapshots off."));
    const failure = el("p", "tk-error");
    failure.hidden = true;
    const save = el("button", "tk-button", "Save schedule");
    save.type = "submit";
    const cancel = el("button", "tk-button tk-secondary", "Cancel");
    cancel.type = "button";
    cancel.addEventListener("click", closeScheduleForm);
    const actions = el("div", "tk-actions");
    actions.append(save, cancel);
    form.append(failure, actions);
    form.addEventListener("submit", event => {
        event.preventDefault();
        const values = {};
        for (const p of S.PERIODS) values[p.key] = inputs[p.key].value;
        const problems = S.checkSchedule(values);
        for (const p of S.PERIODS) {
            errors[p.key].hidden = !problems[p.key];
            errors[p.key].textContent = problems[p.key] || "";
        }
        failure.hidden = true;
        if (Object.keys(problems).length) return;
        save.disabled = cancel.disabled = true;
        admin.call(ROOT, MANAGER, "SetSnapshotSchedule", [S.scheduleRequest(values)], { type: "a{su}" })
            .then(closeScheduleForm)
            .catch(error => {
                failure.textContent = S.changeError(error);
                failure.hidden = false;
            })
            .finally(() => { save.disabled = cancel.disabled = false; });
    });
    box.replaceChildren(form);
    inputs.hourly.focus();
}

function closeScheduleForm() {
    const box = document.getElementById("tk-schedule-form");
    box.hidden = true;
    box.replaceChildren();
    document.getElementById("tk-schedule-change").hidden = false;
}

document.getElementById("tk-schedule-change").addEventListener("click", openScheduleForm);

function historyList(entries) {
    const list = el("ol", "tk-history");
    for (const e of entries) {
        const item = el("li", "tk-history-item");
        const head = el("div", "tk-history-head");
        const when = el("span", "tk-muted tk-small", e.when);
        when.title = e.exact;
        head.append(el("strong", "", e.title), when,
                    el("span", "tk-pill tk-tone-" + (e.tone === "working" ? "neutral" : e.tone), e.resultText));
        item.append(head);
        for (const line of e.lines) item.append(el("p", "tk-small tk-muted", line));
        list.append(item);
    }
    return list;
}

function drawActivity(act) {
    document.getElementById("tk-waiting-card").hidden = !act.waiting.length;
    replace("tk-waiting", historyList(act.waiting));
    replace("tk-history", act.entries.length
        ? historyList(act.entries)
        : el("p", "tk-muted", "Nothing has happened yet."));
    document.getElementById("tk-alerts").textContent = act.alerts;
}

function drawCare(lines) {
    const list = el("dl", "tk-care");
    for (const line of lines) list.append(el("dt", "", line.label), el("dd", "tk-muted", line.value));
    replace("tk-care", list);
}

function drawSummary(model) {
    const text = !model.layout
        ? "No pool yet"
        : model.layout.drives.length + " drives · " + S.formatSize(model.usable) + " for your files · " +
          (model.tolerated >= 2 ? "survives two drive failures"
              : model.tolerated === 1 ? "survives one drive failure" : "not protected against a drive failure");
    document.getElementById("tk-summary").textContent = text;
}

// --- the engine -------------------------------------------------------------

const client = cockpit.dbus(BUS_NAME, { bus: "system" });
const manager = client.proxy(MANAGER, ROOT);
// Changes go through Cockpit's administrative access when it is on; polkit
// still decides (com.borealislogistics.tamarack.share).
const admin = cockpit.dbus(BUS_NAME, { bus: "system", superuser: "try" });
let job = null;

function properties(proxy) {
    const out = {};
    for (const name of Object.keys(proxy.data || {})) out[name] = proxy[name];
    return out;
}

// --- tabs --------------------------------------------------------------------

const TABS = ["overview", "drives", "shares", "snapshots", "activity"];

function currentTab() {
    const tab = cockpit.location.path[0];
    return TABS.includes(tab) ? tab : "overview";
}

function goTab(tab) {
    cockpit.location.go(tab === "overview" || !TABS.includes(tab) ? [] : [tab]);
}

function showTab(hasPool) {
    const tab = currentTab();
    document.getElementById("tk-tabs").hidden = !hasPool;
    for (const a of document.querySelectorAll("#tk-tabs a")) {
        if (a.dataset.tab === tab) a.setAttribute("aria-current", "page");
        else a.removeAttribute("aria-current");
    }
    for (const panel of document.querySelectorAll("[data-panel]")) {
        panel.hidden = !hasPool || panel.dataset.panel !== tab;
    }
}

for (const a of document.querySelectorAll("#tk-tabs a")) link(a, "#" + a.dataset.tab);
link(document.getElementById("tk-logs"), "/system/logs");

function render() {
    const model = S.read(properties(manager));
    const jobProps = job && job.valid ? properties(job) : null;
    const now = Date.now() / 1000;
    drawSummary(model);
    drawBanner(S.banner(model, jobProps));
    drawDrives(S.drives(model));
    drawCapacity(S.capacity(model));
    drawCare(S.care(model, now));
    drawDriveCards(S.driveCards(model), S.upgradeAdvice(model));
    drawShareCards(S.shareCards(model));
    drawSnapshots(S.snapshotSummary(model, now), S.snapshotRows(model, now, 50));
    drawActivity(S.activity(model, now));
    showTab(!!model.layout);
}

cockpit.addEventListener("locationchanged", render);

function followJob() {
    const path = manager.ActiveJob;
    if (job && job.path === path) return;
    job = path && path !== "/" ? client.proxy(JOB, path) : null;
    if (job) {
        job.addEventListener("changed", render);
        job.wait(render);
    }
}

function unreachable() {
    drawBanner({
        tone: "danger",
        title: "Tamarack's engine isn't answering",
        text: "The storage service may be stopped. This page can't show your pool until it runs again; " +
            "your files are not affected by it.",
        action: { label: "Open the service", href: "/system/services#/tamarack-engine.service" },
    });
    showTab(false);
}

manager.addEventListener("changed", () => {
    followJob();
    render();
});
manager.wait(() => {
    if (!manager.valid) {
        unreachable();
        return;
    }
    followJob();
    render();
});
client.addEventListener("close", unreachable);
