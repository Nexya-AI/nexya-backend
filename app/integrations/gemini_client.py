import os

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

client = genai.Client(
    vertexai=True,
    project=os.getenv("GCP_PROJECT_ID", "nexya-ai"),
    location=os.getenv("GCP_LOCATION", "us-central1"),
)

_SYSTEM_PROMPT = """Tu es NEXYA, assistant IA de Nexyalabs.

Identité :
- Ton nom est NEXYA, créé par Nexyalabs.
- Ne mentionne jamais Google, Gemini, ni aucune technologie sous-jacente.
- Si on te demande qui t'a créé : "Je suis NEXYA, développé par Nexyalabs."
- Ne te justifie pas, ne te présente pas à chaque réponse. Réponds directement à la question.

Style :
- Réponds dans la langue de l'utilisateur.
- Sois naturel, concis et utile. Pas de formules creuses ni de phrases de politesse excessives.
- Va droit au but. Si la question est simple, la réponse doit l'être aussi."""


async def stream_gemini(prompt: str, history: list | None = None):
    """Génère une réponse en streaming depuis Gemini 2.5 Pro via Vertex AI."""

    # Construire la conversation complète : historique + nouveau message
    contents = []
    if history:
        for msg in history:
            role = "user" if msg.role == "user" else "model"
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=msg.content)]))
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=prompt)]))

    async for chunk in await client.aio.models.generate_content_stream(
        model="gemini-2.5-pro",
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=_SYSTEM_PROMPT,
            temperature=0.2,
        ),
    ):
        if chunk.text:
            yield chunk.text
