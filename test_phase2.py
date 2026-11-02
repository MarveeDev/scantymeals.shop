"""
Phase 2 — Database-driven menu & server-side pricing tests.

Run with:  .venv\\Scripts\\python test_phase2.py

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


class Phase2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert app_module.MONGODB_CONNECTED is False, "tests require in-memory mode"
        cls.client = app_module.app.test_client()
        # admin token for protected calls
        r = cls.client.post('/api/auth/admin-login',
                            json={'email': 'admin@scanty.com', 'password': 'admin123'})
        cls.admin_token = r.get_json()['token']
        cls.admin_h = {'Authorization': 'Bearer ' + cls.admin_token}

    def post_order(self, items, **extra):
        payload = {'customer_name': 'Test', 'customer_phone': '0240000000',
                   'customer_location': 'Mpasatia', 'items': items}
        payload.update(extra)
        return self.client.post('/api/orders', json=payload)

    def admin_get(self, path):
        return self.client.get(path, headers=self.admin_h)

    # ---------- Menu retrieval ----------
    def test_menu_returns_three_approved_meals(self):
        r = self.client.get('/api/menu')
        self.assertEqual(r.status_code, 200)
        items = r.get_json()['items']
        names = {i['name'] for i in items}
        # The three approved meals must always be present and available.
        for expected in ('Fried Rice', 'Spaghetti', 'Indomie'):
            self.assertIn(expected, names)
        for i in items:
            self.assertIn('price', i)
            self.assertIn('id', i)
            self.assertIn('available', i)
            self.assertTrue(i['available'])

    def test_menu_prices_from_db(self):
        items = {i['name']: i['price'] for i in self.client.get('/api/menu').get_json()['items']}
        self.assertEqual(items['Fried Rice'], 40.0)
        self.assertEqual(items['Spaghetti'], 40.0)
        self.assertEqual(items['Indomie'], 30.0)

    def test_seed_no_duplicates(self):
        before = len(self.admin_get('/api/admin/meals').get_json()['items'])
        app_module.seed_meals()
        app_module.seed_meals()
        after = len(self.admin_get('/api/admin/meals').get_json()['items'])
        self.assertEqual(before, after)

    # ---------- Admin menu management ----------
    def test_admin_meals_requires_auth(self):
        self.assertEqual(self.client.get('/api/admin/meals').status_code, 401)
        self.assertEqual(self.client.post('/api/admin/meals', json={'name': 'X'}).status_code, 401)

    def test_admin_create_and_update_price(self):
        r = self.client.post('/api/admin/meals',
                             json={'name': 'Jollof', 'price': 25.0, 'category': 'Rice'},
                             headers=self.admin_h)
        self.assertEqual(r.status_code, 201)
        meal_id = r.get_json()['meal']['id']

        r = self.client.put('/api/admin/meals/' + meal_id,
                            json={'price': 35.0, 'description': 'Spicy jollof'},
                            headers=self.admin_h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['meal']['price'], 35.0)

        # Ordering uses the updated DB price (35), not any client price.
        r = self.post_order([{'id': meal_id, 'quantity': 2, 'price': 1, 'name': 'hacked'}])
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()['order']['total'], 70.0)

    def test_admin_availability_toggle_blocks_order(self):
        r = self.client.patch('/api/admin/meals/spaghetti/availability',
                              json={'available': False}, headers=self.admin_h)
        self.assertEqual(r.status_code, 200)
        # unavailable meal is rejected
        self.assertEqual(self.post_order([{'id': 'spaghetti', 'quantity': 1}]).status_code, 400)
        # restore
        self.client.patch('/api/admin/meals/spaghetti/availability',
                          json={'available': True}, headers=self.admin_h)

    # ---------- Server-side pricing ----------
    def test_correct_pricing(self):
        # Fried Rice is now "pay-what-you-want from ₵30" (no amount → minimum).
        r = self.post_order([{'id': 'fried-rice', 'quantity': 2}, {'id': 'indomie', 'quantity': 1}])
        self.assertEqual(r.status_code, 201)
        order = r.get_json()['order']
        self.assertEqual(order['total'], 90.0)  # 30*2 + 30
        self.assertEqual(order['subtotal'], 90.0)

    def test_forged_price_ignored(self):
        r = self.post_order([{'id': 'fried-rice', 'quantity': 1, 'price': 0.01, 'name': 'hacked'}])
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()['order']['total'], 30.0)

        # verify stored order unit_price reflects DB minimum, not the forged value
        orders = self.admin_get('/api/orders').get_json()['orders']
        target = next(o for o in orders if o['customer_name'] == 'Test' and o['total'] == 30.0)
        self.assertEqual(target['items'][0]['unit_price'], 30.0)
        self.assertTrue(target['price_verified'])

    def test_invalid_meal_id(self):
        self.assertEqual(self.post_order([{'id': 'does-not-exist', 'quantity': 1}]).status_code, 400)

    def test_invalid_quantities(self):
        self.assertEqual(self.post_order([{'id': 'fried-rice', 'quantity': 0}]).status_code, 400)
        self.assertEqual(self.post_order([{'id': 'fried-rice', 'quantity': -3}]).status_code, 400)
        self.assertEqual(self.post_order([{'id': 'fried-rice', 'quantity': 2.5}]).status_code, 400)
        self.assertEqual(self.post_order([{'id': 'fried-rice', 'quantity': 'abc'}]).status_code, 400)
        self.assertEqual(self.post_order([{'id': 'fried-rice', 'quantity': 101}]).status_code, 400)

    def test_empty_cart(self):
        self.assertEqual(self.post_order([]).status_code, 400)

    # ---------- Revenue calculations ----------
    def test_cancelled_order_excluded_from_revenue(self):
        r = self.post_order([{'id': 'indomie', 'quantity': 1}])  # 30 pending
        self.assertEqual(r.status_code, 201)
        order_id = r.get_json()['order']['id']

        summary_before = self.admin_get('/api/analytics/summary').get_json()

        # cancel the order
        r = self.client.put('/api/orders/' + order_id + '/status',
                            json={'status': 'Cancelled'}, headers=self.admin_h)
        self.assertEqual(r.status_code, 200)

        summary = self.admin_get('/api/analytics/summary').get_json()
        self.assertEqual(summary['total_revenue'], summary_before['total_revenue'])  # cancelled not counted
        self.assertGreaterEqual(summary['cancelled_revenue'], 30.0)

    def test_pending_not_counted_as_paid(self):
        r = self.post_order([{'id': 'fried-rice', 'quantity': 1}])  # pending
        self.assertEqual(r.status_code, 201)

        summary = self.admin_get('/api/analytics/summary').get_json()
        # pending is in pending_revenue but NOT in total_revenue (paid)
        self.assertGreaterEqual(summary['pending_revenue'], 30.0)
        self.assertGreaterEqual(summary['gross_revenue'], 30.0)

        # every pending order is also in gross, and paid is strictly <= gross
        self.assertLessEqual(summary['total_revenue'], summary['gross_revenue'])

    def test_historical_order_compatibility(self):
        # Simulate a legacy order (no payment_status, no price_verified).
        legacy = {
            "id": "SCM-LEGACY-9",
            "customer_name": "Legacy Customer",
            "customer_phone": "0241111111",
            "customer_location": "Mpasatia",
            "items": [{"name": "Fried Rice", "quantity": 2, "price": 40}],
            "total": 80.0,
            "status": "Delivered",
            "timestamp": "2024-01-01T00:00:00",
            "date": "2024-01-01",
        }
        app_module.orders_db.append(legacy)

        # legacy order still listed
        orders = self.admin_get('/api/orders').get_json()['orders']
        self.assertTrue(any(o['id'] == 'SCM-LEGACY-9' for o in orders))

        # legacy order (no payment_status) counts as paid revenue
        summary = self.admin_get('/api/analytics/summary').get_json()
        self.assertGreaterEqual(summary['total_revenue'], 80.0)

    def test_orders_stats_include_revenue_breakdown(self):
        stats = self.admin_get('/api/orders').get_json()['stats']
        for k in ('totalRevenue', 'grossRevenue', 'pendingRevenue', 'cancelledRevenue',
                  'todayOrders', 'todayRevenue'):
            self.assertIn(k, stats)


if __name__ == '__main__':
    unittest.main(verbosity=2)
