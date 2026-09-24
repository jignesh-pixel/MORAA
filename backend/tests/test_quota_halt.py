import asyncio

from app.ai.image_generation_manager import ImageGenerationManager
from app.ai.providers.image_base import ImageGenerationResult


def test_gemini_quota_halt_stops_before_openai_fallback(monkeypatch):
    """Gemini quota/resource exhaustion must halt immediately and never trigger OpenAI."""
    manager = ImageGenerationManager()

    gemini_calls = {"count": 0}
    openai_calls = {"count": 0}

    class FakeGeminiProvider:
        is_available = True

        def supports_reference_image(self):
            return True

        async def generate_image(
            self,
            prompt,
            context=None,
            reference_image=None,
            reference_mime_type="image/jpeg",
        ):
            gemini_calls["count"] += 1
            return ImageGenerationResult(
                success=False,
                error="429 Resource exhausted: quota exceeded for this project; credit_balance_exhausted",
                provider_name="gemini",
                processing_time=0.0,
                metadata={"recoverable": False},
            )

    class FakeOpenAIProvider:
        is_available = True

        def supports_reference_image(self):
            return True

        async def generate_image(
            self,
            prompt,
            context=None,
            reference_image=None,
            reference_mime_type="image/jpeg",
        ):
            openai_calls["count"] += 1
            return ImageGenerationResult(
                success=True,
                image_url="data:image/png;base64,abc",
                provider_name="openai",
                processing_time=0.1,
            )

    monkeypatch.setattr(manager, "_get_provider_chain", lambda: ["gemini", "openai"])
    monkeypatch.setattr(
        manager,
        "_get_provider",
        lambda name: FakeGeminiProvider() if name == "gemini" else FakeOpenAIProvider(),
    )

    result = asyncio.run(
        manager.generate_image(
            prompt="Generate a product shot",
            context={"request_id": "req-123"},
            reference_image=b"fake-bytes",
        )
    )

    assert result.success is False
    assert result.error is not None
    assert "quota" in result.error.lower() or "resource exhausted" in result.error.lower()
    assert result.metadata.get("non_recoverable") is True
    assert gemini_calls["count"] == 1
    assert openai_calls["count"] == 0
    assert manager.MAX_RETRIES_PER_PROVIDER == 0
