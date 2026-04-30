"""Inventaire des modèles IA — Session N1.

Endpoint `GET /models` qui aggrège runtime les `supported_models` de tous
les providers initialisés (Gemini, OpenAI, Anthropic, Qwen, OpenRouter,
Voice E1, Vision E2). Pas de table DB — source de vérité = providers.
"""
