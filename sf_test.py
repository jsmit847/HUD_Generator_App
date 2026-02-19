import base64
import hashlib
import secrets
import time
import urllib.parse
import re

import pandas as pd
import requests
import streamlit as st
from simple_salesforce import Salesforce

st.set_page_config(page_title="SF -> DataFrames Test", layout="wide")
st.title("Salesforce OAuth (PKCE) → Term + Bridge DataFrames")

# =========================================================
# Secrets
# =========================================================
cfg = st.secrets["salesforce"]
CLIENT_ID = cfg["client_id"]
AUTH_HOST = cfg.get("auth_host", "https://login.salesforce.com").rstrip("/")
REDIRECT_URI = cfg["redirect_uri"]
CLIENT_SECRET = cfg.get("client_secret")  # optional

AUTH_URL = f"{AUTH_HOST}/services/oauth2/authorize"
TOKEN_URL = f"{AUTH_HOST}/services/oauth2/token"

# =========================================================
# PKCE helpers
# =========================================================
def b64url_no_pad(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("utf-8")

def make_verifier() -> str:
    v = secrets.token_urlsafe(96)
    return v[:128]

def make_challenge(verifier: str) -> str:
    return b64url_no_pad(hashlib.sha256(verifier.encode("utf-8")).digest())

@st.cache_resource
def pkce_store():
    return {}  # state -> (verifier, created_epoch)

store = pkce_store()

# cleanup old states
now = time.time()
TTL = 600
for s, (_v, t0) in list(store.items()):
    if now - t0 > TTL:
        store.pop(s, None)

# =========================================================
# OAuth flow
# =========================================================
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

if "sf_token" not in st.session_state:
    st.session_state.sf_token = None

def exchange_code_for_token(code: str, verifier: str) -> dict:
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
        raise RuntimeError(f"Token exchange failed ({resp.status_code}): {resp.text}")
    return resp.json()

# handle redirect
if code:
    if not state or state not in store:
        st.error("Missing/expired OAuth state. Click login again.")
        st.stop()
    verifier, _t0 = store.pop(state)
    tok = exchange_code_for_token(code, verifier)
    st.session_state.sf_token = tok
    st.query_params.clear()
    st.rerun()

# no token yet -> login
if not st.session_state.sf_token:
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

    st.info("Step 1: Authenticate with Salesforce.")
    st.link_button("Login to Salesforce", login_url)
    with st.expander("Debug"):
        st.write("AUTH_HOST:", AUTH_HOST)
        st.write("REDIRECT_URI:", REDIRECT_URI)
    st.stop()

# =========================================================
# Token -> Salesforce client
# =========================================================
tok = st.session_state.sf_token
access_token = tok.get("access_token")
instance_url = tok.get("instance_url")
id_url = tok.get("id")

if not access_token or not instance_url:
    st.error("Token missing access_token/instance_url")
    st.json({k: tok.get(k) for k in ["instance_url", "id", "issued_at", "scope", "token_type"]})
    st.stop()

sf = Salesforce(instance_url=instance_url, session_id=access_token)

c1, c2 = st.columns([2, 1])
with c1:
    st.success("✅ Authenticated")
    st.write("instance_url:", instance_url)
with c2:
    if st.button("Log out / Clear token"):
        st.session_state.sf_token = None
        st.rerun()

with st.expander("Identity (proof it's you)"):
    if id_url:
        headers = {"Authorization": f"Bearer {access_token}"}
        r = requests.get(id_url, headers=headers, timeout=30)
        st.write("status:", r.status_code)
        st.json(r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text})

# =========================================================
# Helpers for your dataframes
# =========================================================
VALID_STAGES = ["Closed Won", "Expired", "Matured", "Paid Off", "Sold"]
TERM_RECORDTYPES = {"Term Loan", "DSCR"}
BRIDGE_RECORDTYPES = {"Acquired Bridge Loan", "Bridge Loan", "SAB Loan"}

def soql_quote(s: str) -> str:
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"

def safe_flatten_recordtype(df: pd.DataFrame) -> pd.DataFrame:
    if "RecordType" in df.columns:
        df["RecordType.Name"] = df["RecordType"].apply(lambda x: (x or {}).get("Name"))
        df = df.drop(columns=["RecordType"], errors="ignore")
    return df

def parse_date_any(x):
    if x in ("", None) or pd.isna(x):
        return None
    dt = pd.to_datetime(x, errors="coerce")
    if pd.isna(dt):
        return None
    return dt.date()

def digits_only(x) -> str:
    return re.sub(r"\D", "", "" if x is None or pd.isna(x) else str(x))

def last5_strip_prefix(x) -> str:
    d = digits_only(x)
    if d.startswith("4030") or d.startswith("6000"):
        d = d[4:]
    return d[-5:] if len(d) >= 5 else d

def build_where_for_search(mode: str, q: str) -> str:
    q = (q or "").strip()
    if mode == "Account Name":
        return "Account_Name__c LIKE " + soql_quote("%" + q + "%")
    elif mode == "Deal Name":
        return "Name LIKE " + soql_quote("%" + q + "%")
    else:
        digits = re.sub(r"\D", "", q)
        if digits:
            return "(" + " OR ".join([
                "Deal_Loan_Number__c = " + soql_quote(digits),
                "Deal_Loan_Number__c LIKE " + soql_quote("%" + digits + "%"),
            ]) + ")"
        return "Deal_Loan_Number__c LIKE " + soql_quote("%" + q + "%")

# =========================================================
# UI: search + pick account
# =========================================================
st.subheader("Step 2: Find an account’s loans and show Term/Bridge dataframes")

mode = st.selectbox("Search by", ["Account Name", "Deal Name", "Deal Loan Number"], index=0)
q = st.text_input("Search text", value="")
stage_filter = st.multiselect("Stages", VALID_STAGES, default=VALID_STAGES)

if st.button("Search", type="primary"):
    if not q.strip():
        st.warning("Enter search text.")
        st.stop()

    where = build_where_for_search(mode, q.strip())
    where += " AND StageName IN (" + ", ".join(soql_quote(s) for s in stage_filter) + ")"

    preview_fields = ["Id","Name","Deal_Loan_Number__c","Account_Name__c","StageName","CloseDate","RecordType.Name"]
    soql = f"SELECT {', '.join(preview_fields)} FROM Opportunity WHERE {where} ORDER BY CloseDate DESC NULLS LAST LIMIT 2000"
    rows = sf.query_all(soql).get("records", [])

    df_prev = pd.DataFrame(rows).drop(columns=["attributes"], errors="ignore")
    df_prev = safe_flatten_recordtype(df_prev)

    if df_prev.empty:
        st.warning("No matches.")
        st.stop()

    st.session_state["df_prev"] = df_prev

    acct_counts = (
        df_prev.groupby("Account_Name__c", dropna=False)
               .size()
               .reset_index(name="loans")
               .sort_values(["loans","Account_Name__c"], ascending=[False, True])
               .reset_index(drop=True)
    )
    st.session_state["acct_counts"] = acct_counts

if "acct_counts" in st.session_state and not st.session_state["acct_counts"].empty:
    df_prev = st.session_state["df_prev"]
    acct_counts = st.session_state["acct_counts"]

    st.write("Preview matches:")
    st.dataframe(df_prev, use_container_width=True)

    acct_idx = st.selectbox(
        "Pick the correct Account",
        options=list(range(len(acct_counts))),
        format_func=lambda i: f"{acct_counts.iloc[i]['Account_Name__c']}  ({acct_counts.iloc[i]['loans']} loans)",
    )
    acct_name = acct_counts.iloc[acct_idx]["Account_Name__c"]

    if st.button("Build Term + Bridge DataFrames"):
        opp_fields = [
            "Id","Name","Deal_Loan_Number__c","Account_Name__c","RecordType.Name","StageName",
            "CloseDate","Current_UPB__c","Next_Payment_Date__c",
        ]
        where_acct = "Account_Name__c = " + soql_quote(acct_name)
        where_acct += " AND StageName IN (" + ", ".join(soql_quote(s) for s in stage_filter) + ")"

        soql2 = f"SELECT {', '.join(opp_fields)} FROM Opportunity WHERE {where_acct} ORDER BY CloseDate DESC NULLS LAST LIMIT 2000"
        rows2 = sf.query_all(soql2).get("records", [])

        df_all = pd.DataFrame(rows2).drop(columns=["attributes"], errors="ignore")
        df_all = safe_flatten_recordtype(df_all)

        if df_all.empty:
            st.warning("No loans found for that account + stage filter.")
            st.stop()

        # normalize a couple of fields the same way your notebook does
        df_all["OriginationDate_dt"] = df_all.get("CloseDate").apply(parse_date_any)
        df_all["NextPay_dt"] = df_all.get("Next_Payment_Date__c").apply(parse_date_any)

        rt = df_all.get("RecordType.Name", pd.Series([""] * len(df_all))).fillna("")
        df_term_raw = df_all[rt.isin(TERM_RECORDTYPES)].copy()
        df_bridge_raw = df_all[rt.isin(BRIDGE_RECORDTYPES)].copy()

        # Build "term_rows" and "bridge_rows" (minimal columns for testing)
        term_rows = pd.DataFrame()
        if not df_term_raw.empty:
            term_rows["Loan ID"] = df_term_raw["Deal_Loan_Number__c"].apply(
                lambda x: str(last5_strip_prefix(x)).zfill(5) if str(last5_strip_prefix(x)).strip() else ""
            )
            term_rows["Loan"] = df_term_raw.get("Name", "")
            term_rows["Account Name"] = df_term_raw.get("Account_Name__c", "")
            term_rows["Origination Date"] = df_term_raw.get("OriginationDate_dt")
            term_rows["Next Payment Date"] = df_term_raw.get("NextPay_dt")
            term_rows["Outstanding Balance Num"] = pd.to_numeric(df_term_raw.get("Current_UPB__c"), errors="coerce")

        bridge_rows = pd.DataFrame()
        if not df_bridge_raw.empty:
            bridge_rows["Loan ID"] = df_bridge_raw["Deal_Loan_Number__c"].apply(
                lambda x: str(last5_strip_prefix(x)).zfill(5) if str(last5_strip_prefix(x)).strip() else ""
            )
            bridge_rows["Loan"] = df_bridge_raw.get("Name", "")
            bridge_rows["Account Name"] = df_bridge_raw.get("Account_Name__c", "")
            bridge_rows["Origination Date"] = df_bridge_raw.get("OriginationDate_dt")
            bridge_rows["Outstanding Balance Num"] = pd.to_numeric(df_bridge_raw.get("Current_UPB__c"), errors="coerce")

        st.subheader(f"✅ Results for Account: {acct_name}")

        c1, c2 = st.columns(2)
        with c1:
            st.markdown(f"### Term loans ({len(term_rows)})")
            st.dataframe(term_rows, use_container_width=True)
        with c2:
            st.markdown(f"### Bridge loans ({len(bridge_rows)})")
            st.dataframe(bridge_rows, use_container_width=True)

        # also keep them accessible for other pages
        st.session_state["term_rows"] = term_rows
        st.session_state["bridge_rows"] = bridge_rows
