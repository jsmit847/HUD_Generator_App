import urllib.parse
import requests
import streamlit as st
from simple_salesforce import Salesforce

st.set_page_config(layout="centered")
st.title("Salesforce OAuth Test")

SF = st.secrets.get("salesforce", {})
CLIENT_ID = SF.get("client_id", "")
CLIENT_SECRET = SF.get("client_secret", "")
DOMAIN = SF.get("domain", "login")

if not CLIENT_ID or not CLIENT_SECRET:
    st.error('Missing Streamlit secrets. Add [salesforce] client_id and client_secret in Secrets.')
    st.stop()

AUTH_BASE = f"https://{DOMAIN}.salesforce.com/services/oauth2/authorize"
TOKEN_URL = f"https://{DOMAIN}.salesforce.com/services/oauth2/token"

# IMPORTANT: redirect must match what you set in the Salesforce Connected App
# On Streamlit Cloud this is your app URL. We use the current page URL without query params.
redirect_uri = st.get_option("server.baseUrlPath")  # often empty
current_url = st.experimental_get_url()  # full URL to this page
base_url = current_url.split("?")[0]

# Read query params
qp = st.query_params
code = qp.get("code")
error = qp.get("error")
error_desc = qp.get("error_description")

if error:
    st.error(f"OAuth error: {error} {error_desc or ''}")
    st.stop()

if not code:
    # Step 1: send user to Salesforce login
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": base_url,
        "prompt": "login",
        # "scope": "refresh_token api"  # optional, depends on your connected app config
    }
    login_url = AUTH_BASE + "?" + urllib.parse.urlencode(params)

    st.write("Click to authenticate with Salesforce:")
    st.link_button("Login to Salesforce", login_url)
    st.caption("After you log in, Salesforce will redirect you back here.")
    st.stop()

# Step 2: exchange auth code for token
with st.spinner("Exchanging auth code for token..."):
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": base_url,
    }
    r = requests.post(TOKEN_URL, data=data, timeout=30)

if r.status_code != 200:
    st.error(f"Token exchange failed ({r.status_code}): {r.text}")
    st.stop()

tok = r.json()
access_token = tok.get("access_token")
instance_url = tok.get("instance_url")

if not access_token or not instance_url:
    st.error(f"Token response missing access_token/instance_url: {tok}")
    st.stop()

# Step 3: test an API call using simple-salesforce
try:
    sf = Salesforce(instance_url=instance_url, session_id=access_token)
    who = sf.query("SELECT Id, Username FROM User LIMIT 1")
    st.success("Connected successfully.")
    st.write("instance_url:", instance_url)
    st.json(who)
except Exception as e:
    st.error(f"Connected but API call failed: {e}")
