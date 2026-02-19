import base64
import hashlib
import secrets
import time
import urllib.parse

import requests
import streamlit as st
from simple_salesforce import Salesforce

st.title("Salesforce OAuth (PKCE) Test")

cfg = st.secrets["salesforce"]
CLIENT_ID = cfg["client_id"]
AUTH_HOST = cfg.get("auth_host", "https://login.salesforce.com").rstrip("/")
REDIRECT_URI = cfg["redirect_uri"]
CLIENT_SECRET = cfg.get("client_secret")  # optional

AUTH_URL = f"{AUTH_HOST}/services/oauth2/authorize"
TOKEN_URL = f"{AUTH_HOST}/services/oauth2/token"


def b64url_no_pad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")


def make_verifier() -> str:
    # 43-128 chars recommended; token_urlsafe gives URL-safe chars
    v = secrets.token_urlsafe(96)
    return v[:128]


def make_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return b64url_no_pad(digest)


# ---- Server-side store to survive redirects ----
# Streamlit Cloud can restart occasionally, but this fixes the *normal* redirect/session reset problem.
@st.cache_resource
def pkce_store():
    return {}  # { state: (verifier, created_epoch) }


store = pkce_store()

# Read query params
qp = st.query_params
code = qp.get("code")
returned_state = qp.get("state")
err = qp.get("error")
err_desc = qp.get("error_description")

if err:
    st.error(f"OAuth error: {err}")
    if err_desc:
        st.code(err_desc)
    st.stop()

# Clean up old states (TTL 10 minutes)
now = time.time()
ttl = 600
for s, (_v, t0) in list(store.items()):
    if now - t0 > ttl:
        store.pop(s, None)

# If we have a code, finish token exchange
if code:
    if not returned_state or returned_state not in store:
        st.error("Missing/expired OAuth state. Click login again.")
        st.stop()

    verifier, _t0 = store.pop(returned_state)

    st.write("Code received. Exchanging for token...")

    data = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code": code,
        "code_verifier": verifier,
    }
    if CLIENT_SECRET:
        data["client_secret"] = CLIENT_SECRET

    resp = requests.post(TOKEN_URL, data=data, timeout=30)

    if resp.status_code != 200:
        st.error(f"Token exchange failed ({resp.status_code})")
        st.code(resp.text)
        st.stop()

    tok = resp.json()
    access_token = tok.get("access_token")
    instance_url = tok.get("instance_url")

    if not access_token or not instance_url:
        st.error("Token response missing access_token/instance_url")
        st.json(tok)
        st.stop()

    st.success("✅ Token acquired")
    st.write("instance_url:", instance_url)

    # Prove API works
    try:
        sf = Salesforce(instance_url=instance_url, session_id=access_token)
        limits = sf.restful("limits")
        st.success("✅ API call successful (limits)")
        st.json(limits)
    except Exception as e:
        st.warning(f"Got token but API call failed: {e}")

    # Clear query params so refresh doesn't re-run exchange
    st.query_params.clear()
    st.stop()

# Otherwise: start login
state = secrets.token_urlsafe(24)
verifier = make_verifier()
challenge = make_challenge(verifier)
store[state] = (verifier, time.time())

login_params = {
    "response_type": "code",
    "client_id": CLIENT_ID,
    "redirect_uri": REDIRECT_URI,
    "code_challenge": challenge,
    "code_challenge_method": "S256",
    "state": state,
    "prompt": "login",
    # optional if you want identity:
    # "scope": "api refresh_token openid",
    "scope": "api refresh_token",
}

login_url = AUTH_URL + "?" + urllib.parse.urlencode(login_params)

st.write("Click to log in (opens in a new tab):")
st.link_button("Login to Salesforce", login_url)

with st.expander("Debug"):
    st.write("AUTH_HOST:", AUTH_HOST)
    st.write("REDIRECT_URI:", REDIRECT_URI)
    st.write("Callback must match exactly in Connected App.")
