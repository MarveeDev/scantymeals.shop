# 🍚 Scanty Meals – Fried Rice Heaven

A full-stack restaurant web application for **Scanty Meals**, built with AngularJS + Tailwind CSS (frontend) and Python Flask (backend).

---

## 📁 Project Structure

```
scanty-meals/
├── frontend/
│   └── index.html          ← Full AngularJS + Tailwind SPA
└── backend/
    ├── app.py              ← Python Flask REST API
    └── requirements.txt    ← Python dependencies
```

---

## 🚀 Running the App

### Frontend (No build needed)
Just open `frontend/index.html` in any browser, or serve it:

```bash
cd frontend
python3 -m http.server 8080
# Visit: http://localhost:8080
```

### Backend (Python Flask)
```bash
cd backend
pip install -r requirements.txt
python app.py
# API runs on: http://localhost:5000
```

---

## 📱 Features

### 🍽 Menu (Customer View)
- Stunning splash screen with animated steam effect
- Visual fried rice menu with Unsplash food photography
- Category tags: Signature, Spicy, Protein, Seafood, Vegetarian
- Add to cart with live cart counter
- Cart panel with quantity controls

### 🛒 Ordering
- Cart sidebar with item management
- Customer name & phone collection
- Order placement with confirmation toast
- Auto-generated Order ID (e.g. SCM-0001)

### 📋 Order History
- Full list of placed orders
- Order status badges (Confirmed / Preparing / Ready / Delivered)
- Order timestamps and item breakdown

### ⚙️ Admin Dashboard
- Overview stats (total, confirmed, preparing, delivered)
- Searchable orders table
- Live status update dropdown per order
- Customer phone & name display

### 📊 Analytics
- **Daily Revenue** – total money earned per day
- **Food Sold Breakdown** – visual bar chart per menu item
- **Orders per Day** – filterable by date
- **All-Time Summary** – total revenue, orders, portions sold
- **Top Sellers Table** – with estimated revenue per item

---

## 🔌 API Endpoints (Flask)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/menu` | Fetch all menu items |
| POST | `/api/orders` | Place a new order |
| GET | `/api/orders` | Get all orders (filter by `?date=YYYY-MM-DD`) |
| PUT | `/api/orders/:id/status` | Update order status |
| GET | `/api/analytics/daily` | Daily food sold & revenue |
| GET | `/api/analytics/summary` | All-time summary |

---

## 🎨 Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend Framework | AngularJS 1.8 |
| CSS Framework | Tailwind CSS 3 |
| Fonts | Playfair Display + DM Sans |
| Images | Google/Unsplash fried rice photos |
| Backend | Python 3 + Flask |
| CORS | Flask-CORS |
| Routing | angular-route |

---

## 🖼 Module Structure (based on AgroFarm platform spec)

| AgroFarm Module | Scanty Meals Equivalent |
|-----------------|------------------------|
| Customer Mobile App | Customer Menu & Order Page |
| Product Management | Menu Management |
| Order Management | Admin Dashboard |
| Analytics & Reports | Analytics Page (Daily Sales) |
| Notification System | Toast Notifications |
| Payment System | Cart & Checkout Flow |
