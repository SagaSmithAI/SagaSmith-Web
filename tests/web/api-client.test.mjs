import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../../src/sagasmith_service/web/api/client.js", import.meta.url), "utf8");
const { api, apiBlobResponse, ApiError } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

test("JSON and blob failures preserve status and readable validation details", async (t) => {
  t.mock.method(globalThis, "fetch", async () => Response.json({detail:[{msg:"Invalid email"}]}, {status:422}));
  for (const request of [api, apiBlobResponse]) {
    await assert.rejects(request("/test"), (error) => error instanceof ApiError && error.status === 422 && error.message === "Invalid email");
  }
});

test("keyed transient retry releases the first response and reuses the request key", async (t) => {
  let calls = 0;
  let cancelled = false;
  t.mock.method(globalThis, "fetch", async (_path, options) => {
    assert.equal(options.headers["Idempotency-Key"], "stable-key");
    calls++;
    if (calls === 1) return new Response(new ReadableStream({cancel() {cancelled = true;}}), {status:503});
    assert.ok(cancelled);
    return Response.json({ok:true});
  });
  assert.deepEqual(await api("/test", {method:"POST", headers:{"Idempotency-Key":"stable-key"}}), {ok:true});
  assert.equal(calls, 2);
});

test("unkeyed writes are never replayed", async (t) => {
  let calls = 0;
  t.mock.method(globalThis, "fetch", async () => {calls++; return Response.json({detail:"Busy"}, {status:503});});
  await assert.rejects(api("/test", {method:"POST"}), {status:503});
  assert.equal(calls, 1);
});
