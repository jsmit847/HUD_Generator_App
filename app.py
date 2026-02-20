# ============================================================
# HUD GENERATOR — ONE-CELL DROP-IN (NON-TECH FRIENDLY)
# Flow:
# 1) Login to Salesforce
# 2) Enter Deal Number -> run PRE-CHECKS (shows plain-English results)
# 3) Only if checks pass -> show HUD inputs
# Notes:
# - Precheck table shows: Check | Result | Details | What to do
# - No "object/field" shown to users
# - Money inputs are TEXT fields that accept $ / commas / parentheses and DISPLAY formatted ($#,###.##)
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
st.caption("Enter Deal Number → Run checks → If eligible, enter fees & generate HUD preview")

# =========================================================
# Helpers (money, pct, date)
# =========================================================
def parse_money(val) -> float:
    if val is None:
        return 0.0
    s = str(val).strip()
    if s == "" or s.lower() in {"nan", "none"}:
        return 0.0
    s = s.replace("$", "").replace(",", "").strip()
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    try:
        x = float(s)
    except Exception:
        raise ValueError(f"Could not read money amount: {val}")
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

def money_text_input(label: str, key: str, default_float: float = 0.0, help_text: str | None = None) -> float:
    """
    Non-tech-friendly money entry:
    - User types: 1234.5 or $1,234.50 or (123.45)
    - We show formatted value right below after parsing
    """
    if key not in st.session_state:
        st.session_state[key] = fmt_money(default_float)

    raw = st.text_input(label, key=key, value=st.session_state[key], help=help_text)
    try:
        val = parse_money(raw)
        st.caption(f"Formatted: **{fmt_money(val)}**")
        st.session_state[key] = fmt_money(val)  # normalize stored display
        return float(val)
    except Exception as e:
        st.error(str(e))
        return float(default_float)

# =========================================================
# Salesforce OAuth (PKCE) — minimal
# IMPORTANT: redirect_uri MUST match Salesforce Connected App exactly.
# =========================================================
cfg = st.secrets["salesforce"]
CLIENT_ID = cfg["client_id"]
AUTH_HOST = cfg.get("auth_host", "https://login.salesforce.com").rstrip("/")
REDIRECT_URI = cfg["redirect_uri"].rstrip("/")  # normalize
CLIENT_SECRET = cfg.get("client_secret")  # optional (your org may require)

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
    import requests
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
        st.error("Login expired. Please click Login again.")
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

    st.info("Step 1: Login to Salesforce.")
    st.link_button("Login to Salesforce", login_url)
    with st.expander("If you see redirect_uri_mismatch"):
        st.write("The Redirect URI in Salesforce Connected App must match EXACTLY:")
        st.code(REDIRECT_URI)
    st.stop()

# token -> Salesforce client
tok = st.session_state.sf_token
access_token = tok.get("access_token")
instance_url = tok.get("instance_url")
if not access_token or not instance_url:
    st.error("Salesforce login token is missing required fields. Please log out and log in again.")
    st.stop()

sf = Salesforce(instance_url=instance_url, session_id=access_token)

h1, h2 = st.columns([3, 1])
with h1:
    st.success("✅ Connected to Salesforce")
with h2:
    if st.button("Log out"):
        st.session_state.sf_token = None
        st.rerun()

# =========================================================
# Safe Salesforce helpers
# =========================================================
@st.cache_data(show_spinner=False, ttl=3600)
def _describe_fields(obj: str) -> set[str]:
    desc = getattr(sf, obj).describe()
    return set(f.get("name") for f in desc.get("fields", []) if f.get("name"))

def _filter_existing(obj: str, fields: list[str]) -> list[str]:
    existing = _describe_fields(obj)
    return [f for f in fields if f in existing]

def sf_query_one(soql: str) -> dict | None:
    try:
        res = sf.query_all(soql)
        rows = res.get("records", []) or []
        return rows[0] if rows else None
    except Exception as e:
        st.error("Salesforce query failed. (See details)")
        with st.expander("Details (for troubleshooting)"):
            st.code(soql)
            st.write(str(e))
        return None

# =========================================================
# Deal lookup (Opportunity) by Deal Number
# =========================================================
def find_opportunity_by_deal_number(deal_number: str) -> dict | None:
    deal_number = str(deal_number).strip()
    if not deal_number:
        return None

    desired_fields = [
        "Id", "Name", "Deal_Loan_Number__c", "Deal_Code__c",
        "Next_Payment_Date__c", "Late_Fees_Servicer__c",
        "Funding_Status__c", "Servicer_Status__c", "Loan_Document_Status__c",
        "Loan_Audit_Status__c", "Delinquency_Status_Notes__c"
    ]
    fields = _filter_existing("Opportunity", desired_fields)
    if "Id" not in fields:
        fields = ["Id"]

    where_clauses = []

    # if pasted Salesforce Id
    if re.fullmatch(r"[a-zA-Z0-9]{15}([a-zA-Z0-9]{3})?", deal_number):
        where_clauses.append(f"Id = '{deal_number}'")

    if "Deal_Loan_Number__c" in _describe_fields("Opportunity"):
        where_clauses.append(f"Deal_Loan_Number__c = '{deal_number}'")

    if "Deal_Code__c" in _describe_fields("Opportunity"):
        where_clauses.append(f"Deal_Code__c = '{deal_number}'")

    where_clauses.append(f"Name = '{deal_number}'")

    for w in where_clauses:
        soql = f"SELECT {', '.join(fields)} FROM Opportunity WHERE {w} ORDER BY LastModifiedDate DESC LIMIT 1"
        hit = sf_query_one(soql)
        if hit:
            return hit
    return None

# =========================================================
# Related Property / Loan (best-effort)
# NOTE: if your org uses a different link field, update these lists.
# =========================================================
PROPERTY_LINK_FIELDS = ["Opportunity__c", "OpportunityId__c", "Deal__c", "DealId__c"]
LOAN_LINK_FIELDS = ["Opportunity__c", "OpportunityId__c", "Deal__c", "DealId__c"]

def fetch_related_property(opp_id: str) -> dict | None:
    existing = _describe_fields("Property__c")
    link_field = next((f for f in PROPERTY_LINK_FIELDS if f in existing), None)
    if not link_field:
        return None

    desired = [
        "Id", "Name",
        "Borrower_Name__c", "Full_Address__c",
        "Initial_Disbursement_Used__c", "Renovation_Advance_Amount_Used__c", "Interest_Allocation__c",
        "Holdback_To_Rehab_Ratio__c",
        "Insurance_Status__c",
        "Late_Fees_Servicer__c",
        "Next_Payment_Date__c",
        "Servicer_Id__c",
        "Yardi_Id__c",
    ]
    fields = _filter_existing("Property__c", desired)
    if "Id" not in fields:
        fields = ["Id"]

    soql = f"SELECT {', '.join(fields)} FROM Property__c WHERE {link_field} = '{opp_id}' ORDER BY LastModifiedDate DESC LIMIT 1"
    return sf_query_one(soql)

def fetch_related_loan(opp_id: str) -> dict | None:
    existing = _describe_fields("Loan__c")
    link_field = next((f for f in LOAN_LINK_FIELDS if f in existing), None)
    if not link_field:
        return None

    desired = ["Id", "Name", "Servicer_Loan_Status__c", "Servicer_Loan_Id__c", "Next_Payment_Date__c"]
    fields = _filter_existing("Loan__c", desired)
    if "Id" not in fields:
        fields = ["Id"]

    soql = f"SELECT {', '.join(fields)} FROM Loan__c WHERE {link_field} = '{opp_id}' ORDER BY LastModifiedDate DESC LIMIT 1"
    return sf_query_one(soql)

# =========================================================
# Prechecks (plain English)
# Matches your “what mattered” idea:
# - Servicer ID found (we’ll display it if present)
# - Next Payment Due
# - Status Enum (Salesforce doesn't have FCI statusenum; we’ll show Servicer Loan Status instead)
# - Late Fees (as a check)
# - Insurance Status (as a check)
# (OSC “Outside Policy In-Force” was from spreadsheet; if you still want it later, we can add a local file lookup.)
# =========================================================
def run_prechecks(deal_number: str) -> dict:
    checks = []
    blockers = []

    def add(check_name: str, result: str, details: str = "", what_to_do: str = ""):
        checks.append({
            "Check": check_name,
            "Result": result,
            "Details": details,
            "What to do": what_to_do
        })

    opp = find_opportunity_by_deal_number(deal_number)
    if not opp:
        add(
            "Deal found in Salesforce",
            "❌ Not found",
            f"Deal Number {deal_number} was not found.",
            "Double-check the Deal Number and try again."
        )
        return {"ok": False, "opp": None, "prop": None, "loan": None, "checks": checks, "blockers": ["Deal not found"]}

    add("Deal found in Salesforce", "✅ Found", f"Deal: {opp.get('Name','')}".strip(), "")

    opp_id = opp.get("Id")
    prop = fetch_related_property(opp_id) if opp_id else None
    loan = fetch_related_loan(opp_id) if opp_id else None

    # Servicer ID (preferred from Property__c.Servicer_Id__c, fallback Loan__c.Servicer_Loan_Id__c)
    servicer_id = ""
    if prop and "Servicer_Id__c" in prop and str(prop.get("Servicer_Id__c") or "").strip():
        servicer_id = str(prop.get("Servicer_Id__c")).strip()
    elif loan and "Servicer_Loan_Id__c" in loan and str(loan.get("Servicer_Loan_Id__c") or "").strip():
        servicer_id = str(loan.get("Servicer_Loan_Id__c")).strip()

    add("Servicer ID", "✅ Found" if servicer_id else "⚠️ Missing", servicer_id if servicer_id else "Not available from Salesforce links.", "If missing, confirm the deal’s servicing info in Salesforce.")

    # Next Payment Date (Loan preferred, fallback Property, fallback Opportunity)
    npd = ""
    if loan and "Next_Payment_Date__c" in loan and loan.get("Next_Payment_Date__c"):
        npd = str(loan.get("Next_Payment_Date__c"))
    elif prop and "Next_Payment_Date__c" in prop and prop.get("Next_Payment_Date__c"):
        npd = str(prop.get("Next_Payment_Date__c"))
    elif "Next_Payment_Date__c" in opp and opp.get("Next_Payment_Date__c"):
        npd = str(opp.get("Next_Payment_Date__c"))

    add("Next Payment Date", "✅ Found" if npd else "⚠️ Missing", npd if npd else "Blank / not available.", "Confirm Next Payment Date is populated.")

    # “Status Enum” replacement (Servicer Loan Status)
    sls = ""
    if loan and "Servicer_Loan_Status__c" in loan and loan.get("Servicer_Loan_Status__c"):
        sls = str(loan.get("Servicer_Loan_Status__c")).strip()

    if sls:
        add("Servicer Loan Status", "✅ Found", sls, "")
        if any(x in sls.lower() for x in ["foreclosure", "foreclose"]):
            blockers.append("Foreclosure status")
            add("Foreclosure Check", "❌ Flagged", "Loan status suggests foreclosure.", "Confirm with servicing / management before proceeding.")
        else:
            add("Foreclosure Check", "✅ Clear", "No foreclosure keywords detected.", "")
    else:
        add("Servicer Loan Status", "⚠️ Missing", "Not available.", "Confirm Servicer Loan Status is populated in Salesforce.")
        add("Foreclosure Check", "⚠️ Not run", "Missing Servicer Loan Status.", "Populate Servicer Loan Status and rerun checks.")

    # Insurance status (Property__c)
    ins = ""
    if prop and "Insurance_Status__c" in prop and prop.get("Insurance_Status__c"):
        ins = str(prop.get("Insurance_Status__c")).strip()
    if ins:
        add("Insurance Status", "✅ Found", ins, "")
    else:
        add("Insurance Status", "⚠️ Missing", "Blank / not available.", "Confirm Insurance Status is populated before HUD.")

    # Late fees (Property preferred, else Opportunity)
    late = None
    if prop and "Late_Fees_Servicer__c" in prop:
        late = prop.get("Late_Fees_Servicer__c")
    elif "Late_Fees_Servicer__c" in opp:
        late = opp.get("Late_Fees_Servicer__c")

    late_amt = parse_money(late) if late not in (None, "") else 0.0
    if late is None:
        add("Late Fees Check", "⚠️ Not available", "Late fee field not available via current Salesforce links.", "Confirm late fees in Salesforce before HUD.")
    else:
        if late_amt > 0:
            blockers.append("Late fees")
            add("Late Fees Check", "❌ Flagged", f"Late Fees: {fmt_money(late_amt)}", "Resolve/confirm late fees before proceeding.")
        else:
            add("Late Fees Check", "✅ Clear", "Late Fees: $0.00", "")

    ok = (len(blockers) == 0)
    return {"ok": ok, "opp": opp, "prop": prop, "loan": loan, "checks": checks, "blockers": blockers, "servicer_id": servicer_id}

# =========================================================
# UI — Step 1: Deal Number -> run checks
# =========================================================
st.divider()
st.subheader("Step 1 — Enter Deal Number")

with st.form("deal_form"):
    deal_number = st.text_input("Deal Number", placeholder="Example: 52874")
    run_checks_btn = st.form_submit_button("Run Checks ✅")

if not run_checks_btn:
    st.stop()

deal_number = str(deal_number).strip()
if not deal_number:
    st.error("Please enter a Deal Number.")
    st.stop()

result = run_prechecks(deal_number)

st.subheader("Checks & Results")
checks_df = pd.DataFrame(result["checks"])
st.dataframe(checks_df, use_container_width=True, hide_index=True)

if not result["ok"]:
    st.error("❌ This deal needs attention before a HUD can be started.")
    st.info("Fix/confirm the items marked ❌ or ⚠️, then rerun checks.")
    st.stop()

st.success("✅ All checks passed — you can now enter fees and HUD inputs.")

# =========================================================
# Step 2: HUD Inputs (money shows as $ with commas)
# For now: Workday SUP Code manual input (per your request)
# =========================================================
st.divider()
st.subheader("Step 2 — HUD Inputs")

opp = result["opp"] or {}
prop = result["prop"] or {}

def rec_val(rec: dict, key: str, default=""):
    return rec.get(key, default) if isinstance(rec, dict) else default

# Prefill from Salesforce where available
initial_advance_sf = parse_money(rec_val(prop, "Initial_Disbursement_Used__c", 0))
total_reno_sf = parse_money(rec_val(prop, "Renovation_Advance_Amount_Used__c", 0))
interest_reserve_sf = parse_money(rec_val(prop, "Interest_Allocation__c", 0))
holdback_ratio_raw = rec_val(prop, "Holdback_To_Rehab_Ratio__c", "")
borrower_name_sf = str(rec_val(prop, "Borrower_Name__c", "") or "").strip().upper()
address_sf = str(rec_val(prop, "Full_Address__c", "") or "").strip().upper()

with st.form("hud_form"):
    st.markdown("### Core Amounts")
    total_loan_amount = money_text_input("Total Loan Amount", "inp_total_loan_amount", 0.0)
    advance_amount = money_text_input("Advance Amount (this draw)", "inp_advance_amount", 0.0)

    st.markdown("### Prefilled from Salesforce (editable)")
    initial_advance = money_text_input("Initial Advance", "inp_initial_advance", float(initial_advance_sf))
    total_reno_drawn = money_text_input("Total Reno Drawn", "inp_total_reno_drawn", float(total_reno_sf))
    interest_reserve = money_text_input("Interest Reserve", "inp_interest_reserve", float(interest_reserve_sf))

    borrower_disp = st.text_input("Borrower", value=borrower_name_sf)
    address_disp = st.text_input("Address", value=address_sf)

    holdback_pct = st.text_input("Holdback % (optional)", value=normalize_pct(holdback_ratio_raw))
    advance_date_raw = st.text_input("Advance Date", placeholder="MM/DD/YYYY")

    workday_sup_code = st.text_input("Workday SUP Code (manual)", value="")

    st.markdown("### Fees")
    inspection_fee = money_text_input("3rd party Inspection Fee", "inp_inspection_fee", 0.0)
    wire_fee = money_text_input("Wire Fee", "inp_wire_fee", 0.0)
    construction_mgmt_fee = money_text_input("Construction Management Fee", "inp_construction_mgmt_fee", 0.0)
    title_fee = money_text_input("Title Fee", "inp_title_fee", 0.0)

    submitted = st.form_submit_button("Generate HUD Preview ✅")

if not submitted:
    st.stop()

# =========================================================
# Compute + Render HUD (preview)
# =========================================================
def recompute(ctx: dict) -> dict:
    ctx["allocated_loan_amount"] = float(ctx.get("advance_amount", 0.0)) + float(ctx.get("total_reno_drawn", 0.0))
    ctx["construction_advance_amount"] = float(ctx.get("advance_amount", 0.0))
    ctx["total_fees"] = (
        float(ctx.get("inspection_fee", 0.0))
        + float(ctx.get("wire_fee", 0.0))
        + float(ctx.get("construction_mgmt_fee", 0.0))
        + float(ctx.get("title_fee", 0.0))
    )
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

    hb_pct = html.escape(str(ctx.get("holdback_pct", "") or ""))

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
        <td class="rlbl">Holdback %</td><td class="rval grid">{hb_pct}</td>
      </tr>
      <tr>
        <td class="lbl">Total Reno Drawn:</td><td class="val grid">{fmt_money(ctx.get("total_reno_drawn", 0.0))}</td>
        <td class="rlbl">Allocated Loan Amount</td><td class="rval grid">{fmt_money(ctx.get("allocated_loan_amount", 0.0))}</td>
      </tr>
      <tr>
        <td class="lbl">Advance Amount:</td><td class="val grid">{fmt_money(ctx.get("advance_amount", 0.0))}</td>
        <td class="rlbl">Net Amount to Borrower</td><td class="rval grid">{fmt_money(ctx.get("net_amount_to_borrower", 0.0))}</td>
      </tr>
      <tr>
        <td class="lbl">Interest Reserve:</td><td class="val grid">{fmt_money(ctx.get("interest_reserve", 0.0))}</td>
        <td class="rlbl">Workday SUP Code:</td><td class="rval grid">{workday_sup_code_esc}</td>
      </tr>
      <tr>
        <td class="lbl">Available Balance:</td><td class="val grid">{fmt_money(ctx.get("available_balance", 0.0))}</td>
        <td class="rlbl">Advance Date:</td><td class="rval grid"><span class="bold">{advance_date_esc}</span></td>
      </tr>
    </table>

    <div class="borrower-line">
      <div><span class="bold">Borrower:</span> {borrower_disp_esc}</div>
      <div class="addr-line"><span class="bold">Address:</span> {address_disp_esc}</div>
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
    <tr class="tot"><td>Total Fees</td><td>{fmt_money(ctx.get("total_fees", 0.0))}</td></tr>
    <tr class="tot"><td>Reimbursement to Borrower</td><td>{fmt_money(ctx.get("net_amount_to_borrower", 0.0))}</td></tr>
  </table>
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
    "holdback_pct": normalize_pct(holdback_pct),
    "advance_date": parse_date_to_mmddyyyy(advance_date_raw),
    "workday_sup_code": str(workday_sup_code).strip(),
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
