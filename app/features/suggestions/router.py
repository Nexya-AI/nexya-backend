"""Router Suggestions — `POST /suggestions` (Session N1).

Endpoint unique : formulaire user → équipe NEXYA. Délègue à
`SuggestionService.submit` qui INSERT + envoie l'email équipe
(fail-safe).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.features.auth.models import User
from app.features.suggestions.schemas import (
    SuggestionCreate,
    SuggestionResponse,
)
from app.features.suggestions.service import SuggestionService
from app.shared.schemas import NexyaResponse

router = APIRouter(prefix="/suggestions", tags=["suggestions"])


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def _user_agent(request: Request) -> str | None:
    ua = request.headers.get("user-agent")
    return ua.strip() if ua else None


@router.post(
    "",
    response_model=NexyaResponse[SuggestionResponse],
    status_code=status.HTTP_201_CREATED,
)
async def submit_suggestion(
    body: SuggestionCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[SuggestionResponse]:
    """Soumet une suggestion utilisateur → équipe NEXYA.

    Rate limit 5/jour/user (anti-spam). Email envoyé à
    `feedback_team_email` (Mock en dev, Brevo en prod).
    """
    suggestion = await SuggestionService.submit(
        current_user,
        body,
        ip=_client_ip(request),
        user_agent=_user_agent(request),
        db=db,
    )
    return NexyaResponse(success=True, data=SuggestionResponse.model_validate(suggestion))
