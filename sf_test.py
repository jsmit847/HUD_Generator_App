import secrets
import base64
import hashlib
import urllib.parse
import requests
import streamlit as st
from simple_salesforce import Salesforce

st.set_page_config(page_title="Salesforce Connection Test", layout="centered")

st.title("Salesforce Connection Test")

# --- Secrets ---
sf_cfg = st.secrets.get("salesforce", {})
DOMAIN = sf_cfg.get("domain", "login")  # "login" (prod) or "test" (sandbox)
CLIENT_ID = sf_cfg.get("client_id")
CLIENT_SECRET = sf_cfg.get("client_secret")
REDIRECT_URI = sf_cfg.get("redirect_uri")  # MUST be your Streamlit Cloud URL

if not CLIENT_ID or not CLIENT_SECRET or not REDIRECT_URI:
    st.error(
        'Missing Streamlit secrets. Add:\n\n'
        '[salesforce]\n'
        'domain = "login"\n'
        'client_id = "..."\n'
        'client_secret = "..."\n'
        'redirect_uri = "https://<your-app>.streamlit.app/"\n'
    )
    st.stop()

AUTH_BASE = "https://login.salesforce.com/services/oauth2/authorize" if DOMAIN == "login" else "https://test.salesforce.com/services/oauth2/authorize"
TOKEN_URL = "https://login.salesforce.com/services/oauth2/token" if DOMAIN == "login" else "https://test.salesforce.com/services/oauth2/token"

# --- PKCE helpers ---
def _b64url_no_pad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")

def make_pkce():
    verifier = secrets.token_urlsafe(64)
    challenge = _b64url_no_pad(hashlib.sha256(verifier.encode("utf-8")).digest())
    return verifier, challenge

# --- Session state ---
if "pkce_verifier" not in st.session_state:
    st.session_state.pkce_verifier = None
if "sf_access_token" not in st.session_state:
    st.session_state.sf_access_token = None
if "sf_instance_url" not in st.session_state:
    st.session_state.sf_instance_url = None

# --- Read code/error from URL query params (Streamlit Cloud redirect lands here) ---
qp = st.query_params  # dict-like
code = qp.get("code")
err = qp.get("error")
err_desc = qp.get("error_description")

if err:
    st.error(f"OAuth error: {err_desc or err}")
    st.stop()

# --- If we have an auth code, exchange it for tokens ---
if code and (st.session_state.sf_access_token is None):
    if not st.session_state.pkce_verifier:
        st.error("PKCE verifier missing (session reset). Click 'Start Login' again.")
        st.stop()

    with st.spinner("Exchanging code for token..."):
        data = {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
            "code": code,
            "code_verifier": st.session_state.pkce_verifier,
        }
        r = requests.post(TOKEN_URL, data=data, timeout=30)
        if r.status_code != 200:
            st.error(f"Token exchange failed ({r.status_code}): {r.text}")
            st.stop()

        tok = r.json()
        st.session_state.sf_access_token = tok.get("access_token")
        st.session_state.sf_instance_url = tok.get("instance_url")

    # Clean query params so you don't keep re-exchanging on refresh
    st.query_params.clear()
    st.success("OAuth complete. Token stored in session for this browser tab.")

# --- Buttons / UI ---
col1, col2 = st.columns(2)

with col1:
    if st.button("Start Login"):
        verifier, challenge = make_pkce()
        st.session_state.pkce_verifier = verifier

        params = {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "login",
        }
        auth_url = AUTH_BASE + "?" + urllib.parse.urlencode(params)
        st.link_button("Open Salesforce Login", auth_url)

with col2:
    if st.button("Clear Session Token"):
        st.session_state.sf_access_token = None
        st.session_state.sf_instance_url = None
        st.session_state.pkce_verifier = None
        st.query_params.clear()
        st.success("Cleared. Click Start Login again.")

st.divider()

# --- Test Salesforce API call ---
if st.session_state.sf_access_token and st.session_state.sf_instance_url:
    st.write("Instance URL:", st.session_state.sf_instance_url)

    if st.button("Test API Call"):
        try:
            sf = Salesforce(
                instance_url=st.session_state.sf_instance_url,
                session_id=st.session_state.sf_access_token,
            )
            # very light call
            limits = sf.limits()
            st.success("Connected successfully.")
            st.json(limits)
        except Exception as e:
            st.error(f"API call failed: {e}")
else:
    st.info("Click Start Login → Open Salesforce Login → complete login → you'll be redirected back here.")
