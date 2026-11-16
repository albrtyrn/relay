def test_health_returns_200_when_db_up(client):
    """Guarantee: /health reports ok when relay_db answers `SELECT 1`.

    Invariant: none of the six; this is the liveness check DESIGN.md section 1 #19 defines.
    """
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_returns_503_when_db_down(broken_client):
    """Guarantee: /health returns 503, not a crash or a false 200, when relay_db is unreachable.

    Invariant: none of the six; DESIGN.md section 1 #19.
    """
    response = broken_client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
