"""API routes for the Earring on Stand Display shot — Prompt 8 (E-Com Pack 1, slot 7).

Provides a single endpoint that builds the complete earring-on-stand studio
display prompt with selectable stand style, base finish, stand tone and
backdrop. The frontend calls this endpoint, receives the prompt, and sends it
to /api/generate-image with the reference image.

The WhatsApp Catalog Pack calls the same builder with no arguments (the
production defaults below); this route only exposes the options for the
web prompt studio and for offline review.

Flow:
    POST /api/earring-stand-shot/prompt  →  complete prompt string
        ↓
    Frontend sends prompt + reference_image to /api/generate-image
        ↓
    ImageGenerationManager keeps the prompt's own reference-priority marker
        + marketplace rules → provider → generated image
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.services.earring_on_stand_shot import (
    VALID_BACKDROPS,
    VALID_BASE_FINISHES,
    VALID_STAND_STYLES,
    VALID_STAND_TONES,
    build_stand_shot_prompt,
)
from app.utils.logger import logger

router = APIRouter(prefix="/api", tags=["Earring Stand Shot"])

VALID_EARRING_TYPES = ("Hoop", "Stud", "Dangle")


class StandShotPromptRequest(BaseModel):
    """Request body for the earring-on-stand display prompt."""

    earring_type: Optional[str] = Field(
        None,
        description=(
            'Earring type: "Hoop", "Stud", or "Dangle". '
            "When provided, type-specific preservation and mounting rules are included. "
            "When None, the generic rules are used (as in the WhatsApp Catalog Pack)."
        ),
    )
    stand_style: str = Field(
        "t_bar",
        description="Display stand. Must be one of: " + ", ".join(VALID_STAND_STYLES) + ". Defaults to 't_bar'.",
    )
    base_finish: str = Field(
        "leatherette",
        description="Stand base finish. Must be one of: " + ", ".join(VALID_BASE_FINISHES)
        + ". Defaults to 'leatherette'.",
    )
    stand_tone: str = Field(
        "auto",
        description="Stand colour. Must be one of: " + ", ".join(VALID_STAND_TONES)
        + ". 'auto' contrasts with the reference metal. Defaults to 'auto'.",
    )
    backdrop: str = Field(
        "soft_grey",
        description="Studio backdrop. Must be one of: " + ", ".join(VALID_BACKDROPS) + ". Defaults to 'soft_grey'.",
    )


class StandShotPromptResponse(BaseModel):
    """Response body for the earring-on-stand display prompt."""

    success: bool
    prompt: Optional[str] = None
    earring_type: Optional[str] = None
    stand_style: Optional[str] = None
    base_finish: Optional[str] = None
    stand_tone: Optional[str] = None
    backdrop: Optional[str] = None
    error: Optional[str] = None


def _require_choice(field: str, value: str, allowed: tuple) -> None:
    """400 for a value outside ``allowed`` (the builder would silently fall back; a caller should know)."""
    if value not in allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {field}: '{value}'. Must be one of: {', '.join(allowed)}",
        )


@router.post(
    "/earring-stand-shot/prompt",
    response_model=StandShotPromptResponse,
    status_code=status.HTTP_200_OK,
    summary="Generate earring-on-stand studio display prompt (Prompt 8)",
    description=(
        "Returns the single authoritative earring-on-stand studio display prompt "
        "for Fashion Jewellery → Earrings with selectable stand, base, tone and "
        "backdrop. The frontend sends this prompt along with the reference image "
        "to /api/generate-image."
    ),
)
async def generate_stand_shot_prompt(request: StandShotPromptRequest):
    """Generate the earring-on-stand studio display prompt.

    This endpoint returns a complete prompt string that includes:
    - Reference image priority marker (prevents backend double-appendition)
    - Product fidelity core and anti-symmetry rules
    - Earring type-specific preservation (Hoop/Stud/Dangle)
    - Colour lock, material fidelity and metal affirmation
    - Stand, base and stand-tone specifications
    - Mounting and gravity rules for the earring type
    - Single vs pair placement logic
    - Camera, lighting and backdrop guidance
    - Negative space and bead cluster preservation
    - Stand-specific negative constraints
    """
    try:
        earring_type = request.earring_type
        if earring_type:
            _require_choice("earring_type", earring_type, VALID_EARRING_TYPES)
        _require_choice("stand_style", request.stand_style, VALID_STAND_STYLES)
        _require_choice("base_finish", request.base_finish, VALID_BASE_FINISHES)
        _require_choice("stand_tone", request.stand_tone, VALID_STAND_TONES)
        _require_choice("backdrop", request.backdrop, VALID_BACKDROPS)

        prompt = build_stand_shot_prompt(
            earring_type=earring_type,
            stand_style=request.stand_style,
            base_finish=request.base_finish,
            stand_tone=request.stand_tone,
            backdrop=request.backdrop,
        )

        logger.info(
            f"Earring stand shot prompt generated: "
            f"earring_type={earring_type or 'generic'} "
            f"stand_style={request.stand_style} base_finish={request.base_finish} "
            f"stand_tone={request.stand_tone} backdrop={request.backdrop} "
            f"prompt_len={len(prompt)}"
        )

        return StandShotPromptResponse(
            success=True,
            prompt=prompt,
            earring_type=earring_type,
            stand_style=request.stand_style,
            base_finish=request.base_finish,
            stand_tone=request.stand_tone,
            backdrop=request.backdrop,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Earring stand shot prompt generation failed: {e}")
        return StandShotPromptResponse(
            success=False,
            error=f"Prompt generation failed: {str(e)}",
        )


@router.get(
    "/earring-stand-shot/prompt/health",
    summary="Earring stand shot prompt health check",
    description="Check if the earring-on-stand display prompt endpoint is ready.",
)
async def stand_shot_health():
    """Health check for the earring-on-stand display prompt service."""
    import datetime

    return {
        "status": "ready",
        "service": "earring-stand-shot-prompt",
        "earring_types": list(VALID_EARRING_TYPES),
        "stand_styles": list(VALID_STAND_STYLES),
        "base_finishes": list(VALID_BASE_FINISHES),
        "stand_tones": list(VALID_STAND_TONES),
        "backdrops": list(VALID_BACKDROPS),
        "prompt_number": 8,
        "pack_slot": 7,
        "timestamp": datetime.datetime.now().isoformat(),
    }
