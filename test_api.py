import time
import urllib.request
import json

start = time.time()
req = urllib.request.Request('http://127.0.0.1:5000/api/auth/login', data=json.dumps({"email": "admin@scanty.com", "password": "admin123"}).encode('utf-8'), headers={'Content-Type': 'application/json'})
try:
    res = urllib.request.urlopen(req)
    data = json.loads(res.read())
    token = data['token']
    print(f"Login took: {time.time() - start} seconds")

    start = time.time()
    req2 = urllib.request.Request('http://127.0.0.1:5000/api/menu', headers={'Authorization': f'Bearer {token}'})
    res2 = urllib.request.urlopen(req2)
    print(f"Menu fetch took: {time.time() - start} seconds")
    print("Menu fetched.")
except Exception as e:
    print(f"Error: {e}")
