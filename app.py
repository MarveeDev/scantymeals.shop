
import flask
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from datetime import datetime, date, timedelta
from pymongo import MongoClient
from pymongo import ReturnDocument
from bson.objectid import ObjectId
import json
import os
import random
import jwt
import bcrypt
import base64
from functools import wraps

try:
    import requests
except Exception:
    requests = None

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
OTP_EXPIRY_MINUTES = int(os.getenv('OTP_EXPIRY_MINUTES', '5'))
DEBUG_OTP = os.getenv('DEBUG_OTP', 'true').lower() == 'true'
HUBTEL_CLIENT_ID = os.getenv('HUBTEL_CLIENT_ID', '').strip()
HUBTEL_CLIENT_SECRET = os.getenv('HUBTEL_CLIENT_SECRET', '').strip()
HUBTEL_SENDER_ID = os.getenv('HUBTEL_SENDER_ID', 'ScantyMeals').strip()
HUBTEL_COUNTRY_CODE = os.getenv('HUBTEL_COUNTRY_CODE', 'GH').strip()
HUBTEL_BASE_URL = os.getenv('HUBTEL_BASE_URL', '').strip()

# MongoDB connection
MONGO_URI = os.getenv('MONGODB_URI', 'mongodb://localhost:27017/')
try:
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    # Test connection
    mongo_client.admin.command('ping')
    db = mongo_client['scanty_pro']
    orders_collection = db['orders']
    order_counter_collection = db['counters']
    users_collection = db['users']
    otp_codes_collection = db['otp_codes']
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
    otp_codes_db = []

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
                return jsonify({"success": False, "message": "Invalid token format"}), 401
        
        if not token:
            return jsonify({"success": False, "message": "Token is missing"}), 401
        
        payload = verify_jwt_token(token)
        if not payload:
            return jsonify({"success": False, "message": "Invalid or expired token"}), 401
        
        request.user = payload
        return f(*args, **kwargs)
    
    return decorated

def admin_required(f):
    """Decorator for routes that require admin role"""
    @wraps(f)
    @token_required
    def decorated(*args, **kwargs):
        if request.user.get('role') != 'admin':
            return jsonify({"success": False, "message": "Admin access required"}), 403
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

def generate_otp():
    return f"{random.randint(0, 999999):06d}"

def hubtel_configured():
    return bool(HUBTEL_CLIENT_ID and HUBTEL_CLIENT_SECRET and requests is not None)

def _hubtel_base_candidates():
    candidates = []
    if HUBTEL_BASE_URL:
        candidates.append(HUBTEL_BASE_URL.rstrip('/'))
    # Common Hubtel API base URLs (can be overridden with HUBTEL_BASE_URL)
    candidates.extend([
        "https://smsc.hubtel.com/v1",
        "https://devp-sms03726-api.hubtel.com/v1"
    ])
    # Keep ordering while removing duplicates
    ordered = []
    for base in candidates:
        if base and base not in ordered:
            ordered.append(base)
    return ordered

def hubtel_send_otp(phone):
    payload = {
        "senderId": HUBTEL_SENDER_ID,
        "phoneNumber": phone,
        "countryCode": HUBTEL_COUNTRY_CODE
    }

    last_error = "Hubtel OTP request failed"
    for base_url in _hubtel_base_candidates():
        url = f"{base_url}/otp/send"
        try:
            res = requests.post(
                url,
                json=payload,
                auth=(HUBTEL_CLIENT_ID, HUBTEL_CLIENT_SECRET),
                timeout=15
            )
            data = res.json() if res.headers.get("content-type", "").startswith("application/json") else {}

            if res.status_code == 200:
                return True, data, None

            last_error = data.get("message") or f"Hubtel send OTP failed ({res.status_code})"
        except Exception as exc:
            last_error = f"Hubtel send OTP error: {str(exc)}"

    return False, None, last_error

def hubtel_verify_otp(request_id, prefix, otp_code):
    payload = {
        "requestId": request_id,
        "prefix": prefix,
        "code": otp_code
    }

    last_error = "Hubtel OTP verification failed"
    for base_url in _hubtel_base_candidates():
        url = f"{base_url}/otp/verify"
        try:
            res = requests.post(
                url,
                json=payload,
                auth=(HUBTEL_CLIENT_ID, HUBTEL_CLIENT_SECRET),
                timeout=15
            )
            data = res.json() if res.headers.get("content-type", "").startswith("application/json") else {}

            if res.status_code == 200:
                status = str(data.get("status", "")).lower()
                # Some Hubtel responses include status in body; treat explicit failure as error.
                if status and status not in ("success", "ok", "verified", "completed"):
                    last_error = data.get("message") or "OTP could not be verified"
                    continue
                return True, data, None

            last_error = data.get("message") or f"Hubtel verify OTP failed ({res.status_code})"
        except Exception as exc:
            last_error = f"Hubtel verify OTP error: {str(exc)}"

    return False, None, last_error

def save_otp_code(phone, otp_code):
    expires_at = datetime.utcnow() + timedelta(minutes=OTP_EXPIRY_MINUTES)
    if MONGODB_CONNECTED:
        otp_codes_collection.update_one(
            {"phone": phone},
            {"$set": {
                "phone": phone,
                "otp": otp_code,
                "used": False,
                "expires_at": expires_at,
                "created_at": datetime.utcnow().isoformat()
            }},
            upsert=True
        )
    else:
        otp_codes_db[:] = [o for o in otp_codes_db if o.get("phone") != phone]
        otp_codes_db.append({
            "phone": phone,
            "otp": otp_code,
            "used": False,
            "expires_at": expires_at
        })

def verify_and_consume_otp(phone, otp_code):
    now = datetime.utcnow()
    if MONGODB_CONNECTED:
        otp_doc = otp_codes_collection.find_one({
            "phone": phone,
            "otp": otp_code,
            "used": False,
            "expires_at": {"$gt": now}
        })
        if not otp_doc:
            return False
        otp_codes_collection.update_one({"_id": otp_doc["_id"]}, {"$set": {"used": True}})
        return True

    for otp_doc in otp_codes_db:
        if (
            otp_doc.get("phone") == phone and
            otp_doc.get("otp") == otp_code and
            not otp_doc.get("used") and
            otp_doc.get("expires_at") > now
        ):
            otp_doc["used"] = True
            return True
    return False

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
    """Register a new user"""
    data = request.json
    email = data.get('email')
    password = data.get('password')
    name = data.get('name')
    role = data.get('role', 'customer')  # default role is customer
    
    if not email or not password or not name:
        return jsonify({"success": False, "message": "Email, password, and name are required"}), 400
    
    if MONGODB_CONNECTED:
        # Check if user already exists
        existing_user = users_collection.find_one({"email": email})
        if existing_user:
            return jsonify({"success": False, "message": "Email already registered"}), 400
        
        # Create new user
        new_user = {
            "email": email,
            "password": hash_password(password),
            "name": name,
            "role": role,
            "created_at": datetime.now().isoformat()
        }
        result = users_collection.insert_one(new_user)
        user_id = str(result.inserted_id)
    else:
        # Fallback to in-memory
        existing_user = next((u for u in users_db if u["email"] == email), None)
        if existing_user:
            return jsonify({"success": False, "message": "Email already registered"}), 400
        
        new_user = {
            "_id": f"usr_{len(users_db) + 1}",
            "email": email,
            "password": hash_password(password),
            "name": name,
            "role": role,
            "created_at": datetime.now().isoformat()
        }
        users_db.append(new_user)
        user_id = new_user["_id"]

    return jsonify({
        "success": True,
        "message": "User registered successfully",
        "user_id": user_id
    }), 201

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

@app.route('/api/auth/request-otp', methods=['POST'])
def request_phone_otp():
    """Send OTP for phone login. Uses Hubtel when configured."""
    data = request.json or {}
    phone = normalize_phone(data.get('phone'))

    if not phone:
        return jsonify({"success": False, "message": "Valid phone number is required"}), 400

    if hubtel_configured():
        ok, hubtel_data, error_msg = hubtel_send_otp(phone)
        if not ok:
            return jsonify({
                "success": False,
                "message": error_msg or "Unable to send OTP via Hubtel"
            }), 502

        hubtel_payload = hubtel_data or {}
        hubtel_tokens = hubtel_payload.get("data", {}) if isinstance(hubtel_payload.get("data"), dict) else {}
        request_id = hubtel_tokens.get("requestId")
        prefix = hubtel_tokens.get("prefix")

        if not request_id or not prefix:
            return jsonify({
                "success": False,
                "message": "Hubtel OTP response missing verification tokens"
            }), 502

        return jsonify({
            "success": True,
            "message": hubtel_payload.get("message", "OTP sent successfully"),
            "phone": phone,
            "provider": "hubtel",
            "request_id": request_id,
            "prefix": prefix
        }), 200

    # Local fallback (dev/testing only)
    otp_code = generate_otp()
    save_otp_code(phone, otp_code)
    print(f"[OTP] phone={phone} code={otp_code}")

    response = {
        "success": True,
        "message": "OTP sent successfully",
        "phone": phone,
        "expires_in_minutes": OTP_EXPIRY_MINUTES,
        "provider": "local"
    }
    # For development/demo where no SMS provider is configured yet.
    if DEBUG_OTP:
        response["otp_preview"] = otp_code

    return jsonify(response), 200

@app.route('/api/auth/verify-otp', methods=['POST'])
def verify_phone_otp():
    """Verify OTP and issue JWT session."""
    data = request.json or {}
    phone = normalize_phone(data.get('phone'))
    otp_code = (data.get('otp') or '').strip()
    name = (data.get('name') or '').strip()
    request_id = (data.get('request_id') or '').strip()
    prefix = (data.get('prefix') or '').strip()

    if not phone or not otp_code:
        return jsonify({"success": False, "message": "Phone and OTP are required"}), 400

    verified = False

    if hubtel_configured():
        if not request_id or not prefix:
            return jsonify({
                "success": False,
                "message": "Missing Hubtel OTP verification tokens (request_id/prefix)"
            }), 400

        ok, _, error_msg = hubtel_verify_otp(request_id, prefix, otp_code)
        if not ok:
            return jsonify({
                "success": False,
                "message": error_msg or "Invalid or expired OTP"
            }), 401
        verified = True
    else:
        verified = verify_and_consume_otp(phone, otp_code)

    if not verified:
        return jsonify({"success": False, "message": "Invalid or expired OTP"}), 401

    user = get_or_create_user_by_phone(phone, name)
    token = create_jwt_token(user['_id'], user['email'], user.get('role', 'customer'))

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
@token_required
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
        "items": data.get("items", []),
        "total": data.get("total", 0),
        "status": "Confirmed",
        "timestamp": datetime.now().isoformat(),
        "date": date.today().isoformat()
    }
    
    if MONGODB_CONNECTED:
        result = orders_collection.insert_one(order)
        order["_id"] = str(result.inserted_id)
    else:
        orders_db.append(order)
    
    return jsonify({"success": True, "order": order}), 201

@app.route('/api/orders', methods=['GET'])
@token_required
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
