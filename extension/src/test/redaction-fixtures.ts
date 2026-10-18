// Shared loader for tests/fixtures/redaction/, the HAR entries with
// credentials in them that the masking tests run over. The same files feed
// tests/unit/test_redaction_parity.py, which runs the Python analyzer on each
// entry and on its masked copy (<name>.redacted.json, written by
// lib/redact.test.ts).
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect } from "vitest";

const here = path.dirname(fileURLToPath(import.meta.url));

export const REDACTION_FIXTURES_DIR = path.resolve(
  here,
  "..",
  "..",
  "..",
  "tests",
  "fixtures",
  "redaction",
);

export interface RedactionFixture {
  description: string;
  expect_findings: string[];
  // Values that must not appear anywhere in the payload sent to /api/analyze.
  secrets: string[];
  entry: unknown;
}

export interface RedactionScenario {
  name: string;
  fixture: RedactionFixture;
}

export function loadRedactionScenarios(): RedactionScenario[] {
  return fs
    .readdirSync(REDACTION_FIXTURES_DIR)
    .filter(
      (f) =>
        f.endsWith(".json") &&
        !f.endsWith(".redacted.json") &&
        f !== "check-read-headers.json",
    )
    .sort()
    .map((f) => ({
      name: f.replace(/\.json$/, ""),
      fixture: JSON.parse(
        fs.readFileSync(path.join(REDACTION_FIXTURES_DIR, f), "utf8"),
      ) as RedactionFixture,
    }));
}

export function loadRedactionScenario(name: string): RedactionFixture {
  const found = loadRedactionScenarios().find((s) => s.name === name);
  if (!found) throw new Error(`No redaction fixture named ${name}`);
  return found.fixture;
}

// Every 8-character piece of a secret (the whole secret when shorter), so a
// mask that keeps part of a value still fails the leak check.
export function secretPieces(secret: string): string[] {
  if (secret.length <= 8) return [secret];
  const pieces: string[] = [];
  for (let i = 0; i + 8 <= secret.length; i++) pieces.push(secret.slice(i, i + 8));
  return pieces;
}

export function expectNoSecrets(text: string, secrets: string[]): void {
  for (const secret of secrets) {
    for (const piece of secretPieces(secret)) {
      expect(text, `leaked part of ${secret}`).not.toContain(piece);
    }
  }
}
