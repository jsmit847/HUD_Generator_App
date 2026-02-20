# =========================
# HUD GENERATOR (APP.PY) — TEST-STYLE OAUTH PKCE + SALESFORCE DATA
# No Hayden. No FCI. OSC + CAF from repo files.
# =========================
import re
import html
import textwrap
import base64
import hashlib
import secrets
import urllib.parse
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
import streamlit as st
from simple_salesforce import Salesforce

# -------------------------
# Page
# -------------------------
st.set_page_config(page_title="HUD Generator", page_icon="🏗️", layout="wide")
st.title("🏗️ HUD Generator")
st.caption("Salesforce-only deal data (no Hayden/FCI). OSC + CAF loaded from repo.")

# -------------------------
# Repo files (must exist in repo root)
# -------------------------
CAF_PATH = Path("Corevest_CAF National 52874_2.10.xlsx")
OSC_PATH = Path("OSC_Zstatus_COREVEST_2026-02-17_180520.xlsx")

# -------------------------
# Helpers
# -------------------------
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

def require_cols(df: pd.DataFrame, cols: list[str], dataset_name: str):
    missing = [c for c in cols if c not in df.columns]
    if missing:
        st.error(
            f"Missing expected column(s) in **{dataset_name}**: "
            + ", ".join([f"`{m}`" for m in missing])
        )
        st.stop()

def recompute(ctx: dict) -> dict:
    # Allocated Loan Amount = Advance Amount + Total Reno Drawn
    ctx["allocated_loan_amount"] = float(ctx.get("advance_amount", 0.0)) + float(ctx.get("total_reno_drawn", 0.0))

    # Construction Advance Amount = Advance Amount
    ctx["construction_advance_amount"] = float(ctx.get("advance_amount", 0.0))

    # Fees
    fee_keys = ["inspection_fee", "wire_fee", "construction_mgmt_fee", "title_fee"]
    ctx["total_fees"] = sum(float(ctx.get(k, 0.0)) for k in fee_keys)

    # Optional late charges line item (SF Late Fees)
    include_lates = bool(ctx.get("include_late_charges", False))
    late_charges = float(ctx.get("accrued_late_charges_amt", 0.0))
    ctx["late_charges_line_item"] = late_charges if include_lates else 0.0

    # Net Amount to Borrower
    ctx["net_amount_to_borrower"] = ctx["construction_advance_amount"] - ctx["total_fees"] - ctx["late_charges_line_item"]

    # Available Balance (same logic you had)
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
    {"<tr><td>Late Fees (Servicer)</td><td>" + fmt_money(ctx.get("late_charges_line_item", 0.0)) + "</td></tr>" if show_lates else ""}
    <tr class="tot"><td>Total Fees</td><td>{fmt_money(ctx.get("total_fees", 0.0) + (ctx.get("late_charges_line_item",0.0) if show_lates else 0.0))}</td></tr>
    <tr class="tot"><td>Reimbursement to Borrower</td><td>{fmt_money(ctx.get("net_amount_to_borrower", 0.0))}</td></tr>
  </table>
</div>
"""
    return textwrap.dedent(html_str).strip()

# -------------------------
# Secrets (same as your test)
# -------------------------
cfg = st.secrets["salesforce"]
CLIENT_ID = cfg["client_id"]
AUTH_HOST = cfg.get("auth_host", "https://login.salesforce.com").rstrip("/")
REDIRECT_URI = cfg["redirect_uri"]
CLIENT_SECRET = cfg.get("client_secret")  # optional
API_VERSION = str(cfg.get("api_version", "59.0"))

AUTH_URL = f"{AUTH_HOST}/services/oauth2/authorize"
TOKEN_URL = f"{AUTH_HOST}/services/oauth2/token"

# -------------------------
# PKCE helpers (same as your test)
# -------------------------
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

# -------------------------
# OAuth flow (same structure)
# -------------------------
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
    st.link_button("Login to Salesforce", login_url, use_container_width=True)
    with st.expander("Debug"):
        st.write("AUTH_HOST:", AUTH_HOST)
        st.write("REDIRECT_URI:", REDIRECT_URI)
    st.stop()

# -------------------------
# Token -> Salesforce client
# -------------------------
tok = st.session_state.sf_token
access_token = tok.get("access_token")
instance_url = tok.get("instance_url")
id_url = tok.get("id")

if not access_token or not instance_url:
    st.error("Token missing access_token/instance_url")
    st.json({k: tok.get(k) for k in ["instance_url", "id", "issued_at", "scope", "token_type"]})
    st.stop()

sf = Salesforce(instance_url=instance_url, session_id=access_token, version=API_VERSION)

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
        try:
            st.json(r.json())
        except Exception:
            st.write(r.text)

# -------------------------
# Load OSC + CAF from repo
# -------------------------
@st.cache_data(show_spinner=False)
def load_osc(path: Path) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name="COREVEST", dtype=str)
    df = norm(df)
    require_cols(df, ["account_number", "primary_status"], "OSC ZStatus")
    return df

@st.cache_data(show_spinner=False)
def load_caf(path: Path) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name=0, dtype=str)
    return norm(df)

osc_df = None
caf_df = None

if OSC_PATH.exists():
    osc_df = load_osc(OSC_PATH)
else:
    st.warning(f"OSC file not found: {OSC_PATH}")

if CAF_PATH.exists():
    caf_df = load_caf(CAF_PATH)
else:
    st.warning(f"CAF file not found: {CAF_PATH}")

# -------------------------
# SOQL helpers
# -------------------------
def soql_quote(s: str) -> str:
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"

def sf_one(soql: str) -> dict | None:
    res = sf.query(soql)
    recs = res.get("records", [])
    return recs[0] if recs else None

def find_property_by_key(user_key: str) -> dict | None:
    q = user_key.strip()
    if not q:
        return None

    base = """
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

    # exact Yardi
    rec = sf_one(f"{base} WHERE Yardi_Id__c = {soql_quote(q)} LIMIT 1")
    if rec:
        return rec
    # exact Servicer Id
    rec = sf_one(f"{base} WHERE Servicer_Id__c = {soql_quote(q)} LIMIT 1")
    if rec:
        return rec
    # fallback: partial on Yardi
    like = "%" + q.replace("%", "\\%") + "%"
    rec = sf_one(f"{base} WHERE Yardi_Id__c LIKE {soql_quote(like)} LIMIT 1")
    return rec

def find_loan(loan_id: str) -> dict | None:
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
    return sf_one(soql)

def find_opportunity(opp_id: str) -> dict | None:
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
    return sf_one(soql)

def find_account(acct_id: str) -> dict | None:
    if not acct_id:
        return None
    soql = f"""
    SELECT Id, Name,
           Yardi_Vendor_Code__c
    FROM Account
    WHERE Id = {soql_quote(acct_id)}
    LIMIT 1
    """.strip()
    return sf_one(soql)

def find_commitment_from_advance(property_rec: dict) -> float:
    # Try the newest Advance__c related to the property/opportunity
    prop_id = property_rec.get("Id")
    opp_id = property_rec.get("Opportunity__c")

    candidates = [
        ("Property__c", prop_id),
        ("Opportunity__c", opp_id),
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
        rec = sf_one(soql)
        if rec and rec.get("LOC_Commitment__c") is not None:
            return float(rec.get("LOC_Commitment__c") or 0.0)
    return 0.0

# -------------------------
# UI
# -------------------------
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

    prop = find_property_by_key(deal_key)
    if not prop:
        st.error("No matching Property__c found using Yardi_Id__c or Servicer_Id__c.")
        st.stop()

    deal_number_display = prop.get("Yardi_Id__c") or deal_key
    servicer_id = (prop.get("Servicer_Id__c") or "").strip()

    loan = find_loan(prop.get("Loan__c")) if prop.get("Loan__c") else None
    opp  = find_opportunity(prop.get("Opportunity__c")) if prop.get("Opportunity__c") else None
    acct = find_account(prop.get("Account__c")) if prop.get("Account__c") else None

    # HUD numbers from SF fields you provided
    total_loan_amount = find_commitment_from_advance(prop)                    # LOC_Commitment__c on Advance__c (latest related)
    initial_advance   = float(prop.get("Initial_Disbursement_Used__c") or 0)  # Property__c
    total_reno_drawn  = float(prop.get("Renovation_Advance_Amount_Used__c") or 0)  # Property__c
    interest_reserve  = float(prop.get("Interest_Allocation__c") or 0)        # Property__c

    borrower_name = (prop.get("Borrower_Name__c") or "").strip().upper()
    address_full  = (prop.get("Full_Address__c") or "").strip().upper()

    # Holdback current from SF ratio unless overridden
    sf_holdback_current = ratio_to_pct_str(prop.get("Holdback_To_Rehab_Ratio__c"))
    holdback_current = normalize_pct(holdback_current_raw) if holdback_current_raw.strip() else sf_holdback_current
    holdback_closing = normalize_pct(holdback_closing_raw)

    # Late fees (prefer Property; fallback Opportunity)
    late_prop = prop.get("Late_Fees_Servicer__c")
    late_opp  = opp.get("Late_Fees_Servicer__c") if opp else None
    late_fees_amt = float(late_prop or late_opp or 0.0)

    # Next payment date (prefer Loan; fallback Opp; fallback Property)
    next_payment = None
    if loan and loan.get("Next_Payment_Date__c"):
        next_payment = loan.get("Next_Payment_Date__c")
    elif opp and opp.get("Next_Payment_Date__c"):
        next_payment = opp.get("Next_Payment_Date__c")
    elif prop.get("Next_Payment_Date__c"):
        next_payment = prop.get("Next_Payment_Date__c")
    next_payment_disp = parse_date_to_mmddyyyy(next_payment) if next_payment else ""

    # "StatusEnum" replacement -> Servicer Loan Status (Loan__c)
    servicer_loan_status = (loan.get("Servicer_Loan_Status__c") if loan else "") or ""
    servicer_loan_id = (loan.get("Servicer_Loan_Id__c") if loan else "") or ""

    # Workday SUP Code replacement -> Yardi Vendor Code on Account (you asked this)
    workday_sup_code = (acct.get("Yardi_Vendor_Code__c") if acct else "") or ""

    # OSC policy check
    primary_status = ""
    if osc_df is not None and servicer_id:
        osc_match = osc_df[osc_df["account_number"].astype(str).str.strip() == servicer_id]
        if not osc_match.empty:
            primary_status = (osc_match["primary_status"].iloc[0] or "")
            if primary_status != "Outside Policy In-Force":
                st.error("🚨 OSC Primary Status is NOT Outside Policy In-Force — reach out to the borrower.")
                st.stop()

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

        # "accruedlatecharges" replacement -> Late Fees Servicer (SF)
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
