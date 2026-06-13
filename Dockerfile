FROM python:3.10-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Copy application files
COPY app.py .
COPY index.html .
COPY admin.html .
COPY admin-login.html .
COPY meals.json .
COPY IMG ./IMG/

# Run with Gunicorn on port 8080 (Cloud Run default)
ENV PORT=8080
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "app:app"]
