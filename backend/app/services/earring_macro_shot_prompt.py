"""Macro Shot Prompt — Prompt 7 — MORAA GemVision.

Lean 3-block architecture (2026-09-17 revision) for realistic macro/detail
photographs of the exact jewellery product provided by the user.

Visual-failure fixes in this revision:
- REMOVED the `- silver/white-gold appearance` line from the preserve list.
  Listing silver as a *preserve target* primed diffusion models to render
  gold earrings as silver at macro scale (silver-hallucination root cause).
- REFERENCE-BOUND metal lock: the prompt binds metal colour/tone to the
  positively instead of relying on a generic colour-negative list.
- STRIPPED the 10-point FINAL VERIFICATION checklist and conversational
  clauses — non-functional for image models and token-diluting.
- CATALOG STILL-LIFE FIX (2026-09-17): the previous framing clause "all
  elements stay sharp and inspectable" forced the model to zoom out and
  render the FULL earring pair as a catalog still life. The framing is now
  a TIGHT MACRO CROP: one single focal section (a focal stone from the
  prongs, adjacent marquise leaf facets) fills 85% of the frame, shot on a
  100mm macro lens at f/2.8 with razor-thin depth of field. Full earrings
  in frame is declared a direct task failure.

The uploaded product image is the sole source of truth for the jewellery.
This prompt produces a professional macro photograph that highlights genuine
fine product details such as stone settings, cuts, prongs, metal texture,
surface finish, edges, joints, hooks, links, and construction details.

Architecture (assembly order — product identity always first)::

    REFERENCE IMAGE
          ↓
    MACRO SHOT FOUNDATION  ← this module (lean 3-block)
          ↓
    REFERENCE PRIORITY (marker present → ImageGenerationManager skips re-append)
          ↓
    MARKETPLACE PRESENTATION (auto-appended by ImageGenerationManager)
          ↓
    PROVIDER (OPENAI_IDENTITY_ANCHOR prepended by provider /
    REFERENCE_IMAGE_ANCHOR injected by Gemini provider)
          ↓
    IMAGE GENERATION
"""

from typing import Optional

# ─── Reuse proven constants from Prompt 1 ───────────────────────────────
# REFERENCE_PRIORITY_MARKER must stay byte-identical to the marker checked
# by ImageGenerationManager ("REFERENCE IMAGE PRIORITY" in prompt.upper())
# so the manager does not double-append its REFERENCE_PRIORITY_BLOCK.
# The proven preservation constants (anti-symmetry, colour lock, material
# fidelity, earring-type preservation) are still imported and composed —
# they are validated, high-value instructions, unlike the removed checklist.

from app.services.earring_ecommerce_prompt import (
    REFERENCE_PRIORITY_MARKER,
    ANTI_SYMMETRY_INSTRUCTION,
    COLOUR_LOCK_INSTRUCTION,
    MATERIAL_FIDELITY_INSTRUCTION,
    EARRING_TYPE_PRESERVATION,
    GENERIC_EARRING_PRESERVATION,
)


# ─── Metal affirmation (affirmative, macro-specific) ────────────────────
# Affirmative anchoring outperforms negative lists for colour fidelity:
# the model is told exactly what the metal IS, in the lighting context of
# this shot, instead of only what it must not become.
METAL_AFFIRMATION_INSTRUCTION = (
    "METAL AFFIRMATION — MACRO COLOR LOCK (NON-NEGOTIABLE):\n"
    "Match the metal colour and tone of the reference image EXACTLY. The "
    "reference is the sole authority on metal type: render yellow gold as "
    "rich saturated yellow gold, white gold as white gold, silver as "
    "silver, rhodium as rhodium, platinum as platinum, and rose gold as "
    "rose gold — never substitute one metal for another. Every visible "
    "prong, link, frame, and bezel takes the reference metal's colour — "
    "under this macro studio lighting as well. High dynamic range capture "
    "retaining the reference metal's true saturation: zero desaturation, "
    "zero cooling, and no specular highlight blowouts that shift the metal "
    "toward a different colour."
)


# ─── Macro photography style (lean) ─────────────────────────────────────
_MACRO_STYLE_INSTRUCTION = (
    "MACRO STYLE EXECUTION (NON-NEGOTIABLE):\n"
    "FRAMING & COMPOSITION: TIGHT MACRO CROP. Do NOT show the full earring. "
    "Do NOT show both earrings. Show only ONE tight focal section: an "
    "extreme close-up of a single focal stone from the reference, its "
    "reference-matched prongs, and adjacent facets filling 85% of the "
    "frame — reproduce the gemstone cut actually present in the reference "
    "(e.g. emerald, round, marquise, pear, oval); never invent a cut.\n"
    "• OPTICS: 100mm macro lens at f/2.8, extreme shallow depth of field. "
    "Razor-sharp focus on prong craftsmanship and gemstone facet reflections "
    "with soft background blur.\n"
    "• PROHIBITION: Do NOT frame the entire earring pair. Full earrings in "
    "frame is a direct task failure.\n"
    "• Sharp detail on the focal section only: stone facets, prong "
    "structure, metal texture, edges, joints.\n"
    "• Controlled professional studio lighting that reveals detail without "
    "changing product colour.\n"
    "• Clean, unobtrusive background — secondary to the jewellery; no "
    "props, people, hands, or unrelated objects.\n"
    "• This is genuine macro photography — not a digital zoom, not a CGI "
    "render, not an exaggerated fantasy macro effect.\n"
    "• Preferred aspect ratio: 4:5."
)


# ─── Lean global negatives (concise — negative lists do not scale) ──────
_MACRO_GLOBAL_NEGATIVES = (
    "DO NOT generate: a substitute metal type that differs from the "
    "reference, oversized or resized "
    "jewellery, added/removed/merged stones or beads, grey background, "
    "full earring pair in frame, entire earring visible, catalog still-life "
    "framing, earring photographed on cloth or fabric, artificial sharpening "
    "that invents detail, excessive sparkle, text, logo, or watermark."
)


# ─── Complete Prompt Builder ───────────────────────────────────────────
def build_macro_shot_prompt(
    earring_type: Optional[str] = None,
) -> str:
    """Build the lean 3-block Prompt 7 Macro Shot prompt.

    Signature, parameter default, and ``str`` return type are unchanged.
    The prompt embeds REFERENCE_PRIORITY_MARKER to prevent the
    ImageGenerationManager from double-appending REFERENCE_PRIORITY_BLOCK.

    Args:
        earring_type: Optional earring type ("Hoop", "Stud", "Dangle").
            When provided, type-specific preservation rules are included.
            When None, generic earring preservation rules are used.

    Returns:
        The complete Prompt 7 Macro Shot prompt string.
    """
    parts: list[str] = []

    # ── BLOCK 1 — Identity & Metal Lock (anchor + affirmative colour) ──
    parts.append(
        "PRODUCT FIDELITY — ABSOLUTE PRIORITY:\n"
        "The uploaded reference image is the single source of truth for "
        "the jewellery.\n"
        "Preserve the original product exactly: exact product geometry, "
        "silhouette, gemstone count and placement, gemstone cuts and facet "
        "structure, prong settings, micro-pavé details, bead arrangement, "
        "hanging components, hooks and connectors, metal structure, "
        "original proportions, distinctive product details, surface finish "
        "(polished, brushed, matte, hammered), construction details "
        "(joints, links, settings), visible imperfections, and asymmetry.\n"
        "Do NOT: redesign the jewellery, reconstruct into a similar "
        "product, add/remove/duplicate stones, merge components, change "
        "geometry, beautify into a different design, invent missing "
        "details, simplify detailed components, or make the design more "
        "symmetrical.\n"
        "Every visible stone, bead, prong, connector, clasp, hook, link, "
        "and structural element must correspond to the reference image. "
        "If any detail is unclear in the reference, DO NOT invent a "
        "replacement detail."
    )
    parts.append(METAL_AFFIRMATION_INSTRUCTION)
    parts.append(COLOUR_LOCK_INSTRUCTION)
    parts.append(MATERIAL_FIDELITY_INSTRUCTION)

    # ── BLOCK 2 — Task & Style Execution ─────────────────────────────
    parts.append(
        "TASK: Extreme Macro Jewelry Close-Up Photography.\n"
        "Create a realistic extreme macro close-up of ONE tight focal "
        "section of the exact jewellery from the provided reference image, "
        "photographed at macro range so genuine fine product details are "
        "clearly visible. Do NOT render the full earring or the earring "
        "pair."
    )
    parts.append(REFERENCE_PRIORITY_MARKER)
    parts.append(ANTI_SYMMETRY_INSTRUCTION)
    parts.append(_MACRO_STYLE_INSTRUCTION)

    # ── Earring Type ────────────────────────────────────────────────
    if earring_type and earring_type in EARRING_TYPE_PRESERVATION:
        parts.append(EARRING_TYPE_PRESERVATION[earring_type])
    else:
        parts.append(GENERIC_EARRING_PRESERVATION)

    # ── BLOCK 3 — Negative Token Cleanliness ────────────────────────
    parts.append(_MACRO_GLOBAL_NEGATIVES)

    return "\n\n".join(parts)


# ─── Self-test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    prompt = build_macro_shot_prompt()
    assert REFERENCE_PRIORITY_MARKER in prompt
    assert "sole authority on metal type" in prompt
    assert "never invent a cut" in prompt
    assert "silver/white-gold appearance" not in prompt
    assert "FINAL VERIFICATION" not in prompt
    print(f"lean macro prompt: {len(prompt)} chars")
