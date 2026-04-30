"""
Tests MockFCMProvider — F2.

Couvre :
- send_push happy-path retourne success + message_id déterministe en forme
- accumulation de `calls` pour assertions tests
- force_fail raise FCMUnavailableError
- force_unregistered raise FCMUnregisteredError
- data dict préservé tel quel (strings only)
- mock message_id prefixé "mock-"
"""

from __future__ import annotations

import pytest

from app.ai.fcm import (
    FCMResult,
    FCMUnavailableError,
    FCMUnregisteredError,
    MockFCMProvider,
)


@pytest.mark.asyncio
async def test_mock_send_push_happy_path():
    provider = MockFCMProvider()
    result = await provider.send_push(
        "tok-1",
        title="hello",
        body="world",
        data={"k": "v"},
    )
    assert isinstance(result, FCMResult)
    assert result.success is True
    assert result.message_id is not None
    assert result.message_id.startswith("mock-")


@pytest.mark.asyncio
async def test_mock_accumulates_calls():
    provider = MockFCMProvider()
    await provider.send_push("tok-A", title="T1", body="B1", data={"x": "1"})
    await provider.send_push("tok-B", title="T2", body="B2")
    assert len(provider.calls) == 2
    assert provider.calls[0]["token"] == "tok-A"
    assert provider.calls[0]["title"] == "T1"
    assert provider.calls[0]["data"] == {"x": "1"}
    assert provider.calls[1]["token"] == "tok-B"
    assert provider.calls[1]["data"] == {}  # None data devient {}


@pytest.mark.asyncio
async def test_mock_force_fail_raises_unavailable():
    provider = MockFCMProvider(force_fail=True)
    with pytest.raises(FCMUnavailableError):
        await provider.send_push("tok-x", title="t", body="b")


@pytest.mark.asyncio
async def test_mock_force_unregistered_raises():
    provider = MockFCMProvider(force_unregistered=True)
    with pytest.raises(FCMUnregisteredError) as exc_info:
        await provider.send_push("tok-expired", title="t", body="b")
    assert exc_info.value.token == "tok-expired"


@pytest.mark.asyncio
async def test_mock_data_strings_preserved():
    provider = MockFCMProvider()
    data = {"task_id": "abc-123", "deep_link": "nexya://task/abc"}
    await provider.send_push("tok", title="t", body="b", data=data)
    assert provider.calls[0]["data"] == data


def test_mock_identity():
    provider = MockFCMProvider()
    assert provider.name == "mock-fcm"
