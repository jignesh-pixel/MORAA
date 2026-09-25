"""Earring E-Commerce Main Image Prompt Foundation — MORAA GemVision.

Single authoritative prompt foundation for generating e-commerce-ready
main images for Fashion Jewellery → Earrings.

This module is:
- **Provider-independent**: works with OpenAI, Gemini, or any future provider.
- **Marketplace-independent**: contains NO Amazon/Myntra/eBay-specific rules.
  Marketplace rules are appended by ImageGenerationManager separately.
- **Reusable**: consumed by the /api/earring-ecommerce/prompt endpoint.

Architecture::

    REFERENCE IMAGE
          ↓
    EARRING E-COMMERCE FOUNDATION  ← this module
          ↓
    REFERENCE PRIORITY (auto-appended by ImageGenerationManager)
          ↓
    MARKETPLACE PRESENTATION (auto-appended by ImageGenerationManager)
          ↓
    PROVIDER (OPENAI_IDENTITY_ANCHOR prepended by provider)
          ↓
    IMAGE GENERATION

This prompt is designed to:
1. Preserve exact product identity (source of truth = reference image)
2. Prevent anti-symmetry normalisation (Phase 4D failure mode)
3. Prevent anti-beautification / geometry normalisation
4. Support Hoop, Stud, Dangle earring types
5. Preserve material and colour fidelity
6. Remove photographic distractions (hand, card, backing, packaging)
7. Produce 100% pure solid white background (#FFFFFF) main e-commerce presentation
8. Coexist with existing marketplace layers (Amazon India)
"""

from typing import Optional


# ─── Pure White Background Lock (NON-NEGOTIABLE) ──────────────────────
PURE_WHITE_BACKGROUND_INSTRUCTION = (
    "BACKGROUND SPECIFICATION — 100% PURE SOLID WHITE (#FFFFFF, RGB 255, 255, 255) (NON-NEGOTIABLE):\n"
    "• The background must be completely, uniformly, and seamlessly solid pure white (#FFFFFF, RGB 255, 255, 255).\n"
    "• Amazon India & Global Marketplace Main Listing standard: pure white infinite studio cutout.\n"
    "• ZERO physical tabletop, zero floor surface, zero marble/stone/wood texture, zero podium, zero pedestal, zero acrylic block.\n"
    "• ZERO horizon line, zero room corner, zero wall-to-floor transition.\n"
    "• ZERO dark cast shadows, zero vignette, zero grey edge drop-off, zero ambient gradient.\n"
    "• The earrings must appear cleanly floating or upright in an infinite, pure white void with ultra-crisp edges.\n"
    "• Only minimal, natural contact occlusion lighting underneath the jewelry is permitted; the surrounding canvas must remain 100% pristine solid white."
)


# ─── Earring Type Definitions ─────────────────────────────────────────
EARRING_TYPE_PRESERVATION: dict[str, str] = {
    "Hoop": (
        "EARRING TYPE — HOOP: Preserve the EXACT hoop geometry including "
        "diameter, thickness, opening width, closure mechanism, curvature, "
        "and any decorative elements on the hoop surface. Do NOT change the "
        "hoop into a stud or dangle. Do NOT alter the curvature, thickness, "
        "or diameter. Preserve the closure/clasp mechanism exactly."
    ),
    "Stud": (
        "EARRING TYPE — STUD: Preserve the EXACT front shape, post/attachment "
        "structure, and backing when visible as part of the product. Preserve "
        "stone placement, proportions, decorative details, and geometry. "
        "Do NOT convert the stud into a hoop or dangle. Do NOT add hanging "
        "elements that are not in the reference."
    ),
    "Dangle": (
        "EARRING TYPE — DANGLE/DROP: Preserve the EXACT vertical structure "
        "including top attachment (hook/post/lever-back), connecting elements, "
        "hanging sections, relative lengths of each section, decorative "
        "elements, stones, and asymmetry. Do NOT convert into a hoop or stud. "
        "Do NOT shorten or lengthen any section. Do NOT remove hanging "
        "elements."
    ),
}

GENERIC_EARRING_PRESERVATION = (
    "EARRING TYPE: Preserve the exact earring type as shown in the reference. "
    "Do not convert one earring type into another (e.g. hoop to stud, stud "
    "to dangle, dangle to hoop). Preserve the complete structure including "
    "all attachment mechanisms, hanging elements, and connecting components."
)


# ─── Material / Colour Fidelity ───────────────────────────────────────
MATERIAL_FIDELITY_INSTRUCTION = (
    "MATERIAL & COLOUR FIDELITY (NON-NEGOTIABLE):\n"
    "The reference image is the authoritative source for all material "
    "appearance. Preserve EXACTLY as shown:\n"
    "• Metal colour — do not convert silver to gold, gold to silver, "
    "brass to gold, or any other material substitution.\n"
    "• Metal finish — preserve polished, brushed, matte, hammered, or "
    "any other surface treatment exactly.\n"
    "• Plating appearance — preserve gold plating, silver plating, or "
    "any coating as shown.\n"
    "• Gemstone colour — preserve every stone's exact colour without "
    "oversaturation or artificial brightening.\n"
    "• Pearl appearance — preserve lustre, colour, and surface quality.\n"
    "• Bead appearance — preserve colour, size, and arrangement.\n"
    "• Reflectivity — preserve the natural reflectivity of the metal "
    "and stones.\n"
    "Do NOT oversaturate colours. Do NOT artificially brighten the "
    "jewellery. A white background must NOT be interpreted as white "
    "jewellery. The material in the reference is the truth."
)


# ─── Anti-Symmetry / Anti-Beautification ──────────────────────────────
ANTI_SYMMETRY_INSTRUCTION = (
    "CRITICAL — ANTI-SYMMETRY & ANTI-BEAUTIFICATION RULE (NON-NEGOTIABLE):\n"
    "The reference image's actual visible asymmetry IS part of the product "
    "identity. If the reference shows asymmetry, the output MUST preserve "
    "that asymmetry exactly.\n"
    "\n"
    "DO NOT:\n"
    "• Make both sides identical simply because symmetry looks more "
    "aesthetically pleasing.\n"
    "• Normalise geometry — do not straighten curves, regularise shapes, "
    "or correct perceived manufacturing imperfections.\n"
    "• Beautify the product — do not smooth surfaces, round edges, or "
    "improve proportions beyond what the reference shows.\n"
    "• Correct asymmetry — do not mirror one side to match the other.\n"
    "• Adjust proportions — do not elongate, compress, or resize any "
    "element for visual balance.\n"
    "• Invent details — do not add stones, engravings, filigree, or "
    "decorative elements not visible in the reference.\n"
    "• Remove genuine details — do not remove elements that appear "
    "irregular, imperfect, or asymmetric.\n"
    "• Convert an asymmetric product into a symmetric one.\n"
    "• Treat visible irregularity as an error to be corrected.\n"
    "\n"
    "The product in the reference is the ground truth. Any asymmetry, "
    "irregularity, or imperfection in the reference IS the product. "
    "Reproduce it faithfully."
)


# ─── Input Cleanup ────────────────────────────────────────────────────
INPUT_CLEANUP_INSTRUCTION = (
    "INPUT CLEANUP (NON-NEGOTIABLE):\n"
    "The reference image may contain photographic distractions that must "
    "be removed from the e-commerce output. Remove:\n"
    "• Human hand, fingers, or body parts holding the earring.\n"
    "• Jewellery display card, backing card, or packaging.\n"
    "• Surface, counter, or table the earring is resting on.\n"
    "• Background distractions, unrelated objects, clutter.\n"
    "• Shadows cast on the background by the earring or hand.\n"
    "• Inconsistent lighting artefacts.\n"
    "\n"
    "CRITICAL: When removing a card, backing, or hand, NEVER remove a "
    "component that is actually part of the jewellery product. An earring "
    "hook, post, clasp, chain, connector, lever-back, or decorative "
    "component must NOT be mistaken for removable background material.\n"
    "When in doubt, PRESERVE the component — it may be part of the "
    "jewellery.\n"
    "\n"
    "ANTI-RECONSTRUCTION RULE:\n"
    "If a section of the product is hidden behind a hand, angle, or other "
    "object in the reference, do NOT reconstruct or invent that section. "
    "Preserve only what is visibly present in the reference. Omitted "
    "geometry is preferable to fabricated geometry. Do not hallucinate "
    "product details that are not visible in the source image."
)


# ─── Angle / View Preservation ────────────────────────────────────────
ANGLE_PRESERVATION_INSTRUCTION = (
    "ANGLE & VIEW PRESERVATION:\n"
    "Preserve the meaningful visible orientation of the reference wherever "
    "possible. Do NOT rotate the product to make the composition prettier. "
    "Do NOT convert a front-facing product into an artificial perspective. "
    "Do NOT change the visible geometry by changing the viewing angle. "
    "If the source image is photographed at an angle, preserve the "
    "product's actual structure while cleaning the presentation. "
    "The earring's orientation in the reference is the correct orientation "
    "for the e-commerce output."
)


# ─── E-Commerce Presentation ──────────────────────────────────────────
ECOMMERCE_PRESENTATION_INSTRUCTION = (
    "E-COMMERCE PRESENTATION (PRESENTATION ONLY — not product-identity):\n"
    "Generate a professional, high-end e-commerce main catalog image:\n"
    "• The product is the absolute sole visual focus, set on seamless pure solid white (#FFFFFF).\n"
    "• Product centered appropriately with comfortable listing margins (occupying ~85% of the frame).\n"
    "• Sharp, pristine product details with accurate material and metal rendering.\n"
    "• High-key, bright, balanced commercial studio lighting across the entire piece.\n"
    "• No reflections of photographers, equipment, or unnatural colored lights.\n"
    "• No distracting props, stands, acrylic holders, text, watermarks, or overlays.\n"
    "• No packaging, no jewellery card, no display backing.\n"
    "• Accurate scale — the earrings should appear at realistic size relative to their actual dimensions.\n"
    "• The colour temperature of the output MUST match the reference. Silver metals must stay silver. "
    "Gold metals must stay gold. Do NOT warm or cool the product's natural colour."
)


# ─── Colour Lock (Task 3 validated) ──────────────────────────────────
COLOUR_LOCK_INSTRUCTION = (
    "COLOUR LOCK (NON-NEGOTIABLE — HIGHEST PRIORITY):\n"
    "The reference image is the sole authority for the product's actual "
    "material and colour.\n"
    "Do NOT reinterpret, warm, cool, enhance, beautify, recolour, tint, "
    "tone-shift, or transform the product's metal or stone colours.\n"
    "Silver must remain silver.\n"
    "Gold must remain gold.\n"
    "Gold plating must remain gold plating.\n"
    "Silver plating must remain silver plating.\n"
    "Platinum must remain platinum.\n"
    "Brass must remain brass.\n"
    "925 silver must remain 925 silver.\n"
    "Blue stones must remain blue.\n"
    "Red stones must remain red.\n"
    "Green stones must remain green.\n"
    "Clear stones must remain clear.\n"
    "Do not infer a different material from studio lighting or reflections.\n"
    "Lighting may change illumination ONLY.\n"
    "Lighting must NEVER change the perceived underlying product material "
    "or colour.\n"
    "The colour temperature of the output MUST match the reference. "
    "If the reference shows cool/silver tones, the output must NOT warm "
    "them to gold. If the reference shows warm tones, the output must "
    "NOT cool them to silver."
)


REFERENCE_PRIORITY_MARKER = "REFERENCE IMAGE PRIORITY: MAXIMUM"

ANTI_REDESIGN_INSTRUCTION = (
    "ANTI-REDESIGN RULE (NON-NEGOTIABLE):\n"
    "This is a PRODUCT PHOTOGRAPHY task, NOT a design task.\n"
    "You are photographing the EXACT uploaded product in a professional "
    "pure white background setting. You are NOT designing a new earring, creating an "
    "inspired variation, or improving a product.\n"
    "The generated image must show the EXACT same product — same shape, "
    "same stones, same metal, same proportions, same craftsmanship, "
    "same asymmetry, same imperfections.\n"
    "Only the presentation changes: background to pure solid white #FFFFFF, lighting, composition, "
    "and commercial marketplace quality."
)


# ─── Complete Prompt Builder ───────────────────────────────────────────

def build_earring_ecommerce_prompt_v1(
    earring_type: Optional[str] = None,
) -> str:
    """v1 (live default): the original earring e-commerce main-image prompt.

    Enforces 100% pure white (#FFFFFF) background and 1:1 physical identity preservation.
    Kept byte-for-byte identical to the pre-v2 builder output.
    """
    parts: list[str] = []

    # ── 1. Pure White Background Specification (TOP PRIORITY) ─────────
    parts.append(PURE_WHITE_BACKGROUND_INSTRUCTION)

    # ── 2. 1:1 Visual Preservation Lock ───────────────────────────────
    parts.append(
        "CRITICAL: The earrings in the output MUST be an exact 1:1 physical "
        "replica of the earrings provided in the reference image. Retain "
        "exact stone count, stone shapes (e.g., baguette, marquise, pear, "
        "round), prong setting structure, metal tone, and earring "
        "silhouette. DO NOT alter the core jewelry design or invent "
        "alternate motifs."
    )

    # ── 3. Input Extraction (remove packaging / table / fingers) ──────
    parts.append(
        "Remove all retail packaging, polybags, display cards, plastic film, "
        "human fingers, and tabletop surfaces. Extract the jewelry piece with pristine "
        "studio fidelity directly onto pure solid white (#FFFFFF)."
    )

    # ── 4. Task Header ────────────────────────────────────────────────
    parts.append(
        "TASK: Generate a single e-commerce main image for a Fashion "
        "Jewellery Earring product on a seamless pure solid white (#FFFFFF) background. "
        "The uploaded reference image is the authoritative source of truth for the actual product."
    )

    # ── 5. Reference Priority Marker ──────────────────────────────────
    parts.append(REFERENCE_PRIORITY_MARKER)

    # ── 6. Anti-Redesign ──────────────────────────────────────────────
    parts.append(ANTI_REDESIGN_INSTRUCTION)

    # ── 7. Anti-Symmetry ──────────────────────────────────────────────
    parts.append(ANTI_SYMMETRY_INSTRUCTION)

    # ── 8. Product Identity Preservation ──────────────────────────────
    parts.append(
        "PRODUCT IDENTITY — PRESERVE EXACTLY:\n"
        "• Overall silhouette and outline shape.\n"
        "• Geometry: the exact form (circular, teardrop, geometric, "
        "organic, or any other visible shape).\n"
        "• Proportions: the exact length-to-width ratio, the relationship "
        "between elements, and the relative size of every visible component.\n"
        "• Stone arrangement: preserve every visible stone's position, "
        "spacing, grouping, and spatial relationship to other stones and "
        "to the metal structure.\n"
        "• Stone placement: do not move stones from their visible locations.\n"
        "• Stone characteristics: preserve the visible count, shapes, sizes, "
        "colours, and relative prominence of every stone as shown.\n"
        "• Metal appearance: preserve the exact metal colour, finish, "
        "texture, and surface quality as shown in the reference.\n"
        "• Attachment structure: preserve any visible hooks, posts, "
        "clasps, lever-backs, chains, or other attachment mechanisms "
        "exactly as shown.\n"
        "• Decorative details: preserve all visible filigree, engravings, "
        "cut-outs, milgrain, surface patterns, and ornamental elements.\n"
        "• Surface details: preserve visible texture, polish, brushing, "
        "hammering, or any other surface treatment as shown."
    )

    # ── 9. Earring Type ───────────────────────────────────────────────
    if earring_type and earring_type in EARRING_TYPE_PRESERVATION:
        parts.append(EARRING_TYPE_PRESERVATION[earring_type])
    else:
        parts.append(GENERIC_EARRING_PRESERVATION)

    # ── 10. Material / Colour Fidelity ────────────────────────────────
    parts.append(MATERIAL_FIDELITY_INSTRUCTION)

    # ── 11. Colour Lock ───────────────────────────────────────────────
    parts.append(COLOUR_LOCK_INSTRUCTION)

    # ── 12. Input Cleanup ─────────────────────────────────────────────
    parts.append(INPUT_CLEANUP_INSTRUCTION)

    # ── 13. Angle Preservation ────────────────────────────────────────
    parts.append(ANGLE_PRESERVATION_INSTRUCTION)

    # ── 14. E-Commerce Presentation ───────────────────────────────────
    parts.append(ECOMMERCE_PRESENTATION_INSTRUCTION)

    # ── 15. Final Non-Negotiable Output Rule ──────────────────────────
    parts.append(
        "FINAL OUTPUT RULE:\n"
        "The earring must appear on a 100% pure solid white background (#FFFFFF, RGB 255, 255, 255) ONLY. "
        "No tabletop, no surface gradient, no grey vignette, no floor reflection, no display card, "
        "no human hand. The earring is the ONLY object in the entire image."
    )

    return "\n\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════
# v2 — de-duplicated prompt (opt-in via EARRING_PROMPT_VERSION=v2)
# ═══════════════════════════════════════════════════════════════════════
# Same rules as v1, each stated once, positive phrasing where possible.
# Resolves two v1 contradictions: "realistic size" vs "~85% of the frame",
# and the Amazon reference inside this marketplace-independent module.
# Keeps REFERENCE_PRIORITY_MARKER so ImageGenerationManager still skips
# appending REFERENCE_PRIORITY_BLOCK (no extra tokens, no double block).

V2_TASK = (
    "TASK: Product photography of the exact Fashion Jewellery earring shown "
    "in the reference image, as a single e-commerce main image on a pure "
    "white (#FFFFFF) background. This is photography, not design: only the "
    "background, lighting and framing change."
)

V2_IDENTITY = (
    "PRODUCT IDENTITY — reproduce the reference as an exact 1:1 replica:\n"
    "• Silhouette, geometry and proportions of every part.\n"
    "• Stones: count, cut (e.g. round, pear, marquise, baguette), size, "
    "colour, position, spacing and setting/prongs.\n"
    "• Metal: colour, finish (polished, brushed, matte, hammered) and plating.\n"
    "• Attachments: hooks, posts, clasps, lever-backs, chains and connectors.\n"
    "• Decoration and surface: filigree, engraving, milgrain, cut-outs, texture.\n"
    "Keep any asymmetry and irregularity exactly as shown; they are part of "
    "the product. Do not mirror, straighten, smooth, resize, add or remove "
    "anything."
)

V2_COLOUR = (
    "MATERIAL & COLOUR: the reference is the only authority. Lighting changes "
    "brightness only, never material or colour: silver stays silver, gold "
    "stays gold, stones keep their exact hue without added saturation, and "
    "colour temperature matches the reference. The white background does "
    "not make the jewellery white."
)

V2_CLEANUP = (
    "CLEANUP: remove hands and fingers, display cards and backing, "
    "packaging, polybags and film, tables and surfaces, clutter, and "
    "cast shadows. Never remove a part of the jewellery (hook, post, clasp, "
    "chain, connector, lever-back); when unsure, keep it. Parts hidden in "
    "the reference stay unreconstructed — show only what is visible."
)

V2_ANGLE = (
    "ANGLE: keep the orientation and viewpoint of the reference; do not "
    "rotate or re-pose the earring for a prettier composition."
)

V2_PRESENTATION = (
    "PRESENTATION: seamless pure white (#FFFFFF) with no surface, horizon, "
    "podium, vignette or gradient; at most a faint natural contact shadow "
    "directly beneath the piece. The earring is the only object, centred "
    "and filling most of the frame with even margins, true proportions "
    "between its parts. Bright, even, high-key studio light; sharp detail; "
    "no props, stands, text, watermarks or equipment reflections."
)


def build_earring_ecommerce_prompt_v2(
    earring_type: Optional[str] = None,
) -> str:
    """v2: de-duplicated earring e-commerce prompt (same rules as v1)."""
    type_block = EARRING_TYPE_PRESERVATION.get(earring_type or "", GENERIC_EARRING_PRESERVATION)
    return "\n\n".join([
        V2_TASK,
        REFERENCE_PRIORITY_MARKER,
        V2_IDENTITY,
        type_block,
        V2_COLOUR,
        V2_CLEANUP,
        V2_ANGLE,
        V2_PRESENTATION,
    ])


PROMPT_VERSIONS = {
    "v1": build_earring_ecommerce_prompt_v1,
    "v2": build_earring_ecommerce_prompt_v2,
}


def _active_prompt_version() -> str:
    """EARRING_PROMPT_VERSION from settings; anything unknown/unreadable -> v1."""
    try:
        from app.config import settings

        version = str(getattr(settings, "EARRING_PROMPT_VERSION", "v1") or "v1").strip().lower()
    except Exception:  # noqa: BLE001 — config problems must never change the live prompt
        return "v1"
    return version if version in PROMPT_VERSIONS else "v1"


def build_earring_ecommerce_prompt(
    earring_type: Optional[str] = None,
) -> str:
    """Build the earring e-commerce main-image prompt used by every caller.

    Returns v1 (unchanged live prompt) unless EARRING_PROMPT_VERSION=v2.
    """
    return PROMPT_VERSIONS[_active_prompt_version()](earring_type=earring_type)
