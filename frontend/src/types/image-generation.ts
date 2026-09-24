// ============================================================
// image-generation.ts — Image Generation Type Definitions
// MORAA GemVision
// ============================================================
// Types for the AI image generation feature that generates
// product images from promotional prompts.
// ============================================================


export type ImageGenerationStatus =
  | "idle"
  | "generating"
  | "completed"
  | "error";

export interface ImageGenerationState {
  status: ImageGenerationStatus;
  imageUrl: string | null;
  provider: string;
  fallbackUsed: boolean;
  fallbackReason: string | null;
  generationTime: number;
  errorMessage: string | null;
  /** True when the image was generated via a manual provider switch. */
  manualSwitch?: boolean;
}

export interface GenerateImageRequest {
  prompt: string;
  aspectRatio?: string;
  imageId?: string;
  /**
   * Optional base64-encoded reference image of the original product.
   * When provided, the AI will use this as a visual reference to preserve
   * product identity (shape, stones, metalwork, proportions).
   * Supports data:image/...;base64,... format or raw base64.
   */
  referenceImage?: string;
  /**
   * MIME type of the reference image (default: image/jpeg).
   */
  referenceMimeType?: string;
  /**
   * Optional marketplace identifier.
   * When provided, marketplace-specific presentation rules are applied
   * on the backend. When omitted, the image is generated without
   * marketplace overlay.
   */
  marketplace?: string;
  /**
   * Optional provider to force (e.g. 'openai' or 'gemini').
   * When set, uses ONLY this provider with no automatic fallback.
   * Used for manual provider switching.
   */
  forceProvider?: string;
  /**
   * When true, the backend must pass a lightweight deterministic quality
   * floor (decodable, minimum dimensions) before the image is returned as
   * a success. Default false/omitted — existing callers are unaffected.
   */
  enforceQualityFloor?: boolean;
}

export interface GenerateImageResponse {
  success: boolean;
  provider: string;
  fallback_used: boolean;
  fallback_reason: string | null;
  image_url: string | null;
  generation_time: number;
  model_used?: string;
  error?: string;
  metadata?: Record<string, unknown>;
}
