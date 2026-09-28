from pathlib import Path
import tempfile
import unittest

from familyos.db import Database
from familyos.households import HouseholdService, ValidationError
from familyos.service_directory import ServiceDirectory


class ServiceDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Database(str(Path(self.temp.name) / 'test.db'))
        self.database.migrate()
        state = HouseholdService(self.database).initialize({'household_name':'测试', 'member_name':'管理员', 'username':'admin', 'timezone':'Asia/Shanghai'})
        self.admin = {**state['members'][0], 'household_id':state['household']['id']}
        self.directory = ServiceDirectory(self.database)

    def test_shared_entry_authority_and_household_scope(self):
        payload = {'name':'本地模型', 'url':'http://192.168.50.10:11434', 'category':'系统'}
        visitor = {**self.admin, 'is_admin':False}
        with self.assertRaises(PermissionError):
            self.directory.create(visitor, payload)
        entry = self.directory.create(self.admin, payload)
        self.assertEqual(self.directory.list(visitor)[0]['category'], 'system')
        with self.assertRaises(PermissionError):
            self.directory.delete(visitor, entry['id'])
        other = {**self.admin, 'household_id':'other'}
        self.assertEqual(self.directory.list(other), [])
        self.assertFalse(self.directory.delete(other, entry['id'])['deleted'])
        self.assertEqual(len(self.directory.list(self.admin)), 1)

    def test_reject_non_web_or_credential_urls(self):
        for url in ['javascript:alert(1)', 'file:///etc/passwd', 'https://user:password@example.com', 'https://example.com:bad', 'http://[broken', 'http://example.com\\@evil.com']:
            with self.subTest(url=url), self.assertRaises(ValidationError):
                self.directory.create(self.admin, {'name':'入口', 'category':'娱乐', 'url':url})
