import { prepareOfficialBundleExecution } from "./iflow_cli_official_roundtrip.mjs";

function normalizeBaseUrl(baseUrl) {
  if (typeof baseUrl !== "string") {
    return "";
  }
  return baseUrl.endsWith("/") ? baseUrl.slice(0, -1) : baseUrl;
}

function resolveScene(baseUrl) {
  return normalizeBaseUrl(baseUrl).toLowerCase().includes("ducky.code.alibaba-inc.com")
    ? "aone"
    : "global";
}

function isPositiveInteger(value) {
  return Number.isInteger(value) && value > 0;
}

function headersToObject(headers) {
  if (!headers) {
    return {};
  }
  if (typeof Headers !== "undefined" && headers instanceof Headers) {
    return Object.fromEntries(headers.entries());
  }
  if (Array.isArray(headers)) {
    return Object.fromEntries(headers.map(([key, value]) => [String(key), String(value)]));
  }
  if (typeof headers === "object") {
    return Object.fromEntries(
      Object.entries(headers).map(([key, value]) => [String(key), String(value)]),
    );
  }
  return {};
}

function buildCaptureResponse(stream) {
  if (stream) {
    return new Response("", {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });
  }
  return new Response(
    JSON.stringify({
      choices: [
        {
          message: { role: "assistant", content: "ok" },
          finish_reason: "stop",
        },
      ],
      usage: {
        prompt_tokens: 1,
        completion_tokens: 1,
        total_tokens: 2,
      },
    }),
    {
      status: 200,
      headers: { "content-type": "application/json" },
    },
  );
}

export async function captureOfficialChatCompletionsRequest(input) {
  const payload = input && typeof input === "object" ? input : {};
  const requestBody =
    payload.requestBody && typeof payload.requestBody === "object" ? payload.requestBody : {};
  const stream = Boolean(payload.stream);
  const { generator, officialRequest } = await prepareOfficialBundleExecution(requestBody, {
    apiKey: payload.apiKey,
    baseUrl: payload.baseUrl,
    authType: payload.authType,
    sessionId: payload.sessionId,
    conversationId: payload.conversationId,
    strictOfficial: true,
  });

  const originalFetch = globalThis.fetch;
  const originalNow = Date.now;
  let capturedRequest = null;

  if (isPositiveInteger(payload.timestampMs)) {
    Date.now = () => payload.timestampMs;
  }

  globalThis.fetch = async (url, init = {}) => {
    const headers = headersToObject(init.headers);
    let body = init.body;
    if (typeof body === "string") {
      try {
        body = JSON.parse(body);
      } catch {
        // keep raw string for debugging
      }
    }
    capturedRequest = {
      url: String(url),
      headers,
      body,
    };
    return buildCaptureResponse(stream);
  };

  try {
    await generator.generateContentInternal(
      officialRequest,
      JSON.stringify(officialRequest.contents),
      null,
      null,
      stream,
    );
  } finally {
    globalThis.fetch = originalFetch;
    Date.now = originalNow;
  }

  if (!capturedRequest) {
    throw new Error("官方 bundle 未生成 /chat/completions 请求。");
  }

  const timestampHeader = Number.parseInt(String(capturedRequest.headers["x-iflow-timestamp"] || ""), 10);
  const sessionId = String(capturedRequest.headers["session-id"] || "");
  const conversationId = String(capturedRequest.headers["conversation-id"] || "");
  const traceparent = String(capturedRequest.headers.traceparent || "");

  return {
    url: capturedRequest.url,
    headers: capturedRequest.headers,
    body: capturedRequest.body,
    meta: {
      sessionId,
      conversationId,
      traceparent,
      timestampMs: Number.isFinite(timestampHeader) ? timestampHeader : Date.now(),
      scene: resolveScene(payload.baseUrl || ""),
      normalizationSource: "official_bundle",
    },
  };
}
