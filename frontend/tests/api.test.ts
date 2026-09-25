import assert from "node:assert/strict";
import { afterEach, mock, test } from "node:test";
import { api } from "../src/api.ts";

afterEach(() => mock.restoreAll());

test("HTML restart responses produce a useful error without retrying backfill", async () => {
  const fetch = mock.method(globalThis, "fetch", async () => new Response("<!DOCTYPE html><title>Unavailable</title>", {
    status: 502, headers: { "Content-Type": "text/html" },
  }));
  await assert.rejects(api("/api/sync", "POST", {}), /server is temporarily unavailable/i);
  assert.equal(fetch.mock.callCount(), 1);
});

test("polling can recover after a non-JSON response", async () => {
  let calls = 0;
  mock.method(globalThis, "fetch", async () => ++calls === 1
    ? new Response("<!DOCTYPE html>", { status: 503 })
    : Response.json({ status: { running: true } }));
  await assert.rejects(api("/api/state"), /temporarily unavailable/);
  assert.deepEqual(await api("/api/state"), { status: { running: true } });
});

test("a lost connection does not repeat a potentially accepted request", async () => {
  const fetch = mock.method(globalThis, "fetch", async () => { throw new TypeError("Failed to fetch"); });
  await assert.rejects(api("/api/settings", "PUT", { repos: ["company/repo"] }), /Unable to reach the server/);
  assert.equal(fetch.mock.callCount(), 1);
});

test("JSON validation errors remain actionable", async () => {
  mock.method(globalThis, "fetch", async () => Response.json({ detail: "Choose an estimator model." }, { status: 422 }));
  await assert.rejects(api("/api/settings", "PUT", {}), /Choose an estimator model/);
});

test("unexpected successful HTML responses and JSON errors without details are handled", async () => {
  mock.method(globalThis, "fetch", async () => new Response("<!DOCTYPE html>", { status: 200 }));
  await assert.rejects(api("/api/state"), /unexpected response/);
  mock.restoreAll();
  mock.method(globalThis, "fetch", async () => Response.json(null, { status: 500 }));
  await assert.rejects(api("/api/state"), /temporarily unavailable/);
});
