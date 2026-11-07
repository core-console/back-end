import assert from "node:assert/strict";
import { createServer } from "node:http";
import { createRequire } from "node:module";
import { mkdtemp, readFile, rm, rmdir } from "node:fs/promises";
import { writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { prepareFinanceCompatibility } from "./prepare-finance-compatibility.mjs";

const front = fileURLToPath(
  new URL("../../../front-end/package.json", import.meta.url),
);
const { chromium } = createRequire(front)("@playwright/test");

const finishFixture = `async function finish(state, detail) {
  const issues = diagnosticIssues(state);
  const trace = join(out, state.name + '-trace.zip');
  await state.context.tracing.stop({path: trace});
  state.traceStopped = true;
  await state.context.close();
  results.scenarios.push({name: state.name, outcome: issues.length ? 'FAIL' : 'PASS', detail});
  writeFileSync(join(out, 'browser-results.json'), JSON.stringify(results));
}`;

test(
  "compatibility finish drains a pending route fetch before disposing its context",
  { timeout: 20_000 },
  async (t) => {
    const seed = process.env.FINANCE_COMPATIBILITY_TEST_SOURCE;
    const source = seed ? await readFile(seed, "utf8") : finishFixture;
    const prepared = prepareFinanceCompatibility(source);
    const finishSource = prepared.match(
      /^async function finish\(state, detail\) \{[\s\S]*?^\}/gm,
    );
    assert.equal(finishSource?.length, 1);
    const out = await mkdtemp(join(tmpdir(), "finance-route-teardown-"));
    const trace = join(out, "foreign-fixture-trace.zip");
    const receipt = join(out, "browser-results.json");
    const arrived = Promise.withResolvers();
    const release = Promise.withResolvers();
    const handlerDone = Promise.withResolvers();
    const aborted = Promise.withResolvers();
    t.signal.addEventListener(
      "abort",
      () => {
        release.resolve();
        aborted.resolve();
      },
      { once: true },
    );
    async function waitFor(promise) {
      await Promise.race([promise, aborted.promise]);
      t.signal.throwIfAborted();
    }
    const order = [];
    let routeError;

    const server = createServer(async (request, response) => {
      if (request.url === "/api/positions") {
        arrived.resolve();
        await release.promise;
        response.writeHead(200, { "content-type": "application/json" });
        response.end("[]");
      } else {
        response.writeHead(200, { "content-type": "text/html" });
        response.end("<!doctype html><title>Route teardown fixture</title>");
      }
    });

    let browserPromise;
    t.after(async () => {
      release.resolve();
      const shutdown = await Promise.allSettled([
        browserPromise?.then(
          (browser) => browser.close(),
          () => {},
        ),
        new Promise((resolve, reject) => {
          server.close((error) => {
            if (error && error.code !== "ERR_SERVER_NOT_RUNNING") reject(error);
            else resolve();
          });
          server.closeAllConnections();
        }),
      ]);
      await Promise.all([
        rm(trace, { force: true }),
        rm(receipt, { force: true }),
      ]);
      await rmdir(out);
      for (const result of shutdown)
        if (result.status === "rejected") throw result.reason;
    });
    browserPromise = chromium.launch();
    const browser = await browserPromise;
    t.signal.throwIfAborted();
    await new Promise((resolve, reject) =>
      server.once("error", reject).listen(0, "127.0.0.1", resolve),
    );
    const context = await browser.newContext();
    context.on("close", () => order.push("context closed"));
    await context.tracing.start({ snapshots: true });
    await context.route("**/api/positions", async (route) => {
      try {
        const response = await route.fetch();
        assert.equal(response.status(), 200);
        await route.fulfill({ response });
        order.push("route fulfilled");
      } catch (error) {
        routeError = error;
      } finally {
        handlerDone.resolve();
      }
    });
    const page = await context.newPage();
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    // Closing the page after its route completes can cancel the page-side
    // evaluation. The handler completion is the lifecycle boundary here.
    void page.evaluate(() => fetch("/api/positions")).catch(() => {});
    await waitFor(arrived.promise);

    const state = {
      context,
      name: "foreign-fixture",
      advancePosts: [],
      settlementPosts: [],
    };
    const results = { scenarios: [] };
    // Load only the staged teardown function, leaving the acceptance entry
    // point, fixture seeding and business scenarios untouched and unexecuted.
    const finish = new Function(
      "diagnosticIssues",
      "join",
      "out",
      "results",
      "writeFileSync",
      `return (${finishSource[0]});`,
    )(
      () => {
        order.push("diagnostics");
        return [];
      },
      join,
      out,
      results,
      writeFileSync,
    );
    const finishing = finish(state, { fixtureOnly: true });
    if (order.includes("diagnostics")) {
      // The old implementation has already started closing while the server
      // still holds the request. Wait for the actual disposal before failing.
      await waitFor(finishing);
      await waitFor(handlerDone.promise);
      assert.ifError(routeError);
    }
    release.resolve();
    await waitFor(finishing);
    await waitFor(handlerDone.promise);
    assert.ifError(routeError);
    assert.deepEqual(order, [
      "route fulfilled",
      "diagnostics",
      "context closed",
    ]);
    assert.equal(
      JSON.parse(await readFile(receipt, "utf8")).scenarios[0].outcome,
      "PASS",
    );
    assert.ok((await readFile(trace)).length > 0);
  },
);
