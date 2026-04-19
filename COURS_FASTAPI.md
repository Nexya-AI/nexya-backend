# Cours complet FastAPI — De zéro à prêt pour NEXYA

> Lis ce cours en entier. Quand tu auras terminé, on branchera Gemini ensemble.

---

## CHAPITRE 1 — C'est quoi FastAPI ?

FastAPI est un framework Python pour créer des **API web**. C'est l'équivalent backend de ce que Flutter est pour le frontend.

**Analogie simple :**
- Flutter = l'interface que l'utilisateur voit et touche
- FastAPI = le serveur qui reçoit les demandes et retourne les réponses

Quand tu tapes un message dans NEXYA et que tu appuies sur Envoyer :
1. Flutter envoie une requête HTTP au serveur
2. FastAPI reçoit cette requête
3. FastAPI appelle Gemini (l'IA)
4. FastAPI retourne la réponse à Flutter
5. Flutter affiche la réponse dans la bulle chat

**Pourquoi FastAPI et pas Django ou Flask ?**
- Plus rapide (async natif — parfait pour le streaming IA)
- Validation automatique des données (Pydantic)
- Documentation auto-générée (Swagger UI)
- Syntaxe moderne et concise

---

## CHAPITRE 2 — Installation

### 2.1 — Prérequis
Tu as besoin de Python 3.10+ installé. Vérifie :
```bash
python --version
```

### 2.2 — Créer un environnement virtuel
Un environnement virtuel isole les packages de ton projet. C'est comme un `pubspec.yaml` mais pour Python.

```bash
# Crée le dossier backend
mkdir nexya_backend
cd nexya_backend

# Crée un environnement virtuel
python -m venv venv

# Active l'environnement (Windows)
venv\Scripts\activate

# Active l'environnement (Mac/Linux)
source venv/bin/activate
```

Quand l'environnement est activé, tu verras `(venv)` au début de ta ligne de commande.

### 2.3 — Installer FastAPI + Uvicorn
```bash
pip install fastapi uvicorn
```

- **fastapi** = le framework
- **uvicorn** = le serveur qui fait tourner ton app (comme `flutter run` mais pour le backend)

---

## CHAPITRE 3 — Ton premier serveur

### 3.1 — Le Hello World

Crée un fichier `main.py` :

```python
from fastapi import FastAPI

app = FastAPI()

@app.get("/")
async def root():
    return {"message": "Bienvenue sur NEXYA API"}
```

**Décortiquons ligne par ligne :**

```python
from fastapi import FastAPI
```
On importe la classe FastAPI. Comme `import 'package:flutter/material.dart';` en Dart.

```python
app = FastAPI()
```
On crée une instance de l'application. C'est ton serveur. Comme `MaterialApp()` en Flutter.

```python
@app.get("/")
```
C'est un **décorateur**. Il dit : "quand quelqu'un fait une requête GET sur `/`, exécute la fonction en dessous". C'est comme une route dans `app_router.dart`.

```python
async def root():
    return {"message": "Bienvenue sur NEXYA API"}
```
La fonction qui s'exécute. Elle retourne un dictionnaire Python qui sera automatiquement converti en JSON.

### 3.2 — Lancer le serveur

```bash
uvicorn main:app --reload
```

- `main` = le fichier `main.py`
- `app` = la variable `app` dans ce fichier
- `--reload` = redémarre automatiquement quand tu modifies le code (comme le hot reload Flutter)

Tu verras :
```
INFO:     Uvicorn running on http://127.0.0.1:8000
```

### 3.3 — Tester

Ouvre ton navigateur et va sur :
- `http://127.0.0.1:8000` → tu vois `{"message": "Bienvenue sur NEXYA API"}`
- `http://127.0.0.1:8000/docs` → tu vois le **Swagger UI** — une interface interactive pour tester tes endpoints

Le Swagger UI, c'est comme Postman mais gratuit et intégré. Tu peux cliquer sur chaque endpoint, envoyer des requêtes et voir les réponses.

---

## CHAPITRE 4 — Les méthodes HTTP

### 4.1 — Les 4 méthodes principales

| Méthode | Usage | Exemple NEXYA |
|---|---|---|
| **GET** | Lire des données | Récupérer l'historique des conversations |
| **POST** | Créer/envoyer des données | Envoyer un message au chat |
| **PUT** | Modifier des données | Renommer une conversation |
| **DELETE** | Supprimer des données | Supprimer un projet |

**Analogie Flutter :**
- GET = `ref.watch(provider)` — tu lis
- POST = `ref.read(provider.notifier).sendMessage()` — tu envoies
- PUT = `ref.read(provider.notifier).rename()` — tu modifies
- DELETE = `ref.read(provider.notifier).delete()` — tu supprimes

### 4.2 — Exemples concrets

```python
from fastapi import FastAPI

app = FastAPI()

# GET — Lire
@app.get("/health")
async def health_check():
    return {"status": "ok"}

# POST — Créer
@app.post("/chat")
async def send_message():
    return {"reply": "Je suis NEXYA !"}

# PUT — Modifier
@app.put("/conversations/123")
async def rename_conversation():
    return {"renamed": True}

# DELETE — Supprimer
@app.delete("/conversations/123")
async def delete_conversation():
    return {"deleted": True}
```

---

## CHAPITRE 5 — Pydantic : valider les données

### 5.1 — Le problème

Sans validation, n'importe qui peut envoyer n'importe quoi à ton API :
```json
{"blabla": 42, "n'importe": "quoi"}
```

Tu veux forcer un format précis. C'est là que **Pydantic** entre en jeu.

### 5.2 — BaseModel

Pydantic, c'est comme définir un modèle Dart/Flutter. Compare :

**Dart (Flutter) :**
```dart
class ChatRequest {
  final String message;
  final String? model;
  ChatRequest({required this.message, this.model});
}
```

**Python (Pydantic) :**
```python
from pydantic import BaseModel

class ChatRequest(BaseModel):
    message: str
    model: str | None = None
```

C'est presque la même chose :
- `str` = `String`
- `int` = `int`
- `float` = `double`
- `bool` = `bool`
- `str | None = None` = `String?` (optionnel)
- `list[str]` = `List<String>`

### 5.3 — Utiliser le modèle dans un endpoint

```python
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

# Modèle de requête (ce que Flutter envoie)
class ChatRequest(BaseModel):
    message: str
    model: str | None = None

# Modèle de réponse (ce que Flutter reçoit)
class ChatResponse(BaseModel):
    reply: str
    tokens_used: int

@app.post("/chat")
async def chat(request: ChatRequest):
    # request.message contient le message de l'utilisateur
    # request.model contient le modèle choisi (ou None)
    
    return ChatResponse(
        reply=f"Tu as dit : {request.message}",
        tokens_used=42
    )
```

**Ce qui se passe :**
1. Flutter envoie `{"message": "Salut", "model": "gemini"}` via Dio
2. FastAPI reçoit le JSON
3. Pydantic vérifie que `message` est bien une `str` — si non, erreur 422 automatique
4. La fonction `chat()` s'exécute
5. FastAPI retourne `{"reply": "Tu as dit : Salut", "tokens_used": 42}`
6. Flutter reçoit la réponse et l'affiche dans `NxChatBubble`

**Si Flutter envoie des données invalides** (ex: `{"message": 123}`) :
FastAPI retourne automatiquement une erreur 422 avec un message clair. Tu n'as rien à coder pour ça.

### 5.4 — Validation avancée

```python
from pydantic import BaseModel, Field

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    model: str | None = Field(default=None, pattern="^(gemini|gemma)$")
```

- `min_length=1` → le message ne peut pas être vide
- `max_length=4000` → le message ne peut pas dépasser 4000 caractères
- `pattern="^(gemini|gemma)$"` → le modèle doit être "gemini" ou "gemma"

---

## CHAPITRE 6 — Paramètres d'URL et de requête

### 6.1 — Path parameters (paramètres dans l'URL)

C'est comme les `:id` dans tes routes GoRouter.

**Flutter (GoRouter) :**
```dart
GoRoute(path: '/projects/:id', ...)
// Accès : state.pathParameters['id']
```

**FastAPI :**
```python
@app.get("/projects/{project_id}")
async def get_project(project_id: str):
    return {"project_id": project_id}
```

Requête : `GET /projects/abc123`
Réponse : `{"project_id": "abc123"}`

### 6.2 — Query parameters (paramètres de recherche)

Ce sont les `?key=value` dans l'URL.

```python
@app.get("/history")
async def get_history(
    page: int = 1,
    limit: int = 20,
    search: str | None = None
):
    return {
        "page": page,
        "limit": limit,
        "search": search
    }
```

Requête : `GET /history?page=2&limit=10&search=python`
Réponse : `{"page": 2, "limit": 10, "search": "python"}`

Les valeurs par défaut (`page: int = 1`) rendent le paramètre optionnel.

---

## CHAPITRE 7 — Codes de statut HTTP

### 7.1 — Les codes importants

| Code | Signification | Quand l'utiliser |
|---|---|---|
| **200** | OK | Tout s'est bien passé (par défaut) |
| **201** | Created | Une ressource a été créée (nouveau projet, etc.) |
| **204** | No Content | Suppression réussie, rien à retourner |
| **400** | Bad Request | La requête est mal formée |
| **401** | Unauthorized | Pas de token JWT ou token invalide |
| **403** | Forbidden | Token valide mais pas les droits |
| **404** | Not Found | La ressource n'existe pas |
| **422** | Unprocessable | Validation Pydantic échouée (automatique) |
| **500** | Server Error | Bug dans ton code |

### 7.2 — Utiliser les codes dans FastAPI

```python
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI()

@app.post("/projects", status_code=201)
async def create_project(request: ProjectRequest):
    # status_code=201 → retourne 201 au lieu de 200
    return {"id": "new_project_id"}

@app.get("/projects/{project_id}")
async def get_project(project_id: str):
    project = find_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Projet introuvable")
    return project
```

**`HTTPException`** c'est comme le `throw` en Dart. Ça interrompt l'exécution et retourne une erreur au client.

---

## CHAPITRE 8 — async/await

### 8.1 — Pourquoi c'est important

Quand NEXYA a 100 utilisateurs en même temps :
- L'utilisateur A envoie un message → attente de Gemini (2 secondes)
- L'utilisateur B envoie un message → **ne doit PAS attendre que A ait fini**

`async/await` permet de gérer plusieurs requêtes en parallèle sans bloquer.

### 8.2 — Comparaison avec Dart

**Dart (Flutter) :**
```dart
Future<String> fetchData() async {
  final response = await dio.get('/data');
  return response.data;
}
```

**Python (FastAPI) :**
```python
async def fetch_data():
    response = await client.get("/data")
    return response.json()
```

C'est presque identique. Tu connais déjà le concept grâce à Flutter.

### 8.3 — Règle simple

| Situation | Utiliser |
|---|---|
| L'endpoint appelle une API externe (Gemini, base de données async) | `async def` |
| L'endpoint fait un calcul simple sans attente | `def` ou `async def` (les deux marchent) |

```python
# Appel à Gemini → async
@app.post("/chat")
async def chat(request: ChatRequest):
    response = await call_gemini(request.message)
    return {"reply": response}

# Simple retour de données → def suffit
@app.get("/health")
def health():
    return {"status": "ok"}
```

---

## CHAPITRE 9 — Structure d'un vrai projet

### 9.1 — Structure de base pour NEXYA

```
nexya_backend/
├── main.py              ← Point d'entrée
├── requirements.txt     ← Dépendances (comme pubspec.yaml)
├── config.py            ← Configuration (clés API, etc.)
├── routers/
│   ├── chat.py          ← Endpoints /chat
│   ├── history.py       ← Endpoints /history
│   └── projects.py      ← Endpoints /projects
├── models/
│   ├── chat.py          ← Pydantic models pour le chat
│   └── project.py       ← Pydantic models pour les projets
└── services/
    ├── gemini.py         ← Appel à l'API Gemini
    └── gemma.py          ← Appel au modèle Gemma local
```

**Analogie avec ton frontend :**
- `routers/` = comme `features/*/screens/` — les points d'entrée
- `models/` = comme `features/*/models/` — la forme des données
- `services/` = comme `features/*/data/` — la logique métier

### 9.2 — Les Routers (organiser les endpoints)

Sans router, tout est dans `main.py`. Avec des routers, on sépare par feature :

**routers/chat.py :**
```python
from fastapi import APIRouter
from models.chat import ChatRequest, ChatResponse

router = APIRouter(prefix="/chat", tags=["Chat"])

@router.post("/")
async def send_message(request: ChatRequest):
    return ChatResponse(reply="Réponse IA ici")

@router.post("/stream")
async def stream_message(request: ChatRequest):
    # Streaming SSE — on verra ça ensemble demain
    pass
```

**main.py :**
```python
from fastapi import FastAPI
from routers import chat, history, projects

app = FastAPI(title="NEXYA API")

app.include_router(chat.router)
app.include_router(history.router)
app.include_router(projects.router)
```

C'est comme `app_router.dart` : tu déclares toutes les routes au même endroit.

---

## CHAPITRE 10 — Middleware et CORS

### 10.1 — C'est quoi un middleware ?

Un middleware intercepte **chaque requête** avant qu'elle arrive à ton endpoint, et **chaque réponse** avant qu'elle parte au client.

**Analogie :** c'est comme les interceptors Dio dans ton frontend (`auth_interceptor.dart`, `retry_interceptor.dart`). Même concept, côté serveur.

### 10.2 — CORS (indispensable pour Flutter)

Par défaut, un serveur refuse les requêtes venant d'une autre origine (ton app Flutter). CORS autorise ces requêtes.

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # En production, limiter aux domaines autorisés
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

Sans ça, Flutter recevra une erreur `CORS policy` et rien ne marchera.

### 10.3 — Middleware personnalisé

```python
import time
from fastapi import Request

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    duration = time.time() - start
    print(f"{request.method} {request.url.path} → {response.status_code} ({duration:.2f}s)")
    return response
```

Ce middleware affiche dans la console :
```
POST /chat → 200 (1.34s)
GET /history → 200 (0.02s)
```

---

## CHAPITRE 11 — Dépendances (Dependency Injection)

### 11.1 — Le concept

Les dépendances dans FastAPI, c'est comme les `Provider` dans Riverpod. Au lieu de créer un objet à chaque fois, tu le déclares une fois et FastAPI l'injecte automatiquement.

### 11.2 — Exemple concret : vérifier l'authentification

```python
from fastapi import Depends, HTTPException, Header

async def get_current_user(authorization: str = Header(None)):
    """Vérifie le token JWT et retourne l'utilisateur."""
    if authorization is None:
        raise HTTPException(status_code=401, detail="Token manquant")
    
    token = authorization.replace("Bearer ", "")
    user = decode_jwt(token)  # ta fonction de décodage
    
    if user is None:
        raise HTTPException(status_code=401, detail="Token invalide")
    
    return user

# Endpoint protégé — l'utilisateur est injecté automatiquement
@app.get("/profile")
async def get_profile(user = Depends(get_current_user)):
    return {"username": user.username, "email": user.email}

# Endpoint public — pas de Depends
@app.get("/health")
async def health():
    return {"status": "ok"}
```

**Comparaison Flutter :**
```dart
// Riverpod — tu lis l'état auth
final user = ref.watch(authProvider);
if (user == null) context.go('/login');
```

```python
# FastAPI — tu injectes l'utilisateur via Depends
async def endpoint(user = Depends(get_current_user)):
    # Si le token est invalide, l'erreur 401 est levée automatiquement
    # Tu n'arrives ici que si l'utilisateur est authentifié
```

---

## CHAPITRE 12 — Streaming SSE (Server-Sent Events)

### 12.1 — Pourquoi le streaming ?

Sans streaming :
- L'utilisateur envoie un message
- Il attend 3-5 secondes (rien ne se passe)
- La réponse complète apparaît d'un coup

Avec streaming :
- L'utilisateur envoie un message
- Les mots apparaissent **un par un** en temps réel
- Exactement comme ChatGPT

C'est ce que ton `ChatStreamMixin` fait actuellement avec `Timer.periodic` (fake). On va le remplacer par du vrai SSE.

### 12.2 — Comment ça marche

```
Flutter                    FastAPI                    Gemini
  |                           |                          |
  |--- POST /chat/stream ---->|                          |
  |                           |--- appel streaming ----->|
  |                           |                          |
  |<-- data: "Bonjour"  -----|<-- chunk "Bonjour" ------|
  |<-- data: " je"       -----|<-- chunk " je" ---------|
  |<-- data: " suis"     -----|<-- chunk " suis" -------|
  |<-- data: " NEXYA"    -----|<-- chunk " NEXYA" ------|
  |<-- data: [DONE]      -----|                          |
  |                           |                          |
```

Chaque `data:` est un chunk envoyé dès qu'il est disponible. Flutter le reçoit et l'ajoute à la bulle en cours.

### 12.3 — Code FastAPI pour le streaming

```python
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import asyncio

app = FastAPI()

class ChatRequest(BaseModel):
    message: str

async def generate_response(message: str):
    """Simule un streaming (sera remplacé par Gemini)."""
    response = f"Tu m'as dit : {message}. Voici ma réponse détaillée."
    
    for word in response.split(" "):
        yield f"data: {word} \n\n"
        await asyncio.sleep(0.1)  # Simule le délai de l'IA
    
    yield "data: [DONE]\n\n"

@app.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    return StreamingResponse(
        generate_response(request.message),
        media_type="text/event-stream"
    )
```

**Décortiquons :**

```python
async def generate_response(message: str):
```
C'est un **générateur asynchrone** (`yield` au lieu de `return`). Chaque `yield` envoie un morceau de la réponse sans attendre la fin.

```python
yield f"data: {word} \n\n"
```
Le format SSE : `data:` suivi du contenu, puis deux sauts de ligne. C'est le protocole standard.

```python
StreamingResponse(..., media_type="text/event-stream")
```
Dit à Flutter que la réponse arrive en flux continu (pas en une seule fois).

### 12.4 — Côté Flutter (comment recevoir)

Ton `ChatStreamMixin` actuel utilise `Timer.periodic`. Avec le vrai streaming, ça ressemblera à :

```dart
final response = await dio.post(
  '/chat/stream',
  data: {'message': text},
  options: Options(responseType: ResponseType.stream),
);

final stream = response.data.stream;
await for (final chunk in stream) {
  final text = utf8.decode(chunk);
  // Extraire le contenu après "data: "
  // Ajouter au message en cours
}
```

On fera ça ensemble quand le backend tournera.

---

## CHAPITRE 13 — Gestion des erreurs

### 13.1 — Try/except (équivalent de try/catch en Dart)

**Dart :**
```dart
try {
  final response = await dio.get('/data');
} catch (e) {
  print('Erreur : $e');
}
```

**Python :**
```python
try:
    response = await client.get("/data")
except Exception as e:
    print(f"Erreur : {e}")
```

### 13.2 — Gestion d'erreurs dans FastAPI

```python
from fastapi import FastAPI, HTTPException

@app.post("/chat")
async def chat(request: ChatRequest):
    try:
        response = await call_gemini(request.message)
        return {"reply": response}
    except TimeoutError:
        raise HTTPException(
            status_code=504,
            detail="Gemini n'a pas répondu à temps"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Erreur interne : {str(e)}"
        )
```

### 13.3 — Gestionnaire d'erreurs global

```python
from fastapi import Request
from fastapi.responses import JSONResponse

@app.exception_handler(Exception)
async def global_error_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"detail": "Une erreur interne est survenue"}
    )
```

---

## CHAPITRE 14 — Variables d'environnement

### 14.1 — Ne jamais mettre les clés API dans le code

```python
# MAUVAIS — ta clé API visible dans le code
GEMINI_API_KEY = "AIzaSyD..."

# BON — dans un fichier .env
```

### 14.2 — Utiliser python-dotenv

```bash
pip install python-dotenv
```

Crée un fichier `.env` à la racine :
```env
GEMINI_API_KEY=AIzaSyD...
DATABASE_URL=postgresql://user:pass@localhost/nexya
DEBUG=true
```

Dans ton code :
```python
import os
from dotenv import load_dotenv

load_dotenv()  # Charge le fichier .env

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
DEBUG = os.getenv("DEBUG", "false").lower() == "true"
```

**Important :** ajoute `.env` dans `.gitignore` pour ne jamais le pousser sur GitHub.

C'est exactement comme `AppConfig.baseUrl` dans ton frontend — jamais de valeur hardcodée.

---

## CHAPITRE 15 — Résumé des équivalences Flutter ↔ FastAPI

| Concept Flutter | Équivalent FastAPI |
|---|---|
| `MaterialApp()` | `FastAPI()` |
| `GoRoute(path: '/chat')` | `@app.post("/chat")` |
| `app_router.dart` | `main.py` + `routers/` |
| `class ChatModel` (Dart) | `class ChatModel(BaseModel)` (Pydantic) |
| `state.pathParameters['id']` | `def endpoint(id: str)` |
| `ref.watch(provider)` | `Depends(dependency)` |
| `Dio` (client HTTP) | `httpx` ou `aiohttp` (client HTTP) |
| `auth_interceptor.dart` | `@app.middleware("http")` |
| `flutter run` | `uvicorn main:app --reload` |
| `pubspec.yaml` | `requirements.txt` |
| `flutter pub get` | `pip install -r requirements.txt` |
| `Future<T> async/await` | `async def / await` |
| `try/catch` | `try/except` |
| `throw Exception()` | `raise HTTPException()` |
| `const` widget | Pas d'équivalent (Python gère la mémoire seul) |

---

## CHAPITRE 16 — Exercice pratique

### L'objectif

Crée un mini serveur FastAPI avec 4 endpoints qui simulent NEXYA :

### Le code à écrire toi-même

```python
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uuid
from datetime import datetime

app = FastAPI(title="NEXYA API")

# CORS — pour que Flutter puisse appeler ce serveur
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── MODÈLES ─────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)

class ChatResponse(BaseModel):
    id: str
    reply: str
    created_at: str

class ProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = None

class ProjectResponse(BaseModel):
    id: str
    name: str
    description: str | None
    created_at: str

# ─── STOCKAGE EN MÉMOIRE (temporaire) ────────────────

projects: list[dict] = []

# ─── ENDPOINTS ────────────────────────────────────────

# 1. Health check
@app.get("/health")
async def health():
    return {"status": "ok", "service": "nexya-api"}

# 2. Chat (réponse simple, pas encore de streaming)
@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    return ChatResponse(
        id=str(uuid.uuid4()),
        reply=f"[NEXYA] Tu as dit : {request.message}",
        created_at=datetime.now().isoformat()
    )

# 3. Créer un projet
@app.post("/projects", response_model=ProjectResponse, status_code=201)
async def create_project(request: ProjectRequest):
    project = {
        "id": str(uuid.uuid4()),
        "name": request.name,
        "description": request.description,
        "created_at": datetime.now().isoformat()
    }
    projects.append(project)
    return ProjectResponse(**project)

# 4. Lister les projets
@app.get("/projects", response_model=list[ProjectResponse])
async def list_projects():
    return [ProjectResponse(**p) for p in projects]

# 5. Récupérer un projet par ID
@app.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: str):
    for p in projects:
        if p["id"] == project_id:
            return ProjectResponse(**p)
    raise HTTPException(status_code=404, detail="Projet introuvable")
```

### Comment tester

1. Lance le serveur : `uvicorn main:app --reload`
2. Ouvre `http://127.0.0.1:8000/docs`
3. Teste chaque endpoint dans le Swagger UI :
   - `GET /health` → doit retourner `{"status": "ok"}`
   - `POST /chat` → envoie `{"message": "Salut"}` → reçois la réponse
   - `POST /projects` → crée un projet `{"name": "Mon projet"}`
   - `GET /projects` → liste tes projets créés
   - `GET /projects/{id}` → récupère un projet spécifique

Si tout ça marche, tu es prêt pour brancher Gemini demain.

---

## CHAPITRE 17 — Checklist avant de passer à Gemini

Coche mentalement chaque point :

- [ ] Je sais créer un fichier `main.py` avec `FastAPI()`
- [ ] Je sais lancer le serveur avec `uvicorn`
- [ ] Je sais écrire un endpoint GET et POST
- [ ] Je sais définir un modèle Pydantic (`BaseModel`)
- [ ] Je sais utiliser les path parameters (`/projects/{id}`)
- [ ] Je sais lever une erreur (`HTTPException`)
- [ ] Je comprends `async/await` en Python
- [ ] Je sais ce qu'est le CORS et pourquoi c'est nécessaire
- [ ] Je sais ce que fait le streaming SSE (chapitre 12)
- [ ] J'ai testé mon exercice dans le Swagger UI

**Quand tout est coché, reviens ici. On branche Gemini.**
