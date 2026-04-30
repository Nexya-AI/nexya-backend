"""Feature Vision — analyse image multimodale via LLM (Gemini/GPT-4o/Claude).

Session E2. Endpoint unique `POST /vision/analyze` accepté pour Free (tier
flash = Gemini 2.0 Flash) et Pro (tier flash OU pro = Gemini 2.0 Pro ou
GPT-4o selon choix user).

Stratégie cost-smart :
- Free : 3 images/jour, tier flash imposé → ~$0.00054/user/jour max.
- Pro  : 50 images/jour, tier au choix → ~$0.125/user/jour max.
"""
