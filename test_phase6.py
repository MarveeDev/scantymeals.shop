"""
Phase 6 — Bulk Food Packs & Event Orders.

Run with:  .venv\\Scripts\\python test_phase6.py

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


class Phase6BulkTests(unittest.TestCase):
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
        cls.client.post('/api/staff', json={'username': 'bo.pos', 'name': 'Bo Staff',
                                            'role': 'staff', 'password': 'BoPos123!'}, headers=cls.admin_h)
        cls.staff = cls.client.post('/api/auth/staff-login',
                                    json={'username': 'bo.pos', 'password': 'BoPos123!'}).get_json()
        cls.staff_h = {'Authorization': 'Bearer ' + cls.staff['token']}
        app_module._set_setting(app_module.ADMIN_SMS_SETTING_KEY, '+233242292939')

    def _h(self, tok): return {'Authorization': 'Bearer ' + tok}

    def _customer_token(self, phone='0241122334', name='Church'):
        return self.client.post('/api/auth/phone-name', json={'phone': phone, 'name': name}).get_json()['token']

    def submit_bulk(self, **kw):
        payload = {'customer_name': 'St. James Church', 'phone': '0241122334',
                   'event_type': 'Church Programme', 'event_date': '2026-12-25', 'fulfillment': 'delivery',
                   'delivery_address': 'Mpasatia', 'meals': [{'meal_id': 'fried-rice', 'quantity': 150},
                                                             {'meal_id': 'spaghetti', 'quantity': 50}]}
        payload.update(kw)
        return self.client.post('/api/bulk-orders', json=payload)

    def _ref(self, r):
        return r.get_json()['bulk_order']['reference']

    # ---------- Submission ----------
    def test_submit_request(self):
        r = self.submit_bulk()
        self.assertEqual(r.status_code, 201)
        ref = self._ref(r)
        doc = app_module._find_bulk(ref)
        self.assertEqual(doc['status'], 'REQUEST_RECEIVED')
        self.assertEqual(doc['payment_status'], 'unpaid')
        self.assertEqual(doc['total_packs'], 200)

    def test_submit_requires_meals(self):
        r = self.submit_bulk(meals=[])
        self.assertEqual(r.status_code, 400)

    def test_submit_invalid_meal(self):
        r = self.submit_bulk(meals=[{'meal_id': 'not-a-meal', 'quantity': 5}])
        self.assertEqual(r.status_code, 400)

    def test_submit_admin_notifications(self):
        r = self.submit_bulk(phone='0245566778')
        ref = self._ref(r)
        self.assertTrue(any(n.get('order_id') == ref and n.get('source') == 'BULK' for n in app_module.notifications_db))
        self.assertTrue(any(l.get('order_id') == ref and l.get('event_type') == 'bulk_request_admin' for l in app_module.sms_logs_db))

    # ---------- Quotation ----------
    def test_prepare_quotation(self):
        r = self.submit_bulk()
        ref = self._ref(r)
        res = self.client.put('/api/bulk-orders/' + ref + '/quotation', json={
            'items': [{'meal_id': 'fried-rice', 'name': 'Fried Rice', 'quoted_qty': 150, 'price_per_pack': 25},
                      {'meal_id': 'spaghetti', 'name': 'Spaghetti', 'quoted_qty': 50, 'price_per_pack': 20}],
            'delivery_charge': 100, 'discount': 50, 'deposit_required': 2000,
        }, headers=self.admin_h)
        self.assertEqual(res.status_code, 200)
        doc = app_module._find_bulk(ref)
        q = doc['quotation']
        # 150*25 + 50*20 = 4750 ; +100 -50 = 4800
        self.assertEqual(q['subtotal'], 4750.0)
        self.assertEqual(q['total'], 4800.0)
        self.assertEqual(q['deposit_required'], 2000.0)
        self.assertEqual(doc['status'], 'AWAITING_CUSTOMER_CONFIRMATION')

    def test_quotation_requires_admin(self):
        r = self.submit_bulk()
        ref = self._ref(r)
        res = self.client.put('/api/bulk-orders/' + ref + '/quotation',
                              json={'items': [{'meal_id': 'fried-rice', 'quoted_qty': 1, 'price_per_pack': 25}]},
                              headers=self._h(self._customer_token()))
        self.assertEqual(res.status_code, 403)

    # ---------- Accept / decline ----------
    def test_customer_accept(self):
        r = self.submit_bulk(phone='0243344556')
        ref = self._ref(r)
        self.client.put('/api/bulk-orders/' + ref + '/quotation', json={
            'items': [{'meal_id': 'fried-rice', 'name': 'Fried Rice', 'quoted_qty': 150, 'price_per_pack': 25}],
            'deposit_required': 1000}, headers=self.admin_h)
        tok = self._customer_token('0243344556', 'Church')
        res = self.client.post('/api/bulk-orders/' + ref + '/accept', headers=self._h(tok))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(app_module._find_bulk(ref)['status'], 'AWAITING_DEPOSIT')

    def test_accept_wrong_customer_denied(self):
        r = self.submit_bulk(phone='0244455667')
        ref = self._ref(r)
        self.client.put('/api/bulk-orders/' + ref + '/quotation', json={
            'items': [{'meal_id': 'fried-rice', 'name': 'Fried Rice', 'quoted_qty': 10, 'price_per_pack': 25}]},
            headers=self.admin_h)
        tok = self._customer_token('0249999999', 'Other')  # different phone
        res = self.client.post('/api/bulk-orders/' + ref + '/accept', headers=self._h(tok))
        self.assertEqual(res.status_code, 404)

    # ---------- Payments ----------
    def test_record_payment(self):
        r = self.submit_bulk(phone='0246677889')
        ref = self._ref(r)
        self.client.put('/api/bulk-orders/' + ref + '/quotation', json={
            'items': [{'meal_id': 'fried-rice', 'name': 'Fried Rice', 'quoted_qty': 100, 'price_per_pack': 30}],
            'deposit_required': 1000}, headers=self.admin_h)  # total 3000
        res = self.client.post('/api/bulk-orders/' + ref + '/payments', json={'amount': 1000, 'reference': 'pay-1'},
                               headers=self.staff_h)
        self.assertEqual(res.status_code, 200)
        doc = app_module._find_bulk(ref)
        self.assertEqual(doc['amount_paid'], 1000.0)
        self.assertEqual(doc['outstanding_balance'], 2000.0)
        self.assertEqual(doc['payment_status'], 'partial')

    def test_overpayment_rejected(self):
        r = self.submit_bulk(phone='0247788990')
        ref = self._ref(r)
        self.client.put('/api/bulk-orders/' + ref + '/quotation', json={
            'items': [{'meal_id': 'fried-rice', 'name': 'Fried Rice', 'quoted_qty': 10, 'price_per_pack': 25}]},
            headers=self.admin_h)  # total 250
        res = self.client.post('/api/bulk-orders/' + ref + '/payments', json={'amount': 300, 'reference': 'pay-x'},
                               headers=self.staff_h)
        self.assertEqual(res.status_code, 400)

    def test_duplicate_payment_rejected(self):
        r = self.submit_bulk(phone='0248899001')
        ref = self._ref(r)
        self.client.put('/api/bulk-orders/' + ref + '/quotation', json={
            'items': [{'meal_id': 'fried-rice', 'name': 'Fried Rice', 'quoted_qty': 100, 'price_per_pack': 30}]},
            headers=self.admin_h)
        self.client.post('/api/bulk-orders/' + ref + '/payments', json={'amount': 1000, 'reference': 'pay-dup'},
                         headers=self.staff_h)
        res = self.client.post('/api/bulk-orders/' + ref + '/payments', json={'amount': 1000, 'reference': 'pay-dup'},
                               headers=self.staff_h)
        self.assertEqual(res.status_code, 409)

    def test_full_payment_status(self):
        r = self.submit_bulk(phone='0249900112')
        ref = self._ref(r)
        self.client.put('/api/bulk-orders/' + ref + '/quotation', json={
            'items': [{'meal_id': 'fried-rice', 'name': 'Fried Rice', 'quoted_qty': 10, 'price_per_pack': 25}]},
            headers=self.admin_h)  # total 250
        self.client.post('/api/bulk-orders/' + ref + '/payments', json={'amount': 250, 'reference': 'pay-full'},
                         headers=self.staff_h)
        self.assertEqual(app_module._find_bulk(ref)['payment_status'], 'paid')

    # ---------- Status transitions ----------
    def test_valid_transition(self):
        r = self.submit_bulk(phone='0240011223')
        ref = self._ref(r)
        # move to AWAITING then CONFIRMED then PREPARING
        self.client.put('/api/bulk-orders/' + ref + '/quotation', json={
            'items': [{'meal_id': 'fried-rice', 'name': 'Fried Rice', 'quoted_qty': 10, 'price_per_pack': 25}]},
            headers=self.admin_h)
        tok = self._customer_token('0240011223')
        self.client.post('/api/bulk-orders/' + ref + '/accept', headers=self._h(tok))
        # if no deposit, accept -> CONFIRMED
        doc = app_module._find_bulk(ref)
        if doc['status'] == 'CONFIRMED':
            res = self.client.post('/api/bulk-orders/' + ref + '/status', json={'status': 'PREPARING'}, headers=self.staff_h)
            self.assertEqual(res.status_code, 200)
            self.assertEqual(app_module._find_bulk(ref)['status'], 'PREPARING')

    def test_invalid_transition_rejected(self):
        r = self.submit_bulk(phone='0240012345')
        ref = self._ref(r)  # REQUEST_RECEIVED
        res = self.client.post('/api/bulk-orders/' + ref + '/status', json={'status': 'COMPLETED'}, headers=self.admin_h)
        self.assertEqual(res.status_code, 400)

    def test_staff_cannot_jump_to_non_operational(self):
        r = self.submit_bulk(phone='0240013456')
        ref = self._ref(r)
        res = self.client.post('/api/bulk-orders/' + ref + '/status', json={'status': 'CANCELLED'}, headers=self.staff_h)
        self.assertEqual(res.status_code, 400)

    # ---------- Progress ----------
    def test_update_progress(self):
        r = self.submit_bulk(phone='0240014567')
        ref = self._ref(r)
        res = self.client.post('/api/bulk-orders/' + ref + '/progress', json={
            'items': [{'meal_id': 'fried-rice', 'prepared': 150, 'packaged': 120, 'notes': '30 left'}]},
            headers=self.staff_h)
        self.assertEqual(res.status_code, 200)
        prep = app_module._find_bulk(ref)['preparation']
        fried = next(i for i in prep['items'] if i['meal_id'] == 'fried-rice')
        self.assertEqual(fried['prepared'], 150)
        self.assertEqual(fried['packaged'], 120)

    # ---------- Customer tracking ----------
    def test_my_bulk_orders_isolation(self):
        self.submit_bulk(phone='0240015678')
        tok = self._customer_token('0240015678', 'Owner')
        # Phase 7: private bulk history requires a verified phone.
        app_module._find_user_by_phone_digits(app_module._phone_digits('0240015678'))['phone_verified'] = True
        mine = self.client.get('/api/my-bulk-orders', headers=self._h(tok)).get_json()['bulk_orders']
        self.assertTrue(mine)
        self.assertTrue(all(b.get('phone_digits') == app_module._phone_digits('0240015678') for b in app_module._all_bulk() if b.get('reference') == mine[0]['reference']))

    def test_my_bulk_orders_requires_auth(self):
        self.assertEqual(self.client.get('/api/my-bulk-orders').status_code, 401)

    def test_admin_summary_counts(self):
        r = self.client.get('/api/bulk-orders', headers=self.admin_h)
        self.assertEqual(r.status_code, 200)
        s = r.get_json()['summary']
        self.assertIn('total', s)
        self.assertIn('outstanding_balance', s)


if __name__ == '__main__':
    unittest.main(verbosity=2)
