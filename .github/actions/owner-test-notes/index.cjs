"use strict";

const fs = require("node:fs");

function ownerTestNotes(body) {
  const notes = [];
  let collecting = false;
  let level = 0;
  let fence = "";
  let found = false;
  for (const line of body.split(/\r?\n/u)) {
    const marker = line.trimStart().slice(0, 3);
    if (marker === "```" || marker === "~~~") {
      fence = fence === marker ? "" : fence || marker;
    }
    const heading = fence ? null : /^(#{1,6})\s+(.+?)\s*#*\s*$/u.exec(line);
    if (heading) {
      if (heading[2].trim().toLowerCase() === "owner test notes") {
        if (found) throw new Error("Use exactly one Owner test notes section.");
        found = collecting = true;
        level = heading[1].length;
        continue;
      }
      if (collecting && heading[1].length <= level) collecting = false;
    }
    if (collecting) notes.push(line);
  }
  return notes.join("\n").trim();
}

function main() {
  if (process.env.GITHUB_EVENT_NAME !== "pull_request") {
    throw new Error("Owner test notes must run on a pull_request event.");
  }
  const event = JSON.parse(fs.readFileSync(process.env.GITHUB_EVENT_PATH, "utf8"));
  const body = event.pull_request?.body;
  if (!ownerTestNotes(typeof body === "string" ? body : "")) {
    throw new Error('Add an "Owner test notes" heading and instructions. "Nothing for the owner to test" is valid.');
  }
  console.log("Owner test notes are present. Their content is reviewed by the Owner at release.");
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    const message = String(error.message).replaceAll("%", "%25").replaceAll("\r", "%0D").replaceAll("\n", "%0A");
    console.error(`::error::${message}`);
    process.exitCode = 1;
  }
}

module.exports = { ownerTestNotes };
