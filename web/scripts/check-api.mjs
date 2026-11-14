// Fails when src/api/schema.d.ts is not what openapi-typescript makes from schemas/openapi.json.
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const dir = mkdtempSync(join(tmpdir(), "api-"));
const out = join(dir, "schema.d.ts");
try {
  const cli = join("node_modules", "openapi-typescript", "bin", "cli.js");
  execFileSync(process.execPath, [cli, "../schemas/openapi.json", "-o", out], { stdio: "ignore" });
  const same = readFileSync(out, "utf8") === readFileSync("src/api/schema.d.ts", "utf8");
  if (!same) {
    console.error("src/api/schema.d.ts is stale: run `pnpm gen:api` and commit it.");
    process.exit(1);
  }
  console.log("typed client is up to date");
} finally {
  rmSync(dir, { recursive: true, force: true });
}
