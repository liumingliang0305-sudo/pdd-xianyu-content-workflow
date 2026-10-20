from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.main import create_app
from app.storage import Store

TOKEN = "fixture-only-token-not-a-real-secret"


def test_authenticated_import_idempotency_and_resume(tmp_path, source_url):
    app = create_app(tmp_path, TOKEN, start_worker=False)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/jobs").status_code == 401
        assert client.post("/api/session", json={"token": "wrong"}).status_code == 401
        assert client.post("/api/session", json={"token": TOKEN}).status_code == 200
        assert client.post("/api/jobs", json={"text": source_url}).status_code == 422
        headers = {"Idempotency-Key": "fixture-1"}
        first = client.post("/api/jobs", headers=headers, json={"text": source_url})
        assert first.status_code == 202
        job = first.json()
        again = client.post("/api/jobs", headers=headers, json={"text": source_url})
        assert again.json()["id"] == job["id"]
        assert len(client.get("/api/jobs").json()) == 1
        assert (
            client.post("/api/jobs", headers=headers, json={"text": source_url + "2"}).status_code
            == 422
        )
        assert client.post("/api/jobs/" + job["id"] + "/resume").status_code == 409
        assert client.get("/api/jobs/" + job["id"] + "/download").status_code == 409
        assert client.get("/api/jobs/" + job["id"] + "/images/0").status_code == 404
        app.state.store.update(job["id"], status="failed")
        resumed = client.post("/api/jobs/" + job["id"] + "/resume").json()
        assert resumed["attempt"] == 2
        assert resumed["status"] == "queued"
        assert (
            client.post(
                "/api/jobs", headers={"Origin": "https://evil.test"}, json={"text": source_url}
            ).status_code
            == 403
        )
        assert client.post("/api/jobs", content=b"x" * 33000).status_code == 413
        client.delete("/api/session")
        assert client.get("/api/jobs").status_code == 401


def test_restart_and_atomic_claim(tmp_path, source_url):
    store = Store(tmp_path)
    job = store.create("1", source_url)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: store.claim(), range(4)))
    assert sum(result is not None for result in results) == 1
    restarted = Store(tmp_path)
    restarted.recover()
    assert restarted.get(job["id"])["status"] == "interrupted"
    assert restarted.resume(job["id"])["status"] == "queued"
    assert restarted.claim()["id"] == job["id"]
