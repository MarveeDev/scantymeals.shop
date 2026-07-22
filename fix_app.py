import codecs

with codecs.open('app.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix SocketIO async_mode
content = content.replace("socketio = SocketIO(app, cors_allowed_origins=ALLOWED_ORIGINS, async_mode='eventlet')", 
                          "socketio = SocketIO(app, cors_allowed_origins=ALLOWED_ORIGINS)")

# Add "Out for Delivery" status
content = content.replace('VALID_STATUSES = {"Confirmed", "Preparing", "Ready", "Delivered", "Cancelled"}', 
                          'VALID_STATUSES = {"Confirmed", "Preparing", "Ready", "Out for Delivery", "Delivered", "Cancelled"}')
content = content.replace('"Ready": "Your order is ready for pickup.",\n                "Delivered": "Your order has been delivered.",', 
                          '"Ready": "Your order is ready for pickup.",\n                "Out for Delivery": "Your order is out for delivery.",\n                "Delivered": "Your order has been delivered.",')
content = content.replace('"Ready": "Your order is ready for pickup.",\n                    "Delivered": "Your order has been delivered.",', 
                          '"Ready": "Your order is ready for pickup.",\n                    "Out for Delivery": "Your order is out for delivery.",\n                    "Delivered": "Your order has been delivered.",')

# Add POST /api/notifications
post_route = """@app.route('/api/notifications', methods=['POST'])
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

@app.route('/api/notifications', methods=['GET'])"""

content = content.replace("@app.route('/api/notifications', methods=['GET'])", post_route)

# Write to a new file so user can manually swap if locked
with codecs.open('app.py.new', 'w', encoding='utf-8') as f:
    f.write(content)

print("Created app.py.new successfully. Ready to replace.")
