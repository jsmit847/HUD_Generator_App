import base64
import hashlib
import secrets
import time
import urllib.parse

import requests
import streamlit as st
from simple_salesforce import Salesforce

st.set_page_config(page_title="SF OAuth PKCE Test", layout="centered")
st.title("Salesforce OAuth (PKCE) Test — Debug")

# ----------------------------
# Secrets (MUST be simple values)
# ----------------------------
# secrets.toml should look like:
# [salesforce]
# client_id = "..."
# auth_host = "https://cvest.my.salesforce.com"   # OR https://login.salesforce.com
# redirect_uri = "https://hudgeneratorapptest.streamlit.app"
# client_secret = "..."  # optional

cfg = st.secrets["salesforce"]
CLIENT_ID = cfg["client_id"]
AUTH_HOST = cfg.get("auth_host", "https://login.salesforce.com").rstrip("/")
REDIRECT_URI = cfg["redirect_uri"]
CLIENT_SECRET = cfg.get("client_secret")  # optional

AUTH_URL = f"{AUTH_HOST}/services/oauth2/authorize"
TOKEN_URL = f"{AUTH_HOST}/services/oauth2/token"


# ----------------------------
# PKCE helpers
# ----------------------------
def b64url_no_pad(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("utf-8")

def make_verifier() -> str:
    v = secrets.token_urlsafe(96)
    return v[:128]

def make_challenge(verifier: str) -> str:
    return b64url_no_pad(hashlib.sha256(verifier.encode("utf-8")).digest())


# ----------------------------
# Persist PKCE across redirect (do NOT rely on session_state)
# ----------------------------
@st.cache_resource
def pkce_store():
    return {}  # state -> (verifier, created_epoch)

store = pkce_store()

# TTL cleanup (10 minutes)
now = time.time()
TTL = 600
for s, (_v, t0) in list(store.items()):
    if now - t0 > TTL:
        store.pop(s, None)


# ----------------------------
# Read query params from redirect
# ----------------------------
qp = st.query_params
code = qp.get("code")
state = qp.get("state")
err = qp.get("error")
err_desc = qp.get("error_description")

if err:
    st.error(f"OAuth error: {err}")
    if err_desc:
        st.code(err_desc)
    st.stop()


# ----------------------------
# If we have an auth code, exchange it for token and run checks
# ----------------------------
if code:
    if not state or state not in store:
        st.error("Missing/expired state. Click Login again.")
        st.stop()

    verifier, _t0 = store.pop(state)

    st.write("Exchanging auth code for token...")
    data = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code": code,
        "code_verifier": verifier,
    }
    if CLIENT_SECRET:
        data["client_secret"] = CLIENT_SECRET

    tok_resp = requests.post(TOKEN_URL, data=data, timeout=30)
    st.write("Token status:", tok_resp.status_code)
    if tok_resp.status_code != 200:
        st.error("Token exchange failed")
        st.code(tok_resp.text)
        st.stop()

    tok = tok_resp.json()

    # --- Display safe token fields
    safe_tok = {k: tok.get(k) for k in ["instance_url", "id", "issued_at", "signature", "scope", "token_type"]}
    st.success("✅ Token acquired")
    st.json(safe_tok)

    access_token = tok.get("access_token")
    instance_url = tok.get("instance_url")
    id_url = tok.get("id")

    if not access_token or not instance_url:
        st.error("Token response missing access_token and/or instance_url.")
        st.json(tok)
        st.stop()

    headers = {"Authorization": f"Bearer {access_token}"}

    st.divider()
    st.subheader("Check 1 — Who is this token for? (Identity via tok['id'])")
    if id_url:
        id_resp = requests.get(id_url, headers=headers, timeout=30)
        st.write("Identity status:", id_resp.status_code)
        st.text(id_resp.text[:2000])
    else:
        st.warning("No 'id' field returned in token response.")

    st.divider()
    st.subheader("Check 2 — REST API base endpoint (should return versions list)")
    r_versions = requests.get(f"{instance_url}/services/data/", headers=headers, timeout=30)
    st.write("Status:", r_versions.status_code)
    st.text(r_versions.text[:2000])

    st.divider()
    st.subheader("Check 3 — Limits endpoint (may fail if API disabled)")
    r_limits = requests.get(f"{instance_url}/services/data/v59.0/limits", headers=headers, timeout=30)
    st.write("Status:", r_limits.status_code)
    st.text(r_limits.text[:2000])

    st.divider()
    st.subheader("Check 4 — simple-salesforce client + one call")
    try:
        sf = Salesforce(instance_url=instance_url, session_id=access_token)
        st.success("simple-salesforce client created.")
        # Use a very basic call:
        limits = sf.restful("limits")
        st.success("✅ simple-salesforce call OK")
        st.json(limits)
    except Exception as e:
        st.error(f"simple-salesforce call failed: {e}")

    st.divider()
    st.write("Done. Clearing query params to prevent re-running token exchange on refresh.")
    st.query_params.clear()
    st.stop()


# ----------------------------
# Otherwise, start login
# ----------------------------
new_state = secrets.token_urlsafe(24)
new_verifier = make_verifier()
new_challenge = make_challenge(new_verifier)
store[new_state] = (new_verifier, time.time())

login_params = {
    "response_type": "code",
    "client_id": CLIENT_ID,
    "redirect_uri": REDIRECT_URI,
    "code_challenge": new_challenge,
    "code_challenge_method": "S256",
    "state": new_state,
    "prompt": "login",
    "scope": "api refresh_token",
}

login_url = AUTH_URL + "?" + urllib.parse.urlencode(login_params)

st.write("Click Login to start OAuth. This will come back to your Streamlit app.")
st.link_button("Login to Salesforce", login_url)

with st.expander("Debug config (no secrets)"):
    st.write("AUTH_HOST:", AUTH_HOST)
    st.write("AUTH_URL:", AUTH_URL)
    st.write("TOKEN_URL:", TOKEN_URL)
    st.write("REDIRECT_URI:", REDIRECT_URI)
