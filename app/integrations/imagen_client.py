import base64
import logging

from google.genai import types

from .gemini_client import client

log = logging.getLogger(__name__)

# Imagen 3 — modèle Google spécialisé génération d'images via Vertex AI.
IMAGEN_MODEL = "imagen-3.0-generate-002"


async def generate_images(prompt: str, count: int = 1) -> list[str]:
    """Génère des images avec Imagen 3 via Vertex AI.

    Retourne une liste de chaînes base64 (JPEG).
    Imagen 3 supporte jusqu'à 4 images par appel.
    """
    count = min(max(count, 1), 4)

    log.info("Generating %d image(s) with model=%s", count, IMAGEN_MODEL)

    try:
        response = await client.aio.models.generate_images(
            model=IMAGEN_MODEL,
            prompt=prompt,
            config=types.GenerateImagesConfig(
                numberOfImages=count,
                aspectRatio="1:1",
                outputMimeType="image/jpeg",
            ),
        )

        images: list[str] = []
        if response.generated_images:
            for img in response.generated_images:
                b64 = base64.b64encode(img.image.image_bytes).decode("utf-8")
                images.append(b64)

        log.info("Imagen returned %d image(s)", len(images))
        return images

    except Exception as e:
        log.exception("Imagen generation failed: %s", e)
        raise
