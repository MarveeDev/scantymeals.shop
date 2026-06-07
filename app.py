
import flask
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from datetime import datetime, date, timedelta
from pymongo import MongoClient
from pymongo import ReturnDocument
from bson.objectid import ObjectId
import json
import os
import jwt
import bcrypt
import base64
from functools import wraps

app = Flask(__name__, static_folder='.')
CORS(app)

def hash_password(password: str) -> str:
    """
    Hash password with bcrypt and store as base64 string (safe for DB/json).

    bcrypt.hashpw() returns bytes that are not reliably decodable as UTF-8.
    Using base64 ensures we can round-trip the exact hash bytes.
    """
    hashed_bytes = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
    return base64.b64encode(hashed_bytes).decode('ascii')

def verify_password(password: str, hash_val: str) -> bool:
    """
    Verify bcrypt password.

    Supports BOTH:
    1) New hashes stored as base64 strings (recommended)
    2) Legacy hashes stored as utf-8 decoded strings (old broken behavior)

    This is required so existing Mongo users created before the fix can still log in.
    """
    if not hash_val:
        return False

    # 1) Try base64 format
    try:
        hashed_bytes = base64.b64decode(hash_val.encode('ascii'))
        if bcrypt.checkpw(password.encode('utf-8'), hashed_bytes):
            return True
    except Exception:
        pass

    # 2) Fallback to legacy utf-8 string -> bytes
    try:
        return bcrypt.checkpw(password.encode('utf-8'), hash_val.encode('utf-8'))
    except Exception:
        return False

# Prevent browser caching of index.html and static files
@app.after_request
def add_header(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, post-check=0, pre-check=0, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '-1'
    return response

# JWT Secret Key
JWT_SECRET = os.getenv('JWT_SECRET', 'your-secret-key-change-in-production')
JWT_ALGORITHM = 'HS256'

# MongoDB connection
MONGO_URI = os.getenv('MONGODB_URI', 'mongodb://localhost:27017/')
try:
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    # Test connection
    mongo_client.admin.command('ping')
    # Use the 'scantymeals' database in MongoDB Atlas as requested
    db = mongo_client['scantymeals']
    orders_collection = db['orders']
    order_counter_collection = db['counters']
    users_collection = db['users']
    MONGODB_CONNECTED = True
except Exception as e:
    print(f"Warning: MongoDB connection failed ({e}). Using in-memory storage.")
    MONGODB_CONNECTED = False
    orders_db = []
    order_counter = [1]
    
    # In-memory users for demo
    users_db = [
        {
            "_id": "admin_mem",
            "email": "admin@scanty.com",
            "password": hash_password("admin123"),
            "name": "Admin User",
            "role": "admin"
        },
        {
            "_id": "cust_mem",
            "email": "customer@scanty.com",
            "password": hash_password("customer123"),
            "name": "Demo Customer",
            "role": "customer"
        }
    ]

MENU_ITEMS = [
    {
        "id": 1,
        "name": "Classic Fried Rice",
        "description": "Golden wok-tossed rice with egg, spring onion, soy sauce & sesame oil",
        "price": 25.00,
        "category": "Signature",
        "image": "/IMG/menu_1.jpg",
        "available": True
    },
    {
        "id": 2,
        "name": "Spicy Jollof Fried Rice",
        "description": "West African style fried rice loaded with peppers & aromatic spices",
        "price": 30.00,
        "category": "Spicy",
        "image": "/IMG/menu_2.jpg",
        "available": True
    },
    {
        "id": 3,
        "name": "Chicken Fried Rice",
        "description": "Succulent grilled chicken strips over smoky wok-fried rice",
        "price": 35.00,
        "category": "Protein",
        "image": "/IMG/menu_3.jpg",
        "available": True
    },
    {
        "id": 4,
        "name": "Shrimp Fried Rice",
        "description": "Plump tiger shrimps with garlic butter tossed in fragrant fried rice",
        "price": 42.00,
        "category": "Seafood",
        "image": "/IMG/menu_4.jpg",
        "available": True
    },
    {
        "id": 5,
        "name": "Veggie Fried Rice",
        "description": "Fresh garden vegetables, tofu & cashews in light soy fried rice",
        "price": 22.00,
        "category": "Vegetarian",
        "image": "/IMG/menu_5.jpg",
        "available": True
    },
    {
        "id": 6,
        "name": "Special Mixed Fried Rice",
        "description": "A generous feast: chicken, shrimp, egg & mixed veg all in one bowl",
        "price": 50.00,
        "category": "Signature",
        "image": "/IMG/menu_6.jpg",
        "available": True
    }
]

# ==================== Authentication Functions ====================

def create_jwt_token(user_id, email, role, expires_in=24):
    """Create JWT token"""
    payload = {
        'user_id': str(user_id),
        'email': email,
        'role': role,
        'exp': datetime.utcnow() + timedelta(hours=expires_in),
        'iat': datetime.utcnow()
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def verify_jwt_token(token):
    """Verify and decode JWT token"""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None

def token_required(f):
    """Decorator for routes that require JWT token"""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        
        if 'Authorization' in request.headers:
            auth_header = request.headers['Authorization']
            try:
                token = auth_header.split(" ")[1]
            except IndexError:
                # Invalid token format — treat as anonymous rather than failing
                token = None
        # If no token provided or verification fails, treat the request as anonymous.
        if token:
            payload = verify_jwt_token(token)
            if payload:
                request.user = payload
                return f(*args, **kwargs)

        # Anonymous fallback: set a harmless guest user object so handlers can continue.
        request.user = { 'user_id': 'anonymous', 'email': '', 'role': 'customer' }
        return f(*args, **kwargs)
    
    return decorated

def admin_required(f):
    """Decorator for routes that require admin role"""
    @wraps(f)
    def decorated(*args, **kwargs):
        # With authentication removed, do not enforce admin checks.
        # If a previous middleware set a user, keep it; otherwise default to admin-like access.
        if not hasattr(request, 'user'):
            request.user = { 'user_id': 'anonymous', 'email': '', 'role': 'admin' }
        return f(*args, **kwargs)

    return decorated

def normalize_phone(phone_raw):
    """Normalize phone to +<digits> format with a basic Ghana-friendly fallback."""
    if not phone_raw:
        return None

    digits = ''.join(ch for ch in str(phone_raw) if ch.isdigit())
    if not digits:
        return None

    # Common local format: 0XXXXXXXXX -> +233XXXXXXXXX
    if len(digits) == 10 and digits.startswith('0'):
        digits = '233' + digits[1:]

    # Accept explicit country code or long enough international-ish numbers
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
            "name": display_name,
            "role": "customer",
            "phone": phone,
            "provider": "phone_otp",
            "created_at": datetime.now().isoformat()
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
        "name": display_name,
        "role": "customer",
        "phone": phone,
        "provider": "phone_otp",
        "created_at": datetime.now().isoformat()
    }
    users_db.append(user)
    return user

# ==================== Auth Routes ====================

@app.route('/api/auth/register', methods=['POST'])
def register():
    """Registration disabled by administrator."""
    return jsonify({"success": False, "message": "Registration is disabled"}), 403

@app.route('/api/auth/login', methods=['POST'])
def login():
    """Login user and return JWT token"""
    data = request.json
    email = data.get('email')
    password = data.get('password')
    
    if not email or not password:
        return jsonify({"success": False, "message": "Email and password are required"}), 400
    
    if MONGODB_CONNECTED:
        user = users_collection.find_one({"email": email})
    else:
        user = next((u for u in users_db if u["email"] == email), None)

    # Return a handled application-level failure instead of HTTP 401 here
    # so browser consoles do not surface noisy Unauthorized network errors
    # during normal login mistakes.
    if not user or not user.get('password') or not verify_password(password, user['password']):
        return jsonify({"success": False, "message": "Invalid credentials"}), 401
    
    token = create_jwt_token(user['_id'], user['email'], user['role'])
    return jsonify({
        "success": True,
        "token": token,
        "user": {
            "id": str(user['_id']),
            "email": user['email'],
            "name": user['name'],
            "role": user['role']
        }
    }), 200

@app.route('/api/auth/google', methods=['POST'])
def google_login():
    """Login or register user with Google account details."""
    data = request.json or {}
    email = (data.get('email') or '').strip().lower()
    name = (data.get('name') or 'Google User').strip()
    firebase_uid = (data.get('firebase_uid') or '').strip()

    if not email:
        return jsonify({"success": False, "message": "Email is required"}), 400

    role = 'customer'

    if MONGODB_CONNECTED:
        user = users_collection.find_one({"email": email})
        if not user:
            new_user = {
                "email": email,
                "password": hash_password(os.urandom(16).hex()),
                "name": name,
                "role": role,
                "provider": "google",
                "firebase_uid": firebase_uid,
                "created_at": datetime.now().isoformat()
            }
            result = users_collection.insert_one(new_user)
            new_user["_id"] = result.inserted_id
            user = new_user
        else:
            update_fields = {}
            if name and user.get("name") != name:
                update_fields["name"] = name
            if firebase_uid and user.get("firebase_uid") != firebase_uid:
                update_fields["firebase_uid"] = firebase_uid
            if update_fields:
                users_collection.update_one({"_id": user["_id"]}, {"$set": update_fields})
                user.update(update_fields)
    else:
        user = next((u for u in users_db if u["email"] == email), None)
        if not user:
            user = {
                "_id": f"usr_{len(users_db) + 1}",
                "email": email,
                "password": hash_password(os.urandom(16).hex()),
                "name": name,
                "role": role,
                "provider": "google",
                "firebase_uid": firebase_uid,
                "created_at": datetime.now().isoformat()
            }
            users_db.append(user)
        else:
            if name:
                user["name"] = name
            if firebase_uid:
                user["firebase_uid"] = firebase_uid

    token = create_jwt_token(user['_id'], user['email'], user.get('role', role))
    return jsonify({
        "success": True,
        "token": token,
        "user": {
            "id": str(user['_id']),
            "email": user['email'],
            "name": user.get('name', 'Google User'),
            "role": user.get('role', role)
        }
    }), 200

@app.route('/api/auth/phone-name', methods=['POST'])
def phone_name_login():
    """Login/register user with phone number + name (no OTP, no Firebase)."""
    data = request.json or {}
    phone = normalize_phone(data.get('phone'))
    name = (data.get('name') or '').strip()

    if not phone:
        return jsonify({"success": False, "message": "Phone is required"}), 400

    user = get_or_create_user_by_phone(phone, name)

    # Ensure provider is recorded consistently (even for in-memory path).
    if MONGODB_CONNECTED:
        users_collection.update_one(
            {"_id": user["_id"]},
            {"$set": {"provider": "phone_name"}}
        )

    token = create_jwt_token(user['_id'], user.get('email', ''), user.get('role', 'customer'))
    return jsonify({
        "success": True,
        "token": token,
        "user": {
            "id": str(user['_id']),
            "email": user.get('email', ''),
            "name": user.get('name', f"Customer {phone[-4:]}"),
            "role": user.get('role', 'customer'),
            "phone": phone
        }
    }), 200


@app.route('/api/auth/firebase-phone', methods=['POST'])
def firebase_phone_login():
    """Create app session after Firebase phone verification on the client."""
    data = request.json or {}
    phone = normalize_phone(data.get('phone'))
    firebase_uid = (data.get('firebase_uid') or '').strip()
    name = (data.get('name') or '').strip()

    if not phone or not firebase_uid:
        return jsonify({"success": False, "message": "Phone and firebase_uid are required"}), 400

    user = get_or_create_user_by_phone(phone, name)

    if MONGODB_CONNECTED:
        users_collection.update_one(
            {"_id": user["_id"]},
            {"$set": {"firebase_uid": firebase_uid, "provider": "firebase_phone"}}
        )
        user["firebase_uid"] = firebase_uid
        user["provider"] = "firebase_phone"
    else:
        user["firebase_uid"] = firebase_uid
        user["provider"] = "firebase_phone"

    token = create_jwt_token(user['_id'], user.get('email', ''), user.get('role', 'customer'))
    return jsonify({
        "success": True,
        "token": token,
        "user": {
            "id": str(user['_id']),
            "email": user.get('email', ''),
            "name": user.get('name', f"Customer {phone[-4:]}"),
            "role": user.get('role', 'customer'),
            "phone": phone
        }
    }), 200

@app.route('/api/auth/profile', methods=['GET'])
@token_required
def get_profile():
    """Get current user profile"""
    if MONGODB_CONNECTED:
        user = users_collection.find_one({"_id": ObjectId(request.user['user_id'])})
    else:
        user = next((u for u in users_db if str(u["_id"]) == request.user['user_id']), None)

    if user:
        return jsonify({
            "success": True,
            "user": {
                "id": str(user['_id']),
                "email": user['email'],
                "name": user['name'],
                "role": user['role']
            }
        }), 200
    return jsonify({"success": False, "message": "User not found"}), 404

# ==================== Main Routes ====================

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

@app.route('/meals.json')
def meals_file():
    return send_from_directory('.', 'meals.json')

@app.route('/api/menu', methods=['GET'])
def get_menu():
    # Keep menu publicly readable so first-time visitors can browse immediately.
    # If meals.json exists, use it as the source of truth so frontend/backend stay in sync.
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
                    "available": bool(meal.get("available", True))
                })
            return jsonify({"success": True, "items": items})
    except Exception as e:
        print(f"Warning: failed to read meals.json ({e}). Using default menu.")

    return jsonify({"success": True, "items": MENU_ITEMS})

@app.route('/api/orders', methods=['POST'])

def create_order():
    data = request.json
    
    if MONGODB_CONNECTED:
        # Get next order counter from MongoDB
        counter_doc = order_counter_collection.find_one_and_update(
            {"_id": "order_id"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER
        )
        order_number = counter_doc["seq"]
    else:
        order_number = order_counter[0]
        order_counter[0] += 1
    
    order = {
        "id": f"SCM-{order_number:04d}",
        "customer_name": data.get("customer_name", "Guest"),
        "customer_phone": data.get("customer_phone", ""),
        "customer_location": data.get("customer_location", ""),
        "items": data.get("items", []),
        "total": data.get("total", 0),
        "status": "Confirmed",
        "timestamp": datetime.now().isoformat(),
        "date": date.today().isoformat()
    }
    
    if MONGODB_CONNECTED:
        result = orders_collection.insert_one(order)
        order["_id"] = str(result.inserted_id)
        print(f"Order saved to MongoDB with _id={order['_id']}")
    else:
        orders_db.append(order)
        print(f"Order saved to in-memory store with id={order['id']}")
    
    # Return a clear success message for the frontend
    return jsonify({"success": True, "message": "Order placed successfully", "order": order}), 201

@app.route('/api/orders', methods=['GET'])

def get_orders():
    date_filter = request.args.get('date')
    
    if MONGODB_CONNECTED:
        query = {}
        if date_filter:
            query = {"date": date_filter}
        orders = list(orders_collection.find(query))
        # Convert ObjectId to string for JSON serialization
        for order in orders:
            order["_id"] = str(order["_id"])
    else:
        if date_filter:
            orders = [o for o in orders_db if o['date'] == date_filter]
        else:
            orders = orders_db
    
    return jsonify({"success": True, "orders": orders})


@app.route('/api/orders/<order_id>/response', methods=['POST'])
def add_order_response(order_id):
    """Add a customer response/message to an order."""
    data = request.json or {}
    customer_name = data.get('customer_name', '').strip()
    customer_phone = data.get('customer_phone', '').strip()
    message = (data.get('message') or '').strip()

    if not message:
        return jsonify({"success": False, "message": "Message is required"}), 400

    response_obj = {
        'id': str(ObjectId()) if MONGODB_CONNECTED else f"resp_{int(datetime.utcnow().timestamp())}",
        'customer_name': customer_name,
        'customer_phone': customer_phone,
        'message': message,
        'timestamp': datetime.utcnow().isoformat()
    }

    if MONGODB_CONNECTED:
        result = orders_collection.find_one_and_update(
            { 'id': order_id },
            { '$push': { 'responses': response_obj } },
            return_document=ReturnDocument.AFTER
        )
        if not result:
            return jsonify({"success": False, "message": "Order not found"}), 404
        # Convert ObjectId to string if present
        result['_id'] = str(result['_id'])
        return jsonify({"success": True, "order": result, "response": response_obj}), 201

    # In-memory path
    for order in orders_db:
        if order.get('id') == order_id:
            if 'responses' not in order:
                order['responses'] = []
            order['responses'].append(response_obj)
            return jsonify({"success": True, "order": order, "response": response_obj}), 201

    return jsonify({"success": False, "message": "Order not found"}), 404


@app.route('/api/orders/<order_id>/responses', methods=['GET'])
def list_order_responses(order_id):
    """Return customer responses attached to an order."""
    if MONGODB_CONNECTED:
        order = orders_collection.find_one({ 'id': order_id })
        if not order:
            return jsonify({"success": False, "message": "Order not found"}), 404
        responses = order.get('responses', [])
        return jsonify({"success": True, "responses": responses})

    for order in orders_db:
        if order.get('id') == order_id:
            return jsonify({"success": True, "responses": order.get('responses', [])})

    return jsonify({"success": False, "message": "Order not found"}), 404

@app.route('/api/orders/<order_id>/status', methods=['PUT'])
@admin_required
def update_order_status(order_id):
    data = request.json
    
    if MONGODB_CONNECTED:
        result = orders_collection.find_one_and_update(
            {"id": order_id},
            {"$set": {"status": data.get('status')}},
            return_document=True
        )
        if result:
            result["_id"] = str(result["_id"])
            return jsonify({"success": True, "order": result})
        return jsonify({"success": False, "message": "Order not found"}), 404
    else:
        for order in orders_db:
            if order['id'] == order_id:
                order['status'] = data.get('status', order['status'])
                return jsonify({"success": True, "order": order})
        return jsonify({"success": False, "message": "Order not found"}), 404

@app.route('/api/analytics/daily', methods=['GET'])
@admin_required
def daily_analytics():
    today = date.today().isoformat()
    date_filter = request.args.get('date', today)
    
    if MONGODB_CONNECTED:
        day_orders = list(orders_collection.find({"date": date_filter}))
        for order in day_orders:
            order["_id"] = str(order["_id"])
    else:
        day_orders = [o for o in orders_db if o['date'] == date_filter]

    total_revenue = sum(o['total'] for o in day_orders)
    total_orders = len(day_orders)

    # Count food items sold
    food_sold = {}
    for order in day_orders:
        for item in order['items']:
            name = item['name']
            qty = item['quantity']
            food_sold[name] = food_sold.get(name, 0) + qty

    return jsonify({
        "success": True,
        "date": date_filter,
        "total_revenue": total_revenue,
        "total_orders": total_orders,
        "food_sold": food_sold,
        "orders": day_orders
    })

@app.route('/api/analytics/summary', methods=['GET'])
@admin_required
def analytics_summary():
    if MONGODB_CONNECTED:
        all_orders = list(orders_collection.find({}))
    else:
        all_orders = orders_db
    
    total_revenue = sum(o['total'] for o in all_orders)
    total_orders = len(all_orders)
    food_sold = {}
    for order in all_orders:
        for item in order['items']:
            name = item['name']
            qty = item['quantity']
            food_sold[name] = food_sold.get(name, 0) + qty

    return jsonify({
        "success": True,
        "total_revenue": total_revenue,
        "total_orders": total_orders,
        "food_sold": food_sold
    })

if __name__ == '__main__':
    # Disable debug/reloader to prevent auto-restarts.
    # Also bind to 0.0.0.0 for common PaaS/container setups.
    app.run(host="0.0.0.0", debug=False, use_reloader=False, port=5000)
