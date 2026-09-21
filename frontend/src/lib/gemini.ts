// ============================================================
// gemini.ts — Core Google Gemini Client Configuration
// MORAA GemVision
// ============================================================

import { GoogleGenAI } from "@google/genai";
import { DEFAULT_GEMINI_MODEL } from "@/types/gemini";
import type { GeminiModelName } from "@/types/gemini";

/**
 * Application-level Gemini configuration.
 * To upgrade the model, change DEFAULT_GEMINI_MODEL in types/gemini.ts.
 */
export interface GeminiConfig {
  model: GeminiModelName;
  maxOutputTokens: number;
  temperature: number;
  topP: number;
  topK: number;
}

const DEFAULT_CONFIG: GeminiConfig = {
  model: DEFAULT_GEMINI_MODEL,
  maxOutputTokens: 8192,
  temperature: 0.2,
  topP: 0.95,
  topK: 40,
};

// ─── Client Singleton ──────────────────────────────────────

let _client: GoogleGenAI | null = null;
let _activeConfig: GeminiConfig = DEFAULT_CONFIG;

/**
 * Get or initialize the Google Gemini client.
 * Reads the API key from GEMINI_API_KEY environment variable.
 * Throws immediately if the key is missing.
 */
export function getGeminiClient(): GoogleGenAI {
  if (_client) return _client;

  const apiKey = process.env.GEMINI_API_KEY;

  if (!apiKey || apiKey.trim() === "") {
    throw new Error(
      "[MORAA GemVision] GEMINI_API_KEY is not configured. " +
      "Please set the GEMINI_API_KEY environment variable in your .env.local file."
    );
  }

  _client = new GoogleGenAI({ apiKey, apiVersion: 'v1beta' });
  return _client;
}

/**
 * Get the active Gemini configuration.
 */
export function getGeminiConfig(): GeminiConfig {
  return { ..._activeConfig };
}

/**
 * Override the default Gemini configuration at runtime.
 * Only provided fields will be updated.
 */
export function configureGemini(overrides: Partial<GeminiConfig>): GeminiConfig {
  _activeConfig = { ..._activeConfig, ...overrides };
  return { ..._activeConfig };
}

/**
 * Reset the client singleton (useful for testing).
 */
export function resetGeminiClient(): void {
  _client = null;
  _activeConfig = DEFAULT_CONFIG;
}

/**
 * Check if the Gemini API key is configured.
 * Does NOT validate the key against the API.
 */
export function isGeminiConfigured(): boolean {
  const key = process.env.GEMINI_API_KEY;
  return !!key && key.trim() !== "";
}
