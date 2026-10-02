import assert from "node:assert/strict";
import test from "node:test";

import { visibleStrings } from "../scripts/visible-strings.mjs";

function texts(source) {
  return visibleStrings("example.tsx", source).map(entry => entry.text);
}

test("reports the text people see and skips identifiers, class names, and element ids", () => {
  const source = `
import { OwnerPanel } from "./owner-panel";
type Role = "owner" | "admin";
export function Panel({ role }: { role: Role }) {
  const label = role === "owner" ? "Client" : "Read-only";
  return (
    <section className="owner-review-card" id="product-owner-title" aria-label="Client release decision">
      <h2>
        Change the Client
      </h2>
      <p>{label}</p>
    </section>
  );
}
`;
  assert.deepEqual(texts(source), ["Client", "Read-only", "Client release decision", "Change the Client"]);
});

test("reports a short visible attribute and skips class names built in expressions", () => {
  const source = `
export const Badge = ({ active }: { active: boolean }) => (
  <span aria-label="operator" className={active ? "owner badge" : "badge"} title={active ? "owner-review" : "Badge"} />
);
`;
  assert.deepEqual(texts(source), ["operator", "Badge"]);
});

test("skips a literal marked as a legacy marker on its line or the line above", () => {
  const source = `
// role-words: legacy marker that product pull requests write.
export const MARKER = "Nothing for the owner to test";
export const SHOWN = "Nothing for the owner to test";
`;
  assert.deepEqual(visibleStrings("example.ts", source), [{ line: 4, text: "Nothing for the owner to test" }]);
});
