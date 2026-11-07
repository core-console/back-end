import assert from "node:assert/strict";
import { mkdir, readFile, realpath, writeFile } from "node:fs/promises";
import {
  basename,
  dirname,
  isAbsolute,
  join,
  relative,
  resolve,
  sep,
} from "node:path";
import { parseArgs } from "node:util";

import { prepareFinanceCompatibility } from "./prepare-finance-compatibility.mjs";

const { positionals, values } = parseArgs({
  allowPositionals: true,
  options: {
    "from-producer": { type: "string" },
    "from-consumer": { type: "string" },
    producer: { type: "string" },
    consumer: { type: "string" },
  },
});
const [seedPath, outputPath] = positionals;
const {
  "from-producer": oldProducer,
  "from-consumer": oldConsumer,
  producer,
  consumer,
} = values;
assert.ok(
  positionals.length === 2 &&
    [oldProducer, oldConsumer, producer, consumer].every((sha) =>
      /^[0-9a-f]{40}$/.test(sha),
    ),
  "Usage: node tests/browser/stage-finance-compatibility.mjs <seed-dir> <fresh-dir> --from-producer <sha> --from-consumer <sha> --producer <sha> --consumer <sha>",
);
assert.notEqual(
  oldProducer,
  oldConsumer,
  "Expected distinct producer and consumer pins in the seed",
);

const seed = await realpath(seedPath);
const output = resolve(outputPath);
const destination = join(await realpath(dirname(output)), basename(output));
const relativeOutput = relative(seed, destination);
assert.ok(
  isAbsolute(relativeOutput) ||
    relativeOutput === ".." ||
    relativeOutput.startsWith(`..${sep}`),
  "The fresh output must be outside the seed directory",
);

const decoder = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });
const pairPins = new RegExp(`${oldProducer}|${oldConsumer}`, "g");
const files = new Map([
  ["database.py", await readFile(join(seed, "database.py"))],
]);
for (const name of ["acceptance.py", "run.py", "browser.mjs"]) {
  const source = decoder.decode(await readFile(join(seed, name)));
  for (const sha of [oldProducer, oldConsumer]) {
    assert.equal(
      source.split(sha).length - 1,
      1,
      `Expected one occurrence of ${sha} in ${name}`,
    );
  }
  const rebound = source.replace(pairPins, (sha) =>
    sha === oldProducer ? producer : consumer,
  );
  files.set(
    name,
    name === "browser.mjs" ? prepareFinanceCompatibility(rebound) : rebound,
  );
}

// Validate the whole seed before reserving a fresh directory. Never reuse one.
await mkdir(destination);
for (const [name, contents] of files) {
  await writeFile(join(destination, name), contents, { flag: "wx" });
}
