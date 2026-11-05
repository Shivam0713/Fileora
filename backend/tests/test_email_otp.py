from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bson import ObjectId
from fastapi.testclient import TestClient
from fastapi import HTTPException

from app import auth, main, otp
from app.core.config import settings


class MemoryCollection:
    def __init__(self):
        self.documents = []

    @staticmethod
    def matches(document, query):
        for key, expected in query.items():
            actual = document.get(key)
            if isinstance(expected, dict):
                for operator, value in expected.items():
                    left, right = actual, value
                    if isinstance(left, datetime) and isinstance(right, datetime):
                        left = left.replace(tzinfo=timezone.utc) if left.tzinfo is None else left.astimezone(timezone.utc)
                        right = right.replace(tzinfo=timezone.utc) if right.tzinfo is None else right.astimezone(timezone.utc)
                    if operator == "$gt" and not (left is not None and left > right):
                        return False
                    if operator == "$lt" and not (left is not None and left < right):
                        return False
                    if operator == "$lte" and not (left is not None and left <= right):
                        return False
            elif actual != expected:
                return False
        return True

    def find_one(self, query, projection=None):
        for document in self.documents:
            if self.matches(document, query):
                result = deepcopy(document)
                if projection:
                    result = {key: value for key, value in result.items() if key in projection and projection[key]}
                    if "_id" not in projection and "_id" in document:
                        result.pop("_id", None)
                return result
        return None

    def insert_one(self, document):
        stored = deepcopy(document)
        stored.setdefault("_id", ObjectId())
        self.documents.append(stored)
        return SimpleNamespace(inserted_id=stored["_id"])

    def find_one_and_update(self, query, update, return_document=False):
        for document in self.documents:
            if self.matches(document, query):
                before = deepcopy(document)
                self._update(document, update)
                return deepcopy(document if return_document else before)
        return None

    def update_one(self, query, update):
        for document in self.documents:
            if self.matches(document, query):
                before = deepcopy(document)
                self._update(document, update)
                return SimpleNamespace(matched_count=1, modified_count=int(before != document))
        return SimpleNamespace(matched_count=0, modified_count=0)

    def update_many(self, query, update):
        changed = 0
        for document in self.documents:
            if self.matches(document, query):
                self._update(document, update)
                changed += 1
        return SimpleNamespace(matched_count=changed, modified_count=changed)

    def delete_one(self, query):
        for index, document in enumerate(self.documents):
            if self.matches(document, query):
                self.documents.pop(index)
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    def delete_many(self, query):
        before = len(self.documents)
        self.documents[:] = [item for item in self.documents if not self.matches(item, query)]
        return SimpleNamespace(deleted_count=before - len(self.documents))

    @staticmethod
    def _update(document, update):
        for key, value in update.get("$set", {}).items():
            document[key] = deepcopy(value)
        for key, value in update.get("$inc", {}).items():
            document[key] = document.get(key, 0) + value


class MemoryDatabase:
    def __init__(self):
        self.users = MemoryCollection()
        self.sessions = MemoryCollection()
        self.otp_challenges = MemoryCollection()
        self.files = MemoryCollection()
        self.jobs = MemoryCollection()


class EmailOtpFlowTests(unittest.TestCase):
    def setUp(self):
        self.database = MemoryDatabase()
        self.codes = []
        self.db_patches = [
            patch.object(main, "db", self.database),
            patch.object(auth, "db", self.database),
            patch.object(otp, "db", self.database),
            patch.object(main, "ensure_indexes"),
            patch.object(main, "seed_admin_account"),
            patch.object(otp, "_increment_rate", return_value=1),
            patch.object(otp, "_send_email", side_effect=self.capture_email),
            patch.object(settings, "jwt_secret_key", "j" * 64),
            patch.object(settings, "otp_hash_secret", "o" * 64),
            patch.object(settings, "email_provider", "resend"),
            patch.object(settings, "email_from", "Fileora <test@example.test>"),
            patch.object(settings, "resend_api_key", "test-only-key"),
        ]
        for active_patch in self.db_patches:
            active_patch.start()
            self.addCleanup(active_patch.stop)
        self.client_context = TestClient(main.app)
        self.client = self.client_context.__enter__()
        self.addCleanup(self.client_context.__exit__, None, None, None)

    def capture_email(self, email, name, code, purpose):
        self.codes.append({"email": email, "name": name, "code": code, "purpose": purpose})

    def register(self, email="person@example.com", password="a-safe-password-123"):
        response = self.client.post("/api/v1/auth/register", json={"name": "Fileora Person", "email": email, "password": password})
        return response, self.codes[-1]["code"] if self.codes else None

    def test_registration_creates_account_only_after_valid_otp_and_issues_jwt_cookie(self):
        response, code = self.register()
        self.assertEqual(response.status_code, 202)
        self.assertNotIn(code, response.text)
        self.assertEqual(len(self.database.users.documents), 0)
        challenge_id = response.json()["challenge_id"]
        verified = self.client.post("/api/v1/auth/otp/verify", json={"challenge_id": challenge_id, "code": code})
        self.assertEqual(verified.status_code, 200)
        self.assertEqual(verified.json()["user"]["name"], "Fileora Person")
        self.assertTrue(self.client.cookies.get(auth.SESSION_COOKIE).count(".") == 2)
        self.assertEqual(self.client.get("/api/v1/auth/me").json()["user"]["email"], "person@example.com")
        self.assertEqual(self.database.otp_challenges.documents[0]["status"], "used")

    def test_incorrect_code_is_rejected_and_fifth_attempt_locks_challenge(self):
        response, _ = self.register()
        body = {"challenge_id": response.json()["challenge_id"], "code": "000000"}
        for _ in range(5):
            result = self.client.post("/api/v1/auth/otp/verify", json=body)
            self.assertEqual(result.status_code, 400)
        blocked = self.client.post("/api/v1/auth/otp/verify", json=body)
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(len(self.database.users.documents), 0)

    def test_expired_and_reused_codes_are_rejected(self):
        response, code = self.register()
        challenge_id = response.json()["challenge_id"]
        self.database.otp_challenges.documents[0]["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).replace(tzinfo=None)
        expired = self.client.post("/api/v1/auth/otp/verify", json={"challenge_id": challenge_id, "code": code})
        self.assertEqual(expired.status_code, 410)

        response, code = self.register(email="second@example.com")
        challenge_id = response.json()["challenge_id"]
        self.assertEqual(self.client.post("/api/v1/auth/otp/verify", json={"challenge_id": challenge_id, "code": code}).status_code, 200)
        reused = self.client.post("/api/v1/auth/otp/verify", json={"challenge_id": challenge_id, "code": code})
        self.assertEqual(reused.status_code, 409)

    def test_resend_cooldown_rotates_code_and_invalidates_previous_code(self):
        response, old_code = self.register()
        challenge_id = response.json()["challenge_id"]
        early = self.client.post("/api/v1/auth/otp/resend", json={"challenge_id": challenge_id})
        self.assertEqual(early.status_code, 429)
        self.database.otp_challenges.documents[0]["resend_after"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).replace(tzinfo=None)
        resent = self.client.post("/api/v1/auth/otp/resend", json={"challenge_id": challenge_id})
        self.assertEqual(resent.status_code, 200)
        self.assertEqual(self.client.post("/api/v1/auth/otp/resend", json={"challenge_id": challenge_id}).status_code, 429)
        new_code = self.codes[-1]["code"]
        self.assertNotEqual(old_code, new_code)
        self.assertEqual(self.client.post("/api/v1/auth/otp/verify", json={"challenge_id": challenge_id, "code": old_code}).status_code, 400)
        self.assertEqual(self.client.post("/api/v1/auth/otp/verify", json={"challenge_id": challenge_id, "code": new_code}).status_code, 200)

    def test_login_requires_password_then_email_code(self):
        created, code = self.register()
        self.client.post("/api/v1/auth/otp/verify", json={"challenge_id": created.json()["challenge_id"], "code": code})
        self.client.post("/api/v1/auth/logout")
        bad = self.client.post("/api/v1/auth/login", json={"email": "person@example.com", "password": "incorrect-password"})
        self.assertEqual(bad.status_code, 401)
        missing = self.client.post("/api/v1/auth/login", json={"email": "missing@example.com", "password": "incorrect-password"})
        self.assertEqual(missing.status_code, 401)
        start = self.client.post("/api/v1/auth/login", json={"email": "person@example.com", "password": "a-safe-password-123"})
        self.assertEqual(start.status_code, 202)
        self.assertFalse(start.json().get("user"))
        self.assertEqual(self.client.get("/api/v1/auth/me").json()["authenticated"], False)
        result = self.client.post("/api/v1/auth/otp/verify", json={"challenge_id": start.json()["challenge_id"], "code": self.codes[-1]["code"]})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["user"]["email"], "person@example.com")
        self.assertEqual(self.client.get("/api/v1/admin/overview").status_code, 403)

    def test_forgot_password_uses_otp_then_revokes_sessions_and_changes_password(self):
        created, code = self.register()
        self.client.post("/api/v1/auth/otp/verify", json={"challenge_id": created.json()["challenge_id"], "code": code})
        recovery = self.client.post("/api/v1/auth/forgot-password", json={"email": "person@example.com"})
        self.assertEqual(recovery.status_code, 202)
        code = self.codes[-1]["code"]
        verified = self.client.post("/api/v1/auth/otp/verify", json={"challenge_id": recovery.json()["challenge_id"], "code": code})
        self.assertEqual(verified.status_code, 200)
        reset = self.client.post("/api/v1/auth/password-reset", json={"reset_token": verified.json()["reset_token"], "password": "a-new-safe-password-456"})
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(self.client.get("/api/v1/auth/me").json()["authenticated"], False)
        result = self.client.post("/api/v1/auth/login", json={"email": "person@example.com", "password": "a-new-safe-password-456"})
        self.assertEqual(result.status_code, 202)

    def test_unregistered_recovery_email_returns_generic_response_without_sending_email(self):
        before = len(self.codes)
        response = self.client.post("/api/v1/auth/forgot-password", json={"email": "unknown@example.com"})
        self.assertEqual(response.status_code, 202)
        self.assertIn("If an account exists", response.json()["message"])
        self.assertEqual(len(self.codes), before)

    def test_rate_limiter_rejects_excess_requests_and_unauthorized_admin_access(self):
        with patch.object(otp, "_increment_rate", return_value=6):
            with self.assertRaises(HTTPException) as raised:
                otp.enforce_rate_limits(SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")), "rate@example.com", "test", email_limit=5)
        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(self.client.get("/api/v1/admin/overview").status_code, 401)


if __name__ == "__main__":
    unittest.main()
