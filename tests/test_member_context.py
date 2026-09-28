from pathlib import Path
import tempfile
import unittest

from familyos.auth import SessionService
from familyos.db import Database
from familyos.households import HouseholdService, ValidationError
from familyos.member_context import MemberContextService


class MemberContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp.name) / "familyos.db"))
        self.db.migrate()
        household = HouseholdService(self.db)
        household.initialize({"household_name": "Test", "member_name": "Lin", "username": "lin", "timezone": "Asia/Shanghai"})
        _, self.member = SessionService(self.db).create("lin")
        household.add_member(self.member, {"display_name": "Yan", "username": "yan", "role": "adult"})
        _, self.other = SessionService(self.db).create("yan")
        self.service = MemberContextService(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def test_profile_is_member_scoped(self):
        self.service.save_profile(self.member, {"quiet_start": "22:30", "quiet_end": "07:00", "assistance_preferences": "早晨少打扰"})
        self.assertEqual(self.service.get(self.member)["profile"]["quiet_start"], "22:30")
        self.assertIsNone(self.service.get(self.other)["profile"]["quiet_start"])

    def test_assistant_context_has_shared_names_but_no_other_member_login(self):
        context = self.service.assistant_context(self.member)
        self.assertEqual([row['display_name'] for row in context['household_members']], ['Lin', 'Yan'])
        self.assertNotIn('username', context['household_members'][1])

    def test_member_context_exposes_safe_household_choices_for_consent(self):
        context = self.service.get(self.member)
        self.assertEqual(
            [(row['display_name'], row['is_current']) for row in context['household_members']],
            [('Lin', True), ('Yan', False)],
        )
        self.assertTrue(all('username' not in row for row in context['household_members']))

    def test_consent_can_only_be_granted_and_revoked_by_owner(self):
        consents = self.service.grant(self.member, {"grantee_member_id": self.other["id"], "data_scope": "calendar", "purpose": "帮助安排接送", "expires_at": None})
        consent_id = consents[0]["id"]
        with self.assertRaises(ValidationError):
            self.service.revoke(self.other, consent_id)
        self.assertTrue(self.service.revoke(self.member, consent_id)["revoked"])


if __name__ == "__main__":
    unittest.main()
