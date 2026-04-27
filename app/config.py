"""
NEXYA Backend — Configuration centralisée.

Toutes les variables d'environnement sont lues, typées et validées ici.
Si une variable obligatoire manque, l'API refuse de démarrer avec un message explicite.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings chargés depuis .env — validés au démarrage par Pydantic."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # ignore les variables inconnues dans .env
        case_sensitive=False,
    )

    # ── App ────────────────────────────────────────────────────
    env: str = "development"
    app_secret: str = "change-me"
    debug: bool = True

    # ── Database ───────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://nexya:nexya_dev@localhost:5432/nexya"

    # ── Redis ──────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── JWT RS256 ──────────────────────────────────────────────
    # Peut être soit le contenu PEM brut, soit un chemin vers un fichier .pem.
    # Le validator ci-dessous détecte automatiquement le format.
    jwt_private_key: str = ""
    jwt_public_key: str = ""
    jwt_access_ttl_minutes: int = 15
    jwt_refresh_ttl_days: int = 30

    @field_validator("jwt_private_key", "jwt_public_key", mode="after")
    @classmethod
    def load_key_from_file_if_path(cls, v: str) -> str:
        """Si la valeur est un chemin vers un .pem existant, charge son contenu.

        Permet deux usages :
        - Dev : JWT_PRIVATE_KEY=private.pem (chemin relatif au backend)
        - Prod : JWT_PRIVATE_KEY=<contenu PEM brut> (variable d'env multi-ligne)
        """
        if not v:
            return v
        # Si ça ressemble à une clé PEM (commence par -----BEGIN), on renvoie tel quel
        if v.startswith("-----BEGIN"):
            return v
        # Sinon on tente de lire le fichier
        path = Path(v)
        if path.is_file():
            return path.read_text(encoding="utf-8")
        return v

    # ── IA — Gemini (Vertex AI) ────────────────────────────────
    gemini_api_key: str = ""
    gcp_project_id: str = "nexya-ai"
    gcp_location: str = "us-central1"

    # ── IA — OpenAI ────────────────────────────────────────────
    openai_api_key: str = ""

    # ── IA — Anthropic (Claude) ───────────────────────────────
    anthropic_api_key: str = ""

    # ── IA — Qwen (DashScope International, OpenAI-compatible) ─
    # Si `qwen_api_key` est vide, le provider tombe sur un MockChatProvider.
    # Endpoint : https://dashscope-intl.aliyuncs.com/compatible-mode/v1 (US region,
    # évite les restrictions géographiques du endpoint Chine continentale).
    qwen_api_key: str = ""
    qwen_base_url: str = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

    # ── IA — OpenRouter (agrégateur multi-modèles, OpenAI-compatible) ─
    # Second fallback généraliste pour `general`, `productivity`, `sciences`
    # quand OpenAI/Anthropic/Qwen sont tous KO. JAMAIS sur safety-critical
    # (medicine/legal) — l'agrégateur peut router vers un modèle dont
    # l'alignement éthique n'a pas été vérifié.
    # En-têtes optionnels `HTTP-Referer` + `X-Title` : identifient NEXYA
    # dans les dashboards OpenRouter, non bloquants (vides = absents).
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_referer: str = "https://nexya.ai"
    openrouter_app_title: str = "NEXYA"

    # ── Storage (MinIO / S3 / R2) ──────────────────────────────
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket_name: str = "nexya-media"
    # Région AWS — ignorée par MinIO/R2, requise par boto3/aioboto3.
    s3_region_name: str = "us-east-1"
    # HTTPS pour S3 prod / R2 ; HTTP pour MinIO dev.
    s3_use_ssl: bool = False
    # Presigned URL TTL — 1 h par défaut. Le client Flutter refresh via
    # GET /library/{id} s'il garde l'URL plus longtemps (ex: listing
    # chargé puis gardé en RAM une demi-journée). Max S3 = 7 j, mais on
    # capée volontairement à 1 h pour minimiser la fenêtre d'exploitation
    # en cas d'URL leakée (share accidentel, logs client).
    s3_presigned_ttl_seconds: int = 3600
    # Taille max d'un upload Library (bytes décodés, post-base64).
    s3_max_upload_bytes: int = 20 * 1024 * 1024
    # Auto-create du bucket au premier upload — pratique dev, à désactiver
    # en prod où le bucket est provisionné par IaC/Terraform.
    storage_auto_create_bucket: bool = True
    # Force le MockObjectStore même si des creds S3 sont posées.
    # Utile pour les tests CI où on veut l'isolation totale.
    storage_mock_enabled: bool = False

    # ── Library (quotas Free / Pro) ────────────────────────────
    # Session C3 — plafonds sur le nombre d'items actifs dans la biblio.
    # Dépassement → 402 `LIBRARY_QUOTA_EXCEEDED` avec jauge en data.
    library_max_free: int = Field(default=50, ge=1)
    library_max_pro: int = Field(default=1000, ge=1)

    # ── Files (upload, extraction, virus scan) — Session E3 ─────
    # Cap dur applicatif pour un upload unitaire. Les PDFs enterprise
    # peuvent être gros (rapports scannés), d'où 100 MB vs 20 MB côté
    # Library. Au-delà il faut un vrai multipart S3 streaming (phase 12).
    files_max_upload_bytes: int = 100 * 1024 * 1024
    # Whitelist stricte des MIME acceptés côté /files/upload.
    # Double validation : MIME annoncé ∈ liste + MIME détecté magic-bytes
    # cohérent avec MIME annoncé.
    files_allowed_mimes: list[str] = Field(
        default_factory=lambda: [
            "image/png",
            "image/jpeg",
            "image/jpg",
            "image/gif",
            "image/webp",
            "video/mp4",
            "audio/mpeg",
            "audio/ogg",
            "audio/wav",
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "text/plain",
            "text/markdown",
            "text/csv",
        ]
    )
    # Cap sur le texte extrait (anti-OOM sur scans géants sans intérêt).
    files_extraction_max_chars: int = 500_000
    # Rate limit upload : 20/h/user (sliding window Redis).
    files_upload_rate_limit_per_hour: int = 20
    # TTL presigned URL pour un upload — plus court que Library (sensible).
    files_presigned_ttl_seconds: int = 1800  # 30 min

    # ── Virus scanner (mock-first) ─────────────────────────────
    # `virus_scan_enabled=False` → NoOpVirusScanner (clean systématique).
    # `clamav_host=""`           → MockVirusScanner (détection EICAR).
    # `clamav_host` renseigné    → ClamAVScanner (stub, activation prod).
    virus_scan_enabled: bool = True
    clamav_host: str = ""
    clamav_port: int = 3310

    # ── Mémoire IA (embeddings + pgvector) — Session D1 ────────
    # Provider par défaut : OpenAI `text-embedding-3-small` (1536 dim,
    # $0.02/1M tokens). Mock auto si `openai_api_key` vide OU si
    # `embeddings_mock_enabled=True` (force mock en CI/tests).
    openai_embedding_model: str = "text-embedding-3-small"
    embeddings_mock_enabled: bool = False
    # Dimension figée v1 au DDL (colonne `vector(1536)` en migration).
    # Changer cette valeur implique une migration backfill — prévu Phase 12.
    embeddings_dim: int = 1536
    # Cap applicatif sur `content` d'une mémoire — 2000 chars ≈ 500 tokens,
    # largement suffisant pour un fait durable (« Ivan est dev Flutter
    # basé au Cameroun depuis 2023, il code NEXYA avec Claude Code… »).
    # Le cap dur API OpenAI est à 8192 chars pour une unité.
    embeddings_content_max_chars: int = 2000
    # Anti-abus : limite le batch d'une requête embed() unique.
    # OpenAI accepte jusqu'à 2048 inputs par appel — on est bien en-dessous.
    embeddings_batch_max_size: int = 100

    # Quotas mémoires par plan. Free = 100 (saturé en 2-3 semaines d'usage
    # power-user → friction naturelle vers Pro). Pro = 10 000 (3 ans à
    # 10 mémoires/jour, tout en coupant un script abusif).
    memory_max_free: int = Field(default=100, ge=1)
    memory_max_pro: int = Field(default=10_000, ge=1)

    # Budget journalier sur les appels embed() par user (comptés via
    # BudgetTracker Redis). 10k appels/jour est largement au-dessus de
    # l'usage légitime — un attaquant qui spam est coupé avant.
    budget_embeddings_per_day: int = 10_000

    # ── Embeddings — sélection provider (G1) ───────────────────
    # Stratégie de sélection du provider embeddings NEXYA :
    # - `auto` (défaut) → auto-détection (voir `app/ai/embeddings/runtime.py`).
    # - `gemini` → force `GeminiEmbeddingsProvider` (`text-embedding-004`
    #   768 dim). Requiert `gemini_api_key`.
    # - `openai` → force `OpenAIEmbeddingsProvider`
    #   (`text-embedding-3-small` 1536 dim). Requiert `openai_api_key`.
    # - `mock` → force Mock déterministe (dim =
    #   `expert_corpus_embedding_dim`). Utile CI/tests.
    # Les colonnes DB `memories.embedding vector(1536)` (D1),
    # `document_chunks.embedding vector(1536)` (D4) et
    # `expert_corpus_chunks.embedding vector(768)` (G1) sont FIGÉES au
    # DDL — un switch de provider avec dim différente implique une
    # migration + re-ingestion complète (pipeline `--force-reembed`).
    embeddings_provider: str = "auto"  # 'auto'|'openai'|'gemini'|'mock'

    # Modèle Gemini embeddings par défaut. `gemini-embedding-001` = 768 dim.
    # 2026-04-24 : Google a renommé `text-embedding-004` → `gemini-embedding-001`
    # (même dim, même qualité). L'ancien nom retourne 404 NOT_FOUND sur v1beta.
    gemini_embedding_model: str = "gemini-embedding-001"

    # ── Vertex AI vs AI Studio (embeddings Gemini) ───────────────
    # Par défaut (False), le provider Gemini utilise AI Studio via
    # `GEMINI_API_KEY`. Passer à True pour router vers Vertex AI :
    # utile si les crédits Gemini sont sur GCP (free trial $300,
    # billing account GCP) plutôt que sur AI Studio. Vertex AI utilise
    # Application Default Credentials — faire `gcloud auth application-default
    # login` avant le démarrage de l'API.
    gemini_use_vertex: bool = False
    # Project GCP utilisé en mode Vertex AI (ignoré en mode AI Studio).
    gcp_project_id: str = ""
    # Region GCP utilisée en mode Vertex AI. `us-central1` est le plus
    # standard + le moins cher + le mieux disponible. Autres possibilités :
    # `europe-west4` (Belgique), `asia-southeast1` (Singapour).
    gcp_region: str = "us-central1"

    # ── Corpus Experts RAG (G1) ─────────────────────────────────
    # Kill-switch global du corpus Experts. Si False, l'injection RAG
    # est short-circuitée pour tous les experts, quelle que soit la
    # valeur de `ExpertConfig.corpus_enabled`.
    expert_corpus_enabled: bool = True

    # Dimension vectorielle du corpus Experts — FIGÉE au DDL
    # (`expert_corpus_chunks.embedding vector(768)`). 768 correspond à
    # Gemini `text-embedding-004`, notre provider par défaut au
    # 2026-04-26. **Switch de dim** : drop index HNSW → ALTER COLUMN
    # TYPE → re-ingestion complète via
    # `scripts/import_expert_corpus_langues.py --force-reembed`
    # → recréation HNSW. Estimé ~20 min pour le corpus langues.
    expert_corpus_embedding_dim: int = 768

    # Top-K chunks corpus injectés dans le system prompt.
    expert_corpus_k: int = Field(default=5, ge=1, le=20)
    # Seuil plancher de similarité cosinus (filtre les chunks
    # tangentiels).
    expert_corpus_min_similarity: float = Field(default=0.7, ge=0.0, le=1.0)
    # Cap dur sur la taille du bloc corpus injecté.
    expert_corpus_max_chars: int = Field(default=3_000, ge=100, le=10_000)

    # URLs des dumps Tatoeba (corpus expert Langues). Caché MinIO au
    # premier download, évite de retélécharger ~300 MB à chaque run.
    tatoeba_sentences_url: str = "https://downloads.tatoeba.org/exports/sentences.tar.bz2"
    tatoeba_links_url: str = "https://downloads.tatoeba.org/exports/links.tar.bz2"

    # ── Mémoire IA — injection system prompt (D3) ──────────────
    # Kill-switch global pour dégrader vite en cas d'incident
    # (pgvector lent, embeddings down, faux positifs sur le filtre
    # sensibilité qui pollueraient les prompts, etc.).
    memory_injection_enabled: bool = True
    # Top-K memories injectées dans le system prompt. 5 = bon compromis
    # pertinence vs coût tokens. Plus on en met, plus le LLM a de
    # contexte mais plus le prompt coûte.
    memory_injection_k: int = Field(default=5, ge=1, le=20)
    # Seuil plancher de similarity cosinus. 0.7 filtre les memories
    # tangentielles. 1.0 = identique strict, 0.0 = tout passe.
    memory_injection_min_similarity: float = Field(default=0.7, ge=0.0, le=1.0)
    # Cap dur sur la taille totale du bloc mémoire injecté. Protège
    # contre l'explosion du prompt si les memories sont longues ou
    # nombreuses. 2000 chars ≈ 500 tokens — acceptable overhead.
    memory_injection_max_chars: int = Field(default=2_000, ge=100)

    # ── Documents RAG (Bloc D4) ────────────────────────────────
    # PRICING — TODO(Ivan): valider lors du pricing final (provisoire)
    # Ces 4 valeurs définissent l'offre commerciale (ce qu'un Free peut
    # faire, ce qu'un Pro débloque). Elles sont démarrées sur des valeurs
    # raisonnables pour ne pas bloquer le dev, mais doivent être
    # tranchées par Ivan avant la mise en prod du pricing.
    # Documents actifs par plan (soft-delete non compté).
    documents_max_free: int = Field(default=3, ge=0)  # TODO(Ivan): provisoire
    documents_max_pro: int = Field(default=50, ge=0)  # TODO(Ivan): provisoire
    # Plafond de qualité de service par document (cap le nombre max de
    # chunks = taille max acceptée du doc ≈ 250k tokens à 500 tokens/chunk).
    documents_chunks_per_file_max: int = Field(default=500, ge=1)  # TODO(Ivan): provisoire
    # Parallélisme worker par user — capacité que paye le Pro vs Free.
    max_concurrent_chunking_per_user: int = Field(default=2, ge=1)  # TODO(Ivan): provisoire

    # TECHNIQUE — à ma main (calibrage algorithmique non-commercial)
    documents_chunk_target_tokens: int = Field(default=500, ge=100, le=2000)
    documents_chunk_overlap_tokens: int = Field(default=50, ge=0, le=500)
    documents_embed_batch_size: int = Field(default=100, ge=1, le=500)
    documents_pre_clean_min_chars: int = Field(default=50, ge=1)
    # TTL du sémaphore Redis par user (sécurité : si le worker crash avec
    # un slot acquis, le slot se libère tout seul après ce délai).
    documents_chunking_semaphore_ttl_seconds: int = Field(default=600, ge=60, le=3600)

    # ── Voice (Bloc E1) — Pro only ──────────────────────────────
    # Stratégie asymétrique Free vs Pro :
    # - Free : STT/TTS natif Flutter (`speech_to_text` + `flutter_tts`).
    #   Zéro backend → $0 de coût backend. Fonctionne offline.
    # - Pro  : endpoints backend gated `require_pro` qui appellent
    #   Whisper API / OpenAI TTS pour qualité premium + historique DB +
    #   features exclusives (fichiers longs, langue auto 99 dialects).
    # Un Free qui tape `/voice/*` reçoit 403 PLAN_REQUIRED immédiat.
    #
    # PRICING — TODO(Ivan): valider lors du pricing final (provisoire)
    voice_minutes_pro_per_day: int = Field(default=120, ge=0)  # TODO(Ivan): provisoire
    voice_tts_chars_pro_per_day: int = Field(default=50_000, ge=0)  # TODO(Ivan): provisoire

    # TECHNIQUE — à ma main (calibrage non-commercial)
    voice_max_upload_bytes: int = 20 * 1024 * 1024  # 20 MB (Whisper API cap 25 MB, marge)
    voice_max_duration_seconds: int = 600  # 10 min max par appel (anti-OOM + coût)
    voice_transcribe_rate_limit_per_hour: int = Field(default=30, ge=1, le=10_000)
    voice_tts_rate_limit_per_hour: int = Field(default=60, ge=1, le=10_000)
    voice_allowed_mimes: list[str] = Field(
        default_factory=lambda: [
            "audio/mpeg",
            "audio/mp3",
            "audio/mp4",
            "audio/m4a",
            "audio/wav",
            "audio/webm",
            "audio/ogg",
            "audio/x-m4a",
        ]
    )
    voice_mock_enabled: bool = False  # force mock même si clé OpenAI présente
    voice_default_stt_model: str = "whisper-1"
    voice_default_tts_model: str = "tts-1"
    voice_default_voice: str = "alloy"

    # ── Images watermark (Bloc E4) ──────────────────────────────
    # Watermark visuel NEXYA (logo oiseau bleu) sur toutes les images
    # générées via `/image/generate`. Retirable par les utilisateurs
    # Pro moyennant surcoût (différentiel prix à implémenter en
    # wallet v2 — voir mémoire `project_nexya_pricing_model_v2.md`).
    #
    # PRICING — TODO(Ivan): valider lors du pricing final (provisoire)
    image_no_watermark_price_multiplier: float = Field(
        default=2.0, ge=1.0, le=10.0
    )  # TODO(Ivan): provisoire — ratio prix image sans vs avec watermark

    # TECHNIQUE — à ma main (calibrage non-commercial)
    image_watermark_scale_ratio: float = Field(default=0.12, ge=0.05, le=0.25)
    image_watermark_opacity: float = Field(default=0.70, ge=0.3, le=1.0)

    # ── Vision (Bloc E2) — Free + Pro asymétrie par tier ───────
    # Free : tier='flash' imposé → Gemini 2.0 Flash (cheap).
    # Pro  : choix tier 'flash' ou 'pro' → Gemini 2.0 Pro ou GPT-4o.
    # Gate dans le service (pas au router) : 403 PLAN_REQUIRED si Free
    # tente tier='pro'.
    #
    # PRICING — TODO(Ivan): valider lors du pricing final (provisoire)
    vision_images_free_per_day: int = Field(default=3, ge=0)  # TODO(Ivan): provisoire
    vision_images_pro_per_day: int = Field(default=50, ge=0)  # TODO(Ivan): provisoire
    vision_max_images_per_request: int = Field(
        default=4, ge=1, le=20
    )  # TODO(Ivan): provisoire (capacité Pro)
    vision_max_output_tokens_pro: int = Field(
        default=4096, ge=64, le=8192
    )  # TODO(Ivan): provisoire

    # TECHNIQUE — à ma main (calibrage non-commercial)
    vision_max_image_bytes: int = 10 * 1024 * 1024  # 10 MB
    vision_max_dimension: int = 2048  # pixels (resize-down au-delà)
    vision_max_input_tokens_per_request: int = Field(default=10_000, ge=1_000)
    vision_rate_limit_per_hour: int = Field(default=30, ge=1, le=10_000)
    vision_allowed_mimes: list[str] = Field(
        default_factory=lambda: [
            "image/png",
            "image/jpeg",
            "image/jpg",
            "image/webp",
            "image/gif",
        ]
    )
    vision_mock_enabled: bool = False
    vision_default_flash_model: str = "gemini-2.0-flash"
    vision_default_pro_model: str = "gemini-2.0-pro"
    vision_pro_provider: str = "gemini"  # 'gemini' | 'openai'

    # ── Planner Scheduler (Bloc F1) ─────────────────────────────
    # Tâches IA planifiées. CRUD /tasks + 2 workers arq
    # (`dispatch_due_tasks` toutes les minutes + `execute_scheduled_task`
    # par tâche). Rétention résultats 30 jours via cron cleanup.
    #
    # PRICING — TODO(Ivan): provisoire, à trancher au pricing final
    tasks_max_free: int = Field(default=3, ge=0)  # TODO(Ivan): provisoire
    tasks_max_pro: int = Field(default=50, ge=0)  # TODO(Ivan): provisoire

    # TECHNIQUE — à ma main (calibrage non-commercial)
    tasks_min_interval_minutes: int = Field(default=5, ge=1, le=60)
    tasks_results_retention_days: int = Field(default=30, ge=1)
    tasks_dispatch_batch_size: int = Field(default=50, ge=1, le=500)
    tasks_max_title_chars: int = Field(default=200, ge=1)
    tasks_max_prompt_chars: int = Field(default=4000, ge=100)

    # ── RAG query public (Bloc D5) ──────────────────────────────
    # TECHNIQUE — calibrage anti-abus, pas une valeur pricing commerciale.
    # 60/h couvre un power-user légitime (1 recherche/min pendant une
    # heure intense). Au-delà, indique un script qui spamme. Distinct
    # du `budget_embeddings_per_day` (fusible global) — les deux
    # cohabitent pour couper tôt les boucles client buggées.
    rag_query_rate_limit_per_hour: int = Field(default=60, ge=1, le=10_000)

    # ── Paiements ──────────────────────────────────────────────
    cinetpay_api_key: str = ""
    cinetpay_site_id: str = ""
    notchpay_public_key: str = ""
    notchpay_secret_key: str = ""
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""

    # ── Notifications — FCM (Session F2) ───────────────────────
    # Firebase Cloud Messaging HTTP v1 avec OAuth2 service account.
    # Pattern mock-first : si NI `fcm_service_account_json` NI
    # `fcm_service_account_file` n'est renseigné, on bascule sur
    # `MockFCMProvider` avec un warning unique au boot. Aucun push
    # réel n'est envoyé en prod tant que le service account n'est
    # pas branché — c'est volontaire, le dev/CI ne doit pas dépendre
    # d'une clé Firebase.
    #
    # Deux façons de fournir le service account :
    # - `fcm_service_account_json` : contenu JSON brut échappé (une
    #   seule ligne, pratique pour variables d'env prod).
    # - `fcm_service_account_file` : chemin vers le fichier JSON
    #   (pratique pour dev local).
    # Si les deux sont posés, `_json` prime.
    fcm_server_key: str = ""  # legacy — conservé pour rétro-compat, plus utilisé
    fcm_service_account_json: str = ""
    fcm_service_account_file: str = ""
    # `project_id` optionnel — sinon lu depuis le JSON service account.
    fcm_project_id: str = ""
    # Force MockFCMProvider même si une clé est posée (utile tests CI).
    fcm_mock_enabled: bool = False
    # Timeout httpx client FCM — court pour ne pas bloquer le worker
    # Planner plusieurs secondes sur une panne Firebase.
    fcm_push_timeout_seconds: int = Field(default=10, ge=1, le=60)
    # Troncature du corps de notification (extrait du result_text de la
    # tâche). Au-delà c'est illisible sur lock-screen mobile.
    fcm_body_preview_max_chars: int = Field(default=140, ge=20, le=500)

    # ── Chat — function calling / tools LLM (Session F2) ────────
    # Cap dur sur le nombre de rounds tool_calls autorisés dans un
    # même `/chat/stream`. 5 = largement au-dessus du besoin légitime
    # (rappelle-moi X demain = 1 round), coupe les boucles infinies
    # où le LLM s'obstinerait à appeler un tool en boucle.
    chat_max_tool_rounds: int = Field(default=5, ge=1, le=20)

    # F2.5 — kill-switch global du function calling dans `/chat/stream`.
    # Si False, le router n'injecte JAMAIS `tools` dans le `StreamContext`
    # (équivalent à un mode F2 inerte côté providers réels). Permet de
    # désactiver les tools en prod sans déployer de hotfix code en cas
    # d'incident (Planner DB down, bug critique sur un handler tool, etc.).
    # Best practice canary : poser False au premier déploiement prod, puis
    # passer à True après vérification manuelle bout-en-bout.
    tools_enabled_in_chat: bool = True

    # ── Notifications dispatcher (Session F3) ───────────────────
    # Kill-switch global du fallback email : si False, un push KO reste
    # KO sans tentative email — utile pour débogger le chemin push pur
    # ou si la facture email explose temporairement. Par défaut True
    # (UX pro : l'user reçoit toujours une notif, push OU email).
    notification_fallback_email_enabled: bool = True
    # Nombre max de tentatives push avant de considérer push échoué et
    # basculer sur le fallback email. 3 = un retry + 2 essais = ~3s
    # total avec les timeouts FCM.
    notification_max_attempts_push: int = Field(default=3, ge=1, le=10)
    # TTL du JWT d'unsubscribe — long délibérément (365j) pour que les
    # liens dans des emails archivés restent fonctionnels. CAN-SPAM US
    # impose 10j minimum, on est largement au-dessus. RGPD n'impose pas
    # de TTL explicite mais exige que l'unsubscribe reste possible
    # « à tout moment » — le TTL long honore cet esprit.
    notification_unsubscribe_token_ttl_days: int = Field(default=365, ge=10, le=3650)
    # URL publique de la page d'unsubscribe (Flutter/web) qui reçoit le
    # token en query param et appelle ensuite POST /notifications/unsubscribe/
    # {token} côté backend. Mobile = scheme `nexya://unsubscribe?token=...`,
    # web fallback = https://app.nexya.ai/unsubscribe.
    frontend_unsubscribe_url: str = "https://app.nexya.ai/unsubscribe"
    # Rate limit IP sur `POST /notifications/unsubscribe/{token}` (public,
    # sans auth). Un attaquant qui brute-force des tokens doit être coupé
    # tôt. 10/h suffit largement pour un legit qui clique ré-clique.
    unsubscribe_rate_limit_per_hour: int = Field(default=10, ge=1, le=1000)

    # ── Email transactionnel (Brevo / Sendinblue) ─────────────
    # En dev/test, si `brevo_api_key` est vide, l'app utilise un
    # MockEmailService qui loggue les emails au lieu de les envoyer.
    brevo_api_key: str = ""
    brevo_sender_email: str = "no-reply@nexya.ai"
    brevo_sender_name: str = "NEXYA"

    # URL publique du frontend — sert à construire les deep links
    # envoyés dans les emails (reset password, confirmation, etc.).
    # En mobile, le Flutter interceptera le scheme `nexya://` ;
    # la version web fallback sur `https://app.nexya.ai/...`.
    frontend_password_reset_url: str = "https://app.nexya.ai/reset-password"

    # ── Captcha — hCaptcha (anti-bot à l'inscription) ─────────
    # En dev/test, si `hcaptcha_secret_key` est vide OU `hcaptcha_enabled=False`,
    # l'app utilise un MockCaptchaVerifier qui accepte le token "mock-success"
    # et rejette "mock-fail". `hcaptcha_enabled=False` sert aussi de kill-switch
    # en prod : en cas d'incident hCaptcha, on préfère ouvrir les inscriptions
    # plutôt que tout bloquer (on a d'autres couches : rate limit IP + device quota).
    hcaptcha_enabled: bool = True
    hcaptcha_secret_key: str = ""
    hcaptcha_site_key: str = ""

    # ── Anti-abus : quotas par appareil & limites journalières ─
    # Chaque inscription envoie un header `X-Device-Id` (UUID stable généré
    # par le Flutter au premier lancement, persisté en local). Un même
    # device ne peut créer que `device_registration_daily_limit` comptes
    # par fenêtre de 24 h — bloque les fermes d'inscriptions automatisées.
    # Si le header est absent, on considère le device "unknown" et on
    # applique une limite plus stricte (même clé pour tous les unknowns).
    device_registration_daily_limit: int = 3
    # Limite IP journalière pour /auth/register — couche 2 (la couche 1 est
    # le sliding window 5/min déjà en place). 5/jour suffit : une personne
    # normale ne crée pas 6 comptes/jour sur un même réseau.
    register_daily_ip_limit: int = 5
    # Limite messages chat user-scoped : >100 msg/min indique un bot — on
    # bloque avant même d'appeler le LLM (économise tokens + protège rerank).
    chat_message_per_minute_limit: int = 100

    # ── IA — Cache prompt Redis (brique B2) ───────────────────
    # Cache clé = (model, sha256(canonical_messages)), TTL 24 h par défaut.
    # Désactivable par kill-switch en cas d'incident (mauvaise réponse
    # cachée → on force un passage par le provider le temps d'investiguer).
    # Économise 40-60 % de coût LLM sur les questions fréquentes (FAQ, onboarding).
    # Les experts tagués `safety-critical` (médecine, légal) ne sont JAMAIS
    # cachés — chaque réponse est recalculée pour éviter qu'une réponse
    # médicale erronée soit servie à un autre user (garde-fou éthique).
    prompt_cache_enabled: bool = True
    prompt_cache_ttl_seconds: int = 86_400

    # ── IA — Moderation business rules (brique B2) ────────────
    # Règles métier additionnelles à la modération OpenAI `omni-moderation`.
    # Refuse : prescription médicale nominative, conseil juridique nominatif,
    # etc. Whitelist par expert : l'expert "medicine" peut parler de symptômes
    # généraux mais pas prescrire un dosage ; l'expert "legal" peut expliquer
    # un article de loi mais pas rédiger un contrat nommant des parties.
    # Kill-switch pour débrayer en cas de faux positifs massifs.
    moderation_rules_enabled: bool = True

    # ── IA — Estimation tokens pré-appel (brique B2) ──────────
    # Ratio caractères → tokens utilisé en heuristique quand tiktoken n'a
    # pas de tokenizer adapté (Gemini, Qwen avant download du tokenizer).
    # 3.0 = un token ≈ 3 caractères pour du texte mixte FR/EN.
    # On majore volontairement (pessimiste) pour ne pas sous-estimer le coût.
    token_estimate_chars_per_token: float = 3.0

    # Cap dur sur le nombre de tokens en entrée pour une requête chat
    # (anti-abus). Un user qui balance 30 000 tokens de prompt (~100 000
    # caractères) n'est pas un usage légitime — on coupe avec 402
    # `LLM_QUOTA_EXCEEDED` AVANT d'appeler le LLM. Gemini Pro supporte
    # 128 k tokens de contexte : ce cap laisse largement de marge pour
    # un historique + un prompt réaliste, tout en bloquant les uploads
    # de romans en plain text.
    chat_prompt_tokens_per_request_max: int = 30_000

    # ── Projects (quotas Free / Pro) ──────────────────────────
    # Session C2 — plafonds appliqués en pré-flight par `ProjectService`.
    # Une atteinte se traduit par 402 `PROJECT_QUOTA_EXCEEDED` ou
    # `PROJECT_FILES_QUOTA_EXCEEDED` avec `data={current, max, plan}` pour
    # que le Flutter affiche la jauge et propose l'upgrade.
    # Pensés pour « assez large pour un power-user légitime » sans ouvrir
    # la porte à de l'abus (chacun Free = 3×5 = 15 fichiers max).
    projects_max_free: int = Field(default=3, ge=1)
    projects_max_pro: int = Field(default=50, ge=1)
    project_files_max_free: int = Field(default=5, ge=1)
    project_files_max_pro: int = Field(default=100, ge=1)

    # ── CORS ───────────────────────────────────────────────────
    allowed_origins: str = "*"

    # ── Database pool ──────────────────────────────────────────
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_echo: bool = False

    # ── Redis pool ─────────────────────────────────────────────
    redis_max_connections: int = 50

    # ── Timeouts (secondes) — Africa-first ─────────────────────
    llm_timeout: int = 30
    stream_timeout: int = 120
    upload_timeout: int = 60

    # ── Pagination ─────────────────────────────────────────────
    pagination_max_limit: int = Field(default=50, ge=1)
    pagination_default_limit: int = Field(default=20, ge=1)

    # ── Observabilité prod (Session K1) ────────────────────────
    # Version applicative — utilisée par Sentry pour tracker les
    # releases, et par OTel comme `service.version` dans la
    # ressource. Posée par le pipeline CI/CD via env var
    # `APP_VERSION=v0.1.0+sha.abc1234` au build. Défaut "dev" en
    # local pour ne pas bloquer le démarrage.
    app_version: str = Field(default="dev", min_length=1, max_length=128)

    # ── OpenTelemetry tracing (K1) ─────────────────────────────
    # Kill-switch global : False = aucune instrumentation ne
    # s'attache, le service tourne comme avant. À poser False au
    # 1ᵉʳ déploiement prod, True après vérification que le
    # collecteur OTLP cible (Jaeger/Tempo/Honeycomb) est joignable.
    otel_enabled: bool = Field(default=False)
    # Endpoint OTLP/HTTP du collecteur (port 4318 standard). Le SDK
    # envoie en silence vers le rien si l'endpoint est inaccessible
    # (fail-open : un seul warning au boot, pas de crash).
    otel_exporter_otlp_endpoint: str = Field(
        default="http://localhost:4318",
        min_length=1,
    )
    # Nom du service tel qu'il apparaîtra dans Jaeger/Tempo. Cap
    # 64 chars : convention OTel `service.name` resource attribute.
    otel_service_name: str = Field(default="nexya-backend", min_length=1, max_length=64)
    # Ratio de sampling — défaut 10 % en prod (économie facture
    # OTLP), 1.0 en dev/CI pour debug 100 %. Le ParentBased honore
    # la décision parent si un upstream a déjà sampled.
    otel_traces_sampler_ratio: float = Field(default=0.1, ge=0.0, le=1.0)
    # Inclusion du `user_id` dans les attributs span. Off par
    # défaut (RGPD : un user_id en clair côté APM tiers est une
    # donnée personnelle). À activer ponctuellement pour debug.
    otel_log_user_ids: bool = Field(default=False)

    # ── Sentry exceptions + breadcrumbs (K1) ───────────────────
    # DSN env-aware : vide = sentry_sdk.init n'est PAS appelé du
    # tout (zéro overhead, zéro outbound). Rempli = init avec
    # 5 integrations standards + scrubber secrets ponté depuis
    # `core/errors/handlers.py`.
    sentry_dsn: str = Field(default="")
    # Environnement Sentry (development|staging|production) — sert
    # à filtrer les events dans le dashboard Sentry par stack.
    sentry_environment: str = Field(
        default="development",
        pattern=r"^(development|staging|production)$",
    )
    # Sample rate des transactions APM Sentry (en plus des events).
    # 5 % en prod = trade-off coût/visibilité. 0.0 désactive
    # complètement le tracing Sentry tout en gardant la capture
    # d'exceptions.
    sentry_traces_sample_rate: float = Field(default=0.05, ge=0.0, le=1.0)
    # Profiling continu Sentry — désactivé V1 (coût + maturité
    # 2026 sur Python). À ré-évaluer Phase 19.
    sentry_profiles_sample_rate: float = Field(default=0.0, ge=0.0, le=1.0)

    # ── Prometheus métriques (K1) ──────────────────────────────
    # Activation de l'endpoint /metrics + de la collecte custom.
    # Coût zéro côté client, scrape pull-based externe.
    prometheus_enabled: bool = Field(default=True)
    # Path de l'endpoint /metrics — configurable au cas où on
    # veut le déplacer derrière `/internal/metrics` ou autre.
    prometheus_metrics_path: str = Field(default="/metrics", pattern=r"^/[\w/-]+$")
    # Token d'auth pour le scraper Prometheus. Vide en dev =
    # endpoint ouvert avec WARNING au boot. Vide en prod =
    # ValueError fail-fast au boot (refus de démarrer — un
    # endpoint /metrics ouvert en prod expose les KPI métier
    # à Internet, c'est un trou DDoS / business intelligence).
    prometheus_scrape_token: str = Field(default="")

    # ── Logging — injection contexte OTel (K1) ─────────────────
    # Quand True, chaque log structlog porte automatiquement
    # `trace_id` (32 hex) + `span_id` (16 hex) du span OTel
    # actif — corrélation logs ↔ traces dans Tempo/Loki/Grafana.
    # Désactivable ponctuellement si problème de perf ou bruit.
    observability_log_trace_injection: bool = Field(default=True)

    # ── RGPD + AI Act (Session J1) ─────────────────────────────
    # Délai légal entre demande de suppression et hard delete physique.
    # Article 17 RGPD : « sans retard injustifié ». La CNIL recommande
    # 30j minimum pour permettre la rétractation user. Certains acteurs
    # offrent 7j (UX plus rapide) — TODO Ivan provisoire.
    rgpd_deletion_grace_period_days: int = Field(default=30, ge=0, le=365)  # TODO(Ivan): provisoire
    # Cap soft sur la taille du ZIP d'export RGPD. Au-delà, le ZIP est
    # créé mais avec un flag `truncated=True` dans le manifest. À tuner
    # selon le profil moyen utilisateur — TODO Ivan provisoire.
    rgpd_export_max_size_bytes: int = Field(
        default=100 * 1024 * 1024, ge=1024 * 1024
    )  # TODO(Ivan): provisoire
    # Rate limit user-scope sur GET /rgpd/user/data-export. 1/24h suffit
    # largement pour un usage légitime (un export par jour max). Coupe
    # un script abusif qui tenterait de dump en boucle.
    rgpd_export_rate_limit_per_24h: int = Field(default=1, ge=1, le=10)
    # TTL des presigned URLs pour les blobs MinIO inclus dans le ZIP.
    # 7 jours = délai raisonnable pour que l'user télécharge tout.
    # Au-delà, il refait un export.
    rgpd_blob_presigned_ttl_seconds: int = Field(default=7 * 24 * 3600, ge=300, le=30 * 24 * 3600)
    # Liste des emails autorisés à appeler /rgpd/admin/ai-act-registry.
    # Vide en dev (avec warning au boot). Vide en prod = ValueError
    # fail-fast (un endpoint admin sans ACL = fuite catastrophique
    # du registre AI Act complet).
    rgpd_admin_emails: list[str] = Field(default_factory=list)

    # ── N1 — Endpoints divers (feedback / suggestions / models) ──
    # Email de l'équipe NEXYA recevant les suggestions user via
    # `POST /suggestions`. Recommandation prod : créer un alias
    # `feedback@nexya.ai` qui pointe vers la boîte d'Ivan / DPO V1,
    # remplacer par une mailing-list équipe Phase 14.
    feedback_team_email: str = Field(default="feedback@nexya.ai")
    # Anti-spam suggestions user (5 submits / jour / user, sliding
    # window Redis 24 h). Au 6ᵉ → 429 `RATE_LIMIT_ABUSE`.
    suggestions_rate_limit_per_day: int = Field(default=5, ge=1, le=100)
    # Rate limit feedback chat user-scoped (60/h sliding window).
    # Plus large que suggestions car les thumbs up/down peuvent être
    # rapides sur plusieurs messages d'une même conv.
    feedback_rate_limit_per_hour: int = Field(default=60, ge=1, le=10_000)
    # TTL `Cache-Control: max-age=` du `GET /models` côté client.
    # 5 min = compromis entre transparence (changements clé provider)
    # et économie requêtes.
    models_endpoint_cache_ttl_seconds: int = Field(default=300, ge=0, le=3600)

    # ── Grafana + Prometheus dev (Session K2) ──────────────────
    # K2 livre 5 dashboards Grafana + 6 alertes Prometheus + un
    # docker-compose séparé pour faire tourner Grafana + Prometheus
    # localement. Le backend NEXYA n'utilise PAS ces settings au
    # runtime — ils servent à interpoler des fichiers de config
    # consommés exclusivement par Grafana et Prometheus côté infra.
    # Présents ici (vs un .env infra distinct) pour centraliser la
    # configuration et faire passer la production safety guard.
    #
    # Mot de passe admin Grafana en dev. Vide ou "admin" en prod
    # → ValueError fail-fast au boot (un compte admin Grafana
    # connu = takeover instantané du dashboard d'observabilité).
    grafana_admin_password: str = Field(default="admin")
    # Période de scrape Prometheus → /metrics. 15s = standard
    # industrie, suffisant pour les graphes 5min/1h sans saturer
    # le backend de requêtes.
    prometheus_scrape_interval_seconds: int = Field(default=15, ge=5, le=300)
    # Seuil USD/jour déclenchant l'alerte `NexyaCostUSDDailyExceeded`.
    # 100 USD/jour = défaut conservateur ; à affiner par Ivan selon
    # le budget IA prod réel (50 USD = signal d'attaque/abus, 500
    # USD = mode normal high-volume). TODO(Ivan): provisoire.
    cost_usd_daily_alert_threshold: float = Field(default=100.0, ge=0.0)  # TODO(Ivan): provisoire

    # ── Évals IA reproductibles en CI (Session N3) ─────────────
    # Harness de détection de régression qualité IA. Tourne en mock
    # judge sur PR (gratuit, bloque si pp_drop > seuil) et en real
    # judge nightly (coûte ~$1/run × 30 = ~$30/mois).
    evals_judge_model: str = Field(
        default="gemini-2.5-pro",
        description="Modèle utilisé par GeminiJudge.",
    )
    evals_regression_threshold_pp: float = Field(
        default=10.0,
        ge=0.0,
        le=100.0,
        description="Seuil en pp pour détecter une régression. PR=10pp, nightly=5pp.",
    )
    evals_corpus_min_size: int = Field(
        default=100,
        ge=1,
        description="Sanity check anti-corpus-vide (CI fail si < cette taille).",
    )

    # ── Phase 18 — Crisp + Helpdesk (Session N4 volet B) ──────
    # Escalation auto vers Crisp (chat support) quand un user Pro
    # rencontre un incident critique (paiement, LLM down).
    # Mock-first auto si CRISP_API_KEY ou CRISP_WEBSITE_ID vide.
    crisp_website_id: str = Field(default="", description="ID du website Crisp.")
    crisp_identifier: str = Field(
        default="plugin",
        description="Identifier Crisp (plugin_id ou user_id selon le mode).",
    )
    crisp_api_key: str = Field(default="", description="Clé API Crisp.")
    crisp_escalation_enabled: bool = Field(
        default=True,
        description="Kill-switch global escalation Crisp (False = log seulement).",
    )

    # ── Tests de charge k6 (Session N4 volet A) ────────────────
    # Plafonds de sanity pour éviter qu'un scenario k6 buggé ne lance
    # 100 000 VUs et explose le runner. Lus côté `tests/load/run.sh`.
    load_test_max_vus: int = Field(
        default=100,
        ge=1,
        le=10_000,
        description="Cap VUs simultanés (anti-runaway).",
    )
    load_test_default_duration_seconds: int = Field(
        default=60,
        ge=1,
        le=3600,
        description="Durée par défaut d'un scenario en secondes.",
    )

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def is_development(self) -> bool:
        return self.env == "development"

    @property
    def cors_origins(self) -> list[str]:
        """Parse la liste d'origines CORS depuis la string comma-separated."""
        if self.allowed_origins == "*":
            return ["*"]
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]

    # ── Garde-fous de production ──────────────────────────────
    # Si `ENV=production`, on refuse de démarrer l'API avec des valeurs de dev.
    # Mieux vaut un crash explicite au boot qu'une fuite silencieuse en prod.
    @model_validator(mode="after")
    def _enforce_production_safety(self) -> Settings:
        if not self.is_production:
            return self

        problems: list[str] = []

        # CORS — "*" + allow_credentials=True est un trou béant (CSRF + token theft)
        if self.allowed_origins.strip() == "*":
            problems.append("ALLOWED_ORIGINS=* est interdit en production")

        # Secret d'app — un défaut identifiable = secret cassé
        insecure_secrets = {"", "change-me", "dev-local-secret-change-me-in-production-please"}
        if self.app_secret in insecure_secrets or self.app_secret.startswith("dev-"):
            problems.append("APP_SECRET doit être une valeur aléatoire forte en production")

        # Clés JWT — impossibles à signer sans elles
        if not self.jwt_private_key or not self.jwt_public_key:
            problems.append("JWT_PRIVATE_KEY et JWT_PUBLIC_KEY sont obligatoires en production")

        # Debug — expose les stacks et les détails internes
        if self.debug:
            problems.append("DEBUG=true est interdit en production")

        # Echo SQL — imprime les requêtes (et parfois les paramètres) sur stdout
        if self.db_echo:
            problems.append("DB_ECHO=true est interdit en production")

        # K1 — endpoint /metrics : token obligatoire en prod pour ne pas
        # exposer les KPI métier (compteurs IA, coûts, conversions) au
        # premier scraper venu. En dev, vide = ouvert avec warning.
        if self.prometheus_enabled and not self.prometheus_scrape_token:
            problems.append(
                "PROMETHEUS_SCRAPE_TOKEN est obligatoire en production "
                "(endpoint /metrics ouvert = fuite KPI + DDoS)"
            )

        # K2 — mot de passe admin Grafana : un défaut connu ("admin")
        # ou vide en prod = takeover instantané du dashboard
        # d'observabilité (lecture KPI + édition de panels = fuite
        # business intelligence + sabotage potentiel).
        if self.grafana_admin_password in ("", "admin"):
            problems.append(
                "GRAFANA_ADMIN_PASSWORD doit être un mot de passe fort en "
                "production (vide ou 'admin' interdit)"
            )

        # J1 — liste admin RGPD : un endpoint /rgpd/admin/ai-act-registry
        # sans ACL en prod = fuite catastrophique du registre AI Act
        # complet (tous les appels IA de tous les users avec finalité
        # + base légale + durée de conservation).
        if not self.rgpd_admin_emails:
            problems.append(
                "RGPD_ADMIN_EMAILS doit contenir au moins un email DPO en "
                "production (endpoint /rgpd/admin/* sans ACL = fuite "
                "registre AI Act)"
            )

        if problems:
            joined = "\n  - ".join(problems)
            raise ValueError("Configuration production invalide :\n  - " + joined)
        return self


# Singleton — importé partout via `from app.config import settings`
settings = Settings()
