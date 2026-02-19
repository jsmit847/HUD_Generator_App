import secrets
import hashlib
import base64
import urllib.parse
import requests
import streamlit as st
from simple_salesforce import Salesforce

st.title("Salesforce OAuth (PKCE) Test")

cfg = st.secrets.get("salesforce", {})
CLIENT_ID = cfg.get("client_id") or cfg.get("consumer_key")
AUTH_HOST = (cfg.get("auth_host") or "https://login.salesforce.com").rstrip("/")
REDIRECT_URI = cfg.get("redirect_uri")

if not CLIENT_ID or not REDIRECT_URI:
    st.error('Missing secrets. Need at least: [salesforce] client_id (or consumer_key) and redirect_uri')
    st.stop()

# IMPORTANT: redirect_uri must match Connected App callback EXACTLY (including trailing slash or not)
AUTH_URL = f"{AUTH_HOST}/services/oauth2/authorize"
TOKEN_URL = f"{AUTH_HOST}/services/oauth2/token"

def make_code_verifier() -> str:
    return secrets.token_urlsafe(64)

def make_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")

# Read query params after Salesforce redirects back
qp = st.query_params
auth_code = qp.get("code")
auth_error = qp.get("error")
auth_error_desc = qp.get("error_description")

if auth_error:
    st.error(f"OAuth error: {auth_error}\n\n{auth_error_desc or ''}")
    st.stop()

# Create verifier + state ONCE and keep them for the redirect
if "pkce_verifier" not in st.session_state:
    st.session_state.pkce_verifier = make_code_verifier()
if "oauth_state" not in st.session_state:
    st.session_state.oauth_state = secrets.token_urlsafe(24)

code_verifier = st.session_state.pkce_verifier
code_challenge = make_code_challenge(code_verifier)
state = st.session_state.oauth_state

# Build auth link
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

st.write("Step 1: Authenticate with Salesforce (opens in SAME tab to preserve PKCE verifier).")
st.markdown(
    f'<a href="{login_link}" target="_self">Login to Salesforce</a>',
    unsafe_allow_html=True
)

# After redirect: validate state + exchange code
if auth_code:
    returned_state = qp.get("state")
    if returned_state != state:
        st.error("State mismatch. Start over.")
        st.session_state.pop("pkce_verifier", None)
        st.session_state.pop("oauth_state", None)
        st.stop()

    st.write("Step 2: Code received. Exchanging for token...")

    data = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code": auth_code,
        "code_verifier": code_verifier,
    }

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

    st.success("Token acquired.")

    # Prove API works
    sf = Salesforce(instance_url=instance_url, session_id=access_token)
    st.success("simple-salesforce client created.")
    st.write("Instance URL:", instance_url)

    # Minimal API call to confirm auth
    try:
        limits = sf.restful("limits")
        st.write("API call OK (limits endpoint).")
        st.json(limits)
    except Exception as e:
        st.warning(f"Got token but API call failed (permissions/scopes): {e}")

    # Optional: clear query params so refresh doesn't re-run the token exchange
    try:
        st.query_params.clear()
    except Exception:
        pass
