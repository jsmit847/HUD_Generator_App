# ============================================================
# DROP-IN REPLACEMENT BLOCK (ONE CELL)
# Pre-check gate BEFORE fees/inputs:
# 1) User enters Deal Number
# 2) App runs checks + shows results
# 3) Only if checks pass -> show HUD inputs (fees, advance amount, etc.)
# ============================================================

import re
import time
import html
import base64
import secrets
import hashlib
import urllib.parse
import textwrap
from datetime import datetime
import pandas as pd
import streamlit as st

from simple_salesforce import Salesforce

# -------------------------
# Page config
# -------------------------
st.set_page_config(page_title="HUD Generator", page_icon="🏗️", layout="wide")
st.title("🏗️ HUD Generator")
st.caption("Enter Deal Number → Run pre-checks → If eligible, enter fees/advance → Generate output")

# =========================================================
# Salesforce OAuth (PKCE) — minimal + robust
# IMPORTANT: redirect_uri MUST match Salesforce Connected App exactly.
# =========================================================
cfg = st.secrets["salesforce"]
CLIENT_ID = cfg["client_id"]
AUTH_HOST = cfg.get("auth_host", "https://login.salesforce.com").rstrip("/")
REDIRECT_URI = cfg["redirect_uri"].rstrip("/")  # normalize
CLIENT_SECRET = cfg.get("client_secret")  # optional (PKCE can work without)

AUTH_URL = f"{AUTH_HOST}/services/oauth2/authorize"
TOKEN_URL = f"{AUTH_HOST}/services/oauth2/token"

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

if "sf_token" not in st.session_state:
    st.session_state.sf_token = None

def exchange_code_for_token(code: str, verifier: str) -> dict:
    data = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,  # must match exactly
        "code": code,
        "code_verifier": verifier,
    }
    if CLIENT_SECRET:
        data["client_secret"] = CLIENT_SECRET
    import requests
    resp = requests.post(TOKEN_URL, data=data, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Token exchange failed ({resp.status_code}): {resp.text}")
    return resp.json()

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

# handle redirect callback
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
    with st.expander("Debug (redirect must match Connected App)"):
        st.write("AUTH_HOST:", AUTH_HOST)
        st.write("REDIRECT_URI:", REDIRECT_URI)
    st.stop()

# token -> Salesforce client
tok = st.session_state.sf_token
access_token = tok.get("access_token")
instance_url = tok.get("instance_url")
if not access_token or not instance_url:
    st.error("Token missing access_token/instance_url")
    st.json({k: tok.get(k) for k in ["instance_url", "id", "issued_at", "scope", "token_type"]})
    st.stop()

sf = Salesforce(instance_url=instance_url, session_id=access_token)

c1, c2 = st.columns([3, 1])
with c1:
    st.success("✅ Authenticated")
    st.write("instance_url:", instance_url)
with c2:
    if st.button("Log out / Clear token"):
        st.session_state.sf_token = None
        st.rerun()

# =========================================================
# Helpers — safe querying + field existence
# =========================================================
@st.cache_data(show_spinner=False, ttl=3600)
def _describe_fields(obj: str) -> set[str]:
    desc = getattr(sf, obj).describe()
    return set(f.get("name") for f in desc.get("fields", []) if f.get("name"))

def _fieldnames(_sf, obj: str) -> set[str]:
    return _describe_fields(obj)

def _filter_existing(_sf, obj: str, fields: list[str]) -> list[str]:
    existing = _fieldnames(_sf, obj)
    return [f for f in fields if f in existing]

def sf_query_all_safe(soql: str) -> list[dict]:
    try:
        res = sf.query_all(soql)
        return res.get("records", []) or []
    except Exception as e:
        # show real SOQL in an expander so you can debug quickly
        st.error("Salesforce query failed (see details).")
        with st.expander("Query + error details"):
            st.code(soql)
            st.write(str(e))
        return []

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

# =========================================================
# Deal lookup: Opportunity by deal number (your confirmed mapping)
# =========================================================
def find_opportunity_by_deal_number(deal_number: str) -> dict | None:
    deal_number = str(deal_number).strip()
    if not deal_number:
        return None

    desired_fields = [
        "Id",
        "Name",
        "AccountId",
        "Deal_Loan_Number__c",
        "Deal_Code__c",
        "Late_Fees_Servicer__c",
        "Next_Payment_Date__c",
        "Funding_Status__c",
        "Loan_Document_Status__c",
        "Loan_Audit_Status__c",
        "Servicer_Status__c",
        "Delinquency_Status_Notes__c",
    ]
    fields = _filter_existing(sf, "Opportunity", desired_fields)
    if "Id" not in fields:
        fields = ["Id"]

    where_candidates = []
    # if pasted Id
    if re.fullmatch(r"[a-zA-Z0-9]{15}([a-zA-Z0-9]{3})?", deal_number):
        where_candidates.append(f"Id = '{deal_number}'")

    # primary deal number
    if "Deal_Loan_Number__c" in _fieldnames(sf, "Opportunity"):
        where_candidates.append(f"Deal_Loan_Number__c = '{deal_number}'")

    # fallback external id
    if "Deal_Code__c" in _fieldnames(sf, "Opportunity"):
        where_candidates.append(f"Deal_Code__c = '{deal_number}'")

    # fallback name
    where_candidates.append(f"Name = '{deal_number}'")

    for w in where_candidates:
        soql = f"SELECT {', '.join(fields)} FROM Opportunity WHERE {w} ORDER BY LastModifiedDate DESC LIMIT 1"
        rows = sf_query_all_safe(soql)
        if rows:
            return rows[0]
    return None

# =========================================================
# Pre-checks
# You asked:
# - late fees check (not a front-facing line item; only a check)
# - insurance status check (you mentioned earlier)
# - show which checks ran + values
#
# We’ll check these (where available):
#   Property__c: Insurance_Status__c, Late_Fees_Servicer__c, Next_Payment_Date__c, Servicer_Id__c
#   Opportunity: Late_Fees_Servicer__c, Next_Payment_Date__c, Funding_Status__c, Servicer_Status__c, etc.
#   Loan__c: Servicer_Loan_Status__c, Servicer_Loan_Id__c, Next_Payment_Date__c
#
# NOTE: Relationship links differ by org. We attempt common ones:
#   - Property__c.Opportunity__c == Opportunity.Id   (common custom)
#   - Property__c.OpportunityId__c (less common)
# If your org uses a different field, update PROPERTY_LINK_FIELDS below.
# =========================================================
PROPERTY_LINK_FIELDS = ["Opportunity__c", "OpportunityId__c", "Deal__c", "DealId__c"]

def fetch_related_property(opp_id: str) -> dict | None:
    if not opp_id:
        return None

    desired = [
        "Id",
        "Name",
        "Borrower_Name__c",
        "Full_Address__c",
        "Yardi_Id__c",
        "Initial_Disbursement_Used__c",
        "Renovation_Advance_Amount_Used__c",
        "Interest_Allocation__c",
        "Holdback_To_Rehab_Ratio__c",
        "Insurance_Status__c",
        "HUD_Settlement_Statement_Status__c",
        "Loan_Status__c",
        "Status__c",
        "Next_Payment_Date__c",
        "Late_Fees_Servicer__c",
        "Servicer_Id__c",
    ]
    fields = _filter_existing(sf, "Property__c", desired)
    if "Id" not in fields:
        fields = ["Id"]

    existing = _fieldnames(sf, "Property__c")
    link_field = next((f for f in PROPERTY_LINK_FIELDS if f in existing), None)
    if not link_field:
        return None

    soql = f"SELECT {', '.join(fields)} FROM Property__c WHERE {link_field} = '{opp_id}' ORDER BY LastModifiedDate DESC LIMIT 1"
    rows = sf_query_all_safe(soql)
    return rows[0] if rows else None

def fetch_related_loan(opp_id: str) -> dict | None:
    if not opp_id:
        return None

    # Try common relationship fields from Loan__c -> Opportunity (adjust if needed)
    LOAN_LINK_FIELDS = ["Opportunity__c", "OpportunityId__c", "Deal__c", "DealId__c"]
    desired = [
        "Id",
        "Name",
        "Servicer_Loan_Status__c",
        "Servicer_Loan_Id__c",
        "Next_Payment_Date__c",
    ]
    fields = _filter_existing(sf, "Loan__c", desired)
    if "Id" not in fields:
        fields = ["Id"]

    existing = _fieldnames(sf, "Loan__c")
    link_field = next((f for f in LOAN_LINK_FIELDS if f in existing), None)
    if not link_field:
        return None

    soql = f"SELECT {', '.join(fields)} FROM Loan__c WHERE {link_field} = '{opp_id}' ORDER BY LastModifiedDate DESC LIMIT 1"
    rows = sf_query_all_safe(soql)
    return rows[0] if rows else None

def run_prechecks(deal_number: str) -> dict:
    """
    Returns dict with:
      - ok (bool)
      - opp, prop, loan (records or None)
      - checks (list of dict rows for display)
      - blockers (list[str]) reasons that must be resolved before HUD entry
    """
    opp = find_opportunity_by_deal_number(deal_number)
    if not opp:
        return {
            "ok": False,
            "opp": None, "prop": None, "loan": None,
            "checks": [{"Check": "Deal lookup", "Object": "Opportunity", "Field": "Deal_Loan_Number__c", "Value": "(not found)"}],
            "blockers": ["Deal Number not found in Salesforce Opportunity."],
        }

    opp_id = opp.get("Id")
    prop = fetch_related_property(opp_id)
    loan = fetch_related_loan(opp_id)

    checks = []
    blockers = []

    # Helper to push checks safely
    def add_check(name, obj, field, value):
        checks.append({"Check": name, "Object": obj, "Field": field, "Value": value})

    # --- Late fees check (prefer Property, fallback Opportunity) ---
    late_val = None
    late_src = None
    if prop and "Late_Fees_Servicer__c" in prop:
        late_val = prop.get("Late_Fees_Servicer__c")
        late_src = "Property__c"
    elif "Late_Fees_Servicer__c" in opp:
        late_val = opp.get("Late_Fees_Servicer__c")
        late_src = "Opportunity"

    if late_src:
        add_check("Late fees present?", late_src, "Late_Fees_Servicer__c", fmt_money(parse_money(late_val)))
        if parse_money(late_val) > 0:
            blockers.append("Late fees are non-zero. Resolve/confirm before generating HUD.")
    else:
        add_check("Late fees present?", "(n/a)", "Late_Fees_Servicer__c", "Field not found / not linked")

    # --- Insurance status check (Property__c) ---
    if prop and "Insurance_Status__c" in prop:
        ins = str(prop.get("Insurance_Status__c") or "").strip()
        add_check("Insurance status", "Property__c", "Insurance_Status__c", ins if ins else "(blank)")
        # You did not specify the exact pass value here, so we only flag blank
        if ins == "":
            blockers.append("Insurance Status is blank. Confirm insurance before HUD.")
    else:
        add_check("Insurance status", "(n/a)", "Insurance_Status__c", "Property not found / field missing")

    # --- Next payment date check (Loan__c preferred, fallback Property/Opportunity) ---
    npd = None
    npd_src = None
    if loan and "Next_Payment_Date__c" in loan:
        npd = loan.get("Next_Payment_Date__c")
        npd_src = "Loan__c"
    elif prop and "Next_Payment_Date__c" in prop:
        npd = prop.get("Next_Payment_Date__c")
        npd_src = "Property__c"
    elif "Next_Payment_Date__c" in opp:
        npd = opp.get("Next_Payment_Date__c")
        npd_src = "Opportunity"

    if npd_src:
        add_check("Next payment date", npd_src, "Next_Payment_Date__c", str(npd or "(blank)"))
    else:
        add_check("Next payment date", "(n/a)", "Next_Payment_Date__c", "Not available")

    # --- Servicer loan status check (Loan__c) ---
    if loan and "Servicer_Loan_Status__c" in loan:
        sls = str(loan.get("Servicer_Loan_Status__c") or "").strip()
        add_check("Servicer loan status", "Loan__c", "Servicer_Loan_Status__c", sls if sls else "(blank)")
        # You asked about values like foreclosure / performing earlier:
        if sls and any(x in sls.lower() for x in ["foreclosure", "foreclose"]):
            blockers.append("Servicer loan status indicates foreclosure. Confirm eligibility before HUD.")
    else:
        add_check("Servicer loan status", "(n/a)", "Servicer_Loan_Status__c", "Loan not found / field missing")

    # --- Basic found-objects summary checks ---
    add_check("Opportunity found", "Opportunity", "Id", opp_id)
    add_check("Property linked", "Property__c", "Id", prop.get("Id") if prop else "(not found)")
    add_check("Loan linked", "Loan__c", "Id", loan.get("Id") if loan else "(not found)")

    ok = (len(blockers) == 0)
    return {"ok": ok, "opp": opp, "prop": prop, "loan": loan, "checks": checks, "blockers": blockers}

# =========================================================
# UI — Step 1: Deal Number -> run checks
# =========================================================
st.divider()
st.subheader("Step 1 — Enter Deal Number & run eligibility checks")

with st.form("deal_form"):
    deal_number = st.text_input("Deal Number", placeholder="e.g., 52874")
    run_checks = st.form_submit_button("Run Checks ✅")

if not run_checks:
    st.stop()

deal_number = str(deal_number).strip()
if not deal_number:
    st.error("Deal Number is required.")
    st.stop()

result = run_prechecks(deal_number)

st.subheader("Pre-check Results")
checks_df = pd.DataFrame(result["checks"])
st.dataframe(checks_df, use_container_width=True, hide_index=True)

if result["blockers"]:
    st.error("❌ Not eligible to proceed yet. Fix/confirm the following:")
    for b in result["blockers"]:
        st.write(f"- {b}")
    st.info("Once fixed, re-run checks with the same Deal Number.")
    st.stop()

st.success("✅ All checks passed — you can proceed to HUD inputs.")

# =========================================================
# Step 2: HUD inputs (only after checks pass)
# - You asked: Workday SUP Code should be manual for now
# - We also pull known HUD fields from Property__c / Opportunity if present
# =========================================================
st.divider()
st.subheader("Step 2 — Enter HUD inputs (now that deal passed checks)")

opp = result["opp"] or {}
prop = result["prop"] or {}

def get_val(rec: dict, key: str, default=None):
    return rec.get(key, default) if isinstance(rec, dict) else default

# From your earlier mapping:
# Initial Disbursement Funded -> Initial_Disbursement_Used__c (Property__c)
# Total Reno Drawn -> Renovation_Advance_Amount_Used__c (Property__c)
# Interest Reserve -> Interest_Allocation__c (Property__c)
# Holdback % Current -> Holdback_To_Rehab_Ratio__c (Property__c) (display as %)
# Borrower Name -> Borrower_Name__c (Property__c)
# Address -> Full_Address__c (Property__c)
# Loan Commitment -> LOC_Commitment__c (Advance__c) (not implemented here; user can still enter total loan amount manually OR extend later)
# Yardi ID -> Yardi_Id__c (Property__c)

initial_advance_sf = parse_money(get_val(prop, "Initial_Disbursement_Used__c", 0))
total_reno_sf = parse_money(get_val(prop, "Renovation_Advance_Amount_Used__c", 0))
interest_reserve_sf = parse_money(get_val(prop, "Interest_Allocation__c", 0))
holdback_ratio_raw = get_val(prop, "Holdback_To_Rehab_Ratio__c", "")
borrower_name_sf = str(get_val(prop, "Borrower_Name__c", "") or "").strip().upper()
address_sf = str(get_val(prop, "Full_Address__c", "") or "").strip().upper()
yardi_id_sf = str(get_val(prop, "Yardi_Id__c", "") or "").strip()

# Inputs
with st.form("hud_inputs"):
    st.markdown("**Core amounts**")
    c1, c2, c3 = st.columns(3)
    total_loan_amount = c1.number_input("Total Loan Amount", min_value=0.0, step=0.01, format="%.2f")
    advance_amount = c2.number_input("Advance Amount (this draw)", min_value=0.0, step=0.01, format="%.2f")
    workday_sup_code = c3.text_input("Workday SUP Code (manual for now)")

    st.markdown("**Prefilled (from Salesforce, editable)**")
    d1, d2, d3 = st.columns(3)
    initial_advance = d1.number_input("Initial Advance", min_value=0.0, step=0.01, format="%.2f", value=float(initial_advance_sf))
    total_reno_drawn = d2.number_input("Total Reno Drawn", min_value=0.0, step=0.01, format="%.2f", value=float(total_reno_sf))
    interest_reserve = d3.number_input("Interest Reserve", min_value=0.0, step=0.01, format="%.2f", value=float(interest_reserve_sf))

    e1, e2 = st.columns(2)
    borrower_disp = e1.text_input("Borrower", value=borrower_name_sf)
    address_disp = e2.text_input("Address", value=address_sf)

    f1, f2 = st.columns(2)
    yardi_id = f1.text_input("Yardi ID", value=yardi_id_sf)
    holdback_current_raw = f2.text_input("Holdback % Current", value=str(holdback_ratio_raw or ""))

    holdback_closing_raw = st.text_input("Holdback % at Closing", value="")  # keep manual unless you later map it

    st.markdown("**Fees (manual)**")
    g1, g2, g3, g4 = st.columns(4)
    inspection_fee = g1.number_input("3rd party Inspection Fee", min_value=0.0, step=0.01, format="%.2f")
    wire_fee = g2.number_input("Wire Fee", min_value=0.0, step=0.01, format="%.2f")
    construction_mgmt_fee = g3.number_input("Construction Management Fee", min_value=0.0, step=0.01, format="%.2f")
    title_fee = g4.number_input("Title Fee", min_value=0.0, step=0.01, format="%.2f")

    advance_date_raw = st.text_input("Advance Date", placeholder="MM/DD/YYYY")
    submitted = st.form_submit_button("Generate HUD Preview ✅")

if not submitted:
    st.stop()

# =========================================================
# Compute + preview (still HTML preview here; you can swap to Excel later)
# =========================================================
def recompute(ctx: dict) -> dict:
    ctx["allocated_loan_amount"] = float(ctx.get("advance_amount", 0.0)) + float(ctx.get("total_reno_drawn", 0.0))
    ctx["construction_advance_amount"] = float(ctx.get("advance_amount", 0.0))
    fee_keys = ["inspection_fee", "wire_fee", "construction_mgmt_fee", "title_fee"]
    ctx["total_fees"] = sum(float(ctx.get(k, 0.0)) for k in fee_keys)
    ctx["net_amount_to_borrower"] = ctx["construction_advance_amount"] - ctx["total_fees"]
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

    borrower_disp_esc = html.escape(str(ctx.get("borrower_disp", "") or ""))
    address_disp_esc = html.escape(str(ctx.get("address_disp", "") or ""))
    workday_sup_code_esc = html.escape(str(ctx.get("workday_sup_code", "") or ""))
    advance_date_esc = html.escape(str(ctx.get("advance_date", "") or ""))

    hb_current = html.escape(str(ctx.get("holdback_current", "") or ""))
    hb_closing = html.escape(str(ctx.get("holdback_closing", "") or ""))

    html_str = f"""
<style>
  .hud-page {{ width: 980px; font-family: Arial, Helvetica, sans-serif; font-size: 13px; color: #000; }}
  .hud-top {{ text-align: center; margin-bottom: 10px; line-height: 1.25; }}
  .hud-top .c1 {{ font-weight: 700; }}
  .hud-top .c3 {{ font-weight: 800; font-size: 16px; }}
  .hud-box {{ border: 2px solid #000; padding: 10px; }}
  table.hud {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
  table.hud td {{ border: 0; padding: 4px 6px; vertical-align: middle; }}
  .grid {{ border: 1px solid #d0d0d0; }}
  .lbl {{ font-weight: 700; text-align: left; width: 24%; }}
  .val {{ text-align: right; width: 26%; white-space: nowrap; }}
  .rlbl {{ font-weight: 700; text-align: left; width: 24%; }}
  .rval {{ text-align: right; width: 26%; white-space: nowrap; }}
  .borrower-line {{ border-top: 2px solid #000; margin-top: 10px; padding-top: 8px; }}
  .addr-line {{ margin-top: 2px; }}
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
        <td class="rlbl">Workday SUP Code:</td><td class="rval grid">{workday_sup_code_esc}</td>
      </tr>
      <tr>
        <td class="lbl"></td><td class="val"></td>
        <td class="rlbl">Advance Date:</td><td class="rval grid"><b>{advance_date_esc}</b></td>
      </tr>
    </table>

    <div class="borrower-line">
      <div><b>Borrower:</b> {borrower_disp_esc}</div>
      <div class="addr-line"><b>Address:</b> {address_disp_esc}</div>
    </div>
  </div>
</div>
"""
    return textwrap.dedent(html_str).strip()

ctx = {
    "deal_number": deal_number,
    "total_loan_amount": float(total_loan_amount),
    "initial_advance": float(initial_advance),
    "total_reno_drawn": float(total_reno_drawn),
    "advance_amount": float(advance_amount),
    "interest_reserve": float(interest_reserve),
    "holdback_current": normalize_pct(holdback_current_raw),
    "holdback_closing": normalize_pct(holdback_closing_raw),
    "workday_sup_code": str(workday_sup_code).strip(),
    "advance_date": parse_date_to_mmddyyyy(advance_date_raw),
    "borrower_disp": str(borrower_disp).strip().upper(),
    "address_disp": str(address_disp).strip().upper(),
    "inspection_fee": float(inspection_fee),
    "wire_fee": float(wire_fee),
    "construction_mgmt_fee": float(construction_mgmt_fee),
    "title_fee": float(title_fee),
}

ctx = recompute(ctx)

st.divider()
st.subheader("HUD Preview")
st.markdown(render_hud_html(ctx), unsafe_allow_html=True)
