import unittest

from familyos.butler import ButlerStateSource


class ButlerStateSourceTests(unittest.TestCase):
    def setUp(self):
        self.member = {'is_admin': True, 'household_timezone': 'Asia/Shanghai'}

    def test_reads_only_validated_ddls_and_maps_shared_calendar_fields(self):
        payload = {
            'ddls': [{'id': 'ddl-1', 'taskName': '提交作业', 'dueDate': '2026-09-08', 'dueTime': '21:30',
                      'completed': False, 'status': 'todo'}, {'id': '', 'taskName': '无效'}],
            'messages': [{'content': '不得进入 FamilyOS'}], 'notes': [{'body': '不得进入 FamilyOS'}],
        }
        result = ButlerStateSource('http://butler-web:3000/api/state?key=ddls', transport=lambda: payload).tasks(self.member)
        self.assertTrue(result['available'])
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['title'], '提交作业')
        self.assertEqual(result['items'][0]['source'], 'butler')
        self.assertNotIn('messages', result)
        self.assertNotIn('notes', result)

    def test_global_butler_workspace_is_not_exposed_to_non_admin_members(self):
        called = []
        source = ButlerStateSource('http://butler-web:3000/api/state?key=ddls', transport=lambda: called.append(True) or {'ddls': []})
        result = source.tasks({'is_admin': False, 'household_timezone': 'Asia/Shanghai'})
        self.assertEqual(result['items'], [])
        self.assertEqual(called, [])


if __name__ == '__main__':
    unittest.main()
