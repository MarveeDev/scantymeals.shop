FROM python:3.10-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Copy application files
COPY app.py .
COPY sms_provider.py .
COPY index.html .
COPY admin.html .
COPY admin-login.html .
COPY staff-login.html .
COPY staff.html .
COPY scanty-theme.css .
COPY meals.json .
COPY IMG ./IMG/

# Run with Gunicorn on port 8080 (Cloud Run default).
# Single eventlet worker keeps Socket.IO sessions in one process (no
# cross-worker "Invalid session" loss). The WSGI entrypoint is the Flask
# `app` (SocketIO wraps it via middleware; the SocketIO object itself is
# not a WSGI callable in Flask-SocketIO 5.x).
ENV PORT=8080
CMD ["gunicorn", "--worker-class", "eventlet", "--workers", "1", "--bind", "0.0.0.0:8080", "app:app"]
