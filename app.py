# ============================================================
# HUD GENERATOR (APP.PY) — Salesforce-driven (no Hayden, no FCI)
# - Deal Number is the primary identifier
# - Runs validation checks (late fees / insurance / statuses) BEFORE HUD export
# - Outputs Excel (no HTML)
# ============================================================

import re
import io
import time
import html
import base64
import secrets
import hashlib
import urllib.parse
from datetime import datetime

import pandas as pd
import streamlit as st
import requests
from simple_salesforce import Salesforce
from simple_salesforce.exceptions import SalesforceMalformedRequest, SalesforceGeneralError

# -------------------------
# PAGE CONFIG
# -------------------------
st.set_page_config(page_title="HUD Generator (Salesforce)", page_icon="🏗️", layout="wide")
st.title("🏗️ HUD Generator (Salesforce)")
st.caption("Enter Deal Number → run checks → export HUD to Excel")

# -------------------------
# DISPLAY SETTINGS
# -------------------------
pd.set_option("display.max_rows", 2000)
pd.set_option("display.max_columns", None)
pd.set_option("display.width", None)
pd.set_option("display.max_colwidth", None)

# =========================================================
# Helpers (money, pct, date)
# =========================================================
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

def safe_get(d: dict, key: str, default=""):
    try:
        v = d.get(key, default)
        return default if v is None else v
    except Exception:
        return default

# =========================================================
# Salesforce OAuth (PKCE) using Streamlit query params
# IMPORTANT: redirect_uri MUST match Connected App config EXACTLY.
# =========================================================
if "salesforce" not in st.secrets:
    st.error("Missing [salesforce] in .streamlit/secrets.toml")
    st.stop()

cfg = st.secrets["salesforce"]
CLIENT_ID = cfg["client_id"]
AUTH_HOST = cfg.get("auth_host", "https://login.salesforce.com").rstrip("/")
REDIRECT_URI = cfg["redirect_uri"].rstrip("/")  # normalize
CLIENT_SECRET = cfg.get("client_secret", None)  # optional (still read from secrets)

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

qp = st.query_params
code = qp.get("code")
state = qp.get("state")
err = qp.get("error")
err_desc = qp.get("error_description")

if err:
    st.error(f"OAuth error: {err}")
    if err_desc:
        st.code(err_desc)
    st.info("Most common fix: Connected App 'Callback URL' must exactly equal REDIRECT_URI.")
    st.code(f"REDIRECT_URI used by app:\n{REDIRECT_URI}")
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

    st.info("Authenticate with Salesforce to continue.")
    st.link_button("Login to Salesforce", login_url)
    with st.expander("Debug (Redirect URI must match Connected App)", expanded=False):
        st.write("AUTH_HOST:", AUTH_HOST)
        st.write("REDIRECT_URI:", REDIRECT_URI)
    st.stop()

tok = st.session_state.sf_token
access_token = tok.get("access_token")
instance_url = tok.get("instance_url")
id_url = tok.get("id")

if not access_token or not instance_url:
    st.error("Token missing access_token/instance_url")
    st.json({k: tok.get(k) for k in ["instance_url", "id", "issued_at", "scope", "token_type"]})
    st.stop()

sf = Salesforce(instance_url=instance_url, session_id=access_token)

topL, topR = st.columns([2, 1])
with topL:
    st.success("✅ Salesforce connected")
    st.write("instance_url:", instance_url)
with topR:
    if st.button("Log out / Clear token"):
        st.session_state.sf_token = None
        st.rerun()

# =========================================================
# Robust SF querying utilities (avoid MalformedRequest mystery)
# =========================================================
@st.cache_data(show_spinner=False)
def _describe(sf, obj_api: str) -> dict:
    return getattr(sf, obj_api).describe()

@st.cache_data(show_spinner=False)
def _fieldnames(sf, obj_api: str) -> set[str]:
    desc = _describe(sf, obj_api)
    return {f.get("name") for f in desc.get("fields", []) if f.get("name")}

def _filter_existing(sf, obj_api: str, desired: list[str]) -> list[str]:
    ex = _fieldnames(sf, obj_api)
    return [f for f in desired if f in ex]

def sf_query_all_safe(soql: str) -> list[dict]:
    try:
        res = sf.query_all(soql)
        return res.get("records", []) or []
    except SalesforceMalformedRequest as e:
        st.error("SalesforceMalformedRequest (bad field / relationship / permissions).")
        st.code(soql)
        st.exception(e)
        return []
    except SalesforceGeneralError as e:
        st.error("Salesforce error.")
        st.code(soql)
        st.exception(e)
        return []
    except Exception as e:
        st.error("Unexpected query error.")
        st.code(soql)
        st.exception(e)
        return []

def query_first_by_links(obj_api: str, link_candidates: list[str], link_value: str, desired_fields: list[str], order_by="LastModifiedDate DESC") -> dict | None:
    existing = _fieldnames(sf, obj_api)
    usable_links = [lk for lk in link_candidates if lk in existing]
    fields = _filter_existing(sf, obj_api, desired_fields)
    if not fields:
        return None

    for lk in usable_links:
        soql = f"SELECT {', '.join(fields)} FROM {obj_api} WHERE {lk} = '{link_value}' ORDER BY {order_by} LIMIT 1"
        rows = sf_query_all_safe(soql)
        if rows:
            return rows[0]
    return None

def find_opportunity_by_deal_number(deal_number: str) -> dict | None:
    """
    Try common 'deal number' fields on Opportunity; fallback to Name exact.
    Adjust the candidate list if your org uses a specific field.
    """
    deal_number = str(deal_number).strip()
    cand_fields = [
        "Deal_Number__c",
        "DealNumber__c",
        "Deal__c",                 # sometimes a text field (rare)
        "Deal_ID__c",
        "DealId__c",
        "CV_Deal_Number__c",
        "Corevest_Deal_Number__c",
    ]
    existing = _fieldnames(sf, "Opportunity")
    usable = [f for f in cand_fields if f in existing]

    base_fields = _filter_existing(sf, "Opportunity", [
        "Id", "Name",
        "Borrower_Name__c",
        "Next_Payment_Date__c",
        "Late_Fees_Servicer__c",
        "Approval_Email_Status__c",
        "Funding_Status__c",
        "Loan_Document_Status__c",
        "Loan_Status_Change_Date__c",
        "Servicer_Status__c",
        "Delinquency_Status_Notes__c",
    ])
    if "Id" not in base_fields:
        base_fields = ["Id", "Name"]

    # Try each candidate field as exact match
    for f in usable:
        soql = f"SELECT {', '.join(base_fields)} FROM Opportunity WHERE {f} = '{deal_number}' ORDER BY LastModifiedDate DESC LIMIT 1"
        rows = sf_query_all_safe(soql)
        if rows:
            return rows[0]

    # Fallback: Name exact match
    soql = f"SELECT {', '.join(base_fields)} FROM Opportunity WHERE Name = '{deal_number}' ORDER BY LastModifiedDate DESC LIMIT 1"
    rows = sf_query_all_safe(soql)
    return rows[0] if rows else None

# =========================================================
# Data fetchers for this HUD
# =========================================================
def fetch_property_for_opp(opp_id: str) -> dict | None:
    desired = [
        "Id",
        "Servicer_Id__c",
        "Borrower_Name__c",
        "Full_Address__c",
        "Yardi_Id__c",
        "Initial_Disbursement_Used__c",
        "Renovation_Advance_Amount_Used__c",
        "Interest_Allocation__c",
        "Holdback_To_Rehab_Ratio__c",
        "Late_Fees_Servicer__c",
        "Insurance_Status__c",
        "Insurance_Status__c",
        "Loan_Status__c",
        "Status__c",
        "Next_Payment_Date__c",
        "HUD_Settlement_Statement_Status__c",
    ]
    return query_first_by_links(
        obj_api="Property__c",
        link_candidates=["Opportunity__c", "Deal__c", "OpportunityId__c", "Deal_Lookup__c"],
        link_value=opp_id,
        desired_fields=desired,
        order_by="LastModifiedDate DESC",
    )

def fetch_loan_for_opp(opp_id: str) -> dict | None:
    desired = [
        "Id",
        "Servicer_Loan_Status__c",
        "Servicer_Loan_Id__c",
        "Next_Payment_Date__c",
        "Late_Fees_Servicer__c",
    ]
    return query_first_by_links(
        obj_api="Loan__c",
        link_candidates=["Opportunity__c", "Deal__c", "Property__c"],
        link_value=opp_id,
        desired_fields=desired,
        order_by="LastModifiedDate DESC",
    )

def fetch_latest_advance_for_opp(opp_id: str) -> dict | None:
    desired = [
        "Id",
        "LOC_Commitment__c",
        "LoanDocumentStatus__c",
        "Status__c",
        "CreatedDate",
        "LastModifiedDate",
    ]
    return query_first_by_links(
        obj_api="Advance__c",
        link_candidates=["Opportunity__c", "Deal__c", "Property__c"],
        link_value=opp_id,
        desired_fields=desired,
        order_by="CreatedDate DESC",
    )

def fetch_account_for_opp(opp: dict) -> dict | None:
    # If Opportunity has AccountId, we can query Account for vendor code.
    acc_id = safe_get(opp, "AccountId", None)
    if not acc_id:
        # Sometimes custom relationship; try Property -> Account? (skip if unknown)
        return None
    desired = ["Id", "Name", "Yardi_Vendor_Code__c"]
    fields = _filter_existing(sf, "Account", desired)
    if not fields:
        return None
    soql = f"SELECT {', '.join(fields)} FROM Account WHERE Id = '{acc_id}' LIMIT 1"
    rows = sf_query_all_safe(soql)
    return rows[0] if rows else None

# =========================================================
# Picklist utilities
# =========================================================
def get_picklist_values(obj_api: str, field_api: str) -> list[str]:
    desc = _describe(sf, obj_api)
    for f in desc.get("fields", []):
        if f.get("name") == field_api:
            if f.get("type") == "picklist":
                vals = []
                for v in f.get("picklistValues", []) or []:
                    lab = v.get("label") or v.get("value")
                    if lab:
                        vals.append(str(lab))
                return vals
    return []

def search_picklists_for_keywords(objects: list[str], keywords: list[str]) -> pd.DataFrame:
    kw = [k.lower().strip() for k in keywords if str(k).strip()]
    rows_out = []
    for obj in objects:
        try:
            desc = _describe(sf, obj)
        except Exception:
            continue
        for f in desc.get("fields", []):
            if f.get("type") != "picklist":
                continue
            vals = []
            for v in f.get("picklistValues", []) or []:
                lab = v.get("label") or v.get("value")
                if lab:
                    vals.append(str(lab))
            hay = " | ".join([v.lower() for v in vals])
            if any(k in hay for k in kw):
                rows_out.append({
                    "object": obj,
                    "field_label": f.get("label",""),
                    "field_api": f.get("name",""),
                    "type": "picklist",
                    "matching_values": ", ".join([v for v in vals if any(k in v.lower() for k in kw)])[:5000],
                    "all_values_preview": ", ".join(vals[:30]) + (" ..." if len(vals) > 30 else "")
                })
    df = pd.DataFrame(rows_out)
    if df.empty:
        return df
    return df.sort_values(["object","field_label","field_api"], kind="stable").reset_index(drop=True)

# =========================================================
# HUD calculations (same formulas as your old code)
# =========================================================
def recompute(ctx: dict) -> dict:
    ctx["allocated_loan_amount"] = float(ctx.get("advance_amount", 0.0)) + float(ctx.get("total_reno_drawn", 0.0))
    ctx["construction_advance_amount"] = float(ctx.get("advance_amount", 0.0))

    fee_keys = ["inspection_fee", "wire_fee", "construction_mgmt_fee", "title_fee"]
    ctx["total_fees"] = sum(float(ctx.get(k, 0.0)) for k in fee_keys)

    # Late fees are a CHECK. They are NOT auto-included as a HUD line item now.
    ctx["late_fees_check_amount"] = float(ctx.get("late_fees_amt", 0.0))

    ctx["net_amount_to_borrower"] = ctx["construction_advance_amount"] - ctx["total_fees"]

    ctx["available_balance"] = (
        float(ctx.get("total_loan_amount", 0.0))
        - float(ctx.get("initial_advance", 0.0))
        - float(ctx.get("total_reno_drawn", 0.0))
        - float(ctx.get("advance_amount", 0.0))
        - float(ctx.get("interest_reserve", 0.0))
    )
    return ctx

# =========================================================
# Excel export (HUD-style table)
# =========================================================
def build_hud_export_df(ctx: dict) -> pd.DataFrame:
    rows = [
        ("Total Loan Amount", fmt_money(ctx.get("total_loan_amount", 0.0))),
        ("Initial Advance", fmt_money(ctx.get("initial_advance", 0.0))),
        ("Total Reno Drawn", fmt_money(ctx.get("total_reno_drawn", 0.0))),
        ("Advance Amount", fmt_money(ctx.get("advance_amount", 0.0))),
        ("Allocated Loan Amount", fmt_money(ctx.get("allocated_loan_amount", 0.0))),
        ("Interest Reserve", fmt_money(ctx.get("interest_reserve", 0.0))),
        ("Available Balance", fmt_money(ctx.get("available_balance", 0.0))),
        ("Net Amount to Borrower", fmt_money(ctx.get("net_amount_to_borrower", 0.0))),
        ("Holdback % Current", ctx.get("holdback_current", "")),
        ("Holdback % at Closing", ctx.get("holdback_closing", "")),
        ("Workday SUP Code", ctx.get("workday_sup_code", "")),
        ("Advance Date", ctx.get("advance_date", "")),
        ("Borrower", ctx.get("borrower_disp", "")),
        ("Address", ctx.get("address_disp", "")),
        ("Loan ID (Deal Number)", ctx.get("deal_number", "")),
        ("Servicer ID", ctx.get("servicer_id", "")),
        ("Yardi ID", ctx.get("yardi_id", "")),
        ("Yardi Vendor Code", ctx.get("yardi_vendor_code", "")),
    ]
    return pd.DataFrame(rows, columns=["Field", "Value"])

def to_excel_bytes(hud_df: pd.DataFrame, checks_df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        hud_df.to_excel(writer, index=False, sheet_name="HUD")
        checks_df.to_excel(writer, index=False, sheet_name="Checks")
        # light formatting widths
        wb = writer.book
        for sh in ["HUD", "Checks"]:
            ws = wb[sh]
            ws.freeze_panes = "A2"
            for col in ws.columns:
                max_len = 0
                col_letter = col[0].column_letter
                for cell in col[:200]:
                    try:
                        max_len = max(max_len, len(str(cell.value)) if cell.value is not None else 0)
                    except Exception:
                        pass
                ws.column_dimensions[col_letter].width = min(max(12, max_len + 2), 60)
    return buf.getvalue()

# =========================================================
# UI
# =========================================================
tab_run, tab_picklists = st.tabs(["🧾 Deal → Checks → Export", "🔎 Picklist search"])

with tab_run:
    st.subheader("1) Enter Deal Number")
    with st.form("deal_form"):
        deal_number = st.text_input("Deal Number", placeholder="e.g., 58439")
        advance_amount = st.number_input("Advance Amount (manual)", min_value=0.0, step=0.01, format="%.2f")

        c1, c2, c3 = st.columns(3)
        holdback_current_raw = c1.text_input("Holdback % Current (manual if needed)", placeholder="100")
        holdback_closing_raw = c2.text_input("Holdback % at Closing (manual if needed)", placeholder="100")
        advance_date_raw = c3.text_input("Advance Date (manual)", placeholder="MM/DD/YYYY")

        st.markdown("**Fees (manual):**")
        f1, f2, f3, f4 = st.columns(4)
        inspection_fee = f1.number_input("3rd party Inspection Fee", min_value=0.0, step=0.01, format="%.2f")
        wire_fee = f2.number_input("Wire Fee", min_value=0.0, step=0.01, format="%.2f")
        construction_mgmt_fee = f3.number_input("Construction Management Fee", min_value=0.0, step=0.01, format="%.2f")
        title_fee = f4.number_input("Title Fee", min_value=0.0, step=0.01, format="%.2f")

        workday_sup_code_input = st.text_input("Workday SUP Code (manual for now)", placeholder="SUP12345")

        submitted = st.form_submit_button("Run checks ✅")

    if not submitted:
        st.stop()

    deal_number = str(deal_number).strip()
    if not deal_number:
        st.error("Deal Number is required.")
        st.stop()

    st.subheader("2) Pull Salesforce records")
    with st.spinner("Looking up Opportunity by Deal Number..."):
        opp = find_opportunity_by_deal_number(deal_number)

    if not opp:
        st.error("No Opportunity found for that Deal Number. (Adjust the deal-number field mapping in find_opportunity_by_deal_number.)")
        st.stop()

    opp_id = opp.get("Id")
    st.success(f"Found Opportunity: {safe_get(opp,'Name','')}  (Id: {opp_id})")

    with st.spinner("Loading related objects (Property / Loan / latest Advance)..."):
        prop = fetch_property_for_opp(opp_id) if opp_id else None
        loan = fetch_loan_for_opp(opp_id) if opp_id else None
        adv = fetch_latest_advance_for_opp(opp_id) if opp_id else None

    # Account (for vendor code)
    # Need AccountId field; if it doesn't exist, skip gracefully.
    # Try to describe and pull it if present.
    if "AccountId" not in _fieldnames(sf, "Opportunity"):
        acct = None
    else:
        # If not already in opp dict, fetch it quickly
        if "AccountId" not in opp:
            soql = f"SELECT Id, AccountId FROM Opportunity WHERE Id = '{opp_id}' LIMIT 1"
            rr = sf_query_all_safe(soql)
            if rr:
                opp = rr[0]
        acct = fetch_account_for_opp(opp)

    # =========================================================
    # Map SF → HUD inputs
    # =========================================================
    # NOTE: You already confirmed these mappings:
    # - Initial Disbursement Funded -> Initial_Disbursement_Used__c (Property__c)
    # - Total Reno Drawn -> Renovation_Advance_Amount_Used__c (Property__c)
    # - Interest Reserve -> Interest_Allocation__c (Property__c)
    # - Loan Commitment -> LOC_Commitment__c (Advance__c)
    # - Borrower -> Borrower_Name__c (Property__c)
    # - Address -> Full_Address__c (Property__c)
    # - Yardi ID -> Yardi_Id__c (Property__c)
    # - Late Fees -> Late_Fees_Servicer__c (Property__c or Opportunity)
    # - Next Payment Date -> Next_Payment_Date__c (Loan/Opportunity/Property)
    #
    # Holdbacks: you mentioned Holdback_To_Rehab_Ratio__c (Property__c) exists
    # We'll treat that as the "current" holdback fallback if user leaves blank.

    # Amounts
    total_loan_amount = parse_money(safe_get(adv or {}, "LOC_Commitment__c", 0.0))
    initial_advance = parse_money(safe_get(prop or {}, "Initial_Disbursement_Used__c", 0.0))
    total_reno_drawn = parse_money(safe_get(prop or {}, "Renovation_Advance_Amount_Used__c", 0.0))
    interest_reserve = parse_money(safe_get(prop or {}, "Interest_Allocation__c", 0.0))

    # Borrower + address + ids
    borrower = str(safe_get(prop or {}, "Borrower_Name__c", "")) or str(safe_get(opp or {}, "Borrower_Name__c", ""))
    address = str(safe_get(prop or {}, "Full_Address__c", ""))
    yardi_id = str(safe_get(prop or {}, "Yardi_Id__c", ""))
    servicer_id = str(safe_get(prop or {}, "Servicer_Id__c", ""))  # fallback requested
    yardi_vendor_code = str(safe_get(acct or {}, "Yardi_Vendor_Code__c", ""))

    # Holdback fallback (if user leaves blank)
    holdback_ratio = safe_get(prop or {}, "Holdback_To_Rehab_Ratio__c", "")
    hb_current = normalize_pct(holdback_current_raw) if str(holdback_current_raw).strip() else normalize_pct(holdback_ratio)
    hb_closing = normalize_pct(holdback_closing_raw)

    # Late fees check (prefer Property, else Opportunity, else Loan)
    late_fees_amt = parse_money(
        safe_get(prop or {}, "Late_Fees_Servicer__c",
        safe_get(opp or {}, "Late_Fees_Servicer__c",
        safe_get(loan or {}, "Late_Fees_Servicer__c", 0.0)))
    )

    # Next payment date check (Loan -> Opportunity -> Property)
    next_payment_date = safe_get(loan or {}, "Next_Payment_Date__c",
                        safe_get(opp or {}, "Next_Payment_Date__c",
                        safe_get(prop or {}, "Next_Payment_Date__c", "")))
    next_payment_date = parse_date_to_mmddyyyy(next_payment_date)

    # Insurance status check
    insurance_status = str(safe_get(prop or {}, "Insurance_Status__c", "")).strip()

    # Status checks (these are just shown so the user can interpret)
    prop_status = str(safe_get(prop or {}, "Status__c", "")).strip()
    prop_loan_status = str(safe_get(prop or {}, "Loan_Status__c", "")).strip()
    loan_servicer_status = str(safe_get(loan or {}, "Servicer_Loan_Status__c", "")).strip()

    # =========================================================
    # Present checks first
    # =========================================================
    st.subheader("3) Pre-HUD Checks (review before export)")

    checks = []
    checks.append(("Deal Number", deal_number, "OK" if deal_number else "FAIL", ""))
    checks.append(("Opportunity Found", safe_get(opp, "Name", ""), "OK" if opp else "FAIL", ""))

    checks.append(("Late Fees (Servicer)", fmt_money(late_fees_amt), "OK" if late_fees_amt <= 0 else "REVIEW",
                   "Late fees present — confirm with servicer / notes." if late_fees_amt > 0 else ""))

    checks.append(("Insurance Status", insurance_status or "(blank)", "OK" if insurance_status else "REVIEW",
                   "Insurance status blank — verify insurance is in-force/outside policy per process." if not insurance_status else ""))

    checks.append(("Next Payment Date", next_payment_date or "(blank)", "OK" if next_payment_date else "REVIEW",
                   "Next payment date missing — confirm servicer schedule." if not next_payment_date else ""))

    checks.append(("Property Status", prop_status or "(blank)", "INFO", ""))
    checks.append(("Property Servicer Loan Status", prop_loan_status or "(blank)", "INFO", ""))
    checks.append(("Loan Servicer Loan Status", loan_servicer_status or "(blank)", "INFO", ""))

    checks_df = pd.DataFrame(checks, columns=["Check", "Value", "Result", "Notes"])

    st.dataframe(checks_df, use_container_width=True, hide_index=True)

    # Gate: require Opportunity + (optional) require insurance not blank? you decide.
    qualifies = True
    blockers = []

    if not opp:
        qualifies = False
        blockers.append("No Opportunity for this Deal Number.")
    # If you want hard blockers, uncomment:
    # if late_fees_amt > 0:
    #     qualifies = False
    #     blockers.append("Late fees present (must resolve/confirm before HUD).")
    # if not insurance_status:
    #     qualifies = False
    #     blockers.append("Insurance status is blank (must verify before HUD).")

    if qualifies:
        st.success("✅ Deal passes minimum requirements to build/export HUD (per current gating rules).")
    else:
        st.error("🚫 Deal does NOT qualify for HUD export yet.")
        for b in blockers:
            st.write("•", b)

    # =========================================================
    # Build HUD context + export
    # =========================================================
    st.subheader("4) Build HUD + Export to Excel")
    ctx = {
        "deal_number": deal_number,
        "servicer_id": servicer_id,
        "yardi_id": yardi_id,
        "yardi_vendor_code": yardi_vendor_code,

        "total_loan_amount": total_loan_amount,
        "initial_advance": initial_advance,
        "total_reno_drawn": total_reno_drawn,
        "interest_reserve": interest_reserve,

        "advance_amount": float(advance_amount),

        "holdback_current": hb_current,
        "holdback_closing": hb_closing,
        "advance_date": parse_date_to_mmddyyyy(advance_date_raw),

        "workday_sup_code": str(workday_sup_code_input).strip(),

        "borrower_disp": str(borrower).strip().upper(),
        "address_disp": str(address).strip().upper(),

        "inspection_fee": float(inspection_fee),
        "wire_fee": float(wire_fee),
        "construction_mgmt_fee": float(construction_mgmt_fee),
        "title_fee": float(title_fee),

        "late_fees_amt": float(late_fees_amt),
        "insurance_status": insurance_status,
        "next_payment_date": next_payment_date,
    }
    ctx = recompute(ctx)

    hud_df = build_hud_export_df(ctx)

    # show HUD df for sanity
    st.dataframe(hud_df, use_container_width=True, hide_index=True)

    excel_bytes = to_excel_bytes(hud_df=hud_df, checks_df=checks_df)

    st.download_button(
        "⬇️ Download HUD Excel",
        data=excel_bytes,
        file_name=f"HUD_{deal_number}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        disabled=not qualifies,  # only allow download if qualifies
    )

with tab_picklists:
    st.subheader("Picklist search (e.g., foreclosure / performing)")
    st.caption("This searches picklist VALUES across objects and shows which fields contain those words.")
    kw1, kw2 = st.columns(2)
    k1 = kw1.text_input("Keyword 1", value="foreclosure")
    k2 = kw2.text_input("Keyword 2", value="performing")

    objects_default = ["Property__c", "Loan__c", "Opportunity", "Advance__c"]
    obj_str = st.text_input("Objects to scan (comma-separated)", value=", ".join(objects_default))
    objs = [o.strip() for o in obj_str.split(",") if o.strip()]

    if st.button("Search picklists"):
        df = search_picklists_for_keywords(objs, [k1, k2])
        if df.empty:
            st.warning("No picklists found containing those keywords.")
        else:
            st.dataframe(df, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Show full picklist options for a specific field")
    c1, c2 = st.columns(2)
    obj = c1.text_input("Object API Name", value="Property__c")
    fld = c2.text_input("Field API Name", value="Loan_Status__c")
    if st.button("Show picklist options"):
        vals = get_picklist_values(obj, fld)
        if not vals:
            st.warning("No picklist values found (field not picklist, wrong name, or no access).")
        else:
            st.write(f"{len(vals)} values:")
            st.dataframe(pd.DataFrame({"value": vals}), use_container_width=True, hide_index=True)
