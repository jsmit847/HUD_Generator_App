import secrets
import hashlib
import base64
import urllib.parse
import requests
import streamlit as st
from simple_salesforce import Salesforce

st.set_page_config(page_title="Salesforce OAuth (PKCE) Test", layout="centered")
st.title("Salesforce OAuth (PKCE) Connection Test")

sf_cfg = st.secrets.get("salesforce", {})
CLIENT_ID = sf_cfg.get("client_id") or sf_cfg.get("consumer_key")
CLIENT_SECRET = sf_cfg.get("client_secret")  # optional depending on Connected App settings
AUTH_HOST = (sf_cfg.get("auth_host") or "https://login.salesforce.com").rstrip("/")
REDIRECT_URI = sf_cfg.get("redirect_uri")

if not CLIENT_ID or not REDIRECT_URI:
    st.error("Missing secrets. Need: [salesforce] client_id and redirect_uri")
    st.stop()

# Build endpoints from My Domain host
AUTH_URL = f"{AUTH_HOST}/services/oauth2/authorize"
TOKEN_URL = f"{AUTH_HOST}/services/oauth2/token"

def make_code_verifier() -> str:
    return secrets.token_urlsafe(64)

def make_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")

# --- Server-side cache for PKCE verifiers keyed by OAuth "state" ---
@st.cache_resource
def verifier_store():
    # persists in the Streamlit process between reruns
    return {}

store = verifier_store()

# --- Read query params after redirect ---
qp = st.query_params
auth_code = qp.get("code")
auth_error = qp.get("error")
auth_error_desc = qp.get("error_description")
returned_state = qp.get("state")

if auth_error:
    st.error(f"OAuth error: {auth_error}\n\n{auth_error_desc or ''}")
    st.stop()

# --- Step 1: Create a login URL with PKCE + state ---
if "oauth_state" not in st.session_state:
    st.session_state.oauth_state = secrets.token_urlsafe(16)

state = st.session_state.oauth_state

if state not in store:
    # only generate verifier once per state
    store[state] = make_code_verifier()

code_verifier = store[state]
code_challenge = make_code_challenge(code_verifier)

login_params = {
    "response_type": "code",
    "client_id": CLIENT_ID,
    "redirect_uri": REDIRECT_URI,
    "code_challenge": code_challenge,
    "code_challenge_method": "S256",
    "state": state,
    "prompt": "login",
}

login_link = AUTH_URL + "?" + urllib.parse.urlencode(login_params)

st.write("1) Click to authenticate with Salesforce:")
# Use a normal link so it stays in the same tab/session more reliably
st.markdown(f'<a href="{login_link}" target="_self">Login to Salesforce</a>', unsafe_allow_html=True)

st.divider()

# --- Step 2: Exchange code for token (after redirect) ---
if auth_code:
    st.write("2) Authorization code received. Exchanging for token...")

    # Make sure we can recover the original verifier
    if not returned_state or returned_state not in store:
        st.error(
            "Missing/unknown state after redirect. This usually means the login happened in a different session/tab.\n\n"
            "Try again by clicking the login link above (it opens in the same tab)."
        )
        st.stop()

    verifier_for_exchange = store[returned_state]

    data = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code": auth_code,
        "code_verifier": verifier_for_exchange,
    }
    if CLIENT_SECRET:
        data["client_secret"] = CLIENT_SECRET

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

    st.success("Token acquired. Creating simple-salesforce client...")

    sf = Salesforce(instance_url=instance_url, session_id=access_token)

    st.success("Connected successfully.")
    st.write(f"Instance URL: {instance_url}")
    st.write(f"API Version: {sf.sf_version}")

    # Optional proof call
    try:
        ident = sf.restful("identity")
        st.json(ident)
    except Exception as e:
        st.warning(f"Connected, but identity call failed (scope/permissions): {e}")
