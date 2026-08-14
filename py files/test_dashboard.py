#!/usr/bin/env python3
"""Tests for the dashboard's Resync-only authorization challenge."""

import os
import time
import unittest

import dashboard


class ResyncAuthorizationTests(unittest.TestCase):
    def setUp(self):
        os.environ["CANOE_RESYNC_SECRET"] = "test-resync-secret"
        dashboard.app.config.update(TESTING=True)
        dashboard._resync_tokens.clear()
        self.client = dashboard.app.test_client()

    def tearDown(self):
        os.environ.pop("CANOE_RESYNC_SECRET", None)

    def test_dashboard_view_does_not_require_authentication(self):
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_resync_rejects_request_without_challenge(self):
        response = self.client.post("/api/resync", json={"confirm": "RESYNC"})
        self.assertEqual(response.status_code, 401)

    def test_challenge_rejects_incorrect_secret(self):
        response = self.client.post(
            "/api/resync/challenge", json={"secret": "incorrect"}
        )
        self.assertEqual(response.status_code, 401)

    def test_authorization_is_consumed_by_one_resync_attempt(self):
        response = self.client.post(
            "/api/resync/challenge", json={"secret": "test-resync-secret"}
        )
        self.assertEqual(response.status_code, 200)
        token = response.get_json()["token"]
        headers = {"X-Resync-Token": token}

        # A malformed confirmation stops before destructive work but consumes the token.
        response = self.client.post(
            "/api/resync", json={"confirm": "not-resync"}, headers=headers
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            "/api/resync", json={"confirm": "RESYNC"}, headers=headers
        )
        self.assertEqual(response.status_code, 401)

    def test_expired_authorization_is_rejected(self):
        token = "expired-token"
        dashboard._resync_tokens[dashboard._token_digest(token)] = int(time.time()) - 1
        response = self.client.post(
            "/api/resync",
            json={"confirm": "RESYNC"},
            headers={"X-Resync-Token": token},
        )
        self.assertEqual(response.status_code, 401)

    def test_missing_configuration_disables_challenge_only(self):
        os.environ.pop("CANOE_RESYNC_SECRET")
        response = self.client.post(
            "/api/resync/challenge", json={"secret": "anything"}
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.client.get("/").status_code, 200)


if __name__ == "__main__":
    unittest.main()
