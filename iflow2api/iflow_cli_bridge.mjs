import process from "node:process";
import { Buffer } from "node:buffer";
import { once } from "node:events";
import readline from "node:readline";
import { Agent, fetch, ProxyAgent, setGlobalDispatcher } from "undici";

import { buildOfficialChatCompletionsRequest } from "./iflow_cli_request_builder.mjs";

let dispatcherKey = null;
const activeRequests = new Map();

function redirectConsoleToStderr(method) {
  return (...args) => {
    const line = args
      .map((item) => (typeof item === "string" ? item : String(item)))
      .join(" ");
    if (line) {
      process.stderr.write(`[node-bridge:${method}] ${line}\n`);
    }
  };
}

console.log = redirectConsoleToStderr("log");
console.info = redirectConsoleToStderr("info");
console.debug = redirectConsoleToStderr("debug");

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

function createRequestState(timeoutMs) {
  const controller = new AbortController();
  const requestState = {
    controller,
    cancelled: false,
    timedOut: false,
  };
  const timer =
    Number.isFinite(timeoutMs) && timeoutMs > 0
      ? setTimeout(() => {
          requestState.timedOut = true;
          requestState.controller.abort();
        }, timeoutMs)
      : null;
  return { requestState, timer };
}

function releaseTimer(timer) {
  if (timer) {
    clearTimeout(timer);
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

async function emitBufferedResponse(requestId, response) {
  const buffer = Buffer.from(await response.arrayBuffer());
  if (buffer.length > 0) {
    await writeEvent({
      request_id: requestId,
      type: "chunk",
      data_base64: buffer.toString("base64"),
    });
  }
}

async function emitStreamingResponse(requestId, response) {
  if (!response.body) {
    return;
  }
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
}

async function handleBuildRequest(spec) {
  const requestId = typeof spec.request_id === "string" ? spec.request_id : "";
  try {
    const payload = await buildOfficialChatCompletionsRequest(spec.payload || {});
    await writeEvent({
      request_id: requestId,
      type: "result",
      payload,
    });
    await writeEvent({
      request_id: requestId,
      type: "end",
    });
  } catch (error) {
    await writeEvent({
      request_id: requestId,
      type: "error",
      name: error?.name || "Error",
      message: error?.message || String(error),
      code: error?.code || null,
    });
  }
}

async function handleChatCompletions(spec) {
  const requestId = typeof spec.request_id === "string" ? spec.request_id : "";
  const timeoutMs = Number(spec.timeout_ms || 300000);
  const { requestState, timer } = createRequestState(timeoutMs);
  activeRequests.set(requestId, requestState);

  try {
    await ensureDispatcher(spec.proxy);
    const officialRequest = await buildOfficialChatCompletionsRequest(spec.payload || {});
    const response = await fetch(officialRequest.url, {
      method: "POST",
      headers: officialRequest.headers,
      body: JSON.stringify(officialRequest.body),
      signal: requestState.controller.signal,
      redirect: "follow",
    });

    await writeEvent({
      request_id: requestId,
      type: "meta",
      status_code: response.status,
      headers: Object.fromEntries(response.headers.entries()),
      request_meta: officialRequest.meta,
    });

    if (spec.stream) {
      await emitStreamingResponse(requestId, response);
    } else {
      await emitBufferedResponse(requestId, response);
    }

    await writeEvent({
      request_id: requestId,
      type: "end",
    });
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
    releaseTimer(timer);
    activeRequests.delete(requestId);
  }
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
    if (spec?.action === "build_chat_request") {
      void handleBuildRequest(spec);
      continue;
    }
    if (spec?.action === "chat_completions") {
      void handleChatCompletions(spec);
      continue;
    }
    await writeEvent({
      request_id: typeof spec?.request_id === "string" ? spec.request_id : "",
      type: "error",
      name: "UnsupportedAction",
      message: `Unsupported bridge action: ${String(spec?.action || "")}`,
      code: null,
    });
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
