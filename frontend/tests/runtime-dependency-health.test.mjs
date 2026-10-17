import { test } from "node:test";
import assert from "node:assert/strict";
import { dependencyHealth, dependencyHealthResponse } from "../../helpers/generic-web/dependency-health.mjs";

test("returns only pass or fail per dependency and conceals exceptions", async () => {
  assert.deepEqual(await dependencyHealth({
    database: async () => true,
    email: () => { throw new Error("password=private hostname=internal"); },
    cache: () => ({ status: "pass", password: "private" }),
  }), { dependencies: { database: "pass", email: "fail", cache: "fail" } });
});

test("bounds an unresponsive callback and aborts its signal", async () => {
  let signal;
  const result = await dependencyHealth({ database: value => {
    signal = value;
    return new Promise(() => {});
  } }, { timeoutMs: 5 });
  assert.equal(signal.aborted, true);
  assert.deepEqual(result, { dependencies: { database: "fail" } });
});

test("handles late rejection after timeout without exposing it", async () => {
  let reject;
  const result = await dependencyHealth({ database: () => new Promise((_, fail) => { reject = fail; }) }, { timeoutMs: 5 });
  reject(new Error("private"));
  assert.equal(result.dependencies.database, "fail");
});

test("empty dependency set is valid and public responses are uncacheable", async () => {
  const response = await dependencyHealthResponse({});
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.deepEqual(await response.json(), { dependencies: {} });
  assert.equal((await dependencyHealthResponse({ database: () => false })).status, 503);
});

test("rejects arbitrary public names and unbounded deadlines", async () => {
  await assert.rejects(dependencyHealth({ private_hostname: () => true }), TypeError);
  await assert.rejects(dependencyHealth({ database: true }), TypeError);
  await assert.rejects(dependencyHealth({}, { timeoutMs: Infinity }), TypeError);
});
