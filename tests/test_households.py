from pathlib import Path
import tempfile
import unittest

from familyos.db import Database
from familyos.households import HouseholdService, ValidationError


class HouseholdTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        database = Database(str(Path(self.temp.name) / "familyos.db"))
        database.migrate()
        self.service = HouseholdService(database)

    def tearDown(self):
        self.temp.cleanup()

    def test_initialization_creates_household_and_adult_admin_atomically(self):
        state = self.service.initialize({
            "household_name": " 林家 ", "member_name": " 小林 ", "username": "xiaolin", "timezone": "Asia/Shanghai"
        })
        self.assertTrue(state["configured"])
        self.assertEqual(state["household"]["name"], "林家")
        self.assertEqual(state["members"][0]["role"], "adult")
        self.assertTrue(state["members"][0]["is_admin"])
        self.assertEqual(state["members"][0]["username"], "xiaolin")

    def test_initialization_can_only_happen_once(self):
        payload = {"household_name": "林家", "member_name": "小林", "username": "xiaolin", "timezone": "Asia/Shanghai"}
        self.service.initialize(payload)
        with self.assertRaisesRegex(ValidationError, "已经初始化"):
            self.service.initialize(payload)

    def test_invalid_timezone_does_not_write_partial_household(self):
        with self.assertRaisesRegex(ValidationError, "有效的 IANA"):
            self.service.initialize({"household_name": "林家", "member_name": "小林", "username": "xiaolin", "timezone": "Mars/Base"})
        self.assertFalse(self.service.get_setup()["configured"])

    def test_public_setup_does_not_disclose_member_list(self):
        self.service.initialize({"household_name": "林家", "member_name": "小林", "username": "xiaolin", "timezone": "Asia/Shanghai"})
        self.assertEqual(self.service.get_setup()["members"], [])


if __name__ == "__main__":
    unittest.main()
