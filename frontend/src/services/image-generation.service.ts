// ============================================================
// image-generation.service.ts — Image Generation Service
// MORAA GemVision
// ============================================================
// Calls the backend's /api/generate-image endpoint which routes
// through OpenAI gpt-image-1 (primary, reference-aware) -> Gemini
// (fallback) with automatic failover for recoverable errors.
// ============================================================

import { logger, generateRequestId } from "@/lib/logger";
import type {
  GenerateImageRequest,
  GenerateImageResponse,
} from "@/types/image-generation";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export async function generateImage(
  request: GenerateImageRequest,
): Promise<GenerateImageResponse> {
  const requestId = generateRequestId();
  const startTime = Date.now();

  logger.info("Image generation requested via backend", {
    requestId,
    promptLength: request.prompt.length,
    aspectRatio: request.aspectRatio || "4:5",
  });

  try {
    const response = await fetch(`${API_BASE_URL}/api/generate-image`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        prompt: request.prompt,
        aspect_ratio: request.aspectRatio || "4:5",
        image_id: request.imageId,
        reference_image: request.referenceImage || null,
        reference_mime_type: request.referenceMimeType || "image/jpeg",
        marketplace: request.marketplace,
        force_provider: request.forceProvider || null,
        enforce_quality_floor: request.enforceQualityFloor || false,
      }),
    });

    // Check for HTTP errors before parsing JSON
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try {
        const errorBody = await response.json();
        detail = errorBody.detail || errorBody.error || detail;
      } catch {
        // Response body may not be JSON — use status text
        detail = response.statusText || detail;
      }

      const category =
        response.status === 404
          ? "Endpoint not found"
          : response.status === 422
            ? "Invalid request"
            : response.status >= 500
              ? "Image provider unavailable"
              : `Request failed (${response.status})`;

      logger.error("Image generation HTTP error", {
        requestId,
        status: response.status,
        detail,
        totalTime: Date.now() - startTime,
      });

      return {
        success: false,
        provider: "none",
        fallback_used: false,
        fallback_reason: null,
        image_url: null,
        generation_time: 0,
        error: `${category}: ${detail}`,
      };
    }

    const result: GenerateImageResponse = await response.json();

    if (result.success) {
      logger.info("Image generation completed", {
        requestId,
        provider: result.provider,
        fallbackUsed: result.fallback_used,
        generationTime: result.generation_time,
        totalTime: Date.now() - startTime,
      });
    } else {
      logger.error("Image generation failed", {
        requestId,
        error: result.error,
        provider: result.provider,
      });
    }

    return result;
  } catch (error: unknown) {
    const errMsg = error instanceof Error ? error.message : String(error);
    const isNetworkError = errMsg.includes("Failed to fetch") || errMsg.includes("NetworkError");
    const category = isNetworkError ? "Backend unavailable" : "Image generation error";

    logger.error("Image generation network error", {
      requestId,
      error: errMsg,
      totalTime: Date.now() - startTime,
    });
    return {
      success: false,
      provider: "none",
      fallback_used: false,
      fallback_reason: null,
      image_url: null,
      generation_time: 0,
      error: `${category}: ${errMsg}`,
    };
  }
}
