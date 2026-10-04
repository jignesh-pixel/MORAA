"""
Earring Stand Shot Prompt Builder
Module: backend/app/services/earring_on_stand_shot.py

Generates a dedicated commercial jewelry studio display prompt featuring earrings 
suspended from a clean jewelry stand (T-bar / display mount).

Architecture:
1. TASK & IDENTITY ANCHOR
2. REFERENCE PRIORITY MARKER
3. CORE PRODUCT FIDELITY & ANTI-SYMMETRY
4. EARRING TYPE SPECIFICATION (Hoop, Stud, Dangle, or Generic)
5. MATERIAL FIDELITY & AFFIRMATIVE METAL LOCK
6. STAND & BASE SPECIFICATIONS (T-bar, Ladder, Single Post)
7. MOUNTING & GRAVITY RULES (Hook/Post/Closure mechanics)
8. SINGLE VS PAIR PLACEMENT & SYMMETRY LOGIC
9. CAMERA & FRAMING GUIDANCE (100mm macro aesthetic, f/5.6)
10. LIGHTING, SPECULAR CONTROL & RIM SEPARATION
11. SEAMLESS STUDIO BACKDROP
12. NEGATIVE SPACE & BEAD CLUSTER PRESERVATION
13. STAND-SPECIFIC CRITICAL NEGATIVES
"""

from typing import Optional

# Imports from existing core shared prompt libraries. Imported directly (no fallback copy): a missing shared block
# must fail loudly at import, never silently change the prompt a paying customer's pack is generated from.
from app.services.earring_ecommerce_prompt import (
    REFERENCE_PRIORITY_MARKER,
    ANTI_SYMMETRY_INSTRUCTION,
    COLOUR_LOCK_INSTRUCTION,
    MATERIAL_FIDELITY_INSTRUCTION,
    EARRING_TYPE_PRESERVATION,
    GENERIC_EARRING_PRESERVATION,
)
from app.services.earring_scale_reference_prompt import (
    NEGATIVE_SPACE_INSTRUCTION,
    BEAD_CLUSTER_PRESERVATION_INSTRUCTION,
)
from app.services.earring_macro_shot_prompt import METAL_AFFIRMATION_INSTRUCTION

# --- Configuration Enums & Mappings ---

VALID_STAND_STYLES = ("t_bar", "ladder_bar", "single_post_arm")
VALID_BASE_FINISHES = ("leatherette", "velvet", "matte_acrylic")
VALID_STAND_TONES = ("auto", "black", "ivory", "taupe")
VALID_BACKDROPS = ("soft_grey", "warm_beige", "charcoal")

STAND_INSTRUCTIONS = {
    "t_bar": (
        "DISPLAY STAND SETUP (T-BAR):\n"
        "- A sleek, professional vertical jewelry T-bar stand featuring a single horizontal crossbar.\n"
        "- The crossbar contains clean, discreet pre-drilled hanging perforations.\n"
        "- The stand upright and bar feature an ultra-matte, non-reflective finish.\n"
        "- The stand remains secondary in frame, occupying no more than 20-25% visual weight."
    ),
    "ladder_bar": (
        "DISPLAY STAND SETUP (LADDER BAR):\n"
        "- A dual-tier horizontal jewelry bar stand with clean parallel crossbars.\n"
        "- Minimalist, non-reflective matte finish.\n"
        "- The structure serves strictly as a functional support, occupying secondary visual weight."
    ),
    "single_post_arm": (
        "DISPLAY STAND SETUP (SINGLE POST ARM):\n"
        "- A single cantilevered L-arm minimalist display stand designed for showcasing an individual piece.\n"
        "- Ultra-matte, non-distracting finish with a slender profile."
    ),
}

BASE_INSTRUCTIONS = {
    "leatherette": (
        "STAND BASE SPECIFICATION:\n"
        "- Grounded by a solid, weighted circular or rectangular low-profile base wrapped in fine-grain matte leatherette.\n"
        "- Premium matte finish that produces zero mirror reflections of the jewelry piece."
    ),
    "velvet": (
        "STAND BASE SPECIFICATION:\n"
        "- Weighted, low-profile base covered in smooth, ultra-short-pile matte velvet.\n"
        "- Deep matte surface absorbing excess light with soft, subtle ambient occlusion shadows."
    ),
    "matte_acrylic": (
        "STAND BASE SPECIFICATION:\n"
        "- Weighted solid matte frosted acrylic base.\n"
        "- Completely non-specular, soft diffuse surface."
    ),
}

STAND_TONE_INSTRUCTIONS = {
    "auto": (
        "STAND TONE SELECTION (CONTRAST ADAPTIVE):\n"
        "- The stand and crossbar must render in a neutral tone that visually contrasts with the earring metal in the reference image.\n"
        "- If the earrings are silver, white gold, or platinum: use a refined dark charcoal/black matte stand.\n"
        "- If the earrings are yellow gold, rose gold, or brass: use an elegant ivory or soft taupe matte stand."
    ),
    "black": "STAND TONE: Pure neutral matte black / dark charcoal.",
    "ivory": "STAND TONE: Soft neutral matte ivory / off-white cream.",
    "taupe": "STAND TONE: Neutral muted matte taupe / warm stone grey.",
}

BACKDROP_INSTRUCTIONS = {
    "soft_grey": (
        "STUDIO BACKDROP:\n"
        "- Seamless neutral soft grey studio sweep with gentle, gradual vignetting.\n"
        "- Positioned well behind the stand with smooth optical falloff; completely devoid of props or patterns."
    ),
    "warm_beige": (
        "STUDIO BACKDROP:\n"
        "- Seamless warm beige / champagne studio backdrop with delicate tonal gradients.\n"
        "- Smooth out-of-focus background maintaining clean commercial separation."
    ),
    "charcoal": (
        "STUDIO BACKDROP:\n"
        "- Seamless deep charcoal studio sweep providing high-contrast editorial separation.\n"
        "- Soft tonal gradient falloff behind the stand."
    ),
}

MOUNTING_RULES = {
    "Dangle": (
        "MOUNTING & ATTACHMENT PHYSICS (DANGLE / DROP):\n"
        "- The ear-wire or fish hook passes cleanly through the designated mounting hole on the crossbar.\n"
        "- Only the front curved contour of the hook rests visibly above the bar; do NOT invent or draw fictional backing closures behind the bar.\n"
        "- The earring body hangs naturally plumb straight downward under true gravity without unnatural tilting."
    ),
    "Stud": (
        "MOUNTING & ATTACHMENT PHYSICS (STUD):\n"
        "- The stud post passes directly through the crossbar mounting hole.\n"
        "- The front decorative face sits flush, vertical, and upright against the front plane of the bar.\n"
        "- The post closure remains hidden behind the bar."
    ),
    "Hoop": (
        "MOUNTING & ATTACHMENT PHYSICS (HOOP):\n"
        "- The hoop hangs suspended over the crossbar at its top apex closure.\n"
        "- The curvature of the hoop remains perfectly round and structurally undeformed by contact with the bar."
    ),
}

GENERIC_MOUNTING_RULE = (
    "MOUNTING & ATTACHMENT PHYSICS:\n"
    "- Mount the earring naturally using its visible closure mechanism through or over the crossbar.\n"
    "- The piece must hang strictly according to gravity without distorting the attachment geometry.\n"
    "- Never invent imaginary mechanical attachments."
)

PAIR_AND_SINGLE_PLACEMENT_RULES = (
    "DISPLAY PLACEMENT & SYMMETRY LOGIC (NON-NEGOTIABLE):\n"
    "- IF THE REFERENCE CONTAINS A PAIR:\n"
    "  * Hang both earrings symmetrically on the crossbar, equidistant from the center upright.\n"
    "  * Both earrings must hang at the EXACT SAME horizontal height alignment.\n"
    "  * Both earrings face directly forward towards the camera.\n"
    "  * CRITICAL: 'Symmetry' applies ONLY to stand placement and spacing. DO NOT symmetrize or normalize the intrinsic handcrafted geometry of each earring. Preserve individual artisanal details from the reference.\n"
    "- IF THE REFERENCE CONTAINS ONLY ONE EARRING:\n"
    "  * Hang the single earring centered on the crossbar.\n"
    "  * NEVER invent or duplicate a second earring. Do not convert a single piece into a pair."
)

CAMERA_AND_COMPOSITION = (
    "CAMERA, OPTICS & FRAMING:\n"
    "- Viewpoint: Direct eye-level perspective to subtle 10-degree elevated hero angle.\n"
    "- Optics: 100mm macro telephoto aesthetic at approximately f/5.6 aperture.\n"
    "- Framing: The earring(s) are the hero subject, occupying 60% to 70% of the vertical frame height.\n"
    "- Focus & Sharpness: Front plane completely sharp across all jewelry facets, stones, and metal edges; backdrop falls into a creamy, subtle blur.\n"
    "- Disclaimer: Visual photographic guidance, not absolute camera parameters."
)

LIGHTING_AND_REFLECTIONS = (
    "STUDIO LIGHTING & SPECULAR CONTROL:\n"
    "- Key Light: Large diffuse softbox positioned front-above creating smooth, elegant specular gradients across metal contours.\n"
    "- Edge Separation: Subtle, clean rim lighting highlighting the silhouette and separating metal edges from the backdrop.\n"
    "- Reflection Flags: Diffusion flags prevent harsh reflections, room mirrors, camera rigs, or stand colors on polished metal.\n"
    "- Gemstone Brilliance: Crisp, natural stone dispersion and facet clarity without artificial glowing, blown highlights, or fantasy flares.\n"
    "- Chromatic Integrity: The stand material, base, and background colors must NEVER cast tint or color contamination onto the product."
)

STAND_SHOT_NEGATIVES = (
    "CRITICAL NEGATIVE CONSTRAINTS (STAND DISPLAY SPECIFIC):\n"
    "- Do NOT merge, weld, or fuse the earring metal into the stand material or crossbar.\n"
    "- Do NOT render tilted, diagonal, or floating gravity-defying drops.\n"
    "- Do NOT hang earrings at mismatched vertical heights or uneven spacing.\n"
    "- Do NOT generate pure #FFFFFF cutout backgrounds, outdoor scenes, flat-lays, or on-model ear shots.\n"
    "- Do NOT generate human bodies, ears, hands, mannequin heads, or neck busts.\n"
    "- Do NOT add secondary lifestyle props, pedestals, ring boxes, flower petals, or draped fabrics.\n"
    "- Do NOT render brand marks, logos, etched serial numbers, or watermarks on the stand or background.\n"
    "- Zero CGI plastic gloss; maintain hyper-realistic macro jewelry physics."
)


def build_stand_shot_prompt(
    earring_type: Optional[str] = None,
    stand_style: str = "t_bar",
    base_finish: str = "leatherette",
    stand_tone: str = "auto",
    backdrop: str = "soft_grey",
) -> str:
    """
    Builds the standalone prompt for the Earring on Stand commercial studio shot.
    Zero-argument call returns production-ready defaults.
    """
    # Enforce safe fallbacks for unexpected inputs
    if stand_style not in VALID_STAND_STYLES:
        stand_style = "t_bar"
    if base_finish not in VALID_BASE_FINISHES:
        base_finish = "leatherette"
    if stand_tone not in VALID_STAND_TONES:
        stand_tone = "auto"
    if backdrop not in VALID_BACKDROPS:
        backdrop = "soft_grey"

    parts: list[str] = []

    # 1. Task & Scope
    parts.append(
        "TASK: Earring on Stand Shot.\n"
        "A clean, high-end commercial jewelry studio display photograph featuring the EXACT "
        "earring(s) from the reference image suspended from a professional jewelry stand.\n"
        "This is dedicated product photography on a display mount. It is NOT a white-background cutout, "
        "NOT on-model, and NOT a flat-lay."
    )

    # 2. Reference Priority Marker
    parts.append(REFERENCE_PRIORITY_MARKER)

    # 3. Core Fidelity & Anti-Symmetry
    parts.append(
        "PRODUCT FIDELITY CORE:\n"
        "Preserve 100% of the original earring geometry, stone shapes, prong placements, surface textures, "
        "and proportions depicted in the reference image."
    )
    parts.append(ANTI_SYMMETRY_INSTRUCTION)

    # 4. Earring Type Specification
    parts.append(
        EARRING_TYPE_PRESERVATION.get(earring_type, GENERIC_EARRING_PRESERVATION)
    )

    # 5. Material Fidelity & Color Lock
    parts.append(COLOUR_LOCK_INSTRUCTION)
    parts.append(MATERIAL_FIDELITY_INSTRUCTION)
    parts.append(METAL_AFFIRMATION_INSTRUCTION)

    # 6. Stand & Base Specs
    parts.append(STAND_INSTRUCTIONS[stand_style])
    parts.append(BASE_INSTRUCTIONS[base_finish])
    parts.append(STAND_TONE_INSTRUCTIONS[stand_tone])

    # 7. Mounting Physics
    mounting_block = MOUNTING_RULES.get(earring_type, GENERIC_MOUNTING_RULE)
    parts.append(mounting_block)

    # 8. Single vs Pair Placement
    parts.append(PAIR_AND_SINGLE_PLACEMENT_RULES)

    # 9. Camera & Optics
    parts.append(CAMERA_AND_COMPOSITION)

    # 10. Lighting & Highlights
    parts.append(LIGHTING_AND_REFLECTIONS)

    # 11. Backdrop
    parts.append(BACKDROP_INSTRUCTIONS[backdrop])

    # 12. Negative Space & Bead Protection
    parts.append(NEGATIVE_SPACE_INSTRUCTION)
    parts.append(BEAD_CLUSTER_PRESERVATION_INSTRUCTION)

    # 13. Critical Negatives
    parts.append(STAND_SHOT_NEGATIVES)

    return "\n\n".join(parts)