// Run only from the protected pytest integration. All business HTTP reaches the
// real backend. Interception discards bytes only after independent SQL evidence.
import { createRequire } from "node:module";
import { randomUUID } from "node:crypto";
import { pathToFileURL } from "node:url";

const [consumer, backend] = process.argv.slice(2);
const require = createRequire(`${consumer}/package.json`);
const { chromium, expect, request } = require("@playwright/test");
const { AxeBuilder } = require("@axe-core/playwright");
const { preview } = await import(pathToFileURL(require.resolve("vite")));
const previewServer = await preview({
  root: consumer,
  preview: { host: "127.0.0.1", port: 0, strictPort: true },
});
const base = `http://127.0.0.1:${previewServer.httpServer.address().port}`;
const browser = await chromium.launch();
const api = await request.newContext({ baseURL: backend });
const actor = await (await api.get("/api/me")).json();
const money = (amount) => ({ amount, currency: "CNY" });
const day = "2026-10-02";
const evidence = [];

async function post(path, body) {
  const result = await api.post(path, {
    data: body,
    headers: {
      "Idempotency-Key": randomUUID(),
      "Finance-Command-Version": "1",
      "Finance-Submission-Owner": actor.id,
    },
  });
  expect(result.status(), await result.text()).toBe(
    path.endsWith("balance-adjustments") ? 200 : 201,
  );
  return result.json();
}

async function stored(page) {
  return page.evaluate(async () => {
    const db = await new Promise((resolve, reject) => {
      const open = indexedDB.open("core-console.finance.submissions", 1);
      open.onsuccess = () => resolve(open.result);
      open.onerror = () => reject(open.error);
    });
    try {
      return await new Promise((resolve, reject) => {
        const tx = db.transaction("submissions", "readonly");
        const read = tx.objectStore("submissions").getAll();
        tx.oncomplete = () => resolve(read.result);
        tx.onabort = () => reject(tx.error);
      });
    } finally {
      db.close();
    }
  });
}

async function sql(key) {
  const response = await api.get(`/t11-test/evidence/${key}`);
  expect(response.status()).toBe(200);
  return response.json();
}

async function run(scenario) {
  console.log(`START ${scenario}`);
  const isLedger = scenario.endsWith("ledger");
  let ledger;
  let account;
  let destination;
  if (!isLedger) {
    ledger = (await post("/api/finance/ledgers", { name: scenario })).outcome
      .resource.id;
    for (const name of ["Cash", "Reserve"]) {
      const id = (
        await post(`/api/finance/ledgers/${ledger}/accounts`, {
          name,
          currency: "CNY",
          nature: "asset",
          openingBalance: money("0"),
          trackingStartDate: "2026-10-01",
        })
      ).outcome.resource.id;
      if (name === "Cash") account = id;
      else destination = id;
    }
  }
  const prefix = `/api/finance/ledgers/${ledger}`;
  const context = await browser.newContext();
  // Force the two-tab qualification to rely on IndexedDB and backend serialization.
  if (scenario === "expense")
    await context.addInitScript(() => {
      globalThis.BroadcastChannel = undefined;
    });
  const pages = [];
  const errors = [];
  const expectedHttpErrors = [];
  function watch(page) {
    pages.push(page);
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error")
        errors.push({ message: message.text(), url: message.location().url });
    });
    return page;
  }
  const page = watch(await context.newPage());
  page.setDefaultTimeout(10_000);
  const posts = [];
  const lookups = [];
  let receipt;
  let originalStatus;
  let originalResponse;
  let committed;
  let hideLookup = true;
  let barrier;
  let release;
  let retryArrivals = 0;
  let intentional = false;
  let additionalReceipt;
  let deletedResource = false;
  await context.route("**/api/**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const isPost = req.method() === "POST";
    if (isPost) {
      const headers = req.headers();
      const record = {
        key: headers["idempotency-key"],
        owner: headers["finance-submission-owner"],
        version: headers["finance-command-version"],
        path: url.pathname,
        body: req.postData(),
      };
      expect(record.owner).toBe(actor.id);
      expect(record.version).toBe("1");
      expect(await stored(route.request().frame().page())).toEqual(
        expect.arrayContaining([
          expect.objectContaining({
            submissionId: record.key,
            state: "unresolved",
            body: JSON.parse(record.body),
          }),
        ]),
      );
      posts.push(record);
      if (barrier) {
        retryArrivals += 1;
        if (retryArrivals === 2) release();
        await barrier;
      }
    }
    const response = await route.fetch({
      url: `${backend}${url.pathname}${url.search}`,
      maxRetries: 0,
    });
    if (
      (deletedResource &&
        req.method() === "GET" &&
        url.pathname ===
          `${prefix}/transactions/${receipt.outcome.resource.id}` &&
        response.status() === 404) ||
      (isPost &&
        receipt?.outcome.kind === "rejected" &&
        response.status() === originalStatus)
    ) {
      expectedHttpErrors.push({
        url: req.url(),
        message: `Failed to load resource: the server responded with a status of ${response.status()} (${response.statusText()})`,
      });
    }
    if (url.pathname.includes("/submissions/")) {
      expect(response.headers()["cache-control"]).toBe("no-store");
      const result = await response.json();
      expect(result).toEqual({ state: "terminal", receipt });
      lookups.push(result);
      if (hideLookup)
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: "{",
        });
    }
    if (isPost) {
      const result = await response.json();
      const terminal = result.submissionReceipt ?? result;
      if (intentional) {
        const fresh = posts.at(-1);
        expect(fresh.key).not.toBe(posts[0].key);
        expect({ ...fresh, key: posts[0].key }).toEqual(posts[0]);
        expect(response.status()).toBe(201);
        expect(terminal.outcome.resource.id).not.toBe(
          receipt.outcome.resource.id,
        );
        additionalReceipt = terminal;
        const effects = await sql(fresh.key);
        expect(effects.transactions).toHaveLength(2);
        expect(effects.movements.map((row) => row.amount)).toEqual([
          "-12.30",
          "-12.30",
        ]);
        expect(effects.allocations.map((row) => row.amount)).toEqual([
          "12.30",
          "12.30",
        ]);
        return route.fulfill({ response });
      }
      if (!receipt) {
        receipt = terminal;
        originalStatus = response.status();
        originalResponse = result;
        expect(originalStatus).toBe(
          scenario.startsWith("rejected")
            ? 409
            : scenario.startsWith("adjustment")
              ? 200
              : 201,
        );
        committed = await sql(posts[0].key);
        expect(committed.submission.terminal_outcome).toEqual(receipt.outcome);
        expect(committed.submission.resolved_at).toBeTruthy();
        expect(committed.submission.canonical_command.operation).toBe(
          receipt.operation,
        );
        expect(receipt.submissionId).toBe(posts[0].key);
        expect(receipt.targetLedgerId).toBe(isLedger ? null : ledger);
        // A separate PostgreSQL session observes commit before we abort the
        // browser delivery. The real server response never reaches the client.
        expectedHttpErrors.push({
          url: req.url(),
          message: "Failed to load resource: net::ERR_FAILED",
        });
        return route.abort("failed");
      }
      expect(posts.at(-1)).toEqual(posts[0]);
      expect(response.status()).toBe(originalStatus);
      expect(result).toEqual(originalResponse);
    }
    await route.fulfill({ response });
  });
  try {
    let form;
    if (isLedger) {
      await page.goto(`${base}/finance/accounts`);
      if (scenario === "additional-ledger") {
        await page
          .getByRole("button", {
            name: "Renamed onboarding-ledger",
            exact: true,
          })
          .click();
        await page.getByRole("menuitem", { name: "Create Ledger" }).click();
        form = page.getByRole("dialog", { name: "Create Ledger" });
      }
      await page.getByLabel("Ledger name").fill(`T11 ${scenario}`);
      await page
        .getByRole("button", {
          name: form ? "Create" : "Create Ledger",
          exact: true,
        })
        .click();
    } else if (scenario === "account" || scenario === "category") {
      await page.goto(
        `${base}/finance/${scenario === "category" ? "categories" : "accounts"}?ledger=${ledger}`,
      );
      await page
        .getByRole("button", { name: `Create ${scenario}`, exact: true })
        .click();
      form = page.getByRole("dialog");
      await form
        .getByLabel(`${scenario === "account" ? "Account" : "Category"} name`)
        .fill("Recovered");
      if (scenario === "account") {
        await form.getByLabel("Nature").selectOption("liability");
        await form.getByLabel("Opening balance").fill("-9007199254740993.01");
        await form.getByLabel("Tracking start date").fill("2026-10-01");
      }
      await form
        .getByRole("button", { name: `Create ${scenario}`, exact: true })
        .click();
    } else {
      await page.goto(`${base}/finance/transactions?ledger=${ledger}`);
      await page
        .getByRole("button", { name: "Record transaction", exact: true })
        .first()
        .click();
      const adjustment = scenario.includes("adjustment");
      const kind = adjustment
        ? "Balance Adjustment"
        : scenario === "income"
          ? "Income"
          : scenario === "transfer"
            ? "Internal Transfer"
            : "Expense";
      await page.getByRole("menuitem", { name: kind, exact: true }).click();
      form = page.getByRole("dialog");
      await form.getByLabel("Transaction date").fill(day);
      await form
        .getByLabel(
          adjustment
            ? "Account"
            : scenario === "transfer"
              ? "Source Account"
              : "Account",
          { exact: true },
        )
        .selectOption(account);
      if (adjustment) {
        await expect(form.getByLabel("Target balance")).toBeEnabled();
        await form
          .getByLabel("Target balance")
          .fill(scenario === "adjustment-noChange" ? "0" : "12.30");
      } else {
        await form.getByLabel("Amount", { exact: true }).fill("12.30");
        if (scenario === "transfer")
          await form
            .getByLabel("Destination Account")
            .selectOption(destination);
      }
      await form.getByLabel("Note", { exact: true }).fill("Original command");
      if (scenario === "rejected-transaction") {
        expect(
          (await api.post(`${prefix}/accounts/${account}/archive`)).status(),
        ).toBe(200);
      }
      if (scenario === "rejected-adjustment") {
        await post(`${prefix}/balance-adjustments`, {
          accountId: account,
          transactionDate: day,
          expectedDerivedBalance: money("0"),
          expectedAccountNature: "asset",
          targetBalance: money("1"),
        });
      }
      await form
        .getByRole("button", {
          name: adjustment
            ? "Record adjustment"
            : scenario === "transfer"
              ? "Record transfer"
              : `Record ${scenario === "income" ? "income" : "expense"}`,
          exact: true,
        })
        .click();
    }
    if (form) {
      await expect(form.getByRole("alert").first()).toContainText(
        "outcome is unknown",
      );
      await form.getByRole("button", { name: "Cancel", exact: true }).click();
    }
    const recovery = page.getByRole("region", {
      name: "Finance submission recovery",
    });
    await expect(recovery).toContainText("outcome is unknown");
    expect(posts).toHaveLength(1);
    const key = posts[0].key;
    const resource = receipt.outcome.resource?.id;
    // Direct SQL checks are distinct from UI/HTTP projections.
    if (isLedger)
      expect(
        committed.ledgers.filter((row) => row.id === resource),
      ).toHaveLength(1);
    else if (scenario === "account") {
      expect(committed.accounts).toHaveLength(3);
      expect(
        committed.accounts.find((row) => row.id === resource),
      ).toMatchObject({
        nature: "liability",
        opening_balance: "-9007199254740993.01",
      });
      expect(committed.transactions).toEqual([]);
      expect(committed.movements).toEqual([]);
    } else if (scenario === "category")
      expect(committed.categories).toEqual([
        { id: resource, name: "Recovered", status: "active" },
      ]);
    else if (
      scenario === "adjustment-noChange" ||
      scenario === "rejected-transaction"
    ) {
      expect(committed.transactions).toEqual([]);
      expect(committed.movements).toEqual([]);
      expect(receipt.outcome.kind).toBe(
        scenario === "adjustment-noChange" ? "noChange" : "rejected",
      );
    } else {
      expect(committed.transactions).toHaveLength(1);
      if (receipt.outcome.kind === "created")
        expect(committed.transactions[0].id).toBe(resource);
      const amounts = committed.movements.map((row) => row.amount).sort();
      expect(amounts).toEqual(
        scenario === "transfer"
          ? ["-12.30", "12.30"]
          : [
              scenario === "expense"
                ? "-12.30"
                : scenario === "rejected-adjustment"
                  ? "1"
                  : "12.30",
            ],
      );
      if (scenario === "transfer") {
        expect(committed.movements).toEqual(
          expect.arrayContaining([
            { account_id: account, amount: "-12.30" },
            { account_id: destination, amount: "12.30" },
          ]),
        );
      }
      expect(committed.allocations.map((row) => row.amount)).toEqual(
        ["income", "expense"].includes(scenario) ? ["12.30"] : [],
      );
    }
    // Change mutable state before recovery. Original outcomes must remain authoritative.
    if (isLedger)
      expect(
        (
          await api.patch(`/api/finance/ledgers/${resource}`, {
            data: { name: `Renamed ${scenario}` },
          })
        ).status(),
      ).toBe(200);
    else if (scenario === "account")
      expect(
        (
          await api.patch(`${prefix}/accounts/${resource}`, {
            data: { name: "Renamed account" },
          })
        ).status(),
      ).toBe(200);
    else if (scenario === "category") {
      expect(
        (
          await api.patch(`${prefix}/categories/${resource}`, {
            data: { name: "Renamed category" },
          })
        ).status(),
      ).toBe(200);
      expect(
        (await api.post(`${prefix}/categories/${resource}/archive`)).status(),
      ).toBe(200);
    } else if (scenario === "rejected-transaction")
      expect(
        (await api.post(`${prefix}/accounts/${account}/unarchive`)).status(),
      ).toBe(200);
    else if (scenario === "rejected-adjustment")
      expect(
        (
          await api.delete(
            `${prefix}/transactions/${committed.transactions[0].id}`,
          )
        ).status(),
      ).toBe(204);
    else if (scenario === "adjustment-noChange")
      await post(`${prefix}/balance-adjustments`, {
        accountId: account,
        transactionDate: day,
        expectedDerivedBalance: money("0"),
        expectedAccountNature: "asset",
        targetBalance: money("9.99"),
      });
    else if (scenario !== "expense") {
      expect(
        (await api.delete(`${prefix}/transactions/${resource}`)).status(),
      ).toBe(204);
      deletedResource = true;
    }
    const afterMutation = await sql(key);
    expect(afterMutation.submission).toEqual(committed.submission);
    if (isLedger)
      expect(
        afterMutation.ledgers.find((row) => row.id === resource).name,
      ).toBe(`Renamed ${scenario}`);
    else if (scenario === "account")
      expect(
        afterMutation.accounts.find((row) => row.id === resource).name,
      ).toBe("Renamed account");
    else if (scenario === "category")
      expect(afterMutation.categories).toEqual([
        { id: resource, name: "Renamed category", status: "archived" },
      ]);
    else if (scenario === "rejected-transaction")
      expect(
        afterMutation.accounts.find((row) => row.id === account).status,
      ).toBe("active");
    else if (scenario === "adjustment-noChange") {
      expect(afterMutation.transactions).toHaveLength(1);
      expect(afterMutation.movements).toEqual([
        { account_id: account, amount: "9.99" },
      ]);
    } else if (scenario !== "expense") {
      expect(afterMutation.transactions).toEqual([]);
      expect(afterMutation.movements).toEqual([]);
      expect(afterMutation.allocations).toEqual([]);
    }
    if (scenario === "additional-ledger") hideLookup = false;
    await page.reload();
    if (scenario !== "additional-ledger") {
      await expect(
        recovery.getByRole("button", { name: "Retry original submission" }),
      ).toBeEnabled();
      expect((await stored(page))[0]).toMatchObject({
        submissionId: key,
        state: "unresolved",
        body: JSON.parse(posts[0].body),
      });
    }
    expect(posts).toHaveLength(1);
    if (scenario === "additional-ledger") {
      await expect
        .poll(async () => (await stored(page))[0]?.state)
        .toBe("resolved");
      const replay = await api.post(posts[0].path, {
        data: JSON.parse(posts[0].body),
        headers: {
          "Idempotency-Key": key,
          "Finance-Submission-Owner": actor.id,
          "Finance-Command-Version": "1",
        },
      });
      expect(replay.status()).toBe(originalStatus);
      expect(await replay.json()).toEqual(originalResponse);
    } else if (scenario === "expense") {
      const other = watch(await context.newPage());
      await other.goto(page.url());
      const retry = other.getByRole("button", {
        name: "Retry original submission",
      });
      await expect(retry).toBeEnabled();
      barrier = new Promise((resolve) => {
        release = resolve;
      });
      await Promise.all([
        recovery
          .getByRole("button", { name: "Retry original submission" })
          .click(),
        retry.click(),
      ]);
      await expect.poll(() => posts.length).toBe(3);
      await expect
        .poll(async () => (await stored(other))[0]?.state)
        .toBe("resolved");
    } else {
      await recovery
        .getByRole("button", { name: "Retry original submission" })
        .focus();
      await page.keyboard.press("Enter");
    }
    await expect
      .poll(async () => (await stored(page))[0]?.state)
      .toBe("resolved");
    expect(await sql(key)).toEqual(afterMutation);
    hideLookup = false;
    const lookup = await api.get(`/api/finance/submissions/${key}`, {
      headers: { "Finance-Submission-Owner": actor.id },
    });
    expect(lookup.headers()["cache-control"]).toBe("no-store");
    expect(await lookup.json()).toEqual({ state: "terminal", receipt });
    await page.reload();
    await expect(
      recovery.getByRole("button", { name: "Acknowledge outcome" }),
    ).toBeVisible();
    expect(posts).toHaveLength(
      scenario === "expense" ? 3 : scenario === "additional-ledger" ? 1 : 2,
    );
    // Inspect settled rendering rather than sampling a partially faded label.
    await page.evaluate(async () => {
      await document.fonts.ready;
      await Promise.all(
        document
          .getAnimations()
          .filter(
            (animation) =>
              animation.effect?.getTiming().iterations !== Infinity,
          )
          .map((animation) => animation.finished.catch(() => {})),
      );
    });
    const axe = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
      .analyze();
    expect(axe.violations).toEqual([]);
    await recovery.getByRole("button", { name: "Acknowledge outcome" }).click();
    await expect.poll(() => stored(page)).toEqual([]);
    if (scenario === "expense") {
      barrier = undefined;
      intentional = true;
      await page
        .getByRole("button", { name: "Record transaction", exact: true })
        .first()
        .click();
      await page
        .getByRole("menuitem", { name: "Expense", exact: true })
        .click();
      const freshForm = page.getByRole("dialog");
      await freshForm.getByLabel("Transaction date").fill(day);
      await freshForm
        .getByLabel("Account", { exact: true })
        .selectOption(account);
      await freshForm.getByLabel("Amount", { exact: true }).fill("12.30");
      await freshForm
        .getByLabel("Note", { exact: true })
        .fill("Original command");
      await freshForm
        .getByRole("button", { name: "Record expense", exact: true })
        .click();
      await expect.poll(() => additionalReceipt?.outcome.kind).toBe("created");
      await expect
        .poll(async () => (await stored(page))[0]?.state)
        .toBe("resolved");
    }
    expect(
      errors.filter(
        (error) =>
          !expectedHttpErrors.some(
            (expected) =>
              expected.url === error.url && expected.message === error.message,
          ),
      ),
    ).toEqual([]);
    evidence.push({
      scenario,
      originalStatus,
      receipt,
      posts,
      committed,
      afterMutation,
      additionalReceipt,
      expectedHttpErrors,
      lookups: lookups.length,
    });
    console.log(`PASS ${scenario}`);
  } finally {
    await context.close();
  }
}

try {
  for (const scenario of [
    "onboarding-ledger",
    "additional-ledger",
    "account",
    "category",
    "income",
    "expense",
    "transfer",
    "adjustment-created",
    "adjustment-noChange",
    "rejected-transaction",
    "rejected-adjustment",
  ])
    await run(scenario);
  console.log(
    JSON.stringify({
      scenarios: evidence.length,
      node: process.version,
      browser: browser.version(),
      evidence,
    }),
  );
} finally {
  await api.dispose();
  await browser.close();
  await new Promise((resolve, reject) =>
    previewServer.httpServer.close((error) =>
      error ? reject(error) : resolve(),
    ),
  );
}
