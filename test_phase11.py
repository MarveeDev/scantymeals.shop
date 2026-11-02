"""
Phase 11 — Customer-defined food amounts (pay-what-you-want above a minimum).

Run with:  .venv\\Scripts\\python test_phase11.py

Runs against the Flask app in IN-MEMORY mode.
"""

import os
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


class Phase11AmountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert app_module.MONGODB_CONNECTED is False, "tests require in-memory mode"
        cls.client = app_module.app.test_client()
        cls.admin = cls.client.post('/api/auth/admin-login',
                                    json={'email': 'admin@scanty.com', 'password': 'admin123'}).get_json()
        cls.admin_h = {'Authorization': 'Bearer ' + cls.admin['token']}

    def setUp(self):
        app_module.sms_outbox_db.clear()
        app_module.sms_logs_db.clear()

    def place_order(self, items):
        return self.client.post('/api/orders', json={
            'customer_name': 'T', 'customer_phone': '0241111111',
            'customer_location': 'x', 'items': items,
        })

    def pos_sale(self, items):
        return self.client.post('/api/pos/sale', json={'items': items, 'payment_method': 'cash'},
                                headers=self.admin_h)

    # ---------- customer custom amounts ----------
    def test_fried_rice_below_min_rejected(self):
        r = self.place_order([{'id': 'fried-rice', 'quantity': 1, 'amount': 29.99}])
        self.assertEqual(r.status_code, 400)

    def test_fried_rice_valid_higher(self):
        r = self.place_order([{'id': 'fried-rice', 'quantity': 2, 'amount': 35}])
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()['order']['total'], 70.0)

    def test_spaghetti_below_min_rejected(self):
        r = self.place_order([{'id': 'spaghetti', 'quantity': 1, 'amount': 19.99}])
        self.assertEqual(r.status_code, 400)

    def test_spaghetti_valid(self):
        r = self.place_order([{'id': 'spaghetti', 'quantity': 1, 'amount': 25}])
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()['order']['total'], 25.0)

    def test_defaults_to_minimum_when_no_amount(self):
        r = self.place_order([{'id': 'fried-rice', 'quantity': 1}])
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()['order']['total'], 30.0)

    def test_invalid_amounts_rejected(self):
        self.assertEqual(self.place_order([{'id': 'fried-rice', 'quantity': 1, 'amount': 0}]).status_code, 400)
        self.assertEqual(self.place_order([{'id': 'fried-rice', 'quantity': 1, 'amount': -5}]).status_code, 400)
        self.assertEqual(self.place_order([{'id': 'fried-rice', 'quantity': 1, 'amount': 'abc'}]).status_code, 400)

    def test_indomie_fixed_ignores_amount(self):
        r = self.place_order([{'id': 'indomie', 'quantity': 1, 'amount': 999}])
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()['order']['total'], 30.0)

    def test_mixed_items(self):
        r = self.place_order([{'id': 'fried-rice', 'quantity': 1, 'amount': 35},
                              {'id': 'indomie', 'quantity': 1}])
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()['order']['total'], 65.0)

    # ---------- POS uses fixed price ----------
    def test_pos_uses_fixed_price(self):
        r = self.pos_sale([{'id': 'fried-rice', 'quantity': 1}])
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()['order']['total'], 40.0)

    def test_pos_ignores_custom_amount(self):
        r = self.pos_sale([{'id': 'fried-rice', 'quantity': 1, 'amount': 100}])
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()['order']['total'], 40.0)

    # ---------- menu exposure ----------
    def test_menu_exposes_custom_amount_fields(self):
        items = {i['name']: i for i in self.client.get('/api/menu').get_json()['items']}
        self.assertTrue(items['Fried Rice']['allow_custom_amount'])
        self.assertEqual(items['Fried Rice']['min_price'], 30.0)
        self.assertTrue(items['Spaghetti']['allow_custom_amount'])
        self.assertEqual(items['Spaghetti']['min_price'], 20.0)
        self.assertFalse(items['Indomie']['allow_custom_amount'])

    # ---------- frontend regression (Add to Cart onclick) ----------
    def test_frontend_add_to_cart_onclick_not_broken(self):
        # Guard the exact production bug: the custom-amount button's inline
        # onclick must use single-quote delimiters so the double quotes emitted
        # by JSON.stringify(mealId) do not terminate the attribute early.
        import pathlib
        here = pathlib.Path(__file__).resolve().parent
        html = (here / 'index.html').read_text(encoding='utf-8')
        self.assertIn("onclick='addToCartWithAmount(", html)
        self.assertNotIn('onclick="addToCartWithAmount(', html)
        # fixed-meal button (Indomie) still wired correctly
        self.assertIn("onclick='addToCart(", html)


if __name__ == '__main__':
    unittest.main(verbosity=2)
