#!/usr/bin/env python3
import base64, hashlib, json, os, sys
pw = sys.stdin.readline().rstrip("\n")
assert 6 <= len(pw) <= 128, "length out of range"
salt = os.urandom(16)
h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 200000)
rec = {"salt": base64.b64encode(salt).decode(), "hash": base64.b64encode(h).decode(), "iter": 200000}
open("/etc/fleet-panel/password.json", "w").write(json.dumps(rec))
print("password updated")
