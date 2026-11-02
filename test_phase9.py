"""
Phase 9 — Post-deployment repair verification (frontend structure + config +
a backend behavior check).

Run with:  .venv\\Scripts\\python test_phase9.py

Most Phase 9 fixes are frontend/static (cart onclick, admin customers UI,
credential-strip sanitizer, firebase rewrites). These are verified by reading
the actual files. One backend behavior (POS sales do not emit a customer SMS)
is verified against the Flask app in-memory.
"""

import os
import json
import unittest

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

ROOT = os.path.dirname(os.path.abspath(__file__))


def read(p):
    with open(os.path.join(ROOT, p), 'r', encoding='utf-8') as f:
        return f.read()


class Phase9FrontendTests(unittest.TestCase):
    def test_cart_add_button_uses_single_quoted_onclick(self):
        html = read('index.html')
        # The bug was nested double quotes: onclick="addToCart("...")"
        self.assertNotIn('onclick="addToCart(', html)
        self.assertIn("onclick='addToCart(", html)

    def test_admin_customer_table_present(self):
        html = read('admin.html')
        self.assertIn('renderCustomers', html)
        self.assertIn('customerSearch', html)
        self.assertIn('filterCustomers', html)
        self.assertIn('Total Spend', html)

    def test_login_pages_strip_credentials(self):
        for p in ('admin-login.html', 'staff-login.html'):
            html = read(p)
            self.assertIn('history.replaceState', html)
            self.assertIn('SENSITIVE', html)

    def test_firebase_rewrites_cover_all_pages(self):
        cfg = json.loads(read('firebase.json'))
        rewrites = cfg['hosting']['rewrites']
        sources = [r['source'] for r in rewrites]
        for s in ('/', '/admin', '/admin-login', '/staff', '/staff-login'):
            self.assertIn(s, sources)
        # catch-all must be last so it never shadows a specific page
        self.assertEqual(rewrites[-1]['source'], '**')


class Phase9BackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert app_module.MONGODB_CONNECTED is False, "tests require in-memory mode"
        cls.client = app_module.app.test_client()
        cls.admin = cls.client.post('/api/auth/admin-login',
                                    json={'email': 'admin@scanty.com', 'password': 'admin123'}).get_json()
        cls.admin_h = {'Authorization': 'Bearer ' + cls.admin['token']}

    def test_pos_sale_does_not_emit_customer_sms(self):
        # POS cash sales are walk-in (customer present) — no customer SMS by design.
        before = [l for l in app_module.sms_logs_db if l.get('event_type') == 'order_placed']
        r = self.client.post('/api/pos/sale', json={
            'items': [{'id': 'fried-rice', 'quantity': 1}], 'payment_method': 'cash',
            'customer_name': 'Walk-in', 'customer_phone': '0241234567'}, headers=self.admin_h)
        self.assertEqual(r.status_code, 201)
        after = [l for l in app_module.sms_logs_db if l.get('event_type') == 'order_placed']
        self.assertEqual(len(after), len(before))  # no new customer order SMS

    def test_customer_checkout_emits_customer_sms(self):
        r = self.client.post('/api/orders', json={
            'customer_name': 'Online Customer', 'customer_phone': '0249876543',
            'customer_location': 'Mpasatia', 'items': [{'id': 'fried-rice', 'quantity': 1}]})
        self.assertEqual(r.status_code, 201)
        oid = r.get_json()['order']['id']
        logs = [l for l in app_module.sms_logs_db if l.get('event_type') == 'order_placed']
        self.assertTrue(any(l.get('order_id') == oid and l.get('recipient') == '+233249876543' for l in logs))


if __name__ == '__main__':
    unittest.main(verbosity=2)
