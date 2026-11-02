"""
Phase 7A (revised) — Real Gonline SMS provider + customer phone OTP.

Run with:  .venv\\Scripts\\python test_phase7.py

Runs against the Flask app in IN-MEMORY mode. All HTTP (Gonline) calls are
mocked; no real SMS is ever sent.
"""

import os
import json
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
import sms_provider as sp


class FakeResp:
    def __init__(self, data=None, status_code=200, bad_json=False):
        self._data = data
        self.status_code = status_code
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError('no json')
        return self._data


class FakeSmsProvider:
    name = 'fake'

    def __init__(self, fail_times=0):
        self.calls = 0
        self.fail_times = fail_times

    def send(self, phone, message):
        self.calls += 1
        if self.calls <= self.fail_times:
            return False, {"provider": "fake", "error": "transient down", "retryable": True}
        return True, {"provider": "fake", "message_id": "ok-" + str(self.calls)}


def make_gonline():
    p = sp.GonlineSmsProvider()
    p.api_url = 'http://gonline.test/sms'
    p.api_key = 'sekret-key-123'
    p.sender_id = 'SCANTY'
    p.ready = True
    return p


class Phase7Tests(unittest.TestCase):
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

    def _h(self, tok): return {'Authorization': 'Bearer ' + tok}

    def _request_otp(self, phone, code=123456):
        with mock.patch.object(app_module.secrets, 'randbelow', return_value=code):
            return self.client.post('/api/auth/otp/request', json={'phone': phone})

    def _verify_otp(self, phone, code):
        return self.client.post('/api/auth/otp/verify', json={'phone': phone, 'code': code})

    # ---------- Gonline provider (mocked HTTP) ----------
    def test_gonline_success(self):
        p = make_gonline()
        with mock.patch.object(sp.requests, 'get', return_value=FakeResp({
                'code': 'OK', 'message_id': 'm-1', 'to': '+233244000000', 'units': 1, 'balance': 99})):
            ok, resp = p.send('+233244000000', 'hi')
        self.assertTrue(ok)
        self.assertEqual(resp['code'], 'OK')
        self.assertEqual(resp['message_id'], 'm-1')

    def test_gonline_numeric_error_permanent(self):
        p = make_gonline()
        with mock.patch.object(sp.requests, 'get', return_value=FakeResp({'code': '101'})):
            ok, resp = p.send('+233244000000', 'hi')
        self.assertFalse(ok)
        self.assertEqual(resp['code'], '101')
        self.assertFalse(resp['retryable'])

    def test_gonline_timeout_retryable(self):
        p = make_gonline()
        with mock.patch.object(sp.requests, 'get', side_effect=sp.requests.exceptions.Timeout()):
            ok, resp = p.send('+233244000000', 'hi')
        self.assertFalse(ok)
        self.assertTrue(resp['retryable'])
        self.assertEqual(resp['error'], 'timeout')

    def test_gonline_malformed_json_retryable(self):
        p = make_gonline()
        with mock.patch.object(sp.requests, 'get', return_value=FakeResp(bad_json=True, status_code=200)):
            ok, resp = p.send('+233244000000', 'hi')
        self.assertFalse(ok)
        self.assertTrue(resp['retryable'])
        self.assertEqual(resp['error'], 'invalid_response')

    def test_gonline_not_configured(self):
        p = sp.GonlineSmsProvider()
        p.ready = False
        ok, resp = p.send('+233244000000', 'hi')
        self.assertFalse(ok)
        self.assertEqual(resp['status'], 'not_configured')
        self.assertFalse(resp['retryable'])

    def test_gonline_never_reports_delivered(self):
        p = make_gonline()
        with mock.patch.object(sp.requests, 'get', return_value=FakeResp({'code': 'OK'})):
            ok, resp = p.send('+233244000000', 'hi')
        self.assertTrue(ok)
        self.assertNotIn('delivered', resp)
        self.assertEqual(resp['code'], 'OK')  # accepted, not delivered

    # ---------- Phone normalization ----------
    def test_phone_normalization_ghana(self):
        self.assertEqual(app_module.normalize_phone('0241234567'), '+233241234567')
        self.assertEqual(app_module.normalize_phone('+233241234567'), '+233241234567')
        self.assertEqual(app_module._phone_digits('0241234567'), '241234567')
        self.assertEqual(app_module._phone_digits('+233241234567'), '241234567')

    # ---------- OTP flow ----------
    def test_otp_request_and_verify(self):
        r = self._request_otp('0241234567', code=123456)
        self.assertEqual(r.status_code, 200)
        r = self._verify_otp('0241234567', '123456')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['token'])
        self.assertTrue(body['user']['phone_verified'])

    def test_otp_incorrect_rejected(self):
        self._request_otp('0241111222', code=123456)
        r = self._verify_otp('0241111222', '000000')
        self.assertEqual(r.status_code, 400)

    def test_otp_expired_rejected(self):
        self._request_otp('0241111333', code=123456)
        doc = app_module._active_otp(app_module._phone_digits('0241111333'))
        doc['expires_ts'] = 1.0  # epoch 1970 — far in the past
        r = self._verify_otp('0241111333', '123456')
        self.assertEqual(r.status_code, 400)

    def test_otp_five_attempts(self):
        self._request_otp('0241111444', code=123456)
        for _ in range(5):
            self._verify_otp('0241111444', '000000')
        r = self._verify_otp('0241111444', '123456')  # even correct now rejected
        self.assertEqual(r.status_code, 400)

    def test_otp_resend_cooldown(self):
        self._request_otp('0241111555', code=123456)
        r = self._request_otp('0241111555', code=123456)
        self.assertEqual(r.status_code, 429)

    def test_otp_rate_limit_helper(self):
        key = 'otp:test:key'
        self.assertTrue(app_module._rate_limit_ok(key, 2))
        self.assertTrue(app_module._rate_limit_ok(key, 2))
        self.assertFalse(app_module._rate_limit_ok(key, 2))

    def test_otp_single_use(self):
        self._request_otp('0241111666', code=123456)
        self.assertEqual(self._verify_otp('0241111666', '123456').status_code, 200)
        r = self._verify_otp('0241111666', '123456')
        self.assertEqual(r.status_code, 400)

    def test_otp_not_stored_plaintext(self):
        self._request_otp('0241111777', code=123456)
        doc = app_module._active_otp(app_module._phone_digits('0241111777'))
        self.assertNotEqual(doc['otp_hash'], '123456')
        self.assertNotIn('123456', json.dumps(doc))

    # ---------- Linking / history / ownership ----------
    def test_existing_customer_linked_and_history_preserved(self):
        self.client.post('/api/orders', json={'customer_name': 'Ada', 'customer_phone': '0242000000',
                                              'customer_location': 'Mpasatia',
                                              'items': [{'id': 'fried-rice', 'quantity': 1}]})
        c = self.client.post('/api/auth/phone-name', json={'phone': '0242000000', 'name': 'Ada'}).get_json()
        cid = c['user']['id']
        # unverified -> denied
        self.assertEqual(self.client.get('/api/my-orders', headers=self._h(c['token'])).status_code, 403)
        # verify via OTP (same phone)
        self._request_otp('0242000000', code=654321)
        r = self._verify_otp('0242000000', '654321')
        self.assertEqual(r.status_code, 200)
        user = app_module._find_user_by_id(cid)
        self.assertTrue(user['phone_verified'])
        self.assertEqual(user['provider'], 'gonline_otp')
        # verified -> history preserved
        res = self.client.get('/api/my-orders', headers=self._h(r.get_json()['token']))
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()['orders'])

    def test_ownership_isolation(self):
        self._request_otp('0243000000', code=111111)
        tok_a = self._verify_otp('0243000000', '111111').get_json()['token']
        self._request_otp('0243000001', code=222222)
        tok_b = self._verify_otp('0243000001', '222222').get_json()['token']
        self.client.post('/api/orders', json={'customer_name': 'A', 'customer_phone': '0243000000',
                                              'customer_location': 'x', 'items': [{'id': 'fried-rice', 'quantity': 1}]})
        a_orders = self.client.get('/api/my-orders', headers=self._h(tok_a)).get_json()['orders']
        b_orders = self.client.get('/api/my-orders', headers=self._h(tok_b)).get_json()['orders']
        self.assertTrue(a_orders)
        self.assertFalse(b_orders)  # B sees nothing of A's

    def test_google_login_disabled(self):
        r = self.client.post('/api/auth/google', json={'email': 'x@y.com'})
        self.assertEqual(r.status_code, 403)

    def test_staff_admin_auth_unaffected(self):
        self.assertTrue(self.admin['success'])
        self.client.post('/api/staff', json={'username': 'w.staff', 'name': 'W', 'role': 'staff', 'password': 'Wstaff123!'},
                         headers=self.admin_h)
        s = self.client.post('/api/auth/staff-login', json={'username': 'w.staff', 'password': 'Wstaff123!'})
        self.assertEqual(s.status_code, 200)

    # ---------- SMS reliability (unchanged from earlier) ----------
    def test_provider_failure_records_structured_reason(self):
        provider = FakeSmsProvider(fail_times=1)
        with mock.patch.object(app_module, 'get_sms_provider', return_value=provider):
            ok, doc = app_module.send_sms('0249000000', 'hi', 'SM-1', 'order_placed')
        self.assertFalse(ok)
        self.assertEqual(doc['status'], 'failed')
        self.assertEqual(doc['failure_reason'], 'transient down')
        self.assertTrue(doc['retryable'])

    def test_retry_scheduling_and_success(self):
        provider = FakeSmsProvider(fail_times=1)
        with mock.patch.object(app_module, 'get_sms_provider', return_value=provider):
            ok, doc = app_module.send_sms('0249000001', 'hi', 'SM-2', 'order_placed')
            doc['next_retry_at'] = '2000-01-01T00:00:00'
            count = app_module._retry_due_sms(limit=10)
        self.assertEqual(count, 1)
        matching = [l for l in app_module.sms_logs_db if l.get('dedupe_key') == doc['dedupe_key']]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]['status'], 'accepted')

    def test_retry_limit_reached(self):
        provider = FakeSmsProvider(fail_times=1000)
        with mock.patch.object(app_module, 'get_sms_provider', return_value=provider):
            ok, doc = app_module.send_sms('0249000002', 'hi', 'SM-3', 'order_placed')
            dk = doc['dedupe_key']
            for _ in range(app_module.SMS_MAX_RETRIES):
                doc['next_retry_at'] = '2000-01-01T00:00:00'
                app_module._retry_due_sms(limit=10)
                doc = next(l for l in app_module.sms_logs_db if l.get('dedupe_key') == dk)
        self.assertEqual(doc['retry_count'], app_module.SMS_MAX_RETRIES)
        self.assertIsNone(doc['next_retry_at'])

    def test_manual_retry_endpoint(self):
        provider = FakeSmsProvider(fail_times=1)
        with mock.patch.object(app_module, 'get_sms_provider', return_value=provider):
            ok, doc = app_module.send_sms('0249000004', 'hi', 'SM-5', 'order_placed')
        r = self.client.post('/api/sms/retry', json={'dedupe_key': doc['dedupe_key']}, headers=self.admin_h)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['ok'])

    def test_mock_never_reported_delivered(self):
        ok, doc = app_module.send_sms('0249000006', 'hi', 'SM-7', 'order_placed')
        self.assertTrue(ok)
        self.assertEqual(doc['status'], 'simulated')
        self.assertNotIn('delivered', doc['status'])

    # ---------- Phase 7B security hardening ----------
    def _run_import(self, overrides, pops=()):
        import subprocess, sys
        env = dict(os.environ)
        env.update(overrides)
        for k in pops:
            env.pop(k, None)
        env['MONGODB_URI'] = 'mongodb://127.0.0.1:59999/?serverSelectionTimeoutMS=1000'
        r = subprocess.run([sys.executable, '-c', 'import app'], capture_output=True, text=True,
                           env=env, cwd=os.path.dirname(os.path.abspath(__file__)))
        return r

    def test_otp_secret_required_in_production(self):
        r = self._run_import({'RENDER': '1', 'JWT_SECRET': 'x' * 40},
                             pops=('OTP_SECRET', 'SMS_PROVIDER', 'FLASK_ENV'))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('OTP_SECRET', r.stderr)

    def test_sms_provider_required_in_production(self):
        r = self._run_import({'RENDER': '1', 'JWT_SECRET': 'x' * 40, 'OTP_SECRET': 'y' * 40, 'SMS_PROVIDER': 'mock'},
                             pops=('FLASK_ENV',))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('SMS_PROVIDER', r.stderr)

    def test_client_ip_default_remote_addr(self):
        with app_module.app.test_request_context('/', environ_base={'REMOTE_ADDR': '1.2.3.4'},
                                                 headers={'X-Forwarded-For': '9.9.9.9'}):
            self.assertEqual(app_module._client_ip(), '1.2.3.4')

    def test_client_ip_trusted_proxy(self):
        with mock.patch.dict('os.environ', {'TRUSTED_PROXY': '1'}):
            with app_module.app.test_request_context('/', environ_base={'REMOTE_ADDR': '10.0.0.1'},
                                                     headers={'X-Forwarded-For': 'spoofed, real-client'}):
                self.assertEqual(app_module._client_ip(), 'real-client')

    def test_sms_recipient_normalized(self):
        ok, doc = app_module.send_sms('0242292939', 'hi', 'SM-N', 'order_placed')
        self.assertTrue(ok)
        self.assertEqual(doc['recipient'], '+233242292939')

    def test_otp_send_failure_reason_safe(self):
        provider = FakeSmsProvider(fail_times=1)
        with mock.patch.object(app_module, 'get_sms_provider', return_value=provider):
            ok, reason = app_module._send_otp_sms('+233241234567', 'Your code is 123456')
        self.assertFalse(ok)
        self.assertIsNotNone(reason)
        self.assertNotIn('123456', reason)  # OTP never leaks into the reason


if __name__ == '__main__':
    unittest.main(verbosity=2)
