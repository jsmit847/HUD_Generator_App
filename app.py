# ============================================
# HUD GENERATOR (Streamlit / app.py) — Salesforce-native (NO Hayden / NO FCI / NO HTML)
# - Deal identifier = Deal Number (Deal_Loan_Number__c)
# - Pulls HUD inputs + validation checks from Salesforce
# - Shows “Pre-checks / Qualification” FIRST (late fees, insurance status, servicer status, etc.)
# - If qualified: user can enter remaining manual inputs + Download Excel HUD
#
# Requirements:
#   pip install simple-salesforce openpyxl requests
#
# Streamlit secrets (example):
# [salesforce]
# client_id = "..."
# auth_host = "https://cvest.my.salesforce.com"
# redirect_uri = "https://YOUR-APP.streamlit.app"  # MUST match Connected App callback EXACTLY
# client_secret = "..."  # optional (PKCE can work without, but you have it)
# ============================================

import re
import io
import time
import html
import base64
import hashlib
import secrets
import urllib.parse
from datetime import datetime, date

import pandas as pd
import requests
import streamlit as st
from simple_salesforce import Salesforce
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter


# =========================
# PAGE CONFIG
# =========================
st.set_page_config(page_title="HUD Generator", page_icon="🏗️", layout="wide")
st.title("🏗️ HUD Generator (Salesforce-native)")
st.caption("Enter Deal Number → run pre-checks → if qualified, fill remaining inputs → download Excel HUD")


# =========================
# HELPERS
# =========================
def digits_only(x) -> str:
    if x is None:
        return ""
    return re.sub(r"\D", "", str(x))

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

def pct_to_display(x) -> str:
    """Accept ratio (0.8) or percent (80) or '80%' and return '80%'."""
    if x is None:
        return ""
    s = str(x).strip()
    if s == "":
        return ""
    s = s.replace("%", "").strip()
    try:
        v = float(s)
    except Exception:
        return ""
    if 0 < v <= 1:
        v *= 100
    return f"{v:.0f}%"

def parse_date_to_mmddyyyy(s: str) -> str:
    t = str(s).strip() if s is not None else ""
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

def safe_get(d: dict, key: str, default=None):
    return d.get(key, default) if isinstance(d, dict) else default

def picklist_values_from_describe(sf, obj: str, field_api: str) -> list[str]:
    try:
        desc = getattr(sf, obj).describe()
        for f in desc.get("fields", []):
            if f.get("name") == field_api and f.get("type") == "picklist":
                vals = []
                for pv in f.get("picklistValues", []) or []:
                    if pv.get("active", True):
                        vals.append(pv.get("label") or pv.get("value"))
                return [v for v in vals if v]
    except Exception:
        return []
    return []

def recompute(ctx: dict) -> dict:
    # Allocated Loan Amount = Advance Amount + Total Reno Drawn
    ctx["allocated_loan_amount"] = float(ctx.get("advance_amount", 0.0)) + float(ctx.get("total_reno_drawn", 0.0))

    # Construction Advance Amount = Advance Amount
    ctx["construction_advance_amount"] = float(ctx.get("advance_amount", 0.0))

    # Fees
    fee_keys = ["inspection_fee", "wire_fee", "construction_mgmt_fee", "title_fee"]
    ctx["total_fees"] = sum(float(ctx.get(k, 0.0)) for k in fee_keys)

    # Late charges: NOT front-facing as a HUD line item anymore (still a check)
    # Keep it in ctx for reporting only:
    ctx["late_charges_amt"] = float(ctx.get("late_charges_amt", 0.0))

    # Net Amount to Borrower = Construction Advance Amount - Total Fees
    ctx["net_amount_to_borrower"] = ctx["construction_advance_amount"] - ctx["total_fees"]

    # Available Balance rule (your prior rule)
    ctx["available_balance"] = (
        float(ctx.get("total_loan_amount", 0.0))
        - float(ctx.get("initial_advance", 0.0))
        - float(ctx.get("total_reno_drawn", 0.0))
        - float(ctx.get("advance_amount", 0.0))
        - float(ctx.get("interest_reserve", 0.0))
    )
    return ctx


# =========================
# SALESFORCE OAUTH (PKCE) — like your test
# =========================
cfg = st.secrets["salesforce"]
CLIENT_ID = cfg["client_id"]
AUTH_HOST = cfg.get("auth_host", "https://login.salesforce.com").rstrip("/")
REDIRECT_URI = cfg["redirect_uri"]  # DO NOT rstrip('/') — must match Connected App EXACTLY
CLIENT_SECRET = cfg.get("client_secret")  # optional

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

# Handle redirect back
if code:
    if not state or state not in store:
        st.error("Missing/expired OAuth state. Click login again.")
        st.stop()
    verifier, _t0 = store.pop(state)
    tok = exchange_code_for_token(code, verifier)
    st.session_state.sf_token = tok
    st.query_params.clear()
    st.rerun()

# Not authed yet
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

    st.info("Authenticate with Salesforce to use the HUD Generator.")
    st.link_button("Login to Salesforce", login_url)

    with st.expander("OAuth Debug (redirect_uri mismatch lives here)"):
        st.write("AUTH_HOST:", AUTH_HOST)
        st.write("REDIRECT_URI sent:", REDIRECT_URI)
        st.caption("Salesforce Connected App callback URL must match this EXACTLY (including trailing slash).")
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

topL, topR = st.columns([3, 1])
with topL:
    st.success("✅ Salesforce authenticated")
    st.write("instance_url:", instance_url)
with topR:
    if st.button("Log out / Clear token"):
        st.session_state.sf_token = None
        st.rerun()


# =========================
# SF QUERIES (Deal Number → Opportunity → Property/Loan/Advance/Account)
# =========================
def sf_query_all(soql: str) -> list[dict]:
    res = sf.query_all(soql)
    return res.get("records", []) or []

def pick_best_opportunity_by_deal_number(deal_number_digits: str) -> dict | None:
    # Prefer exact match; fallback to LIKE contains
    dn = deal_number_digits
    if not dn:
        return None

    soql_exact = f"""
    SELECT Id, Name, Deal_Loan_Number__c, AccountId, Account_Name__c, StageName, RecordType.Name
    FROM Opportunity
    WHERE Deal_Loan_Number__c = '{dn}'
    ORDER BY LastModifiedDate DESC
    LIMIT 5
    """.strip()

    rows = sf_query_all(soql_exact)
    if rows:
        return rows[0]

    soql_like = f"""
    SELECT Id, Name, Deal_Loan_Number__c, AccountId, Account_Name__c, StageName, RecordType.Name
    FROM Opportunity
    WHERE Deal_Loan_Number__c LIKE '%{dn}%'
    ORDER BY LastModifiedDate DESC
    LIMIT 5
    """.strip()
    rows = sf_query_all(soql_like)
    return rows[0] if rows else None

def fetch_property_for_deal(opp_id: str) -> dict | None:
    # We’ll take the most recently modified property if multiple
    soql = f"""
    SELECT
      Id,
      Deal__c,
      Servicer_Id__c,
      Borrower_Name__c,
      Full_Address__c,
      Yardi_Id__c,
      Initial_Disbursement_Used__c,
      Renovation_Advance_Amount_Used__c,
      Interest_Allocation__c,
      Holdback_To_Rehab_Ratio__c,
      Late_Fees_Servicer__c,
      Insurance_Status__c,
      Loan_Status__c,
      Status__c
    FROM Property__c
    WHERE Deal__c = '{opp_id}'
    ORDER BY LastModifiedDate DESC
    LIMIT 1
    """.strip()
    rows = sf_query_all(soql)
    return rows[0] if rows else None

def fetch_loan_for_deal(opp_id: str) -> dict | None:
    # Some orgs relate Loan__c to Opportunity via Deal__c or Opportunity__c.
    # We try common link fields by probing.
    candidates = [
        ("Deal__c", opp_id),
        ("Opportunity__c", opp_id),
    ]
    for field, val in candidates:
        soql = f"""
        SELECT
          Id,
          {field},
          Servicer_Loan_Status__c,
          Servicer_Loan_Id__c,
          Next_Payment_Date__c,
          Late_Fees_Servicer__c
        FROM Loan__c
        WHERE {field} = '{val}'
        ORDER BY LastModifiedDate DESC
        LIMIT 1
        """.strip()
        try:
            rows = sf_query_all(soql)
            if rows:
                return rows[0]
        except Exception:
            continue
    return None

def fetch_latest_advance_for_deal(opp_id: str) -> dict | None:
    # Commitment is on Advance__c (LOC_Commitment__c). Also has Status fields.
    soql = f"""
    SELECT
      Id,
      Deal__c,
      LOC_Commitment__c,
      Status__c,
      LoanDocumentStatus__c,
      Wire_Date__c
    FROM Advance__c
    WHERE Deal__c = '{opp_id}'
    ORDER BY CreatedDate DESC
    LIMIT 1
    """.strip()
    rows = sf_query_all(soql)
    return rows[0] if rows else None

def fetch_account_vendor_code(account_id: str) -> dict | None:
    if not account_id:
        return None
    soql = f"""
    SELECT Id, Name, Yardi_Vendor_Code__c
    FROM Account
    WHERE Id = '{account_id}'
    LIMIT 1
    """.strip()
    rows = sf_query_all(soql)
    return rows[0] if rows else None


# =========================
# EXCEL EXPORT (HUD-like layout)
# =========================
def build_hud_excel_bytes(ctx: dict, checks: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "HUD"

    # Formatting
    bold = Font(bold=True)
    big = Font(bold=True, size=14)
    thin = Side(style="thin", color="000000")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    def set_cell(r, c, v, b=False, fmt=None, align="left", boxed=False):
        cell = ws.cell(r, c, v)
        if b:
            cell.font = bold
        if fmt:
            cell.number_format = fmt
        cell.alignment = Alignment(horizontal=align, vertical="center", wrap_text=True)
        if boxed:
            cell.border = border
        return cell

    # Column widths
    widths = {1: 30, 2: 22, 3: 30, 4: 28}
    for col, w in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = w

    # Header
    set_cell(1, 1, "COREVEST AMERICAN FINANCE LENDER LLC", b=True)
    set_cell(2, 1, "4 Park Plaza, Suite 900, Irvine, CA 92614")
    ws.cell(3, 1, "Final Settlement Statement").font = big

    # Key section
    r0 = 5
    rows = [
        ("Total Loan Amount:", ctx.get("total_loan_amount", 0.0), "money", "Loan ID:", ctx.get("deal_number","")),
        ("Initial Advance:",   ctx.get("initial_advance", 0.0), "money", "Holdback % Current:", ctx.get("holdback_current","")),
        ("Total Reno Drawn:",  ctx.get("total_reno_drawn", 0.0), "money", "Holdback % at Closing:", ctx.get("holdback_closing","")),
        ("Advance Amount:",    ctx.get("advance_amount", 0.0), "money", "Allocated Loan Amount:", ctx.get("allocated_loan_amount", 0.0)),
        ("Interest Reserve:",  ctx.get("interest_reserve", 0.0), "money", "Net Amount to Borrower:", ctx.get("net_amount_to_borrower", 0.0)),
        ("Available Balance:", ctx.get("available_balance", 0.0), "money", "Workday SUP Code:", ctx.get("workday_sup_code","")),
        ("Advance Date:",      ctx.get("advance_date",""), "text", "Yardi Vendor Code:", ctx.get("yardi_vendor_code","")),
    ]

    for i, (l1, v1, t1, l2, v2) in enumerate(rows):
        rr = r0 + i
        set_cell(rr, 1, l1, b=True)
        if t1 == "money":
            set_cell(rr, 2, float(v1 or 0.0), fmt='"$"#,##0.00', align="right", boxed=True)
        else:
            set_cell(rr, 2, v1 or "", boxed=True)

        set_cell(rr, 3, l2, b=True)
        if isinstance(v2, (int, float)) and "Amount" in str(l2):
            set_cell(rr, 4, float(v2 or 0.0), fmt='"$"#,##0.00', align="right", boxed=True)
        elif isinstance(v2, (int, float)) and "Borrower" not in str(l2):
            # for net/allocated money rows
            if l2 in ("Allocated Loan Amount:", "Net Amount to Borrower:"):
                set_cell(rr, 4, float(v2 or 0.0), fmt='"$"#,##0.00', align="right", boxed=True)
            else:
                set_cell(rr, 4, v2, boxed=True)
        else:
            set_cell(rr, 4, v2 or "", boxed=True)

    # Borrower / Address
    rr = r0 + len(rows) + 1
    set_cell(rr, 1, "Borrower:", b=True)
    set_cell(rr, 2, ctx.get("borrower_disp",""), boxed=True)
    rr += 1
    set_cell(rr, 1, "Address:", b=True)
    set_cell(rr, 2, ctx.get("address_disp",""), boxed=True)
    ws.merge_cells(start_row=rr, start_column=2, end_row=rr, end_column=4)
    ws.cell(rr, 2).alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)

    # Charges table
    rr += 2
    set_cell(rr, 1, "Charge Description", b=True, boxed=True)
    set_cell(rr, 4, "Amount", b=True, align="right", boxed=True)
    ws.merge_cells(start_row=rr, start_column=1, end_row=rr, end_column=3)

    rr += 1
    charges = [
        ("Construction Advance Amount", float(ctx.get("construction_advance_amount", 0.0))),
        ("3rd party Inspection Fee", float(ctx.get("inspection_fee", 0.0))),
        ("Wire Fee", float(ctx.get("wire_fee", 0.0))),
        ("Construction Management Fee", float(ctx.get("construction_mgmt_fee", 0.0))),
        ("Title Fee", float(ctx.get("title_fee", 0.0))),
    ]
    for desc, amt in charges:
        ws.merge_cells(start_row=rr, start_column=1, end_row=rr, end_column=3)
        set_cell(rr, 1, desc, boxed=True)
        set_cell(rr, 4, amt, fmt='"$"#,##0.00', align="right", boxed=True)
        rr += 1

    # Total fees + reimbursement
    set_cell(rr, 1, "Total Fees", b=True, boxed=True)
    ws.merge_cells(start_row=rr, start_column=1, end_row=rr, end_column=3)
    set_cell(rr, 4, float(ctx.get("total_fees", 0.0)), fmt='"$"#,##0.00', align="right", boxed=True)
    rr += 1
    set_cell(rr, 1, "Reimbursement to Borrower", b=True, boxed=True)
    ws.merge_cells(start_row=rr, start_column=1, end_row=rr, end_column=3)
    set_cell(rr, 4, float(ctx.get("net_amount_to_borrower", 0.0)), fmt='"$"#,##0.00', align="right", boxed=True)

    # Checks sheet
    ws2 = wb.create_sheet("Checks")
    ws2.column_dimensions["A"].width = 34
    ws2.column_dimensions["B"].width = 80
    ws2["A1"] = "Check"
    ws2["B1"] = "Result"
    ws2["A1"].font = bold
    ws2["B1"].font = bold

    r = 2
    for k, v in checks.items():
        ws2[f"A{r}"] = k
        ws2[f"B{r}"] = str(v)
        r += 1

    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


# =========================
# UI — Deal Number → Pre-checks → Qualified → Build HUD + Excel
# =========================
st.subheader("1) Enter Deal Number")
with st.form("deal_form"):
    deal_number_input = st.text_input("Deal Number", placeholder="e.g., 58439")
    run_checks = st.form_submit_button("Run Pre-checks ✅")

if not run_checks:
    st.stop()

deal_number_digits = digits_only(deal_number_input)
if not deal_number_digits:
    st.error("Please enter a Deal Number (digits).")
    st.stop()

# ---- Pull core records ----
with st.spinner("Querying Salesforce..."):
    opp = pick_best_opportunity_by_deal_number(deal_number_digits)
    if not opp:
        st.error("No Opportunity found for that Deal Number (Deal_Loan_Number__c).")
        st.stop()

    opp_id = opp.get("Id")
    prop = fetch_property_for_deal(opp_id) if opp_id else None
    loan = fetch_loan_for_deal(opp_id) if opp_id else None
    adv  = fetch_latest_advance_for_deal(opp_id) if opp_id else None
    acct = fetch_account_vendor_code(opp.get("AccountId")) if opp.get("AccountId") else None

# ---- Build ctx from SF fields you mapped ----
# Total Loan Amount: from Advance__c.LOC_Commitment__c (Loan Commitment)
total_loan_amount = parse_money(safe_get(adv, "LOC_Commitment__c", 0.0))

# Initial Advance: Property__c.Initial_Disbursement_Used__c
initial_advance = parse_money(safe_get(prop, "Initial_Disbursement_Used__c", 0.0))

# Total Reno Drawn: Property__c.Renovation_Advance_Amount_Used__c
total_reno_drawn = parse_money(safe_get(prop, "Renovation_Advance_Amount_Used__c", 0.0))

# Interest Reserve: Property__c.Interest_Allocation__c
interest_reserve = parse_money(safe_get(prop, "Interest_Allocation__c", 0.0))

# Borrower / Address: Property__c.Borrower_Name__c / Full_Address__c
borrower_name = (safe_get(prop, "Borrower_Name__c", "") or "").strip().upper()
full_address = (safe_get(prop, "Full_Address__c", "") or "").strip().upper()

# Holdback % Current: Property__c.Holdback_To_Rehab_Ratio__c (ratio)
holdback_current = pct_to_display(safe_get(prop, "Holdback_To_Rehab_Ratio__c", ""))

# Late fees check: Property__c.Late_Fees_Servicer__c (also possibly on Loan__c; we’ll prefer Property)
late_fees_amt = parse_money(safe_get(prop, "Late_Fees_Servicer__c", None))
if late_fees_amt == 0.0 and loan:
    late_fees_amt = parse_money(safe_get(loan, "Late_Fees_Servicer__c", 0.0))

# Insurance status check: Property__c.Insurance_Status__c (picklist)
insurance_status = safe_get(prop, "Insurance_Status__c", "")

# Servicer loan status: Loan__c.Servicer_Loan_Status__c OR Property__c.Loan_Status__c
servicer_loan_status = safe_get(loan, "Servicer_Loan_Status__c", "") if loan else ""
if not servicer_loan_status:
    servicer_loan_status = safe_get(prop, "Loan_Status__c", "")

# Next Payment Date from Loan__c/Opportunity/Property (for snapshot)
next_payment_date = ""
if loan and safe_get(loan, "Next_Payment_Date__c"):
    next_payment_date = safe_get(loan, "Next_Payment_Date__c")
elif safe_get(opp, "Next_Payment_Date__c"):
    next_payment_date = safe_get(opp, "Next_Payment_Date__c")
elif prop and safe_get(prop, "Next_Payment_Date__c"):
    next_payment_date = safe_get(prop, "Next_Payment_Date__c")

# Yardi Vendor Code from Account (per your latest ask)
yardi_vendor_code = safe_get(acct, "Yardi_Vendor_Code__c", "") if acct else ""

# We are making Workday SUP Code manual for now (per your request)
# Also: Holdback % at Closing remains manual like before.

# ---- Pre-check logic / qualification ----
# You can tune these rules. For now:
#   - insurance must be "Outside Policy In-Force" OR contain "In-Force"
#   - late fees must be 0
#   - servicer status must not contain foreclosure / non-performing (simple keyword check)
ins_ok = False
if isinstance(insurance_status, str):
    s = insurance_status.strip().lower()
    ins_ok = ("outside policy in-force" in s) or ("in-force" in s)

late_ok = float(late_fees_amt or 0.0) <= 0.0

status_ok = True
bad_words = ["foreclosure", "non-performing", "nonperforming", "default"]
if isinstance(servicer_loan_status, str):
    s = servicer_loan_status.strip().lower()
    if any(w in s for w in bad_words):
        status_ok = False

qualified = ins_ok and late_ok and status_ok

# ---- Display snapshot + guidance ----
st.subheader("2) Pre-checks / Qualification")
m1, m2, m3, m4 = st.columns(4)
m1.metric("Deal Number", deal_number_digits)
m2.metric("Opportunity", (opp.get("Name") or "")[:40])
m3.metric("Stage", opp.get("StageName") or "")
m4.metric("Record Type", safe_get(opp.get("RecordType", {}), "Name", ""))

c1, c2, c3 = st.columns(3)
c1.metric("Insurance Status", insurance_status or "(blank)")
c2.metric("Late Fees (Servicer)", fmt_money(late_fees_amt))
c3.metric("Servicer Loan Status", servicer_loan_status or "(blank)")

x1, x2 = st.columns(2)
with x1:
    st.write("**Next Payment Date:**", next_payment_date or "(blank)")
    st.write("**Borrower:**", borrower_name or "(blank)")
with x2:
    st.write("**Address:**", full_address or "(blank)")
    st.write("**Yardi Vendor Code (Account):**", yardi_vendor_code or "(blank)")

st.divider()

if qualified:
    st.success("✅ Qualified to build HUD (based on current pre-check rules).")
else:
    st.error("🚫 Not qualified to build HUD yet.")
    reasons = []
    if not ins_ok:
        reasons.append("Insurance status is not in-force (or blank). Recheck Insurance_Status__c on Property__c.")
    if not late_ok:
        reasons.append("Late fees are non-zero. Recheck Late_Fees_Servicer__c.")
    if not status_ok:
        reasons.append("Servicer status indicates a problem (foreclosure/non-performing/default). Recheck Servicer_Loan_Status__c.")
    for r in reasons:
        st.warning(r)

    with st.expander("Optional: show picklist options (to help you verify expected values)"):
        # show picklist choices for the fields used in checks
        opts_ins = picklist_values_from_describe(sf, "Property__c", "Insurance_Status__c")
        if opts_ins:
            st.write("**Property__c.Insurance_Status__c options:**")
            st.write(opts_ins)
        else:
            st.write("No picklist options found (field may not be picklist or no describe access).")

        opts_stat = picklist_values_from_describe(sf, "Loan__c", "Servicer_Loan_Status__c")
        if opts_stat:
            st.write("**Loan__c.Servicer_Loan_Status__c options:**")
            st.write(opts_stat)
        else:
            st.write("No picklist options found (field may not be picklist or link differs in this org).")

    st.stop()

# =========================
# Build HUD inputs (manual + SF) and export Excel
# =========================
st.subheader("3) Build HUD (manual inputs + Salesforce values)")
with st.form("hud_form"):
    # Manual inputs remaining
    advance_amount = st.number_input("Advance Amount (manual)", min_value=0.0, step=0.01, format="%.2f")
    holdback_closing_raw = st.text_input("Holdback % at Closing (manual)", placeholder="100")
    workday_sup_code_input = st.text_input("Workday SUP Code (manual for now)", placeholder="SUP12345")
    advance_date_raw = st.text_input("Advance Date (manual)", placeholder="MM/DD/YYYY")

    st.markdown("**Fees (manual):**")
    f1, f2, f3, f4 = st.columns(4)
    inspection_fee = f1.number_input("3rd party Inspection Fee", min_value=0.0, step=0.01, format="%.2f")
    wire_fee = f2.number_input("Wire Fee", min_value=0.0, step=0.01, format="%.2f")
    construction_mgmt_fee = f3.number_input("Construction Management Fee", min_value=0.0, step=0.01, format="%.2f")
    title_fee = f4.number_input("Title Fee", min_value=0.0, step=0.01, format="%.2f")

    build_clicked = st.form_submit_button("Build Excel HUD ✅")

if not build_clicked:
    st.stop()

ctx = {
    "deal_number": deal_number_digits,

    "total_loan_amount": total_loan_amount,
    "initial_advance": initial_advance,
    "total_reno_drawn": total_reno_drawn,
    "interest_reserve": interest_reserve,

    "advance_amount": float(advance_amount),

    "holdback_current": holdback_current,
    "holdback_closing": pct_to_display(holdback_closing_raw),

    "advance_date": parse_date_to_mmddyyyy(advance_date_raw),
    "workday_sup_code": (workday_sup_code_input or "").strip(),

    "borrower_disp": borrower_name,
    "address_disp": full_address,

    "inspection_fee": float(inspection_fee),
    "wire_fee": float(wire_fee),
    "construction_mgmt_fee": float(construction_mgmt_fee),
    "title_fee": float(title_fee),

    # Reporting only (not a HUD line item anymore)
    "late_charges_amt": float(late_fees_amt or 0.0),

    # For convenience
    "yardi_vendor_code": yardi_vendor_code,
}

ctx = recompute(ctx)

checks = {
    "Qualified": qualified,
    "Insurance Status (Property__c.Insurance_Status__c)": insurance_status,
    "Late Fees (Property/Loan Late_Fees_Servicer__c)": fmt_money(late_fees_amt),
    "Servicer Loan Status (Loan__c.Servicer_Loan_Status__c)": servicer_loan_status,
    "Next Payment Date": next_payment_date,
    "Opportunity Stage": opp.get("StageName") or "",
    "Opportunity Record Type": safe_get(opp.get("RecordType", {}), "Name", ""),
}

st.subheader("HUD Summary (computed)")
a, b, c = st.columns(3)
a.metric("Allocated Loan Amount", fmt_money(ctx["allocated_loan_amount"]))
b.metric("Net Amount to Borrower", fmt_money(ctx["net_amount_to_borrower"]))
c.metric("Available Balance", fmt_money(ctx["available_balance"]))

with st.expander("Show full ctx (debug)"):
    st.json(ctx)

excel_bytes = build_hud_excel_bytes(ctx, checks)
fname = f"HUD_{deal_number_digits}.xlsx"
st.download_button("⬇️ Download HUD (Excel)", data=excel_bytes, file_name=fname, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
