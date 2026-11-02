"""
Phase 6 — production-readiness verification tests (ownership, quotation
integrity, payment concurrency, packaging integrity, cancellation, handover).

Run with:  .venv\\Scripts\\python test_phase6_readiness.py

Runs against the Flask app in IN-MEMORY mode.
"""

import os
import unittest

os.environ['MONGODB_URI'] = 'mongodb://127.0.0.1:59999/?serverSelectionTimeoutMS=1000'
os.environ.pop('RENDER', None)
os.environ.pop('FLASK_ENV', None)
os.environ['SMS_PROVIDER'] = 'mock'
os.environ['SUPER_ADMIN_EMAIL'] = 'super@scanty.com'
os.environ['SUPER_ADMIN_PASSWORD'] = 'SuperSecret123!'
os.environ['ADMIN_EMAIL'] = 'admin@scanty.com'
os.environ['ADMIN_PASSWORD'] = 'admin123'

import app as app_module


class Phase6ReadinessTests(unittest.TestCase):
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
        cls.client.post('/api/staff', json={'username': 'rd.staff', 'name': 'Ready Staff',
                                            'role': 'staff', 'password': 'Ready123!'}, headers=cls.admin_h)
        cls.staff = cls.client.post('/api/auth/staff-login',
                                    json={'username': 'rd.staff', 'password': 'Ready123!'}).get_json()
        cls.staff_h = {'Authorization': 'Bearer ' + cls.staff['token']}

    def _h(self, tok): return {'Authorization': 'Bearer ' + tok}

    def _customer_token(self, phone, name='Org'):
        return self.client.post('/api/auth/phone-name', json={'phone': phone, 'name': name}).get_json()['token']

    def _submit(self, phone='0241000001'):
        r = self.client.post('/api/bulk-orders', json={
            'customer_name': 'Org ' + phone[-4:], 'phone': phone, 'event_type': 'Conference',
            'event_date': '2026-12-30', 'fulfillment': 'delivery', 'delivery_address': 'Mpasatia',
            'meals': [{'meal_id': 'fried-rice', 'quantity': 10}],
        })
        return r.get_json()['bulk_order']['reference']

    def _quote(self, ref, qty=10, price=25, deposit=0):
        return self.client.put('/api/bulk-orders/' + ref + '/quotation', json={
            'items': [{'meal_id': 'fried-rice', 'name': 'Fried Rice', 'quoted_qty': qty, 'price_per_pack': price}],
            'deposit_required': deposit,
        }, headers=self.admin_h)

    # ===== 1. Customer ownership =====
    def test_cannot_view_another_customers_bulk_order(self):
        ref = self._submit('0241000002')
        other = self._customer_token('0241000999', 'Attacker')
        res = self.client.get('/api/bulk-orders/' + ref, headers=self._h(other))
        self.assertEqual(res.status_code, 404)

    def test_cannot_accept_another_customers_order(self):
        ref = self._submit('0241000003')
        self._quote(ref)
        other = self._customer_token('0241000998', 'Attacker')
        res = self.client.post('/api/bulk-orders/' + ref + '/accept', headers=self._h(other))
        self.assertEqual(res.status_code, 404)

    def test_my_bulk_orders_excludes_others(self):
        ref = self._submit('0241000004')
        owner = self._customer_token('0241000004', 'Owner')
        other = self._customer_token('0241000997', 'Other')
        # Phase 7: private bulk history requires a verified phone.
        app_module._find_user_by_phone_digits(app_module._phone_digits('0241000004'))['phone_verified'] = True
        app_module._find_user_by_phone_digits(app_module._phone_digits('0241000997'))['phone_verified'] = True
        owner_list = self.client.get('/api/my-bulk-orders', headers=self._h(owner)).get_json()['bulk_orders']
        other_list = self.client.get('/api/my-bulk-orders', headers=self._h(other)).get_json()['bulk_orders']
        self.assertTrue(any(b['reference'] == ref for b in owner_list))
        self.assertFalse(any(b['reference'] == ref for b in other_list))

    # ===== 2. Quotation integrity =====
    def test_accept_without_quotation_rejected(self):
        ref = self._submit('0241000005')  # REQUEST_RECEIVED, no quotation
        tok = self._customer_token('0241000005')
        res = self.client.post('/api/bulk-orders/' + ref + '/accept', headers=self._h(tok))
        self.assertEqual(res.status_code, 409)

    def test_outdated_quotation_version_rejected(self):
        ref = self._submit('0241000006')
        self._quote(ref, qty=10, price=25)   # version 1
        self._quote(ref, qty=10, price=30)   # version 2
        tok = self._customer_token('0241000006')
        res = self.client.post('/api/bulk-orders/' + ref + '/accept',
                               json={'quotation_version': 1}, headers=self._h(tok))
        self.assertEqual(res.status_code, 409)

    def test_quotation_locked_after_acceptance(self):
        ref = self._submit('0241000007')
        self._quote(ref)
        tok = self._customer_token('0241000007')
        self.client.post('/api/bulk-orders/' + ref + '/accept', headers=self._h(tok))
        # re-editing quotation after acceptance must be rejected
        res = self._quote(ref, qty=20, price=25)
        self.assertEqual(res.status_code, 409)

    # ===== 3. Payment concurrency / balance integrity =====
    def test_outstanding_balance_initialised_on_quotation(self):
        ref = self._submit('0241000008')
        self._quote(ref, qty=10, price=25)  # total 250
        doc = app_module._find_bulk(ref)
        self.assertEqual(doc['outstanding_balance'], 250.0)
        self.assertEqual(doc['amount_paid'], 0.0)

    def test_payments_never_exceed_balance(self):
        ref = self._submit('0241000009')
        self._quote(ref, qty=10, price=25)  # total 250
        r1 = self.client.post('/api/bulk-orders/' + ref + '/payments', json={'amount': 200, 'reference': 'p1'}, headers=self.staff_h)
        self.assertEqual(r1.status_code, 200)
        # outstanding is now 50; another 200 is rejected
        r2 = self.client.post('/api/bulk-orders/' + ref + '/payments', json={'amount': 200, 'reference': 'p2'}, headers=self.staff_h)
        self.assertEqual(r2.status_code, 400)
        doc = app_module._find_bulk(ref)
        self.assertEqual(doc['amount_paid'], 200.0)
        self.assertEqual(doc['outstanding_balance'], 50.0)

    # ===== 4. Packaging integrity =====
    def test_cannot_be_ready_without_packaging(self):
        ref = self._submit('0241000010')
        self._quote(ref)
        tok = self._customer_token('0241000010')
        self.client.post('/api/bulk-orders/' + ref + '/accept', headers=self._h(tok))  # -> CONFIRMED (no deposit)
        self.client.post('/api/bulk-orders/' + ref + '/status', json={'status': 'PREPARING'}, headers=self.staff_h)
        self.client.post('/api/bulk-orders/' + ref + '/status', json={'status': 'PACKAGING'}, headers=self.staff_h)
        # packaged = 0 < agreed 10 -> ready must be rejected
        res = self.client.post('/api/bulk-orders/' + ref + '/status', json={'status': 'READY_FOR_PICKUP'}, headers=self.staff_h)
        self.assertEqual(res.status_code, 409)
        # pack all 10 -> ready allowed
        self.client.post('/api/bulk-orders/' + ref + '/progress',
                         json={'items': [{'meal_id': 'fried-rice', 'prepared': 10, 'packaged': 10}]}, headers=self.staff_h)
        res = self.client.post('/api/bulk-orders/' + ref + '/status', json={'status': 'READY_FOR_PICKUP'}, headers=self.staff_h)
        self.assertEqual(res.status_code, 200)

    def test_admin_override_ready(self):
        ref = self._submit('0241000011')
        self._quote(ref)
        tok = self._customer_token('0241000011')
        self.client.post('/api/bulk-orders/' + ref + '/accept', headers=self._h(tok))
        self.client.post('/api/bulk-orders/' + ref + '/status', json={'status': 'PREPARING'}, headers=self.admin_h)
        self.client.post('/api/bulk-orders/' + ref + '/status', json={'status': 'PACKAGING'}, headers=self.admin_h)
        # admin override (incomplete packaging) -> allowed
        res = self.client.post('/api/bulk-orders/' + ref + '/status',
                               json={'status': 'READY_FOR_PICKUP', 'override': True}, headers=self.admin_h)
        self.assertEqual(res.status_code, 200)

    # ===== 5. Cancellation preserves payment history =====
    def test_cancel_preserves_payments_and_records_refund(self):
        ref = self._submit('0241000012')
        self._quote(ref, qty=10, price=30)  # total 300
        self.client.post('/api/bulk-orders/' + ref + '/payments', json={'amount': 100, 'reference': 'dep-1'}, headers=self.staff_h)
        res = self.client.post('/api/bulk-orders/' + ref + '/cancel', json={'reason': 'Event cancelled'}, headers=self.admin_h)
        self.assertEqual(res.status_code, 200)
        doc = app_module._find_bulk(ref)
        self.assertEqual(len(doc['payments']), 1)          # history preserved
        self.assertEqual(doc['amount_paid'], 100.0)         # not zeroed
        self.assertEqual(doc['refund_obligation'], 100.0)   # refund obligation recorded
        self.assertIn('cancelled_at', doc)
        self.assertIn('cancelled_by_name', doc)

    # ===== 6. Handover / delivery / collection records =====
    def test_fulfillment_records_responsible_staff(self):
        ref = self._submit('0241000013')
        self._quote(ref)
        tok = self._customer_token('0241000013')
        self.client.post('/api/bulk-orders/' + ref + '/accept', headers=self._h(tok))
        self.client.post('/api/bulk-orders/' + ref + '/status', json={'status': 'PREPARING'}, headers=self.staff_h)
        self.client.post('/api/bulk-orders/' + ref + '/status', json={'status': 'PACKAGING'}, headers=self.staff_h)
        self.client.post('/api/bulk-orders/' + ref + '/progress',
                         json={'items': [{'meal_id': 'fried-rice', 'prepared': 10, 'packaged': 10}]}, headers=self.staff_h)
        self.client.post('/api/bulk-orders/' + ref + '/status', json={'status': 'READY_FOR_PICKUP'}, headers=self.staff_h)
        self.client.post('/api/bulk-orders/' + ref + '/status', json={'status': 'OUT_FOR_DELIVERY'}, headers=self.staff_h)
        self.client.post('/api/bulk-orders/' + ref + '/status', json={'status': 'DELIVERED'}, headers=self.staff_h)

        rec = app_module._find_bulk(ref)['fulfillment_record']
        self.assertIn('dispatched_at', rec)
        self.assertIn('dispatched_by_name', rec)
        self.assertIn('delivered_at', rec)
        self.assertIn('delivered_by_name', rec)


if __name__ == '__main__':
    unittest.main(verbosity=2)
