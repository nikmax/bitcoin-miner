#!/usr/bin/env python3
import getpass, hashlib
pw = getpass.getpass("Dashboard-Passwort: ")
print(hashlib.sha256(pw.encode("utf-8")).hexdigest())
