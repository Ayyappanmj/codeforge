"""API tests with FastAPI TestClient + in-memory SQLite. Requires fastapi, httpx, sqlalchemy."""
import unittest

import tests  # noqa: F401
from tests.support.db import HAVE_FASTAPI, HAVE_SQLALCHEMY

REASON = "NOT RUN: fastapi/httpx/sqlalchemy are not installed in this environment"


@unittest.skipUnless(HAVE_FASTAPI and HAVE_SQLALCHEMY, REASON)
class ApiTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from codeforge_api.app import create_app
        from codeforge_shared.config import Settings
        from tests.support.db import sqlite_store
        self.store = sqlite_store()
        self.settings = Settings.from_env({})
        self.app = create_app(self.settings, self.store, rabbit_check=lambda: True)
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def submit(self, **over):
        body = {"language": "python", "source_code": "print('hi')", "stdin": ""}
        body.update(over)
        return self.client.post("/api/v1/executions", json=body)

    def test_valid_submission(self):
        r = self.submit()
        self.assertEqual(r.status_code, 202)
        eid = r.json()["execution_id"]
        self.assertEqual(r.json()["status"], "QUEUED")
        g = self.client.get(f"/api/v1/executions/{eid}")
        self.assertEqual(g.status_code, 200)
        self.assertEqual(g.json()["status"], "QUEUED")
        self.assertIsNone(g.json()["exit_code"])
        self.assertEqual(self.store.pending_outbox_count(), 1)

    def test_unsupported_language(self):
        r = self.submit(language="cobol")
        self.assertEqual(r.status_code, 422)
        self.assertIn("Supported", r.text)

    def test_oversized_source(self):
        r = self.submit(source_code="x" * (self.settings.limits.max_source_bytes + 1))
        self.assertEqual(r.status_code, 413)

    def test_oversized_stdin(self):
        r = self.submit(stdin="x" * (self.settings.limits.max_stdin_bytes + 1))
        self.assertEqual(r.status_code, 413)

    def test_huge_body_rejected_before_parsing(self):
        r = self.client.post("/api/v1/executions", content=b"{" + b" " * 3_000_000 + b"}",
                             headers={"content-type": "application/json"})
        self.assertEqual(r.status_code, 413)

    def test_invalid_inputs(self):
        for body in ({}, {"language": "python"}, {"language": "python", "source_code": ""},
                     {"language": "python", "source_code": "   "},
                     {"language": "python", "source_code": "x", "extra": 1},
                     {"language": 5, "source_code": "x"}):
            self.assertEqual(self.client.post("/api/v1/executions", json=body).status_code, 422, body)
        r = self.client.post("/api/v1/executions", content=b"not json",
                             headers={"content-type": "application/json"})
        self.assertEqual(r.status_code, 422)

    def test_validation_errors_do_not_echo_source(self):
        r = self.submit(language="cobol", source_code="SECRET_MARKER")
        self.assertNotIn("SECRET_MARKER", r.text)

    def test_unknown_and_malformed_ids(self):
        import uuid
        self.assertEqual(self.client.get(f"/api/v1/executions/{uuid.uuid4()}").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/executions/not-a-uuid").status_code, 404)
        self.assertEqual(self.client.post(f"/api/v1/executions/{uuid.uuid4()}/cancel").status_code, 404)

    def test_pagination_and_filtering(self):
        for lang in ("python", "python", "javascript"):
            self.submit(language=lang, source_code="1")
        r = self.client.get("/api/v1/executions?page=1&page_size=2").json()
        self.assertEqual((len(r["items"]), r["total"]), (2, 3))
        r = self.client.get("/api/v1/executions?language=javascript").json()
        self.assertEqual(r["total"], 1)
        r = self.client.get("/api/v1/executions?status=QUEUED").json()
        self.assertEqual(r["total"], 3)
        self.assertEqual(self.client.get("/api/v1/executions?status=BOGUS").status_code, 422)
        self.assertEqual(self.client.get("/api/v1/executions?page=0").status_code, 422)
        self.assertEqual(self.client.get("/api/v1/executions?page_size=1000").status_code, 422)

    def test_cancel_queued_then_conflict(self):
        eid = self.submit().json()["execution_id"]
        r = self.client.post(f"/api/v1/executions/{eid}/cancel")
        self.assertEqual((r.status_code, r.json()["status"]), (200, "CANCELLED"))
        self.assertEqual(self.client.post(f"/api/v1/executions/{eid}/cancel").status_code, 409)

    def test_cancel_running_is_accepted_not_forced(self):
        import uuid
        eid = self.submit().json()["execution_id"]
        self.store.claim(uuid.UUID(eid), "w")
        r = self.client.post(f"/api/v1/executions/{eid}/cancel")
        self.assertEqual((r.status_code, r.json()["status"]), (202, "RUNNING"))

    def test_health_and_metrics(self):
        self.assertEqual(self.client.get("/api/v1/health/live").json(), {"status": "ok"})
        self.assertEqual(self.client.get("/api/v1/health/ready").status_code, 200)
        m = self.client.get("/metrics")
        self.assertEqual(m.status_code, 200)
        self.assertIn("codeforge_submissions_total", m.text)

    def test_not_ready_when_database_down(self):
        self.store.ping = lambda: (_ for _ in ()).throw(RuntimeError("db"))
        r = self.client.get("/api/v1/health/ready")
        self.assertEqual(r.status_code, 503)

    def test_internal_errors_do_not_leak_details(self):
        def boom(**_):
            raise RuntimeError("secret /var/lib/postgres path")
        self.store.create_execution = boom
        r = self.submit()
        self.assertEqual(r.status_code, 500)
        self.assertNotIn("secret", r.text)
        self.assertNotIn("Traceback", r.text)

    def test_request_id_is_echoed_or_generated(self):
        self.assertEqual(self.client.get("/api/v1/health/live", headers={"X-Request-ID": "abc-1"})
                         .headers["x-request-id"], "abc-1")
        bad = self.client.get("/api/v1/health/live", headers={"X-Request-ID": "bad id;"})
        self.assertNotEqual(bad.headers["x-request-id"], "bad id;")

    def test_auth_api_key_header(self):
        from fastapi.testclient import TestClient
        from codeforge_api.app import create_app
        from codeforge_shared.config import Settings
        auth_settings = Settings(api_key="test-secret-key")
        auth_app = create_app(auth_settings, self.store, rabbit_check=lambda: True)
        client = TestClient(auth_app)

        # Public endpoints bypass auth
        self.assertEqual(client.get("/api/v1/health/live").status_code, 200)

        # Protected endpoints require API key
        self.assertEqual(client.get("/api/v1/languages").status_code, 401)
        self.assertEqual(client.get("/api/v1/languages", headers={"X-API-Key": "wrong"}).status_code, 401)
        self.assertEqual(client.get("/api/v1/languages", headers={"Authorization": "Bearer wrong"}).status_code, 401)

        # Valid API key succeeds
        r1 = client.get("/api/v1/languages", headers={"X-API-Key": "test-secret-key"})
        self.assertEqual(r1.status_code, 200)
        r2 = client.get("/api/v1/languages", headers={"Authorization": "Bearer test-secret-key"})
        self.assertEqual(r2.status_code, 200)

    def test_rate_limiting_per_ip(self):
        from fastapi.testclient import TestClient
        from codeforge_api.app import create_app
        from codeforge_shared.config import Settings
        rate_settings = Settings(rate_limit_per_minute=2)
        rate_app = create_app(rate_settings, self.store, rabbit_check=lambda: True)
        client = TestClient(rate_app)

        # Health endpoint not rate limited
        self.assertEqual(client.get("/api/v1/health/live").status_code, 200)
        self.assertEqual(client.get("/api/v1/health/live").status_code, 200)
        self.assertEqual(client.get("/api/v1/health/live").status_code, 200)

        # Protected endpoint limited to 2 req/min
        self.assertEqual(client.get("/api/v1/languages").status_code, 200)
        self.assertEqual(client.get("/api/v1/languages").status_code, 200)
        r = client.get("/api/v1/languages")
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.json()["error"]["code"], "rate_limit_exceeded")


if __name__ == "__main__":
    unittest.main()
