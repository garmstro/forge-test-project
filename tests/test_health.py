"""Test health endpoint with scheduler status."""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_health_endpoint_shows_scheduler_status(client):
    """Test that health endpoint returns scheduler status."""
    response = await client.get("/health")
    
    assert response.status_code == 200
    data = response.json()
    
    # Verify structure
    assert "status" in data
    assert "db" in data
    assert "scheduler" in data
    assert "version" in data
    
    # The scheduler may or may not be running in tests
    # but the field should be present
    assert data["scheduler"] in ["running", "not_started"]
