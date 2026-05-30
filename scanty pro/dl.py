import json
import urllib.request
import os
from concurrent.futures import ThreadPoolExecutor

urls = [
    "https://images.unsplash.com/photo-1603133872878-684f208fb84b?w=500&q=80",
    "https://images.unsplash.com/photo-1585032226651-759b368d7246?w=500&q=80",
    "https://images.unsplash.com/photo-1512058564366-18510be2db19?w=500&q=80",
    "https://images.unsplash.com/photo-1563379091339-03b21ab4a4f8?w=500&q=80",
    "https://images.unsplash.com/photo-1546069901-ba9599a7e63c?w=500&q=80",
    "https://images.unsplash.com/photo-1569050467447-ce54b3bbc37d?w=500&q=80"
]

def download_image(url, filename):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        res = urllib.request.urlopen(req)
        with open(filename, 'wb') as f:
            f.write(res.read())
        print(f"Downloaded {filename}")
    except Exception as e:
        print(f"Failed {filename}: {e}")

if not os.path.exists("IMG"):
    os.makedirs("IMG")

with ThreadPoolExecutor(max_workers=6) as executor:
    for i, url in enumerate(urls, 1):
        executor.submit(download_image, url, f"IMG/menu_{i}.jpg")
