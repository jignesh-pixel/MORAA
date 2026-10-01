"""
Engine factory — creates AI engine instances based on configuration.

Usage:
    from app.ai.engine_factory import create_engine
    engine = create_engine()  # reads AI_ENGINE_TYPE from settings
    pipeline = AnalysisPipeline(engine)

Add new engines by:
    1. Create a class that extends ``AIEngine`` in ``app/ai/``
    2. Register it in the ``_ENGINE_REGISTRY`` below
    3. Set ``AI_ENGINE_TYPE=your-engine-key`` in ``.env``
"""

from typing import Dict, Type

from app.ai.base import AIEngine
from app.ai.mock_engine import MockAIEngine
from app.ai.vision_engine import VisionAIEngine
from app.config import settings
from app.utils.logger import logger

# ---------------------------------------------------------------------------
# Registry — map config value → engine class
# Add new engines here.
# ---------------------------------------------------------------------------
_ENGINE_REGISTRY: Dict[str, Type[AIEngine]] = {
    "mock": MockAIEngine,
    "vision": VisionAIEngine,
}


def create_engine(engine_type: str = "") -> AIEngine:
    """
    Factory method — returns an instance of the configured AI engine.

    Args:
        engine_type: Optional override.  If empty, reads from
                     ``settings.AI_ENGINE_TYPE``.

    Returns:
        An :class:`AIEngine` instance ready for use.
    """
    key = (engine_type or settings.AI_ENGINE_TYPE).strip().lower()

    engine_cls = _ENGINE_REGISTRY.get(key)
    if engine_cls is None:
        logger.bind(category="system").warning(f"Unknown AI engine type '{key}', falling back to 'mock'. "
            f"Available: {list(_ENGINE_REGISTRY.keys())}"
        )
        engine_cls = MockAIEngine

    engine = engine_cls()
    logger.info(
        f"Created AI engine: {engine.engine_name} v{engine.engine_version} "
        f"(type='{key}')")
    return engine
