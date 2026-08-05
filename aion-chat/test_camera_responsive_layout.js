const assert = require("assert");
const fs = require("fs");
const path = require("path");

const source = fs.readFileSync(path.join(__dirname, "static", "camera.html"), "utf8");

assert.match(source, /class="cam-source-card"/, "source controls need their own card");
assert.match(source, /class="cam-actions"/, "quick actions need a responsive group");
assert.match(source, /class="cam-sentinel-card"/, "sentinel settings need their own card");
assert.match(source, /id="camWakeMode2"/, "wake mode control is missing");
assert.match(source, /id="camSmartDispatchNote"/, "smart dispatch note is missing");
assert.match(source, /@media\s*\(max-width:\s*520px\)/, "mobile breakpoint is missing");
assert.match(source, /status\.ai_name/, "main AI label must come from status");
assert.match(source, /status\.connor_name/, "second AI label must come from status");
assert.match(source, /wake_mode:\s*\$\("camWakeMode2"\)\.value/, "wake mode must be saved");
assert.match(source, /minmax\(0,\s*1fr\)/, "responsive grids must allow children to shrink");

console.log("camera responsive layout contract: ok");
