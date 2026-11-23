// SPDX-License-Identifier: GPL-3.0-or-later
//
// VM tests only: read the engine's Manager properties as JSON on stdin and
// print what the Cockpit page's banner would say, as "tone: title".
"use strict";
const S = require("/usr/share/cockpit/tamarack/status.js");
let input = "";
process.stdin.on("data", chunk => { input += chunk; });
process.stdin.on("end", () => {
    const b = S.banner(S.read(JSON.parse(input)));
    process.stdout.write(b.tone + ": " + b.title + "\n");
});
