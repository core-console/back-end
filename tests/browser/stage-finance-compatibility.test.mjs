import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  rm,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const entrypoint = fileURLToPath(
  new URL("./stage-finance-compatibility.mjs", import.meta.url),
);

test("fresh campaign staging rebinds the pair and prepares the launcher’s browser runner", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "finance-compatibility-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const seed = join(root, "sealed seed");
  const output = join(root, "fresh campaign");
  await mkdir(seed);

  const oldProducer = "1".repeat(40);
  const oldConsumer = "2".repeat(40);
  const producer = "3".repeat(40);
  const consumer = "4".repeat(40);
  const finish = [
    "async function finish(state, detail) {",
    "  const issues = diagnosticIssues(state);",
    "  await state.context.tracing.stop();",
    "  await state.context.close();",
    "}",
  ];
  const seedFiles = {
    "acceptance.py": `P='${oldProducer}'\nC='${oldConsumer}'\nM=C\n`,
    "run.py": `EXPECTED = {'producer': '${oldProducer}', 'consumer': '${oldConsumer}'}\nBROWSER = OUT / "browser.mjs"\n`,
    "browser.mjs": [
      `const pair = {producer: '${oldProducer}', consumer: '${oldConsumer}'};`,
      ...finish,
      "",
    ].join("\r\n"),
    "database.py":
      "from acceptance import safe_database\ndef target(): return safe_database()\n",
    "browser-results.json": '{"historical": true}\n',
  };
  for (const [name, source] of Object.entries(seedFiles)) {
    await writeFile(join(seed, name), source);
  }

  const stage = (destination) =>
    spawnSync(
      process.execPath,
      [
        entrypoint,
        seed,
        destination,
        "--from-producer",
        oldProducer,
        "--from-consumer",
        oldConsumer,
        "--producer",
        producer,
        "--consumer",
        consumer,
      ],
      { encoding: "utf8", timeout: 10_000 },
    );

  const result = stage(output);
  assert.equal(result.status, 0, result.error?.message ?? result.stderr);
  assert.deepEqual((await readdir(output)).sort(), [
    "acceptance.py",
    "browser.mjs",
    "database.py",
    "run.py",
  ]);
  assert.equal(
    await readFile(join(output, "acceptance.py"), "utf8"),
    `P='${producer}'\nC='${consumer}'\nM=C\n`,
  );
  assert.equal(
    await readFile(join(output, "run.py"), "utf8"),
    `EXPECTED = {'producer': '${producer}', 'consumer': '${consumer}'}\nBROWSER = OUT / "browser.mjs"\n`,
  );
  assert.equal(
    await readFile(join(output, "database.py"), "utf8"),
    seedFiles["database.py"],
  );
  const expectedBrowser = [
    `const pair = {producer: '${producer}', consumer: '${consumer}'};`,
    finish[0],
    '  await state.context.unrouteAll({ behavior: "wait" });',
    ...finish.slice(1),
    "",
  ].join("\r\n");
  assert.equal(
    await readFile(join(output, "browser.mjs"), "utf8"),
    expectedBrowser,
  );

  const retry = stage(output);
  assert.equal(retry.status, 1, retry.error?.message ?? retry.stderr);
  assert.match(retry.stderr, /EEXIST/);
  assert.equal(
    await readFile(join(output, "browser.mjs"), "utf8"),
    expectedBrowser,
  );

  const nested = stage(join(seed, "nested campaign"));
  assert.equal(nested.status, 1, nested.error?.message ?? nested.stderr);
  assert.match(nested.stderr, /outside the seed directory/);
  assert.deepEqual((await readdir(seed)).sort(), Object.keys(seedFiles).sort());
  for (const [name, source] of Object.entries(seedFiles)) {
    assert.deepEqual(await readFile(join(seed, name)), Buffer.from(source));
  }
});
