import base64
import hashlib
import secrets
import urllib.parse

import requests
import streamlit as st

st.title("Salesforce OAuth Test (Streamlit Cloud)")

# ---- Read secrets (must exist in Streamlit Cloud Secrets) ----
sf_cfg = st.secrets.get("salesforce", None)
if not sf_cfg:
    st.error('Missing Streamlit secret section [salesforce]. Add it in "Manage app" -> "Settings" -> "Secrets".')
    st.stop()

CLIENT_ID = sf_cfg.get("client_id", "").strip()
CLIENT_SECRET = sf_cfg.get("client_secret", "").strip()
DOMAIN = sf_cfg.get("domain", "login").strip()
REDIRECT_URI = sf_cfg.get("redirect_uri", "").strip()

if not CLIENT_ID or not CLIENT_SECRET or not REDIRECT_URI:
    st.error("Missing one of: salesforce.client_id, salesforce.client_secret, salesforce.redirect_uri")
    st.stop()

AUTH_BASE = "https://login.salesforce.com/services/oauth2/authorize" if DOMAIN == "login" else "https://test.salesforce.com/services/oauth2/authorize"
TOKEN_URL = "https://login.salesforce.com/services/oauth2/token" if DOMAIN == "login" else "https://test.salesforce.com/services/oauth2/token"

# ---- Helpers ----
def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")

def _build_auth_url() -> str:
    # Create and store verifier for this session
    verifier = secrets.token_urlsafe(64)
    st.session_state["pkce_verifier"] = verifier
    challenge = _pkce_challenge(verifier)

    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "login",
        # If your org uses My Domain and SSO, you can sometimes help routing by using a domain-specific startURL,
        # but keep this simple first.
    }
    return AUTH_BASE + "?" + urllib.parse.urlencode(params)

def _exchange_code_for_token(code: str) -> dict:
    verifier = st.session_state.get("pkce_verifier", None)
    if not verifier:
        raise RuntimeError("Missing PKCE verifier in session. Click the login link again to restart the flow.")

    data = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "code": code,
        "code_verifier": verifier,
    }
    resp = requests.post(TOKEN_URL, data=data, timeout=30)
    # Show a readable error if Salesforce rejects it
    if resp.status_code >= 400:
        raise RuntimeError(f"Token exchange failed ({resp.status_code}): {resp.text}")
    return resp.json()

# ---- Read query params returned by Salesforce ----
# Streamlit new API:
code = st.query_params.get("code")
err = st.query_params.get("error")
err_desc = st.query_params.get("error_description")

if err:
    st.error(f"Salesforce returned error: {err}\n\n{err_desc or ''}")
    st.stop()

if not code:
    st.write("Step 1: Click below to log into Salesforce and authorize.")
    auth_url = _build_auth_url()
    st.link_button("Log in to Salesforce", auth_url)

    st.info(
        "If you get a redirect_uri error: your Salesforce Connected App must allow the exact callback URL:\n"
        + REDIRECT_URI
    )
    st.stop()

# ---- Exchange code for token ----
st.write("Step 2: Exchanging authorization code for token...")
try:
    tok = _exchange_code_for_token(code)
    access_token = tok.get("access_token", "")
    instance_url = tok.get("instance_url", "")

    st.success("Connected successfully.")
    st.write("Instance URL:", instance_url)

    # Don’t print the whole token unless you absolutely need it
    if access_token:
        st.write("Access token (first 20 chars):", access_token[:20] + "...")
    else:
        st.warning("No access_token returned (unexpected). Full response below.")
        st.json(tok)

except Exception as e:
    st.error(str(e))
    st.stop()

# Optional: clear query params so refresh doesn't re-run exchange
if st.button("Clear URL params"):
    st.query_params.clear()
    st.rerun()
