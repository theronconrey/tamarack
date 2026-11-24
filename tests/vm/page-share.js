// SPDX-License-Identifier: GPL-3.0-or-later
//
// VM tests only: read {"form", "user", "existing"} as JSON on stdin and print
// the SetShare request the Cockpit page would send, as JSON, for
// tests/vm/cockpit.sh to send to the engine.
"use strict";
const S = require("/usr/share/cockpit/tamarack/status.js");
let input = "";
process.stdin.on("data", chunk => { input += chunk; });
process.stdin.on("end", () => {
    const { form, user, existing } = JSON.parse(input);
    process.stdout.write(JSON.stringify(S.shareRequest(form, user, existing || null)) + "\n");
});
