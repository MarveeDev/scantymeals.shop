import os
import json
import base64
import hmac
import hashlib
import secrets
import threading
from datetime import datetime, date, timedelta
from functools import wraps

from flask import Flask, request, jsonify, send_from_directory, redirect, make_response
from flask_cors import CORS
from pymongo import MongoClient, ReturnDocument
from bson.objectid import ObjectId
import jwt
import bcrypt
from flask_socketio import SocketIO, emit, join_room, leave_room
import firebase_admin
from firebase_admin import credentials, messaging

from sms_provider import get_sms_provider


# ============================================================================
# CONFIG
# ============================================================================

app = Flask(__name__, static_folder='.')

# CORS — restrict to known origins in production
_origins_env = os.getenv(
     'ADMIN_ORIGINS',
    'https://scantymeals.shop,https://www.scantymeals.shop,https://scanty-meals-95d3f.web.app,http://localhost:5000,http://127.0.0.1:5000,https://scanty-meals-95d3f.firebaseapp.com,'
)
ALLOWED_ORIGINS = [o.strip() for o in _origins_env.split(',') if o.strip()]
CORS(
    app,
    resources={r"/api/*": {"origins": ALLOWED_ORIGINS}},
    supports_credentials=False,
    expose_headers=["Content-Type", "Authorization"],
)

# Setup SocketIO
socketio = SocketIO(app, cors_allowed_origins=ALLOWED_ORIGINS)

# Setup Firebase Admin
try:
    if os.path.exists("firebase-adminsdk.json"):
        cred = credentials.Certificate("firebase-adminsdk.json")
        firebase_admin.initialize_app(cred)
        FIREBASE_ADMIN_READY = True
    elif os.getenv("FIREBASE_CREDENTIALS"):
        cred_dict = json.loads(base64.b64decode(os.getenv("FIREBASE_CREDENTIALS")).decode('utf-8'))
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        FIREBASE_ADMIN_READY = True
    else:
        FIREBASE_ADMIN_READY = False
        print("Warning: Firebase Admin SDK credentials not found. Push notifications disabled.")
except Exception as e:
    print(f"Warning: Firebase Admin SDK init failed: {e}")
    FIREBASE_ADMIN_READY = False

@app.before_request
def log_request():
    app.logger.info(
        "REQ %s %s | Origin: %s | UA: %s",
        request.method,
        request.path,
        request.headers.get('Origin', 'none'),
        request.headers.get('User-Agent', '')[:80],
    )

# JWT — FAIL FAST in production if secret is missing/default
JWT_SECRET = os.getenv('JWT_SECRET')
JWT_ALGORITHM = 'HS256'
if not JWT_SECRET or JWT_SECRET == 'your-secret-key-change-in-production':
    if os.getenv('FLASK_ENV') == 'production' or os.getenv('RENDER'):
        raise RuntimeError(
            "JWT_SECRET env var must be set to a strong random value in production"
        )
    # Dev-only fallback
    JWT_SECRET = 'dev-only-secret-do-not-use-in-prod'
    print("WARNING: using dev JWT_SECRET. Set JWT_SECRET env var for production.")

# OTP hashing secret — INDEPENDENT of JWT_SECRET. FAIL FAST in production.
OTP_SECRET = os.getenv('OTP_SECRET')
if not OTP_SECRET:
    if os.getenv('FLASK_ENV') == 'production' or os.getenv('RENDER'):
        raise RuntimeError(
            "OTP_SECRET env var must be set to a strong random value in production"
        )
    OTP_SECRET = 'dev-only-otp-secret-do-not-use-in-prod'
    print("WARNING: using dev OTP_SECRET. Set OTP_SECRET env var for production.")


def _check_sms_provider_config():
    is_prod = bool(os.getenv('RENDER') or os.getenv('FLASK_ENV') == 'production')
    provider = (os.getenv('SMS_PROVIDER') or 'mock').strip().lower()
    if is_prod and provider != 'gonline':
        raise RuntimeError(
            f"SMS_PROVIDER must be 'gonline' in production (got '{provider or 'unset'}'). "
            "The mock provider must not be used as production SMS."
        )
    if provider == 'gonline' and not os.getenv('GONLINE_API_KEY'):
        if is_prod:
            raise RuntimeError(
                "GONLINE_API_KEY must be set when SMS_PROVIDER=gonline in production."
            )
        print("Warning: SMS_PROVIDER=gonline but GONLINE_API_KEY is not set.")


_check_sms_provider_config()

# MongoDB
MONGO_URI = os.getenv('MONGODB_URI', 'mongodb://localhost:27017/')
try:
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    mongo_client.admin.command('ping')
    db = mongo_client['scantymeals']
    orders_collection = db['orders']
    order_counter_collection = db['counters']
    users_collection = db['users']
    meals_collection = db['meals']
    notifications_collection = db['notifications']
    fcm_tokens_collection = db['fcm_tokens']
    audit_logs_collection = db['audit_logs']
    sms_logs_collection = db['sms_logs']
    sms_outbox_collection = db['sms_outbox']
    settings_collection = db['settings']
    bulk_orders_collection = db['bulk_orders']
    otp_codes_collection = db['otp_codes']
    rate_limits_collection = db['rate_limits']
    # Helpful indexes
    try:
        orders_collection.create_index('id', unique=True)
        orders_collection.create_index('date')
        orders_collection.create_index('idempotency_key', unique=True, sparse=True)
        users_collection.create_index('email', unique=True, sparse=True)
        users_collection.create_index('phone', sparse=True)
        meals_collection.create_index('key', unique=True)
        notifications_collection.create_index('audience')
        notifications_collection.create_index('created_at')
        notifications_collection.create_index('dedupe_key', unique=True, sparse=True)
        fcm_tokens_collection.create_index('audience', unique=True)
        sms_logs_collection.create_index('dedupe_key', unique=True, sparse=True)
        settings_collection.create_index('key', unique=True)
        sms_outbox_collection.create_index('status')
        sms_outbox_collection.create_index('dedupe_key', unique=True, sparse=True)
        bulk_orders_collection.create_index('reference', unique=True)
        bulk_orders_collection.create_index('status')
        bulk_orders_collection.create_index('event_date')
        bulk_orders_collection.create_index('phone_digits')
        bulk_orders_collection.create_index('created_at')
        otp_codes_collection.create_index('phone_digits')
        otp_codes_collection.create_index('expires_at')
        rate_limits_collection.create_index('key', unique=True)
    except Exception as ie:
        print(f"Index init warning: {ie}")
    MONGODB_CONNECTED = True
    print("MongoDB connected.")
except Exception as e:
    print(f"Warning: MongoDB connection failed ({e}). Using in-memory storage.")
    MONGODB_CONNECTED = False
    orders_db = []
    order_counter = [1]
    users_db = []  # populated below once helpers exist
    meals_db = []
    notifications_db = []
    fcm_tokens_db = {} # audience -> token mapping
    audit_logs_db = []
    sms_logs_db = []
    sms_outbox_db = []
    settings_db = {}
    bulk_orders_db = []
    bulk_counter = [1]
    otp_codes_db = []
    rate_limits_db = {}




# ============================================================================
# PASSWORD HELPERS
# ============================================================================

def hash_password(password: str) -> str:
    hashed_bytes = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
    return base64.b64encode(hashed_bytes).decode('ascii')


def verify_password(password: str, hash_val: str) -> bool:
    if not hash_val:
        return False
    # New base64 format
    try:
        hashed_bytes = base64.b64decode(hash_val.encode('ascii'))
        if bcrypt.checkpw(password.encode('utf-8'), hashed_bytes):
            return True
    except Exception:
        pass
    # Legacy utf-8 format
    try:
        return bcrypt.checkpw(password.encode('utf-8'), hash_val.encode('utf-8'))
    except Exception:
        return False


# Now that hash_password exists, populate in-memory demo users
if not MONGODB_CONNECTED:
    users_db = [
        {"_id": "admin_mem", "email": "admin@scanty.com",
         "password": hash_password("admin123"),
         "name": "Admin User", "role": "admin"},
        {"_id": "cust_mem", "email": "customer@scanty.com",
         "password": hash_password("customer123"),
         "name": "Demo Customer", "role": "customer"},
    ]


def ensure_admin_user():
    """
    Ensure an admin user exists in MongoDB so /api/auth/admin-login can work
    on production deployments.

    Credentials come from env:
      - ADMIN_EMAIL
      - ADMIN_PASSWORD
    Optional:
      - ADMIN_ROLE (defaults to 'admin')
    """
    admin_email = (os.getenv('ADMIN_EMAIL') or 'admin@scanty.com').strip().lower()
    admin_password = os.getenv('ADMIN_PASSWORD')
    if not admin_password:
        if os.getenv('RENDER') or os.getenv('FLASK_ENV') == 'production':
            print("Warning: ADMIN_PASSWORD not set; skipping default admin provisioning in production.")
            return
        admin_password = 'admin123'  # dev-only fallback (never used in production)
    admin_role = (os.getenv('ADMIN_ROLE') or 'admin').strip().lower() or 'admin'

    if admin_role != 'admin':
        # Enforce role gate semantics used by @admin_required
        admin_role = 'admin'

    # In-memory mode: just ensure demo admin exists
    if not MONGODB_CONNECTED:
        global users_db
        if any(u.get('role') == 'admin' and (u.get('email') or '').lower() == admin_email for u in users_db):
            return
        users_db.insert(0, {
            "_id": "admin_mem",
            "email": admin_email,
            "password": hash_password(admin_password),
            "name": "Admin User",
            "role": "admin",
        })
        return

    # Mongo mode
    existing = users_collection.find_one({"email": admin_email, "role": "admin"})
    if existing:
        return

    # If there is any admin (or super admin) at all, do not overwrite; otherwise create a default admin.
    any_admin = users_collection.find_one({"role": {"$in": ["admin", "super_admin"]}})
    if any_admin:
        return

    new_admin = {
        "email": admin_email,
        "password": hash_password(admin_password),
        "name": "Admin User",
        "role": "admin",
        "created_at": datetime.now().isoformat(),
    }
    users_collection.insert_one(new_admin)


def ensure_super_admin():
    """
    Provision a SUPER_ADMIN account from env without touching existing admins.

      - SUPER_ADMIN_EMAIL (falls back to ADMIN_EMAIL)
      - SUPER_ADMIN_PASSWORD (falls back to ADMIN_PASSWORD)

    If a super_admin already exists, nothing happens.
    If the email matches an existing user, they are promoted to super_admin
    (their password is reset only when a password is explicitly provided).
    """
    email = (os.getenv('SUPER_ADMIN_EMAIL') or os.getenv('ADMIN_EMAIL') or '').strip().lower()
    password = os.getenv('SUPER_ADMIN_PASSWORD') or os.getenv('ADMIN_PASSWORD')

    if not email:
        print("Warning: no SUPER_ADMIN_EMAIL set; skipping super_admin provisioning.")
        return

    if MONGODB_CONNECTED:
        existing_super = users_collection.find_one({"role": "super_admin"})
        if existing_super:
            return
        user = users_collection.find_one({"email": email})
        if user:
            updates = {"role": "super_admin"}
            if password:
                updates["password"] = hash_password(password)
            users_collection.update_one({"_id": user["_id"]}, {"$set": updates})
            print(f"Promoted existing user {email} to super_admin.")
            return
        if not password:
            print("Warning: cannot create super_admin without SUPER_ADMIN_PASSWORD/ADMIN_PASSWORD.")
            return
        users_collection.insert_one({
            "email": email,
            "password": hash_password(password),
            "name": "Super Admin",
            "username": email.split("@")[0],
            "role": "super_admin",
            "status": "active",
            "created_at": datetime.now().isoformat(),
        })
        print(f"Provisioned super_admin {email}.")
        return

    # In-memory mode
    global users_db
    if any(u.get("role") == "super_admin" for u in users_db):
        return
    user = next((u for u in users_db if (u.get("email") or "").strip().lower() == email), None)
    if user:
        user["role"] = "super_admin"
        if password:
            user["password"] = hash_password(password)
        return
    if not password:
        print("Warning: cannot create super_admin without SUPER_ADMIN_PASSWORD/ADMIN_PASSWORD.")
        return
    users_db.insert(0, {
        "_id": "superadmin_mem",
        "email": email,
        "password": hash_password(password),
        "name": "Super Admin",
        "username": email.split("@")[0],
        "role": "super_admin",
        "status": "active",
        "created_at": datetime.now().isoformat(),
    })


# Ensure admin exists at startup (so admin-login can work immediately)
try:
    ensure_super_admin()
    ensure_admin_user()
except Exception as e:
    print(f"Warning: ensure_admin_user failed ({e})")


# ============================================================================
# RESPONSE HEADERS — only no-store on HTML (don't kill image caching)
# ============================================================================

@app.after_request
def add_header(response):
    ct = response.headers.get('Content-Type', '')
    if ct.startswith('text/html'):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response


# ============================================================================
# MEALS (database-driven menu — source of truth for prices)
# ============================================================================

# The approved ScantyMeals V2 menu. Seeded idempotently on startup; admins
# manage these (and may add more) via the admin menu endpoints.
DEFAULT_MEALS = [
    {"key": "fried-rice", "name": "Fried Rice",
     "description": "Golden wok-tossed rice with vegetables, egg and a savoury sauce.",
     "price": 40.00, "category": "Rice", "image": "/IMG/fried rice.jpg", "available": True},
    {"key": "spaghetti", "name": "Spaghetti",
     "description": "Classic spaghetti tossed in a rich, flavourful sauce.",
     "price": 40.00, "category": "Pasta", "image": "/IMG/menu_2.jpg", "available": True},
    {"key": "indomie", "name": "Indomie",
     "description": "Indomie noodles prepared Scanty-style.",
     "price": 30.00, "category": "Noodles", "image": "/IMG/EXT noddles.jpg", "available": True},
]


def _slugify(text):
    out = []
    for ch in (text or '').lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != '-':
            out.append('-')
    return ''.join(out).strip('-')


def seed_meals():
    """Idempotently seed the approved menu (no duplicates, no clobbering)."""
    now = datetime.now().isoformat()
    for meal in DEFAULT_MEALS:
        doc = dict(meal)
        doc.setdefault("created_at", now)
        if MONGODB_CONNECTED:
            # $setOnInsert: never overwrite admin edits on restart.
            meals_collection.update_one(
                {"key": doc["key"]},
                {"$setOnInsert": doc},
                upsert=True,
            )
        else:
            if not any(m.get("key") == doc["key"] for m in meals_db):
                entry = dict(doc)
                entry["_id"] = doc["key"]
                meals_db.append(entry)


def _meal_dict(meal):
    return {
        "id": str(meal.get("_id")),
        "key": meal.get("key"),
        "name": meal.get("name", ""),
        "description": meal.get("description", ""),
        "price": float(meal.get("price") or 0),
        "category": meal.get("category", ""),
        "image": meal.get("image", ""),
        "available": bool(meal.get("available", True)),
    }


def get_meals(available_only=False):
    if MONGODB_CONNECTED:
        query = {"available": True} if available_only else {}
        meals = list(meals_collection.find(query))
    else:
        meals = [m for m in meals_db if (not available_only or m.get("available", True))]
    meals.sort(key=lambda m: (m.get("name") or "").lower())
    return [_meal_dict(m) for m in meals]


def get_meal_by_id(meal_id):
    """Resolve a meal by its exposed id (ObjectId string in Mongo, key in-memory)."""
    if not meal_id:
        return None
    if MONGODB_CONNECTED:
        try:
            return meals_collection.find_one({"_id": ObjectId(meal_id)})
        except Exception:
            return meals_collection.find_one({"key": meal_id})
    for m in meals_db:
        if str(m.get("_id")) == str(meal_id) or m.get("key") == meal_id:
            return m
    return None


# Seed the approved menu (idempotent) so prices are always available.
try:
    seed_meals()
except Exception as e:
    print(f"Warning: seed_meals failed ({e})")


# ============================================================================
# ROLES & PERMISSIONS
# ============================================================================

VALID_ROLES = {'super_admin', 'admin', 'staff', 'customer'}
ROLE_LEVEL = {'super_admin': 3, 'admin': 2, 'staff': 1, 'customer': 0}

# Roles permitted on the existing administrative surface.
ADMIN_ROLES = ('admin', 'super_admin')
# Roles permitted on the staff/POS surface.
STAFF_ROLES = ('staff', 'admin', 'super_admin')


def _safe_user(user):
    """Return a user dict safe for API responses (never exposes the password)."""
    if not user:
        return None
    return {
        "id": str(user.get('_id')),
        "email": user.get('email', ''),
        "name": user.get('name', ''),
        "username": user.get('username'),
        "role": user.get('role', 'customer'),
        "status": user.get('status', 'active'),
        "phone_verified": bool(user.get('phone_verified')),
        "created_at": user.get('created_at'),
    }


def _find_user_by_id(uid):
    if not uid:
        return None
    if MONGODB_CONNECTED:
        try:
            return users_collection.find_one({"_id": ObjectId(uid)})
        except Exception:
            return users_collection.find_one({"_id": uid})
    return next((u for u in users_db if str(u.get('_id')) == uid), None)


def _find_user_by_firebase_uid(fb_uid):
    if not fb_uid:
        return None
    if MONGODB_CONNECTED:
        return users_collection.find_one({"firebase_uid": fb_uid})
    return next((u for u in users_db if u.get('firebase_uid') == fb_uid), None)


def _find_user_by_phone_digits(pd):
    """Find a customer by canonical phone digits (Ghana-normalized)."""
    if not pd:
        return None
    norm = "+233" + pd
    if MONGODB_CONNECTED:
        return users_collection.find_one({"phone": norm})
    for u in users_db:
        if _phone_digits(u.get('phone')) == pd or u.get('phone') == norm:
            return u
    return None


def _staff_role_allowed(requester_role, target_role):
    """Role-assignment rules: only super_admin may grant admin/super_admin."""
    if target_role == 'super_admin':
        return requester_role == 'super_admin'
    if target_role == 'admin':
        return requester_role == 'super_admin'
    if target_role == 'staff':
        return requester_role in ('admin', 'super_admin')
    return False


# ============================================================================
# AUTH HELPERS
# ============================================================================

def create_jwt_token(user_id, email, role, expires_in=24):
    payload = {
        'user_id': str(user_id),
        'email': email,
        'role': role,
        'exp': datetime.utcnow() + timedelta(hours=expires_in),
        'iat': datetime.utcnow(),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_jwt_token(token):
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def _read_token():
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[7:].strip() or None
    return None


def token_required(f):
    """Sets request.user (real user or anonymous). Never blocks."""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = _read_token()
        if token:
            payload = verify_jwt_token(token)
            if payload:
                request.user = payload
                return f(*args, **kwargs)
        request.user = {'user_id': 'anonymous', 'email': '', 'role': 'guest'}
        return f(*args, **kwargs)
    return decorated


def roles_required(*allowed_roles):
    """Hard gate: must be authenticated AND role in allowed_roles."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            token = _read_token()
            payload = verify_jwt_token(token) if token else None
            if not payload or payload.get('role') not in allowed_roles:
                resp = jsonify({"success": False, "message": "Access denied"})
                resp.status_code = 401 if not payload else 403
                resp.headers['X-Robots-Tag'] = 'noindex, nofollow'
                return resp
            request.user = payload
            return f(*args, **kwargs)
        return decorated
    return decorator


def admin_required(f):
    """Hard gate: must be authenticated AND role in (admin, super_admin)."""
    return roles_required(*ADMIN_ROLES)(f)


def super_admin_required(f):
    """Hard gate: must be authenticated AND role == super_admin."""
    return roles_required('super_admin')(f)


def staff_required(f):
    """Hard gate: staff, admin or super_admin."""
    return roles_required(*STAFF_ROLES)(f)


def normalize_phone(phone_raw):
    if not phone_raw:
        return None
    digits = ''.join(ch for ch in str(phone_raw) if ch.isdigit())
    if not digits:
        return None
    if len(digits) == 10 and digits.startswith('0'):
        digits = '233' + digits[1:]
    if len(digits) < 10:
        return None
    return '+' + digits


def _phone_digits(p):
    """Canonical phone digits (strips Ghana country code + leading zeros)."""
    d = ''.join(ch for ch in str(p or '') if ch.isdigit())
    d = d.lstrip('0')
    if d.startswith('233'):
        d = d[3:]
    return d


def get_or_create_user_by_phone(phone, name=''):
    display_name = (name or f"Customer {phone[-4:]}").strip()
    safe_digits = ''.join(ch for ch in phone if ch.isdigit())
    phone_email = f"phone_{safe_digits}@scanty.local"

    if MONGODB_CONNECTED:
        user = users_collection.find_one({"phone": phone})
        if user:
            if display_name and user.get("name") != display_name:
                users_collection.update_one({"_id": user["_id"]}, {"$set": {"name": display_name}})
                user["name"] = display_name
            return user
        new_user = {
            "email": phone_email,
            "password": hash_password(os.urandom(16).hex()),
            "name": display_name, "role": "customer",
            "phone": phone, "provider": "phone_otp",
            "created_at": datetime.now().isoformat(),
        }
        result = users_collection.insert_one(new_user)
        new_user["_id"] = result.inserted_id
        return new_user

    user = next((u for u in users_db if u.get("phone") == phone), None)
    if user:
        if display_name:
            user["name"] = display_name
        return user
    user = {
        "_id": f"usr_{len(users_db) + 1}",
        "email": phone_email,
        "password": hash_password(os.urandom(16).hex()),
        "name": display_name, "role": "customer",
        "phone": phone, "provider": "phone_otp",
        "created_at": datetime.now().isoformat(),
    }
    users_db.append(user)
    return user


# ============================================================================
# AUTH ROUTES
# ============================================================================

@app.route('/api/auth/register', methods=['POST'])
def register():
    return jsonify({"success": False, "message": "Registration is disabled"}), 403


@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    if not email or not password:
        return jsonify({"success": False, "message": "Email and password are required"}), 400

    if MONGODB_CONNECTED:
        user = users_collection.find_one({"email": email})
    else:
        user = next((u for u in users_db if u["email"] == email), None)

    if not user or not user.get('password') or not verify_password(password, user['password']):
        return jsonify({"success": False, "message": "Invalid credentials"}), 401

    token = create_jwt_token(user['_id'], user['email'], user['role'])
    return jsonify({
        "success": True, "token": token,
        "user": {"id": str(user['_id']), "email": user['email'],
                 "name": user['name'], "role": user['role']},
    }), 200


@app.route('/api/auth/admin-login', methods=['POST'])
def admin_login():
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''

    if not email or not password:
        return jsonify({"success": False, "message": "Email and password are required"}), 400

    if MONGODB_CONNECTED:
        user = users_collection.find_one({"email": email, "role": {"$in": list(ADMIN_ROLES)}})
    else:
        user = next((u for u in users_db if (u.get('email') or '').strip().lower() == email and u.get('role') in ADMIN_ROLES), None)

    if not user or not user.get('password') or not verify_password(password, user['password']):
        return jsonify({"success": False, "message": "Invalid admin credentials"}), 401

    if user.get('status') == 'suspended':
        return jsonify({"success": False, "message": "Account suspended. Contact your administrator."}), 403

    token = create_jwt_token(user['_id'], user.get('email', email), user.get('role', 'admin'))
    return jsonify({
        "success": True,
        "token": token,
        "user": _safe_user(user)
    }), 200


@app.route('/api/auth/staff-login', methods=['POST'])
def staff_login():
    """Staff, admin and super_admin login using username + password."""
    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    if not username or not password:
        return jsonify({"success": False, "message": "Username and password are required"}), 400

    if MONGODB_CONNECTED:
        user = users_collection.find_one({"username": username})
    else:
        user = next((u for u in users_db if u.get('username') == username), None)

    if not user or user.get('role') not in STAFF_ROLES:
        return jsonify({"success": False, "message": "Invalid credentials"}), 401

    if user.get('status') == 'suspended':
        return jsonify({"success": False, "message": "Account suspended. Contact your administrator."}), 403

    if not user.get('password') or not verify_password(password, user['password']):
        return jsonify({"success": False, "message": "Invalid credentials"}), 401

    token = create_jwt_token(user['_id'], user.get('email', username), user.get('role', 'staff'))
    return jsonify({
        "success": True,
        "token": token,
        "user": _safe_user(user)
    }), 200


@app.route('/api/auth/google', methods=['POST'])
def google_login():
    """Temporarily disabled.

    The previous implementation trusted a client-supplied email/firebase_uid,
    which is insecure. It will be re-enabled only after proper server-side
    Google/Firebase ID-token verification is implemented.
    """
    return jsonify({"success": False, "message": "Google login is temporarily disabled"}), 403


@app.route('/api/auth/phone-name', methods=['POST'])
def phone_name_login():
    data = request.get_json(silent=True) or {}
    phone = normalize_phone(data.get('phone'))
    name = (data.get('name') or '').strip()
    if not phone:
        return jsonify({"success": False, "message": "Phone is required"}), 400

    user = get_or_create_user_by_phone(phone, name)
    if MONGODB_CONNECTED:
        users_collection.update_one({"_id": user["_id"]}, {"$set": {"provider": "phone_name"}})

    token = create_jwt_token(user['_id'], user.get('email', ''), user.get('role', 'customer'))
    return jsonify({
        "success": True, "token": token,
        "user": {"id": str(user['_id']), "email": user.get('email', ''),
                 "name": user.get('name', f"Customer {phone[-4:]}"),
                 "role": user.get('role', 'customer'), "phone": phone},
    }), 200


@app.route('/api/auth/firebase-phone', methods=['POST'])
def firebase_phone_login():
    data = request.get_json(silent=True) or {}
    phone = normalize_phone(data.get('phone'))
    firebase_uid = (data.get('firebase_uid') or '').strip()
    name = (data.get('name') or '').strip()
    if not phone or not firebase_uid:
        return jsonify({"success": False, "message": "Phone and firebase_uid are required"}), 400

    user = get_or_create_user_by_phone(phone, name)
    if MONGODB_CONNECTED:
        users_collection.update_one({"_id": user["_id"]},
                                    {"$set": {"firebase_uid": firebase_uid, "provider": "firebase_phone"}})
    user["firebase_uid"] = firebase_uid
    user["provider"] = "firebase_phone"

    token = create_jwt_token(user['_id'], user.get('email', ''), user.get('role', 'customer'))
    return jsonify({
        "success": True, "token": token,
        "user": {"id": str(user['_id']), "email": user.get('email', ''),
                 "name": user.get('name', f"Customer {phone[-4:]}"),
                 "role": user.get('role', 'customer'), "phone": phone},
    }), 200


def _client_ip():
    """Return the client IP, honoring forwarded headers only behind a trusted proxy."""
    if os.getenv('TRUSTED_PROXY') == '1':
        fwd = request.headers.get('X-Forwarded-For')
        if fwd:
            return fwd.split(',')[-1].strip()
    return request.remote_addr or 'unknown'


@app.route('/api/auth/otp/request', methods=['POST'])
def request_otp():
    """Request a Gonline-delivered 6-digit OTP for customer phone verification."""
    data = request.get_json(silent=True) or {}
    raw_phone = (data.get('phone') or data.get('phone_number') or '').strip()
    phone = normalize_phone(raw_phone)
    if not phone:
        return jsonify({"success": False, "message": "Invalid phone number"}), 400
    pd = _phone_digits(phone)

    ip = _client_ip()
    if not _rate_limit_ok(f"otp:ip:{ip}", OTP_RATE_LIMIT_PER_IP):
        return jsonify({"success": False, "message": "Too many requests. Please try again later."}), 429
    if not _rate_limit_ok(f"otp:phone:{pd}", OTP_RATE_LIMIT_PER_PHONE):
        return jsonify({"success": False, "message": "Too many requests. Please try again later."}), 429

    latest = _latest_otp(pd)
    if latest and latest.get("issued_ts"):
        elapsed = datetime.utcnow().timestamp() - float(latest["issued_ts"])
        if elapsed < OTP_RESEND_COOLDOWN_SECONDS:
            wait = int(OTP_RESEND_COOLDOWN_SECONDS - elapsed)
            return jsonify({"success": False,
                            "message": f"Please wait {wait} seconds before requesting another code."}), 429

    otp = f"{secrets.randbelow(1000000):06d}"
    salt = secrets.token_hex(8)
    otp_hash = _hash_otp(otp, salt)
    now = datetime.utcnow()

    _invalidate_phone_otps(pd)

    doc = {
        "phone_digits": pd,
        "phone": phone,
        "otp_hash": otp_hash,
        "salt": salt,
        "purpose": "auth",
        "expires_at": (now + timedelta(minutes=OTP_EXPIRY_MINUTES)).isoformat(),
        "expires_ts": (now + timedelta(minutes=OTP_EXPIRY_MINUTES)).timestamp(),
        "issued_at": now.isoformat(),
        "issued_ts": now.timestamp(),
        "attempts": 0,
        "verified": False,
        "superseded": False,
    }
    if MONGODB_CONNECTED:
        otp_codes_collection.insert_one(doc)
    else:
        otp_codes_db.append(doc)

    message = f"Your ScantyMeals verification code is {otp}. It expires in 5 minutes."
    sent_ok, send_reason = _send_otp_sms(phone, message)
    if not sent_ok:
        # Safe internal diagnostics only — never the OTP, API key, or full URL.
        app.logger.warning("OTP send failed: %s", send_reason)

    return jsonify({"success": True,
                    "message": "If this number is active, a verification code has been sent."}), 200


@app.route('/api/auth/otp/verify', methods=['POST'])
def verify_otp():
    """Verify a customer OTP and issue the application JWT."""
    data = request.get_json(silent=True) or {}
    raw_phone = (data.get('phone') or '').strip()
    code = (data.get('code') or data.get('otp') or '').strip()
    phone = normalize_phone(raw_phone)
    if not phone or not code:
        return jsonify({"success": False, "message": "Phone and code are required"}), 400
    pd = _phone_digits(phone)

    otp_doc = _active_otp(pd)
    if not otp_doc:
        return jsonify({"success": False, "message": "No active code found. Please request a new one."}), 400

    now = datetime.utcnow()
    if otp_doc.get("expires_ts") and now.timestamp() > float(otp_doc["expires_ts"]):
        return jsonify({"success": False, "message": "This code has expired. Please request a new one."}), 400
    if int(otp_doc.get("attempts") or 0) >= OTP_MAX_ATTEMPTS:
        return jsonify({"success": False, "message": "Too many incorrect attempts. Please request a new code."}), 400

    expected = _hash_otp(code, otp_doc.get("salt", ""))
    if not hmac.compare_digest(expected, otp_doc.get("otp_hash", "")):
        _increment_otp_attempts(otp_doc)
        remaining = OTP_MAX_ATTEMPTS - int(otp_doc.get("attempts") or 0)
        return jsonify({"success": False, "message": f"Incorrect code. {remaining} attempts remaining."}), 400

    if not _mark_otp_verified(otp_doc):
        return jsonify({"success": False, "message": "This code has already been used."}), 400

    user = _link_customer_by_phone(pd, phone)
    token = create_jwt_token(user['_id'], user.get('email', ''), user.get('role', 'customer'))
    return jsonify({"success": True, "token": token, "user": _safe_user(user)}), 200


# ============================================================================
# PHONE-NUMBER REGISTRATION / LOGIN / RECOVERY
# ============================================================================

MIN_PASSWORD_LENGTH = 6


def _request_otp_internal(phone, purpose, extra=None):
    """Generate + send an OTP for a purpose. Returns (ok, message)."""
    pd = _phone_digits(phone)
    ip = _client_ip()
    if not _rate_limit_ok(f"otp:ip:{ip}", OTP_RATE_LIMIT_PER_IP):
        return False, "Too many requests. Please try again later."
    if not _rate_limit_ok(f"otp:phone:{pd}", OTP_RATE_LIMIT_PER_PHONE):
        return False, "Too many requests. Please try again later."
    latest = _latest_otp(pd)
    if latest and latest.get("issued_ts"):
        elapsed = datetime.utcnow().timestamp() - float(latest["issued_ts"])
        if elapsed < OTP_RESEND_COOLDOWN_SECONDS:
            wait = int(OTP_RESEND_COOLDOWN_SECONDS - elapsed)
            return False, f"Please wait {wait} seconds before requesting another code."

    otp = f"{secrets.randbelow(1000000):06d}"
    salt = secrets.token_hex(8)
    otp_hash = _hash_otp(otp, salt)
    now = datetime.utcnow()
    _invalidate_phone_otps(pd)
    doc = {
        "phone_digits": pd, "phone": phone, "otp_hash": otp_hash, "salt": salt, "purpose": purpose,
        "expires_at": (now + timedelta(minutes=OTP_EXPIRY_MINUTES)).isoformat(),
        "expires_ts": (now + timedelta(minutes=OTP_EXPIRY_MINUTES)).timestamp(),
        "issued_at": now.isoformat(), "issued_ts": now.timestamp(),
        "attempts": 0, "verified": False, "superseded": False,
    }
    if extra:
        doc.update(extra)
    if MONGODB_CONNECTED:
        otp_codes_collection.insert_one(doc)
    else:
        otp_codes_db.append(doc)

    message = f"Your ScantyMeals verification code is {otp}. It expires in 5 minutes."
    sent_ok, send_reason = _send_otp_sms(phone, message)
    if not sent_ok:
        app.logger.warning("OTP send failed: %s", send_reason)
    return True, "If this number is active, a verification code has been sent."


def _verify_otp_code(phone, code):
    """Validate + single-use-consume an OTP. Returns (ok, message_or_doc, otp_doc)."""
    pd = _phone_digits(phone)
    otp_doc = _active_otp(pd)
    if not otp_doc:
        return False, "No active code found. Please request a new one.", None
    now = datetime.utcnow()
    if otp_doc.get("expires_ts") and now.timestamp() > float(otp_doc["expires_ts"]):
        return False, "This code has expired. Please request a new one.", None
    if int(otp_doc.get("attempts") or 0) >= OTP_MAX_ATTEMPTS:
        return False, "Too many incorrect attempts. Please request a new code.", None
    expected = _hash_otp(code, otp_doc.get("salt", ""))
    if not hmac.compare_digest(expected, otp_doc.get("otp_hash", "")):
        _increment_otp_attempts(otp_doc)
        remaining = OTP_MAX_ATTEMPTS - int(otp_doc.get("attempts") or 0)
        return False, f"Incorrect code. {remaining} attempts remaining.", None
    if not _mark_otp_verified(otp_doc):
        return False, "This code has already been used.", None
    return True, otp_doc, otp_doc


def _register_customer_by_phone(pd, phone, password_hash):
    now = datetime.now().isoformat()
    user = _find_user_by_phone_digits(pd)
    if user:
        updates = {"phone_verified": True, "verified_at": now, "provider": "phone_password",
                   "phone": phone, "password": password_hash}
        if MONGODB_CONNECTED:
            users_collection.update_one({"_id": user["_id"]}, {"$set": updates})
        else:
            user.update(updates)
        return _find_user_by_phone_digits(pd) or user
    new_user = {
        "email": f"phone_{pd}@scanty.local",
        "password": password_hash,
        "name": f"Customer {pd[-4:]}",
        "role": "customer", "phone": phone,
        "provider": "phone_password",
        "phone_verified": True, "verified_at": now, "created_at": now,
    }
    if MONGODB_CONNECTED:
        r = users_collection.insert_one(new_user)
        new_user["_id"] = r.inserted_id
    else:
        new_user["_id"] = f"usr_{len(users_db) + 1}"
        users_db.append(new_user)
    return new_user


@app.route('/api/auth/register-phone', methods=['POST'])
def register_phone():
    """Register a customer by phone + password (OTP verifies ownership)."""
    data = request.get_json(silent=True) or {}
    phone = normalize_phone((data.get('phone') or '').strip())
    password = (data.get('password') or '')
    if not phone:
        return jsonify({"success": False, "message": "Invalid phone number"}), 400
    if len(password) < MIN_PASSWORD_LENGTH:
        return jsonify({"success": False, "message": f"Password must be at least {MIN_PASSWORD_LENGTH} characters"}), 400
    pd = _phone_digits(phone)
    existing = _find_user_by_phone_digits(pd)
    if existing and existing.get('phone_verified') and existing.get('password'):
        return jsonify({"success": False, "message": "This number is already registered. Please sign in."}), 409

    ok, msg = _request_otp_internal(phone, 'register', {'password_hash': hash_password(password)})
    if not ok:
        return jsonify({"success": False, "message": msg}), 429
    return jsonify({"success": True, "message": msg}), 200


@app.route('/api/auth/register-phone/verify', methods=['POST'])
def register_phone_verify():
    data = request.get_json(silent=True) or {}
    phone = normalize_phone((data.get('phone') or '').strip())
    code = (data.get('code') or '').strip()
    if not phone or not code:
        return jsonify({"success": False, "message": "Phone and code are required"}), 400
    ok, res, otp_doc = _verify_otp_code(phone, code)
    if not ok:
        return jsonify({"success": False, "message": res}), 400
    if otp_doc.get('purpose') != 'register':
        return jsonify({"success": False, "message": "No registration in progress"}), 400
    password_hash = otp_doc.get('password_hash')
    if not password_hash:
        return jsonify({"success": False, "message": "No password recorded"}), 400
    user = _register_customer_by_phone(_phone_digits(phone), phone, password_hash)
    token = create_jwt_token(user['_id'], user.get('email', ''), user.get('role', 'customer'))
    return jsonify({"success": True, "token": token, "user": _safe_user(user)}), 200


@app.route('/api/auth/login-phone', methods=['POST'])
def login_phone():
    data = request.get_json(silent=True) or {}
    phone = normalize_phone((data.get('phone') or '').strip())
    password = (data.get('password') or '')
    if not phone or not password:
        return jsonify({"success": False, "message": "Phone and password are required"}), 400
    user = _find_user_by_phone_digits(_phone_digits(phone))
    if not user or not user.get('password') or not verify_password(password, user['password']):
        return jsonify({"success": False, "message": "Invalid credentials"}), 401
    if not user.get('phone_verified'):
        return jsonify({"success": False, "message": "Please verify your phone number first"}), 403
    token = create_jwt_token(user['_id'], user.get('email', ''), user.get('role', 'customer'))
    return jsonify({"success": True, "token": token, "user": _safe_user(user)}), 200


@app.route('/api/auth/forgot-password', methods=['POST'])
def forgot_password():
    data = request.get_json(silent=True) or {}
    phone = normalize_phone((data.get('phone') or '').strip())
    if not phone:
        return jsonify({"success": False, "message": "Invalid phone number"}), 400
    ok, msg = _request_otp_internal(phone, 'reset')
    if not ok:
        return jsonify({"success": False, "message": msg}), 429
    return jsonify({"success": True, "message": msg}), 200


@app.route('/api/auth/reset-password', methods=['POST'])
def reset_password():
    data = request.get_json(silent=True) or {}
    phone = normalize_phone((data.get('phone') or '').strip())
    code = (data.get('code') or '').strip()
    new_password = (data.get('new_password') or '')
    if not phone or not code or len(new_password) < MIN_PASSWORD_LENGTH:
        return jsonify({"success": False, "message": "Phone, code and a valid new password are required"}), 400
    ok, res, otp_doc = _verify_otp_code(phone, code)
    if not ok:
        return jsonify({"success": False, "message": res}), 400
    if otp_doc.get('purpose') != 'reset':
        return jsonify({"success": False, "message": "No password reset in progress"}), 400
    user = _find_user_by_phone_digits(_phone_digits(phone))
    if not user:
        return jsonify({"success": False, "message": "Account not found"}), 404
    new_hash = hash_password(new_password)
    if MONGODB_CONNECTED:
        users_collection.update_one({"_id": user["_id"]}, {"$set": {"password": new_hash}})
    else:
        user["password"] = new_hash
    return jsonify({"success": True, "message": "Password reset successfully"}), 200


@app.route('/api/auth/profile', methods=['GET'])
@token_required
def get_profile():
    uid = request.user.get('user_id')
    if not uid or uid == 'anonymous':
        return jsonify({"success": False, "message": "Not authenticated"}), 401

    if MONGODB_CONNECTED:
        try:
            user = users_collection.find_one({"_id": ObjectId(uid)})
        except Exception:
            # In-memory IDs (e.g. "admin_mem") aren't ObjectIds
            user = users_collection.find_one({"_id": uid})
    else:
        user = next((u for u in users_db if str(u["_id"]) == uid), None)

    if not user:
        return jsonify({"success": False, "message": "User not found"}), 404
    return jsonify({"success": True, "user": {
        "id": str(user['_id']), "email": user['email'],
        "name": user['name'], "role": user['role'],
    }}), 200


# ============================================================================
# STATIC + PAGE ROUTES
# ============================================================================

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/notifications')
def notifications_page():
    return send_from_directory('.', 'notifications.html')

@app.route('/admin-notifications')
def admin_notifications_page():
    return send_from_directory('.', 'admin-notifications.html')


@app.route('/IMG/<path:filename>')
def serve_img(filename):
    return send_from_directory('IMG', filename)


@app.route('/favicon.ico')
def favicon_file():
    return send_from_directory('IMG', 'web logo.jpeg', mimetype='image/jpeg')


@app.route('/site.webmanifest')
def webmanifest_file():
    return send_from_directory('.', 'site.webmanifest', mimetype='application/manifest+json')


@app.route('/robots.txt')
def robots_file():
    return send_from_directory('.', 'robots.txt', mimetype='text/plain')


@app.route('/sitemap.xml')
def sitemap_file():
    return send_from_directory('.', 'sitemap.xml', mimetype='application/xml')


@app.route('/admin')
def admin_page():
    """
    Serve admin.html publicly. The HTML bytes are not sensitive; the data
    behind it is protected by @admin_required on the API endpoints, and
    the client script checks for a valid token in localStorage before
    rendering anything.

    (See AUDIT_REPORT.md §B3 for why this is the correct fix.)
    """
    admin_path = os.path.join(app.root_path, 'admin.html')
    if not os.path.exists(admin_path):
        return jsonify({
            "success": False,
            "message": "Admin UI not available",
            "root_path": app.root_path,
            "cwd": os.getcwd(),
            "files": os.listdir(app.root_path)
        }), 500
    resp = send_from_directory(app.root_path, 'admin.html')
    resp.headers['X-Robots-Tag'] = 'noindex, nofollow'
    return resp


@app.route('/admin-login')
def admin_login_page():
    try:
        html_path = os.path.join(app.root_path, 'admin-login.html')
        if not os.path.exists(html_path):
            return send_from_directory('.', 'admin-login.html')
        resp = make_response(send_from_directory(app.root_path, 'admin-login.html'))
        resp.headers['Content-Type'] = 'text/html; charset=utf-8'
        resp.headers['X-Robots-Tag'] = 'noindex, nofollow'
        return resp
    except Exception:
        return send_from_directory('.', 'admin-login.html')


@app.route('/scanty-theme.css')
def scanty_theme_css():
    resp = make_response(send_from_directory('.', 'scanty-theme.css'))
    resp.headers['Content-Type'] = 'text/css; charset=utf-8'
    return resp


@app.route('/staff')
def staff_page():
    return send_from_directory('.', 'staff.html')


@app.route('/staff-login')
def staff_login_page():
    resp = make_response(send_from_directory('.', 'staff-login.html'))
    resp.headers['Content-Type'] = 'text/html; charset=utf-8'
    resp.headers['X-Robots-Tag'] = 'noindex, nofollow'
    return resp


@app.route('/meals.json')
def meals_file():
    return send_from_directory('.', 'meals.json')


# ============================================================================
# MENU
# ============================================================================

@app.route('/api/menu', methods=['GET'])
def get_menu():
    """Public: returns available meals. Prices come from the database."""
    items = get_meals(available_only=True)
    return jsonify({"success": True, "items": items})


# ============================================================================
# MENU MANAGEMENT (admin + super_admin)
# ============================================================================

def _unique_meal_key(name):
    base = _slugify(name) or 'meal'
    key = base
    ts = str(int(datetime.now().timestamp()))
    if MONGODB_CONNECTED:
        if meals_collection.find_one({"key": key}):
            key = f"{base}-{ts}"
    else:
        if any(m.get("key") == key for m in meals_db):
            key = f"{base}-{ts}"
    return key


@app.route('/api/admin/meals', methods=['GET'])
@admin_required
def admin_list_meals():
    return jsonify({"success": True, "items": get_meals(available_only=False)})


@app.route('/api/admin/meals', methods=['POST'])
@admin_required
def admin_create_meal():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({"success": False, "message": "Name is required"}), 400
    try:
        price = round(float(data.get('price')), 2)
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "Invalid price"}), 400
    if price < 0:
        return jsonify({"success": False, "message": "Invalid price"}), 400

    meal = {
        "key": _unique_meal_key(name),
        "name": name,
        "description": (data.get('description') or '').strip(),
        "price": price,
        "category": (data.get('category') or '').strip(),
        "image": (data.get('image') or '').strip(),
        "available": bool(data.get('available', True)),
        "created_at": datetime.now().isoformat(),
    }
    if MONGODB_CONNECTED:
        result = meals_collection.insert_one(meal)
        meal["_id"] = result.inserted_id
    else:
        meal["_id"] = meal["key"]
        meals_db.append(meal)
    return jsonify({"success": True, "meal": _meal_dict(meal)}), 201


@app.route('/api/admin/meals/<meal_id>', methods=['PUT'])
@admin_required
def admin_update_meal(meal_id):
    meal = get_meal_by_id(meal_id)
    if not meal:
        return jsonify({"success": False, "message": "Meal not found"}), 404
    data = request.get_json(silent=True) or {}
    updates = {}
    if 'name' in data:
        updates['name'] = (data.get('name') or '').strip()
    if 'description' in data:
        updates['description'] = (data.get('description') or '').strip()
    if 'category' in data:
        updates['category'] = (data.get('category') or '').strip()
    if 'image' in data:
        updates['image'] = (data.get('image') or '').strip()
    if 'available' in data:
        updates['available'] = bool(data.get('available'))
    if 'price' in data:
        try:
            price = round(float(data.get('price')), 2)
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "Invalid price"}), 400
        if price < 0:
            return jsonify({"success": False, "message": "Invalid price"}), 400
        updates['price'] = price

    if not updates:
        return jsonify({"success": True, "meal": _meal_dict(meal)})

    if MONGODB_CONNECTED:
        meals_collection.update_one({"_id": meal["_id"]}, {"$set": updates})
    else:
        meal.update(updates)

    updated = get_meal_by_id(meal_id)
    return jsonify({"success": True, "meal": _meal_dict(updated)})


@app.route('/api/admin/meals/<meal_id>/availability', methods=['PATCH'])
@admin_required
def admin_meal_availability(meal_id):
    data = request.get_json(silent=True) or {}
    available = bool(data.get('available', True))
    meal = get_meal_by_id(meal_id)
    if not meal:
        return jsonify({"success": False, "message": "Meal not found"}), 404
    if MONGODB_CONNECTED:
        meals_collection.update_one({"_id": meal["_id"]}, {"$set": {"available": available}})
    else:
        meal["available"] = available
    updated = get_meal_by_id(meal_id)
    return jsonify({"success": True, "meal": _meal_dict(updated)})


# ============================================================================
# ORDERS
# ============================================================================

MAX_ITEM_QUANTITY = 100


def _coerce_items(raw):
    """Extract [{id, quantity}] pairs; validation happens in create_order."""
    items = []
    if not isinstance(raw, list):
        return items
    for it in raw:
        if not isinstance(it, dict):
            continue
        meal_id = it.get('id') or it.get('meal_id')
        if meal_id is None or str(meal_id).strip() == '':
            continue
        items.append({"id": str(meal_id).strip(), "quantity": it.get('quantity')})
    return items


def _verify_order_items(items):
    """Validate items against the meals collection.

    Returns (verified_items, error) where error is a (response, status) tuple
    on failure or None on success. Client-supplied prices/names are ignored —
    unit prices always come from MongoDB.
    """
    verified_items = []
    for item in items:
        qty = item['quantity']
        if isinstance(qty, bool):
            return None, (jsonify({"success": False, "message": "Invalid quantity"}), 400)
        try:
            qty_f = float(qty)
        except (TypeError, ValueError):
            return None, (jsonify({"success": False, "message": "Invalid quantity"}), 400)
        if qty_f <= 0 or not qty_f.is_integer():
            return None, (jsonify({"success": False, "message": "Invalid quantity"}), 400)
        quantity = int(qty_f)
        if quantity > MAX_ITEM_QUANTITY:
            return None, (jsonify({"success": False,
                                   "message": f"Quantity exceeds the maximum of {MAX_ITEM_QUANTITY} per item"}), 400)

        meal = get_meal_by_id(item['id'])
        if not meal:
            return None, (jsonify({"success": False, "message": "Invalid menu item"}), 400)
        if not meal.get('available', True):
            return None, (jsonify({"success": False,
                                   "message": f"{meal.get('name', 'This item')} is currently unavailable"}), 400)

        unit_price = round(float(meal.get('price') or 0), 2)
        line_total = round(unit_price * quantity, 2)
        verified_items.append({
            "id": str(meal.get('_id')),
            "key": meal.get('key'),
            "name": meal.get('name', 'Item'),
            "quantity": quantity,
            "unit_price": unit_price,
            "line_total": line_total,
        })
    return verified_items, None


def _next_order_number():
    if MONGODB_CONNECTED:
        counter_doc = order_counter_collection.find_one_and_update(
            {"_id": "order_id"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        if not counter_doc or "seq" not in counter_doc:
            return None
        return counter_doc["seq"]
    order_number = order_counter[0]
    order_counter[0] += 1
    return order_number


@app.route('/api/orders', methods=['POST'])
def create_order():
    """Public — customers may place orders without an account.

    Prices are ALWAYS resolved server-side from the meals collection; any
    client-supplied price or name is ignored.
    """
    try:
        data = request.get_json(silent=True) or {}

        customer_name = (data.get('customer_name') or data.get('customerName') or 'Guest').strip()
        customer_phone = (data.get('customer_phone') or data.get('phone_number')
                           or data.get('phoneNumber') or data.get('customerPhone') or '').strip()
        customer_location = (data.get('customer_location') or data.get('delivery_location')
                              or data.get('deliveryLocation') or data.get('customerLocation') or '').strip()
        delivery_method = (data.get('delivery_method') or 'delivery').strip().lower()
        if delivery_method not in ('delivery', 'pickup'):
            delivery_method = 'delivery'
        order_instructions = (data.get('order_instructions') or data.get('instructions') or '').strip()
        idempotency_key = (data.get('idempotency_key') or '').strip() or None

        # Duplicate-submission prevention for customer checkout.
        if idempotency_key:
            if MONGODB_CONNECTED:
                existing = orders_collection.find_one({"idempotency_key": idempotency_key})
            else:
                existing = next((o for o in orders_db if o.get('idempotency_key') == idempotency_key), None)
            if existing:
                if MONGODB_CONNECTED:
                    existing["_id"] = str(existing["_id"])
                return jsonify({"success": True, "message": "Order already placed",
                                "order": existing, "duplicate": True}), 200

        items_raw = data.get('items') or data.get('cartItems') or []
        items = _coerce_items(items_raw)

        if not items:
            return jsonify({"success": False, "message": "Cart is empty"}), 400

        verified_items, err = _verify_order_items(items)
        if err:
            return err

        subtotal = round(sum(i['line_total'] for i in verified_items), 2)
        total = subtotal  # no tax/delivery in this phase

        order_number = _next_order_number()
        if order_number is None:
            return jsonify({"success": False, "message": "Order counter error"}), 500

        order = {
            "id": f"SCM-{order_number:04d}",
            "source": "CUSTOMER",
            "customer_name": customer_name,
            "customer_phone": customer_phone,
            "customer_phone_digits": _phone_digits(customer_phone),
            "customer_location": customer_location,
            "delivery_method": delivery_method,
            "order_instructions": order_instructions,
            "items": verified_items,
            "subtotal": subtotal,
            "total": total,
            "currency": "GHS",
            "price_verified": True,
            "payment_method": "cash",
            "payment_status": "pending",
            "status": "Confirmed",
            "idempotency_key": idempotency_key,
            "timestamp": datetime.now().isoformat(),
            "date": date.today().isoformat(),
        }

        if MONGODB_CONNECTED:
            try:
                result = orders_collection.insert_one(order)
                order["_id"] = str(result.inserted_id)
            except Exception as e:
                if "duplicate key" in str(e) and idempotency_key:
                    existing = orders_collection.find_one({"idempotency_key": idempotency_key})
                    if existing:
                        existing["_id"] = str(existing["_id"])
                        return jsonify({"success": True, "message": "Order already placed",
                                        "order": existing, "duplicate": True}), 200
                app.logger.exception("Order insert failed: %s", repr(e))
                return jsonify({"success": False, "message": "Failed to save order"}), 500
        else:
            orders_db.append(order)

        # Fire notifications only AFTER the order is successfully saved.
        # Each channel is independent and never cancels the order.
        _notify_customer_order(order)

        return jsonify({
            "success": True,
            "message": "Order placed successfully",
            "order": {
                "id": order["id"],
                "subtotal": order["subtotal"],
                "total": order["total"],
                "status": order["status"],
                "payment_status": order["payment_status"],
                "created_at": order.get("timestamp"),
                "date": order.get("date"),
            },
        }), 201

    except Exception as e:
        app.logger.exception("POST /api/orders | Unhandled exception: %s", repr(e))
        return jsonify({"success": False, "message": "Failed to place order"}), 500


@app.route('/api/orders', methods=['GET'])
@admin_required
def get_orders():
    """Admin-only: lists all orders (contains PII). Supports ?date, ?source, ?payment_status."""
    date_filter = request.args.get('date')
    source_filter = (request.args.get('source') or '').strip().upper() or None
    payment_filter = (request.args.get('payment_status') or '').strip().lower() or None

    from datetime import datetime
    today = datetime.utcnow().strftime("%Y-%m-%d")

    if MONGODB_CONNECTED:
        query = {}
        if date_filter:
            query["date"] = date_filter
        if source_filter:
            query["source"] = source_filter
        if payment_filter:
            query["payment_status"] = payment_filter
        orders = list(orders_collection.find(query))
        for o in orders:
            o["_id"] = str(o["_id"])

        all_orders = list(orders_collection.find({}))
        today_orders_list = list(orders_collection.find({
            "$or": [
                {"date": today},
                {"timestamp": {"$regex": f"^{today}"}}
            ]
        }))
    else:
        def _matches(o):
            if date_filter and o.get('date') != date_filter:
                return False
            if source_filter and o.get('source') != source_filter:
                return False
            if payment_filter and o.get('payment_status') != payment_filter:
                return False
            return True
        orders = [o for o in orders_db if _matches(o)]
        all_orders = list(orders_db)

        today_orders_list = []
        for o in orders_db:
            o_date = o.get("date", "")
            o_timestamp = o.get("timestamp", "")
            if o_date == today or (o_timestamp and o_timestamp.startswith(today)):
                today_orders_list.append(o)

    total = _revenue_breakdown(all_orders)
    today_bd = _revenue_breakdown(today_orders_list)

    stats = {
        "totalOrders": len(all_orders),
        "totalRevenue": total["paid"],
        "grossRevenue": total["gross"],
        "pendingRevenue": total["pending"],
        "cancelledRevenue": total["cancelled"],
        "todayOrders": len(today_orders_list),
        "todayRevenue": today_bd["paid"],
        "grossTodayRevenue": today_bd["gross"],
        "pendingTodayRevenue": today_bd["pending"],
    }

    return jsonify({
        "success": True,
        "orders": orders,
        "stats": stats
    })


def _orders_for_phone(phone):
    digits = _phone_digits(phone)
    if not digits:
        return []
    if MONGODB_CONNECTED:
        return list(orders_collection.find({"$or": [
            {"customer_phone_digits": digits},
            {"customer_phone": phone},
        ]}))
    return [o for o in orders_db
            if _phone_digits(o.get('customer_phone')) == digits or o.get('customer_phone') == phone]


@app.route('/api/my-orders', methods=['GET'])
def my_orders():
    """
    Customer order history — now requires authentication.

    - Customers see only orders tied to their own verified phone number.
    - Staff/admin/super_admin may look up any customer by ?phone= (support).
    This closes the previous unauthenticated PII exposure.
    """
    token = _read_token()
    payload = verify_jwt_token(token) if token else None
    if not payload:
        return jsonify({"success": False, "message": "Authentication required"}), 401

    role = payload.get('role')
    uid = payload.get('user_id')

    if role in ('staff', 'admin', 'super_admin'):
        phone = (request.args.get('phone') or '').strip()
        if not phone:
            return jsonify({"success": False, "message": "Phone is required"}), 400
    else:
        # Customer: derive phone from their own user record (never trust the query).
        user = _find_user_by_id(uid)
        if not user:
            return jsonify({"success": False, "message": "User not found"}), 404
        if not user.get('phone_verified'):
            return jsonify({"success": False, "message": "Phone verification required to view your orders"}), 403
        phone = (user.get('phone') or '')
        if not phone:
            return jsonify({"success": False, "message": "Phone not found"}), 400

    orders = _orders_for_phone(phone)
    if MONGODB_CONNECTED:
        for o in orders:
            o["_id"] = str(o["_id"])

    return jsonify({"success": True, "orders": orders})


@app.route('/api/orders/<order_id>/response', methods=['POST'])
def add_order_response(order_id):
    """Customers may attach a message to their order — public by design."""
    data = request.get_json(silent=True) or {}
    customer_name = (data.get('customer_name') or '').strip()
    customer_phone = (data.get('customer_phone') or '').strip()
    message = (data.get('message') or '').strip()
    if not message:
        return jsonify({"success": False, "message": "Message is required"}), 400
    if len(message) > 1000:
        return jsonify({"success": False, "message": "Message too long"}), 400

    response_obj = {
        'id': str(ObjectId()) if MONGODB_CONNECTED else f"resp_{int(datetime.utcnow().timestamp())}",
        'customer_name': customer_name,
        'customer_phone': customer_phone,
        'message': message,
        'timestamp': datetime.utcnow().isoformat(),
    }

    if MONGODB_CONNECTED:
        result = orders_collection.find_one_and_update(
            {'id': order_id},
            {'$push': {'responses': response_obj}},
            return_document=ReturnDocument.AFTER,
        )
        if not result:
            return jsonify({"success": False, "message": "Order not found"}), 404
        result['_id'] = str(result['_id'])
        return jsonify({"success": True, "order": result, "response": response_obj}), 201

    for order in orders_db:
        if order.get('id') == order_id:
            order.setdefault('responses', []).append(response_obj)
            return jsonify({"success": True, "order": order, "response": response_obj}), 201
    return jsonify({"success": False, "message": "Order not found"}), 404


@app.route('/api/orders/<order_id>/responses', methods=['GET'])
@admin_required
def list_order_responses(order_id):
    if MONGODB_CONNECTED:
        order = orders_collection.find_one({'id': order_id})
        if not order:
            return jsonify({"success": False, "message": "Order not found"}), 404
        return jsonify({"success": True, "responses": order.get('responses', [])})
    for order in orders_db:
        if order.get('id') == order_id:
            return jsonify({"success": True, "responses": order.get('responses', [])})
    return jsonify({"success": False, "message": "Order not found"}), 404


VALID_STATUSES = {"Confirmed", "Preparing", "Ready", "Out for Delivery", "Delivered", "Completed", "Cancelled"}


@app.route('/api/orders/<order_id>/status', methods=['PUT'])
@admin_required
def update_order_status(order_id):
    data = request.get_json(silent=True) or {}
    new_status = (data.get('status') or '').strip()
    if new_status not in VALID_STATUSES:
        return jsonify({"success": False, "message": "Invalid status"}), 400

    if MONGODB_CONNECTED:
        result = orders_collection.find_one_and_update(
            {"id": order_id},
            {"$set": {"status": new_status}},
            return_document=ReturnDocument.AFTER,   # was: True (bug — silently returned BEFORE doc)
        )
        if not result:
            return jsonify({"success": False, "message": "Order not found"}), 404
        result["_id"] = str(result["_id"])
        
        # Create Customer Notification
        phone = result.get('customer_phone')
        if phone:
            msg_map = {
                "Confirmed": f"Your order {order_id} has been confirmed.",
                "Preparing": "Your food is being prepared.",
                "Ready": "Your order is ready for pickup.",
                "Out for Delivery": "Your order is out for delivery.",
                "Delivered": "Your order has been delivered.",
                "Cancelled": f"Your order {order_id} has been cancelled."
            }
            try:
                _create_notification(
                    audience=f"customer:{phone}",
                    notif_type="status_update",
                    payload={
                        "title": f"Order {new_status}",
                        "message": msg_map.get(new_status, f"Your order status is now {new_status}."),
                        "order_id": order_id
                    }
                )
            except Exception as e:
                app.logger.warning(f"Failed to create customer notification: {e}")
            try:
                _notify_customer_status_sms(result, new_status)
            except Exception as e:
                app.logger.warning(f"Customer status SMS failed: {e}")

        return jsonify({"success": True, "order": result})

    for order in orders_db:
        if order['id'] == order_id:
            order['status'] = new_status
            phone = order.get('customer_phone')
            if phone:
                msg_map = {
                    "Confirmed": f"Your order {order_id} has been confirmed.",
                    "Preparing": "Your food is being prepared.",
                    "Ready": "Your order is ready for pickup.",
                    "Out for Delivery": "Your order is out for delivery.",
                    "Delivered": "Your order has been delivered.",
                    "Cancelled": f"Your order {order_id} has been cancelled."
                }
                try:
                    _create_notification(
                        audience=f"customer:{phone}",
                        notif_type="status_update",
                        payload={
                            "title": f"Order {new_status}",
                            "message": msg_map.get(new_status, f"Your order status is now {new_status}."),
                            "order_id": order_id
                        }
                    )
                except Exception as e:
                    app.logger.warning(f"Failed to create customer notification: {e}")
                try:
                    _notify_customer_status_sms(order, new_status)
                except Exception as e:
                    app.logger.warning(f"Customer status SMS failed: {e}")
            return jsonify({"success": True, "order": order})
    return jsonify({"success": False, "message": "Order not found"}), 404


# ============================================================================
# ANALYTICS (admin only)
# ============================================================================

def _safe_total(o):
    try:
        return float(o.get('total') or 0)
    except (TypeError, ValueError):
        return 0.0


def _order_is_cancelled(order):
    return (order.get('status') or order.get('order_status')) == 'Cancelled'


def _order_is_paid(order):
    """Successful-sales test: not cancelled, and not failed/refunded/unpaid."""
    if _order_is_cancelled(order):
        return False
    ps = order.get('payment_status')
    if ps in ('failed', 'refunded', 'cancelled'):
        return False
    if ps in ('paid', 'completed', 'success'):
        return True
    if ps in ('pending', 'unpaid'):
        return False
    # Legacy orders (no payment_status) are treated as paid-on-delivery so that
    # historical revenue is preserved.
    return True


def _revenue_breakdown(orders):
    paid = sum(_safe_total(o) for o in orders if _order_is_paid(o))
    cancelled = sum(_safe_total(o) for o in orders if _order_is_cancelled(o))
    gross = sum(_safe_total(o) for o in orders if not _order_is_cancelled(o))
    pending = sum(_safe_total(o) for o in orders
                  if (o.get('payment_status') in ('pending', 'unpaid') and not _order_is_cancelled(o)))
    return {
        "paid": round(paid, 2),
        "gross": round(gross, 2),
        "pending": round(pending, 2),
        "cancelled": round(cancelled, 2),
    }


def _tally_food(orders):
    food_sold = {}
    for order in orders:
        for item in (order.get('items') or []):
            name = item.get('name') or 'Unknown'
            try:
                qty = int(item.get('quantity') or 0)
            except (TypeError, ValueError):
                qty = 0
            food_sold[name] = food_sold.get(name, 0) + qty
    return food_sold


@app.route('/api/analytics/daily', methods=['GET'])
@admin_required
def daily_analytics():
    date_filter = request.args.get('date', date.today().isoformat())
    if MONGODB_CONNECTED:
        day_orders = list(orders_collection.find({"date": date_filter}))
        for o in day_orders:
            o["_id"] = str(o["_id"])
    else:
        day_orders = [o for o in orders_db if o.get('date') == date_filter]

    bd = _revenue_breakdown(day_orders)
    return jsonify({
        "success": True,
        "date": date_filter,
        "total_revenue": bd["paid"],
        "gross_revenue": bd["gross"],
        "pending_revenue": bd["pending"],
        "cancelled_revenue": bd["cancelled"],
        "total_orders": len(day_orders),
        "food_sold": _tally_food(day_orders),
        "orders": day_orders,
    })


@app.route('/api/analytics/summary', methods=['GET'])
@admin_required
def analytics_summary():
    if MONGODB_CONNECTED:
        all_orders = list(orders_collection.find({}))
    else:
        all_orders = list(orders_db)
    bd = _revenue_breakdown(all_orders)
    return jsonify({
        "success": True,
        "total_revenue": bd["paid"],
        "gross_revenue": bd["gross"],
        "pending_revenue": bd["pending"],
        "cancelled_revenue": bd["cancelled"],
        "total_orders": len(all_orders),
        "food_sold": _tally_food(all_orders),
    })


# ============================================================================
# POS SALES (staff + admin + super_admin)
# ============================================================================

def _record_audit(event, actor, order_id, total):
    doc = {
        "event": event,
        "actor_id": actor.get("user_id"),
        "actor_name": actor.get("name"),
        "actor_username": actor.get("username"),
        "actor_role": actor.get("role"),
        "order_id": order_id,
        "total": total,
        "created_at": datetime.now().isoformat(),
    }
    if MONGODB_CONNECTED:
        try:
            audit_logs_collection.insert_one(doc)
        except Exception as e:
            app.logger.warning(f"audit log insert failed: {e}")
    else:
        audit_logs_db.append(doc)


def _source_for_role(role):
    return "ADMIN" if role in ("admin", "super_admin") else "STAFF"


@app.route('/api/pos/sale', methods=['POST'])
@staff_required
def create_pos_sale():
    """Record a cash sale. Authorized: staff, admin, super_admin."""
    data = request.get_json(silent=True) or {}
    actor = request.user

    items_raw = data.get('items') or []
    items = _coerce_items(items_raw)
    if not items:
        return jsonify({"success": False, "message": "Cart is empty"}), 400

    verified_items, err = _verify_order_items(items)
    if err:
        return err

    subtotal = round(sum(i['line_total'] for i in verified_items), 2)
    total = subtotal

    payment_method = (data.get('payment_method') or 'cash').strip().lower()
    if payment_method not in ('cash', 'paystack'):
        return jsonify({"success": False, "message": "Invalid payment method"}), 400
    if payment_method != 'cash':
        return jsonify({"success": False, "message": "Paystack payments are not yet available"}), 400

    idempotency_key = (data.get('idempotency_key') or '').strip() or None

    # Duplicate-submission prevention (idempotent cash sales).
    if idempotency_key:
        if MONGODB_CONNECTED:
            existing = orders_collection.find_one({"idempotency_key": idempotency_key})
        else:
            existing = next((o for o in orders_db if o.get('idempotency_key') == idempotency_key), None)
        if existing:
            if MONGODB_CONNECTED:
                existing["_id"] = str(existing["_id"])
            return jsonify({"success": True, "message": "Sale already recorded",
                            "order": existing, "duplicate": True}), 200

    actor_user = _find_user_by_id(actor.get('user_id'))
    actor_name = (actor_user.get('name') if actor_user else None) or actor.get('email') or 'Staff'
    actor_username = (actor_user.get('username') if actor_user else None)

    order_number = _next_order_number()
    if order_number is None:
        return jsonify({"success": False, "message": "Order counter error"}), 500

    order = {
        "id": f"SCM-{order_number:04d}",
        "source": _source_for_role(actor.get('role')),
        "created_by": str(actor.get('user_id')),
        "created_by_name": actor_name,
        "created_by_username": actor_username,
        "customer_name": (data.get('customer_name') or '').strip() or 'Walk-in Customer',
        "customer_phone": (data.get('customer_phone') or '').strip(),
        "customer_phone_digits": _phone_digits((data.get('customer_phone') or '')),
        "customer_location": (data.get('customer_location') or '').strip(),
        "items": verified_items,
        "subtotal": subtotal,
        "total": total,
        "currency": "GHS",
        "price_verified": True,
        "payment_method": payment_method,
        "payment_status": "paid",
        "status": "Completed",
        "idempotency_key": idempotency_key,
        "timestamp": datetime.now().isoformat(),
        "date": date.today().isoformat(),
    }

    if MONGODB_CONNECTED:
        try:
            result = orders_collection.insert_one(order)
            order["_id"] = str(result.inserted_id)
        except Exception as e:
            if "duplicate key" in str(e) and idempotency_key:
                existing = orders_collection.find_one({"idempotency_key": idempotency_key})
                if existing:
                    existing["_id"] = str(existing["_id"])
                    return jsonify({"success": True, "message": "Sale already recorded",
                                    "order": existing, "duplicate": True}), 200
            app.logger.exception("POS sale insert failed: %s", repr(e))
            return jsonify({"success": False, "message": "Failed to save sale"}), 500
    else:
        orders_db.append(order)

    _record_audit("pos_sale", {
        "user_id": str(actor.get('user_id')),
        "name": actor_name,
        "username": actor_username,
        "role": actor.get('role'),
    }, order["id"], total)

    # Notify the admin for staff sales only (avoid self-notifications on admin sales).
    if order.get('source') == 'STAFF':
        try:
            _create_notification(
                audience="admin",
                notif_type="new_order",
                payload={
                    "title": "New Sale (Staff POS)",
                    "message": (f"Staff: {actor_name}\n"
                                f"Order: {order['id']}\n"
                                f"Items: {_order_items_summary(order)}\n"
                                f"Total: GHS {float(order.get('total') or 0):.2f}\n"
                                f"Source: STAFF POS\n"
                                f"Payment: Cash - PAID"),
                    "order_id": order["id"],
                    "source": "STAFF",
                },
                dedupe_key=f"pos_sale:{order['id']}",
            )
        except Exception as e:
            app.logger.warning(f"Admin POS-sale notification failed: {e}")
        try:
            admin_phone = get_admin_sms_phone()
            if admin_phone:
                send_sms(admin_phone,
                         (f"SCANTYMEALS - STAFF SALE!\n\n{actor_name} recorded sale {order['id']}.\n"
                          f"Items: {_order_items_summary(order)}.\n"
                          f"Total: GHS {float(order.get('total') or 0):.2f} (Cash - PAID)."),
                         order['id'], 'admin_pos_sale')
        except Exception as e:
            app.logger.warning(f"Admin POS-sale SMS failed: {e}")

    return jsonify({"success": True, "message": "Sale recorded", "order": order}), 201


@app.route('/api/pos/my-sales', methods=['GET'])
@staff_required
def pos_my_sales():
    """Staff see their own POS sales; admin/super_admin see all POS sales."""
    actor = request.user
    role = actor.get('role')

    if MONGODB_CONNECTED:
        if role == 'staff':
            query = {"source": {"$in": ["STAFF", "ADMIN"]}, "created_by": str(actor.get('user_id'))}
        else:
            query = {"source": {"$in": ["STAFF", "ADMIN"]}}
        sales = list(orders_collection.find(query).sort("timestamp", -1).limit(200))
        for s in sales:
            s["_id"] = str(s["_id"])
    else:
        if role == 'staff':
            sales = [o for o in orders_db
                     if o.get('source') in ('STAFF', 'ADMIN') and o.get('created_by') == str(actor.get('user_id'))]
        else:
            sales = [o for o in orders_db if o.get('source') in ('STAFF', 'ADMIN')]
        sales = sorted(sales, key=lambda o: o.get('timestamp') or '', reverse=True)[:200]

    return jsonify({"success": True, "sales": sales})


# ============================================================================
# SMS SERVICE + SETTINGS + CASH COLLECTION
# ============================================================================

ADMIN_SMS_SETTING_KEY = "admin_sms_phone"


def _get_setting(key):
    if MONGODB_CONNECTED:
        doc = settings_collection.find_one({"key": key})
        return doc.get("value") if doc else None
    return settings_db.get(key)


def _set_setting(key, value):
    if MONGODB_CONNECTED:
        settings_collection.update_one(
            {"key": key},
            {"$set": {"value": value, "updated_at": datetime.now().isoformat()}},
            upsert=True,
        )
    else:
        settings_db[key] = value


def get_admin_sms_phone():
    return _get_setting(ADMIN_SMS_SETTING_KEY)


def get_admin_sms_phones():
    """Return the list of normalized administrator notification numbers."""
    phones = []
    for raw in (os.getenv('ADMIN_NOTIFICATION_PHONES') or '').split(','):
        p = raw.strip()
        if p:
            norm = normalize_phone(p)
            if norm and norm not in phones:
                phones.append(norm)
    single = get_admin_sms_phone()
    if single:
        norm = normalize_phone(single) or single
        if norm not in phones:
            phones.append(norm)
    return phones


# ============================================================================
# CUSTOMER PHONE OTP (Gonline SMS transport)
# ============================================================================

OTP_EXPIRY_MINUTES = 5
OTP_RESEND_COOLDOWN_SECONDS = 60
OTP_MAX_ATTEMPTS = 5
OTP_RATE_LIMIT_PER_IP = 20
OTP_RATE_LIMIT_PER_PHONE = 10
OTP_RATE_WINDOW_SECONDS = 3600


def _hash_otp(otp, salt):
    return hmac.new(OTP_SECRET.encode('utf-8'), (otp + salt).encode('utf-8'), hashlib.sha256).hexdigest()


def _rate_limit_ok(key, limit):
    now = datetime.utcnow()
    window_start = (now - timedelta(seconds=OTP_RATE_WINDOW_SECONDS)).isoformat()
    if MONGODB_CONNECTED:
        # Reset expired windows (best-effort; the atomic $inc below is authoritative).
        rate_limits_collection.update_one(
            {"key": key, "window_start": {"$lt": window_start}},
            {"$set": {"count": 0, "window_start": now.isoformat()}},
        )
        # Atomic increment and read-back; concurrent requests each get a
        # monotonically increasing count, so the limit cannot be exceeded.
        doc = rate_limits_collection.find_one_and_update(
            {"key": key},
            {"$inc": {"count": 1}, "$setOnInsert": {"window_start": now.isoformat()}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return int(doc.get("count") or 0) <= limit
    rec = rate_limits_db.get(key)
    if not rec or rec.get("window_start", "") < window_start:
        rate_limits_db[key] = {"count": 1, "window_start": now.isoformat()}
        return True
    if int(rec.get("count") or 0) >= limit:
        return False
    rec["count"] = int(rec.get("count") or 0) + 1
    return True


def _latest_otp(pd):
    if MONGODB_CONNECTED:
        return otp_codes_collection.find_one({"phone_digits": pd}, sort=[("issued_ts", -1)])
    cands = [o for o in otp_codes_db if o.get("phone_digits") == pd]
    return max(cands, key=lambda o: o.get("issued_ts") or 0) if cands else None


def _active_otp(pd):
    if MONGODB_CONNECTED:
        return otp_codes_collection.find_one(
            {"phone_digits": pd, "verified": False, "superseded": {"$ne": True}},
            sort=[("issued_ts", -1)])
    cands = [o for o in otp_codes_db
             if o.get("phone_digits") == pd and not o.get("verified") and not o.get("superseded")]
    return max(cands, key=lambda o: o.get("issued_ts") or 0) if cands else None


def _invalidate_phone_otps(pd):
    if MONGODB_CONNECTED:
        otp_codes_collection.update_many({"phone_digits": pd, "verified": False},
                                         {"$set": {"superseded": True}})
    else:
        for o in otp_codes_db:
            if o.get("phone_digits") == pd and not o.get("verified"):
                o["superseded"] = True


def _increment_otp_attempts(otp_doc):
    if MONGODB_CONNECTED:
        otp_codes_collection.update_one({"_id": otp_doc["_id"]}, {"$inc": {"attempts": 1}})
        otp_doc["attempts"] = int(otp_doc.get("attempts") or 0) + 1
    else:
        otp_doc["attempts"] = int(otp_doc.get("attempts") or 0) + 1


def _mark_otp_verified(otp_doc):
    if MONGODB_CONNECTED:
        result = otp_codes_collection.find_one_and_update(
            {"_id": otp_doc["_id"], "verified": False},
            {"$set": {"verified": True}},
            return_document=ReturnDocument.AFTER)
        return result is not None
    if otp_doc.get("verified"):
        return False
    otp_doc["verified"] = True
    return True


def _link_customer_by_phone(pd, phone):
    now = datetime.now().isoformat()
    user = _find_user_by_phone_digits(pd)
    if user:
        updates = {"phone_verified": True, "verified_at": now, "provider": "gonline_otp", "phone": phone}
        if MONGODB_CONNECTED:
            users_collection.update_one({"_id": user["_id"]}, {"$set": updates})
        else:
            user.update(updates)
        return _find_user_by_phone_digits(pd) or user
    new_user = {
        "email": f"phone_{pd}@scanty.local",
        "password": hash_password(os.urandom(16).hex()),
        "name": f"Customer {pd[-4:]}",
        "role": "customer",
        "phone": phone,
        "provider": "gonline_otp",
        "phone_verified": True,
        "verified_at": now,
        "created_at": now,
    }
    if MONGODB_CONNECTED:
        r = users_collection.insert_one(new_user)
        new_user["_id"] = r.inserted_id
    else:
        new_user["_id"] = f"usr_{len(users_db) + 1}"
        users_db.append(new_user)
    return new_user


def _send_otp_sms(phone, message):
    """Send an OTP directly via the provider — never logged, never auto-retried.

    Returns (ok, reason) where reason is a safe failure description (no OTP,
    no API key, no full URL).
    """
    try:
        provider = get_sms_provider()
        ok, resp = provider.send(phone, message)
        if ok:
            return True, None
        reason = (resp or {}).get('error') or (resp or {}).get('status') or 'unknown'
        return False, str(reason)
    except Exception as e:
        return False, f"exception:{type(e).__name__}"


def _sms_already_sent(dedupe_key):
    if MONGODB_CONNECTED:
        return sms_logs_collection.find_one({"dedupe_key": dedupe_key}) is not None
    return any(s.get("dedupe_key") == dedupe_key for s in sms_logs_db)


def _outbox_has(dedupe_key):
    if MONGODB_CONNECTED:
        return sms_outbox_collection.find_one({"dedupe_key": dedupe_key}) is not None
    return any(i.get("dedupe_key") == dedupe_key for i in sms_outbox_db)


def enqueue_sms(recipient, message, order_id=None, event_type="alert"):
    """Enqueue an SMS for asynchronous delivery (deduped). Never sends inline."""
    recipient = _normalize_sms_recipient(recipient)
    if not recipient:
        return False
    dedupe_key = f"{order_id or 'noid'}:{event_type}:{recipient}"
    if _sms_already_sent(dedupe_key) or _outbox_has(dedupe_key):
        return False
    doc = {
        "dedupe_key": dedupe_key,
        "recipient": recipient,
        "message": message,
        "order_id": order_id,
        "event_type": event_type,
        "status": "pending",
        "created_at": datetime.now().isoformat(),
    }
    if MONGODB_CONNECTED:
        try:
            sms_outbox_collection.insert_one(doc)
        except Exception:
            return False
    else:
        sms_outbox_db.append(doc)
    return True


def _process_sms_outbox(limit=50):
    """Drain pending outbox items by sending them (dedupe + log via send_sms)."""
    if MONGODB_CONNECTED:
        items = list(sms_outbox_collection.find({"status": "pending"}).limit(limit))
    else:
        items = [i for i in sms_outbox_db if i.get("status") == "pending"][:limit]
    now = datetime.utcnow().isoformat()
    count = 0
    for item in items:
        if MONGODB_CONNECTED:
            claimed = sms_outbox_collection.find_one_and_update(
                {"_id": item["_id"], "status": "pending"},
                {"$set": {"status": "processing"}})
            if not claimed:
                continue
        send_sms(item.get("recipient"), item.get("message"), item.get("order_id"), item.get("event_type"))
        if MONGODB_CONNECTED:
            sms_outbox_collection.update_one({"_id": item["_id"]}, {"$set": {"status": "done", "processed_at": now}})
        else:
            item["status"] = "done"
            item["processed_at"] = now
        count += 1
    return count


SMS_MAX_RETRIES = 3
_SMS_BACKOFF_SECONDS = [60, 300, 900]  # exponential-ish capped backoff


def _sms_status_and_failure(resp, ok):
    """Map a provider result to (status, failure_reason, retryable)."""
    if isinstance(resp, dict) and resp.get('simulated'):
        return "simulated", None, False
    if ok:
        return "accepted", None, False
    reason = None
    retryable = False
    if isinstance(resp, dict):
        reason = resp.get('error') or resp.get('status') or 'unknown'
        retryable = bool(resp.get('retryable'))
    return "failed", reason, retryable


def _sms_next_retry_at(retry_count):
    delay = _SMS_BACKOFF_SECONDS[min(int(retry_count or 0), len(_SMS_BACKOFF_SECONDS) - 1)]
    return (datetime.utcnow() + timedelta(seconds=delay)).isoformat()


def _normalize_sms_recipient(phone):
    """Normalize an SMS recipient to international (+233…) format for Gonline.

    Falls back to the raw input when it cannot be normalized (never silently drops).
    """
    p = normalize_phone(phone)
    return p if p else str(phone or '')


def send_sms(recipient, message, order_id=None, event_type="alert"):
    """Send an SMS via the configured provider with dedupe + persistent log.

    Returns (ok, log_doc). Never raises — an SMS failure never cancels the
    caller's operation. Statuses: simulated | accepted | failed | duplicate.
    (delivered is only set later from confirmed delivery evidence.)
    """
    recipient = _normalize_sms_recipient(recipient)
    provider = get_sms_provider()
    dedupe_key = f"{order_id or 'noid'}:{event_type}:{recipient}"
    if _sms_already_sent(dedupe_key):
        return False, {"dedupe_key": dedupe_key, "status": "duplicate", "provider": provider.name}

    try:
        ok, resp = provider.send(recipient, message)
    except Exception as e:
        ok, resp = False, {"provider": provider.name, "error": str(e)}

    status, failure_reason, retryable = _sms_status_and_failure(resp, ok)

    doc = {
        "dedupe_key": dedupe_key,
        "recipient": recipient,
        "message": message,
        "order_id": order_id,
        "event_type": event_type,
        "provider": provider.name,
        "status": status,
        "simulated": status == "simulated",
        "failure_reason": failure_reason,
        "retryable": retryable,
        "retry_count": 0,
        "next_retry_at": _sms_next_retry_at(0) if (status == "failed" and retryable) else None,
        "attempt_at": datetime.now().isoformat(),
        "response": resp or {},
    }
    if MONGODB_CONNECTED:
        try:
            sms_logs_collection.insert_one(doc)
        except Exception as e:
            app.logger.warning(f"sms log insert failed: {e}")
    else:
        sms_logs_db.append(doc)
    return ok, doc


def _retry_sms(log_doc):
    """Re-attempt one SMS using the same dedupe_key, updating its existing log.

    Never creates a new log entry; never resends an already-accepted/simulated
    message.
    """
    provider = get_sms_provider()
    try:
        ok, resp = provider.send(log_doc.get('recipient'), log_doc.get('message'))
    except Exception as e:
        ok, resp = False, {"provider": provider.name, "error": str(e)}

    status, failure_reason, retryable = _sms_status_and_failure(resp, ok)
    retry_count = int(log_doc.get('retry_count') or 0) + 1
    exhausted = retry_count >= SMS_MAX_RETRIES
    updates = {
        "status": status,
        "simulated": status == "simulated",
        "failure_reason": failure_reason,
        "retryable": retryable,
        "retry_count": retry_count,
        "next_retry_at": _sms_next_retry_at(retry_count) if (status == "failed" and retryable and not exhausted) else None,
        "attempt_at": datetime.now().isoformat(),
        "response": resp or {},
    }
    if MONGODB_CONNECTED:
        sms_logs_collection.update_one({"_id": log_doc["_id"]}, {"$set": updates})
        log_doc.update(updates)
    else:
        log_doc.update(updates)
    return ok, log_doc


def _retry_due_sms(limit=20):
    """Retry up to `limit` failed, retryable, due SMS messages. Returns count."""
    now = datetime.utcnow().isoformat()
    if MONGODB_CONNECTED:
        due = list(sms_logs_collection.find({
            "status": "failed", "retryable": True,
            "retry_count": {"$lt": SMS_MAX_RETRIES},
            "next_retry_at": {"$lte": now},
        }).limit(limit))
    else:
        due = [s for s in sms_logs_db
               if s.get('status') == 'failed' and s.get('retryable')
               and int(s.get('retry_count') or 0) < SMS_MAX_RETRIES
               and s.get('next_retry_at') and s.get('next_retry_at') <= now][:limit]
    count = 0
    for log in due:
        if MONGODB_CONNECTED:
            # Atomic claim so two workers never process the same retry.
            claimed = sms_logs_collection.find_one_and_update(
                {"_id": log["_id"], "status": "failed", "retryable": True},
                {"$set": {"status": "retrying"}},
            )
            if not claimed:
                continue
            claimed["status"] = "failed"  # restore contract for _retry_sms
            log = claimed
        _retry_sms(log)
        count += 1
    return count


def _start_sms_worker():
    """Best-effort background SMS worker (opt-in via SMS_WORKER=1).

    Drains the SMS outbox and retries failed messages. The outbox and sms_logs
    are MongoDB-backed, so a restart does not lose queued work. For guaranteed
    processing in production, schedule a Render cron/job that calls
    POST /api/sms/retry (or drains the outbox).
    """
    if os.getenv('SMS_WORKER') != '1':
        return

    def _loop():
        try:
            _process_sms_outbox(limit=50)
        except Exception as e:
            app.logger.warning(f"sms outbox worker error: {e}")
        try:
            _retry_due_sms(limit=50)
        except Exception as e:
            app.logger.warning(f"sms retry worker error: {e}")
        threading.Timer(15, _loop).start()

    threading.Timer(5, _loop).start()


def _order_items_summary(order):
    return ", ".join(f"{i.get('quantity')} {i.get('name')}" for i in (order.get('items') or []))


def _payment_phrase(order):
    return "Cash on Pickup" if (order.get('delivery_method') or 'delivery') == 'pickup' else "Cash on Delivery"


def _customer_order_sms_message(order):
    return (f"Your ScantyMeals order {order['id']} has been successfully placed. "
            f"Items: {_order_items_summary(order)}. "
            f"Total: GHS {float(order.get('total') or 0):.2f}. "
            f"Payment: {_payment_phrase(order)}. "
            f"Thank you for choosing ScantyMeals!")


def _admin_order_sms_message(order):
    return (f"NEW SCANTYMEALS ORDER: {order['id']}. "
            f"Customer: {order.get('customer_name') or 'Customer'}. "
            f"Phone: {order.get('customer_phone') or 'N/A'}. "
            f"Items: {_order_items_summary(order)}. "
            f"Total: GHS {float(order.get('total') or 0):.2f}. "
            f"Payment: {_payment_phrase(order)}.")


def _notify_customer_order_sms(order):
    phone = order.get('customer_phone')
    if not phone:
        return False
    return enqueue_sms(phone, _customer_order_sms_message(order), order['id'], 'order_placed')


def _notify_admin_order_sms(order):
    sent = False
    for admin_phone in get_admin_sms_phones():
        sent = enqueue_sms(admin_phone, _admin_order_sms_message(order), order['id'], 'admin_new_order') or sent
    return sent


def _notify_customer_order(order):
    """Fire all notifications for a newly saved customer order.

    Dashboard notification is synchronous (fast, in-DB). SMS messages are
    enqueued to the outbox so they never block order creation.
    """
    try:
        _create_notification(
            audience="admin",
            notif_type="new_order",
            payload={
                "title": "New Order Received",
                "message": (f"Customer: {order.get('customer_name')}\n"
                            f"Phone: {order.get('customer_phone') or 'N/A'}\n"
                            f"Order: {order['id']}\n"
                            f"Items: {_order_items_summary(order)}\n"
                            f"Total: GHS {float(order.get('total') or 0):.2f}\n"
                            f"Source: CUSTOMER WEBSITE\n"
                            f"Delivery: {_payment_phrase(order)}\n"
                            f"Payment: Pending"),
                "order_id": order["id"],
                "source": "CUSTOMER",
            },
            dedupe_key=f"new_order:{order['id']}",
        )
    except Exception as e:
        app.logger.warning(f"Admin dashboard notification failed: {e}")

    try:
        _notify_customer_order_sms(order)
    except Exception as e:
        app.logger.warning(f"Customer SMS enqueue failed: {e}")

    try:
        _notify_admin_order_sms(order)
    except Exception as e:
        app.logger.warning(f"Admin SMS enqueue failed: {e}")

    # If no async worker is running (dev/test), drain the outbox now so SMS
    # still fires immediately. In production (SMS_WORKER=1) the worker handles it.
    if os.getenv('SMS_WORKER') != '1':
        try:
            _process_sms_outbox(limit=20)
        except Exception as e:
            app.logger.warning(f"SMS outbox drain failed: {e}")


def _notify_customer_status_sms(order, status):
    phone = order.get('customer_phone')
    if not phone:
        return False
    msgs = {
        "payment_confirmed": f"Payment received for your ScantyMeals order {order['id']}. Thank you!",
        "Preparing": "Your food is being prepared.",
        "Ready": "Your order is ready for pickup.",
        "Out for Delivery": "Your order is out for delivery.",
        "Delivered": "Your order has been delivered. Enjoy!",
    }
    text = msgs.get(status)
    if not text:
        return False
    event = f"status:{status}" if status in ("Preparing", "Ready", "Out for Delivery", "Delivered") else status
    return send_sms(phone, text, order['id'], event)[0]


@app.route('/api/orders/<order_id>/confirm-cash', methods=['POST'])
@staff_required
def confirm_cash(order_id):
    """Authorized staff/admin confirm that cash was actually collected."""
    actor = request.user
    role = actor.get('role')

    if MONGODB_CONNECTED:
        order = orders_collection.find_one({"id": order_id})
    else:
        order = next((o for o in orders_db if o.get('id') == order_id), None)

    if not order:
        return jsonify({"success": False, "message": "Order not found"}), 404
    if order.get('payment_status') == 'paid':
        return jsonify({"success": False, "message": "Order is already paid"}), 409
    if order.get('payment_status') not in ('pending', 'unpaid', None):
        return jsonify({"success": False, "message": "Order is not eligible for cash collection"}), 400
    if order.get('status') == 'Cancelled':
        return jsonify({"success": False, "message": "Cancelled orders cannot be confirmed"}), 400
    if role == 'staff' and order.get('source') != 'CUSTOMER':
        return jsonify({"success": False, "message": "You are not authorized to confirm this order"}), 403

    actor_user = _find_user_by_id(actor.get('user_id'))
    actor_name = (actor_user.get('name') if actor_user else None) or actor.get('email') or 'Staff'

    update = {
        "payment_status": "paid",
        "payment_method": order.get('payment_method') or 'cash',
        "cash_received_by": str(actor.get('user_id')),
        "cash_received_by_name": actor_name,
        "cash_received_at": datetime.now().isoformat(),
    }

    if MONGODB_CONNECTED:
        result = orders_collection.find_one_and_update(
            {"id": order_id, "payment_status": {"$ne": "paid"}},
            {"$set": update},
            return_document=ReturnDocument.AFTER,
        )
        if not result:
            existing = orders_collection.find_one({"id": order_id})
            if existing and existing.get('payment_status') == 'paid':
                return jsonify({"success": False, "message": "Order is already paid"}), 409
            return jsonify({"success": False, "message": "Order not found"}), 404
        result["_id"] = str(result["_id"])
        order = result
    else:
        order.update(update)

    _record_audit("cash_confirmed", {
        "user_id": str(actor.get('user_id')),
        "name": actor_name,
        "username": (actor_user.get('username') if actor_user else None),
        "role": role,
    }, order_id, order.get('total'))

    try:
        _notify_customer_status_sms(order, "payment_confirmed")
    except Exception as e:
        app.logger.warning(f"Customer payment-confirmed SMS failed: {e}")

    return jsonify({"success": True, "order": order}), 200


@app.route('/api/settings/admin-sms', methods=['GET'])
@super_admin_required
def get_admin_sms_setting():
    return jsonify({"success": True, "value": get_admin_sms_phone() or ""})


@app.route('/api/settings/admin-sms', methods=['PUT'])
@super_admin_required
def update_admin_sms_setting():
    data = request.get_json(silent=True) or {}
    phone = normalize_phone(data.get('value'))
    if not phone:
        return jsonify({"success": False, "message": "Invalid phone number"}), 400
    _set_setting(ADMIN_SMS_SETTING_KEY, phone)
    actor = request.user
    _record_audit("update_setting", {
        "user_id": str(actor.get('user_id')),
        "name": actor.get('email'),
        "username": None,
        "role": actor.get('role'),
    }, None, None)
    return jsonify({"success": True, "value": phone})


@app.route('/api/sms-logs', methods=['GET'])
@admin_required
def list_sms_logs():
    if MONGODB_CONNECTED:
        logs = list(sms_logs_collection.find({}).sort("attempt_at", -1).limit(100))
        for l in logs:
            l["_id"] = str(l["_id"])
    else:
        logs = sorted(sms_logs_db, key=lambda x: x.get("attempt_at") or '', reverse=True)[:100]
    return jsonify({"success": True, "logs": logs})


@app.route('/api/sms/retry', methods=['POST'])
@admin_required
def retry_sms():
    """Manually retry a failed SMS (by dedupe_key) or all due failed messages."""
    data = request.get_json(silent=True) or {}
    dedupe_key = (data.get('dedupe_key') or '').strip()
    if dedupe_key:
        if MONGODB_CONNECTED:
            log = sms_logs_collection.find_one({"dedupe_key": dedupe_key})
        else:
            log = next((s for s in sms_logs_db if s.get('dedupe_key') == dedupe_key), None)
        if not log:
            return jsonify({"success": False, "message": "SMS log not found"}), 404
        if log.get('status') != 'failed':
            return jsonify({"success": False, "message": "Only failed messages can be retried"}), 400
        ok, log = _retry_sms(log)
        if MONGODB_CONNECTED and '_id' in log:
            log['_id'] = str(log['_id'])
        return jsonify({"success": True, "ok": ok, "log": log})
    count = _retry_due_sms(limit=100)
    return jsonify({"success": True, "retried": count})


# ============================================================================
# BULK FOOD PACKS & EVENT ORDERS
# ============================================================================

BULK_STATUSES = [
    "REQUEST_RECEIVED", "QUOTATION_PREPARED", "AWAITING_CUSTOMER_CONFIRMATION", "APPROVED",
    "AWAITING_DEPOSIT", "CONFIRMED", "PREPARING", "PACKAGING", "READY_FOR_PICKUP",
    "OUT_FOR_DELIVERY", "DELIVERED", "COLLECTED", "COMPLETED", "CANCELLED",
]

BULK_EVENT_TYPES = ["Wedding", "Funeral", "Church Programme", "Birthday",
                    "Corporate Meeting", "Conference", "School Programme", "Other"]

OPERATIONAL_BULK_STATUSES = {"PREPARING", "PACKAGING", "READY_FOR_PICKUP",
                             "OUT_FOR_DELIVERY", "DELIVERED", "COLLECTED", "COMPLETED"}

ALLOWED_BULK_TRANSITIONS = {
    "REQUEST_RECEIVED": {"AWAITING_CUSTOMER_CONFIRMATION", "CANCELLED"},
    "QUOTATION_PREPARED": {"AWAITING_CUSTOMER_CONFIRMATION", "REQUEST_RECEIVED", "CANCELLED"},
    "AWAITING_CUSTOMER_CONFIRMATION": {"APPROVED", "AWAITING_DEPOSIT", "CONFIRMED", "CANCELLED"},
    "APPROVED": {"AWAITING_DEPOSIT", "CONFIRMED", "CANCELLED"},
    "AWAITING_DEPOSIT": {"CONFIRMED", "CANCELLED"},
    "CONFIRMED": {"PREPARING", "CANCELLED"},
    "PREPARING": {"PACKAGING", "CANCELLED"},
    "PACKAGING": {"READY_FOR_PICKUP", "CANCELLED"},
    "READY_FOR_PICKUP": {"OUT_FOR_DELIVERY", "COLLECTED", "CANCELLED"},
    "OUT_FOR_DELIVERY": {"DELIVERED", "CANCELLED"},
    "DELIVERED": {"COMPLETED"},
    "COLLECTED": {"COMPLETED"},
    "COMPLETED": set(),
    "CANCELLED": set(),
}


def _safe_f(v):
    try:
        return round(float(v or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _bulk_payment_status(total, paid):
    if total <= 0 or paid <= 0:
        return "unpaid"
    if paid >= total - 0.005:
        return "paid"
    return "partial"


# Quotation may only be edited before the customer accepts/confirms it.
BULK_QUOTATION_EDITABLE = {"REQUEST_RECEIVED", "QUOTATION_PREPARED", "AWAITING_CUSTOMER_CONFIRMATION"}


def _bulk_packaging_incomplete(doc):
    """Return a list of meals whose agreed packs are not yet fully packaged."""
    agreed = {str(it.get('meal_id')): int(it.get('quoted_qty') or 0)
              for it in ((doc.get('quotation') or {}).get('items') or [])}
    prep = {str(it.get('meal_id')): it
            for it in ((doc.get('preparation') or {}).get('items') or [])}
    missing = []
    for meal_id, qty in agreed.items():
        packaged = int((prep.get(meal_id) or {}).get('packaged') or 0)
        if packaged < qty:
            missing.append({"meal_id": meal_id,
                            "name": (prep.get(meal_id) or {}).get('name') or meal_id,
                            "agreed": qty, "packaged": packaged})
    return missing


def _bulk_fulfillment_record(doc, target, actor_id, actor_name):
    rec = dict(doc.get('fulfillment_record') or {})
    now = datetime.now().isoformat()
    if target == "OUT_FOR_DELIVERY":
        rec["dispatched_at"] = now
        rec["dispatched_by"] = actor_id
        rec["dispatched_by_name"] = actor_name
    elif target == "DELIVERED":
        rec["delivered_at"] = now
        rec["delivered_by"] = actor_id
        rec["delivered_by_name"] = actor_name
    elif target == "COLLECTED":
        rec["collected_at"] = now
        rec["released_by"] = actor_id
        rec["released_by_name"] = actor_name
    return rec


def _next_bulk_number():
    if MONGODB_CONNECTED:
        counter = order_counter_collection.find_one_and_update(
            {"_id": "bulk_id"}, {"$inc": {"seq": 1}}, upsert=True, return_document=ReturnDocument.AFTER)
        return counter["seq"] if counter and "seq" in counter else None
    n = bulk_counter[0]
    bulk_counter[0] += 1
    return n


def _all_bulk():
    if MONGODB_CONNECTED:
        docs = list(bulk_orders_collection.find({}).sort("created_at", -1))
        for d in docs:
            d["_id"] = str(d["_id"])
        return docs
    return sorted(bulk_orders_db, key=lambda d: d.get("created_at") or "", reverse=True)


def _find_bulk(ref):
    if MONGODB_CONNECTED:
        doc = bulk_orders_collection.find_one({"reference": ref})
        if doc:
            doc["_id"] = str(doc["_id"])
        return doc
    return next((d for d in bulk_orders_db if d.get("reference") == ref), None)


def _bulk_update(ref, set_doc=None, push_doc=None):
    if MONGODB_CONNECTED:
        u = {}
        if set_doc:
            u["$set"] = set_doc
        if push_doc:
            u["$push"] = push_doc
        doc = bulk_orders_collection.find_one_and_update({"reference": ref}, u, return_document=ReturnDocument.AFTER)
        if doc:
            doc["_id"] = str(doc["_id"])
        return doc
    doc = next((d for d in bulk_orders_db if d.get("reference") == ref), None)
    if not doc:
        return None
    if set_doc:
        doc.update(set_doc)
    if push_doc:
        for k, v in push_doc.items():
            doc.setdefault(k, []).append(v)
    return doc


def _bulk_create(doc):
    if MONGODB_CONNECTED:
        r = bulk_orders_collection.insert_one(doc)
        doc["_id"] = str(r.inserted_id)
    else:
        bulk_orders_db.append(doc)
    return doc


def _bulk_customer_view(doc):
    return {
        "reference": doc.get("reference"),
        "customer_name": doc.get("customer_name"),
        "contact_name": doc.get("contact_name"),
        "event_type": doc.get("event_type"),
        "event_description": doc.get("event_description"),
        "event_date": doc.get("event_date"),
        "required_time": doc.get("required_time"),
        "fulfillment": doc.get("fulfillment"),
        "delivery_address": doc.get("delivery_address"),
        "meals": doc.get("meals"),
        "requested_total": doc.get("requested_total"),
        "total_packs": doc.get("total_packs"),
        "quotation": doc.get("quotation"),
        "amount_paid": doc.get("amount_paid"),
        "outstanding_balance": doc.get("outstanding_balance"),
        "payment_status": doc.get("payment_status"),
        "status": doc.get("status"),
        "preparation": doc.get("preparation"),
        "status_history": [{"from": h.get("from"), "to": h.get("to"), "at": h.get("at"), "note": h.get("note")}
                           for h in (doc.get("status_history") or [])],
        "created_at": doc.get("created_at"),
    }


def _bulk_summary():
    docs = _all_bulk()
    counts = {
        "total": len(docs),
        "awaiting_quotation": 0, "awaiting_confirmation": 0, "confirmed": 0,
        "preparing": 0, "packaging": 0, "ready": 0, "out_for_delivery": 0,
        "completed": 0, "outstanding_balance": 0.0,
    }
    for d in docs:
        s = d.get("status")
        if s in ("REQUEST_RECEIVED",):
            counts["awaiting_quotation"] += 1
        elif s in ("QUOTATION_PREPARED", "AWAITING_CUSTOMER_CONFIRMATION"):
            counts["awaiting_confirmation"] += 1
        elif s in ("APPROVED", "AWAITING_DEPOSIT", "CONFIRMED"):
            counts["confirmed"] += 1
        elif s == "PREPARING":
            counts["preparing"] += 1
        elif s == "PACKAGING":
            counts["packaging"] += 1
        elif s == "READY_FOR_PICKUP":
            counts["ready"] += 1
        elif s == "OUT_FOR_DELIVERY":
            counts["out_for_delivery"] += 1
        elif s == "COMPLETED":
            counts["completed"] += 1
        counts["outstanding_balance"] += _safe_f(d.get("outstanding_balance"))
    counts["outstanding_balance"] = round(counts["outstanding_balance"], 2)
    return counts


def _notify_admin_bulk_request(bulk):
    try:
        _create_notification("admin", "bulk_order", {
            "title": "New Bulk Food Pack Request",
            "message": (f"Customer: {bulk.get('customer_name')}\n"
                        f"Event: {bulk.get('event_type')}\n"
                        f"Packs: {bulk.get('total_packs')}\n"
                        f"Ref: {bulk['reference']}\n"
                        f"Source: BULK REQUEST"),
            "order_id": bulk["reference"],
            "source": "BULK",
        }, dedupe_key=f"bulk_request:{bulk['reference']}")
    except Exception as e:
        app.logger.warning(f"bulk admin notification failed: {e}")
    try:
        admin_phone = get_admin_sms_phone()
        if admin_phone:
            send_sms(admin_phone,
                     (f"SCANTYMEALS - BULK REQUEST!\n\n{bulk.get('customer_name')} requested "
                      f"{bulk.get('total_packs')} packs ({bulk.get('event_type')}).\n"
                      f"Ref: {bulk['reference']}.\nPlease prepare a quotation."),
                     bulk['reference'], 'bulk_request_admin')
    except Exception as e:
        app.logger.warning(f"bulk admin sms failed: {e}")


def _notify_customer_bulk_sms(bulk, event, text):
    phone = bulk.get('phone')
    if not phone:
        return
    try:
        send_sms(phone, text, bulk['reference'], event)
    except Exception as e:
        app.logger.warning(f"bulk customer sms failed: {e}")


def _push_status_history(bulk, from_status, to_status, by_name, note=None):
    entry = {"from": from_status, "to": to_status, "by": by_name, "at": datetime.now().isoformat(), "note": note or ""}
    _bulk_update(bulk["reference"], {"status": to_status, "updated_at": datetime.now().isoformat()},
                 {"status_history": entry})
    bulk["status"] = to_status
    bulk.setdefault("status_history", []).append(entry)


@app.route('/api/bulk-orders', methods=['POST'])
def create_bulk_order():
    """Public — customer submits a bulk food pack quotation request."""
    try:
        data = request.get_json(silent=True) or {}

        customer_name = (data.get('customer_name') or data.get('name') or '').strip()
        phone = (data.get('phone') or data.get('customer_phone') or '').strip()
        if not customer_name:
            return jsonify({"success": False, "message": "Customer name is required"}), 400
        if not phone:
            return jsonify({"success": False, "message": "Phone is required"}), 400

        event_type = (data.get('event_type') or 'Other').strip()
        if event_type not in BULK_EVENT_TYPES:
            event_type = 'Other'
        fulfillment = (data.get('fulfillment') or 'delivery').strip().lower()
        if fulfillment not in ('delivery', 'pickup'):
            fulfillment = 'delivery'

        meals_raw = data.get('meals') or []
        if not isinstance(meals_raw, list) or not meals_raw:
            return jsonify({"success": False, "message": "Select at least one meal"}), 400

        # Validate each meal + quantity, compute estimated total from DB prices.
        meals = []
        total_packs = 0
        requested_total = 0.0
        for m in meals_raw:
            if not isinstance(m, dict):
                continue
            meal = get_meal_by_id(m.get('meal_id') or m.get('id'))
            if not meal:
                return jsonify({"success": False, "message": "Invalid meal selected"}), 400
            qty = m.get('quantity') or m.get('requested_qty') or 0
            try:
                qty = int(qty)
            except (TypeError, ValueError):
                return jsonify({"success": False, "message": "Invalid pack quantity"}), 400
            if qty <= 0:
                continue
            unit_price = _safe_f(meal.get('price'))
            meals.append({"meal_id": str(meal.get('_id')), "key": meal.get('key'),
                          "name": meal.get('name'), "requested_qty": qty})
            total_packs += qty
            requested_total += unit_price * qty
        if not meals:
            return jsonify({"success": False, "message": "Select at least one meal"}), 400
        requested_total = round(requested_total, 2)

        number = _next_bulk_number()
        if number is None:
            return jsonify({"success": False, "message": "Reference generation error"}), 500

        doc = {
            "reference": f"SM-BULK-{number:04d}",
            "customer_name": customer_name,
            "contact_name": (data.get('contact_name') or '').strip(),
            "phone": phone,
            "phone_digits": _phone_digits(phone),
            "email": (data.get('email') or '').strip(),
            "event_type": event_type,
            "event_description": (data.get('event_description') or '').strip(),
            "event_date": (data.get('event_date') or '').strip(),
            "required_time": (data.get('required_time') or '').strip(),
            "fulfillment": fulfillment,
            "delivery_address": (data.get('delivery_address') or '').strip(),
            "location_details": (data.get('location_details') or '').strip(),
            "instructions": (data.get('instructions') or data.get('notes') or '').strip(),
            "meals": meals,
            "requested_total": requested_total,
            "total_packs": total_packs,
            "quotation": None,
            "amount_paid": 0.0,
            "outstanding_balance": 0.0,
            "payment_status": "unpaid",
            "payments": [],
            "preparation": {"items": [{"meal_id": mm["meal_id"], "name": mm["name"],
                                       "agreed_qty": None, "prepared": 0, "packaged": 0, "notes": ""}
                                      for mm in meals]},
            "fulfillment_record": {},
            "status": "REQUEST_RECEIVED",
            "status_history": [{"from": None, "to": "REQUEST_RECEIVED", "by": "customer",
                                "at": datetime.now().isoformat(), "note": "Request submitted"}],
            "admin_notes": [],
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }
        _bulk_create(doc)

        _notify_admin_bulk_request(doc)

        return jsonify({"success": True, "message": "Bulk order request submitted",
                        "bulk_order": {"reference": doc["reference"]}}), 201
    except Exception as e:
        app.logger.exception("create_bulk_order failed: %s", repr(e))
        return jsonify({"success": False, "message": "Failed to submit request"}), 500


@app.route('/api/bulk-orders', methods=['GET'])
@admin_required
def list_bulk_orders():
    """Admin list with filters + summary counts."""
    status_f = (request.args.get('status') or '').strip()
    event_f = (request.args.get('event_type') or '').strip()
    payment_f = (request.args.get('payment_status') or '').strip()
    fulfillment_f = (request.args.get('fulfillment') or '').strip()
    search = (request.args.get('search') or '').strip().lower()

    docs = _all_bulk()
    filtered = []
    for d in docs:
        if status_f and d.get('status') != status_f:
            continue
        if event_f and d.get('event_type') != event_f:
            continue
        if payment_f and d.get('payment_status') != payment_f:
            continue
        if fulfillment_f and d.get('fulfillment') != fulfillment_f:
            continue
        if search:
            hay = " ".join(str(d.get(k) or '') for k in ('reference', 'customer_name', 'contact_name', 'phone')).lower()
            if search not in hay:
                continue
        filtered.append(d)
    return jsonify({"success": True, "bulk_orders": filtered, "summary": _bulk_summary()})


@app.route('/api/bulk-orders/<reference>', methods=['GET'])
def get_bulk_order(reference):
    doc = _find_bulk(reference)
    if not doc:
        return jsonify({"success": False, "message": "Bulk order not found"}), 404
    token = _read_token()
    payload = verify_jwt_token(token) if token else None
    role = payload.get('role') if payload else None
    if role in ('staff', 'admin', 'super_admin'):
        return jsonify({"success": True, "bulk_order": doc})
    # Customer: only owner (by phone), sanitized view.
    if role == 'customer':
        user = _find_user_by_id(payload.get('user_id'))
        if user and _phone_digits(user.get('phone')) == doc.get('phone_digits'):
            return jsonify({"success": True, "bulk_order": _bulk_customer_view(doc)})
        return jsonify({"success": False, "message": "Not found"}), 404
    return jsonify({"success": False, "message": "Authentication required"}), 401


@app.route('/api/my-bulk-orders', methods=['GET'])
def my_bulk_orders():
    token = _read_token()
    payload = verify_jwt_token(token) if token else None
    if not payload:
        return jsonify({"success": False, "message": "Authentication required"}), 401
    role = payload.get('role')
    if role in ('staff', 'admin', 'super_admin'):
        docs = _all_bulk()
        return jsonify({"success": True, "bulk_orders": docs})
    user = _find_user_by_id(payload.get('user_id'))
    if not user:
        return jsonify({"success": False, "message": "User not found"}), 404
    if not user.get('phone_verified'):
        return jsonify({"success": False, "message": "Phone verification required to view your bulk orders"}), 403
    digits = _phone_digits((user or {}).get('phone'))
    docs = [d for d in _all_bulk() if d.get('phone_digits') == digits]
    return jsonify({"success": True, "bulk_orders": [_bulk_customer_view(d) for d in docs]})


@app.route('/api/bulk-orders/<reference>/quotation', methods=['PUT'])
@admin_required
def prepare_bulk_quotation(reference):
    doc = _find_bulk(reference)
    if not doc:
        return jsonify({"success": False, "message": "Bulk order not found"}), 404
    if doc.get('status') not in BULK_QUOTATION_EDITABLE:
        return jsonify({"success": False, "message": "Quotation is locked once the customer has accepted it"}), 409
    data = request.get_json(silent=True) or {}
    items_raw = data.get('items') or []
    items = []
    subtotal = 0.0
    total_packs = 0
    for it in items_raw:
        if not isinstance(it, dict):
            continue
        meal_id = it.get('meal_id') or it.get('id')
        quoted_qty = int(it.get('quoted_qty') or it.get('quantity') or 0)
        price = _safe_f(it.get('price_per_pack'))
        if quoted_qty <= 0 or price < 0:
            continue
        name = it.get('name') or 'Meal'
        line_total = round(quoted_qty * price, 2)
        subtotal += line_total
        total_packs += quoted_qty
        items.append({"meal_id": str(meal_id), "name": name, "quoted_qty": quoted_qty,
                      "price_per_pack": price, "line_total": line_total})
    if not items:
        return jsonify({"success": False, "message": "Quotation must include at least one meal"}), 400

    delivery_charge = _safe_f(data.get('delivery_charge'))
    discount = _safe_f(data.get('discount'))
    additional = _safe_f(data.get('additional_charges'))
    total = round(subtotal + delivery_charge + additional - discount, 2)
    if total < 0:
        total = 0.0
    deposit = _safe_f(data.get('deposit_required'))
    if deposit > total:
        deposit = total

    actor = request.user
    actor_user = _find_user_by_id(actor.get('user_id'))
    actor_name = (actor_user.get('name') if actor_user else None) or actor.get('email') or 'Admin'

    quotation = {
        "items": items,
        "subtotal": round(subtotal, 2),
        "delivery_charge": delivery_charge,
        "additional_charges": additional,
        "discount": discount,
        "total": total,
        "deposit_required": deposit,
        "version": int((doc.get('quotation') or {}).get('version') or 0) + 1,
        "prepared_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "prepared_by": str(actor.get('user_id')),
        "prepared_by_name": actor_name,
    }
    _bulk_update(reference, {
        "quotation": quotation,
        "total_packs": total_packs,
        # Reset payment fields when the quotation changes (pre-acceptance only).
        "amount_paid": 0.0,
        "outstanding_balance": total,
        "payment_status": "unpaid",
        "updated_at": datetime.now().isoformat(),
    })
    _push_status_history(doc, doc.get('status'), "AWAITING_CUSTOMER_CONFIRMATION", actor_name, "Quotation prepared")

    _record_audit("bulk_quotation", {"user_id": str(actor.get('user_id')), "name": actor_name,
                                     "username": (actor_user.get('username') if actor_user else None),
                                     "role": actor.get('role')}, reference, total)

    _notify_customer_bulk_sms(doc, "bulk_quotation",
        f"Your ScantyMeals bulk order {reference} quotation is ready: GHS {total:.2f}. "
        f"Please review and confirm. Call 0242292939 for assistance.")

    return jsonify({"success": True, "bulk_order": _find_bulk(reference)}), 200


@app.route('/api/bulk-orders/<reference>/accept', methods=['POST'])
def accept_bulk_quotation(reference):
    token = _read_token()
    payload = verify_jwt_token(token) if token else None
    if not payload or payload.get('role') != 'customer':
        return jsonify({"success": False, "message": "Authentication required"}), 401
    user = _find_user_by_id(payload.get('user_id'))
    doc = _find_bulk(reference)
    if not doc or not user or _phone_digits(user.get('phone')) != doc.get('phone_digits'):
        return jsonify({"success": False, "message": "Not found"}), 404
    if doc.get('status') not in ("QUOTATION_PREPARED", "AWAITING_CUSTOMER_CONFIRMATION"):
        return jsonify({"success": False, "message": "Quotation is not awaiting confirmation"}), 409
    q = doc.get('quotation')
    if not q:
        return jsonify({"success": False, "message": "No quotation available"}), 409
    data = request.get_json(silent=True) or {}
    if data.get('quotation_version') is not None and int(data.get('quotation_version')) != int(q.get('version') or 1):
        return jsonify({"success": False, "message": "Quotation has changed. Please review the latest quotation."}), 409
    deposit = q.get('deposit_required') or 0
    target = "AWAITING_DEPOSIT" if deposit > 0 else "CONFIRMED"
    _push_status_history(doc, doc.get('status'), target, "customer", "Customer accepted quotation")
    _record_audit("bulk_accepted", {"user_id": str(payload.get('user_id')), "name": doc.get('customer_name'),
                                    "username": None, "role": "customer"}, reference, q.get('total'))
    try:
        _create_notification("admin", "bulk_order", {
            "title": "Bulk Quotation Accepted",
            "message": f"{doc.get('customer_name')} accepted quotation {reference} (GHS {_safe_f(q.get('total')):.2f}).",
            "order_id": reference, "source": "BULK",
        }, dedupe_key=f"bulk_accept:{reference}")
    except Exception as e:
        app.logger.warning(f"bulk accept notification failed: {e}")
    return jsonify({"success": True, "bulk_order": _bulk_customer_view(_find_bulk(reference))}), 200


@app.route('/api/bulk-orders/<reference>/decline', methods=['POST'])
def decline_bulk_quotation(reference):
    token = _read_token()
    payload = verify_jwt_token(token) if token else None
    if not payload or payload.get('role') != 'customer':
        return jsonify({"success": False, "message": "Authentication required"}), 401
    user = _find_user_by_id(payload.get('user_id'))
    doc = _find_bulk(reference)
    if not doc or not user or _phone_digits(user.get('phone')) != doc.get('phone_digits'):
        return jsonify({"success": False, "message": "Not found"}), 404
    _push_status_history(doc, doc.get('status'), "CANCELLED", "customer", "Customer declined quotation")
    return jsonify({"success": True, "bulk_order": _bulk_customer_view(_find_bulk(reference))}), 200


@app.route('/api/bulk-orders/<reference>/payments', methods=['POST'])
@staff_required
def record_bulk_payment(reference):
    doc = _find_bulk(reference)
    if not doc:
        return jsonify({"success": False, "message": "Bulk order not found"}), 404
    data = request.get_json(silent=True) or {}
    amount = _safe_f(data.get('amount'))
    if amount <= 0:
        return jsonify({"success": False, "message": "Invalid payment amount"}), 400

    total = _safe_f((doc.get('quotation') or {}).get('total'))
    if total <= 0:
        return jsonify({"success": False, "message": "No quotation to pay against"}), 400

    actor = request.user
    actor_user = _find_user_by_id(actor.get('user_id'))
    actor_name = (actor_user.get('name') if actor_user else None) or actor.get('email') or 'Staff'
    ref = (data.get('reference') or '').strip() or f"pay-{int(datetime.utcnow().timestamp() * 1000)}"

    # Duplicate payment prevention by reference (best-effort pre-check).
    if any(p.get('ref') == ref for p in (doc.get('payments') or [])):
        return jsonify({"success": False, "message": "Duplicate payment reference"}), 409

    payment = {
        "amount": amount,
        "received_by": str(actor.get('user_id')),
        "received_by_name": actor_name,
        "received_at": datetime.now().isoformat(),
        "note": (data.get('note') or '').strip(),
        "ref": ref,
    }

    if MONGODB_CONNECTED:
        # Atomic, concurrency-safe update: only succeed if the outstanding
        # balance is still >= the payment amount (prevents overpayment races).
        result = bulk_orders_collection.find_one_and_update(
            {"reference": reference, "outstanding_balance": {"$gte": amount}},
            {"$inc": {"amount_paid": amount, "outstanding_balance": -amount},
             "$push": {"payments": payment}},
            return_document=ReturnDocument.AFTER,
        )
        if not result:
            existing = bulk_orders_collection.find_one({"reference": reference})
            if not existing:
                return jsonify({"success": False, "message": "Bulk order not found"}), 404
            if _safe_f(existing.get("outstanding_balance")) < amount:
                return jsonify({"success": False,
                                "message": f"Amount exceeds outstanding balance of GHS {_safe_f(existing.get('outstanding_balance')):.2f}"}), 400
            return jsonify({"success": False, "message": "Unable to record payment"}), 400
        result["_id"] = str(result["_id"])
        new_paid = _safe_f(result.get("amount_paid"))
        pstatus = _bulk_payment_status(total, new_paid)
        bulk_orders_collection.update_one(
            {"reference": reference},
            {"$set": {"payment_status": pstatus, "updated_at": datetime.now().isoformat()}},
        )
        doc = result
        doc["payment_status"] = pstatus
    else:
        paid = _safe_f(doc.get("amount_paid"))
        outstanding = round(total - paid, 2)
        if outstanding <= 0:
            return jsonify({"success": False, "message": "Order is already fully paid"}), 400
        if amount > outstanding + 0.005:
            return jsonify({"success": False, "message": f"Amount exceeds outstanding balance of GHS {outstanding:.2f}"}), 400
        new_paid = round(paid + amount, 2)
        new_outstanding = round(total - new_paid, 2)
        pstatus = _bulk_payment_status(total, new_paid)
        _bulk_update(reference, {
            "amount_paid": new_paid,
            "outstanding_balance": new_outstanding,
            "payment_status": pstatus,
            "updated_at": datetime.now().isoformat(),
        }, {"payments": payment})
        doc = _find_bulk(reference)

    _record_audit("bulk_payment", {"user_id": str(actor.get('user_id')), "name": actor_name,
                                   "username": (actor_user.get('username') if actor_user else None),
                                   "role": actor.get('role')}, reference, amount)

    if pstatus == "paid" and doc.get('status') in ("AWAITING_DEPOSIT", "APPROVED", "AWAITING_CUSTOMER_CONFIRMATION"):
        _push_status_history(doc, doc.get('status'), "CONFIRMED", actor_name, "Fully paid")

    _notify_customer_bulk_sms(doc, "bulk_payment",
        f"Payment of GHS {amount:.2f} received for bulk order {reference}. "
        f"Outstanding balance: GHS {_safe_f((_find_bulk(reference) or {}).get('outstanding_balance')):.2f}.")

    return jsonify({"success": True, "bulk_order": _find_bulk(reference)}), 200


@app.route('/api/bulk-orders/<reference>/status', methods=['POST'])
@staff_required
def update_bulk_status(reference):
    doc = _find_bulk(reference)
    if not doc:
        return jsonify({"success": False, "message": "Bulk order not found"}), 404
    data = request.get_json(silent=True) or {}
    target = (data.get('status') or '').strip().upper()
    note = (data.get('note') or '').strip()
    override = bool(data.get('override'))
    if target not in BULK_STATUSES:
        return jsonify({"success": False, "message": "Invalid status"}), 400

    role = request.user.get('role')
    is_admin = role in ('admin', 'super_admin')
    if override and not is_admin:
        override = False
    current = doc.get('status')
    allowed = ALLOWED_BULK_TRANSITIONS.get(current, set())
    if override and is_admin:
        pass
    elif role == 'staff':
        if target not in OPERATIONAL_BULK_STATUSES or target not in allowed:
            return jsonify({"success": False, "message": "Invalid status transition"}), 400
    else:
        if target not in allowed:
            return jsonify({"success": False, "message": "Invalid status transition"}), 400

    # Packaging integrity: cannot become "ready" while required packs remain
    # unpackaged, unless an authorised admin records an explicit override.
    if target == "READY_FOR_PICKUP":
        missing = _bulk_packaging_incomplete(doc)
        if missing and not (override and is_admin):
            return jsonify({"success": False, "message": "Not all packs are packaged yet", "missing": missing}), 409

    actor = request.user
    actor_user = _find_user_by_id(actor.get('user_id'))
    actor_name = (actor_user.get('name') if actor_user else None) or actor.get('email') or 'Staff'
    _push_status_history(doc, current, target, actor_name, note or "Status update")

    # Record responsible staff + timestamp for handover/delivery/collection.
    if target in ("OUT_FOR_DELIVERY", "DELIVERED", "COLLECTED"):
        rec = _bulk_fulfillment_record(doc, target, str(actor.get('user_id')), actor_name)
        _bulk_update(reference, {"fulfillment_record": rec})

    # Customer SMS on key operational milestones.
    msgs = {
        "PREPARING": "We have started preparing your bulk food packs.",
        "PACKAGING": "Your food packs are now being individually packaged.",
        "READY_FOR_PICKUP": f"Your bulk order {reference} is ready for pickup.",
        "OUT_FOR_DELIVERY": f"Your bulk order {reference} is out for delivery.",
        "DELIVERED": f"Your bulk order {reference} has been delivered. Enjoy!",
        "COLLECTED": f"Your bulk order {reference} has been collected. Thank you!",
    }
    if target in msgs:
        _notify_customer_bulk_sms(doc, f"bulk_status:{target}", msgs[target])

    return jsonify({"success": True, "bulk_order": _find_bulk(reference)}), 200


@app.route('/api/bulk-orders/<reference>/progress', methods=['POST'])
@staff_required
def update_bulk_progress(reference):
    doc = _find_bulk(reference)
    if not doc:
        return jsonify({"success": False, "message": "Bulk order not found"}), 404
    data = request.get_json(silent=True) or {}
    items = data.get('items') or []
    if not isinstance(items, list):
        return jsonify({"success": False, "message": "Invalid progress data"}), 400

    prep = doc.get('preparation') or {"items": []}
    prep_items = prep.get('items') or []
    for it in items:
        meal_id = str(it.get('meal_id') or it.get('id') or '')
        prepared = it.get('prepared')
        packaged = it.get('packaged')
        for pi in prep_items:
            if str(pi.get('meal_id')) == meal_id:
                if prepared is not None:
                    try:
                        pi['prepared'] = int(prepared)
                    except (TypeError, ValueError):
                        pass
                if packaged is not None:
                    try:
                        pi['packaged'] = int(packaged)
                    except (TypeError, ValueError):
                        pass
                if it.get('notes'):
                    pi['notes'] = (it.get('notes') or '').strip()
    prep['items'] = prep_items
    _bulk_update(reference, {"preparation": prep, "updated_at": datetime.now().isoformat()})
    return jsonify({"success": True, "bulk_order": _find_bulk(reference)}), 200


@app.route('/api/bulk-orders/<reference>/notes', methods=['POST'])
@admin_required
def add_bulk_admin_note(reference):
    doc = _find_bulk(reference)
    if not doc:
        return jsonify({"success": False, "message": "Bulk order not found"}), 404
    data = request.get_json(silent=True) or {}
    text = (data.get('text') or '').strip()
    if not text:
        return jsonify({"success": False, "message": "Note is required"}), 400
    actor = request.user
    actor_user = _find_user_by_id(actor.get('user_id'))
    actor_name = (actor_user.get('name') if actor_user else None) or actor.get('email') or 'Admin'
    _bulk_update(reference, {"updated_at": datetime.now().isoformat()},
                 {"admin_notes": {"text": text, "by": str(actor.get('user_id')), "by_name": actor_name,
                                  "at": datetime.now().isoformat()}})
    return jsonify({"success": True, "bulk_order": _find_bulk(reference)}), 200


@app.route('/api/bulk-orders/<reference>/cancel', methods=['POST'])
@admin_required
def cancel_bulk_order(reference):
    doc = _find_bulk(reference)
    if not doc:
        return jsonify({"success": False, "message": "Bulk order not found"}), 404
    data = request.get_json(silent=True) or {}
    actor = request.user
    actor_user = _find_user_by_id(actor.get('user_id'))
    actor_name = (actor_user.get('name') if actor_user else None) or actor.get('email') or 'Admin'
    reason = (data.get('reason') or '').strip() or "Cancelled by admin"
    _push_status_history(doc, doc.get('status'), "CANCELLED", actor_name, reason)
    # Preserve payment history; record the amount that must be refunded.
    _bulk_update(reference, {
        "cancelled_at": datetime.now().isoformat(),
        "cancelled_by": str(actor.get('user_id')),
        "cancelled_by_name": actor_name,
        "cancel_reason": reason,
        "refund_obligation": _safe_f(doc.get('amount_paid')),
    })
    _record_audit("bulk_cancelled", {"user_id": str(actor.get('user_id')), "name": actor_name,
                                     "username": (actor_user.get('username') if actor_user else None),
                                     "role": actor.get('role')}, reference, _safe_f(doc.get('amount_paid')))
    return jsonify({"success": True, "bulk_order": _find_bulk(reference)}), 200


# ============================================================================
# NOTIFICATIONS
# ============================================================================

def _create_notification(audience, notif_type, payload, dedupe_key=None):
    if dedupe_key:
        if MONGODB_CONNECTED:
            if notifications_collection.find_one({"dedupe_key": dedupe_key}):
                return None
        else:
            if any(n.get("dedupe_key") == dedupe_key for n in notifications_db):
                return None
    doc = {
        "id": str(ObjectId()) if MONGODB_CONNECTED else f"notif_{int(datetime.utcnow().timestamp()*1000)}",
        "audience": audience,
        "type": notif_type,
        "title": payload.get("title", ""),
        "message": payload.get("message", ""),
        "order_id": payload.get("order_id"),
        "source": payload.get("source"),
        "dedupe_key": dedupe_key,
        "payload": payload,
        "read": False,
        "created_at": datetime.utcnow().isoformat()
    }
    if MONGODB_CONNECTED:
        try:
            result = notifications_collection.insert_one(doc)
            doc["_id"] = str(result.inserted_id)
        except Exception as e:
            app.logger.warning(f"Failed to insert notification: {e}")
    else:
        notifications_db.append(doc)
    
    # Emit to Socket.IO room (room name = audience)
    socketio.emit(notif_type, doc, to=audience)
    
    # Optional FCM
    if FIREBASE_ADMIN_READY:
        fcm_token = payload.get("fcm_token")
        if not fcm_token:
            if MONGODB_CONNECTED:
                token_doc = fcm_tokens_collection.find_one({"audience": audience})
                if token_doc:
                    fcm_token = token_doc.get("token")
            else:
                fcm_token = fcm_tokens_db.get(audience)
                
        if fcm_token:
            try:
                msg = messaging.Message(
                    notification=messaging.Notification(
                        title=doc["title"],
                        body=doc["message"],
                    ),
                    data={"order_id": str(doc["order_id"] or ""), "type": notif_type, "url": "/#orders"},
                    token=fcm_token
                )
                messaging.send(msg)
            except Exception as e:
                app.logger.warning(f"FCM send failed: {e}")

@app.route('/api/notifications/register-token', methods=['POST'])
def register_fcm_token():
    data = request.get_json(silent=True) or {}
    audience = data.get('audience')
    token = data.get('token')
    
    if not audience or not token:
        return jsonify({"success": False, "message": "Audience and token are required"}), 400
        
    if MONGODB_CONNECTED:
        fcm_tokens_collection.update_one(
            {"audience": audience},
            {"$set": {"token": token, "updated_at": datetime.utcnow().isoformat()}},
            upsert=True
        )
    else:
        fcm_tokens_db[audience] = token
        
    return jsonify({"success": True})

@app.route('/api/notifications', methods=['POST'])
@admin_required
def create_notification_api():
    data = request.get_json(silent=True) or {}
    audience = data.get('audience')
    notif_type = data.get('type', 'alert')
    title = data.get('title', 'Notification')
    message = data.get('message', '')
    if not audience or not message:
        return jsonify({"success": False, "message": "Audience and message are required"}), 400
    _create_notification(audience, notif_type, {"title": title, "message": message})
    return jsonify({"success": True})

@app.route('/api/notifications', methods=['GET'])
def get_notifications():
    audience = request.args.get('audience') # e.g. 'admin' or 'customer:<phone>'
    if not audience:
        return jsonify({"success": False, "message": "Audience required"}), 400
    
    if MONGODB_CONNECTED:
        notifs = list(notifications_collection.find({"audience": audience}).sort("created_at", -1).limit(50))
        for n in notifs:
            n["_id"] = str(n["_id"])
    else:
        notifs = [n for n in notifications_db if n["audience"] == audience]
        notifs = sorted(notifs, key=lambda x: x["created_at"], reverse=True)[:50]
        
    return jsonify({"success": True, "notifications": notifs})

@app.route('/api/notifications/<notif_id>/read', methods=['PUT'])
def mark_notification_read(notif_id):
    if MONGODB_CONNECTED:
        try:
            from bson.objectid import ObjectId
            obj_id = ObjectId(notif_id)
            query = {"$or": [{"id": notif_id}, {"_id": obj_id}]}
        except:
            query = {"id": notif_id}
            
        result = notifications_collection.find_one_and_update(
            query,
            {"$set": {"read": True}},
            return_document=ReturnDocument.AFTER
        )
        if result:
            result["_id"] = str(result["_id"])
            return jsonify({"success": True, "notification": result})
    else:
        for n in notifications_db:
            if n["id"] == notif_id or str(n.get("_id", "")) == notif_id:
                n["read"] = True
                return jsonify({"success": True, "notification": n})
    return jsonify({"success": False, "message": "Notification not found"}), 404

@app.route('/api/notifications/<notif_id>', methods=['DELETE'])
def delete_notification(notif_id):
    if MONGODB_CONNECTED:
        try:
            from bson.objectid import ObjectId
            obj_id = ObjectId(notif_id)
            query = {"$or": [{"id": notif_id}, {"_id": obj_id}]}
        except:
            query = {"id": notif_id}
            
        result = notifications_collection.delete_one(query)
        if result.deleted_count:
            return jsonify({"success": True})
    else:
        global notifications_db
        initial_len = len(notifications_db)
        notifications_db = [n for n in notifications_db if n["id"] != notif_id]
        if len(notifications_db) < initial_len:
            return jsonify({"success": True})
    return jsonify({"success": False, "message": "Notification not found"}), 404

# ============================================================================
# STAFF MANAGEMENT (admin + super_admin)
# ============================================================================

@app.route('/api/staff', methods=['GET'])
@admin_required
def list_staff():
    if MONGODB_CONNECTED:
        staff = list(users_collection.find({"role": {"$in": list(STAFF_ROLES)}}))
    else:
        staff = [u for u in users_db if u.get('role') in STAFF_ROLES]
    staff.sort(key=lambda u: (u.get('created_at') or ''), reverse=True)
    return jsonify({"success": True, "staff": [_safe_user(u) for u in staff]})


@app.route('/api/staff', methods=['POST'])
@admin_required
def create_staff():
    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip()
    name = (data.get('name') or '').strip()
    password = data.get('password')
    role = (data.get('role') or 'staff').strip().lower()

    if not username:
        return jsonify({"success": False, "message": "Username is required"}), 400
    if role not in VALID_ROLES:
        return jsonify({"success": False, "message": "Invalid role"}), 400
    if role == 'customer':
        return jsonify({"success": False, "message": "Invalid role"}), 400
    if not _staff_role_allowed(request.user.get('role'), role):
        return jsonify({"success": False, "message": "You are not authorized to assign this role"}), 403

    generated = None
    if not password:
        password = os.urandom(9).hex()
        generated = password

    if MONGODB_CONNECTED:
        if users_collection.find_one({"username": username}):
            return jsonify({"success": False, "message": "Username already exists"}), 409
    else:
        if any(u.get('username') == username for u in users_db):
            return jsonify({"success": False, "message": "Username already exists"}), 409

    new_user = {
        "username": username,
        "name": name or username,
        "email": (data.get('email') or f"{username}@scanty.local").strip().lower(),
        "password": hash_password(password),
        "role": role,
        "status": "active",
        "created_by": str(request.user.get('user_id')),
        "created_at": datetime.now().isoformat(),
    }
    if MONGODB_CONNECTED:
        result = users_collection.insert_one(new_user)
        new_user["_id"] = result.inserted_id
    else:
        new_user["_id"] = f"usr_{len(users_db) + 1}"
        users_db.append(new_user)

    resp = {"success": True, "staff": _safe_user(new_user)}
    if generated:
        resp["password"] = generated
        resp["message"] = "Account created. Share this one-time password securely."
    return jsonify(resp), 201


@app.route('/api/staff/<uid>', methods=['PUT'])
@admin_required
def update_staff(uid):
    data = request.get_json(silent=True) or {}
    user = _find_user_by_id(uid)
    if not user:
        return jsonify({"success": False, "message": "Staff not found"}), 404

    updates = {}
    if data.get('name'):
        updates['name'] = (data.get('name') or '').strip()

    new_role = (data.get('role') or '').strip().lower()
    if new_role:
        if new_role not in VALID_ROLES or new_role == 'customer':
            return jsonify({"success": False, "message": "Invalid role"}), 400
        if not _staff_role_allowed(request.user.get('role'), new_role):
            return jsonify({"success": False, "message": "You are not authorized to assign this role"}), 403
        updates['role'] = new_role

    if data.get('email'):
        updates['email'] = (data.get('email') or '').strip().lower()

    if not updates:
        return jsonify({"success": True, "staff": _safe_user(user)})

    if MONGODB_CONNECTED:
        users_collection.update_one({"_id": user["_id"]}, {"$set": updates})
    else:
        user.update(updates)

    updated = _find_user_by_id(uid)
    return jsonify({"success": True, "staff": _safe_user(updated)})


@app.route('/api/staff/<uid>/status', methods=['PATCH'])
@admin_required
def update_staff_status(uid):
    data = request.get_json(silent=True) or {}
    status = (data.get('status') or '').strip().lower()
    if status not in ('active', 'suspended'):
        return jsonify({"success": False, "message": "Invalid status"}), 400

    user = _find_user_by_id(uid)
    if not user:
        return jsonify({"success": False, "message": "Staff not found"}), 404

    if user.get('role') == 'super_admin' and request.user.get('role') != 'super_admin':
        return jsonify({"success": False, "message": "Cannot modify a super admin account"}), 403

    if MONGODB_CONNECTED:
        users_collection.update_one({"_id": user["_id"]}, {"$set": {"status": status}})
    else:
        user["status"] = status

    updated = _find_user_by_id(uid)
    return jsonify({"success": True, "staff": _safe_user(updated)})


@app.route('/api/staff/<uid>/reset-password', methods=['POST'])
@admin_required
def reset_staff_password(uid):
    data = request.get_json(silent=True) or {}
    password = data.get('password')
    generated = None
    if not password:
        password = os.urandom(9).hex()
        generated = password

    user = _find_user_by_id(uid)
    if not user:
        return jsonify({"success": False, "message": "Staff not found"}), 404

    if user.get('role') == 'super_admin' and request.user.get('role') != 'super_admin':
        return jsonify({"success": False, "message": "Cannot reset a super admin account"}), 403

    if MONGODB_CONNECTED:
        users_collection.update_one({"_id": user["_id"]}, {"$set": {"password": hash_password(password)}})
    else:
        user["password"] = hash_password(password)

    resp = {"success": True, "message": "Password reset."}
    if generated:
        resp["password"] = generated
        resp["message"] = "Password reset. Share this one-time password securely."
    return jsonify(resp)


# ============================================================================
# SOCKET.IO EVENTS
# ============================================================================

@socketio.on('join')
def on_join(data):
    room = data.get('room')
    if room:
        join_room(room)
        app.logger.info(f"Socket connected to room: {room}")

@socketio.on('leave')
def on_leave(data):
    room = data.get('room')
    if room:
        leave_room(room)


# ============================================================================
# HEALTH
# ============================================================================

@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({
        "success": True,
        "mongodb": MONGODB_CONNECTED,
        "time": datetime.utcnow().isoformat(),
    })


# ============================================================================
# ENTRYPOINT (dev only — use gunicorn in production, see Procfile)
# ============================================================================

# Opt-in best-effort SMS worker (drains outbox + retries; disabled by default).
_start_sms_worker()

if __name__ == '__main__':
    socketio.run(app, host="0.0.0.0", debug=False, use_reloader=False, port=int(os.getenv('PORT', 5000)))