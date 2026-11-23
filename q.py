"""Unusual Whales GET helper.

The key is read from the environment (UW_API_KEY) inside the request, never at
import time, and it is never printed or written to a file.
"""
import os
import re

API = "https://api.unusualwhales.com/api/"
_SECRET = re.compile(r"Bearer\s+\S+|UW_API_KEY=\S+")


def scrub(text):
    return _SECRET.sub("[redacted]", str(text))[:300]


def g(path, **params):
    key = os.environ.get("UW_API_KEY")
    if not key:
        raise RuntimeError("UW_API_KEY is not set")
    import requests
    headers = {
        "Authorization": "Bearer " + key,
        "UW-CLIENT-API-ID": "100004",
        "User-Agent": "uw-unusual-flow-filter/1.0",
    }
    r = requests.get(API + path.lstrip("/"), headers=headers, params=params, timeout=30)
    if not r.ok:
        return r.status_code, scrub(r.text[:300])
    return r.status_code, r.json()
