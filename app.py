
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
from functools import wraps

app = Flask(__name__, static_folder='.')
CORS(app)

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
    db = mongo_client['scanty_pro']
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

def hash_password(password):
    """Hash password using bcrypt"""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(password, hash_val):
    """Verify password against hash"""
    return bcrypt.checkpw(password.encode('utf-8'), hash_val.encode('utf-8'))

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

    if not user or not verify_password(password, user['password']):
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

@app.route('/api/menu', methods=['GET'])
@token_required
def get_menu():
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
    app.run(debug=True, port=5000)
