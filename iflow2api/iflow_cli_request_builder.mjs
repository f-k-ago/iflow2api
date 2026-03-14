import crypto from "node:crypto";

export const IFLOW_CLI_USER_AGENT = "iFlow-Cli";
export const IFLOW_CLI_VERSION = "0.5.17";

const OFFLINE_SESSION_HOSTS = [
  "offline-whale-wave.alibaba-inc.com",
  "whale.wave.vipserver.offline",
  "whale-wave.alibaba-inc.com",
  "pre-whale-wave.alibaba-inc.com",
  "internal-offline-whale-wave.alibaba-inc.com",
  "internal-whale-wave.alibaba-inc.com",
];

const OFFICIAL_MAX_NEW_TOKENS_EXACT = {
  "gemini-1.5-pro": 8192,
  "gemini-1.5-flash": 65536,
  "gemini-2.5-pro-preview-05-06": 65536,
  "gemini-2.5-pro-preview-06-05": 65536,
  "gemini-2.5-pro": 65536,
  "gemini-2.5-flash-preview-05-20": 65536,
  "gemini-2.5-flash": 65536,
  "gemini-2.0-flash": 65536,
  "gemini-2.0-flash-preview-image-generation": 8192,
};

const OFFICIAL_MAX_NEW_TOKENS_PATTERNS = [
  {
    pattern: /kimi/i,
    limits: {
      "k2.5": 32768,
      "k2-0905": 32000,
      "k2-thinking": 32000,
      "ide-modelscope": 8192,
      default: 32000,
    },
  },
  {
    pattern: /iFlow-ROME-30BA3B/i,
    limits: { default: 64000 },
  },
  {
    pattern: /minimax/i,
    limits: { default: 64000 },
  },
  {
    pattern: /glm/i,
    limits: {
      "-5": 32000,
      "4.7": 32000,
      "ide-modelscope": 16384,
    },
    defaultLimit: 32000,
  },
  {
    pattern: /claude|haiku|sonnet|opus/i,
    limits: {},
    defaultLimit: 64000,
  },
  {
    pattern: /deepseek/i,
    limits: {
      "v3.2-reasoner": 64000,
      v3: 8192,
      r1: 32000,
      default: 8000,
    },
  },
  {
    pattern: /qwen3/i,
    limits: {
      coder: 64000,
      "max-preview": 32000,
      default: 8192,
    },
  },
  {
    pattern: /qwen/i,
    limits: {
      max: 8192,
      plus: 8192,
      default: 8000,
    },
  },
  {
    pattern: /gpt/i,
    limits: {
      "32k": 4096,
      default: 16384,
    },
  },
  {
    pattern: /mimo/i,
    limits: { default: 64000 },
  },
];

function isPositiveInteger(value) {
  return Number.isInteger(value) && value > 0;
}

function normalizeBaseUrl(baseUrl) {
  if (typeof baseUrl !== "string") {
    return "";
  }
  return baseUrl.endsWith("/") ? baseUrl.slice(0, -1) : baseUrl;
}

function resolveScene(baseUrl) {
  return baseUrl.toLowerCase().includes("ducky.code.alibaba-inc.com") ? "aone" : "global";
}

function resolveOutputTokensLimit(requestBody) {
  if (!requestBody || typeof requestBody !== "object") {
    return undefined;
  }
  if (isPositiveInteger(requestBody.max_completion_tokens)) {
    return requestBody.max_completion_tokens;
  }
  if (isPositiveInteger(requestBody.max_tokens)) {
    return requestBody.max_tokens;
  }
  return undefined;
}

function resolveOfficialMaxNewTokens(model, explicitLimit) {
  if (isPositiveInteger(explicitLimit)) {
    return explicitLimit;
  }
  if (!model || typeof model !== "string") {
    return 8000;
  }
  const normalizedModel = model.toLowerCase();
  if (Object.prototype.hasOwnProperty.call(OFFICIAL_MAX_NEW_TOKENS_EXACT, model)) {
    return OFFICIAL_MAX_NEW_TOKENS_EXACT[model];
  }
  for (const item of OFFICIAL_MAX_NEW_TOKENS_PATTERNS) {
    if (!item.pattern.test(normalizedModel)) {
      continue;
    }
    for (const [key, value] of Object.entries(item.limits)) {
      if (key !== "default" && normalizedModel.includes(key)) {
        return value;
      }
    }
    return item.limits.default ?? item.defaultLimit ?? 8000;
  }
  return 8000;
}

function resolveThinkingEnabled(requestBody) {
  if (!requestBody || typeof requestBody !== "object") {
    return undefined;
  }

  const thinking = requestBody.thinking;
  if (thinking && typeof thinking === "object") {
    if (thinking.enabled === true) {
      return true;
    }
    if (thinking.enabled === false) {
      return false;
    }
    const thinkingType = String(thinking.type || "").trim().toLowerCase();
    if (thinkingType === "enabled") {
      return true;
    }
    if (thinkingType === "disabled") {
      return false;
    }
  }

  if (Object.prototype.hasOwnProperty.call(requestBody, "enable_thinking")) {
    return Boolean(requestBody.enable_thinking);
  }

  const templateKwargs = requestBody.chat_template_kwargs;
  if (
    templateKwargs &&
    typeof templateKwargs === "object" &&
    Object.prototype.hasOwnProperty.call(templateKwargs, "enable_thinking")
  ) {
    return Boolean(templateKwargs.enable_thinking);
  }

  if (requestBody.thinking_mode === true || requestBody.reasoning === true) {
    return true;
  }

  return undefined;
}

function deriveThinkingConfig(requestBody, resolvedMaxTokens) {
  const thinking = requestBody?.thinking;
  const requestedMaxTokens = [
    thinking?.max_tokens,
    requestBody?.max_tokens,
    requestBody?.max_completion_tokens,
    resolvedMaxTokens,
  ].find(isPositiveInteger);
  return {
    maxTokens: requestedMaxTokens ?? resolvedMaxTokens,
    reasoningLevel:
      String(
        thinking?.reasoning_level ??
          requestBody?.reasoning_level ??
          requestBody?.reasoningLevel ??
          "high",
      )
        .trim()
        .toLowerCase() || "high",
  };
}

function getModelCapability(model) {
  const capabilities = [
    {
      pattern: /deepseek/i,
      supportsThinking: true,
      supportedReasoningLevels: ["low", "medium", "high"],
      maxThinkingTokens: 32000,
      applyThinking(body, config) {
        if (config.reasoningLevel !== "low") {
          body.reasoning = true;
        }
        body.thinking_mode = true;
      },
    },
    {
      pattern: /glm-4\.7/i,
      supportsThinking: true,
      supportedReasoningLevels: ["low", "medium", "high"],
      maxThinkingTokens: 20000,
      applyThinking(body) {
        body.chat_template_kwargs = { enable_thinking: true };
      },
      applyNonThinking(body) {
        body.chat_template_kwargs = { enable_thinking: false };
      },
    },
    {
      pattern: /glm-5/i,
      supportsThinking: true,
      supportedReasoningLevels: ["low", "medium", "high"],
      maxThinkingTokens: 20000,
      applyThinking(body) {
        body.chat_template_kwargs = { enable_thinking: true };
        body.enable_thinking = true;
        body.thinking = { type: "enabled" };
      },
      applyNonThinking(body) {
        body.chat_template_kwargs = { enable_thinking: false };
        body.enable_thinking = false;
        body.thinking = { type: "disabled" };
      },
    },
    {
      pattern: /glm-/i,
      supportsThinking: true,
      supportedReasoningLevels: ["low", "medium", "high"],
      maxThinkingTokens: 20000,
      applyThinking(body) {
        body.chat_template_kwargs = { enable_thinking: true };
      },
    },
    {
      pattern: /^claude-3\.5-sonnet/i,
      supportsThinking: true,
      supportedReasoningLevels: ["low", "medium", "high"],
      maxThinkingTokens: 25000,
      applyThinking(body, config) {
        body.thinking = {
          enabled: true,
          max_tokens: config.maxTokens,
          reasoning_level: config.reasoningLevel,
        };
      },
    },
    {
      pattern: /claude|haiku|sonnet|opus/i,
      supportsThinking: true,
      supportedReasoningLevels: ["low", "medium", "high"],
      maxThinkingTokens: 20000,
      applyThinking(body) {
        body.chat_template_kwargs = { enable_thinking: true };
      },
    },
    {
      pattern: /.*reasoning.*/i,
      supportsThinking: true,
      supportedReasoningLevels: ["low", "medium"],
      maxThinkingTokens: 10000,
      applyThinking(body) {
        body.reasoning = true;
      },
    },
    {
      pattern: /^kimi-k2\.5/i,
      supportsThinking: true,
      supportedReasoningLevels: ["low", "medium", "high"],
      maxThinkingTokens: 32768,
      applyThinking(body) {
        body.thinking = { type: "enabled" };
      },
      applyNonThinking(body) {
        body.thinking = { type: "disabled" };
      },
    },
    {
      pattern: /.*thinking.*/i,
      supportsThinking: true,
      supportedReasoningLevels: ["low", "medium", "high"],
      maxThinkingTokens: 15000,
      applyThinking(body) {
        body.thinking_mode = true;
      },
    },
    {
      pattern: /qwen.*4b/i,
      supportsThinking: false,
      supportedReasoningLevels: [],
    },
    {
      pattern: /mimo-/i,
      supportsThinking: true,
      supportedReasoningLevels: ["low", "medium", "high"],
      maxThinkingTokens: 20000,
      applyThinking(body) {
        body.thinking = { type: "enabled" };
      },
    },
  ];

  return capabilities.find((item) => item.pattern.test(model)) ?? null;
}

function applyThinkingConfig(originalModel, sourceBody, body, thinkingEnabled, resolvedMaxTokens) {
  const capability = getModelCapability(originalModel);
  if (!capability) {
    return;
  }

  if (thinkingEnabled === true && capability.supportsThinking) {
    const config = deriveThinkingConfig(sourceBody, resolvedMaxTokens);
    const supportedLevels = capability.supportedReasoningLevels ?? [];
    if (!supportedLevels.includes(config.reasoningLevel)) {
      if (supportedLevels.includes("high")) {
        config.reasoningLevel = "high";
      } else if (supportedLevels.includes("medium")) {
        config.reasoningLevel = "medium";
      } else {
        config.reasoningLevel = "low";
      }
    }
    if (isPositiveInteger(capability.maxThinkingTokens) && config.maxTokens > capability.maxThinkingTokens) {
      config.maxTokens = capability.maxThinkingTokens;
    }
    capability.applyThinking?.(body, config);
    if (originalModel === "deepseek-v3.2-chat") {
      body.model = "deepseek-v3.2-reasoner";
    }
    return;
  }

  if (thinkingEnabled !== true) {
    capability.applyNonThinking?.(body);
  }
}

function shouldCarryStop(stop) {
  if (Array.isArray(stop)) {
    return stop.length > 0;
  }
  return typeof stop === "string" ? stop.length > 0 : false;
}

function pickTemperatureSource(requestBody) {
  if (!requestBody || typeof requestBody !== "object") {
    return {};
  }
  if (requestBody.temperature !== undefined && requestBody.temperature !== null) {
    return { temperature: requestBody.temperature, hasTemperature: true };
  }
  if (requestBody.top_p !== undefined && requestBody.top_p !== null) {
    return { topP: requestBody.top_p, hasTemperature: false };
  }
  return { hasTemperature: false };
}

function buildOfficialBody({
  requestBody,
  stream,
  baseUrl,
  sessionId,
}) {
  const source = requestBody && typeof requestBody === "object" ? requestBody : {};
  const originalModel = String(source.model || "unknown");
  const body = {
    model: originalModel,
    messages: Array.isArray(source.messages) ? source.messages : [],
  };

  if (stream) {
    body.stream = true;
    body.stream_options = { include_usage: true };
  }

  if (originalModel === "glm-4.6") {
    body.model = "glm-4.6-exp";
  }

  const { temperature, topP, hasTemperature } = pickTemperatureSource(source);
  if (temperature !== undefined) {
    body.temperature = temperature;
  } else if (!hasTemperature && topP !== undefined) {
    body.top_p = topP;
  }

  const thinkingEnabled = resolveThinkingEnabled(source);
  const outputTokensLimit = resolveOutputTokensLimit(source);
  const provisionalMaxNewTokens = resolveOfficialMaxNewTokens(body.model, outputTokensLimit);
  applyThinkingConfig(originalModel, source, body, thinkingEnabled, provisionalMaxNewTokens);
  const resolvedMaxNewTokens = resolveOfficialMaxNewTokens(body.model, outputTokensLimit);

  if (originalModel.includes("glm-4.7") || originalModel.includes("glm-5")) {
    body.temperature = 1;
    body.top_p = 0.95;
  }

  if (originalModel.startsWith("kimi-k2.5")) {
    body.top_p = 0.95;
    body.n = 1;
    body.presence_penalty = 0;
    body.frequency_penalty = 0;
    body.max_tokens = resolvedMaxNewTokens;
    delete body.temperature;
  }

  const scene = resolveScene(baseUrl);
  if (scene === "aone") {
    body.max_tokens = resolvedMaxNewTokens;
  }
  if (/deepseek/i.test(originalModel) && body.model !== "deepseek-v3.2-reasoner") {
    body.max_tokens = resolvedMaxNewTokens;
  }
  body.max_new_tokens = resolvedMaxNewTokens;

  if (shouldCarryStop(source.stop)) {
    body.stop = source.stop;
  }

  if (Array.isArray(source.tools) && source.tools.length > 0) {
    body.tools = source.tools;
    if (source.tool_choice !== undefined) {
      body.tool_choice = source.tool_choice;
    } else if (originalModel.startsWith("kimi-k2.5") && body.thinking?.type === "enabled") {
      body.tool_choice = "auto";
    }
  }

  if (OFFLINE_SESSION_HOSTS.some((host) => normalizeBaseUrl(baseUrl).includes(host))) {
    body.extend_fields = {
      sessionId: String(sessionId || ""),
    };
  }

  if (body.model === "iFlow-ROME-30BA3B") {
    body.temperature = 0.7;
    body.top_p = 0.8;
    body.top_k = 20;
  }

  return body;
}

export function generateSignature(userAgent, sessionId, timestampMs, apiKey) {
  if (!apiKey) {
    return null;
  }
  const message = `${userAgent}:${sessionId}:${timestampMs}`;
  return crypto.createHmac("sha256", apiKey).update(message, "utf8").digest("hex");
}

function buildOfficialHeaders({
  apiKey,
  baseUrl,
  sessionId,
  conversationId,
  traceparent,
  timestampMs,
}) {
  const headers = {
    "Content-Type": "application/json",
    Authorization: `Bearer ${apiKey}`,
    "user-agent": IFLOW_CLI_USER_AGENT,
    "session-id": sessionId,
    "conversation-id": conversationId,
  };

  const signature = generateSignature(IFLOW_CLI_USER_AGENT, sessionId, timestampMs, apiKey);
  if (signature) {
    headers["x-iflow-signature"] = signature;
    headers["x-iflow-timestamp"] = String(timestampMs);
  }

  const normalizedTraceparent =
    typeof traceparent === "string" ? traceparent.trim() : "";
  if (normalizedTraceparent) {
    headers.traceparent = normalizedTraceparent;
  }

  if (resolveScene(baseUrl) === "aone") {
    headers["X-Client-Type"] = "iflow-cli";
    headers["X-Client-Version"] = IFLOW_CLI_VERSION;
  }

  return headers;
}

export function buildOfficialChatCompletionsRequest(input) {
  const apiKey = String(input?.apiKey || "");
  const baseUrl = normalizeBaseUrl(String(input?.baseUrl || ""));
  const sessionId = String(input?.sessionId || "");
  const conversationId = String(input?.conversationId || "");
  const stream = Boolean(input?.stream);
  const timestampMs = isPositiveInteger(input?.timestampMs) ? input.timestampMs : Date.now();
  const requestBody = buildOfficialBody({
    requestBody: input?.requestBody,
    stream,
    baseUrl,
    sessionId,
  });

  const headers = buildOfficialHeaders({
    apiKey,
    baseUrl,
    sessionId,
    conversationId,
    traceparent: input?.traceparent,
    timestampMs,
  });

  return {
    url: `${baseUrl}/chat/completions`,
    headers,
    body: requestBody,
    meta: {
      sessionId,
      conversationId,
      traceparent: headers.traceparent || "",
      timestampMs,
      scene: resolveScene(baseUrl),
    },
  };
}
