import os
import secrets
import hashlib
import base64
import urllib.parse
import requests
import streamlit as st
from simple_salesforce import Salesforce

st.title("Salesforce OAuth (PKCE) Connection Test")

sf_cfg = st.secrets.get("salesforce", {})
CLIENT_ID = sf_cfg.get("client_id") or sf_cfg.get("consumer_key")
CLIENT_SECRET = sf_cfg.get("client_secret")  # may be optional based on your connected app settings
DOMAIN = sf_cfg.get("domain", "login")
REDIRECT_URI = sf_cfg.get("redirect_uri")

# Prefer My Domain if you have it (you do)
AUTH_HOST = sf_cfg.get("auth_host", "https://cvest.my.salesforce.com").rstrip("/")

if not CLIENT_ID or not REDIRECT_URI:
    st.error('Missing secrets. Need at least: [salesforce] client_id and redirect_uri')
    st.stop()

if not (REDIRECT_URI.startswith("https://") or REDIRECT_URI.startswith("http://")):
    st.error('redirect_uri must include https:// (example: "https://hudgeneratorapptest.streamlit.app/")')
    st.stop()

AUTH_URL = f"{AUTH_HOST}/services/oauth2/authorize"
TOKEN_URL = f"{AUTH_HOST}/services/oauth2/token"

# --- PKCE helpers ---
def make_code_verifier() -> str:
    return secrets.token_urlsafe(64)

def make_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")

# --- Read query params (Streamlit) ---
qp = st.query_params
auth_code = qp.get("code")
auth_error = qp.get("error")
auth_error_desc = qp.get("error_description")

if auth_error:
    st.error(f"OAuth error: {auth_error}\n\n{auth_error_desc or ''}")
    st.stop()

# Step 1: start login
if "pkce_verifier" not in st.session_state:
    st.session_state.pkce_verifier = make_code_verifier()

code_verifier = st.session_state.pkce_verifier
code_challenge = make_code_challenge(code_verifier)

login_params = {
    "response_type": "code",
    "client_id": CLIENT_ID,
    "redirect_uri": REDIRECT_URI,
    "code_challenge": code_challenge,
    "code_challenge_method": "S256",
    "prompt": "login",
}
login_link = AUTH_URL + "?" + urllib.parse.urlencode(login_params)

st.write("1) Click to authenticate with Salesforce:")
st.link_button("Login to Salesforce", login_link)

# Step 2: after redirect, exchange code for token
if auth_code:
    st.write("2) Authorization code received. Exchanging for token...")

    data = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code": auth_code,
        "code_verifier": code_verifier,
    }
    # Include client_secret only if you have it / your connected app requires it
    if CLIENT_SECRET:
        data["client_secret"] = CLIENT_SECRET

    try:
        resp = requests.post(TOKEN_URL, data=data, timeout=30)
        if resp.status_code != 200:
            st.error(f"Token exchange failed ({resp.status_code}): {resp.text}")
            st.stop()

        tok = resp.json()
        access_token = tok.get("access_token")
        instance_url = tok.get("instance_url")

        if not access_token or not instance_url:
            st.error(f"Token response missing access_token/instance_url: {tok}")
            st.stop()

        st.success("✅ Token acquired!")

        # Step 3: use token with simple-salesforce
        sf = Salesforce(instance_url=instance_url, session_id=access_token)
        st.success("✅ simple-salesforce client created")

        # A tiny read to prove API works (you can change this)
        ver = sf.sf_version
        st.write(f"Salesforce API version: {ver}")
        st.write(f"Instance URL: {instance_url}")

        # Optional: show identity (often works if scope allows)
        try:
            ident = sf.restful("identity")
            st.json(ident)
        except Exception as e:
            st.warning(f"Got token, but identity call failed (scope/permissions): {e}")

    except Exception as e:
        st.error(f"Connection failed: {e}")
