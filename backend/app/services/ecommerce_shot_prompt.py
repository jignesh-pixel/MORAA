"""Ecommerce Shot (₹50) prompt — one catalogue image of the customer's earring.

Used ONLY by process_whatsapp_white_bg. The customer's uploaded photo is sent
to the image model together with this prompt (Gemini: identity lock ->
reference image -> this prompt; OpenAI fallback: image edit on the same
photo), so the output is the SAME earring re-photographed for a marketplace
listing (Amazon / Myntra / website main image):

    study the reference -> isolate the earring(s) -> pure white #FFFFFF
    background, studio softbox lighting, soft natural drop shadow.

Deliberately separate from Prompt 1 (earring_ecommerce_prompt.py, frozen and
used by E-Com Pack 1), which forbids cast shadows; this product asks for a
soft drop shadow. The study step lives inside the generation prompt, so no
extra AI call is made.
"""

from app.ai.product_fidelity import (
    PRODUCT_PRESERVATION_CONSTRAINTS,
    REFERENCE_PRIORITY_BLOCK,
)

ECOMMERCE_SHOT_TASK = (
    "TASK: Create ONE professional e-commerce catalogue photograph of the EXACT "
    "earring(s) in the attached customer photo, ready to be the main image of an "
    "Amazon / Myntra / website product listing.\n"
    "The attached photo is a raw phone picture. Re-photograph that same piece in a "
    "clean studio — this is product photography, not design."
)

ECOMMERCE_SHOT_STUDY = (
    "STEP 1 — STUDY THE REFERENCE FIRST (do not write any text in the image):\n"
    "• Earring type (stud, drop, dangle, jhumka, hoop, chandbali, ear cuff …) and "
    "whether the photo shows ONE earring or a PAIR — keep exactly that number.\n"
    "• Every stone: count, cut/shape, size, colour and exact position.\n"
    "• Metal: colour (yellow gold, rose gold, silver, oxidised, etc.), finish and texture.\n"
    "• Pearls, beads, chains, hanging drops and their counts; hooks, posts, screw "
    "backs or clasps.\n"
    "• Engravings, filigree, meenakari/enamel colours, cut-outs and surface detail."
)

ECOMMERCE_SHOT_ISOLATE = (
    "STEP 2 — ISOLATE THE JEWELLERY:\n"
    "Remove everything that is not the earring: the original background, hands and "
    "fingers, ears, display cards, packaging, tags, tables, fabric and clutter. "
    "Never remove or trim any part of the earring itself (hooks, posts, chains, "
    "drops are part of the product)."
)

ECOMMERCE_SHOT_STAGING = (
    "STEP 3 — STAGE IT FOR THE CATALOGUE:\n"
    "• Background: seamless pure white #FFFFFF (RGB 255,255,255) edge to edge — no "
    "grey cast, gradient, vignette, horizon, texture or props.\n"
    "• Placement: the earring(s) centred, upright and front-facing as worn, filling "
    "about 80–85% of the frame with even margins; a pair sits side by side at the "
    "same height and scale.\n"
    "• Lighting: bright professional studio softbox lighting (key + fill), even and "
    "neutral white balance; crisp specular highlights on metal and natural sparkle "
    "in stones without blown-out areas.\n"
    "• Shadow: a soft, natural, light-grey drop shadow directly beneath the "
    "jewellery, as if resting just above a white sweep — subtle and diffused, never "
    "harsh or dark. Everywhere else the background stays pure white.\n"
    "• Focus: tack-sharp from front to back, high resolution, true-to-life colours.\n"
    "• Nothing else in the image: no model, no ear, no hand, no mannequin, no stand, "
    "no box, no text, no logo, no watermark, no border."
)

ECOMMERCE_SHOT_OUTPUT = (
    "OUTPUT RULE: exactly ONE photorealistic image in which the customer's earring "
    "is clearly visible and is the only subject, on pure white with a soft drop "
    "shadow. A blank or empty white image, or a different/redesigned earring, is a "
    "failed result."
)


def build_ecommerce_shot_prompt() -> str:
    """Return the full Ecommerce Shot prompt (reference-image driven)."""
    return "\n\n".join(
        [
            ECOMMERCE_SHOT_TASK,
            REFERENCE_PRIORITY_BLOCK,
            ECOMMERCE_SHOT_STUDY,
            ECOMMERCE_SHOT_ISOLATE,
            ECOMMERCE_SHOT_STAGING,
            PRODUCT_PRESERVATION_CONSTRAINTS,
            ECOMMERCE_SHOT_OUTPUT,
        ]
    )
