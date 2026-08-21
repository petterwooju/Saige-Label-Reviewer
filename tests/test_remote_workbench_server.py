import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from saige_reviewer.remote_projects import RemoteProjectService, RemoteRuntimeConfig
from saige_reviewer.remote_workbench_server import (
    CloudflareAccessVerifier,
    RemoteServerConfig,
    create_remote_handler,
)
from saige_reviewer.server import ThreadingHTTPServer


class RemoteWorkbenchServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        config = RemoteRuntimeConfig(
            root=Path(self.temporary.name),
            admin_emails=frozenset(),
            max_project_size=1024 * 1024,
            max_managed_storage=1024 * 1024 * 1024,
            min_free_space=0,
            retention_seconds=60,
            recycle_seconds=60,
            lease_seconds=30,
        )
        self.service = RemoteProjectService(config, start_workers=False)
        server_config = RemoteServerConfig(
            "saige-label-reviewer-beta.saigeai.com",
            dev_auth_email="member@saigeai.com",
            csrf_secret=b"fixture-secret",
        )
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), create_remote_handler(self.service, server_config)
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.service.close()
        self.temporary.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        values = dict(response.getheaders())
        connection.close()
        return response.status, values, payload

    def bootstrap(self):
        status, headers, body = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        value = json.loads(body)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        return value, cookie

    def test_full_workbench_and_bootstrap_share_remote_contract(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"remoteProjectsDialog", body)
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        value, _ = self.bootstrap()
        self.assertEqual((value["api_version"], value["mode"]), (2, "remote"))
        self.assertEqual(value["user"]["email"], "member@saigeai.com")
        self.assertTrue(value["capabilities"]["resumable_upload"])

    def test_mutations_require_matching_csrf_cookie_and_header(self):
        value, cookie = self.bootstrap()
        body = json.dumps(
            {
                "name": "fixture",
                "format": "visionproj",
                "total_size": 1,
                "file_count": 1,
                "primary_path": "project/fixture.visionproj",
            }
        )
        status, _, payload = self.request(
            "POST", "/api/uploads", body, {"Content-Type": "application/json"}
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(payload)["error"]["code"], "forbidden")
        status, _, payload = self.request(
            "POST",
            "/api/uploads",
            body,
            {
                "Content-Type": "application/json",
                "Cookie": cookie,
                "X-CSRF-Token": value["csrf_token"],
                "Origin": f"http://127.0.0.1:{self.server.server_port}",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(payload)["state"], "registering")

    def test_public_host_does_not_trust_forwarded_email_without_access_jwt(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request(
            "GET",
            "/api/bootstrap",
            headers={
                "Host": "saige-label-reviewer-beta.saigeai.com",
                "Cf-Access-Authenticated-User-Email": "member@saigeai.com",
            },
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 403)
        self.assertIn("assertion", payload["error"]["message"])

    def test_access_verifier_requires_fixed_issuer_audience_algorithm_and_app_type(self):
        verifier = CloudflareAccessVerifier(
            "example.cloudflareaccess.com", "fixed-audience"
        )
        key = mock.Mock()
        key.get_signing_key_from_jwt.return_value.key = "public-key"
        with mock.patch("jwt.PyJWKClient", return_value=key) as client, mock.patch(
            "jwt.decode",
            return_value={"type": "app", "email": "member@saigeai.com"},
        ) as decode:
            claims = verifier("signed-access-assertion-value")
        self.assertEqual(claims["type"], "app")
        client.assert_called_once_with(
            "https://example.cloudflareaccess.com/cdn-cgi/access/certs"
        )
        self.assertEqual(decode.call_args.kwargs["algorithms"], ["RS256"])
        self.assertEqual(decode.call_args.kwargs["audience"], "fixed-audience")
        self.assertEqual(
            decode.call_args.kwargs["issuer"], "https://example.cloudflareaccess.com"
        )


if __name__ == "__main__":
    unittest.main()
