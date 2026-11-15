"""Tokens, password hashing and request signing (standard library only)."""
import base64
import hashlib
import hmac
import os
import threading
import time


def new_token(prefix, nbytes=32):
    return prefix + "_" + base64.urlsafe_b64encode(os.urandom(nbytes)).decode().rstrip("=")


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def token_matches(token, token_hash):
    return hmac.compare_digest(hash_token(token), token_hash or "")


def hash_password(password):
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$" + salt.hex() + "$" + digest.hex()


def verify_password(password, stored):
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


def sign(key, payload):
    return hmac.new(key.encode(), payload, hashlib.sha256).hexdigest()


def verify_signature(key, payload, signature):
    return hmac.compare_digest(sign(key, payload), signature or "")


class RateLimiter:
    """Blocks a key for `lock_s` seconds after `max_fail` failures within `window_s`."""

    def __init__(self, max_fail=5, window_s=60, lock_s=60):
        self.max_fail, self.window_s, self.lock_s = max_fail, window_s, lock_s
        self._fails = {}
        self._lock = threading.Lock()

    def blocked(self, key):
        with self._lock:
            entry = self._fails.get(key)
            return bool(entry and entry.get("until", 0) > time.time())

    def failure(self, key):
        with self._lock:
            now = time.time()
            entry = self._fails.setdefault(key, {"times": []})
            entry["times"] = [t for t in entry["times"] if now - t < self.window_s] + [now]
            if len(entry["times"]) >= self.max_fail:
                entry["until"] = now + self.lock_s
                entry["times"] = []

    def success(self, key):
        with self._lock:
            self._fails.pop(key, None)
