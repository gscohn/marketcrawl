"""Development server bound to the LAN, for testing on a phone.

Production is gunicorn on loopback behind Apache with TLS (spec section 13).
This exists only so a phone on the same WiFi can reach the config page before
there is a deployed host.
"""
from app import create_app

app = create_app()
# The LAN is plain http, so a Secure-only cookie would never be stored.
app.config["SESSION_COOKIE_SECURE"] = False

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
