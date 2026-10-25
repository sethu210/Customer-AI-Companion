"""
Unit tests call the coded tools directly with sly_data a gateway would send, mostly without signing it. Unsigned
identity is trusted only here (pytest loaded and this variable set; gateway_auth.trust_unsigned). Tests of the
signature itself turn it off with monkeypatch. A sly_data that carries an identity_token is always verified.
"""

import os

os.environ.setdefault("GATEWAY_IDENTITY_TRUST_UNSIGNED", "1")
