import process from "node:process";
import { Buffer } from "node:buffer";
import { once } from "node:events";

const META_PREFIX = "__IFLOW_NODE_META__=";
const ERROR_PREFIX = "__IFLOW_NODE_ERROR__=";

function writeMeta(payload) {
  process.stderr.write(`${META_PREFIX}${JSON.stringify(payload)}\n`);
}

function writeError(error) {
  const payload = {
    name: error?.name || "Error",
    message: error?.message || String(error),
    code: error?.code || null,
    stack: error?.stack || null,
  };
  process.stderr.write(`${ERROR_PREFIX}${JSON.stringify(payload)}\n`);
}

function appendParams(rawUrl, params) {
  const url = new URL(rawUrl);
  if (!params || typeof params !== "object") {
    return url.toString();
  }
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null) {
      continue;
    }
    if (Array.isArray(value)) {
      for (const item of value) {
        url.searchParams.append(key, String(item));
      }
      continue;
    }
    url.searchParams.set(key, String(value));
  }
  return url.toString();
}

function buildBody(spec) {
  if (Object.prototype.hasOwnProperty.call(spec, "json_body")) {
    return JSON.stringify(spec.json_body);
  }
  if (typeof spec.data_base64 === "string" && spec.data_base64) {
    return Buffer.from(spec.data_base64, "base64");
  }
  if (Object.prototype.hasOwnProperty.call(spec, "data_text")) {
    return spec.data_text;
  }
  return undefined;
}

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) {
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  return Buffer.concat(chunks).toString("utf-8");
}

async function writeChunk(chunk) {
  if (!chunk || chunk.length === 0) {
    return;
  }
  if (!process.stdout.write(chunk)) {
    await once(process.stdout, "drain");
  }
}

async function pipeResponseBody(response, stream) {
  if (!response.body) {
    return;
  }
  if (!stream) {
    const buffer = Buffer.from(await response.arrayBuffer());
    await writeChunk(buffer);
    return;
  }
  const reader = response.body.getReader();
  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    await writeChunk(Buffer.from(value));
  }
}

async function main() {
  try {
    const raw = await readStdin();
    const spec = raw ? JSON.parse(raw) : {};
    const timeoutMs = Number(spec.timeout_ms || 300000);
    const controller = new AbortController();
    const timer = Number.isFinite(timeoutMs) && timeoutMs > 0
      ? setTimeout(() => controller.abort(new Error(`Request timed out after ${timeoutMs}ms`)), timeoutMs)
      : null;

    try {
      const response = await fetch(appendParams(spec.url, spec.params), {
        method: spec.method || "GET",
        headers: spec.headers || {},
        body: buildBody(spec),
        signal: controller.signal,
        redirect: spec.follow_redirects === false ? "manual" : "follow",
      });

      writeMeta({
        status_code: response.status,
        headers: Object.fromEntries(response.headers.entries()),
      });
      await pipeResponseBody(response, Boolean(spec.stream));
    } finally {
      if (timer) {
        clearTimeout(timer);
      }
    }
  } catch (error) {
    writeError(error);
    process.exitCode = 1;
  }
}

await main();
