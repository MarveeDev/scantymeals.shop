"""
Phase 10 — Customer phone authentication, SMS outbox automation, performance.

Run with:  .venv\\Scripts\\python test_phase10.py

Runs against the Flask app in IN-MEMORY mode. OTP uses the mock provider.
"""

import os
import unittest
from unittest import mock

os.environ['MONGODB_URI'] = 'mongodb://127.0.0.1:59999/?serverSelectionTimeoutMS=1000'
os.environ.pop('RENDER', None)
os.environ.pop('FLASK_ENV', None)
os.environ['SMS_PROVIDER'] = 'mock'
os.environ['OTP_SECRET'] = 'test-otp-secret-123456'
os.environ['SUPER_ADMIN_EMAIL'] = 'super@scanty.com'
os.environ['SUPER_ADMIN_PASSWORD'] = 'SuperSecret123!'
os.environ['ADMIN_EMAIL'] = 'admin@scanty.com'
os.environ['ADMIN_PASSWORD'] = 'admin123'

import app as app_module


class Phase10Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert app_module.MONGODB_CONNECTED is False, "tests require in-memory mode"
        cls.client = app_module.app.test_client()
        cls.admin = cls.client.post('/api/auth/admin-login',
                                    json={'email': 'admin@scanty.com', 'password': 'admin123'}).get_json()
        cls.admin_h = {'Authorization': 'Bearer ' + cls.admin['token']}

    def setUp(self):
        app_module.otp_codes_db.clear()
        app_module.rate_limits_db.clear()
        app_module.sms_outbox_db.clear()
        app_module.sms_logs_db.clear()

    def _h(self, tok): return {'Authorization': 'Bearer ' + tok}

    def _register(self, phone='0241000001', password='secret123', code=123456):
        with mock.patch.object(app_module.secrets, 'randbelow', return_value=code):
            r1 = self.client.post('/api/auth/register-phone', json={'phone': phone, 'password': password})
        r2 = self.client.post('/api/auth/register-phone/verify', json={'phone': phone, 'code': str(code).zfill(6)})
        return r1, r2

    # ---------- Registration & login ----------
    def test_register_and_login(self):
        r1, r2 = self._register(phone='0241000001', password='secret123')
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        u = r2.get_json()['user']
        self.assertTrue(u['phone_verified'])
        # login with phone + password
        r = self.client.post('/api/auth/login-phone', json={'phone': '0241000001', 'password': 'secret123'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['token'])

    def test_register_phone_uniqueness(self):
        self._register(phone='0241000002', password='secret123')
        r = self.client.post('/api/auth/register-phone', json={'phone': '0241000002', 'password': 'other123'})
        self.assertEqual(r.status_code, 409)

    def test_login_wrong_password(self):
        self._register(phone='0241000003', password='secret123')
        r = self.client.post('/api/auth/login-phone', json={'phone': '0241000003', 'password': 'wrongwrong'})
        self.assertEqual(r.status_code, 401)

    def test_password_is_hashed(self):
        self._register(phone='0241000004', password='secret123')
        user = app_module._find_user_by_phone_digits(app_module._phone_digits('0241000004'))
        self.assertNotEqual(user['password'], 'secret123')

    def test_password_recovery(self):
        self._register(phone='0241000005', password='secret123')
        # Simulate the 60s resend cooldown elapsing before requesting a new OTP.
        app_module.otp_codes_db.clear()
        app_module.rate_limits_db.clear()
        with mock.patch.object(app_module.secrets, 'randbelow', return_value=654321):
            self.client.post('/api/auth/forgot-password', json={'phone': '0241000005'})
        r = self.client.post('/api/auth/reset-password',
                             json={'phone': '0241000005', 'code': '654321', 'new_password': 'newpass123'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.post('/api/auth/login-phone',
                                          json={'phone': '0241000005', 'password': 'newpass123'}).status_code, 200)
        self.assertEqual(self.client.post('/api/auth/login-phone',
                                          json={'phone': '0241000005', 'password': 'secret123'}).status_code, 401)

    def test_register_upgrades_legacy_customer(self):
        c = self.client.post('/api/auth/phone-name', json={'phone': '0241000006', 'name': 'Legacy'}).get_json()
        cid = c['user']['id']
        r1, r2 = self._register(phone='0241000006', password='newsecret')
        self.assertEqual(r2.status_code, 200)
        user = app_module._find_user_by_id(cid)
        self.assertTrue(user['phone_verified'])
        self.assertEqual(user['provider'], 'phone_password')
        self.assertTrue(app_module.verify_password('newsecret', user['password']))

    def test_register_short_password_rejected(self):
        r = self.client.post('/api/auth/register-phone', json={'phone': '0241000007', 'password': '123'})
        self.assertEqual(r.status_code, 400)

    # ---------- SMS outbox & multi-admin ----------
    def test_order_enqueues_and_sends_sms(self):
        r = self.client.post('/api/orders', json={'customer_name': 'C', 'customer_phone': '0241111222',
                                                  'customer_location': 'x', 'items': [{'id': 'fried-rice', 'quantity': 1}]})
        self.assertEqual(r.status_code, 201)
        oid = r.get_json()['order']['id']
        logs = [l for l in app_module.sms_logs_db if l.get('order_id') == oid]
        self.assertTrue(any(l.get('event_type') == 'order_placed' for l in logs))

    def test_multiple_admin_recipients(self):
        with mock.patch.dict('os.environ', {'ADMIN_NOTIFICATION_PHONES': '0241111222, +233241111223'}):
            r = self.client.post('/api/orders', json={'customer_name': 'C', 'customer_phone': '0241111333',
                                                      'customer_location': 'x', 'items': [{'id': 'fried-rice', 'quantity': 1}]})
            oid = r.get_json()['order']['id']
            admins = [l for l in app_module.sms_logs_db
                      if l.get('order_id') == oid and l.get('event_type') == 'admin_new_order']
            recipients = {l['recipient'] for l in admins}
            self.assertIn('+233241111222', recipients)
            self.assertIn('+233241111223', recipients)

    def test_enqueue_dedupe(self):
        self.assertTrue(app_module.enqueue_sms('0241111444', 'hi', 'O1', 'order_placed'))
        self.assertFalse(app_module.enqueue_sms('0241111444', 'hi', 'O1', 'order_placed'))

    def test_checkout_does_not_block_on_sms(self):
        with mock.patch.dict('os.environ', {'SMS_WORKER': '1'}):
            r = self.client.post('/api/orders', json={'customer_name': 'C', 'customer_phone': '0241111555',
                                                      'customer_location': 'x', 'items': [{'id': 'fried-rice', 'quantity': 1}]})
            self.assertEqual(r.status_code, 201)
            # In async mode, SMS is enqueued (outbox) but not yet sent.
            self.assertTrue([i for i in app_module.sms_outbox_db if i.get('status') == 'pending'])
            self.assertFalse([l for l in app_module.sms_logs_db if l.get('event_type') == 'order_placed'])
        # Drain and confirm delivery.
        app_module._process_sms_outbox(limit=50)
        self.assertTrue([l for l in app_module.sms_logs_db if l.get('event_type') == 'order_placed'])

    def test_sms_failure_does_not_cancel_order(self):
        with mock.patch.object(app_module, 'send_sms', side_effect=RuntimeError('sms down')):
            r = self.client.post('/api/orders', json={'customer_name': 'C', 'customer_phone': '0241111666',
                                                      'customer_location': 'x', 'items': [{'id': 'fried-rice', 'quantity': 1}]})
        self.assertEqual(r.status_code, 201)


if __name__ == '__main__':
    unittest.main(verbosity=2)
