"""API routes package."""

from app.api.routes import auth, health, upload, analysis, history, reports, tracking
from app.api.routes import prompts
from app.api.routes import image_generation
from app.api.routes import prompt_fusion
from app.api.routes import earring_ecommerce
from app.api.routes import earring_scale_reference
from app.api.routes import earring_professional_shot
from app.api.routes import earring_complementary_shot
from app.api.routes import earring_ugc_style
from app.api.routes import earring_macro_shot
from app.api.routes import earring_stand_shot

__all__ = ["auth", "health", "upload", "analysis", "history", "reports", "tracking", "prompts", "image_generation", "prompt_fusion", "earring_ecommerce", "earring_scale_reference", "earring_professional_shot", "earring_complementary_shot", "earring_ugc_style", "earring_macro_shot", "earring_stand_shot"]
