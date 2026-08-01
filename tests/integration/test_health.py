"""The health endpoint.

Asserts the exact body, not merely a non-5xx status. A liveness probe that only
checks "not 500" passes against a 404, which is how a misrouted service stays
green in a load balancer.
"""

from fastapi.testclient import TestClient


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
