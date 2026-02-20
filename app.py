# ============================================================
# HUD Generator App — ONE-CELL DROP-IN (Streamlit) [FIXED]
# FIXES IN THIS VERSION:
# ✅ Removes early Workday SUP code (deal number only for now)
# ✅ Fixes SalesforceMalformedRequest in Property__c query by:
#    - auto-discovering which lookup fields exist on Property__c / Loan__c (Deal__c vs Opportunity__c etc.)
#    - only querying with fields that actually exist (via describe())
#    - safer ORDER BY (only if field exists)
# ✅ Keeps: OAuth PKCE flow, pre-checks FIRST, user-friendly outputs, Excel download
# ============================================================

import base64
import hashlib
import io
import re
import secrets
import time
import urllib.parse
from datetime import date

import pandas as pd
import requests
import streamlit as st
from simple_salesforce import Salesforce
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment

# -----------------------------
# PAGE + STYLE
# -----------------------------
st.set_page_config(page_title="HUD Generator (Salesforce)", layout="wide")
st.markdown("""
<style>
  .block-container { padding-top: 1.0rem; padding-bottom: 2rem; }
  h1, h2, h3 { letter-spacing: -0.02em; }
  .soft-card {
    border: 1px solid rgba(49,51,63,.18);
    border-radius: 14px;
    padding: 14px 14px 10px 14px;
    background: rgba(255,255,255,.55);
  }
  .muted { color: rgba(49,51,63,.7); font-size: 0.92rem; }
  .big { font-size: 1.05rem; }
  [data-testid="stTextInput"] input, [data-testid="stNumberInput"] input {
    border-radius: 10px !important;
    padding: 10px 12px !important;
    font-size: 1rem !important;
  }
  [data-testid="stDateInput"] input {
    border-radius: 10px !important;
    padding: 10px 12px !important;
    font-size: 1rem !important;
  }
  .pill {
    display:inline-block; padding: 2px 10px; border-radius: 999px;
    border: 1px solid rgba(49,51,63,.18); background: rgba(255,255,255,.7);
    font-size: 0.85rem;
    margin-right: 6px;
    margin-top: 6px;
  }
</style>
""", unsafe_allow_html=True)

st.title("HUD Generator App (Salesforce)")
st.caption("Enter a Deal Number → we run required checks first → then you can generate the Excel HUD.")

# -----------------------------
# SECRETS
# -----------------------------
cfg = st.secrets["salesforce"]
CLIENT_ID = cfg["client_id"]
AUTH_HOST = cfg.get("auth_host", "https://login.salesforce.com").rstrip("/")
REDIRECT_URI = cfg["redirect_uri"].rstrip("/")
CLIENT_SECRET = cfg.get("client_secret")

AUTH_URL = f"{AUTH_HOST}/services/oauth2/authorize"
TOKEN_URL = f"{AUTH_HOST}/services/oauth2/token"

# -----------------------------
# PKCE HELPERS
# -----------------------------
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

now = time.time()
TTL = 900
for s, (_v, t0) in list(store.items()):
    if now - t0 > TTL:
        store.pop(s, None)

# -----------------------------
# UTIL
# -----------------------------
def soql_quote(s: str) -> str:
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"

def parse_money(val) -> float:
    if val is None:
        return 0.0
    s = str(val).strip()
    if s == "":
        return 0.0
    s = s.replace("$", "").replace(",", "")
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    try:
        x = float(s)
        return -x if neg else x
    except Exception:
        return 0.0

def fmt_money(x) -> str:
    try:
        return f"${float(x):,.2f}"
    except Exception:
        return "$0.00"

def parse_date_any(x):
    if x in ("", None):
        return None
    dt = pd.to_datetime(x, errors="coerce")
    if pd.isna(dt):
        return None
    return dt.date()

def fmt_date_mmddyyyy(x) -> str:
    d = parse_date_any(x)
    return d.strftime("%m/%d/%Y") if d else ""

def normalize_text(x):
    return "" if x is None else str(x).strip()

def norm_lower(x):
    return normalize_text(x).lower()

def pick_first(*vals):
    for v in vals:
        if v is None:
            continue
        s = str(v).strip()
        if s != "":
            return v
    return ""

# -----------------------------
# OAUTH FLOW (PKCE)
# -----------------------------
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

if code:
    if not state or state not in store:
        st.error("Missing/expired OAuth state. Click login again.")
        st.stop()
    verifier, _t0 = store.pop(state)
    tok = exchange_code_for_token(code, verifier)
    st.session_state.sf_token = tok
    st.query_params.clear()
    st.rerun()

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
    st.info("Step 1: Log in to Salesforce.")
    st.link_button("Login to Salesforce", login_url)
    st.stop()

tok = st.session_state.sf_token
access_token = tok.get("access_token")
instance_url = tok.get("instance_url")

if not access_token or not instance_url:
    st.error("Token missing access_token/instance_url")
    st.stop()

sf = Salesforce(instance_url=instance_url, session_id=access_token)

topc1, topc2 = st.columns([3, 1])
with topc1:
    st.success("✅ Authenticated with Salesforce")
    st.caption(f"Instance: {instance_url}")
with topc2:
    if st.button("Log out"):
        st.session_state.sf_token = None
        st.rerun()

# -----------------------------
# LOAD LOCAL EXCEL CHECK FILES (repo)
# -----------------------------
CAF_PATH = "Corevest_CAF National 52874_2.10.xlsx"
OSC_PATH = "OSC_Zstatus_COREVEST_2026-02-17_180520.xlsx"

@st.cache_data(show_spinner=False)
def load_osc_excel(path=OSC_PATH):
    try:
        x = pd.read_excel(path, sheet_name=None, dtype=str)
        if "COREVEST" in x:
            df = x["COREVEST"]
        else:
            df = list(x.values())[0]
        df.columns = df.columns.astype(str).str.strip().str.lower().str.replace(r"\s+", "_", regex=True)
        return df
    except Exception:
        return pd.DataFrame()

osc_df = load_osc_excel()

# -----------------------------
# DESCRIBE CACHES (prevents malformed SOQL)
# -----------------------------
@st.cache_resource
def describe_cache():
    return {}

DESC = describe_cache()

def get_obj_fields(obj_name: str) -> set:
    if obj_name in DESC:
        return DESC[obj_name]
    try:
        d = sf.__getattr__(obj_name).describe()
        fields = {f.get("name") for f in d.get("fields", []) if f.get("name")}
        DESC[obj_name] = fields
        return fields
    except Exception:
        DESC[obj_name] = set()
        return set()

def filter_existing_fields(obj_name: str, fields: list) -> list:
    existing = get_obj_fields(obj_name)
    if not existing:
        # if describe failed, just return as-is; the query helper will still try to drop missing
        return fields
    return [f for f in fields if f in existing]

def choose_first_existing(obj_name: str, candidates: list) -> str | None:
    existing = get_obj_fields(obj_name)
    for c in candidates:
        if c in existing:
            return c
    return None

# -----------------------------
# SAFE QUERY (with better error surfacing)
# -----------------------------
def sf_query_all(sf: Salesforce, soql: str):
    return sf.query_all(soql).get("records", [])

def try_query_drop_missing(sf: Salesforce, obj_name: str, fields, where_clause: str, limit=200, order_by=None):
    fields = list(fields)

    # Only keep fields that exist (if describe succeeded)
    fields = filter_existing_fields(obj_name, fields)

    # Only add ORDER BY if that field exists
    if order_by:
        # order_by like "CreatedDate DESC" -> take first token as field
        ob_field = order_by.split()[0].strip()
        if ob_field not in get_obj_fields(obj_name):
            order_by = None

    while True:
        soql = f"SELECT {', '.join(fields)} FROM {obj_name} WHERE {where_clause}"
        if order_by:
            soql += f" ORDER BY {order_by}"
        soql += f" LIMIT {int(limit)}"
        try:
            rows = sf_query_all(sf, soql)
            return rows, fields, soql
        except Exception as e:
            msg = str(e)

            # common patterns
            m1 = re.search(r"No such column '([^']+)'", msg)
            m2 = re.search(r"Didn't understand relationship '([^']+)'", msg)
            m3 = re.search(r"Invalid field: ([^,]+)", msg)
            m4 = re.search(r"INVALID_FIELD: ([^:]+):", msg)

            if m1:
                bad = m1.group(1)
                if bad in fields:
                    fields.remove(bad)
                    continue

            if m3:
                bad = m3.group(1).strip()
                if bad in fields:
                    fields.remove(bad)
                    continue

            if m4:
                bad = m4.group(1).strip()
                if bad in fields:
                    fields.remove(bad)
                    continue

            if m2:
                relbad = m2.group(1)
                drop = [f for f in fields if f.startswith(relbad + ".") or (("." + relbad + ".") in f)]
                if drop:
                    for f in drop:
                        if f in fields:
                            fields.remove(f)
                    continue

            # If we can't parse it, surface the SOQL to help debug quickly
            raise RuntimeError(f"Salesforce query failed.\nSOQL:\n{soql}\n\nRaw error:\n{msg}") from e

# -----------------------------
# SF FETCHES
# -----------------------------
def fetch_opportunity_by_deal_number(deal_number: str):
    deal_number = re.sub(r"\D", "", (deal_number or "").strip())
    if not deal_number:
        return None

    opp_fields = [
        "Id", "Name", "Deal_Loan_Number__c", "Account_Name__c",
        "StageName", "CloseDate",
        "LOC_Commitment__c",  # main commitment
        "Amount",             # fallback
        "Servicer_Status__c",
        "Next_Payment_Date__c",
        "Late_Fees_Servicer__c",  # you showed it's on Opportunity too
    ]
    where = (
        "("
        f"Deal_Loan_Number__c = {soql_quote(deal_number)}"
        f" OR Deal_Loan_Number__c LIKE {soql_quote('%' + deal_number + '%')}"
        ")"
    )
    rows, used_fields, soql = try_query_drop_missing(
        sf, "Opportunity", opp_fields, where, limit=10, order_by="CloseDate DESC NULLS LAST"
    )
    if not rows:
        return None
    r = rows[0].copy()
    r.pop("attributes", None)
    return r

def fetch_property_for_deal(opp_id: str):
    # discover which lookup field exists
    lk = choose_first_existing("Property__c", ["Deal__c", "Opportunity__c", "Deal_Id__c", "OpportunityId", "DealId"])
    if not lk:
        return None  # can't link; avoid malformed query

    prop_fields = [
        "Id", "Name",
        lk,
        "Servicer_Id__c",
        "Insurance_Status__c",
        "Late_Fees_Servicer__c",
    ]
    where = f"{lk} = {soql_quote(opp_id)}"
    rows, used_fields, soql = try_query_drop_missing(sf, "Property__c", prop_fields, where, limit=5, order_by="CreatedDate DESC")
    if not rows:
        return None
    r = rows[0].copy()
    r.pop("attributes", None)
    return r

def fetch_loan_for_deal(opp_id: str):
    lk = choose_first_existing("Loan__c", ["Deal__c", "Opportunity__c", "Deal_Id__c", "OpportunityId", "DealId"])
    if not lk:
        return None

    loan_fields = [
        "Id", "Name",
        lk,
        "Servicer_Loan_Status__c",
        "Servicer_Loan_Id__c",
        "Next_Payment_Date__c",
    ]
    where = f"{lk} = {soql_quote(opp_id)}"
    rows, used_fields, soql = try_query_drop_missing(sf, "Loan__c", loan_fields, where, limit=5, order_by="CreatedDate DESC")
    if not rows:
        return None
    r = rows[0].copy()
    r.pop("attributes", None)
    return r

# -----------------------------
# PRECHECKS (user-friendly)
# -----------------------------
TARGET_GOOD_INSURANCE = {"outside policy in-force", "outside policy in force", "in-force", "in force"}

def run_prechecks(opp: dict, prop: dict, loan: dict):
    deal_num = normalize_text(opp.get("Deal_Loan_Number__c"))
    deal_name = normalize_text(opp.get("Name"))
    acct_name = normalize_text(opp.get("Account_Name__c"))

    servicer_id = pick_first(
        prop.get("Servicer_Id__c") if prop else "",
    )

    total_loan_amount = parse_money(pick_first(opp.get("LOC_Commitment__c"), opp.get("Amount"), 0))

    next_pay = pick_first(
        loan.get("Next_Payment_Date__c") if loan else "",
        opp.get("Next_Payment_Date__c"),
        ""
    )

    servicer_status = pick_first(
        loan.get("Servicer_Loan_Status__c") if loan else "",
        opp.get("Servicer_Status__c"),
        ""
    )

    insurance_status = normalize_text(prop.get("Insurance_Status__c") if prop else "")

    late_fees = pick_first(
        prop.get("Late_Fees_Servicer__c") if prop else "",
        opp.get("Late_Fees_Servicer__c"),
        ""
    )
    late_fees_num = parse_money(late_fees)

    osc_primary_status = ""
    osc_ok = None
    if not osc_df.empty and servicer_id and "account_number" in osc_df.columns:
        m = osc_df["account_number"].astype(str).str.strip() == str(servicer_id).strip()
        hit = osc_df[m]
        if not hit.empty:
            osc_primary_status = str(hit.iloc[0].get("primary_status", "")).strip()
            osc_ok = (osc_primary_status.strip().lower() == "outside policy in-force".lower())
        else:
            osc_ok = None

    checks = []

    checks.append({
        "Check": "Late fees (should be $0)",
        "Value": fmt_money(late_fees_num),
        "Result": "✅ OK" if late_fees_num == 0 else "⚠️ Review",
        "Note": "Good is $0. If not $0, confirm before generating HUD."
    })

    ins_ok = (norm_lower(insurance_status) in TARGET_GOOD_INSURANCE) if insurance_status else False
    checks.append({
        "Check": "Insurance status",
        "Value": insurance_status if insurance_status else "(blank)",
        "Result": "✅ OK" if ins_ok else "⚠️ Review",
        "Note": "Should be Outside Policy In-Force (or equivalent)."
    })

    checks.append({
        "Check": "Servicer loan status",
        "Value": servicer_status if servicer_status else "(blank)",
        "Result": "✅ OK" if servicer_status else "⚠️ Review",
        "Note": "If blank/incorrect, update Salesforce before proceeding."
    })

    checks.append({
        "Check": "Next payment date",
        "Value": fmt_date_mmddyyyy(next_pay) if next_pay else "(blank)",
        "Result": "✅ OK" if next_pay else "⚠️ Review",
        "Note": "If blank, confirm with servicer / update Salesforce."
    })

    if osc_ok is None:
        checks.append({
            "Check": "OSC insurance file (offline)",
            "Value": "No match found",
            "Result": "⚪ Not found",
            "Note": "If needed, confirm insurance in Salesforce / OSC."
        })
    else:
        checks.append({
            "Check": "OSC insurance file (offline)",
            "Value": osc_primary_status if osc_primary_status else "(blank)",
            "Result": "✅ OK" if osc_ok else "⚠️ Review",
            "Note": "If not Outside Policy In-Force, reach out before HUD."
        })

    overall_ok = (late_fees_num == 0) and ins_ok
    return {
        "deal_number": deal_num,
        "deal_name": deal_name,
        "account_name": acct_name,
        "servicer_id": servicer_id,
        "total_loan_amount": total_loan_amount,
        "checks": checks,
        "overall_ok": overall_ok,
    }

# -----------------------------
# EXCEL BUILDER
# -----------------------------
def recompute_ctx(ctx: dict) -> dict:
    ctx = dict(ctx)
    ctx["allocated_loan_amount"] = float(ctx["advance_amount"]) + float(ctx["total_reno_drawn"])
    ctx["construction_advance_amount"] = float(ctx["advance_amount"])
    ctx["total_fees"] = float(ctx["inspection_fee"]) + float(ctx["wire_fee"]) + float(ctx["construction_mgmt_fee"]) + float(ctx["title_fee"])
    ctx["net_amount_to_borrower"] = float(ctx["construction_advance_amount"]) - float(ctx["total_fees"])
    ctx["available_balance"] = (
        float(ctx["total_loan_amount"])
        - float(ctx["initial_advance"])
        - float(ctx["total_reno_drawn"])
        - float(ctx["advance_amount"])
        - float(ctx["interest_reserve"])
    )
    return ctx

def build_hud_excel_bytes(ctx: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "HUD"

    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 24
    ws.column_dimensions["C"].width = 34
    ws.column_dimensions["D"].width = 24

    ws["A1"] = "COREVEST AMERICAN FINANCE LENDER LLC"
    ws["A2"] = "4 Park Plaza, Suite 900, Irvine, CA 92614"
    ws["A3"] = "Final Settlement Statement"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A3"].font = Font(bold=True, italic=True, size=12)

    rows = [
        ("Total Loan Amount:", ctx["total_loan_amount"], "Loan ID:", ctx["deal_number"]),
        ("Initial Advance:", ctx["initial_advance"], "Holdback %:", ctx["holdback_pct"]),
        ("Total Reno Drawn:", ctx["total_reno_drawn"], "Allocated Loan Amount:", None),
        ("Advance Amount:", ctx["advance_amount"], "Net Amount to Borrower:", None),
        ("Interest Reserve:", ctx["interest_reserve"], "", ""),
        ("Available Balance:", None, "Advance Date:", ctx["advance_date"]),
    ]
    start = 5
    for i, (l1, v1, l2, v2) in enumerate(rows):
        r = start + i
        ws[f"A{r}"] = l1
        ws[f"C{r}"] = l2
        ws[f"A{r}"].font = Font(bold=True)
        if l2:
            ws[f"C{r}"].font = Font(bold=True)

        if isinstance(v1, (int, float)):
            ws[f"B{r}"] = float(v1)
            ws[f"B{r}"].number_format = '$#,##0.00'
        else:
            ws[f"B{r}"] = v1 if v1 is not None else ""

        if isinstance(v2, (int, float)):
            ws[f"D{r}"] = float(v2)
            ws[f"D{r}"].number_format = '$#,##0.00'
        else:
            ws[f"D{r}"] = v2 if v2 is not None else ""

    ws[f"A{start+7}"] = "Borrower:"
    ws[f"A{start+8}"] = "Address:"
    ws[f"A{start+7}"].font = Font(bold=True)
    ws[f"A{start+8}"].font = Font(bold=True)
    ws[f"B{start+7}"] = ctx["borrower_disp"]
    ws[f"B{start+8}"] = ctx["address_disp"]

    t = start + 10
    ws[f"A{t}"] = "Charge Description"
    ws[f"B{t}"] = "Amount"
    ws[f"A{t}"].font = Font(bold=True)
    ws[f"B{t}"].font = Font(bold=True)

    charges = [
        ("Construction Advance Amount", ctx["construction_advance_amount"]),
        ("3rd party Inspection Fee", ctx["inspection_fee"]),
        ("Wire Fee", ctx["wire_fee"]),
        ("Construction Management Fee", ctx["construction_mgmt_fee"]),
        ("Title Fee", ctx["title_fee"]),
        ("Total Fees", ctx["total_fees"]),
        ("Reimbursement to Borrower", ctx["net_amount_to_borrower"]),
    ]
    for i, (desc, amt) in enumerate(charges, start=1):
        r = t + i
        ws[f"A{r}"] = desc
        ws[f"B{r}"] = float(amt)
        ws[f"B{r}"].number_format = '$#,##0.00'
        if desc in ("Construction Advance Amount", "Total Fees", "Reimbursement to Borrower"):
            ws[f"A{r}"].font = Font(bold=True)
            ws[f"B{r}"].font = Font(bold=True)

    alloc = float(ctx["advance_amount"]) + float(ctx["total_reno_drawn"])
    net = float(ctx["advance_amount"]) - float(ctx["total_fees"])
    avail = float(ctx["total_loan_amount"]) - float(ctx["initial_advance"]) - float(ctx["total_reno_drawn"]) - float(ctx["advance_amount"]) - float(ctx["interest_reserve"])
    ws[f"D{start+2}"] = alloc; ws[f"D{start+2}"].number_format = '$#,##0.00'
    ws[f"D{start+3}"] = net;   ws[f"D{start+3}"].number_format = '$#,##0.00'
    ws[f"B{start+5}"] = avail; ws[f"B{start+5}"].number_format = '$#,##0.00'

    for row in ws.iter_rows(min_row=1, max_row=ws.max_row, min_col=1, max_col=4):
        for cell in row:
            cell.alignment = Alignment(vertical="center")

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()

# -----------------------------
# SESSION STATE DEFAULTS (MUST be set BEFORE widgets)
# -----------------------------
def ensure_default(key, val):
    if key not in st.session_state:
        st.session_state[key] = val

ensure_default("deal_number_input", "")
ensure_default("precheck_ran", False)
ensure_default("precheck_payload", None)
ensure_default("allow_override", False)

# HUD inputs as TEXT (so they start blank)
ensure_default("inp_advance_amount", "")
ensure_default("inp_holdback_pct", "")
ensure_default("inp_advance_date", date.today())
ensure_default("inp_inspection_fee", "")
ensure_default("inp_wire_fee", "")
ensure_default("inp_construction_mgmt_fee", "")
ensure_default("inp_title_fee", "")

# -----------------------------
# UI — DEAL INPUT + PRECHECKS
# -----------------------------
st.markdown('<div class="soft-card">', unsafe_allow_html=True)
c1, c2 = st.columns([2.4, 1.2])
with c1:
    deal_number = st.text_input("Deal Number", key="deal_number_input", placeholder="e.g., 403012345 or 12345")
with c2:
    run_btn = st.button("Run checks", type="primary", use_container_width=True)
st.markdown("</div>", unsafe_allow_html=True)

if run_btn:
    st.session_state.precheck_ran = False
    st.session_state.precheck_payload = None
    st.session_state.allow_override = False

    with st.spinner("Looking up deal in Salesforce..."):
        opp = fetch_opportunity_by_deal_number(deal_number)

    if not opp:
        st.error("No deal found for that Deal Number. Make sure you entered the Deal Loan Number.")
        st.stop()

    opp_id = opp.get("Id")
    with st.spinner("Pulling related Property / Loan info..."):
        # These now cannot throw malformed query due to bad lookup fields;
        # they will safely return None if linkage field doesn't exist.
        prop = fetch_property_for_deal(opp_id) if opp_id else None
        loan = fetch_loan_for_deal(opp_id) if opp_id else None

    payload = run_prechecks(opp, prop, loan)
    st.session_state.precheck_payload = {"opp": opp, "prop": prop, "loan": loan, "payload": payload}
    st.session_state.precheck_ran = True

# -----------------------------
# SHOW CHECK RESULTS
# -----------------------------
if st.session_state.precheck_ran and st.session_state.precheck_payload:
    opp = st.session_state.precheck_payload["opp"]
    prop = st.session_state.precheck_payload["prop"]
    payload = st.session_state.precheck_payload["payload"]

    st.subheader("Check results")
    st.markdown(f"""
<div class="soft-card">
  <div class="big"><b>{payload['deal_number']}</b> — {payload['deal_name']}</div>
  <div class="muted">{payload['account_name']}</div>
  <div style="margin-top:8px;">
    <span class="pill">Total Loan Amount: <b>{fmt_money(payload['total_loan_amount'])}</b></span>
    <span class="pill">Servicer ID: <b>{payload['servicer_id'] if payload['servicer_id'] else '—'}</b></span>
  </div>
</div>
""", unsafe_allow_html=True)

    df_checks = pd.DataFrame(payload["checks"])[["Check", "Value", "Result", "Note"]]
    st.dataframe(df_checks, use_container_width=True, hide_index=True)

    if payload["overall_ok"]:
        st.success("✅ Checks look good. You can continue to build the HUD Excel.")
        st.session_state.allow_override = True
    else:
        st.warning("⚠️ Some checks need review before generating the HUD.")
        st.session_state.allow_override = st.checkbox("Override and continue anyway", value=False)

# -----------------------------
# HUD INPUTS (only after checks)
# -----------------------------
if st.session_state.precheck_ran and st.session_state.precheck_payload and st.session_state.allow_override:
    opp = st.session_state.precheck_payload["opp"]
    payload = st.session_state.precheck_payload["payload"]

    # Prefill borrower/address best-effort without “prefilled” text
    borrower_disp = (opp.get("Account_Name__c") or "").strip().upper()

    address_disp = ""
    if payload.get("servicer_id") and not osc_df.empty and "account_number" in osc_df.columns:
        m = osc_df["account_number"].astype(str).str.strip() == str(payload["servicer_id"]).strip()
        hit = osc_df[m]
        if not hit.empty:
            street = str(hit.iloc[0].get("property_street", "")).strip()
            city = str(hit.iloc[0].get("property_city", "")).strip()
            state = str(hit.iloc[0].get("property_state", "")).strip()
            zipc = str(hit.iloc[0].get("property_zip", "")).strip()
            address_disp = f"{street} {city} {state} {zipc}".strip().upper()

    st.subheader("HUD inputs")
    st.caption("Type amounts like `1200` or `$1,200` (leave blank for $0).")

    with st.form("hud_form", clear_on_submit=False):
        cA, cB, cC = st.columns([1.2, 1.0, 1.2])

        with cA:
            st.markdown("**Borrower info**")
            borrower_val = st.text_input("Borrower (for the form)", value=borrower_disp, key="inp_borrower_disp")
            addr_val = st.text_input("Address (for the form)", value=address_disp, key="inp_address_disp")

        with cB:
            st.markdown("**Advance**")
            adv_amt_raw = st.text_input("Advance Amount", key="inp_advance_amount", placeholder="e.g., 25000")
            holdback_pct = st.text_input("Holdback % (optional)", key="inp_holdback_pct", placeholder="e.g., 20%")
            adv_date = st.date_input("Advance Date", key="inp_advance_date")

        with cC:
            st.markdown("**Fees**")
            insp_raw = st.text_input("3rd party Inspection Fee", key="inp_inspection_fee", placeholder="leave blank for 0")
            wire_raw = st.text_input("Wire Fee", key="inp_wire_fee", placeholder="leave blank for 0")
            cm_raw = st.text_input("Construction Management Fee", key="inp_construction_mgmt_fee", placeholder="leave blank for 0")
            title_raw = st.text_input("Title Fee", key="inp_title_fee", placeholder="leave blank for 0")

        submitted = st.form_submit_button("Build HUD Excel", type="primary", use_container_width=True)

    if submitted:
        advance_amount = parse_money(adv_amt_raw)
        inspection_fee = parse_money(insp_raw)
        wire_fee = parse_money(wire_raw)
        construction_mgmt_fee = parse_money(cm_raw)
        title_fee = parse_money(title_raw)

        hb = (holdback_pct or "").strip()
        if hb and not hb.endswith("%"):
            try:
                v = float(hb.replace("%", "").strip())
                hb = f"{v:.0f}%"
            except Exception:
                pass

        # NOTE: These SF-funded fields may not exist in your org; they default to 0.
        sf_initial_advance = parse_money(opp.get("Initial_Disbursement_Funded__c")) if "Initial_Disbursement_Funded__c" in opp else 0.0
        sf_total_reno = parse_money(opp.get("Renovation_HB_Funded__c")) if "Renovation_HB_Funded__c" in opp else 0.0
        sf_interest_reserve = parse_money(opp.get("Interest_Allocation_Funded__c")) if "Interest_Allocation_Funded__c" in opp else 0.0

        ctx = {
            "deal_number": payload["deal_number"],
            "total_loan_amount": float(payload["total_loan_amount"]),
            "initial_advance": float(sf_initial_advance),
            "total_reno_drawn": float(sf_total_reno),
            "interest_reserve": float(sf_interest_reserve),
            "advance_amount": float(advance_amount),
            "holdback_pct": hb,
            "advance_date": adv_date.strftime("%m/%d/%Y"),
            "borrower_disp": (borrower_val or "").strip().upper(),
            "address_disp": (addr_val or "").strip().upper(),
            "inspection_fee": float(inspection_fee),
            "wire_fee": float(wire_fee),
            "construction_mgmt_fee": float(construction_mgmt_fee),
            "title_fee": float(title_fee),
        }
        ctx = recompute_ctx(ctx)

        st.markdown("### Preview")
        prev = pd.DataFrame([
            ["Total Loan Amount", fmt_money(ctx["total_loan_amount"])],
            ["Initial Advance", fmt_money(ctx["initial_advance"])],
            ["Total Reno Drawn", fmt_money(ctx["total_reno_drawn"])],
            ["Interest Reserve", fmt_money(ctx["interest_reserve"])],
            ["Advance Amount", fmt_money(ctx["advance_amount"])],
            ["Allocated Loan Amount", fmt_money(ctx["allocated_loan_amount"])],
            ["Total Fees", fmt_money(ctx["total_fees"])],
            ["Net Amount to Borrower", fmt_money(ctx["net_amount_to_borrower"])],
            ["Available Balance", fmt_money(ctx["available_balance"])],
        ], columns=["Field", "Value"])
        st.dataframe(prev, use_container_width=True, hide_index=True)

        xbytes = build_hud_excel_bytes(ctx)
        out_name = f"HUD_{re.sub(r'[^0-9A-Za-z_-]+','_', ctx['deal_number'] or 'Deal')}.xlsx"
        st.download_button(
            "Download HUD Excel",
            data=xbytes,
            file_name=out_name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )
