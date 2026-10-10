"""
Earring Luxury Drape Shot Prompt Builder
Module: backend/app/services/earring_luxury_drape_shot.py

Generates a dedicated luxury product photograph of the earring(s) resting on a sculpted fabric drape (silk, velvet
or satin). The fabric colour and finish adapt to the earring: champagne silk for silver-toned pieces, emerald or
burgundy velvet for gold, blush-mauve satin for rose gold. When the metal tone is not supplied the prompt asks the
image model to choose from the reference image itself (the same "auto" pattern as the Stand Display tone).

Architecture:
1. TASK & IDENTITY ANCHOR
2. REFERENCE PRIORITY MARKER
3. CORE PRODUCT FIDELITY & ANTI-SYMMETRY
4. EARRING TYPE SPECIFICATION (Hoop, Stud, Dangle, or Generic)
5. MATERIAL FIDELITY & AFFIRMATIVE METAL LOCK
6. DRAPE FABRIC & COLOUR (adaptive to metal tone / primary gemstone)
7. FABRIC SCULPTING & JEWELLERY RESTING RULES (gravity, contact, no wrapping)
8. SINGLE VS PAIR PLACEMENT
9. CAMERA & FRAMING GUIDANCE (100mm macro aesthetic, f/4)
10. LIGHTING, SPECULAR CONTROL & COLOUR-SPILL CONTROL
11. NEGATIVE SPACE & BEAD CLUSTER PRESERVATION
12. DRAPE-SPECIFIC CRITICAL NEGATIVES
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

VALID_METAL_TONES = ("silver", "gold", "rose_gold")
VALID_STONE_FAMILIES = ("clear", "green", "red", "blue", "pearl", "other")
VALID_DRAPES = ("auto", "champagne_silk", "emerald_velvet", "burgundy_velvet", "blush_satin")

# Free-text metal / stone descriptions (as an analysis step or API caller might supply them) -> canonical value.
# Matching is by whole word(s) (plural "s" allowed) on a lower-cased, underscore-separated string, so "coloured" never
# matches "red"; the first matching row wins, so the more specific names ("rose_gold", "white_gold") are listed before
# the generic ones ("gold").
_METAL_TONE_KEYWORDS = (
    ("rose_gold", "rose_gold"),
    ("pink_gold", "rose_gold"),
    ("copper", "rose_gold"),
    ("white_gold", "silver"),
    ("platinum", "silver"),
    ("silver", "silver"),
    ("rhodium", "silver"),
    ("steel", "silver"),
    ("yellow_gold", "gold"),
    ("gold", "gold"),
    ("brass", "gold"),
    ("bronze", "gold"),
)
_STONE_FAMILY_KEYWORDS = (
    ("diamond", "clear"),
    ("zirconia", "clear"),
    ("zircon", "clear"),
    ("cz", "clear"),
    ("moissanite", "clear"),
    ("crystal", "clear"),
    ("clear", "clear"),
    ("white", "clear"),
    ("emerald", "green"),
    ("peridot", "green"),
    ("jade", "green"),
    ("green", "green"),
    ("ruby", "red"),
    ("garnet", "red"),
    ("red", "red"),
    ("sapphire", "blue"),
    ("topaz", "blue"),
    ("aquamarine", "blue"),
    ("blue", "blue"),
    ("pearl", "pearl"),
)

DRAPE_INSTRUCTIONS = {
    "champagne_silk": (
        "LUXURY DRAPE FABRIC (CHAMPAGNE OYSTER SILK):\n"
        "- A generous length of soft champagne / oyster-toned silk charmeuse, gathered into gentle, flowing folds.\n"
        "- The fabric has a refined low sheen with smooth, continuous highlights along the fold crests.\n"
        "- The tone stays pale, warm-neutral and desaturated so it complements silver, white gold and platinum "
        "without competing with clear or white gemstones.\n"
        "- The silk is the elegant stage for the piece; it remains secondary to the jewellery."
    ),
    "emerald_velvet": (
        "LUXURY DRAPE FABRIC (DEEP EMERALD VELVET):\n"
        "- A rich length of deep emerald-green cotton velvet with a dense, even pile, gathered into soft sculpted folds.\n"
        "- The surface is matte with a subtle pile sheen on the fold crests; it absorbs light rather than reflecting it.\n"
        "- The green is dark and slightly desaturated so warm gold metal stands out with strong, flattering contrast.\n"
        "- The velvet is the opulent stage for the piece; it remains secondary to the jewellery."
    ),
    "burgundy_velvet": (
        "LUXURY DRAPE FABRIC (DEEP BURGUNDY VELVET):\n"
        "- A rich length of deep burgundy / wine-red cotton velvet with a dense, even pile, gathered into soft "
        "sculpted folds.\n"
        "- The surface is matte with a subtle pile sheen on the fold crests; it absorbs light rather than reflecting it.\n"
        "- The burgundy is dark and slightly desaturated so warm gold metal stands out with strong, flattering "
        "contrast, and so green gemstones are not visually echoed by the fabric.\n"
        "- The velvet is the opulent stage for the piece; it remains secondary to the jewellery."
    ),
    "blush_satin": (
        "LUXURY DRAPE FABRIC (BLUSH-MAUVE SATIN):\n"
        "- A flowing length of soft blush-mauve duchess satin, gathered into gentle, rounded folds.\n"
        "- The fabric has a soft, controlled satin sheen with smooth tonal gradients along each fold.\n"
        "- The tone stays muted and dusty (never bright pink) so rose-gold metal reads clearly against it.\n"
        "- The satin is the elegant stage for the piece; it remains secondary to the jewellery."
    ),
    "auto": (
        "LUXURY DRAPE FABRIC (ADAPTIVE TO THE EARRING):\n"
        "- Choose ONE luxury drape fabric by looking at the metal and stones in the reference image.\n"
        "- If the earrings are silver, white gold, platinum or set with clear / white stones: use soft champagne "
        "oyster silk.\n"
        "- If the earrings are yellow gold or brass: use deep emerald-green velvet (use deep burgundy velvet instead "
        "if the main stones are green).\n"
        "- If the earrings are rose gold or copper-toned: use dusty blush-mauve satin.\n"
        "- If the metal tone is mixed or unclear: use soft champagne silk.\n"
        "- The fabric is a muted, desaturated, elegant stage that contrasts with the metal and remains secondary to "
        "the jewellery."
    ),
}

RESTING_RULES = {
    "Dangle": (
        "RESTING & ATTACHMENT PHYSICS (DANGLE / DROP):\n"
        "- The earring rests on top of the fabric with its hook / ear-wire clearly visible and unchanged.\n"
        "- The drop section lies along or rests against a fold, following the slope of the fabric under true gravity.\n"
        "- Do NOT suspend the earring in mid-air and do NOT hang it from the fabric."
    ),
    "Stud": (
        "RESTING & ATTACHMENT PHYSICS (STUD):\n"
        "- The stud rests on its back or leans against a fold with its decorative face turned towards the camera.\n"
        "- The post and its visible geometry stay unchanged; never invent a backing, cushion or display card."
    ),
    "Hoop": (
        "RESTING & ATTACHMENT PHYSICS (HOOP):\n"
        "- The hoop rests on the fabric, lying on a fold or leaning against one, with its round shape undeformed.\n"
        "- The hoop closure stays visible and unchanged; the fabric never passes through the hoop."
    ),
}

GENERIC_RESTING_RULE = (
    "RESTING & ATTACHMENT PHYSICS:\n"
    "- The earring rests naturally on top of the fabric, supported by a fold according to its real shape and weight.\n"
    "- Its visible closure (hook, post or clasp) stays visible and unchanged.\n"
    "- Never suspend it in mid-air and never invent imaginary attachments."
)

FABRIC_SCULPTING_RULES = (
    "FABRIC SCULPTING & CONTACT (NON-NEGOTIABLE):\n"
    "- The fabric is arranged in a few deliberate, graceful folds with one calm crest or valley that guides the eye to "
    "the earring(s); the fabric fills the whole frame behind and around the piece.\n"
    "- The earring rests ON TOP of the fabric. No fabric folds over, tucks under, wraps around, or covers any part of "
    "the earring, its stones, its hook or its post.\n"
    "- Realistic soft contact shadows and slight fabric compression where the piece meets the cloth; the piece looks "
    "physically present and weighted, never pasted, cut out or floating.\n"
    "- Fabric texture (weave, pile or sheen) is rendered with believable detail, without any fraying, loose threads "
    "or snags near the jewellery."
)

PAIR_AND_SINGLE_PLACEMENT_RULES = (
    "DISPLAY PLACEMENT LOGIC (NON-NEGOTIABLE):\n"
    "- IF THE REFERENCE CONTAINS A PAIR:\n"
    "  * Rest both earrings close together on the drape, each clearly and fully visible, in a relaxed natural "
    "arrangement (for example one slightly angled to the other).\n"
    "  * CRITICAL: Do not symmetrize or normalize the intrinsic handcrafted geometry of either earring. Preserve "
    "the individual details of each piece from the reference.\n"
    "- IF THE REFERENCE CONTAINS ONLY ONE EARRING:\n"
    "  * Rest the single earring on the drape near the compositional centre.\n"
    "  * NEVER invent or duplicate a second earring. Do not convert a single piece into a pair."
)

CAMERA_AND_COMPOSITION = (
    "CAMERA, OPTICS & FRAMING:\n"
    "- Viewpoint: Elevated three-quarter angle of roughly 30 to 45 degrees looking across the folds.\n"
    "- Optics: 100mm macro telephoto aesthetic at approximately f/4 aperture.\n"
    "- Framing: The earring(s) are the hero subject, filling roughly 50% to 60% of the frame, with the draped fabric "
    "providing the surrounding context. Preferred aspect ratio: 4:5.\n"
    "- Focus & Sharpness: The jewellery is completely sharp across all facets, stones and metal edges; the folds "
    "beyond fall into a creamy, soft blur.\n"
    "- Disclaimer: Visual photographic guidance, not absolute camera parameters."
)

LIGHTING_AND_REFLECTIONS = (
    "STUDIO LIGHTING, SPECULAR & COLOUR-SPILL CONTROL:\n"
    "- Key Light: Large diffuse softbox from the side and slightly above, grazing across the fabric so the folds read "
    "with smooth tonal modelling and gentle shadow gradients.\n"
    "- Edge Separation: Subtle clean highlights along the metal edges separate the jewellery from the cloth.\n"
    "- Reflection Control: Diffusion flags prevent harsh hotspots, room mirrors or camera rigs showing on polished "
    "metal.\n"
    "- Gemstone Brilliance: Crisp, natural stone dispersion and facet clarity without artificial glowing, blown "
    "highlights or fantasy flares.\n"
    "- Chromatic Integrity: The fabric colour must NEVER tint, stain or colour-shift the metal or the stones. Any "
    "reflection of the fabric on the metal stays faint and subtle, and the metal keeps its exact reference colour."
)

DRAPE_SHOT_NEGATIVES = (
    "CRITICAL NEGATIVE CONSTRAINTS (LUXURY DRAPE SPECIFIC):\n"
    "- Do NOT cover, wrap, bury or partly hide the earring with fabric, and do NOT tie the cloth around it.\n"
    "- Do NOT place the earring on stone, marble, travertine, plaster, slabs, podiums, wood or glass; the surface is "
    "the drape.\n"
    "- Do NOT render the earring floating, hovering, hanging in mid-air or defying gravity.\n"
    "- Do NOT generate pure #FFFFFF cutout backgrounds, flat-lays on plain paper, outdoor scenes or on-model ear "
    "shots.\n"
    "- Do NOT generate human bodies, ears, hands, fingers, mannequin heads or neck busts.\n"
    "- Do NOT add props such as flowers, petals, perfume bottles, ring boxes, pearls, other jewellery, candles or "
    "ribbons.\n"
    "- Do NOT render brand marks, logos, text, labels or watermarks on the fabric or anywhere in the frame.\n"
    "- Do NOT use bright, neon or heavily patterned fabric; no prints, embroidery, sequins or lace.\n"
    "- Zero CGI plastic gloss on the metal or the fabric; maintain hyper-realistic macro jewellery physics."
)


def _normalise_key(value: Optional[str]) -> str:
    """Lower-cased, underscore-separated form of a free-text value ("Rose Gold" -> "rose_gold"); "" for non-text."""
    if not isinstance(value, str):
        return ""
    return "_".join(value.strip().lower().replace("-", " ").replace("_", " ").split())


def _contains_word(padded_key: str, keyword: str) -> bool:
    """True when ``keyword`` (underscore-joined words) appears in ``padded_key`` ("_a_b_") as whole words."""
    return f"_{keyword}_" in padded_key or f"_{keyword}s_" in padded_key


def normalise_metal_tone(value: Optional[str]) -> Optional[str]:
    """Canonical metal tone ("silver", "gold" or "rose_gold") for a free-text metal description, else None."""
    key = _normalise_key(value)
    if not key:
        return None
    padded = f"_{key}_"
    for keyword, tone in _METAL_TONE_KEYWORDS:
        if _contains_word(padded, keyword):
            return tone
    return None


def normalise_stone_family(value: Optional[str]) -> Optional[str]:
    """Canonical stone family ("clear", "green", "red", "blue", "pearl") for a free-text gemstone description,
    "other" for a recognised-as-present but unlisted stone, None when no gemstone is given."""
    key = _normalise_key(value)
    if not key:
        return None
    padded = f"_{key}_"
    for keyword, family in _STONE_FAMILY_KEYWORDS:
        if _contains_word(padded, keyword):
            return family
    return "other"


def select_drape(metal_tone: Optional[str] = None, gemstone: Optional[str] = None) -> str:
    """The drape key (one of VALID_DRAPES) for an earring's metal tone and primary gemstone.

    Silver-toned metal -> champagne silk; gold -> emerald velvet (burgundy when the main stones are green, so the
    fabric does not echo them); rose gold -> blush satin. An unknown or missing metal tone returns "auto", which
    makes the prompt choose from the reference image.
    """
    tone = normalise_metal_tone(metal_tone)
    if tone == "silver":
        return "champagne_silk"
    if tone == "gold":
        return "burgundy_velvet" if normalise_stone_family(gemstone) == "green" else "emerald_velvet"
    if tone == "rose_gold":
        return "blush_satin"
    return "auto"


def build_luxury_drape_prompt(
    earring_type: Optional[str] = None,
    metal_tone: Optional[str] = None,
    gemstone: Optional[str] = None,
) -> str:
    """
    Builds the standalone prompt for the Earring on Luxury Drape commercial shot.
    Zero-argument call returns production-ready defaults (the drape is chosen from the reference image).

    Args:
        earring_type: Optional "Hoop", "Stud" or "Dangle"; anything else uses the generic rules.
        metal_tone: Optional free-text metal description from earring analysis ("silver", "yellow gold",
            "rose gold", ...). Unknown values fall back to the adaptive "auto" drape.
        gemstone: Optional free-text primary gemstone ("emerald", "diamond", ...), used to avoid a drape that
            echoes the main stone colour.
    """
    drape = select_drape(metal_tone, gemstone)

    parts: list[str] = []

    # 1. Task & Scope
    parts.append(
        "TASK: Earring on Luxury Drape Shot.\n"
        "A refined, high-end luxury jewellery photograph featuring the EXACT earring(s) from the reference image "
        "resting on a sculpted drape of fine fabric.\n"
        "This is dedicated product photography on draped cloth. It is NOT a white-background cutout, NOT on-model, "
        "NOT on a stone slab and NOT a display-stand shot."
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

    # 6. Drape Fabric & Colour
    parts.append(DRAPE_INSTRUCTIONS[drape])

    # 7. Fabric Sculpting & Resting Rules
    parts.append(FABRIC_SCULPTING_RULES)
    parts.append(RESTING_RULES.get(earring_type, GENERIC_RESTING_RULE))

    # 8. Single vs Pair Placement
    parts.append(PAIR_AND_SINGLE_PLACEMENT_RULES)

    # 9. Camera & Optics
    parts.append(CAMERA_AND_COMPOSITION)

    # 10. Lighting & Highlights
    parts.append(LIGHTING_AND_REFLECTIONS)

    # 11. Negative Space & Bead Protection
    parts.append(NEGATIVE_SPACE_INSTRUCTION)
    parts.append(BEAD_CLUSTER_PRESERVATION_INSTRUCTION)

    # 12. Critical Negatives
    parts.append(DRAPE_SHOT_NEGATIVES)

    return "\n\n".join(parts)
