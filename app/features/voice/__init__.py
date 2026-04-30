"""Feature Voice — STT (Whisper) + TTS (OpenAI) Pro-only.

Free plan : zéro backend, tout se fait côté Flutter via `speech_to_text`
et `flutter_tts` natifs. Backend = $0.

Pro plan : endpoints gated `require_pro` qui appellent Whisper API pour
une qualité premium + features exclusives.
"""
