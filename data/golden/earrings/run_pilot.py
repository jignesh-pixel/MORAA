"""
Phase 4D — Limited Real-Earring Controlled Generation Pilot

This is a TEST-ONLY script. It does NOT modify any production code.
It uses the existing production pipeline via API calls and direct provider imports.

Safety constraints:
- No production code modification
- No Task 3 locked files modified
- No PFIE enabling
- No ProductFidelity population
- No provider routing changes
- No marketplace behavior changes
"""

import base64
import json
import os
import sys
import time
import httpx

# Load backend .env before any app imports
def _load_backend_env():
    """Load environment variables from backend/.env file."""
    env_path = os.path.join(PROJECT_ROOT, "backend", ".env")
    if not os.path.exists(env_path):
        print(f"WARNING: backend/.env not found at {env_path}")
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


# ─── Configuration ────────────────────────────────────────────────────

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_load_backend_env()

# ─── Configuration ────────────────────────────────────────────────────

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
RESULTS_DIR = os.path.join(PROJECT_ROOT, "data", "golden", "earrings", "results")

REFERENCES = {
    "R1": os.path.join(PROJECT_ROOT, "docs", "samples", "Earring Examples", "Ex1.jpeg"),
    "R2": os.path.join(PROJECT_ROOT, "docs", "samples", "Earring Examples", "example 1.jpeg"),
}

API_BASE = "http://localhost:8000"
GENERATE_ENDPOINT = f"{API_BASE}/api/generate-image"

# Scene prompt — kept consistent across all tests (baseline, no optimization)
SCENE_PROMPT = (
    "Professional product photography of jewellery earring. "
    "Studio lighting with controlled shadows highlighting texture and craftsmanship. "
    "High resolution, sharp focus on product details. "
    "No text, no watermarks, no human models."
)


def load_reference_base64(path: str) -> str:
    """Load reference image and encode as base64."""
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def call_api_generate(
    reference_b64: str,
    mime_type: str = "image/jpeg",
    aspect_ratio: str = "4:5",
    marketplace: str = None,
    timeout: int = 240,
) -> dict:
    """Call the production /api/generate-image endpoint."""
    payload = {
        "prompt": SCENE_PROMPT,
        "aspect_ratio": aspect_ratio,
        "reference_image": reference_b64,
        "reference_mime_type": mime_type,
    }
    if marketplace:
        payload["marketplace"] = marketplace

    r = httpx.post(GENERATE_ENDPOINT, json=payload, timeout=timeout)
    return r.json()


def save_result(data: dict, output_path: str) -> bool:
    """Extract and save the generated image from API response."""
    url = data.get("image_url") or ""
    if url.startswith("data:"):
        img_b64 = url.split(",", 1)[1]
        raw = base64.b64decode(img_b64)
        with open(output_path, "wb") as f:
            f.write(raw)
        return True
    return False


def run_openai_test(
    ref_label: str,
    ref_path: str,
    case_label: str,
    marketplace: str,
    aspect_ratio: str = "4:5",
):
    """Execute an OpenAI test case via the API."""
    print(f"\n{'='*60}")
    print(f"CASE {case_label}: OpenAI + {'Amazon' if marketplace else 'No marketplace'}")
    print(f"Reference: {ref_label} ({os.path.basename(ref_path)})")
    print(f"{'='*60}")

    ref_b64 = load_reference_base64(ref_path)
    print(f"Reference loaded: {len(ref_b64)} chars base64")

    start = time.time()
    result = call_api_generate(
        reference_b64=ref_b64,
        aspect_ratio=aspect_ratio,
        marketplace=marketplace,
    )
    elapsed = time.time() - start

    print(f"HTTP result: success={result.get('success')}")
    print(f"Provider: {result.get('provider')}")
    print(f"Model used: {result.get('model_used')}")
    print(f"Fallback used: {result.get('fallback_used')}")
    print(f"Generation time: {result.get('generation_time')}s")
    print(f"Wall clock: {elapsed:.1f}s")

    if result.get("error"):
        print(f"ERROR: {result['error']}")
        return None

    # Determine output filename
    provider = result.get("provider", "unknown")
    marketplace_tag = "amazon" if marketplace else "no_marketplace"
    output_name = f"{ref_label}_{provider}_{marketplace_tag}.png"
    output_path = os.path.join(RESULTS_DIR, output_name)

    if save_result(result, output_path):
        sz = os.path.getsize(output_path)
        print(f"Saved: {output_name} ({sz} bytes)")
        return output_name
    else:
        print("FAILED to save image")
        return None


def run_gemini_test(
    ref_label: str,
    ref_path: str,
    case_label: str,
    marketplace: str,
    aspect_ratio: str = "4:5",
):
    """Execute a Gemini test case by directly calling the provider."""
    print(f"\n{'='*60}")
    print(f"CASE {case_label}: Gemini + {'Amazon' if marketplace else 'No marketplace'}")
    print(f"Reference: {ref_label} ({os.path.basename(ref_path)})")
    print(f"{'='*60}")

    # Import and use the Gemini provider directly with mocked settings
    sys.path.insert(0, BACKEND_DIR)
    from unittest.mock import patch, MagicMock
    import asyncio

    # Load reference image bytes
    with open(ref_path, "rb") as ref_bytes:
        ref_data = ref_bytes.read()
    print(f"Reference loaded: {len(ref_data)} bytes")

    # Build the prompt with reference priority block (as production would)
    from app.ai.product_fidelity import REFERENCE_PRIORITY_BLOCK
    from app.ai.marketplaces.amazon_india import get_amazon_india_earrings_presentation

    full_prompt = SCENE_PROMPT

    # Add reference priority block (production behavior)
    if REFERENCE_PRIORITY_BLOCK not in full_prompt:
        full_prompt += "\n\n" + REFERENCE_PRIORITY_BLOCK

    # Add marketplace block if requested (production behavior)
    if marketplace:
        marketplace_presentation = get_amazon_india_earrings_presentation()
        full_prompt += "\n\n" + marketplace_presentation.prompt_block

    print(f"Prompt length: {len(full_prompt)} chars")
    print(f"Marketplace: {marketplace or 'None'}")

    # Mock settings to force Gemini as primary
    # Must mock settings in BOTH the manager AND the provider modules
    gemini_api_key = os.environ.get("GEMINI_API_KEY", "")
    with patch("app.ai.image_generation_manager.settings") as mock_mgr_settings, \
         patch("app.ai.providers.gemini_image_provider.settings") as mock_prov_settings:
        mock_mgr_settings.PRIMARY_IMAGE_PROVIDER = "gemini"
        mock_mgr_settings.FALLBACK_IMAGE_PROVIDER = "gemini"
        mock_prov_settings.GEMINI_API_KEY = gemini_api_key
        mock_prov_settings.GEMINI_IMAGE_MODEL = "gemini-2.0-flash-preview-image-generation"

        from app.ai.image_generation_manager import ImageGenerationManager

        manager = ImageGenerationManager()

        # Determine aspect ratio for marketplace
        effective_ar = aspect_ratio
        if marketplace:
            effective_ar = "1:1"  # Amazon forces 1:1

        context = {
            "request_id": f"phase4d-{ref_label.lower()}-gemini",
            "aspect_ratio": effective_ar,
        }

        start = time.time()
        try:
            result = asyncio.get_event_loop().run_until_complete(
                manager.generate_image(
                    prompt=full_prompt,
                    context=context,
                    reference_image=ref_data,
                    reference_mime_type="image/jpeg",
                    marketplace=marketplace,
                )
            )
            elapsed = time.time() - start

            print(f"Success: {result.success}")
            print(f"Provider: {result.provider_name}")
            print(f"Fallback used: {result.fallback_used}")
            print(f"Processing time: {result.processing_time}s")
            print(f"Wall clock: {elapsed:.1f}s")

            if not result.success:
                print(f"ERROR: {result.error}")
                return None

            # Save the result
            marketplace_tag = "amazon" if marketplace else "no_marketplace"
            output_name = f"{ref_label}_gemini_{marketplace_tag}.png"
            output_path = os.path.join(RESULTS_DIR, output_name)

            if result.image_data:
                with open(output_path, "wb") as f:
                    f.write(result.image_data)
                sz = os.path.getsize(output_path)
                print(f"Saved: {output_name} ({sz} bytes)")
                return output_name
            elif result.image_url:
                if save_result({"image_url": result.image_url}, output_path):
                    sz = os.path.getsize(output_path)
                    print(f"Saved: {output_name} ({sz} bytes)")
                    return output_name

            print("FAILED to save image — no image data in result")
            return None

        except Exception as e:
            elapsed = time.time() - start
            print(f"EXCEPTION after {elapsed:.1f}s: {e}")
            return None


def main():
    """Execute the full controlled generation matrix."""
    os.makedirs(RESULTS_DIR, exist_ok=True)

    results = {}

    # ─── Reference R1 ──────────────────────────────────────────────
    print("\n" + "#"*70)
    print("# REFERENCE R1: Ex1.jpeg")
    print("#"*70)

    # Case A: OpenAI + Amazon
    r = run_openai_test("R1", REFERENCES["R1"], "A", marketplace="amazon_india_fashion_earrings")
    results["R1_A"] = r

    # Case B: OpenAI + no marketplace
    r = run_openai_test("R1", REFERENCES["R1"], "B", marketplace=None)
    results["R1_B"] = r

    # Case C: Gemini + no marketplace
    r = run_gemini_test("R1", REFERENCES["R1"], "C", marketplace=None)
    results["R1_C"] = r

    # Case D: Gemini + Amazon
    r = run_gemini_test("R1", REFERENCES["R1"], "D", marketplace="amazon_india_fashion_earrings")
    results["R1_D"] = r

    # ─── Reference R2 ──────────────────────────────────────────────
    print("\n" + "#"*70)
    print("# REFERENCE R2: example 1.jpeg")
    print("#"*70)

    # Case A: OpenAI + Amazon
    r = run_openai_test("R2", REFERENCES["R2"], "A", marketplace="amazon_india_fashion_earrings")
    results["R2_A"] = r

    # Case B: OpenAI + no marketplace
    r = run_openai_test("R2", REFERENCES["R2"], "B", marketplace=None)
    results["R2_B"] = r

    # Case C: Gemini + no marketplace
    r = run_gemini_test("R2", REFERENCES["R2"], "C", marketplace=None)
    results["R2_C"] = r

    # Case D: Gemini + Amazon
    r = run_gemini_test("R2", REFERENCES["R2"], "D", marketplace="amazon_india_fashion_earrings")
    results["R2_D"] = r

    # ─── Summary ───────────────────────────────────────────────────
    print("\n" + "#"*70)
    print("# EXECUTION SUMMARY")
    print("#"*70)
    for key, val in results.items():
        status = val if val else "NOT EXECUTED"
        print(f"  {key}: {status}")

    # Save results manifest
    manifest_path = os.path.join(RESULTS_DIR, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nManifest saved: {manifest_path}")


if __name__ == "__main__":
    main()
