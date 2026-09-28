from pathlib import Path
import tempfile
import unittest

from familyos.auth import AuthenticationError, SessionService, SessionValidationError, normalize_username
from familyos.db import Database
from familyos.households import HouseholdService


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.temp.name) / "familyos.db"))
        self.database.migrate()
        HouseholdService(self.database).initialize({
            "household_name": "林家", "member_name": "小林", "username": "Xiao.Lin", "timezone": "Asia/Shanghai"
        })
        self.sessions = SessionService(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def test_username_normalization_is_case_insensitive(self):
        token, member = self.sessions.create("xiao.lin", now_epoch=100)
        self.assertEqual(member["username"], "Xiao.Lin")
        self.assertEqual(self.sessions.resolve(token, now_epoch=101)["display_name"], "小林")

    def test_browser_relogin_rotates_token_but_retains_one_device(self):
        browser = 'a' * 43
        previous = None
        for tick in range(20):
            token, member = self.sessions.create('Xiao.Lin', now_epoch=100 + tick, browser_id=browser)
            if previous:
                self.assertIsNone(self.sessions.resolve(previous, now_epoch=121))
            previous = token
        view = self.sessions.list(member, token, now_epoch=121)
        self.assertEqual(len(view['sessions']), 1)
        self.assertEqual(len(view['audit']), 20)
        device_id = view['sessions'][0]['id']
        self.sessions.revoke(member, device_id, token, True, now_epoch=122)
        token, _ = self.sessions.create('Xiao.Lin', now_epoch=123, browser_id=browser)
        self.assertEqual(self.sessions.list(member, token, now_epoch=124)['sessions'][0]['id'], device_id)
        self.assertIsNotNone(self.sessions.resolve(token, now_epoch=124))

    def test_different_browsers_same_label_are_not_merged(self):
        first, member = self.sessions.create('Xiao.Lin', now_epoch=100, browser_id='a' * 43, device_label='Chrome')
        second, _ = self.sessions.create('Xiao.Lin', now_epoch=101, browser_id='b' * 43, device_label='Chrome')
        self.assertEqual(len(self.sessions.list(member, second, now_epoch=102)['sessions']), 2)
        self.assertIsNotNone(self.sessions.resolve(first, now_epoch=102))

    def test_current_legacy_session_is_adopted_not_arbitrarily_merged(self):
        old, member = self.sessions.create('Xiao.Lin', now_epoch=100)
        token, _ = self.sessions.create('Xiao.Lin', now_epoch=101, browser_id='a' * 43, previous_token=old)
        self.assertEqual(len(self.sessions.list(member, token, now_epoch=102)['sessions']), 1)
        with self.database.session() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM member_sessions').fetchone()[0], 1)

    def test_session_stores_only_token_hash_and_expires(self):
        token, _ = self.sessions.create("Xiao.Lin", now_epoch=100)
        with self.database.session() as connection:
            stored = connection.execute("SELECT token_hash FROM member_sessions").fetchone()[0]
        self.assertNotEqual(stored, token)
        self.assertIsNone(self.sessions.resolve(token, now_epoch=100 + 31 * 86400))

    def test_invalid_or_unknown_username_is_rejected(self):
        with self.assertRaises(AuthenticationError):
            normalize_username("a b")
        with self.assertRaisesRegex(AuthenticationError, "不存在"):
            self.sessions.create("missing")

    def test_device_sessions_are_member_scoped_audited_and_revocable(self):
        token, member = self.sessions.create("Xiao.Lin", now_epoch=100, device_label="Edge · Windows")
        snapshot = self.sessions.list(member, token, now_epoch=101)
        self.assertEqual(len(snapshot["sessions"]), 1)
        device = snapshot["sessions"][0]
        self.assertEqual((device["device_label"], device["status"], device["current"]),
                         ("Edge · Windows", "active", True))
        self.assertNotIn("token_hash", device)

        trusted = self.sessions.set_trusted(member, device["id"], True, token, now_epoch=102)
        self.assertTrue(trusted["trusted"])
        self.assertTrue(trusted["current"])
        with self.assertRaisesRegex(SessionValidationError, "再次确认"):
            self.sessions.revoke(member, device["id"], token, False, now_epoch=103)
        revoked = self.sessions.revoke(member, device["id"], token, True, now_epoch=104)
        self.assertTrue(revoked["current_session_revoked"])
        self.assertEqual(revoked["session"]["status"], "revoked")
        self.assertIsNone(self.sessions.resolve(token, now_epoch=105))
        events = [entry["event"] for entry in self.sessions.list(member, token, now_epoch=105)["audit"]]
        self.assertEqual(events[:3], ["revoked", "trusted", "created"])

    def test_device_session_cannot_be_read_or_changed_by_another_member(self):
        owner_token, owner = self.sessions.create("Xiao.Lin", now_epoch=100, device_label="Owner browser")
        other = HouseholdService(self.database).add_member(owner, {
            "display_name": "访客", "username": "guest", "role": "visitor",
        })
        other_token, other_member = self.sessions.create("guest", now_epoch=101, device_label="Guest browser")
        owner_device = self.sessions.list(owner, owner_token, now_epoch=102)["sessions"][0]
        other_snapshot = self.sessions.list(other_member, other_token, now_epoch=102)
        self.assertEqual([row["device_label"] for row in other_snapshot["sessions"]], ["Guest browser"])
        with self.assertRaisesRegex(AuthenticationError, "不存在"):
            self.sessions.set_trusted(other_member, owner_device["id"], True, other_token, now_epoch=103)


if __name__ == "__main__":
    unittest.main()

