import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const SUPPRESS_MAIN_FLAG = "__IFLOW_SUPPRESS_MAIN__";
const OFFICIAL_EXPORTS = ["gH", "MOt", "h2"];
const DEFAULT_BUNDLE_CANDIDATES = [
  process.env.IFLOW_OFFICIAL_BUNDLE_PATH || "",
  fileURLToPath(new URL("../../../package/bundle/iflow.js", import.meta.url)),
];

const MAIN_ENTRY_PATTERN =
  /Eao\(\)\.catch\(t\s*=>\s*\{\s*console\.error\("An unexpected critical error occurred:"\)\s*,\s*t\s+instanceof\s+Error\s*\?\s*console\.error\(t\.stack\)\s*:\s*console\.error\(String\(t\)\)\s*,\s*process\.exit\(1\)\s*\}\s*\);?/;

let cachedHelpersPromise = null;

function createOfficialBundleRequiredError(message) {
  const error = new Error(message);
  error.name = "OfficialBundleRequiredError";
  error.code = "official_bundle_required";
  return error;
}

function normalizeBundlePath(value) {
  if (typeof value !== "string") {
    return "";
  }
  return value.trim();
}

async function firstExistingBundlePath() {
  const disabled = String(process.env.IFLOW_DISABLE_OFFICIAL_BUNDLE_SHIM || "")
    .trim()
    .toLowerCase();
  if (disabled === "1" || disabled === "true" || disabled === "yes") {
    return "";
  }
  for (const candidate of DEFAULT_BUNDLE_CANDIDATES) {
    const normalized = normalizeBundlePath(candidate);
    if (!normalized) {
      continue;
    }
    try {
      const stat = await fs.stat(normalized);
      if (stat.isFile()) {
        return normalized;
      }
    } catch {
      // ignore and try next candidate
    }
  }
  return "";
}

function patchOfficialBundleSource(sourceText) {
  const mainEntryMatch = sourceText.match(MAIN_ENTRY_PATTERN);
  if (!mainEntryMatch) {
    throw new Error("官方 bundle 顶层入口锚点未命中，无法生成 patched shim");
  }
  const mainEntry = mainEntryMatch[0];
  const patchedMain = `if (!globalThis.${SUPPRESS_MAIN_FLAG}) {\n${mainEntry}\n}`;
  const exportFooter = `\nexport { ${OFFICIAL_EXPORTS.join(", ")} };\n`;
  return sourceText.replace(mainEntry, patchedMain) + exportFooter;
}

async function ensurePatchedShim(bundlePath) {
  const bundleSource = await fs.readFile(bundlePath, "utf8");
  const patchedSource = patchOfficialBundleSource(bundleSource);
  const digest = crypto.createHash("sha256").update(patchedSource).digest("hex").slice(0, 16);
  const shimDir = path.join(path.dirname(bundlePath), ".iflow2api_shims");
  const shimPath = path.join(shimDir, `iflow_official_bundle_${digest}.mjs`);
  await fs.mkdir(shimDir, { recursive: true });
  try {
    await fs.access(shimPath);
  } catch {
    await fs.writeFile(shimPath, patchedSource, "utf8");
  }
  return { shimPath, digest };
}

export async function loadOfficialBundleHelpers() {
  if (!cachedHelpersPromise) {
    cachedHelpersPromise = (async () => {
      const bundlePath = await firstExistingBundlePath();
      if (!bundlePath) {
        throw createOfficialBundleRequiredError(
          "当前已强制要求 patched 官方 bundle，但未找到可用的 package/bundle/iflow.js。请设置 IFLOW_OFFICIAL_BUNDLE_PATH，或使用已内置官方 bundle 的 Docker 镜像。",
        );
      }
      const { shimPath, digest } = await ensurePatchedShim(bundlePath);
      globalThis[SUPPRESS_MAIN_FLAG] = true;
      const shimUrl = `${pathToFileURL(shimPath).href}?v=${digest}`;
      const module = await import(shimUrl);
      if (!module?.gH || !module?.MOt || !module?.h2) {
        throw new Error("patched 官方 bundle 未导出 gH/MOt/h2");
      }
      return {
        ...module,
        bundlePath,
        shimPath,
        source: "official_bundle",
      };
    })();
  }
  try {
    return await cachedHelpersPromise;
  } catch (error) {
    cachedHelpersPromise = null;
    throw error;
  }
}

export function resetOfficialBundleHelpersForTests() {
  cachedHelpersPromise = null;
}
