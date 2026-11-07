import assert from "node:assert/strict";
import { readFile, writeFile } from "node:fs/promises";

// Use this preparer when staging browser.mjs for a new compatibility run.
// The input can be a sealed runner. The output must be a new file.
export function prepareFinanceCompatibility(source) {
  const start =
    /^async function finish\(state, detail\) \{(\r?\n)  const issues = diagnosticIssues\(state\);/gm;
  assert.equal(
    [...source.matchAll(start)].length,
    1,
    "Expected one unpatched compatibility finish function",
  );
  return source.replace(start, (opening, newline) =>
    opening.replace(
      newline,
      `${newline}  await state.context.unrouteAll({ behavior: "wait" });${newline}`,
    ),
  );
}

if (import.meta.main) {
  const [sourcePath, outputPath, ...extra] = process.argv.slice(2);
  assert.ok(
    sourcePath && outputPath && !extra.length,
    "Usage: node tests/browser/prepare-finance-compatibility.mjs <source> <new browser.mjs>",
  );
  const source = await readFile(sourcePath, "utf8");
  await writeFile(outputPath, prepareFinanceCompatibility(source), {
    flag: "wx",
  });
}
