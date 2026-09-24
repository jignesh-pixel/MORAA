// ============================================================
// gemini.service.ts — Google Gemini API Business Logic
// MORAA GemVision
// ============================================================
// Uses @google/genai SDK with gemini-2.5-flash model which supports
// vision (image) input via inlineData parts.
// ============================================================

import { getGeminiClient, getGeminiConfig } from "@/lib/gemini";
import { logger, generateRequestId } from "@/lib/logger";

import {
  AppError,
  GeminiError,
  QuotaExceededError,
  SafetyBlockedError,
  RetryableNetworkError,
  TimeoutError,
  ParseError,
} from "@/lib/errors";

// ─── Constants ─────────────────────────────────────────────

/**
 * Pinned analysis model. Kept as an explicit literal so the direct Gemini
 * route can never be resolved against a non-vision or retired model name.
 */
const ANALYSIS_MODEL = "gemini-2.5-flash";

const MAX_RETRIES = 3;
const BASE_RETRY_DELAY_MS = 1000;
const TIMEOUT_MS = 120_000;
const MAX_IMAGE_SIZE_BYTES = 20 * 1024 * 1024; // 20 MB

// ─── Request Builder ───────────────────────────────────────

interface BuildContentParams {
  prompt: string;
  imageBase64: string;
  mimeType: string;
}

function buildGeminiContents({ prompt, imageBase64, mimeType }: BuildContentParams) {
  return [
    {
      role: "user",
      parts: [
        { text: prompt },
        {
          inlineData: {
            mimeType,
            data: imageBase64,
          },
        },
      ],
    },
  ];
}

// ─── Response Parser ───────────────────────────────────────

function extractText(response: any): string {
  // Debug: log the full response object shape (remove after verification)
  console.dir(
    {
      _responseType: typeof response,
      _hasText: "text" in (response ?? {}),
      _textType: typeof response?.text,
      _textIsFunction: typeof response?.text === "function",
      _textPreview:
        typeof response?.text === "string"
          ? response.text.slice(0, 300)
          : typeof response?.text === "function"
            ? "(function)"
            : String(response?.text),
      _hasCandidates: "candidates" in (response ?? {}),
      _candidatesCount: response?.candidates?.length,
      _candidate0FinishReason: response?.candidates?.[0]?.finishReason,
      _hasUsageMetadata: "usageMetadata" in (response ?? {}),
      _allKeys: Object.keys(response ?? {}),
    },
    { depth: null }
  );

  // @google/genai SDK v2.x: response.text is a STRING property, not a method
  const text = response?.text ?? "";

  if (!text) {
    // Check finish reason if available
    const candidate = response?.candidates?.[0];
    if (candidate?.finishReason) {
      throw new GeminiError(
        `Gemini response blocked. Finish reason: ${candidate.finishReason}`,
        "SAFETY_BLOCKED",
        false,
        400
      );
    }
    throw new GeminiError("Empty response from Gemini API.", "EMPTY_RESPONSE", false, 500);
  }

  return text;
}

export function parseJsonResponse(text: string): Record<string, unknown> {
  let cleaned = text.trim();

  if (cleaned.startsWith("```json")) {
    cleaned = cleaned.slice(7);
  } else if (cleaned.startsWith("```")) {
    cleaned = cleaned.slice(3);
  }

  if (cleaned.endsWith("```")) {
    cleaned = cleaned.slice(0, -3);
  }

  cleaned = cleaned.trim();

  try {
    return JSON.parse(cleaned) as Record<string, unknown>;
  } catch {
    throw new ParseError(
      "Failed to parse Gemini response as JSON. Response was: " + cleaned.slice(0, 200)
    );
  }
}

// ─── Retry Logic ───────────────────────────────────────────

function getDelayWithJitter(attempt: number): number {
  const delay = BASE_RETRY_DELAY_MS * Math.pow(2, attempt);
  const jitter = Math.random() * 0.3 * delay;
  return Math.min(delay + jitter, 10_000);
}

// ─── Map errors to AppError hierarchy ─────────────────────

function mapGeminiError(error: unknown): AppError {
  if (error instanceof AppError) return error;

  const msg = error instanceof Error ? error.message.toLowerCase() : String(error).toLowerCase();

  if (msg.includes("429") || msg.includes("rate limit") || msg.includes("quota") || msg.includes("too many requests")) {
    return new QuotaExceededError();
  }
  if (msg.includes("safety") || msg.includes("content_filter") || msg.includes("blocked") || msg.includes("finish_reason_safety")) {
    return new SafetyBlockedError();
  }
  if (msg.includes("timeout") || msg.includes("timed out") || msg.includes("abort") || msg.includes("deadline")) {
    return new TimeoutError();
  }
  if (msg.includes("network") || msg.includes("econnrefused") || msg.includes("enotfound") || msg.includes("fetch")) {
    return new RetryableNetworkError();
  }
  if (msg.includes("401") || msg.includes("unauthorized") || msg.includes("invalid key") || msg.includes("api key") || msg.includes("permission_denied")) {
    return new GeminiError("Gemini API authentication failed. Check your GEMINI_API_KEY.", "AUTH_FAILURE", false, 401);
  }
  if (msg.includes("403") || msg.includes("forbidden")) {
    return new GeminiError("Gemini API access denied.", "ACCESS_DENIED", false, 403);
  }
  if (msg.includes("404") || msg.includes("not found") || msg.includes("model") || msg.includes("not_found")) {
    return new GeminiError("Gemini model not found or unavailable.", "MODEL_NOT_FOUND", false, 404);
  }

  return new GeminiError(String(error));
}

// ─── Main Service ──────────────────────────────────────────

export interface GeminiServiceResult {
  text: string;
  requestId: string;
  model: string;
  promptTokens: number;
  completionTokens: number;
  executionTimeMs: number;
}

export async function analyzeImageWithGemini(
  prompt: string,
  imageBase64: string,
  mimeType: string
): Promise<GeminiServiceResult> {
  const requestId = generateRequestId();
  logger.info("Starting Gemini analysis", { requestId, mimeType });

  const imageBytes = Buffer.from(imageBase64, "base64").length;
  if (imageBytes > MAX_IMAGE_SIZE_BYTES) {
    throw new AppError(
      `Image too large: ${(imageBytes / 1024 / 1024).toFixed(1)} MB. Maximum is 20 MB.`,
      "IMAGE_TOO_LARGE",
      false,
      413
    );
  }

  const client = getGeminiClient();
  const config = getGeminiConfig();
  const contents = buildGeminiContents({ prompt, imageBase64, mimeType });
  const model = ANALYSIS_MODEL;

  let lastError: Error | null = null;

  for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
    const startTime = Date.now();

    try {
      logger.debug("Gemini API call start", {
        requestId,
        attempt: attempt + 1,
        model,
      });

      const response = await client.models.generateContent({
        model,
        contents,
        config: {
          temperature: config.temperature,
          topP: config.topP,
          topK: config.topK,
          maxOutputTokens: config.maxOutputTokens,
        },
      });

      const executionTimeMs = Date.now() - startTime;
      const text = extractText(response);

      // Extract token usage from the response
      const usage = (response as any)?.usageMetadata || {};
      const promptTokens = usage.promptTokenCount ?? 0;
      const completionTokens = usage.candidatesTokenCount ?? 0;

      logger.info("Gemini analysis completed", {
        requestId,
        executionTimeMs,
        promptTokens,
        completionTokens,
        model,
      });

      return {
        text,
        requestId,
        model,
        promptTokens,
        completionTokens,
        executionTimeMs,
      };
    } catch (error: unknown) {
      lastError = error instanceof Error ? error : new Error(String(error));

      const mappedError = mapGeminiError(error);

      logger.warn("Gemini API attempt failed", {
        requestId,
        attempt: attempt + 1,
        error: mappedError.message,
        code: mappedError.code,
        retryable: mappedError.retryable,
      });

      if (!mappedError.retryable) {
        throw mappedError;
      }

      if (attempt >= MAX_RETRIES - 1) {
        logger.error("Gemini API all retries exhausted", {
          requestId,
          attempts: MAX_RETRIES,
          lastError: mappedError.message,
        });
        throw mappedError;
      }

      const delay = getDelayWithJitter(attempt);
      await new Promise((resolve) => setTimeout(resolve, delay));
    }
  }

  throw lastError ?? new GeminiError("Unknown error during Gemini analysis.");
}
