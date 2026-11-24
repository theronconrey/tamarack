// SPDX-License-Identifier: GPL-3.0-or-later
//
// The Tamarack page in Cockpit, read-only: the Overview and the Drives,
// Shares and Activity tabs. It talks only to
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
    if (b.action) {
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
                     el("p", "tk-muted", "Create one from a terminal for now: " +
                        "tamarack engine share set NAME --client ADDRESS."));
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
        const trust = el("div", "tk-trust tk-tone-" + c.tone);
        trust.append(el("strong", "", "Who you are trusting"));
        const notes = el("ul");
        for (const t of c.trust) notes.append(el("li", "", t));
        trust.append(notes);
        card.append(head, chips, facts, trust);
        list.push(card);
    }
    replace("tk-share-cards", ...list);
}

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
let job = null;

function properties(proxy) {
    const out = {};
    for (const name of Object.keys(proxy.data || {})) out[name] = proxy[name];
    return out;
}

// --- tabs --------------------------------------------------------------------

const TABS = ["overview", "drives", "shares", "activity"];

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
