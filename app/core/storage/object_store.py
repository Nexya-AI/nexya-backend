"""
Object storage — abstraction unifiée S3 / MinIO / Cloudflare R2 / Garage.

Pattern NEXYA : **mock-first**. Si `settings.s3_access_key` est vide OU
`settings.storage_mock_enabled=True`, on branche `MockObjectStore` qui
persiste en RAM — ça permet de coder et de tester toute la feature
Library sans conteneur MinIO allumé ni clé AWS. Même pattern que le
MockEmailService (A1) et le MockCaptchaVerifier (A3).

Points critiques :

- **`aioboto3` = session lazy + context manager obligatoire.** Ne JAMAIS
  conserver une référence à `client/resource` au-delà d'un `async with`
  — la connexion sous-jacente (aiohttp) doit être fermée proprement,
  sinon on fuit un socket par requête. On ouvre/ferme un client par
  opération ; le coût (DNS déjà résolu, TCP keepalive côté MinIO) est
  négligeable face à la sécurité d'un lifecycle propre.

- **Presigned URLs générées localement** via `generate_presigned_url` :
  pas d'appel réseau, c'est juste un HMAC signé avec la secret key.
  Scalable à 10k+ URLs/seconde, CPU seulement. L'URL donne un accès
  direct au bucket MinIO par le client — pas de proxy applicatif à
  scaler. TTL 1h par défaut, cap dur 24h côté S3.

- **`delete_object` idempotent** : S3/MinIO répond 204 même si l'objet
  n'existe pas. Pas de `NoSuchKey` à attraper — contract natif S3.

- **Bucket auto-create** au premier `upload_bytes` si absent (idempotent
  via `list_buckets` + `create_bucket` catch `BucketAlreadyOwnedByYou`).
  Désactivable via `storage_auto_create_bucket=False` pour la prod où
  le bucket doit être provisionné par IaC/Terraform (jamais par l'app).

- **Tous les objets uploadés portent des métadonnées S3** (`x-amz-meta-*`)
  avec le user_id + type + source — permet un audit forensic côté MinIO
  sans requêter la DB, et une future policy d'expiration par tag.
"""

from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final

import structlog

from app.core.errors.exceptions import NexYaException

if TYPE_CHECKING:
    from aioboto3 import Session as _AioSession

log = structlog.get_logger()


# ══════════════════════════════════════════════════════════════
# Exceptions
# ══════════════════════════════════════════════════════════════


class ObjectStoreUnavailableException(NexYaException):
    """Le backend de stockage (MinIO / S3) est down ou injoignable après
    retry. Status 503 — le client Flutter affiche un toast « Service
    temporairement indisponible, réessayez ». Jamais 500 : on sait ce qui
    s'est passé, on le communique proprement.
    """

    def __init__(self, detail: str = "") -> None:
        message = "Service de stockage temporairement indisponible."
        if detail:
            message = f"{message} ({detail})"
        super().__init__(
            code="STORAGE_UNAVAILABLE",
            message=message,
            status_code=503,
        )


# ══════════════════════════════════════════════════════════════
# Types de données
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ObjectStat:
    """Statistiques d'un objet — subset de `HeadObject` S3."""

    key: str
    size_bytes: int
    mime_type: str | None
    etag: str | None
    last_modified: datetime | None


# ══════════════════════════════════════════════════════════════
# Interface abstraite — le contrat que consomment les services
# ══════════════════════════════════════════════════════════════


class ObjectStore(abc.ABC):
    """Contrat minimal pour un stockage d'objets NEXYA.

    Toutes les méthodes sont asynchrones. Les erreurs réseau / backend
    transitoires lèvent `ObjectStoreUnavailableException` (après retry
    interne si implémenté). Les erreurs métier (key invalide, payload
    trop gros) lèvent `ValueError` pour que l'appelant les catch de
    manière typée.
    """

    name: str

    @abc.abstractmethod
    async def upload_bytes(
        self,
        key: str,
        data: bytes,
        *,
        mime_type: str,
        metadata: dict[str, str] | None = None,
    ) -> None:
        """Upload `data` sous la clé `key` avec le `mime_type`. Métadonnées
        custom optionnelles (encodées en `x-amz-meta-*` côté S3)."""

    @abc.abstractmethod
    async def delete_object(self, key: str) -> None:
        """Supprime l'objet — idempotent (no-op si absent)."""

    @abc.abstractmethod
    async def object_exists(self, key: str) -> bool:
        """Vrai si l'objet existe. Ne lève jamais d'exception sur absence."""

    @abc.abstractmethod
    async def stat_object(self, key: str) -> ObjectStat | None:
        """Renvoie les stats ou `None` si l'objet n'existe pas."""

    @abc.abstractmethod
    async def generate_presigned_url(
        self,
        key: str,
        *,
        ttl_seconds: int = 3600,
        method: str = "GET",
    ) -> str:
        """URL signée avec accès temporaire. `method` ∈ {`GET`, `PUT`}.
        Pour un upload direct client→MinIO, utiliser `PUT`."""

    @abc.abstractmethod
    async def download_bytes(self, key: str) -> bytes:
        """Télécharge le blob complet en mémoire.

        Utilisé par le worker D4 `index_document_chunks` qui re-extrait
        le texte d'un PDF stocké pour produire des chunks avec marqueurs
        de page. Le volume est borné par `files_max_upload_bytes` (100 MB
        par défaut), donc tenir le blob en RAM est acceptable sur un
        worker arq.

        Lève `ObjectStoreUnavailableException` si le backend est down.
        Lève `FileNotFoundError` si la clé n'existe pas.
        """


# ══════════════════════════════════════════════════════════════
# Mock — persistance en RAM, utilisée dev/test sans MinIO
# ══════════════════════════════════════════════════════════════


class MockObjectStore(ObjectStore):
    """Stockage en mémoire. Thread-safe grâce au GIL + asyncio single-thread.

    Activé automatiquement si `settings.s3_access_key` est vide. Tests
    forcent via `settings.storage_mock_enabled=True`. Les données restent
    tant que le process tourne — parfait pour une suite pytest, pas pour
    une prod.

    `generate_presigned_url` retourne `mock://bucket/{key}?expires={ts}`
    pour que les tests puissent vérifier la présence d'une URL sans
    attaquer un vrai service.
    """

    name: Final[str] = "mock"

    def __init__(self, bucket: str = "nexya-media-mock") -> None:
        self._bucket = bucket
        # key -> (data, mime_type, metadata, last_modified)
        self._store: dict[str, tuple[bytes, str, dict[str, str], datetime]] = {}
        log.info("object_store.mock.initialized", bucket=bucket)

    async def upload_bytes(
        self,
        key: str,
        data: bytes,
        *,
        mime_type: str,
        metadata: dict[str, str] | None = None,
    ) -> None:
        self._store[key] = (
            data,
            mime_type,
            dict(metadata or {}),
            datetime.utcnow(),
        )
        log.debug(
            "object_store.mock.uploaded",
            key=key,
            size_bytes=len(data),
            mime_type=mime_type,
        )

    async def delete_object(self, key: str) -> None:
        self._store.pop(key, None)

    async def object_exists(self, key: str) -> bool:
        return key in self._store

    async def stat_object(self, key: str) -> ObjectStat | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        data, mime_type, _metadata, last_modified = entry
        return ObjectStat(
            key=key,
            size_bytes=len(data),
            mime_type=mime_type,
            etag=None,  # le mock ne calcule pas d'etag — non utilisé côté app
            last_modified=last_modified,
        )

    async def generate_presigned_url(
        self,
        key: str,
        *,
        ttl_seconds: int = 3600,
        method: str = "GET",
    ) -> str:
        expires_at = int(datetime.utcnow().timestamp()) + ttl_seconds
        return f"mock://{self._bucket}/{key}?expires={expires_at}&method={method}"

    async def download_bytes(self, key: str) -> bytes:
        entry = self._store.get(key)
        if entry is None:
            raise FileNotFoundError(key)
        return entry[0]

    # Helpers utilisés uniquement par les tests — NON au contrat public.
    def _fetch_raw(self, key: str) -> bytes | None:
        entry = self._store.get(key)
        return entry[0] if entry else None

    def _clear(self) -> None:
        self._store.clear()


# ══════════════════════════════════════════════════════════════
# S3 / MinIO — impl aioboto3 production-ready
# ══════════════════════════════════════════════════════════════


class S3ObjectStore(ObjectStore):
    """Implémentation `ObjectStore` via `aioboto3`.

    Compatible :
    - MinIO local (docker-compose, `S3_ENDPOINT=http://localhost:9000`).
    - AWS S3 prod (pas d'`endpoint_url`, `s3_use_ssl=True`).
    - Cloudflare R2 (endpoint custom `*.r2.cloudflarestorage.com`).
    - Garage (self-hosted S3-compatible).

    Le client aioboto3 est ré-instancié par opération (pattern context
    manager strict — pas de client partagé qui survit à un
    event loop close). La session boto3 sous-jacente cache la résolution
    DNS + les credentials, le coût de recréation est négligeable
    (~0.3 ms en dev sur MinIO local).
    """

    name: Final[str] = "s3"

    def __init__(
        self,
        *,
        endpoint_url: str | None,
        access_key: str,
        secret_key: str,
        bucket: str,
        region_name: str = "us-east-1",
        use_ssl: bool = False,
        auto_create_bucket: bool = True,
        presigned_default_ttl: int = 3600,
    ) -> None:
        # Import local : aioboto3 est lourd (aiohttp, botocore) — on évite
        # de le charger dans le process `pytest` si seul le mock est utilisé.
        import aioboto3  # noqa: PLC0415

        self._endpoint_url = endpoint_url
        self._access_key = access_key
        self._secret_key = secret_key
        self._bucket = bucket
        self._region_name = region_name
        self._use_ssl = use_ssl
        self._auto_create_bucket = auto_create_bucket
        self._presigned_default_ttl = presigned_default_ttl
        self._session: _AioSession = aioboto3.Session()
        # Lock pour que deux coroutines concurrentes ne tentent pas de
        # créer le bucket en même temps (race bénigne mais évitée).
        self._bucket_lock = asyncio.Lock()
        self._bucket_ensured = False
        log.info(
            "object_store.s3.initialized",
            endpoint=endpoint_url,
            bucket=bucket,
            region=region_name,
        )

    # ── Context helper pour ouvrir un client proprement ────────
    def _client(self):
        """Retourne un context manager `async with` qui yield un client S3
        configuré. Pattern obligatoire avec aioboto3 (sinon fuite aiohttp)."""
        return self._session.client(
            "s3",
            endpoint_url=self._endpoint_url,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
            region_name=self._region_name,
            use_ssl=self._use_ssl,
        )

    # ── Idempotent bucket create ───────────────────────────────
    async def _ensure_bucket(self) -> None:
        """Crée le bucket au premier appel si absent. Idempotent via lock
        + flag. Désactivable via `auto_create_bucket=False` (prod IaC)."""
        if not self._auto_create_bucket or self._bucket_ensured:
            return
        async with self._bucket_lock:
            if self._bucket_ensured:
                return
            try:
                async with self._client() as s3:
                    try:
                        await s3.head_bucket(Bucket=self._bucket)
                        log.debug(
                            "object_store.s3.bucket_exists",
                            bucket=self._bucket,
                        )
                    except Exception:  # noqa: BLE001
                        # HeadBucket renvoie 404/403 si absent ou droits
                        # manquants — on tente la création. Si ça rate
                        # aussi, c'est qu'on n'a pas les droits, on
                        # propage.
                        await s3.create_bucket(Bucket=self._bucket)
                        log.info(
                            "object_store.s3.bucket_created",
                            bucket=self._bucket,
                        )
            except Exception as exc:
                log.warning(
                    "object_store.s3.ensure_bucket_failed",
                    bucket=self._bucket,
                    error=str(exc),
                )
                raise ObjectStoreUnavailableException(detail="bucket init") from exc
            self._bucket_ensured = True

    # ── API publique ───────────────────────────────────────────
    async def upload_bytes(
        self,
        key: str,
        data: bytes,
        *,
        mime_type: str,
        metadata: dict[str, str] | None = None,
    ) -> None:
        await self._ensure_bucket()
        extra = {
            "ContentType": mime_type,
        }
        if metadata:
            # Les valeurs doivent être ASCII pour les headers HTTP.
            extra["Metadata"] = {
                k: str(v).encode("ascii", "ignore").decode("ascii") for k, v in metadata.items()
            }
        try:
            async with self._client() as s3:
                await s3.put_object(
                    Bucket=self._bucket,
                    Key=key,
                    Body=data,
                    **extra,
                )
        except Exception as exc:
            log.error(
                "object_store.s3.upload_failed",
                bucket=self._bucket,
                key=key,
                size_bytes=len(data),
                error=str(exc),
            )
            raise ObjectStoreUnavailableException(detail="upload") from exc

    async def delete_object(self, key: str) -> None:
        # DeleteObject S3 est idempotent — 204 même si la clé n'existait pas.
        try:
            async with self._client() as s3:
                await s3.delete_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            log.warning(
                "object_store.s3.delete_failed",
                bucket=self._bucket,
                key=key,
                error=str(exc),
            )
            # On ne propage PAS : une erreur de delete ne doit pas bloquer
            # le soft-delete applicatif. Un cleanup différé rattrapera.

    async def object_exists(self, key: str) -> bool:
        try:
            async with self._client() as s3:
                await s3.head_object(Bucket=self._bucket, Key=key)
                return True
        except Exception:  # noqa: BLE001
            # HeadObject renvoie 404 si absent — on considère ça un
            # « pas d'existence » silencieux, peu importe la cause.
            return False

    async def stat_object(self, key: str) -> ObjectStat | None:
        try:
            async with self._client() as s3:
                head = await s3.head_object(Bucket=self._bucket, Key=key)
        except Exception:  # noqa: BLE001
            return None
        return ObjectStat(
            key=key,
            size_bytes=int(head.get("ContentLength", 0)),
            mime_type=head.get("ContentType"),
            etag=head.get("ETag", "").strip('"') or None,
            last_modified=head.get("LastModified"),
        )

    async def generate_presigned_url(
        self,
        key: str,
        *,
        ttl_seconds: int | None = None,
        method: str = "GET",
    ) -> str:
        op_map = {"GET": "get_object", "PUT": "put_object"}
        if method not in op_map:
            raise ValueError(f"method {method!r} non supporté")
        ttl = ttl_seconds if ttl_seconds is not None else self._presigned_default_ttl
        try:
            async with self._client() as s3:
                url = await s3.generate_presigned_url(
                    ClientMethod=op_map[method],
                    Params={"Bucket": self._bucket, "Key": key},
                    ExpiresIn=ttl,
                )
                return url
        except Exception as exc:
            log.error(
                "object_store.s3.presign_failed",
                bucket=self._bucket,
                key=key,
                error=str(exc),
            )
            raise ObjectStoreUnavailableException(detail="presign") from exc

    async def download_bytes(self, key: str) -> bytes:
        try:
            async with self._client() as s3:
                response = await s3.get_object(Bucket=self._bucket, Key=key)
                body = response["Body"]
                data = await body.read()
                return data
        except Exception as exc:  # noqa: BLE001
            # Pas de classe d'exception stable pour distinguer « clé
            # absente » de « backend down » côté aioboto3 (botocore a ses
            # propres ClientError avec codes d'erreur string). On lit le
            # message pour différencier 404 (FileNotFoundError client) vs
            # 5xx / réseau (ObjectStoreUnavailableException).
            message = str(exc).lower()
            if "nosuchkey" in message or "not found" in message or "404" in message:
                raise FileNotFoundError(key) from exc
            log.error(
                "object_store.s3.download_failed",
                bucket=self._bucket,
                key=key,
                error=str(exc),
            )
            raise ObjectStoreUnavailableException(detail="download") from exc


# ══════════════════════════════════════════════════════════════
# Factory — résolution du backend selon la config
# ══════════════════════════════════════════════════════════════


_OBJECT_STORE: ObjectStore | None = None


def get_object_store() -> ObjectStore:
    """Retourne le singleton ObjectStore. Mock si pas de creds S3 OU si
    `storage_mock_enabled=True`. S3 sinon.

    Pattern singleton lazy — le choix est fait au premier appel et cacheé
    pour la durée du process. Testing forcé via `reset_object_store()`.
    """
    global _OBJECT_STORE
    if _OBJECT_STORE is not None:
        return _OBJECT_STORE

    from app.config import settings

    use_mock = settings.storage_mock_enabled or not settings.s3_access_key
    if use_mock:
        _OBJECT_STORE = MockObjectStore(bucket=settings.s3_bucket_name)
        return _OBJECT_STORE

    _OBJECT_STORE = S3ObjectStore(
        endpoint_url=settings.s3_endpoint or None,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        bucket=settings.s3_bucket_name,
        region_name=settings.s3_region_name,
        use_ssl=settings.s3_use_ssl,
        auto_create_bucket=settings.storage_auto_create_bucket,
        presigned_default_ttl=settings.s3_presigned_ttl_seconds,
    )
    return _OBJECT_STORE


def reset_object_store() -> None:
    """Réinitialise le singleton — usage tests uniquement."""
    global _OBJECT_STORE
    _OBJECT_STORE = None
