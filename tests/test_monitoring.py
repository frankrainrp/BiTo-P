from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from familyos.auth import SessionService
from familyos.db import Database
from familyos.households import HouseholdService, ValidationError
from familyos.monitoring import MonitoringService, NotificationChannels, _resolve_public, extract_value, validate_public_url


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp.name) / 'db.sqlite'))
        self.db.migrate()
        households = HouseholdService(self.db)
        households.initialize({'household_name':'Test','member_name':'Lin','username':'lin','timezone':'Asia/Shanghai'})
        _, self.member = SessionService(self.db).create('lin')
        households.add_member(self.member, {'display_name':'Yan','username':'yan','role':'adult'})
        _, self.other = SessionService(self.db).create('yan')
        self.now = 1800000000
        self.body = '{"quote":{"price":10}}'
        self.service = MonitoringService(self.db, fetcher=lambda url: (self.body,'application/json'), channels=NotificationChannels({}), clock=lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    def create(self, **extra):
        return self.service.create_subscription(self.member, {'title':'Test price','url':'https://example.com/quote','source_type':'json','json_path':'quote.price','condition':'above','target':'9','interval_seconds':60,'cooldown_seconds':120, **extra})

    def test_public_url_and_dns_fail_closed(self):
        for value in ['file:///etc/passwd','http://127.0.0.1/','http://[::1]/','http://192.168.1.1/','http://169.254.169.254/','http://user:pass@example.com/','https://example.com:9443/','https://host.local/','https://example.com/\r\nx:1']:
            with self.assertRaises(ValidationError): validate_public_url(value)
        self.assertEqual(validate_public_url('https://example.com')[0], 'https://example.com/')
        private = [(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]
        with patch('familyos.monitoring.socket.getaddrinfo',return_value=private):
            with self.assertRaises(ValidationError): _resolve_public('example.com',443)

    def test_extract_is_explicit_and_scripts_are_ignored(self):
        self.assertEqual(extract_value('<p>Price 12</p><script>secret</script>', 'text','','text/html'), 'Price 12')
        self.assertEqual(extract_value('{"items":[{"price":12}]}','json','items.0.price','application/json'), '12')
        for path in ['items.bad','items.0','missing']:
            with self.assertRaises(ValidationError): extract_value('{"items":[{"price":12}]}','json',path,'application/json')

    def test_threshold_edge_and_cooldown_do_not_spam(self):
        subscription = self.create()
        first = self.service.run_subscription(self.member,subscription['id'])
        self.assertTrue(first['notified'])
        self.assertEqual(first['evidence']['sample_size'],1)
        self.now += 60
        self.assertFalse(self.service.run_subscription(self.member,subscription['id'])['notified'])
        self.body = '{"quote":{"price":8}}'; self.now += 60
        self.assertFalse(self.service.run_subscription(self.member,subscription['id'])['matched'])
        self.body = '{"quote":{"price":12}}'; self.now += 60
        self.assertTrue(self.service.run_subscription(self.member,subscription['id'])['notified'])
        self.assertEqual(len(self.service.list_alerts(self.member)),2)

    def test_change_first_observation_is_baseline_and_pending_survives_cooldown(self):
        subscription = self.create(condition='change',target='')
        self.assertFalse(self.service.run_subscription(self.member,subscription['id'])['notified'])
        self.now += 60; self.body = '{"quote":{"price":11}}'
        self.assertTrue(self.service.run_subscription(self.member,subscription['id'])['notified'])
        self.now += 60; self.body = '{"quote":{"price":12}}'
        self.assertFalse(self.service.run_subscription(self.member,subscription['id'])['notified'])
        self.now += 60
        self.assertTrue(self.service.run_subscription(self.member,subscription['id'])['notified'])

    def test_member_cannot_read_or_modify_another_subscription(self):
        sub = self.create(); self.service.run_subscription(self.member,sub['id'])
        alert = self.service.list_alerts(self.member)[0]
        self.assertEqual(self.service.list_subscriptions(self.other),[])
        self.assertEqual(self.service.list_alerts(self.other),[])
        for callback in [lambda: self.service.list_runs(self.other,sub['id']), lambda: self.service.update_subscription(self.other,sub['id'],{'enabled':False}),lambda:self.service.delete_subscription(self.other,sub['id']),lambda:self.service.acknowledge_alert(self.other,alert['id'])]:
            with self.assertRaises(ValidationError): callback()

    def test_failed_source_never_notifies_and_recovers_next_cycle(self):
        sub = self.create(); self.body = 'not json'
        result = self.service.run_subscription(self.member,sub['id'])
        self.assertEqual(result['status'],'error'); self.assertFalse(result['notified']); self.assertEqual(result['evidence']['denominator'],0)
        self.body = '{"quote":{"price":15}}'; self.now += 60
        self.assertEqual(self.service.run_due(),1)
        self.assertEqual(len(self.service.list_alerts(self.member)),1)

    def test_pause_persistence_manual_rate_and_validation(self):
        sub = self.create(); self.service.run_subscription(self.member,sub['id'])
        with self.assertRaises(ValidationError): self.service.run_subscription(self.member,sub['id'])
        self.service.update_subscription(self.member,sub['id'],{'enabled':False}); self.now += 180
        self.assertEqual(self.service.run_due(),0)
        self.assertFalse(MonitoringService(self.db).list_subscriptions(self.member)[0]['enabled'])
        for extra in [{'interval_seconds':0},{'interval_seconds':float('inf')},{'channel':'telegram'},{'target':'nan'},{'enabled':'true'}]:
            with self.assertRaises(ValidationError): self.create(**extra)

    def test_revision_change_discards_inflight_result(self):
        sub = self.create()
        def fetch(url):
            self.service.update_subscription(self.member,sub['id'],{'enabled':False})
            return self.body,'application/json'
        self.service.fetcher = fetch
        with self.assertRaises(ValidationError): self.service.run_subscription(self.member,sub['id'])
        self.assertEqual(self.service.list_runs(self.member,sub['id']),[])

    def test_channel_status_never_exposes_credentials(self):
        channels = NotificationChannels({'FAMILYOS_MONITOR_WEBHOOK_URL':'https://example.com/private-token','FAMILYOS_TELEGRAM_BOT_TOKEN':'123:secret','FAMILYOS_TELEGRAM_CHAT_ID':'999'})
        self.assertEqual(channels.status(),{'in_app':{'ready':True},'webhook':{'ready':True},'telegram':{'ready':True}})
