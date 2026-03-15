import { loadOfficialBundleHelpers } from "./iflow_cli_official_bundle_loader.mjs";

const OFFICIAL_MULTIMODAL_MODELS = [
  "gemini-2.5-flash-06-17",
  "gemini-2.5-flash-lite-preview-06-17",
  "gemini-2.5-flash-preview-05-20",
  "gemini-2.5-flash-preview-04-17",
  "gemini-2.5-pro-06-17",
  "gemini-2.5-pro-preview-05-06",
  "gemini-2.5-pro-03-25",
  "gemini-2.5-pro-preview-06-05",
  "o3-pro-0610-global",
  "claude_opus4",
  "claude3_opus",
  "claude_sonnet4",
  "claude37_sonnet",
  "claude35_sonnet2",
  "claude35_sonnet",
  "o3-0416-global",
  "o4-mini-0416-global",
  "o3-mini-2025-01-31",
  "o3-mini-0131-global",
  "o1-preview-0912-global",
  "o1-preview-0912",
  "o1-mini-0912-global",
  "o1-mini-0912",
  "o1-2024-12-17",
  "o1-1217-global",
  "o1-1217",
  "qwen-plus-latest-inc",
  "kimi-k2.5",
  "qwen-plus-latest",
  "qwen-plus",
  "qwen-plus-safe",
  "gemini-2.0-flash",
  "gemini-2.0-flash-thinking",
  "gemini-2.0-flash-exp",
  "qwen2.5-vl-72b-instruct",
  "qwen-vl-max",
  "qwen3-vl-plus",
  "Qwen-VL",
  "qwen-vl-max-latest",
  "nova-lite-v1",
  "nova-pro-v1",
  "gpt-5",
  "gpt-5-0807-global",
  "gpt-5-mini",
  "gpt-5-chat-0807-global",
  "gpt-5-mini-0807-global",
  "gpt-5-nano-0807-global",
  "gpt-4o-1120-global",
  "gpt-4o-0806-global",
  "gpt-4o-0806",
  "gpt-4o-0513-global",
  "gpt-4o-0513-Batch",
  "gpt-4o-0513",
  "gpt-4o-0806-Batch",
  "gpt-4o-mini-0718-global",
  "gpt-4o-mini-0718",
  "gpt-4o-mini-0718-Batch",
];

const AUTH_IFLOW = "iflow";
const AUTH_AONE = "aone";
const AUTH_LOGIN_WITH_IFLOW = "oauth-iflow";
const AUTH_LOGIN_WITH_AONE = "oauth-aone";
const CLAUDE_MODEL_PATTERN = /claude|haiku|sonnet|opus/i;
const GPT5_MODEL_PATTERN = /gpt-5/i;
const TOOL_CALL_PREFIX = "call_";

function normalizeOfficialAuthType(authType, normalizedBaseUrl) {
  const normalizedAuthType = typeof authType === "string" ? authType.trim().toLowerCase() : "";
  if (normalizedAuthType === AUTH_LOGIN_WITH_AONE || normalizedAuthType === AUTH_AONE) {
    return normalizedAuthType;
  }
  if (normalizedAuthType === AUTH_LOGIN_WITH_IFLOW) {
    return AUTH_LOGIN_WITH_IFLOW;
  }
  if (normalizedAuthType === AUTH_IFLOW || normalizedAuthType === "cookie" || normalizedAuthType === "api-key") {
    return AUTH_IFLOW;
  }
  return normalizedBaseUrl.includes("ducky.code.alibaba-inc.com") ? AUTH_AONE : AUTH_IFLOW;
}

function createRuntimeContext({ model, baseUrl, authType }) {
  const normalizedModel = typeof model === "string" ? model : "unknown";
  const normalizedBaseUrl = typeof baseUrl === "string" ? baseUrl.trim().toLowerCase() : "";
  return {
    model: normalizedModel,
    baseUrl: normalizedBaseUrl,
    authType: normalizeOfficialAuthType(authType, normalizedBaseUrl),
  };
}

function isPositiveInteger(value) {
  return Number.isInteger(value) && value > 0;
}

function isPlainObject(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseDataUrl(value) {
  if (typeof value !== "string") {
    return null;
  }
  const match = value.match(/^data:([^;,]+);base64,([a-zA-Z0-9+/=\s]+)$/);
  if (!match) {
    return null;
  }
  return {
    mimeType: match[1],
    data: match[2].replace(/\s+/g, ""),
  };
}

function isMultimodalModel(model) {
  const normalizedModel = String(model || "").toLowerCase();
  return (
    OFFICIAL_MULTIMODAL_MODELS.some((candidate) =>
      normalizedModel.includes(candidate.toLowerCase()),
    ) ||
    normalizedModel.includes("vision") ||
    normalizedModel.includes("visual") ||
    normalizedModel.includes("vl")
  );
}

function normalizeTextBlocks(content) {
  if (typeof content === "string") {
    return [{ type: "text", text: content }];
  }
  if (!Array.isArray(content)) {
    return null;
  }
  const textBlocks = [];
  for (const block of content) {
    if (!isPlainObject(block) || block.type !== "text" || typeof block.text !== "string") {
      return null;
    }
    textBlocks.push({ type: "text", text: block.text });
  }
  return textBlocks;
}

function normalizeToolResponseOutput(content) {
  if (typeof content === "string") {
    return content;
  }
  const textBlocks = normalizeTextBlocks(content);
  if (!textBlocks) {
    return null;
  }
  return textBlocks.map((block) => block.text).join("");
}

function shouldCarryStop(stop) {
  if (Array.isArray(stop)) {
    return stop.length > 0;
  }
  return typeof stop === "string" ? stop.length > 0 : false;
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

function normalizeAssistantText(content) {
  if (typeof content === "string") {
    return content.length > 0 ? [{ text: content }] : [];
  }
  const textBlocks = normalizeTextBlocks(content);
  if (!textBlocks) {
    return null;
  }
  return textBlocks.map((block) => ({ text: block.text }));
}

function normalizeSystemParts(content) {
  if (typeof content === "string") {
    return content.length > 0 ? [{ text: content }] : [];
  }
  const textBlocks = normalizeTextBlocks(content);
  if (!textBlocks) {
    return null;
  }
  return textBlocks.map((block) => ({ text: block.text }));
}

function normalizeUserParts(content, runtime) {
  if (typeof content === "string") {
    return content.length > 0 ? [{ text: content }] : [];
  }
  if (!Array.isArray(content)) {
    return null;
  }
  const parts = [];
  for (const block of content) {
    if (!isPlainObject(block)) {
      return null;
    }
    if (block.type === "text" && typeof block.text === "string") {
      parts.push({ text: block.text });
      continue;
    }
    if (block.type === "image_url" && isPlainObject(block.image_url)) {
      const parsed = parseDataUrl(block.image_url.url);
      if (!parsed) {
        return null;
      }
      if ((runtime.authType === AUTH_IFLOW || runtime.authType === AUTH_AONE) && !isMultimodalModel(runtime.model)) {
        return null;
      }
      parts.push({
        inlineData: {
          mimeType: parsed.mimeType,
          data: parsed.data,
        },
      });
      continue;
    }
    return null;
  }
  return parts;
}

function translateOpenAIToolsToOfficialTools(tools) {
  if (!Array.isArray(tools) || tools.length === 0) {
    return [];
  }
  const declarations = [];
  for (const tool of tools) {
    if (!isPlainObject(tool) || tool.type !== "function" || !isPlainObject(tool.function)) {
      return null;
    }
    declarations.push({
      name: String(tool.function.name || ""),
      description: tool.function.description,
      parametersJsonSchema: tool.function.parameters,
    });
  }
  return declarations.length > 0 ? [{ functionDeclarations: declarations }] : [];
}

function translateOpenAIMessagesToOfficialRequest(messages, runtime) {
  if (!Array.isArray(messages)) {
    return null;
  }

  const contents = [];
  const systemParts = [];
  const toolCallNames = new Map();

  for (const message of messages) {
    if (!isPlainObject(message)) {
      return null;
    }
    const role = String(message.role || "").trim().toLowerCase();
    if (role === "system" || role === "developer") {
      const parts = normalizeSystemParts(message.content);
      if (!parts) {
        return null;
      }
      systemParts.push(...parts);
      continue;
    }

    if (role === "user") {
      if (typeof message.content === "string") {
        contents.push(message.content);
        continue;
      }
      const parts = normalizeUserParts(message.content, runtime);
      if (!parts) {
        return null;
      }
      contents.push({ role: "user", parts });
      continue;
    }

    if (role === "assistant") {
      const parts = [];
      if (typeof message.reasoning_content === "string" && message.reasoning_content) {
        parts.push({
          thought: true,
          text: message.reasoning_content,
          thoughtSignature:
            typeof message.signature === "string" && message.signature ? message.signature : undefined,
        });
      }
      if (typeof message.reasoning === "string" && message.reasoning) {
        parts.push({ thought: true, text: message.reasoning });
      }
      const assistantTextParts = normalizeAssistantText(message.content);
      if (!assistantTextParts) {
        return null;
      }
      parts.push(...assistantTextParts);
      if (Array.isArray(message.tool_calls)) {
        for (const toolCall of message.tool_calls) {
          if (!isPlainObject(toolCall) || !isPlainObject(toolCall.function)) {
            return null;
          }
          const toolCallId = String(
            toolCall.id || `${TOOL_CALL_PREFIX}${Date.now()}_${Math.random().toString(36).slice(2, 11)}`,
          );
          const toolName = String(toolCall.function.name || "");
          toolCallNames.set(toolCallId, toolName);
          let toolArgs = toolCall.function.arguments;
          if (typeof toolArgs === "string") {
            try {
              toolArgs = JSON.parse(toolArgs);
            } catch {
              return null;
            }
          }
          if (toolArgs === undefined || toolArgs === null) {
            toolArgs = {};
          }
          parts.push({
            functionCall: {
              id: toolCallId,
              name: toolName,
              args: toolArgs,
            },
          });
        }
      }
      contents.push({ role: "model", parts });
      continue;
    }

    if (role === "tool") {
      const toolCallId = String(message.tool_call_id || message.id || message.name || "");
      if (!toolCallId) {
        return null;
      }
      const output = normalizeToolResponseOutput(message.content);
      if (output === null) {
        return null;
      }
      contents.push({
        role: "user",
        parts: [
          {
            functionResponse: {
              id: toolCallId,
              name: String(message.name || toolCallNames.get(toolCallId) || ""),
              response: { output },
            },
          },
        ],
      });
      continue;
    }

    return null;
  }

  const request = {
    contents,
    config: {},
  };

  if (systemParts.length > 0) {
    request.config.systemInstruction = { parts: systemParts };
  }

  return request;
}

export class OfficialChatInputValidationError extends Error {
  constructor(message) {
    super(message);
    this.name = "OfficialChatInputValidationError";
    this.code = "invalid_request_error";
    this.status_code = 400;
  }
}

function createOfficialBundleConfig(runtime, context, options = {}) {
  const sessionId = typeof context?.sessionId === "string" ? context.sessionId : "";
  const conversationId = typeof context?.conversationId === "string" ? context.conversationId : "";
  const outputTokensLimit = Number.isInteger(options.outputTokensLimit ?? context?.outputTokensLimit)
    ? (options.outputTokensLimit ?? context?.outputTokensLimit)
    : undefined;
  const contentGeneratorConfig = {
    authType: runtime.authType,
    baseUrl: runtime.baseUrl,
    multimodalModelName: context?.multimodalModelName || runtime.model || "qwen3-vl-plus",
  };
  if (options.thinkingConfig) {
    contentGeneratorConfig.thinking = options.thinkingConfig;
  }
  return {
    getModel: () => runtime.model,
    getSessionId: () => sessionId,
    getConversationId: () => conversationId,
    getTemperature: () => undefined,
    getTopP: () => undefined,
    getOutputTokensLimit: () => outputTokensLimit,
    getContentGeneratorConfig: () => contentGeneratorConfig,
    getDebugMode: () => false,
    getDisableTelemetry: () => true,
    getUsageStatisticsEnabled: () => false,
  };
}

class OfficialBundleRoundTripShim {
  constructor(runtime, context, helpers) {
    const config = createOfficialBundleConfig(runtime, context);
    this.instance = new helpers.gH({
      model: runtime.model,
      apiKey: context?.apiKey || "sk-iflow2api-shim",
      baseUrl: context?.baseUrl || runtime.baseUrl,
      authType: runtime.authType,
      debugMode: false,
      multimodalModelName: config.getContentGeneratorConfig().multimodalModelName,
      config,
    });
  }

  convertToOpenAITools(tools) {
    return this.instance.convertToOpenAITools(tools);
  }

  async calculateInputTokens(request) {
    return this.instance.calculateInputTokens(request);
  }

  async convertToOpenAIMessages(request) {
    return this.instance.convertToOpenAIMessages(request);
  }
}

class OfficialRoundTripShim {
  constructor(runtime) {
    this.runtime = runtime;
  }

  extractSystemInstruction(value) {
    let text = "";
    if (typeof value === "string") {
      text = value;
    } else if (Array.isArray(value)) {
      for (const item of value) {
        if (isPlainObject(item) && typeof item.text === "string" && item.text) {
          text += `${item.text} `;
        }
      }
      text = text.trim();
    } else if (isPlainObject(value)) {
      if (Array.isArray(value.parts)) {
        for (const part of value.parts) {
          if (isPlainObject(part) && typeof part.text === "string" && part.text) {
            text += `${part.text} `;
          }
        }
        text = text.trim();
      } else if (typeof value.text === "string" && value.text) {
        text = value.text;
      }
    }
    return text;
  }

  convertParameters(value) {
    if (!value) {
      return undefined;
    }
    const typeMap = {
      STRING: "string",
      INTEGER: "integer",
      NUMBER: "number",
      BOOLEAN: "boolean",
      OBJECT: "object",
      ARRAY: "array",
      string: "string",
      integer: "integer",
      number: "number",
      boolean: "boolean",
      object: "object",
      array: "array",
    };

    if (isPlainObject(value) && value.type) {
      const rawType = value.type;
      const normalizedType =
        typeof rawType === "string"
          ? (typeMap[rawType] || rawType.toLowerCase())
          : rawType;
      const properties = {};
      if (isPlainObject(value.properties)) {
        for (const [key, propertyValue] of Object.entries(value.properties)) {
          properties[key] = this.convertParameters(propertyValue);
        }
      }
      const items = value.items ? this.convertParameters(value.items) : undefined;
      const converted = {
        type: normalizedType,
      };
      if (value.description) {
        converted.description = value.description;
      }
      if (Array.isArray(value.enum)) {
        converted.enum = value.enum;
      }
      if ((normalizedType === "object" && GPT5_MODEL_PATTERN.test(this.runtime.model)) || Object.keys(properties).length > 0) {
        converted.properties = properties;
      }
      if (items) {
        converted.items = items;
      }
      if (Array.isArray(value.required)) {
        converted.required = value.required;
      }
      if (value.minItems !== undefined) {
        converted.minItems = typeof value.minItems === "string" ? Number(value.minItems) : value.minItems;
      }
      if (value.minLength !== undefined) {
        converted.minLength = typeof value.minLength === "string" ? Number(value.minLength) : value.minLength;
      }
      if (normalizedType === "object" && !("additionalProperties" in value)) {
        converted.additionalProperties = false;
      } else if ("additionalProperties" in value) {
        converted.additionalProperties = value.additionalProperties;
      }
      return converted;
    }

    return undefined;
  }

  convertToOpenAITools(tools) {
    if (!Array.isArray(tools) || tools.length === 0) {
      return [];
    }
    const convertedTools = [];
    const isClaudeModel = CLAUDE_MODEL_PATTERN.test(this.runtime.model);
    for (const item of tools) {
      if (!isPlainObject(item) || !Array.isArray(item.functionDeclarations)) {
        continue;
      }
      for (const declaration of item.functionDeclarations) {
        convertedTools.push({
          type: "function",
          function: {
            name: declaration.name || "",
            description: declaration.description,
            parameters: this.convertParameters(
              declaration.parameters || declaration.parametersJsonSchema,
            ),
          },
        });
      }
    }
    if (this.runtime.authType === AUTH_AONE && isClaudeModel && convertedTools.length > 0) {
      const lastTool = convertedTools[convertedTools.length - 1];
      if (lastTool.function) {
        lastTool.function.cache_control = { type: "ephemeral" };
      }
    }
    return convertedTools;
  }

  extractTextFromRequest(request) {
    const chunks = [];
    const systemInstruction = request?.config?.systemInstruction;
    if (systemInstruction) {
      chunks.push(this.extractSystemInstruction(systemInstruction));
    }
    const contents = Array.isArray(request?.contents) ? request.contents : [];
    for (const content of contents) {
      if (typeof content === "string") {
        chunks.push(content);
        continue;
      }
      if (isPlainObject(content) && typeof content.text === "string") {
        chunks.push(content.text);
        continue;
      }
      if (isPlainObject(content) && Array.isArray(content.parts)) {
        for (const part of content.parts) {
          if (!isPlainObject(part)) {
            continue;
          }
          if (typeof part.text === "string") {
            chunks.push(part.text);
          } else if (isPlainObject(part.functionCall)) {
            chunks.push(part.functionCall.name || "");
            chunks.push(JSON.stringify(part.functionCall.args || {}));
          } else if (isPlainObject(part.functionResponse)) {
            const output = part.functionResponse.response?.output;
            if (typeof output === "string") {
              chunks.push(output);
            }
          }
        }
      }
    }
    return chunks.filter(Boolean).join("\n");
  }

  async countTokens(request, useUsageMetadata = false) {
    if (!useUsageMetadata && this.lastUsageMetadata?.total_tokens) {
      return { totalTokens: this.lastUsageMetadata.total_tokens };
    }
    const text = this.extractTextFromRequest(request);
    return { totalTokens: Math.ceil(text.length / 4) };
  }

  async calculateInputTokens(request) {
    try {
      const prepared = {
        model: this.runtime.model,
        contents: request.contents,
        config: {
          systemInstruction: request.config?.systemInstruction,
        },
      };
      return (await this.countTokens(prepared)).totalTokens || 0;
    } catch {
      return Math.ceil(this.extractTextFromRequest(request).length / 4);
    }
  }

  async convertToOpenAIMessages(request) {
    const messages = [];
    const model = this.runtime.model || "unknown";
    const observedToolCalls = new Set();
    const toolResponses = new Map();

    if (Array.isArray(request?.contents)) {
      const normalizedContents = Array.isArray(request.contents) ? request.contents : [request.contents];
      for (const content of normalizedContents) {
        if (isPlainObject(content) && Array.isArray(content.parts)) {
          const parts = Array.isArray(content.parts) ? content.parts : [content.parts];
          for (const part of parts) {
            if (!isPlainObject(part)) {
              continue;
            }
            if (isPlainObject(part.functionCall)) {
              const toolCallId =
                part.functionCall.id || `${TOOL_CALL_PREFIX}${Date.now()}_${Math.random().toString(36).slice(2, 11)}`;
              observedToolCalls.add(toolCallId);
            }
            if (isPlainObject(part.functionResponse)) {
              const toolCallId = part.functionResponse.id || part.functionResponse.name || "";
              if (toolCallId.startsWith(TOOL_CALL_PREFIX)) {
                observedToolCalls.add(toolCallId);
              }
              const output =
                part.functionResponse.response?.output ??
                (part.functionResponse.response
                  ? JSON.stringify(part.functionResponse.response)
                  : "");
              toolResponses.set(toolCallId, {
                role: "tool",
                tool_call_id: toolCallId,
                content: output,
              });
            }
          }
        }
      }
      for (const toolCallId of [...toolResponses.keys()]) {
        if (!observedToolCalls.has(toolCallId)) {
          toolResponses.delete(toolCallId);
        }
      }
    }

    if (request?.config?.systemInstruction) {
      const systemInstruction = this.extractSystemInstruction(request.config.systemInstruction);
      if (systemInstruction) {
        messages.push({ role: "system", content: systemInstruction });
      }
    }

    if (Array.isArray(request?.contents)) {
      const normalizedContents = Array.isArray(request.contents) ? request.contents : [request.contents];
      for (const content of normalizedContents) {
        if (typeof content === "string") {
          messages.push({ role: "user", content });
          continue;
        }
        if (isPlainObject(content) && typeof content.text === "string" && content.text) {
          messages.push({ role: "user", content: content.text });
          continue;
        }
        if (isPlainObject(content) && Array.isArray(content.parts)) {
          const parts = Array.isArray(content.parts) ? content.parts : [content.parts];
          if (content.role === "model") {
            const assistantMessage = {
              role: "assistant",
              content: "",
              tool_calls: [],
            };
            const thoughtSegments = [];
            let thoughtSignature = "";
            for (const part of parts) {
              if (isPlainObject(part) && part.thought && typeof part.text === "string" && part.text) {
                thoughtSegments.push(part.text);
                if (typeof part.thoughtSignature === "string" && part.thoughtSignature) {
                  thoughtSignature = part.thoughtSignature;
                }
              }
            }
            if (thoughtSegments.length > 0) {
              assistantMessage.reasoning_content = thoughtSegments.join("");
              if (thoughtSignature) {
                assistantMessage.signature = thoughtSignature;
              }
            }

            const textSegments = [];
            for (const part of parts) {
              if (!isPlainObject(part) || part.thought) {
                continue;
              }
              if (typeof part.text === "string" && part.text) {
                textSegments.push(part.text);
                continue;
              }
              if (isPlainObject(part.functionCall)) {
                const toolCallId =
                  part.functionCall.id || `${TOOL_CALL_PREFIX}${Date.now()}_${Math.random().toString(36).slice(2, 11)}`;
                assistantMessage.tool_calls.push({
                  id: toolCallId,
                  function: {
                    name: part.functionCall.name || "",
                    arguments: JSON.stringify(part.functionCall.args || {}),
                  },
                  type: "function",
                });
              }
            }
            if (textSegments.length > 0) {
              assistantMessage.content = textSegments.join("");
            }
            if (assistantMessage.content || assistantMessage.reasoning_content || assistantMessage.tool_calls.length > 0) {
              if (assistantMessage.tool_calls.length === 0) {
                delete assistantMessage.tool_calls;
              }
              messages.push(assistantMessage);
              if (Array.isArray(assistantMessage.tool_calls)) {
                for (const toolCall of assistantMessage.tool_calls) {
                  const toolMessage = toolResponses.get(toolCall.id);
                  messages.push(
                    toolMessage || {
                      role: "tool",
                      tool_call_id: toolCall.id,
                      content: "tool not executed",
                    },
                  );
                }
              }
            }
            continue;
          }

          const userBlocks = [];
          for (const part of parts) {
            if (!isPlainObject(part)) {
              continue;
            }
            if (typeof part.text === "string" && part.text) {
              userBlocks.push({ type: "text", text: part.text });
              continue;
            }
            if (isPlainObject(part.inlineData)) {
              const mimeType = part.inlineData.mimeType;
              if (typeof mimeType === "string" && mimeType.startsWith("image/")) {
                userBlocks.push({
                  type: "image_url",
                  image_url: {
                    url: `data:${mimeType};base64,${part.inlineData.data || ""}`,
                  },
                });
              }
            }
          }
          if (userBlocks.length > 0) {
            messages.push({ role: "user", content: userBlocks });
          }
        }
      }
    }

    if (this.runtime.authType === AUTH_AONE && CLAUDE_MODEL_PATTERN.test(model) && messages.length > 0) {
      const lastMessage = messages[messages.length - 1];
      if (lastMessage.role === "user" || lastMessage.role === "assistant") {
        if (typeof lastMessage.content === "string") {
          lastMessage.content = [{
            type: "text",
            text: lastMessage.content,
            cache_control: { type: "ephemeral" },
          }];
        } else if (Array.isArray(lastMessage.content)) {
          const lastBlock = lastMessage.content[lastMessage.content.length - 1];
          if (isPlainObject(lastBlock)) {
            lastBlock.cache_control = { type: "ephemeral" };
          }
        }
      } else if (lastMessage.role === "tool") {
        lastMessage.cache_control = { type: "ephemeral" };
      }
    }

    return messages;
  }
}

async function createRoundTripShim(runtime, context) {
  const helpers = await loadOfficialBundleHelpers();
  return {
    shim: new OfficialBundleRoundTripShim(runtime, context, helpers),
    helpers,
    source: helpers.source,
  };
}

export async function normalizeChatRequestViaOfficialRoundTrip(requestBody, context) {
  const runtime = createRuntimeContext({ model: requestBody?.model, baseUrl: context?.baseUrl });
  const strictOfficial = context?.strictOfficial !== false;
  const officialRequest = translateOpenAIMessagesToOfficialRequest(requestBody?.messages, runtime);
  if (!officialRequest && strictOfficial) {
    throw new OfficialChatInputValidationError(
      "当前 messages 结构无法严格翻译为官方 iFlow internal request；请改用更标准的 OpenAI chat 消息形状。",
    );
  }
  const officialTools = translateOpenAIToolsToOfficialTools(requestBody?.tools);
  if (officialTools === null && strictOfficial) {
    throw new OfficialChatInputValidationError(
      "当前 tools 结构无法严格翻译为官方 iFlow internal tool declarations。",
    );
  }

  const { shim, helpers, source } = await createRoundTripShim(runtime, context);
  return {
    messages: officialRequest ? await shim.convertToOpenAIMessages(officialRequest) : [],
    tools: Array.isArray(officialTools) ? shim.convertToOpenAITools(officialTools) : [],
    inputTokens: officialRequest ? await shim.calculateInputTokens(officialRequest) : 0,
    source,
    helpers,
  };
}

export async function prepareOfficialBundleExecution(requestBody, context) {
  const source = requestBody && typeof requestBody === "object" ? requestBody : {};
  const runtime = createRuntimeContext({
    model: source.model,
    baseUrl: context?.baseUrl,
    authType: context?.authType,
  });
  const strictOfficial = context?.strictOfficial !== false;
  const officialRequest = translateOpenAIMessagesToOfficialRequest(source.messages, runtime);
  if (!officialRequest && strictOfficial) {
    throw new OfficialChatInputValidationError(
      "当前 messages 结构无法严格翻译为官方 iFlow internal request；请改用更标准的 OpenAI chat 消息形状。",
    );
  }
  const officialTools = translateOpenAIToolsToOfficialTools(source.tools);
  if (officialTools === null && strictOfficial) {
    throw new OfficialChatInputValidationError(
      "当前 tools 结构无法严格翻译为官方 iFlow internal tool declarations。",
    );
  }

  if (!officialRequest) {
    throw new OfficialChatInputValidationError("当前请求缺少可翻译的官方 messages 内容。");
  }

  if (Array.isArray(officialTools) && officialTools.length > 0) {
    officialRequest.config.tools = officialTools;
  }

  if (source.temperature !== undefined && source.temperature !== null) {
    officialRequest.config.temperature = source.temperature;
  }
  if (source.top_p !== undefined && source.top_p !== null) {
    officialRequest.config.topP = source.top_p;
  }
  if (shouldCarryStop(source.stop)) {
    officialRequest.config.stopSequences = source.stop;
  }

  const helpers = await loadOfficialBundleHelpers();
  const outputTokensLimit = resolveOutputTokensLimit(source);
  const thinkingEnabled = resolveThinkingEnabled(source);
  const thinkingConfig =
    thinkingEnabled === true ? deriveThinkingConfig(source, outputTokensLimit) : undefined;
  if (thinkingConfig && !isPositiveInteger(thinkingConfig.maxTokens)) {
    const fallbackMaxTokens = helpers?.MOt?.(runtime.model, 0, outputTokensLimit);
    thinkingConfig.maxTokens = isPositiveInteger(fallbackMaxTokens) ? fallbackMaxTokens : 8000;
  }
  const config = createOfficialBundleConfig(runtime, context, {
    outputTokensLimit,
    thinkingConfig,
  });
  const generator = new helpers.gH({
    model: runtime.model,
    apiKey: context?.apiKey || "sk-iflow2api-shim",
    baseUrl: context?.baseUrl || runtime.baseUrl,
    authType: runtime.authType,
    debugMode: false,
    multimodalModelName: config.getContentGeneratorConfig().multimodalModelName,
    config,
  });

  return {
    runtime,
    officialRequest,
    helpers,
    generator,
  };
}
