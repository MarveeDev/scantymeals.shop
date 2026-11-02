"""
Phase 5 — Cash payment, cash collection, customer SMS & dual admin notifications.

Run with:  .venv\\Scripts\\python test_phase5.py

Runs against the Flask app in IN-MEMORY mode. SMS uses the Mock provider
(clearly simulated — never real delivery).
"""

import os
import unittest
from unittest import mock

os.environ['MONGODB_URI'] = 'mongodb://127.0.0.1:59999/?serverSelectionTimeoutMS=1000'
os.environ.pop('RENDER', None)
os.environ.pop('FLASK_ENV', None)
os.environ['SMS_PROVIDER'] = 'mock'
os.environ['SUPER_ADMIN_EMAIL'] = 'super@scanty.com'
os.environ['SUPER_ADMIN_PASSWORD'] = 'SuperSecret123!'
os.environ['ADMIN_EMAIL'] = 'admin@scanty.com'
os.environ['ADMIN_PASSWORD'] = 'admin123'

import app as app_module


class Phase5Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert app_module.MONGODB_CONNECTED is False, "tests require in-memory mode"
        cls.client = app_module.app.test_client()
        cls.admin = cls.client.post('/api/auth/admin-login',
                                    json={'email': 'admin@scanty.com', 'password': 'admin123'}).get_json()
        cls.super = cls.client.post('/api/auth/admin-login',
                                    json={'email': 'super@scanty.com', 'password': 'SuperSecret123!'}).get_json()
        cls.admin_h = {'Authorization': 'Bearer ' + cls.admin['token']}
        cls.super_h = {'Authorization': 'Bearer ' + cls.super['token']}

        cls.client.post('/api/staff', json={'username': 'ama.pos', 'name': 'Ama Staff',
                                            'role': 'staff', 'password': 'AmaPos123!'}, headers=cls.admin_h)
        cls.staff = cls.client.post('/api/auth/staff-login',
                                    json={'username': 'ama.pos', 'password': 'AmaPos123!'}).get_json()
        cls.staff_h = {'Authorization': 'Bearer ' + cls.staff['token']}

        # Configure admin SMS phone
        app_module._set_setting(app_module.ADMIN_SMS_SETTING_KEY, '+233242292939')

    def _h(self, tok): return {'Authorization': 'Bearer ' + tok}

    def place_order(self, phone='0241234567', delivery_method='delivery', items=None, **kw):
        payload = {'customer_name': 'Test Customer', 'customer_phone': phone,
                   'customer_location': 'Mpasatia', 'delivery_method': delivery_method,
                   'items': items or [{'id': 'fried-rice', 'quantity': 1}]}
        payload.update(kw)
        return self.client.post('/api/orders', json=payload)

    def customer_token(self, phone='0241111111', name='Cust'):
        return self.client.post('/api/auth/phone-name', json={'phone': phone, 'name': name}).get_json()['token']

    def sms_logs(self, event=None):
        logs = [s for s in app_module.sms_logs_db if (not event or s.get('event_type') == event)]
        return logs

    # ---------- Cash collection ----------
    def test_customer_order_is_pending_cash(self):
        r = self.place_order(delivery_method='delivery')
        self.assertEqual(r.status_code, 201)
        oid = r.get_json()['order']['id']
        order = next(o for o in app_module.orders_db if o['id'] == oid)
        self.assertEqual(order['payment_status'], 'pending')
        self.assertEqual(order['payment_method'], 'cash')
        self.assertEqual(order['delivery_method'], 'delivery')

    def test_confirm_cash_success(self):
        r = self.place_order(phone='0242222222')
        oid = r.get_json()['order']['id']
        res = self.client.post('/api/orders/' + oid + '/confirm-cash', json={}, headers=self.staff_h)
        self.assertEqual(res.status_code, 200)
        order = next(o for o in app_module.orders_db if o['id'] == oid)
        self.assertEqual(order['payment_status'], 'paid')
        self.assertIn('cash_received_by', order)
        self.assertIn('cash_received_at', order)

    def test_confirm_cash_already_paid(self):
        r = self.place_order(phone='0243333333')
        oid = r.get_json()['order']['id']
        self.client.post('/api/orders/' + oid + '/confirm-cash', json={}, headers=self.staff_h)
        res = self.client.post('/api/orders/' + oid + '/confirm-cash', json={}, headers=self.staff_h)
        self.assertEqual(res.status_code, 409)

    def test_confirm_cash_unknown_order(self):
        res = self.client.post('/api/orders/SCM-99999/confirm-cash', json={}, headers=self.staff_h)
        self.assertEqual(res.status_code, 404)

    def test_confirm_cash_unauthorized(self):
        r = self.place_order(phone='0244444444')
        oid = r.get_json()['order']['id']
        # no token
        self.assertEqual(self.client.post('/api/orders/' + oid + '/confirm-cash', json={}).status_code, 401)
        # customer token
        c = self.customer_token('0245555555')
        self.assertEqual(self.client.post('/api/orders/' + oid + '/confirm-cash', json={},
                                          headers=self._h(c)).status_code, 403)

    def test_confirm_cash_audit_log(self):
        r = self.place_order(phone='0246666666')
        oid = r.get_json()['order']['id']
        self.client.post('/api/orders/' + oid + '/confirm-cash', json={}, headers=self.staff_h)
        self.assertTrue(any(a.get('event') == 'cash_confirmed' and a.get('order_id') == oid
                            for a in app_module.audit_logs_db))

    def test_staff_cannot_confirm_non_customer_order(self):
        # inject a pending order with a non-CUSTOMER source
        app_module.orders_db.append({
            "id": "SCM-PENDING-1", "source": "ADMIN", "customer_name": "X", "customer_phone": "0247777777",
            "items": [{"name": "Fried Rice", "quantity": 1, "unit_price": 40, "line_total": 40}],
            "total": 40.0, "payment_method": "cash", "payment_status": "pending", "status": "Confirmed",
            "timestamp": "2025-01-01T00:00:00", "date": "2025-01-01",
        })
        res = self.client.post('/api/orders/SCM-PENDING-1/confirm-cash', json={}, headers=self.staff_h)
        self.assertEqual(res.status_code, 403)

    # ---------- Customer & admin notifications ----------
    def test_customer_sms_sent_on_order(self):
        r = self.place_order(phone='0248888888')
        self.assertEqual(r.status_code, 201)
        oid = r.get_json()['order']['id']
        logs = self.sms_logs('order_placed')
        # recipient is normalized to international format (Phase 7B Fix 4)
        self.assertTrue(any(l.get('order_id') == oid and l.get('recipient') == '+233248888888' for l in logs))

    def test_admin_dashboard_notification_created(self):
        r = self.place_order(phone='0249999999')
        oid = r.get_json()['order']['id']
        notifs = [n for n in app_module.notifications_db if n.get('order_id') == oid]
        self.assertTrue(any(n.get('type') == 'new_order' and n.get('source') == 'CUSTOMER' for n in notifs))

    def test_admin_sms_sent(self):
        r = self.place_order(phone='0240000001')
        oid = r.get_json()['order']['id']
        logs = self.sms_logs('admin_new_order')
        self.assertTrue(any(l.get('order_id') == oid and l.get('recipient') == '+233242292939' for l in logs))

    def test_delivery_vs_pickup_wording(self):
        r1 = self.place_order(phone='0240000002', delivery_method='delivery')
        m1 = next(l['message'] for l in self.sms_logs('order_placed')
                  if l.get('order_id') == r1.get_json()['order']['id'])
        self.assertIn('Cash on Delivery', m1)

        r2 = self.place_order(phone='0240000003', delivery_method='pickup')
        m2 = next(l['message'] for l in self.sms_logs('order_placed')
                  if l.get('order_id') == r2.get_json()['order']['id'])
        self.assertIn('Cash on Pickup', m2)

    def test_sms_failure_does_not_cancel_order(self):
        with mock.patch.object(app_module, 'send_sms', side_effect=RuntimeError('sms boom')):
            r = self.place_order(phone='0240000004')
            self.assertEqual(r.status_code, 201)
            oid = r.get_json()['order']['id']
            self.assertTrue(any(o['id'] == oid for o in app_module.orders_db))

    def test_dashboard_failure_does_not_cancel_order(self):
        with mock.patch.object(app_module, '_create_notification', side_effect=RuntimeError('notif boom')):
            r = self.place_order(phone='0240000005')
            self.assertEqual(r.status_code, 201)
            oid = r.get_json()['order']['id']
            self.assertTrue(any(o['id'] == oid for o in app_module.orders_db))

    def test_missing_admin_phone_handled_safely(self):
        key = app_module.ADMIN_SMS_SETTING_KEY
        saved = app_module.settings_db.get(key)
        app_module.settings_db.pop(key, None)
        try:
            r = self.place_order(phone='0240000006')
            self.assertEqual(r.status_code, 201)
        finally:
            app_module.settings_db[key] = saved

    def test_unauthorized_settings_access(self):
        self.assertEqual(self.client.get('/api/settings/admin-sms', headers=self.admin_h).status_code, 403)
        res = self.client.get('/api/settings/admin-sms', headers=self.super_h)
        self.assertEqual(res.status_code, 200)

    def test_settings_update_and_validation(self):
        res = self.client.put('/api/settings/admin-sms', json={'value': '0242292939'}, headers=self.super_h)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()['value'], '+233242292939')
        # invalid phone rejected
        self.assertEqual(self.client.put('/api/settings/admin-sms', json={'value': 'abc'}, headers=self.super_h).status_code, 400)

    # ---------- Internal sales ----------
    def test_staff_pos_sale_notification(self):
        r = self.client.post('/api/pos/sale', json={'items': [{'id': 'fried-rice', 'quantity': 1}], 'payment_method': 'cash'},
                             headers=self.staff_h)
        self.assertEqual(r.status_code, 201)
        oid = r.get_json()['order']['id']
        notifs = [n for n in app_module.notifications_db if n.get('order_id') == oid]
        self.assertTrue(any(n.get('source') == 'STAFF' for n in notifs))

    def test_admin_pos_sale_no_duplicate_alert(self):
        r = self.client.post('/api/pos/sale', json={'items': [{'id': 'indomie', 'quantity': 1}], 'payment_method': 'cash'},
                             headers=self.admin_h)
        oid = r.get_json()['order']['id']
        notifs = [n for n in app_module.notifications_db if n.get('order_id') == oid]
        self.assertEqual(notifs, [])

    # ---------- Reliability ----------
    def test_duplicate_order_event_idempotency(self):
        key = 'cust-idem-1'
        r1 = self.place_order(phone='0240000007', idempotency_key=key)
        r2 = self.place_order(phone='0240000007', idempotency_key=key)
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.get_json()['duplicate'])
        self.assertEqual(r1.get_json()['order']['id'], r2.get_json()['order']['id'])

    def test_duplicate_sms_prevention(self):
        r = self.place_order(phone='0240000008')
        oid = r.get_json()['order']['id']
        # manual re-send for same event/recipient must be deduped
        ok, doc = app_module.send_sms('0240000008', 'dup', oid, 'order_placed')
        self.assertFalse(ok)
        self.assertEqual(doc['status'], 'duplicate')
        self.assertEqual(sum(1 for l in self.sms_logs('order_placed') if l.get('order_id') == oid), 1)

    # ---------- my-orders hardening ----------
    def test_my_orders_requires_auth(self):
        self.assertEqual(self.client.get('/api/my-orders?phone=0240000009').status_code, 401)

    def test_customer_my_orders_own_only(self):
        self.place_order(phone='0240000010')
        tok = self.customer_token('0240000010', 'Owner')
        # Phase 7: private order history requires a verified phone.
        app_module._find_user_by_phone_digits(app_module._phone_digits('0240000010'))['phone_verified'] = True
        orders = self.client.get('/api/my-orders', headers=self._h(tok)).get_json()['orders']
        self.assertTrue(orders)
        self.assertTrue(all(o['customer_phone'] == '0240000010' for o in orders))

    def test_staff_can_query_by_phone(self):
        self.place_order(phone='0240000011')
        orders = self.client.get('/api/my-orders?phone=0240000011', headers=self.staff_h).get_json()['orders']
        self.assertTrue(all(o['customer_phone'] == '0240000011' for o in orders))


if __name__ == '__main__':
    unittest.main(verbosity=2)
