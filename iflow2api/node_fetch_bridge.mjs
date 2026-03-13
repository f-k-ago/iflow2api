import process from "node:process";
import { Buffer } from "node:buffer";
import { once } from "node:events";
import readline from "node:readline";
import { Agent, fetch, ProxyAgent, setGlobalDispatcher } from "undici";

let dispatcherKey = null;
const activeRequests = new Map();

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

async function writeEvent(event) {
  const line = `${JSON.stringify(event)}\n`;
  if (!process.stdout.write(line)) {
    await once(process.stdout, "drain");
  }
}

async function ensureDispatcher(proxy) {
  const normalizedProxy = typeof proxy === "string" ? proxy.trim() : "";
  if (dispatcherKey === normalizedProxy) {
    return;
  }
  setGlobalDispatcher(normalizedProxy ? new ProxyAgent(normalizedProxy) : new Agent());
  dispatcherKey = normalizedProxy;
}

async function handleRequest(spec) {
  const requestId = typeof spec.request_id === "string" ? spec.request_id : "";
  const timeoutMs = Number(spec.timeout_ms || 300000);
  const requestState = {
    controller: new AbortController(),
    cancelled: false,
    timedOut: false,
  };
  activeRequests.set(requestId, requestState);

  try {
    await ensureDispatcher(spec.proxy);

    const timer = Number.isFinite(timeoutMs) && timeoutMs > 0
      ? setTimeout(() => {
        requestState.timedOut = true;
        requestState.controller.abort();
      }, timeoutMs)
      : null;

    try {
      const response = await fetch(appendParams(spec.url, spec.params), {
        method: spec.method || "GET",
        headers: spec.headers || {},
        body: buildBody(spec),
        signal: requestState.controller.signal,
        redirect: spec.follow_redirects === false ? "manual" : "follow",
      });

      await writeEvent({
        request_id: requestId,
        type: "meta",
        status_code: response.status,
        headers: Object.fromEntries(response.headers.entries()),
      });

      if (response.body) {
        if (spec.stream) {
          const reader = response.body.getReader();
          while (true) {
            const { done, value } = await reader.read();
            if (done) {
              break;
            }
            await writeEvent({
              request_id: requestId,
              type: "chunk",
              data_base64: Buffer.from(value).toString("base64"),
            });
          }
        } else {
          const buffer = Buffer.from(await response.arrayBuffer());
          if (buffer.length > 0) {
            await writeEvent({
              request_id: requestId,
              type: "chunk",
              data_base64: buffer.toString("base64"),
            });
          }
        }
      }

      await writeEvent({
        request_id: requestId,
        type: "end",
      });
    } finally {
      if (timer) {
        clearTimeout(timer);
      }
    }
  } catch (error) {
    if (requestState.cancelled) {
      await writeEvent({
        request_id: requestId,
        type: "aborted",
      });
      return;
    }

    await writeEvent({
      request_id: requestId,
      type: "error",
      name: error?.name || "Error",
      message: requestState.timedOut
        ? `Request timed out after ${timeoutMs}ms`
        : (error?.message || String(error)),
      code: error?.code || null,
    });
  } finally {
    activeRequests.delete(requestId);
  }
}

function handleCancel(spec) {
  const requestId = typeof spec.request_id === "string" ? spec.request_id : "";
  const requestState = activeRequests.get(requestId);
  if (!requestState) {
    return;
  }
  requestState.cancelled = true;
  requestState.controller.abort();
}

const rl = readline.createInterface({
  input: process.stdin,
  crlfDelay: Infinity,
});

for await (const line of rl) {
  const text = line.trim();
  if (!text) {
    continue;
  }
  try {
    const spec = JSON.parse(text);
    if (spec?.action === "cancel") {
      handleCancel(spec);
      continue;
    }
    void handleRequest(spec);
  } catch (error) {
    await writeEvent({
      request_id: "",
      type: "error",
      name: error?.name || "Error",
      message: error?.message || String(error),
      code: error?.code || null,
    });
  }
}
