# =========================
# HUD GENERATOR (APP.PY) — SALESFORCE-ONLY (NO HAYDEN, NO FCI)
# OAuth Authorization Code + PKCE using Streamlit secrets
# =========================
import re
import html
import textwrap
import base64
import hashlib
import secrets
import urllib.parse
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
import streamlit as st
from simple_salesforce import Salesforce

# =========================
# PAGE CONFIG
# =========================
st.set_page_config(page_title="HUD Generator", page_icon="🏗️", layout="wide")

# =========================
# REPO FILE PATHS (in same repo)
# =========================
DEFAULT_CAF_PATH = Path("Corevest_CAF National 52874_2.10.xlsx")
DEFAULT_OSC_PATH = Path("OSC_Zstatus_COREVEST_2026-02-17_180520.xlsx")

# =========================
# HELPERS
# =========================
def norm(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = (
        df.columns.astype(str)
        .str.strip()
        .str.lower()
        .str.replace(r"\s+", "_", regex=True)
    )
    return df

def parse_money(val) -> float:
    if val is None:
        return 0.0
    s = str(val).strip()
    if s == "" or s.lower() in {"nan", "none"}:
        return 0.0
    s = s.replace("$", "").replace(",", "")
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    try:
        x = float(s)
    except Exception:
        return 0.0
    return -x if neg else x

def fmt_money(x) -> str:
    try:
        return f"${float(x):,.2f}"
    except Exception:
        return "$0.00"

def normalize_pct(s: str) -> str:
    t = str(s).strip()
    if t == "":
        return ""
    t = t.replace("%", "").strip()
    try:
        v = float(t)
    except Exception:
        return ""
    if 0 < v <= 1:
        v *= 100
    return f"{v:.0f}%"

def ratio_to_pct_str(x) -> str:
    if x is None or str(x).strip() == "":
        return ""
    try:
        v = float(str(x).replace("%", "").strip())
    except Exception:
        return ""
    if 0 < v <= 1:
        v *= 100
    return f"{v:.0f}%"

def parse_date_to_mmddyyyy(s: str) -> str:
    t = str(s).strip()
    if t == "":
        return ""
    digits = re.sub(r"\D", "", t)
    if len(digits) == 8:
        yyyy = int(digits[:4])
        try:
            if 1900 <= yyyy <= 2100:
                dt = datetime.strptime(digits, "%Y%m%d")
            else:
                dt = datetime.strptime(digits, "%m%d%Y")
            return dt.strftime("%m/%d/%Y")
        except Exception:
            pass
    try:
        dt = pd.to_datetime(t, errors="raise")
        return dt.strftime("%m/%d/%Y")
    except Exception:
        return ""

def safe_first(df: pd.DataFrame, col: str, default=""):
    if df is None or df.empty:
        return default
    if col not in df.columns:
        return default
    return df[col].iloc[0]

def require_cols(df: pd.DataFrame, cols: list[str], dataset_name: str):
    missing = [c for c in cols if c not in df.columns]
    if missing:
        st.error(
            f"Missing expected column(s) in **{dataset_name}**: "
            + ", ".join([f"`{m}`" for m in missing])
        )
        st.stop()

def recompute(ctx: dict) -> dict:
    ctx["allocated_loan_amount"] = float(ctx.get("advance_amount", 0.0)) + float(ctx.get("total_reno_drawn", 0.0))
    ctx["construction_advance_amount"] = float(ctx.get("advance_amount", 0.0))

    fee_keys = ["inspection_fee", "wire_fee", "construction_mgmt_fee", "title_fee"]
    ctx["total_fees"] = sum(float(ctx.get(k, 0.0)) for k in fee_keys)

    include_lates = bool(ctx.get("include_late_charges", False))
    late_charges = float(ctx.get("accrued_late_charges_amt", 0.0))
    ctx["late_charges_line_item"] = late_charges if include_lates else 0.0

    ctx["net_amount_to_borrower"] = ctx["construction_advance_amount"] - ctx["total_fees"] - ctx["late_charges_line_item"]

    ctx["available_balance"] = (
        float(ctx.get("total_loan_amount", 0.0))
        - float(ctx.get("initial_advance", 0.0))
        - float(ctx.get("total_reno_drawn", 0.0))
        - float(ctx.get("advance_amount", 0.0))
        - float(ctx.get("interest_reserve", 0.0))
    )
    return ctx

def render_hud_html(ctx: dict) -> str:
    company_name = "COREVEST AMERICAN FINANCE LENDER LLC"
    company_addr = "4 Park Plaza, Suite 900, Irvine, CA 92614"

    borrower_disp = html.escape(str(ctx.get("borrower_disp", "") or ""))
    address_disp  = html.escape(str(ctx.get("address_disp", "") or ""))
    workday_sup_code = html.escape(str(ctx.get("workday_sup_code", "") or ""))
    advance_date  = html.escape(str(ctx.get("advance_date", "") or ""))

    hb_current = html.escape(str(ctx.get("holdback_current", "") or ""))
    hb_closing = html.escape(str(ctx.get("holdback_closing", "") or ""))

    show_lates = bool(ctx.get("include_late_charges", False))

    html_str = f"""
<style>
  .hud-page {{
    width: 980px;
    font-family: Arial, Helvetica, sans-serif;
    font-size: 13px;
    color: #000;
  }}
  .hud-top {{
    text-align: center;
    margin-bottom: 10px;
    line-height: 1.25;
  }}
  .hud-top .c1 {{ font-weight: 700; }}
  .hud-top .c3 {{ font-weight: 800; font-size: 16px; }}
  .hud-box {{
    border: 2px solid #000;
    padding: 10px;
  }}
  table.hud {{
    width: 100%;
    border-collapse: collapse;
    table-layout: fixed;
  }}
  table.hud td {{
    border: 0;
    padding: 4px 6px;
    vertical-align: middle;
  }}
  .grid {{ border: 1px solid #d0d0d0; }}
  .lbl {{ font-weight: 700; text-align: left; width: 24%; }}
  .val {{ text-align: right; width: 26%; white-space: nowrap; }}
  .rlbl {{ font-weight: 700; text-align: left; width: 24%; }}
  .rval {{ text-align: right; width: 26%; white-space: nowrap; }}
  .borrower-line {{
    border-top: 2px solid #000;
    margin-top: 10px;
    padding-top: 8px;
  }}
  .addr-line {{ margin-top: 2px; }}

  .section-title {{
    margin-top: 14px;
    border: 2px solid #000;
    border-bottom: 0;
    padding: 6px 8px;
    font-weight: 700;
    background: #e6e6e6;
  }}
  table.charges {{
    width: 100%;
    border-collapse: collapse;
    table-layout: fixed;
    border: 2px solid #000;
  }}
  table.charges th, table.charges td {{
    border: 1px solid #000;
    padding: 6px 8px;
  }}
  table.charges th {{
    font-weight: 700;
    background: #e6e6e6;
  }}
  table.charges th:last-child, table.charges td:last-child {{
    text-align: right;
    white-space: nowrap;
    width: 26%;
  }}
  table.charges td:first-child {{ width: 74%; }}
  .bold {{ font-weight: 700; }}
  .tot {{ font-weight: 800; }}
</style>

<div class="hud-page">
  <div class="hud-top">
    <div class="c1">{company_name}</div>
    <div>{company_addr}</div>
    <div class="c3">Final Settlement Statement</div>
  </div>

  <div class="hud-box">
    <table class="hud">
      <tr>
        <td class="lbl">Total Loan Amount:</td><td class="val grid">{fmt_money(ctx.get("total_loan_amount", 0.0))}</td>
        <td class="rlbl">Loan ID:</td><td class="rval grid">{html.escape(str(ctx.get("deal_number","")))}</td>
      </tr>
      <tr>
        <td class="lbl">Initial Advance:</td><td class="val grid">{fmt_money(ctx.get("initial_advance", 0.0))}</td>
        <td class="rlbl">Holdback % Current:</td><td class="rval grid">{hb_current}</td>
      </tr>
      <tr>
        <td class="lbl">Total Reno Drawn:</td><td class="val grid">{fmt_money(ctx.get("total_reno_drawn", 0.0))}</td>
        <td class="rlbl">Holdback % at Closing:</td><td class="rval grid">{hb_closing}</td>
      </tr>
      <tr>
        <td class="lbl">Advance Amount:</td><td class="val grid">{fmt_money(ctx.get("advance_amount", 0.0))}</td>
        <td class="rlbl">Allocated Loan Amount:</td><td class="rval grid">{fmt_money(ctx.get("allocated_loan_amount", 0.0))}</td>
      </tr>
      <tr>
        <td class="lbl">Interest Reserve:</td><td class="val grid">{fmt_money(ctx.get("interest_reserve", 0.0))}</td>
        <td class="rlbl">Net Amount to Borrower:</td><td class="rval grid">{fmt_money(ctx.get("net_amount_to_borrower", 0.0))}</td>
      </tr>
      <tr>
        <td class="lbl">Available Balance:</td><td class="val grid">{fmt_money(ctx.get("available_balance", 0.0))}</td>
        <td class="rlbl">Workday SUP Code:</td><td class="rval grid">{workday_sup_code}</td>
      </tr>
      <tr>
        <td class="lbl"></td><td class="val"></td>
        <td class="rlbl">Advance Date:</td><td class="rval grid"><span class="bold">{advance_date}</span></td>
      </tr>
    </table>

    <div class="borrower-line">
      <div><span class="bold">Borrower:</span> {borrower_disp}</div>
      <div class="addr-line"><span class="bold">Address:</span> {address_disp}</div>
    </div>
  </div>

  <div class="section-title">Charge Description</div>
  <table class="charges">
    <tr><th>Charge Description</th><th>Amount</th></tr>
    <tr><td class="bold">Construction Advance Amount</td><td class="bold">{fmt_money(ctx.get("construction_advance_amount", 0.0))}</td></tr>
    <tr><td>3rd party Inspection Fee</td><td>{fmt_money(ctx.get("inspection_fee", 0.0))}</td></tr>
    <tr><td>Wire Fee</td><td>{fmt_money(ctx.get("wire_fee", 0.0))}</td></tr>
    <tr><td>Construction Management Fee</td><td>{fmt_money(ctx.get("construction_mgmt_fee", 0.0))}</td></tr>
    <tr><td>Title Fee</td><td>{fmt_money(ctx.get("title_fee", 0.0))}</td></tr>
    {"<tr><td>Accrued Late Charges</td><td>" + fmt_money(ctx.get("late_charges_line_item", 0.0)) + "</td></tr>" if show_lates else ""}
    <tr class="tot"><td>Total Fees</td><td>{fmt_money(ctx.get("total_fees", 0.0) + (ctx.get("late_charges_line_item",0.0) if show_lates else 0.0))}</td></tr>
    <tr class="tot"><td>Reimbursement to Borrower</td><td>{fmt_money(ctx.get("net_amount_to_borrower", 0.0))}</td></tr>
  </table>
</div>
"""
    return textwrap.dedent(html_str).strip()

# =========================
# SALESFORCE OAUTH PKCE
# =========================
def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")

def pkce_pair():
    verifier = b64url(secrets.token_bytes(32))
    challenge = b64url(hashlib.sha256(verifier.encode("utf-8")).digest())
    return verifier, challenge

def get_sf_secrets():
    if "salesforce" not in st.secrets:
        st.error("Missing [salesforce] in Streamlit secrets.")
        st.stop()
    s = st.secrets["salesforce"]
    # Must exist in secrets; do not print them
    for k in ["client_id", "client_secret", "auth_host", "redirect_uri"]:
        if not s.get(k):
            st.error(f"Missing salesforce secret key: {k}")
            st.stop()
    return s

def oauth_login_button(auth_host, client_id, redirect_uri, scope="refresh_token api"):
    if "pkce_verifier" not in st.session_state:
        v, c = pkce_pair()
        st.session_state.pkce_verifier = v
        st.session_state.pkce_challenge = c
        st.session_state.oauth_state = b64url(secrets.token_bytes(16))

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": st.session_state.pkce_challenge,
        "code_challenge_method": "S256",
        "state": st.session_state.oauth_state,
        "scope": scope,
    }
    auth_url = f"{auth_host}/services/oauth2/authorize?{urllib.parse.urlencode(params)}"
    st.link_button("🔑 Login to Salesforce", auth_url, use_container_width=True)

def exchange_code_for_token(auth_host, client_id, client_secret, redirect_uri, code, code_verifier):
    token_url = f"{auth_host}/services/oauth2/token"
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    resp = requests.post(token_url, data=data, timeout=30)
    resp.raise_for_status()
    return resp.json()

@st.cache_resource(show_spinner=False)
def sf_from_token(instance_url: str, access_token: str, api_version="59.0"):
    return Salesforce(instance_url=instance_url, session_id=access_token, version=api_version)

def soql_quote(s: str) -> str:
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"

def sf_one(sf, soql: str) -> dict | None:
    res = sf.query(soql)
    recs = res.get("records", [])
    return recs[0] if recs else None

def find_property_by_input(sf, user_input: str) -> dict | None:
    q = user_input.strip()
    if not q:
        return None
    soql_base = """
    SELECT Id, Name,
           Yardi_Id__c, Servicer_Id__c,
           Borrower_Name__c, Full_Address__c,
           Initial_Disbursement_Used__c,
           Renovation_Advance_Amount_Used__c,
           Interest_Allocation__c,
           Holdback_To_Rehab_Ratio__c,
           Late_Fees_Servicer__c,
           Next_Payment_Date__c,
           Loan__c,
           Opportunity__c,
           Account__c
    FROM Property__c
    """

    rec = sf_one(sf, f"{soql_base} WHERE Yardi_Id__c = {soql_quote(q)} LIMIT 1")
    if rec:
        return rec

    rec = sf_one(sf, f"{soql_base} WHERE Servicer_Id__c = {soql_quote(q)} LIMIT 1")
    if rec:
        return rec

    like = "%" + q.replace("%", "\\%") + "%"
    rec = sf_one(sf, f"{soql_base} WHERE Yardi_Id__c LIKE {soql_quote(like)} LIMIT 1")
    return rec

def find_loan(sf, loan_id: str) -> dict | None:
    if not loan_id:
        return None
    soql = f"""
    SELECT Id, Name,
           Servicer_Loan_Id__c,
           Servicer_Loan_Status__c,
           Next_Payment_Date__c
    FROM Loan__c
    WHERE Id = {soql_quote(loan_id)}
    LIMIT 1
    """.strip()
    return sf_one(sf, soql)

def find_opportunity(sf, opp_id: str) -> dict | None:
    if not opp_id:
        return None
    soql = f"""
    SELECT Id, Name,
           Next_Payment_Date__c,
           Late_Fees_Servicer__c
    FROM Opportunity
    WHERE Id = {soql_quote(opp_id)}
    LIMIT 1
    """.strip()
    return sf_one(sf, soql)

def find_account(sf, acct_id: str) -> dict | None:
    if not acct_id:
        return None
    soql = f"""
    SELECT Id, Name,
           Yardi_Vendor_Code__c
    FROM Account
    WHERE Id = {soql_quote(acct_id)}
    LIMIT 1
    """.strip()
    return sf_one(sf, soql)

def find_commitment_from_advance(sf, property_rec: dict) -> float:
    # Try common relationship fields; adjust if your org uses a different link.
    prop_id = property_rec.get("Id")
    opp_id = property_rec.get("Opportunity__c")

    candidates = [
        ("Property__c", prop_id),
        ("Opportunity__c", opp_id),
        ("Deal__c", opp_id),
    ]
    for field, val in candidates:
        if not val:
            continue
        soql = f"""
        SELECT Id, Name, LOC_Commitment__c
        FROM Advance__c
        WHERE {field} = {soql_quote(val)}
        ORDER BY CreatedDate DESC
        LIMIT 1
        """.strip()
        rec = sf_one(sf, soql)
        if rec and rec.get("LOC_Commitment__c") is not None:
            return float(rec.get("LOC_Commitment__c") or 0.0)
    return 0.0

# =========================
# LOAD OSC + CAF FROM REPO
# =========================
@st.cache_data(show_spinner=False)
def load_osc(path: Path) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name="COREVEST", dtype=str)
    return norm(df)

@st.cache_data(show_spinner=False)
def load_caf(path: Path) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name=0, dtype=str)
    return norm(df)

# =========================
# APP
# =========================
st.title("🏗️ HUD Generator")
st.caption("Salesforce-only deal data (no Hayden/FCI). OSC + CAF loaded from repo.")

sf_secret = get_sf_secrets()
client_id = sf_secret["client_id"]
client_secret = sf_secret["client_secret"]
auth_host = sf_secret["auth_host"].rstrip("/")
redirect_uri = sf_secret["redirect_uri"].strip()
api_version = str(sf_secret.get("api_version", "59.0"))

# Read OAuth callback params
params = st.query_params
code = params.get("code", None)
state = params.get("state", None)

# Login section
if "sf_token" not in st.session_state:
    st.session_state.sf_token = None

if st.session_state.sf_token is None:
    oauth_login_button(auth_host, client_id, redirect_uri)

    if code:
        # verify state
        if not st.session_state.get("oauth_state") or state != st.session_state.oauth_state:
            st.error("OAuth state mismatch. Click Login again.")
            st.stop()

        try:
            tok = exchange_code_for_token(
                auth_host=auth_host,
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=redirect_uri,
                code=code,
                code_verifier=st.session_state.pkce_verifier,
            )
            st.session_state.sf_token = tok
            # Clear query params so refresh doesn't re-exchange
            st.query_params.clear()
            st.success("Salesforce connected ✅")
        except Exception as e:
            st.error(f"Token exchange failed: {e}")
            st.stop()
    else:
        st.stop()

tok = st.session_state.sf_token
access_token = tok.get("access_token", "")
instance_url = tok.get("instance_url", "") or auth_host
if not access_token:
    st.error("Missing access_token after OAuth.")
    st.stop()

sf = sf_from_token(instance_url, access_token, api_version=api_version)

# Load OSC/CAF from repo
osc_df = None
caf_df = None
if DEFAULT_OSC_PATH.exists():
    osc_df = load_osc(DEFAULT_OSC_PATH)
    require_cols(osc_df, ["account_number", "primary_status"], "OSC ZStatus")
else:
    st.warning(f"OSC file not found in repo: {DEFAULT_OSC_PATH}")

if DEFAULT_CAF_PATH.exists():
    caf_df = load_caf(DEFAULT_CAF_PATH)
else:
    st.warning(f"CAF file not found in repo: {DEFAULT_CAF_PATH}")

# Tabs
tab_inputs, tab_results = st.tabs(["🧾 Inputs", "📄 Results / Export"])

with tab_inputs:
    with st.form("inputs_form"):
        deal_key = st.text_input("Deal Identifier (Yardi ID or Servicer ID)", placeholder="e.g., 52874 or Servicer ID")
        advance_amount = st.number_input("Advance Amount", min_value=0.0, step=0.01, format="%.2f")

        c1, c2, c3 = st.columns(3)
        holdback_current_raw = c1.text_input("Holdback % Current (optional override)", placeholder="leave blank to use Salesforce ratio")
        holdback_closing_raw = c2.text_input("Holdback % at Closing", placeholder="100")
        advance_date_raw = c3.text_input("Advance Date", placeholder="MM/DD/YYYY")

        st.markdown("**Fees (manual):**")
        f1, f2, f3, f4 = st.columns(4)
        inspection_fee = f1.number_input("3rd party Inspection Fee", min_value=0.0, step=0.01, format="%.2f")
        wire_fee = f2.number_input("Wire Fee", min_value=0.0, step=0.01, format="%.2f")
        construction_mgmt_fee = f3.number_input("Construction Management Fee", min_value=0.0, step=0.01, format="%.2f")
        title_fee = f4.number_input("Title Fee", min_value=0.0, step=0.01, format="%.2f")

        include_late_charges = st.checkbox("Include Salesforce 'Late Fees Servicer' as HUD line item", value=False)

        submitted = st.form_submit_button("Generate HUD ✅")

    if not submitted:
        st.stop()

    deal_key = str(deal_key).strip()
    if not deal_key:
        st.error("Deal Identifier required.")
        st.stop()

    prop = find_property_by_input(sf, deal_key)
    if not prop:
        st.error("No matching Property__c found using Yardi_Id__c or Servicer_Id__c.")
        st.stop()

    deal_number_display = prop.get("Yardi_Id__c") or deal_key
    servicer_id = (prop.get("Servicer_Id__c") or "").strip()

    loan = find_loan(sf, prop.get("Loan__c")) if prop.get("Loan__c") else None
    opp = find_opportunity(sf, prop.get("Opportunity__c")) if prop.get("Opportunity__c") else None
    acct = find_account(sf, prop.get("Account__c")) if prop.get("Account__c") else None

    total_loan_amount = find_commitment_from_advance(sf, prop)
    initial_advance = float(prop.get("Initial_Disbursement_Used__c") or 0.0)
    total_reno_drawn = float(prop.get("Renovation_Advance_Amount_Used__c") or 0.0)
    interest_reserve = float(prop.get("Interest_Allocation__c") or 0.0)

    borrower_name = (prop.get("Borrower_Name__c") or "").strip().upper()
    address_full = (prop.get("Full_Address__c") or "").strip().upper()

    sf_holdback_current = ratio_to_pct_str(prop.get("Holdback_To_Rehab_Ratio__c"))
    holdback_current = normalize_pct(holdback_current_raw) if holdback_current_raw.strip() else sf_holdback_current
    holdback_closing = normalize_pct(holdback_closing_raw)

    late_fees_prop = prop.get("Late_Fees_Servicer__c")
    late_fees_opp = opp.get("Late_Fees_Servicer__c") if opp else None
    late_fees_amt = float(late_fees_prop or late_fees_opp or 0.0)

    next_payment = None
    if loan and loan.get("Next_Payment_Date__c"):
        next_payment = loan.get("Next_Payment_Date__c")
    elif opp and opp.get("Next_Payment_Date__c"):
        next_payment = opp.get("Next_Payment_Date__c")
    elif prop.get("Next_Payment_Date__c"):
        next_payment = prop.get("Next_Payment_Date__c")
    next_payment_disp = parse_date_to_mmddyyyy(next_payment) if next_payment else ""

    servicer_loan_status = (loan.get("Servicer_Loan_Status__c") if loan else "") or ""
    servicer_loan_id = (loan.get("Servicer_Loan_Id__c") if loan else "") or ""
    workday_sup_code = (acct.get("Yardi_Vendor_Code__c") if acct else "") or ""

    # OSC check + address fallback
    primary_status = ""
    if osc_df is not None and servicer_id:
        osc_match = osc_df[osc_df["account_number"].astype(str).str.strip() == servicer_id]
        if not osc_match.empty:
            primary_status = safe_first(osc_match, "primary_status", "")
            if primary_status != "Outside Policy In-Force":
                st.error("🚨 OSC Primary Status is NOT Outside Policy In-Force — reach out to the borrower.")
                st.stop()
            if not address_full:
                street = str(safe_first(osc_match, "property_street", "")).strip()
                city   = str(safe_first(osc_match, "property_city", "")).strip()
                state  = str(safe_first(osc_match, "property_state", "")).strip()
                zipc   = str(safe_first(osc_match, "property_zip", "")).strip()
                address_full = " ".join([p for p in [street, city, state, zipc] if p]).strip().upper()

    # ctx
    ctx = {
        "deal_number": deal_number_display,
        "servicer_id": servicer_id,

        "total_loan_amount": float(total_loan_amount),
        "initial_advance": float(initial_advance),
        "total_reno_drawn": float(total_reno_drawn),
        "interest_reserve": float(interest_reserve),

        "advance_amount": float(advance_amount),

        "holdback_current": holdback_current,
        "holdback_closing": holdback_closing,
        "advance_date": parse_date_to_mmddyyyy(advance_date_raw),

        "workday_sup_code": str(workday_sup_code).strip(),
        "borrower_disp": borrower_name,
        "address_disp": address_full,

        "inspection_fee": float(inspection_fee),
        "wire_fee": float(wire_fee),
        "construction_mgmt_fee": float(construction_mgmt_fee),
        "title_fee": float(title_fee),

        "accrued_late_charges_amt": float(late_fees_amt),
        "include_late_charges": bool(include_late_charges),
    }
    ctx = recompute(ctx)

    st.session_state["last_ctx"] = ctx
    st.session_state["last_snapshot"] = {
        "primary_status": primary_status,
        "next_payment_due": next_payment_disp,
        "status_enum": servicer_loan_status,
        "late_fees_amt": late_fees_amt,
        "servicer_loan_id": servicer_loan_id,
    }

with tab_results:
    if "last_ctx" not in st.session_state:
        st.info("Generate a HUD from the Inputs tab first.")
        st.stop()

    ctx = st.session_state["last_ctx"]
    snap = st.session_state.get("last_snapshot", {})

    st.subheader("Validation Snapshot")
    a, b, c, d = st.columns(4)
    a.metric("Deal", ctx.get("deal_number", ""))
    b.metric("Servicer ID", ctx.get("servicer_id", ""))
    c.metric("Next Payment Due", snap.get("next_payment_due", ""))
    d.metric("Servicer Loan Status", snap.get("status_enum", ""))

    e, f, g = st.columns(3)
    e.metric("Servicer Loan ID", snap.get("servicer_loan_id", ""))
    f.metric("Late Fees (SF)", fmt_money(snap.get("late_fees_amt", 0.0)))
    g.metric("OSC Primary Status", snap.get("primary_status", "") or "N/A")

    st.divider()
    st.subheader("HUD Preview")
    st.markdown(render_hud_html(ctx), unsafe_allow_html=True)

    st.divider()
    st.download_button(
        "⬇️ Download HUD as HTML",
        data=render_hud_html(ctx),
        file_name=f"HUD_{ctx.get('deal_number','')}.html",
        mime="text/html",
    )
