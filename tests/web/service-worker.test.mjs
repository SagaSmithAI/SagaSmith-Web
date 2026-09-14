import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../../src/sagasmith_service/web/service-worker.js", import.meta.url), "utf8");

function worker() {
  const handlers = {};
  const stored = new Map();
  const deleted = [];
  let fetches = 0;
  const cache = {
    async addAll(requests) { for (const request of requests) stored.set(request.url, new Response("shell")); },
    async match(request) { return stored.get(request.url); },
  };
  vm.runInNewContext(source, {
    self: {location: {origin:"https://example.com"}, addEventListener(name, handler) {handlers[name] = handler;}},
    caches: {async open() {return cache;}, async keys() {return ["unrelated", "sagasmith-shell-v12", "sagasmith-shell-v13"];},
      async delete(key) {deleted.push(key);}},
    Request: class extends Request {constructor(path, options) {super(new URL(path, "https://example.com"), options);}},
    Response, URL,
    async fetch() {fetches++; return new Response("network");},
  });
  return {handlers, stored, deleted, fetches: () => fetches};
}

test("warm shell reads use zero network requests and retain unrelated caches", async () => {
  const w = worker();
  let pending;
  w.handlers.install({waitUntil(value) {pending = value;}});
  await pending;
  assert.ok(w.stored.size >= 30);
  for (const url of w.stored.keys()) {
    w.handlers.fetch({request: new Request(url), respondWith(value) {pending = value;}});
    assert.equal(await (await pending).text(), "shell");
  }
  assert.equal(w.fetches(), 0);
  w.handlers.activate({waitUntil(value) {pending = value;}});
  await pending;
  assert.deepEqual(w.deleted, ["sagasmith-shell-v12"]);
});

test("private, unknown, query and cross-origin requests bypass the shell cache", () => {
  const w = worker();
  for (const url of ["https://example.com/api/auth/me", "https://example.com/private/file",
    "https://example.com/app.js?release=next", "https://other.example/app.js"]) {
    w.handlers.fetch({request: new Request(url), respondWith() {assert.fail(`intercepted ${url}`);}});
  }
  w.handlers.fetch({request: new Request("https://example.com/", {method:"POST"}),
    respondWith() {assert.fail("intercepted POST");}});
});

test("a missing shell entry is fetched without contaminating the installed version", async () => {
  const w = worker();
  let pending;
  w.handlers.fetch({request: new Request("https://example.com/app.js"), respondWith(value) {pending = value;}});
  assert.equal(await (await pending).text(), "network");
  assert.equal(w.fetches(), 1);
  assert.equal(w.stored.size, 0);
});
