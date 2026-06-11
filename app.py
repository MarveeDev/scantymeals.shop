import os
import json
import base64
from datetime import datetime, date, timedelta
from functools import wraps

from flask import Flask, request, jsonify, send_from_directory, redirect, make_response
from flask_cors import CORS
from pymongo import MongoClient, ReturnDocument
from bson.objectid import ObjectId
import jwt
import bcrypt

# ============================================================================
# CONFIG
# ============================================================================

app = Flask(__name__, static_folder='.')

# CORS — restrict to known origins in production
_origins_env = os.getenv(
    'ADMIN_ORIGINS',
    'http://localhost:5000,http://127.0.0.1:5000'
)
ALLOWED_ORIGINS = [o.strip() for o in _origins_env.split(',') if o.strip()]
CORS(
    app,
    resources={r"/api/*": {"origins": ALLOWED_ORIGINS}},
    supports_credentials=False,
    expose_headers=["Content-Type", "Authorization"],
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

# MongoDB
MONGO_URI = os.getenv('MONGODB_URI', 'mongodb://localhost:27017/')
try:
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    mongo_client.admin.command('ping')
    db = mongo_client['scantymeals']
    orders_collection = db['orders']
    order_counter_collection = db['counters']
    users_collection = db['users']
    # Helpful indexes
    try:
        orders_collection.create_index('id', unique=True)
        orders_collection.create_index('date')
        users_collection.create_index('email', unique=True, sparse=True)
        users_collection.create_index('phone', sparse=True)
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
# MENU FALLBACK
# ============================================================================

MENU_ITEMS = [
    {"id": 1, "name": "Classic Fried Rice",
     "description": "Golden wok-tossed rice with egg, spring onion, soy sauce & sesame oil",
     "price": 25.00, "category": "Signature", "image": "/IMG/menu_1.jpg", "available": True},
    {"id": 2, "name": "Spicy Jollof Fried Rice",
     "description": "West African style fried rice loaded with peppers & aromatic spices",
     "price": 30.00, "category": "Spicy", "image": "/IMG/menu_2.jpg", "available": True},
    {"id": 3, "name": "Chicken Fried Rice",
     "description": "Succulent grilled chicken strips over smoky wok-fried rice",
     "price": 35.00, "category": "Protein", "image": "/IMG/menu_3.jpg", "available": True},
    {"id": 4, "name": "Shrimp Fried Rice",
     "description": "Plump tiger shrimps with garlic butter tossed in fragrant fried rice",
     "price": 42.00, "category": "Seafood", "image": "/IMG/menu_4.jpg", "available": True},
    {"id": 5, "name": "Veggie Fried Rice",
     "description": "Fresh garden vegetables, tofu & cashews in light soy fried rice",
     "price": 22.00, "category": "Vegetarian", "image": "/IMG/menu_5.jpg", "available": True},
    {"id": 6, "name": "Special Mixed Fried Rice",
     "description": "A generous feast: chicken, shrimp, egg & mixed veg all in one bowl",
     "price": 50.00, "category": "Signature", "image": "/IMG/menu_6.jpg", "available": True},
]


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


def admin_required(f):
    """Hard gate: must be authenticated AND role == admin."""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = _read_token()
        payload = verify_jwt_token(token) if token else None
        if not payload or payload.get('role') != 'admin':
            resp = jsonify({"success": False, "message": "Admin access required"})
            resp.status_code = 401 if not payload else 403
            resp.headers['X-Robots-Tag'] = 'noindex, nofollow'
            return resp
        request.user = payload
        return f(*args, **kwargs)
    return decorated


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


@app.route('/api/auth/google', methods=['POST'])
def google_login():
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    name = (data.get('name') or 'Google User').strip()
    firebase_uid = (data.get('firebase_uid') or '').strip()
    if not email:
        return jsonify({"success": False, "message": "Email is required"}), 400
    role = 'customer'

    if MONGODB_CONNECTED:
        user = users_collection.find_one({"email": email})
        if not user:
            new_user = {"email": email, "password": hash_password(os.urandom(16).hex()),
                        "name": name, "role": role, "provider": "google",
                        "firebase_uid": firebase_uid,
                        "created_at": datetime.now().isoformat()}
            result = users_collection.insert_one(new_user)
            new_user["_id"] = result.inserted_id
            user = new_user
        else:
            updates = {}
            if name and user.get("name") != name:
                updates["name"] = name
            if firebase_uid and user.get("firebase_uid") != firebase_uid:
                updates["firebase_uid"] = firebase_uid
            if updates:
                users_collection.update_one({"_id": user["_id"]}, {"$set": updates})
                user.update(updates)
    else:
        user = next((u for u in users_db if u["email"] == email), None)
        if not user:
            user = {"_id": f"usr_{len(users_db) + 1}", "email": email,
                    "password": hash_password(os.urandom(16).hex()),
                    "name": name, "role": role, "provider": "google",
                    "firebase_uid": firebase_uid,
                    "created_at": datetime.now().isoformat()}
            users_db.append(user)
        else:
            if name: user["name"] = name
            if firebase_uid: user["firebase_uid"] = firebase_uid

    token = create_jwt_token(user['_id'], user['email'], user.get('role', role))
    return jsonify({
        "success": True, "token": token,
        "user": {"id": str(user['_id']), "email": user['email'],
                 "name": user.get('name', 'Google User'),
                 "role": user.get('role', role)},
    }), 200


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
        return jsonify({"success": False, "message": "Admin UI not available"}), 500
    resp = send_from_directory(app.root_path, 'admin.html')
    resp.headers['X-Robots-Tag'] = 'noindex, nofollow'
    return resp


@app.route('/admin-login')
def admin_login_page():
    try:
        idx_path = os.path.join(app.root_path, 'index.html')
        with open(idx_path, 'r', encoding='utf-8') as f:
            html = f.read()
        if '<meta name="robots"' not in html:
            html = html.replace('</head>', '  <meta name="robots" content="noindex,nofollow">\n</head>')
        resp = make_response(html)
        resp.headers['Content-Type'] = 'text/html; charset=utf-8'
        resp.headers['X-Robots-Tag'] = 'noindex, nofollow'
        return resp
    except Exception:
        return send_from_directory('.', 'index.html')


@app.route('/meals.json')
def meals_file():
    return send_from_directory('.', 'meals.json')


# ============================================================================
# MENU
# ============================================================================

@app.route('/api/menu', methods=['GET'])
def get_menu():
    try:
        meals_path = os.path.join(app.root_path, 'meals.json')
        if os.path.exists(meals_path):
            with open(meals_path, 'r', encoding='utf-8') as f:
                meals = json.load(f)
            items = []
            for idx, meal in enumerate(meals, start=1):
                items.append({
                    "id": meal.get("id", idx),
                    "name": meal.get("name", f"Item {idx}"),
                    "description": meal.get("description", ""),
                    "price": float(meal.get("price", 0)),
                    "category": meal.get("category", "Signature"),
                    "image": meal.get("image", "/IMG/menu_1.jpg"),
                    "available": bool(meal.get("available", True)),
                })
            return jsonify({"success": True, "items": items})
    except Exception as e:
        print(f"Warning: failed to read meals.json ({e}). Using default menu.")
    return jsonify({"success": True, "items": MENU_ITEMS})


# ============================================================================
# ORDERS
# ============================================================================

def _coerce_items(raw):
    """Coerce items into [{name, quantity, price}, ...]. Drop garbage entries."""
    items = []
    if not isinstance(raw, list):
        return items
    for it in raw:
        if not isinstance(it, dict):
            continue
        try:
            items.append({
                "id": it.get("id"),
                "name": str(it.get("name") or "Item"),
                "quantity": int(it.get("quantity") or 1),
                "price": float(it.get("price") or 0),
            })
        except (TypeError, ValueError):
            continue
    return items


@app.route('/api/orders', methods=['POST'])
def create_order():
    """Public — customers may place orders without an account."""
    data = request.get_json(silent=True) or {}

    if MONGODB_CONNECTED:
        counter_doc = order_counter_collection.find_one_and_update(
            {"_id": "order_id"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        order_number = counter_doc["seq"]
    else:
        order_number = order_counter[0]
        order_counter[0] += 1

    customer_name = (data.get('customer_name') or data.get('customerName') or 'Guest').strip()
    customer_phone = (data.get('customer_phone') or data.get('phone_number')
                      or data.get('phoneNumber') or data.get('customerPhone') or '').strip()
    customer_location = (data.get('customer_location') or data.get('delivery_location')
                         or data.get('deliveryLocation') or data.get('customerLocation') or '').strip()

    items = _coerce_items(data.get('items') or data.get('cartItems') or [])
    if not items:
        return jsonify({"success": False, "message": "Cart is empty"}), 400

    # Always compute total server-side; ignore any client-supplied total
    total = round(sum(it['price'] * it['quantity'] for it in items), 2)

    order = {
        "id": f"SCM-{order_number:04d}",
        "customer_name": customer_name,
        "customer_phone": customer_phone,
        "customer_location": customer_location,
        "items": items,
        "total": total,
        "status": "Confirmed",
        "timestamp": datetime.now().isoformat(),
        "date": date.today().isoformat(),
    }

    if MONGODB_CONNECTED:
        try:
            result = orders_collection.insert_one(order)
            order["_id"] = str(result.inserted_id)
        except Exception:
            app.logger.exception("Error inserting order into MongoDB")
            return jsonify({"success": False, "message": "Failed to save order"}), 500
    else:
        orders_db.append(order)

    return jsonify({
        "success": True,
        "message": "Order placed successfully",
        "order": {
            "id": order["id"],
            "total": order["total"],
            "status": order["status"],
            "created_at": order.get("timestamp"),
            "date": order.get("date"),
        },
    }), 201


@app.route('/api/orders', methods=['GET'])
@admin_required
def get_orders():
    """Admin-only: lists all orders (contains PII)."""
    date_filter = request.args.get('date')
    if MONGODB_CONNECTED:
        query = {"date": date_filter} if date_filter else {}
        orders = list(orders_collection.find(query))
        for o in orders:
            o["_id"] = str(o["_id"])
    else:
        orders = ([o for o in orders_db if o['date'] == date_filter]
                  if date_filter else list(orders_db))
    return jsonify({"success": True, "orders": orders})


@app.route('/api/my-orders', methods=['GET'])
def my_orders():
    """
    Customer-facing order history lookup by phone.
    NOTE: frontend uses ?phone=<customerPhone>
    """
    # FRONTEND CONTRACT: use `phone` query param (matches index.html)
    phone = (request.args.get('phone') or '').strip()
    if not phone:
        return jsonify({"success": False, "message": "Phone is required"}), 400

    if MONGODB_CONNECTED:
        query = {"customer_phone": phone}
        orders = list(orders_collection.find(query))
        for o in orders:
            o["_id"] = str(o["_id"])
    else:
        orders = [o for o in orders_db if o.get('customer_phone') == phone]

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


VALID_STATUSES = {"Confirmed", "Preparing", "Ready", "Delivered", "Cancelled"}


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
        return jsonify({"success": True, "order": result})

    for order in orders_db:
        if order['id'] == order_id:
            order['status'] = new_status
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

    return jsonify({
        "success": True,
        "date": date_filter,
        "total_revenue": round(sum(_safe_total(o) for o in day_orders), 2),
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
    return jsonify({
        "success": True,
        "total_revenue": round(sum(_safe_total(o) for o in all_orders), 2),
        "total_orders": len(all_orders),
        "food_sold": _tally_food(all_orders),
    })


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

if __name__ == '__main__':
    app.run(host="0.0.0.0", debug=False, use_reloader=False, port=int(os.getenv('PORT', 5000)))
