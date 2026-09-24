// ============================================================
// route.ts — Direct Gemini Image Analysis API Route
// MORAA GemVision
// ============================================================
// Route path: /api/gemini/analyze
//
// Accepts an image (base64 + mimeType), calls Google Gemini
// directly via the @google/genai SDK, and returns a structured
// AnalysisResult.
//
// This is the PRIMARY analysis endpoint called by the frontend.
// All prompt construction, retry logic, and response parsing
// are handled by the existing image-analysis.service.ts pipeline.
// ============================================================

import { NextRequest, NextResponse } from "next/server";
import { analyzeImage } from "@/services/image-analysis.service";
import { generateRequestId } from "@/lib/logger";
import { logger } from "@/lib/logger";
import type { ImageAnalysisRequest } from "@/types/analysis";

// ─── Configuration ─────────────────────────────────────────

export const maxDuration = 120;
export const dynamic = "force-dynamic";

// ─── Validation ─────────────────────────────────────────────

function validateBody(body: unknown): body is ImageAnalysisRequest {
  if (!body || typeof body !== "object") {
    return false;
  }

  const data = body as Record<string, unknown>;

  if (typeof data.imageBase64 !== "string" || data.imageBase64.length === 0) {
    return false;
  }

  if (typeof data.mimeType !== "string" || !data.mimeType.startsWith("image/")) {
    return false;
  }

  return true;
}

// ─── Handlers ───────────────────────────────────────────────

/**
 * POST /api/gemini/analyze
 *
 * Accepts:  { imageBase64: string, mimeType: string, customPrompt?: string }
 *
 * Calls Google Gemini directly via the image-analysis pipeline.
 * Returns { success, data, metadata, error? } in the standard format.
 */
export async function POST(request: NextRequest): Promise<NextResponse> {
  try {
    const body: unknown = await request.json();

    if (!validateBody(body)) {
      return NextResponse.json(
        {
          success: false,
          data: null,
          metadata: null,
          error: {
            code: "INVALID_REQUEST",
            message:
              "Invalid request body. Expected { imageBase64: string, mimeType: string }.",
          },
        },
        { status: 400 }
      );
    }

    // Call Gemini directly via the existing analysis pipeline
    const result = await analyzeImage(body);

    if (!result.success || !result.data) {
      logger.error("Analysis failed", {
        error: result.error?.message,
        metadata: result.metadata,
      });
      return NextResponse.json(
        {
          success: false,
          data: null,
          metadata: result.metadata || {
            requestId: generateRequestId(),
            executionTimeMs: 0,
            model: "",
            promptTokens: 0,
            completionTokens: 0,
            timestamp: new Date().toISOString(),
          },
          error: {
            code: result.error?.code || "ANALYSIS_FAILED",
            message: result.error?.message || "Analysis failed",
          },
        },
        { status: 422 }
      );
    }

    logger.info("Analysis completed via direct Gemini call", {
      category: result.data?.category,
      confidenceScore: result.data?.confidenceScore,
      executionTimeMs: result.metadata.executionTimeMs,
    });

    return NextResponse.json(result, { status: 200 });
  } catch (error: unknown) {
    const errMsg = error instanceof Error ? error.message : String(error);

    // Raw SDK/trace output — surfaces request-validation rejections from the
    // Google GenAI SDK that the structured logger would flatten.
    console.error("ANALYSIS_ROUTE_ERROR:", error);

    logger.error("Unhandled error in /api/gemini/analyze", {
      error: errMsg,
    });

    return NextResponse.json(
      {
        success: false,
        data: null,
        metadata: null,
        error: {
          code: "INTERNAL_ERROR",
          message: "An unexpected error occurred during analysis.",
        },
      },
      { status: 500 }
    );
  }
}

/**
 * GET /api/gemini/analyze
 *
 * Returns health/status of the analysis endpoint.
 */
export async function GET(): Promise<NextResponse> {
  return NextResponse.json({
    status: "ready",
    service: "gemini-analysis",
    timestamp: new Date().toISOString(),
  });
}
