"""
Phase 4 — Staff & Admin POS tests (centralized sales).

Run with:  .venv\\Scripts\\python test_phase4.py

Runs against the Flask app in IN-MEMORY mode (no MongoDB writes).
"""

import os
import unittest

os.environ['MONGODB_URI'] = 'mongodb://127.0.0.1:59999/?serverSelectionTimeoutMS=1000'
os.environ.pop('RENDER', None)
os.environ.pop('FLASK_ENV', None)
os.environ['SUPER_ADMIN_EMAIL'] = 'super@scanty.com'
os.environ['SUPER_ADMIN_PASSWORD'] = 'SuperSecret123!'
os.environ['ADMIN_EMAIL'] = 'admin@scanty.com'
os.environ['ADMIN_PASSWORD'] = 'admin123'

import app as app_module


class Phase4PosTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert app_module.MONGODB_CONNECTED is False, "tests require in-memory mode"
        cls.client = app_module.app.test_client()

        cls.admin = cls.client.post('/api/auth/admin-login',
                                    json={'email': 'admin@scanty.com', 'password': 'admin123'}).get_json()
        cls.admin_h = {'Authorization': 'Bearer ' + cls.admin['token']}

        # two staff accounts for isolation testing
        cls.staff_a = cls._make_staff(cls, 'ada.pos', 'AdaPos123!')
        cls.staff_b = cls._make_staff(cls, 'kofi.pos', 'KofiPos123!')

    def _make_staff(self, username, password):
        self.client.post('/api/staff',
                         json={'username': username, 'name': 'Staff ' + username,
                               'role': 'staff', 'password': password},
                         headers=self.admin_h)
        tok = self.client.post('/api/auth/staff-login',
                               json={'username': username, 'password': password}).get_json()['token']
        return {'token': tok, 'username': username}

    def _h(self, tok):
        return {'Authorization': 'Bearer ' + tok}

    def _sale(self, token, items, **extra):
        payload = {'items': items, 'payment_method': 'cash'}
        payload.update(extra)
        return self.client.post('/api/pos/sale', json=payload, headers=self._h(token))

    # ---------- auth ----------
    def test_pos_sale_requires_auth(self):
        r = self.client.post('/api/pos/sale', json={'items': [{'id': 'fried-rice', 'quantity': 1}], 'payment_method': 'cash'})
        self.assertEqual(r.status_code, 401)

    def test_customer_cannot_pos_sale(self):
        c = self.client.post('/api/auth/phone-name', json={'phone': '0249999999', 'name': 'Cust'}).get_json()
        r = self._sale(c['token'], [{'id': 'fried-rice', 'quantity': 1}])
        self.assertEqual(r.status_code, 403)

    def test_staff_cannot_access_admin_orders(self):
        r = self.client.get('/api/orders', headers=self._h(self.staff_a['token']))
        self.assertEqual(r.status_code, 403)

    # ---------- staff/admin sales ----------
    def test_staff_pos_sale(self):
        r = self._sale(self.staff_a['token'], [{'id': 'fried-rice', 'quantity': 2}],
                       customer_name='Ada Customer', customer_phone='0241234567')
        self.assertEqual(r.status_code, 201)
        o = r.get_json()['order']
        self.assertEqual(o['source'], 'STAFF')
        self.assertEqual(o['payment_method'], 'cash')
        self.assertEqual(o['payment_status'], 'paid')
        self.assertEqual(o['status'], 'Completed')
        self.assertEqual(o['total'], 80.0)
        self.assertTrue(o['price_verified'])

    def test_admin_pos_sale_source_admin(self):
        r = self._sale(self.admin['token'], [{'id': 'indomie', 'quantity': 1}])
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()['order']['source'], 'ADMIN')

    def test_forged_price_ignored_pos(self):
        r = self._sale(self.staff_a['token'], [{'id': 'fried-rice', 'quantity': 1, 'price': 0.01, 'name': 'hacked'}])
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()['order']['total'], 40.0)

    def test_invalid_quantity_pos(self):
        r = self._sale(self.staff_a['token'], [{'id': 'fried-rice', 'quantity': 0}])
        self.assertEqual(r.status_code, 400)

    def test_unavailable_meal_pos(self):
        self.client.patch('/api/admin/meals/spaghetti/availability',
                          json={'available': False}, headers=self.admin_h)
        r = self._sale(self.staff_a['token'], [{'id': 'spaghetti', 'quantity': 1}])
        self.assertEqual(r.status_code, 400)
        self.client.patch('/api/admin/meals/spaghetti/availability',
                          json={'available': True}, headers=self.admin_h)

    def test_paystack_not_yet_available(self):
        r = self._sale(self.staff_a['token'], [{'id': 'fried-rice', 'quantity': 1}], payment_method='paystack')
        self.assertEqual(r.status_code, 400)

    # ---------- duplicate prevention ----------
    def test_duplicate_submission_prevented(self):
        key = 'dup-key-0001'
        r1 = self._sale(self.staff_a['token'], [{'id': 'fried-rice', 'quantity': 1}], idempotency_key=key)
        self.assertEqual(r1.status_code, 201)
        r2 = self._sale(self.staff_a['token'], [{'id': 'fried-rice', 'quantity': 1}], idempotency_key=key)
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.get_json()['duplicate'])
        self.assertEqual(r1.get_json()['order']['id'], r2.get_json()['order']['id'])

        orders = self.client.get('/api/orders', headers=self.admin_h).get_json()['orders']
        self.assertEqual(sum(1 for o in orders if o.get('idempotency_key') == key), 1)

    # ---------- receipt data ----------
    def test_receipt_data_present(self):
        r = self._sale(self.staff_a['token'], [{'id': 'fried-rice', 'quantity': 1}], customer_name='Receipt Person')
        o = r.get_json()['order']
        self.assertIn('created_by_name', o)
        self.assertIn('unit_price', o['items'][0])
        self.assertIn('line_total', o['items'][0])
        self.assertEqual(o['payment_method'], 'cash')

    # ---------- isolation & reporting ----------
    def test_staff_sales_isolation(self):
        self._sale(self.staff_a['token'], [{'id': 'fried-rice', 'quantity': 1}])
        self._sale(self.staff_b['token'], [{'id': 'indomie', 'quantity': 1}])

        a_sales = self.client.get('/api/pos/my-sales', headers=self._h(self.staff_a['token'])).get_json()['sales']
        a_ids = {s['id'] for s in a_sales}
        # staff A must not see staff B's sales
        b_orders = [s for s in a_sales if s.get('created_by') == str(self._staff_uid(self.staff_b['username']))]
        self.assertEqual(b_orders, [])
        # every sale staff A sees belongs to staff A
        for s in a_sales:
            if s.get('source') == 'STAFF':
                self.assertEqual(s.get('created_by'), str(self._staff_uid(self.staff_a['username'])))
        self.assertTrue(a_ids)

    def _staff_uid(self, username):
        # resolve in-memory user id by username
        for u in app_module.users_db:
            if u.get('username') == username:
                return u.get('_id')
        return None

    def test_admin_sees_all_pos_sales(self):
        # admin can view both staff and admin sales in one list
        self._sale(self.staff_a['token'], [{'id': 'fried-rice', 'quantity': 1}])
        self._sale(self.admin['token'], [{'id': 'indomie', 'quantity': 1}])

        admin_sales = self.client.get('/api/pos/my-sales', headers=self.admin_h).get_json()['sales']
        sources = {s['source'] for s in admin_sales}
        self.assertIn('STAFF', sources)
        self.assertIn('ADMIN', sources)
        self.assertGreaterEqual(len(admin_sales), 2)

    def test_central_reporting_source_filter(self):
        self._sale(self.staff_a['token'], [{'id': 'fried-rice', 'quantity': 1}])
        self._sale(self.admin['token'], [{'id': 'indomie', 'quantity': 1}])

        staff_orders = self.client.get('/api/orders?source=STAFF', headers=self.admin_h).get_json()['orders']
        self.assertTrue(all(o['source'] == 'STAFF' for o in staff_orders))
        self.assertTrue(staff_orders)

        admin_orders = self.client.get('/api/orders?source=ADMIN', headers=self.admin_h).get_json()['orders']
        self.assertTrue(all(o['source'] == 'ADMIN' for o in admin_orders))
        self.assertTrue(admin_orders)

        paid_orders = self.client.get('/api/orders?payment_status=paid', headers=self.admin_h).get_json()['orders']
        self.assertTrue(all(o['payment_status'] == 'paid' for o in paid_orders))


if __name__ == '__main__':
    unittest.main(verbosity=2)
