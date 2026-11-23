// SPDX-License-Identifier: GPL-3.0-or-later
//
// VM tests only: read the engine's Manager properties as JSON on stdin and
// print what the Cockpit page's Drives, Shares and Activity tabs would show,
// as JSON, for tests/vm/cockpit.sh to check.
"use strict";
const S = require("/usr/share/cockpit/tamarack/status.js");
let input = "";
process.stdin.on("data", chunk => { input += chunk; });
process.stdin.on("end", () => {
    const model = S.read(JSON.parse(input));
    const now = Date.now() / 1000;
    const out = {
        drives: S.driveCards(model).map(c => ({ label: c.label, state: c.stateText, model: c.model, holds: c.holds })),
        advice: S.upgradeAdvice(model),
        shares: S.shareCards(model).map(c => ({ name: c.name, address: c.address })),
        history: S.activity(model, now).entries.map(e => ({ title: e.title, result: e.resultText, when: e.when })),
    };
    process.stdout.write(JSON.stringify(out) + "\n");
});
