"""
Phase 1 — Authentication & Authorization tests for ScantyMeals V2.

Run with:  .venv\\Scripts\\python test_phase1.py

Runs against the Flask app in IN-MEMORY mode (no MongoDB writes). The
unreachable MONGODB_URI below forces the in-memory fallback so no production
data is touched.
"""

import os
import unittest

# Force in-memory mode BEFORE importing the app (no real MongoDB writes).
os.environ['MONGODB_URI'] = 'mongodb://127.0.0.1:59999/?serverSelectionTimeoutMS=1000'
os.environ.pop('RENDER', None)
os.environ.pop('FLASK_ENV', None)

# Provision a deterministic super_admin + admin in the in-memory store.
os.environ['SUPER_ADMIN_EMAIL'] = 'super@scanty.com'
os.environ['SUPER_ADMIN_PASSWORD'] = 'SuperSecret123!'
os.environ['ADMIN_EMAIL'] = 'admin@scanty.com'
os.environ['ADMIN_PASSWORD'] = 'admin123'

import app as app_module


class Phase1AuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert app_module.MONGODB_CONNECTED is False, \
            "Tests require in-memory mode to avoid touching a real database"
        cls.client = app_module.app.test_client()

    # ---- helpers ----
    def post_json(self, path, payload, headers=None):
        return self.client.post(path, json=payload, headers=headers or {})

    def get(self, path, token=None):
        headers = {'Authorization': 'Bearer ' + token} if token else {}
        return self.client.get(path, headers=headers)

    def admin_login(self):
        r = self.post_json('/api/auth/admin-login', {'email': 'admin@scanty.com', 'password': 'admin123'})
        assert r.status_code == 200, f"admin login failed: {r.status_code} {r.get_data(as_text=True)}"
        return r.get_json()

    def super_login(self):
        r = self.post_json('/api/auth/admin-login', {'email': 'super@scanty.com', 'password': 'SuperSecret123!'})
        assert r.status_code == 200, f"super login failed: {r.status_code} {r.get_data(as_text=True)}"
        return r.get_json()

    def create_staff(self, token, username, role='staff', password=None):
        payload = {'username': username, 'name': 'Test ' + username, 'role': role}
        if password:
            payload['password'] = password
        r = self.post_json('/api/staff', payload, {'Authorization': 'Bearer ' + token})
        return r

    # ---- tests ----
    def test_health_in_memory(self):
        r = self.client.get('/api/health')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()['mongodb'])

    def test_admin_login_preserved(self):
        data = self.admin_login()
        self.assertTrue(data['success'])
        self.assertEqual(data['user']['role'], 'admin')
        self.assertNotIn('password', data['user'])

    def test_admin_login_bad_password(self):
        r = self.post_json('/api/auth/admin-login', {'email': 'admin@scanty.com', 'password': 'wrong'})
        self.assertEqual(r.status_code, 401)

    def test_super_admin_login(self):
        data = self.super_login()
        self.assertEqual(data['user']['role'], 'super_admin')
        self.assertNotIn('password', data['user'])

    def test_staff_list_requires_auth(self):
        self.assertEqual(self.get('/api/staff').status_code, 401)

    def test_staff_list_admin(self):
        token = self.admin_login()['token']
        r = self.get('/api/staff', token)
        self.assertEqual(r.status_code, 200)
        self.assertIn('staff', r.get_json())

    def test_admin_create_staff_generates_password(self):
        token = self.admin_login()['token']
        r = self.create_staff(token, 'kwame.staff')
        self.assertEqual(r.status_code, 201)
        body = r.get_json()
        self.assertIn('password', body)  # generated once
        self.assertNotIn('password', body['staff'])  # never in the user object
        self.assertEqual(body['staff']['role'], 'staff')

    def test_admin_cannot_assign_admin_role(self):
        token = self.admin_login()['token']
        r = self.create_staff(token, 'nopriv.admin', role='admin')
        self.assertEqual(r.status_code, 403)

    def test_super_admin_can_assign_admin_role(self):
        token = self.super_login()['token']
        r = self.create_staff(token, 'sub.admin', role='admin', password='SubAdmin123!')
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()['staff']['role'], 'admin')

    def test_duplicate_username_conflict(self):
        token = self.admin_login()['token']
        self.create_staff(token, 'dup.user', password='DupUser123!')
        r = self.create_staff(token, 'dup.user', password='DupUser123!')
        self.assertEqual(r.status_code, 409)

    def test_staff_login(self):
        token = self.admin_login()['token']
        self.create_staff(token, 'ada.staff', password='AdaPass123!')
        r = self.post_json('/api/auth/staff-login', {'username': 'ada.staff', 'password': 'AdaPass123!'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['user']['role'], 'staff')

    def test_staff_login_wrong_password(self):
        r = self.post_json('/api/auth/staff-login', {'username': 'ada.staff', 'password': 'wrong'})
        self.assertEqual(r.status_code, 401)

    def test_staff_cannot_access_admin_route(self):
        token = self.admin_login()['token']
        self.create_staff(token, 'kofi.staff', password='KofiPass123!')
        staff = self.post_json('/api/auth/staff-login', {'username': 'kofi.staff', 'password': 'KofiPass123!'}).get_json()
        r = self.get('/api/orders', staff['token'])
        self.assertEqual(r.status_code, 403)

    def test_staff_can_access_profile(self):
        token = self.admin_login()['token']
        self.create_staff(token, 'ama.staff', password='AmaPass123!')
        staff = self.post_json('/api/auth/staff-login', {'username': 'ama.staff', 'password': 'AmaPass123!'}).get_json()
        r = self.get('/api/auth/profile', staff['token'])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['user']['role'], 'staff')

    def test_super_admin_can_access_admin_route(self):
        token = self.super_login()['token']
        r = self.get('/api/orders', token)
        self.assertEqual(r.status_code, 200)

    def test_suspend_and_reactivate(self):
        token = self.admin_login()['token']
        r = self.create_staff(token, 'yaw.staff', password='YawPass123!')
        uid = r.get_json()['staff']['id']

        # suspend
        r = self.client.patch('/api/staff/' + uid + '/status',
                              json={'status': 'suspended'},
                              headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['staff']['status'], 'suspended')

        # suspended login blocked
        r = self.post_json('/api/auth/staff-login', {'username': 'yaw.staff', 'password': 'YawPass123!'})
        self.assertEqual(r.status_code, 403)

        # reactivate
        r = self.client.patch('/api/staff/' + uid + '/status',
                              json={'status': 'active'},
                              headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(r.status_code, 200)

        # login works again
        r = self.post_json('/api/auth/staff-login', {'username': 'yaw.staff', 'password': 'YawPass123!'})
        self.assertEqual(r.status_code, 200)

    def test_reset_password(self):
        token = self.admin_login()['token']
        r = self.create_staff(token, 'akos.staff', password='OldPass123!')
        uid = r.get_json()['staff']['id']

        r = self.client.post('/api/staff/' + uid + '/reset-password',
                             json={'password': 'NewPass123!'},
                             headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(r.status_code, 200)

        self.assertEqual(self.post_json('/api/auth/staff-login',
                                        {'username': 'akos.staff', 'password': 'OldPass123!'}).status_code, 401)
        self.assertEqual(self.post_json('/api/auth/staff-login',
                                        {'username': 'akos.staff', 'password': 'NewPass123!'}).status_code, 200)

    def test_no_password_leaked_in_staff_list(self):
        token = self.admin_login()['token']
        r = self.get('/api/staff', token)
        for s in r.get_json()['staff']:
            self.assertNotIn('password', s)

    def test_cannot_modify_super_admin_by_admin(self):
        admin_token = self.admin_login()['token']
        # find super admin id
        r = self.get('/api/staff', admin_token)
        super_id = next((s['id'] for s in r.get_json()['staff'] if s['role'] == 'super_admin'), None)
        self.assertIsNotNone(super_id)
        r = self.client.patch('/api/staff/' + super_id + '/status',
                              json={'status': 'suspended'},
                              headers={'Authorization': 'Bearer ' + admin_token})
        self.assertEqual(r.status_code, 403)


if __name__ == '__main__':
    unittest.main(verbosity=2)
