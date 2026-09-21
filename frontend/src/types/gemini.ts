// ============================================================
// gemini.ts — Google Gemini API Type Definitions
// MORAA GemVision
// ============================================================

/**
 * Supported Gemini model identifiers.
 * Configured centrally so upgrades happen in one place.
 * gemini-3.6-flash supports vision (image) input via inlineData.
 */
export const SUPPORTED_GEMINI_MODELS = {
  VISION: "gemini-2.5-flash",
  FAST: "gemini-2.5-flash-lite",
  POWERFUL: "gemini-2.5-pro",
} as const;

export type GeminiModelName = (typeof SUPPORTED_GEMINI_MODELS)[keyof typeof SUPPORTED_GEMINI_MODELS];

/**
 * Default model used across the application.
 * gemini-3.6-flash supports multimodal (text + image) inputs.
 */
export const DEFAULT_GEMINI_MODEL: GeminiModelName = SUPPORTED_GEMINI_MODELS.VISION;

// ─── Safety Settings ───────────────────────────────────────

export type HarmCategory =
  | "HARM_CATEGORY_HARASSMENT"
  | "HARM_CATEGORY_HATE_SPEECH"
  | "HARM_CATEGORY_SEXUALLY_EXPLICIT"
  | "HARM_CATEGORY_DANGEROUS_CONTENT";

export type HarmBlockThreshold =
  | "BLOCK_NONE"
  | "BLOCK_ONLY_HIGH"
  | "BLOCK_MEDIUM_AND_ABOVE"
  | "BLOCK_LOW_AND_ABOVE";

export interface SafetySetting {
  category: HarmCategory;
  threshold: HarmBlockThreshold;
}

// ─── Generation Config ─────────────────────────────────────

export interface GenerationConfig {
  temperature?: number;
  topP?: number;
  topK?: number;
  maxOutputTokens?: number;
  responseMimeType?: string;
  responseSchema?: Record<string, unknown>;
  stopSequences?: string[];
}

// ─── Gemini Generate Content Response Type ─────────────────

export interface GeminiGenerateContentResponse {
  id: string;
  model: string;
  text: string;
  usage: {
    promptTokens: number;
    completionTokens: number;
    totalTokens: number;
  };
}

// ─── Error Codes ───────────────────────────────────────────

export enum GeminiErrorCode {
  INVALID_IMAGE = "INVALID_IMAGE",
  EMPTY_IMAGE = "EMPTY_IMAGE",
  IMAGE_TOO_LARGE = "IMAGE_TOO_LARGE",
  TIMEOUT = "TIMEOUT",
  NETWORK_FAILURE = "NETWORK_FAILURE",
  API_ERROR = "API_ERROR",
  QUOTA_EXCEEDED = "QUOTA_EXCEEDED",
  SAFETY_BLOCKED = "SAFETY_BLOCKED",
  PARSE_FAILURE = "PARSE_FAILURE",
  UNKNOWN = "UNKNOWN",
}

export interface GeminiErrorResult {
  code: GeminiErrorCode;
  message: string;
  retryable: boolean;
  requestId?: string;
}
