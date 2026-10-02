"""Image preprocessing service — prepares images before AI analysis.

Performs:
- File validation (size, type, integrity)
- Image resizing (maintains aspect ratio)
- Compression (JPEG quality optimisation)
- Normalisation (white balance, orientation)
- Corrupted image rejection

This service is called by the analysis pipeline before any AI provider
processes the images.
"""

import io
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image as PILImage
from PIL import ImageOps, UnidentifiedImageError

from app.config import settings
from app.utils.executors import run_cpu
from app.utils.logger import logger


class ImagePreprocessingError(Exception):
    """Raised when image preprocessing fails."""


class PreprocessingResult:
    """Result of preprocessing a single image."""

    def __init__(
        self,
        original_path: str,
        processed_path: Optional[str] = None,
        width: int = 0,
        height: int = 0,
        original_size: int = 0,
        processed_size: int = 0,
        success: bool = True,
        error: Optional[str] = None,
    ):
        self.original_path = original_path
        self.processed_path = processed_path or original_path
        self.width = width
        self.height = height
        self.original_size = original_size
        self.processed_size = processed_size
        self.success = success
        self.error = error


class PreprocessingService:
    """Prepares images for AI analysis."""

    ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}
    MIN_DIMENSION = 64
    MAX_DIMENSION = settings.PREPROCESS_MAX_DIMENSION
    COMPRESSION_QUALITY = settings.PREPROCESS_COMPRESSION_QUALITY

    def validate_file(self, file_path: str) -> bool:
        """Validate that a file is a readable, non-corrupted image.

        Returns:
            True if the image is valid and meets minimum requirements.
        """
        path = Path(file_path)
        if not path.exists() or not path.is_file():
            logger.error(f"File not found: {file_path}")
            return False

        if path.stat().st_size == 0:
            logger.error(f"Empty file: {file_path}")
            return False

        try:
            with PILImage.open(file_path) as img:
                img.verify()
            # Re-open after verify (verify can leave file in bad state)
            with PILImage.open(file_path) as img:
                w, h = img.size
                if w < self.MIN_DIMENSION or h < self.MIN_DIMENSION:
                    logger.warning(
                        f"Image too small: {w}x{h} (min {self.MIN_DIMENSION}px)"
                    )
                    return False
                if img.format not in self.ALLOWED_FORMATS:
                    logger.warning(f"Unsupported format: {img.format}")
                    return False
            return True
        except (UnidentifiedImageError, Exception) as e:
            logger.error(f"Image validation failed for {file_path}: {e}")
            return False

    async def preprocess(
        self,
        file_path: str,
        output_dir: Optional[str] = None,
    ) -> PreprocessingResult:
        """Preprocess a single image: validate, resize, compress.

        The decode / resize / JPEG encode is CPU-heavy (seconds for a large photo), so it runs on the CPU pool
        and never blocks the event loop (PERF-6).

        Args:
            file_path: Absolute path to the original image.
            output_dir: Optional directory for the processed image.
                        If not provided, the original file is overwritten.

        Returns:
            ``PreprocessingResult`` with processed image details.
        """
        return await run_cpu(self._preprocess_sync, file_path, output_dir)

    def _preprocess_sync(
        self,
        file_path: str,
        output_dir: Optional[str] = None,
    ) -> PreprocessingResult:
        """The blocking body of ``preprocess`` (unchanged logic)."""
        path = Path(file_path)
        original_size = path.stat().st_size if path.exists() else 0

        # Validate
        if not self.validate_file(file_path):
            return PreprocessingResult(
                original_path=file_path,
                original_size=original_size,
                success=False,
                error="Image validation failed",
            )

        try:
            with PILImage.open(file_path) as img:
                # Convert to RGB if necessary (RGBA -> RGB for JPEG)
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")

                # Auto-orient based on EXIF
                img = ImageOps.exif_transpose(img) or img

                original_w, original_h = img.size

                # Resize maintaining aspect ratio
                if original_w > self.MAX_DIMENSION or original_h > self.MAX_DIMENSION:
                    ratio = min(
                        self.MAX_DIMENSION / original_w,
                        self.MAX_DIMENSION / original_h,
                    )
                    new_w = int(original_w * ratio)
                    new_h = int(original_h * ratio)
                    img = img.resize((new_w, new_h), PILImage.LANCZOS)
                else:
                    new_w, new_h = original_w, original_h

                # Determine output path
                if output_dir:
                    out_path = Path(output_dir) / path.name
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                else:
                    out_path = path  # Overwrite original

                # Save with compression
                img.save(
                    str(out_path),
                    format="JPEG",
                    quality=self.COMPRESSION_QUALITY,
                    optimize=True,
                    progressive=True,
                )

                processed_size = out_path.stat().st_size

                logger.info(
                    f"Preprocessed: {path.name} "
                    f"{original_w}x{original_h} -> {new_w}x{new_h} "
                    f"{original_size} -> {processed_size} bytes "
                    f"({100 * (original_size - processed_size) // original_size if original_size else 0}% reduction)"
                )

                return PreprocessingResult(
                    original_path=file_path,
                    processed_path=str(out_path),
                    width=new_w,
                    height=new_h,
                    original_size=original_size,
                    processed_size=processed_size,
                    success=True,
                )

        except Exception as e:
            logger.error(f"Preprocessing failed for {file_path}: {e}")
            return PreprocessingResult(
                original_path=file_path,
                original_size=original_size,
                success=False,
                error=str(e),
            )

    async def preprocess_batch(
        self,
        file_paths: List[str],
        output_dir: Optional[str] = None,
    ) -> List[PreprocessingResult]:
        """Preprocess multiple images in parallel.

        Args:
            file_paths: List of absolute paths to image files.
            output_dir: Optional output directory for processed images.

        Returns:
            List of ``PreprocessingResult`` for each image.
        """
        import asyncio

        tasks = [self.preprocess(path, output_dir) for path in file_paths]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        final: List[PreprocessingResult] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                final.append(
                    PreprocessingResult(
                        original_path=file_paths[i],
                        success=False,
                        error=str(result),
                    )
                )
            else:
                final.append(result)

        success_count = sum(1 for r in final if r.success)
        logger.info(
            f"Batch preprocessing: {success_count}/{len(file_paths)} images succeeded"
        )

        return final

    def get_image_info(self, file_path: str) -> Dict:
        """Get basic image metadata without processing."""
        try:
            with PILImage.open(file_path) as img:
                return {
                    "width": img.width,
                    "height": img.height,
                    "format": img.format,
                    "mode": img.mode,
                    "size_bytes": Path(file_path).stat().st_size,
                }
        except Exception as e:
            return {"error": str(e)}
