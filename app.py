# ============================================================
# HUD Generator App — ONE FILE (Streamlit) — SF-FALLBACK READY
# Fixes requested:
# ✅ Loan ID on template = Deal Number (NOT Yardi)  -> writes Deal_Loan_Number__c into G7
# ✅ Uses your confirmed mappings AND adds robust fallbacks using the field list you provided
# ✅ Pulls from Opportunity + Property__c + Advance__c (and keeps Loan__c for servicer fallback)
# ✅ Loan Commitment prefers Advance__c.LOC_Commitment__c, then Property__c.LOC_Commitment__c, then Opp LOC_Commitment__c/Amount
# ✅ Initial Advance prefers Property__c.Initial_Disbursement_Used__c, then Property__c.Initial_Disbursement__c, then Advance__c.Initial_Disbursement_Total__c
# ✅ Total Reno Drawn prefers Property__c.Renovation_Advance_Amount_Used__c, then Advance__c.Renovation_Reserve_Total__c, then Opp Total_Amount_Advances__c (last resort)
# ✅ Interest Reserve prefers Property__c.Interest_Allocation__c, then Opp Interest_Reserves__c / Current_* fields, then Advance__c Interest reserve totals
# ✅ Borrower + Address prefer Property__c, then Opportunity/Account fallbacks
#
# NEW FIXES ADDED (for "works for me but not for them"):
# ✅ Describe cache is PER SESSION (no cross-user permission leakage)
# ✅ Permission/FLS/object access errors return empty results (doesn't crash app)
# ✅ Loan__c fetch is NON-BLOCKING (permissions won't break checks)
# ============================================================

import base64
import hashlib
import io
import re
import secrets
import time
import urllib.parse
from datetime import date
from pathlib import Path
from typing import Dict, Tuple

import pandas as pd
import requests
import streamlit as st
from simple_salesforce import Salesforce
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font

# -----------------------------
# PAGE + STYLE
# -----------------------------
st.set_page_config(page_title="HUD Generator (Salesforce)", layout="wide")
st.markdown(
    """
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
  .pill {
    display:inline-block; padding: 2px 10px; border-radius: 999px;
    border: 1px solid rgba(49,51,63,.18); background: rgba(255,255,255,.7);
    font-size: 0.85rem;
    margin-right: 6px;
    margin-top: 6px;
  }
  [data-testid="stTextInput"] input, [data-testid="stNumberInput"] input, [data-testid="stDateInput"] input {
    border-radius: 10px !important;
    padding: 10px 12px !important;
    font-size: 1rem !important;
  }
</style>
""",
    unsafe_allow_html=True,
)
st.title("HUD Generator App")
st.caption("Use the sidebar to switch between the HUD Generator and the Construction Checklist workflow.")
if "debug_last_sf_error" not in st.session_state:
    st.session_state.debug_last_sf_error = None

# -----------------------------
# TEMPLATE SETTINGS
# -----------------------------
APP_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = APP_DIR / "HUD TEMPLATE.xlsx"
TEMPLATE_SHEET = "TL-15255"

# Template mapping (unchanged positions; Loan ID cell gets DEAL #)
CELL_MAP = {
    "total_loan_amount": "D7",
    "initial_advance": "D8",
    "total_reno_drawn": "D9",
    "advance_amount": "D10",
    "interest_reserve": "D11",

    "deal_number": "G7",      # ✅ Loan ID cell now = Deal #
    "advance_date": "I13",

    "borrower_disp": "D14",
    "address_disp": "D15",

    "inspection_fee": "H21",
    "wire_fee": "H22",
    "construction_mgmt_fee": "H23",
    "title_fee": "H24",
}

# -----------------------------
# SECRETS
# -----------------------------
cfg = st.secrets["salesforce"]
CLIENT_ID = cfg["client_id"]
AUTH_HOST = cfg.get("auth_host", "https://cvest.my.salesforce.com").rstrip("/")
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
    return {}

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

def digits_only(x: str) -> str:
    return re.sub(r"\D", "", x or "")

def normalize_text(x):
    return "" if x is None else str(x).strip()

def pick_first(*vals):
    for v in vals:
        if v is None:
            continue
        s = str(v).strip()
        if s != "":
            return s
    return ""

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

def norm(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = (
        df.columns.astype(str)
        .str.strip()
        .str.lower()
        .str.replace(r"\s+", "_", regex=True)
        .str.replace(r"[^0-9a-z_]+", "", regex=True)
    )
    return df

def extract_order_id_deal_prefix(order_id_val: str) -> str:
    if not order_id_val:
        return ""
    left = str(order_id_val).split("-", 1)[0].strip()
    return digits_only(left)

def strip_zip4(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"(\b\d{5})-\d{4}\b", r"\1", str(s))

DIR_MAP = {
    "north": "n", "n": "n",
    "south": "s", "s": "s",
    "east": "e", "e": "e",
    "west": "w", "w": "w",
    "northeast": "ne", "ne": "ne",
    "northwest": "nw", "nw": "nw",
    "southeast": "se", "se": "se",
    "southwest": "sw", "sw": "sw",
}
STATE_MAP = {"oregon": "or", "or": "or", "washington": "wa", "wa": "wa", "california": "ca", "ca": "ca"}
SUFFIX_MAP = {
    "street": "st", "st": "st",
    "avenue": "ave", "ave": "ave",
    "road": "rd", "rd": "rd",
    "drive": "dr", "dr": "dr",
    "lane": "ln", "ln": "ln",
    "court": "ct", "ct": "ct",
    "place": "pl", "pl": "pl",
    "terrace": "ter", "ter": "ter",
    "trail": "trl", "trl": "trl",
    "circle": "cir", "cir": "cir",
    "boulevard": "blvd", "blvd": "blvd",
    "parkway": "pkwy", "pkwy": "pkwy",
}

def address_tokens(s: str) -> set:
    if not s:
        return set()
    s = strip_zip4(str(s)).lower()
    s = re.sub(r"[,#]", " ", s)
    s = s.replace("-", " ")
    s = re.sub(r"[^0-9a-z\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    toks = s.split()
    out = []
    for t in toks:
        if t in DIR_MAP:
            out.append(DIR_MAP[t])
        elif t in STATE_MAP:
            out.append(STATE_MAP[t])
        elif t in SUFFIX_MAP:
            out.append(SUFFIX_MAP[t])
        else:
            out.append(t)
    return set(out)

def zip5_from_addr(s: str) -> str:
    s = strip_zip4(s or "")
    m = re.search(r"\b(\d{5})\b", s)
    return m.group(1) if m else ""

def house_num_from_addr(s: str) -> str:
    m = re.match(r"\s*(\d+)\b", (s or "").strip())
    return m.group(1) if m else ""

def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0

def pick_first_nonblank_field(record: dict, fields: list):
    """
    Returns (field_name, value) for first field with nonblank value.
    """
    if not record:
        return None, None
    for f in fields:
        if f in record:
            v = record.get(f)
            if v is None:
                continue
            s = str(v).strip()
            if s != "":
                return f, v
    return None, None

# -----------------------------
# OAUTH FLOW (PKCE)
# -----------------------------
qp = st.query_params
code = qp.get("code")
state = qp.get("state")
err = qp.get("error")
err_desc = qp.get("error_description")

if err:
    st.error(f"Login error: {err}")
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
        st.error("Login link expired. Click login again.")
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
    st.info("Step 1: Log in.")
    st.link_button("Login", login_url)
    st.stop()

tok = st.session_state.sf_token
access_token = tok.get("access_token")
instance_url = tok.get("instance_url")

if not access_token or not instance_url:
    st.error("Login token missing needed values.")
    st.stop()

sf = Salesforce(instance_url=instance_url, session_id=access_token)

topc1, topc2 = st.columns([3, 1])
with topc1:
    st.success("✅ Logged in")
    st.caption(f"Connected to: {instance_url}")
with topc2:
    if st.button("Log out"):
        st.session_state.sf_token = None
        st.rerun()

# -----------------------------
# OPTIONAL FCI CONFIG
# -----------------------------
FCI_DEFAULT_URL = "https://fapi.myfci.com/graphql"
FCI_LOAN_INFORMATION_QUERY = """
query GetLoanInformation {
  getLoanInformation {
    poffUnpaidLateCharges
    lateChargesDays
    lateChargesPct
    lenderAccount
    maturityDate
    nextDueDate
    noteRate
  }
}
"""


def get_fci_config() -> dict:
    try:
        cfg = st.secrets.get("fci", {})
    except Exception:
        cfg = {}

    url = normalize_text(cfg.get("url")) if hasattr(cfg, "get") else ""
    token = normalize_text(cfg.get("api_token")) if hasattr(cfg, "get") else ""
    return {
        "enabled": bool(token),
        "url": url or FCI_DEFAULT_URL,
        "api_token": token,
    }


@st.cache_data(ttl=300, show_spinner=False)
def fetch_fci_loan_information_rows(url: str, api_token: str) -> dict:
    if not url or not api_token:
        return {"ok": False, "rows": [], "error": "FCI API token is not configured."}

    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }
    payload = {"query": FCI_LOAN_INFORMATION_QUERY, "variables": {}}

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        result = response.json()
    except Exception as exc:
        return {"ok": False, "rows": [], "error": f"FCI request failed: {exc}"}

    if "errors" in result:
        return {"ok": False, "rows": [], "error": json.dumps(result["errors"], indent=2)}

    records = (result.get("data") or {}).get("getLoanInformation") or []
    if isinstance(records, dict):
        records = [records]
    if not isinstance(records, list):
        records = []

    cleaned_rows = []
    for record in records:
        if not isinstance(record, dict):
            continue
        cleaned_rows.append(
            {
                "poffUnpaidLateCharges": record.get("poffUnpaidLateCharges"),
                "lateChargesDays": record.get("lateChargesDays"),
                "lateChargesPct": record.get("lateChargesPct"),
                "lenderAccount": record.get("lenderAccount"),
                "maturityDate": record.get("maturityDate"),
                "nextDueDate": record.get("nextDueDate"),
                "noteRate": record.get("noteRate"),
            }
        )

    return {"ok": True, "rows": cleaned_rows, "error": ""}

# -----------------------------
# LOAD EXCEL CHECK FILES
# -----------------------------
_CANDIDATES = [
    "OSC_Zstatus_COREVEST_2026-04-14_202850.xlsx",
    "OSC_Zstatus_COREVEST_2026-04-14_202850.xlsx,
]
CAF_CANDIDATES = [
    "Corevest_CAF National 52874_3.9.26.xlsx",
    "Corevest_CAF National 52874_3.9.26.xlsx",
]

def first_existing_path(candidates):
    for c in candidates:
        p1 = Path(c)
        if p1.exists():
            return str(p1)
        p2 = Path("/mnt/data") / c
        if p2.exists():
            return str(p2)
    return candidates[0]

@st.cache_data(show_spinner=False)
def load_osc_excel():
    path = first_existing_path(OSC_CANDIDATES)
    try:
        x = pd.read_excel(path, sheet_name=None, dtype=str)
        df = x["COREVEST"] if "COREVEST" in x else list(x.values())[0]
        df = norm(df)
        return df, path, None
    except Exception as e:
        return pd.DataFrame(), path, str(e)

@st.cache_data(show_spinner=False)
def load_caf_excel():
    path = first_existing_path(CAF_CANDIDATES)
    try:
        x = pd.read_excel(path, sheet_name=None, dtype=str)
        df = x["Completed"] if "Completed" in x else list(x.values())[0]
        df = norm(df)
        return df, path, None
    except Exception as e:
        return pd.DataFrame(), path, str(e)

osc_df, osc_path_used, osc_err = load_osc_excel()
caf_df, caf_path_used, caf_err = load_caf_excel()

# -----------------------------
# DESCRIBE CACHES (FIXED: PER SESSION)
# -----------------------------
# IMPORTANT: st.cache_resource here would be shared across users.
# We want describe results per user session because permissions differ.
if "DESC" not in st.session_state:
    st.session_state.DESC = {}
DESC = st.session_state.DESC

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
        return fields
    return [f for f in fields if f in existing]

def choose_first_existing(obj_name: str, candidates: list):
    existing = get_obj_fields(obj_name)
    if not existing:
        return None
    for c in candidates:
        if c in existing:
            return c
    return None

# -----------------------------
# SAFE QUERY (FIXED: PERMISSION ERRORS DON'T CRASH)
# -----------------------------
def sf_query_all(sf: Salesforce, soql: str):
    return sf.query_all(soql).get("records", [])

def _is_perm_error(msg: str) -> bool:
    m = (msg or "").lower()
    needles = [
        "insufficient", "permission", "not authorized", "not permitted",
        "invalid_type",  # no access to object (or object doesn't exist for user)
        "insufficient_access", "insufficient access",
        "insufficient_privileges",
        "insufficient_access_on_cross_reference_entity",
        "entity is not accessible",
        "field is not accessible",
        "no access", "access denied",
    ]
    return any(n in m for n in needles)

def try_query_drop_missing(sf: Salesforce, obj_name: str, fields, where_clause: str, limit=200, order_by=None):
    fields = list(dict.fromkeys([f for f in fields if f]))
    fields = filter_existing_fields(obj_name, fields)

    # If describe failed or they have no accessible fields, don't crash app
    if not fields:
        st.session_state.debug_last_sf_error = {
            "soql": f"(no accessible fields) FROM {obj_name}",
            "error": "No accessible fields for this user (object/FLS).",
        }
        return [], [], ""

    if order_by:
        ob_field = order_by.split()[0].strip()
        existing = get_obj_fields(obj_name)
        if existing and ob_field not in existing:
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
            st.session_state.debug_last_sf_error = {"soql": soql, "error": msg}

            # FIX: Treat permission/FLS/sharing errors as "no rows" instead of crashing
            if _is_perm_error(msg):
                return [], fields, soql

            if order_by and ("unexpected token" in msg.lower() or "nulls" in msg.lower()):
                order_by = None
                continue

            m1 = re.search(r"No such column '([^']+)'", msg)
            m3 = re.search(r"Invalid field: ([^,]+)", msg)
            m4 = re.search(r"INVALID_FIELD: ([^:]+):", msg)
            bad = None
            if m1:
                bad = m1.group(1).strip()
            elif m3:
                bad = m3.group(1).strip()
            elif m4:
                bad = m4.group(1).strip()

            if bad and bad in fields:
                fields.remove(bad)
                if not fields:
                    # FIX: don't crash whole app
                    return [], [], soql
                continue

            raise RuntimeError("Salesforce query failed.") from e

# -----------------------------
# SF FETCHES
# -----------------------------
def fetch_opportunity_by_deal_number(deal_number: str):
    dn_digits = digits_only((deal_number or "").strip())
    if not dn_digits:
        return None

    opp_fields = [
        "Id", "Name",
        "Deal_Loan_Number__c",
        "Account_Name__c",
        "CloseDate",
        "Servicer_Commitment_Id__c",
        "Servicer_Status__c",
        "Next_Payment_Date__c",
        "Late_Fees_Servicer__c",

        # Fallback monetary fields (from your list)
        "Amount",
        "LOC_Commitment__c",
        "Current_Loan_Amount__c",
        "Final_Loan_Amount__c",
        "Total_Amount_Advances__c",

        # Interest reserve fallbacks
        "Current_Interest_Reserves_Paid__c",
        "Current_Interest_Reserves_Remaining__c",
        "Interest_Reserves__c",
        "Current_UPB_Interest_Reserves__c",
        "Current_UPB_Interest_Reserve__c",
    ]

    where = (
        "("
        f"Deal_Loan_Number__c = {soql_quote(dn_digits)}"
        f" OR Deal_Loan_Number__c LIKE {soql_quote('%' + dn_digits + '%')}"
        ")"
    )
    rows, _used, _soql = try_query_drop_missing(sf, "Opportunity", opp_fields, where, limit=10, order_by="CloseDate DESC")
    if not rows:
        return None
    r = rows[0].copy()
    r.pop("attributes", None)
    return r

def fetch_property_for_deal(opp_id: str):
    lk = choose_first_existing("Property__c", ["Deal__c", "Opportunity__c", "Deal_Id__c", "OpportunityId", "DealId"])
    if not lk:
        return None

    prop_fields = [
        "Id", "Name", lk,
        "Servicer_Id__c",
        "Full_Address__c",
        "Borrower_Name__c",
        "Yardi_Id__c",

        # From your list
        "Initial_Disbursement_Used__c",
        "Initial_Disbursement__c",
        "Initial_Disbursement_Total__c",
        "Initial_Disbursement_Remaining__c",
        "Total_Initial_Disbursement__c",

        "Interest_Allocation__c",

        # Total loan amount fallbacks
        "LOC_Commitment__c",
        "Outstanding_Facility_Amount__c",
        "Current_Outstanding_Loan_Amount__c",
        "Max_Total_Loan_Amount__c",
        "Max_Total_Loan_Amount_Input__c",

        # Total Reno drawn / reserve funded fallbacks (from your list)
        "Renovation_Advance_Amount_Used__c",
        "Approved_Renovation_Holdback__c",
        "Renovation_Reserve_Total__c",  # (may not exist on Property__c; drop-missing loop handles)
        "Interest_Reserves__c",         # listed under "Total Reno Drawn" group in your export

        # Requested/funding date fallbacks
        "Requested_Funding_Date__c",
        "Funding_Date__c",
        "First_Funding_Date__c",

        # Other useful values
        "Holdback_To_Rehab_Ratio__c",
        "Late_Fees_Servicer__c",
    ]

    where = f"{lk} = {soql_quote(opp_id)}"
    try:
        rows, _used, _soql = try_query_drop_missing(sf, "Property__c", prop_fields, where, limit=5, order_by="CreatedDate DESC")
        if not rows:
            return None
        r = rows[0].copy()
        r.pop("attributes", None)
        return r
    except Exception:
        st.warning("⚠️ Could not pull property details. Continuing without them.")
        return None

def fetch_loan_for_deal(opp_id: str):
    """
    FIX: Non-blocking. If user doesn't have Loan__c access/FLS, this returns None.
    """
    lk = choose_first_existing("Loan__c", ["Deal__c", "Opportunity__c", "Deal_Id__c", "OpportunityId", "DealId"])
    if not lk:
        return None

    loan_fields = ["Id", "Name", lk, "Servicer_Loan_Status__c", "Servicer_Loan_Id__c", "Next_Payment_Date__c"]
    where = f"{lk} = {soql_quote(opp_id)}"
    try:
        rows, _used, _soql = try_query_drop_missing(sf, "Loan__c", loan_fields, where, limit=5, order_by="CreatedDate DESC")
    except Exception:
        # extra safety; should be rare now
        return None

    if not rows:
        return None
    r = rows[0].copy()
    r.pop("attributes", None)
    return r

def fetch_advances_for_deal(opp_id: str):
    """
    Pull multiple advances; we will choose values using best "nonblank" priority.
    """
    lk = choose_first_existing("Advance__c", ["Deal__c", "Opportunity__c", "Deal_Id__c", "OpportunityId", "DealId", "Advance__c"])
    if not lk:
        return []

    adv_fields = [
        "Id", "Name", lk,

        # Amounts and key fields from your list
        "LOC_Commitment__c",
        "Advance__c",  # (reference)
        "Approved_Advance_Amount_Total__c",
        "Approved_Advance_Amount_Max_Total__c",
        "Renovation_Reserve_Total__c",
        "Initial_Disbursement_Total__c",

        # Interest reserve totals
        "Interest_Reserve_Total__c",
        "Interest_Reserve_Subtotal__c",
        "Total_Interest_Reserves_andStub_Interest__c",
        "Remaining_Interest_Reserve__c",

        # Dates
        "Target_Advance_Date__c",
        "Wire_Date__c",
        "Date_Advance_Requested__c",
    ]

    where = f"{lk} = {soql_quote(opp_id)}"
    try:
        rows, _used, _soql = try_query_drop_missing(sf, "Advance__c", adv_fields, where, limit=50, order_by="CreatedDate DESC")
        cleaned = []
        for r in rows:
            rr = r.copy()
            rr.pop("attributes", None)
            cleaned.append(rr)
        return cleaned
    except Exception:
        return []

# -----------------------------
# OFFLINE LOOKUPS (OSC + CAF)
# -----------------------------
def osc_lookup(servicer_key: str):
    if osc_df.empty:
        return {"found": False, "error": "Insurance file did not load.", "row": None}
    if "account_number" not in osc_df.columns:
        return {"found": False, "error": "Insurance file is missing the ID field.", "row": None}
    key = (servicer_key or "").strip()
    if not key:
        return {"found": False, "error": "Missing servicer ID.", "row": None}
    hit = osc_df[osc_df["account_number"].astype(str).str.strip() == key]
    if hit.empty:
        return {"found": False, "error": "No insurance record found for that servicer ID.", "row": None}
    return {"found": True, "error": None, "row": hit.iloc[0].to_dict()}

def caf_try_match_by_deal_id(deal_digits: str):
    if caf_df.empty:
        return {"found": False, "error": "Payment file did not load.", "row": None, "method": "deal id"}
    if "order_id" not in caf_df.columns:
        return {"found": False, "error": "Payment file is missing deal IDs.", "row": None, "method": "deal id"}
    dn = digits_only(deal_digits)
    if not dn:
        return {"found": False, "error": "Missing deal number.", "row": None, "method": "deal id"}
    prefixes = caf_df["order_id"].astype(str).fillna("").map(extract_order_id_deal_prefix)
    hit = caf_df[prefixes == dn]
    if hit.empty:
        return {"found": False, "error": "No payment record found by deal ID.", "row": None, "method": "deal id"}
    return {"found": True, "error": None, "row": hit.iloc[0].to_dict(), "method": "deal id"}

def caf_try_match_by_address(sf_addr: str, osc_addr: str):
    if caf_df.empty:
        return {"found": False, "error": "Payment file did not load.", "row": None, "method": "address"}
    if "property_address" not in caf_df.columns:
        return {"found": False, "error": "Payment file is missing property addresses.", "row": None, "method": "address"}
    target = normalize_text(sf_addr) or normalize_text(osc_addr)
    if not target:
        return {"found": False, "error": "No address available to match.", "row": None, "method": "address"}

    target_zip = zip5_from_addr(target)
    target_house = house_num_from_addr(target)
    target_tokens = address_tokens(target)

    caf_addr_raw = caf_df["property_address"].astype(str).fillna("")
    candidates = caf_df.copy()
    candidates["_addr_raw"] = caf_addr_raw

    if target_zip:
        candidates["_zip5"] = candidates["_addr_raw"].map(zip5_from_addr)
        candidates = candidates[candidates["_zip5"] == target_zip]
    if target_house and not candidates.empty:
        candidates["_house"] = candidates["_addr_raw"].map(house_num_from_addr)
        candidates = candidates[candidates["_house"] == target_house]

    if candidates.empty:
        candidates = caf_df.copy()
        candidates["_addr_raw"] = caf_addr_raw

    scores = []
    for idx, row in candidates.iterrows():
        toks = address_tokens(row["_addr_raw"])
        score = jaccard(target_tokens, toks)
        scores.append((idx, score))
    if not scores:
        return {"found": False, "error": "No address candidates found.", "row": None, "method": "address"}

    best_idx, best_score = max(scores, key=lambda t: t[1])
    if best_score < 0.45:
        return {"found": False, "error": "No close address match found.", "row": None, "method": "address"}
    return {"found": True, "error": None, "row": caf_df.loc[best_idx].to_dict(), "method": "address match"}

def pick_payment_statuses(caf_row: dict):
    out = []
    if not caf_row:
        return out
    for col in ["inst_1_payment_status", "inst_2_payment_status", "inst_3_payment_status", "inst_4_payment_status"]:
        if col in caf_row:
            v = normalize_text(caf_row.get(col))
            if v:
                out.append((col, v))
    if not out:
        for k, v in caf_row.items():
            if "payment_status" in (k or "") and v:
                out.append((k, normalize_text(v)))
    return out

def is_payment_status_ok(val: str) -> bool:
    t = (val or "").strip().lower()
    if t == "":
        return False
    bad_words = ["delinquent", "late", "unpaid", "past due", "default", "foreclosure"]
    return not any(w in t for w in bad_words)

# -----------------------------
# PRECHECKS (OLD LOGIC STYLE)
# -----------------------------
TARGET_INSURANCE_OK = "outside policy in-force"

def run_prechecks(opp: dict, prop: dict, loan: dict, user_deal_input: str):
    deal_digits = digits_only(user_deal_input)
    deal_label = normalize_text(opp.get("Deal_Loan_Number__c")) or user_deal_input

    deal_name = normalize_text(opp.get("Name"))
    acct_name = normalize_text(opp.get("Account_Name__c"))

    servicer_key = pick_first(
        (prop or {}).get("Servicer_Id__c"),
        (opp or {}).get("Servicer_Commitment_Id__c"),
        (loan or {}).get("Servicer_Loan_Id__c"),
    )

    # System address (display only)
    sf_full_address = normalize_text((prop or {}).get("Full_Address__c"))
    sf_full_address_disp = sf_full_address.upper() if sf_full_address else ""

    # OSC (required / blocking)
    osc = osc_lookup(servicer_key)
    osc_primary = ""
    osc_ok = False
    osc_addr_disp = ""
    if osc["found"]:
        r = osc["row"] or {}
        osc_primary = normalize_text(r.get("primary_status"))
        osc_ok = (osc_primary.strip().lower() == TARGET_INSURANCE_OK)
        osc_addr_disp = " ".join([
            normalize_text(r.get("property_street")),
            normalize_text(r.get("property_city")),
            normalize_text(r.get("property_state")),
            normalize_text(r.get("property_zip")),
        ]).strip().upper()

    # CAF (optional / best-effort)
    caf = caf_try_match_by_deal_id(deal_digits)
    if not caf.get("found"):
        caf = caf_try_match_by_address(sf_full_address, osc_addr_disp)

    caf_addr_disp = ""
    caf_statuses = []
    caf_ok = False
    caf_found = bool(caf.get("found"))
    if caf_found:
        row = caf.get("row") or {}
        caf_addr_disp = normalize_text(row.get("property_address")).upper()
        caf_statuses = pick_payment_statuses(row)
        if caf_statuses:
            caf_ok = all(is_payment_status_ok(v) for (_k, v) in caf_statuses)

    # ONLY OSC blocks (old behavior)
    osc_blocking_ok = bool(servicer_key) and osc.get("found") and osc_ok
    overall_ok = bool(osc_blocking_ok)

    checks = []
    if not servicer_key:
        checks.append({"Check":"Servicer identifier","Value":"(missing)","Result":"Stop","Note":"Missing identifier needed to find the insurance record."})
    elif not osc.get("found"):
        checks.append({"Check":"Insurance status","Value":osc.get("error","Not found"),"Result":"Stop","Note":"We need an insurance record before creating the HUD."})
    else:
        checks.append({"Check":"Insurance status","Value":osc_primary if osc_primary else "(blank)","Result":"OK" if osc_ok else "Stop","Note":"Must be outside-policy in-force."})

    if caf_found:
        checks.append({"Check":"Payment info (optional)","Value":"Found","Result":"OK" if caf_ok else "Review","Note":"Shown for visibility; it does not block HUD creation."})
    else:
        checks.append({"Check":"Payment info (optional)","Value":caf.get("error","Not found"),"Result":"Review","Note":"Not required to create the HUD."})

    # HUD address
    hud_address_disp = osc_addr_disp or sf_full_address_disp
    checks.append({"Check":"HUD address source","Value":"Insurance record" if osc_addr_disp else "System address","Result":"OK" if hud_address_disp else "Review","Note":"HUD uses insurance address when available."})

    return {
        "deal_number": deal_label,
        "deal_name": deal_name,
        "account_name": acct_name,
        "servicer_key": servicer_key,
        "checks": checks,
        "overall_ok": overall_ok,
        "sf_address": sf_full_address_disp,
        "osc_address": osc_addr_disp,
        "caf_address": caf_addr_disp,
        "caf_method": caf.get("method", ""),
        "hud_address_disp": hud_address_disp,
    }

# -----------------------------
# EXCEL TEMPLATE OUTPUT
# -----------------------------
def _is_red_font(cell) -> bool:
    c = getattr(getattr(cell, "font", None), "color", None)
    rgb = getattr(c, "rgb", None)
    if not rgb:
        return False
    rgb = str(rgb).upper()
    return "FF0000" in rgb

def _clear_red_text(ws):
    for row in ws.iter_rows():
        for cell in row:
            if cell.value not in (None, "") and _is_red_font(cell):
                cell.value = None

def build_hud_excel_bytes_from_template(ctx: dict) -> bytes:
    if not TEMPLATE_PATH.exists():
        raise FileNotFoundError("HUD template not found. Add it to your repo next to app.py.")

    wb = load_workbook(TEMPLATE_PATH)
    ws = wb[TEMPLATE_SHEET] if TEMPLATE_SHEET in wb.sheetnames else wb.active
    _clear_red_text(ws)

    def write_cell(key, value):
        addr = CELL_MAP.get(key)
        if not addr:
            return
        ws[addr] = value

    # TEXT
    write_cell("deal_number", str(ctx.get("deal_number", "")))      # ✅ Loan ID = Deal #
    write_cell("advance_date", str(ctx.get("advance_date", "")))
    write_cell("borrower_disp", str(ctx.get("borrower_disp", "")))
    write_cell("address_disp", str(ctx.get("address_disp", "")))

    # NUMBERS
    write_cell("total_loan_amount", float(ctx.get("total_loan_amount", 0.0)))
    write_cell("initial_advance", float(ctx.get("initial_advance", 0.0)))
    write_cell("total_reno_drawn", float(ctx.get("total_reno_drawn", 0.0)))
    write_cell("advance_amount", float(ctx.get("advance_amount", 0.0)))
    write_cell("interest_reserve", float(ctx.get("interest_reserve", 0.0)))

    write_cell("inspection_fee", float(ctx.get("inspection_fee", 0.0)))
    write_cell("wire_fee", float(ctx.get("wire_fee", 0.0)))
    write_cell("construction_mgmt_fee", float(ctx.get("construction_mgmt_fee", 0.0)))
    write_cell("title_fee", float(ctx.get("title_fee", 0.0)))

    black = Font(color="FF000000")
    for addr in set(CELL_MAP.values()):
        try:
            ws[addr].font = ws[addr].font.copy(color=black.color)
        except Exception:
            pass

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()

# -----------------------------
# SESSION DEFAULTS
# -----------------------------

# -----------------------------
# CONSTRUCTION CHECKLIST HELPERS
# -----------------------------
CHECKLIST_TEMPLATE_CANDIDATES = [
    "Copy of Draw Check List REV 12.30.25 - Jonathan.xlsx",
    "Draw Check List REV 12.30.25.xlsx",
    "Draw Check List.xlsx",
]

CHECKLIST_STATUS_OPTIONS = [
    "Pending",
    "Complete",
    "Review",
    "Missing",
    "Not Applicable",
]

CHECKLIST_AUTO_ROW_HELP = {
    2: "Loan Buyer / Capital Partner",
    3: "Next payment due",
    4: "Late payment check",
    5: "Current maturity date",
    6: "Taxes check",
    7: "Supplier code",
    8: "Property insurance",
    32: "Remaining value check",
}

CHECKLIST_EXPORT_SPECS = [
    {
        "order": 1,
        "field": "sold_loan_status",
        "label": "NLB / sold loan status",
        "row_number": 2,
        "source_hint": "Derived from Opportunity.Intended_Capital_Partner__c and Sold_Loan_Pool__c",
    },
    {
        "order": 2,
        "field": "loan_buyer_or_cap_partner",
        "label": "Loan buyer / capital partner",
        "row_number": 2,
        "source_hint": "Account name from Opportunity.Intended_Capital_Partner__c or Sold_Loan_Pool__c.Sold_To__c",
    },
    {
        "order": 3,
        "field": "next_payment_due",
        "label": "Next payment due",
        "row_number": 3,
        "source_hint": "FCI nextDueDate, then Property__c.Next_Payment_Date__c or Opportunity.Next_Payment_Date__c",
    },
    {
        "order": 4,
        "field": "late_payment_check",
        "label": "Late payment check",
        "row_number": 4,
        "source_hint": "Derived from FCI late-charge fields, then Servicer_Loan__c delinquency fields",
    },
    {
        "order": 5,
        "field": "maturity_date",
        "label": "Current maturity date",
        "row_number": 5,
        "source_hint": "FCI maturityDate, then Property__c.Updated_Asset_Maturity_Date__c or Opportunity.Updated_Loan_Maturity_Date__c",
    },
    {
        "order": 6,
        "field": "tax_status",
        "label": "Taxes status",
        "row_number": 6,
        "source_hint": "REO__c.Taxes_Status__c with Property__c.Tax_Payment_Next_Due_Date__c support",
    },
    {
        "order": 7,
        "field": "supplier_code",
        "label": "Supplier code",
        "row_number": 7,
        "source_hint": "Salesforce Warehouse Line (Property__c.Warehouse_Line_New__c / Property__c.Warehouse_Line__c / Opportunity.Warehouse_Line__c)",
    },
    {
        "order": 8,
        "field": "insurance_status",
        "label": "Property insurance",
        "row_number": 8,
        "source_hint": "Property__c.Insurance_Status__c and Property__c.Insurance_Expiration_Date__c",
    },
    {
        "order": 9,
        "field": "remaining_value_status",
        "label": "Remaining value in Salesforce",
        "row_number": 32,
        "source_hint": "Derived from Property__c.Outstanding_Facility_Amount__c",
    },
]


def checklist_is_red_font(cell) -> bool:
    color = getattr(getattr(cell, "font", None), "color", None)
    rgb = getattr(color, "rgb", None)
    if not rgb:
        return False
    return "FF0000" in str(rgb).upper()


def checklist_is_blue_font(cell) -> bool:
    color = getattr(getattr(cell, "font", None), "color", None)
    rgb = getattr(color, "rgb", None)
    if not rgb:
        return False
    return "0070C0" in str(rgb).upper()


def pick_checklist_template_bytes(uploaded_file) -> Tuple[bytes | None, str | None]:
    if uploaded_file is not None:
        return uploaded_file.getvalue(), uploaded_file.name

    for candidate in CHECKLIST_TEMPLATE_CANDIDATES:
        path = APP_DIR / candidate
        if path.exists():
            return path.read_bytes(), path.name
        alt = Path('/mnt/data') / candidate
        if alt.exists():
            return alt.read_bytes(), alt.name

    return None, None


@st.cache_data(show_spinner=False)
def extract_checklist_template_rows(template_bytes: bytes) -> pd.DataFrame:
    wb = load_workbook(io.BytesIO(template_bytes))
    ws = wb[wb.sheetnames[0]]

    section = "General"
    rows = []

    for row_idx in range(1, ws.max_row + 1):
        a = ws[f"A{row_idx}"]
        b = ws[f"B{row_idx}"]

        label = "" if a.value is None else str(a.value).strip()
        helper = "" if b.value is None else str(b.value).strip()

        if not label and not helper:
            continue

        is_section = bool(label and a.font and a.font.bold)
        if is_section:
            section = label
            continue

        rows.append(
            {
                "row_number": row_idx,
                "section": section,
                "item": label,
                "helper": helper,
                "is_red": checklist_is_red_font(a) or checklist_is_red_font(b),
                "is_blue": checklist_is_blue_font(a) or checklist_is_blue_font(b),
                "status": "Pending",
                "value": "",
                "source": "",
                "notes": "",
            }
        )

    return pd.DataFrame(rows)


def checklist_answer_from_choice(
    choice: str,
    source: str,
    good_label: str = "Complete",
    bad_label: str = "Missing",
) -> Tuple[str, str, str]:
    mapping = {
        "Current / good": (good_label, "Current", source),
        "Current": (good_label, "Current", source),
        "No issues": (good_label, "No issues", source),
        "Enough remaining value": (good_label, "Enough remaining value", source),
        "Expired / missing": (bad_label, "Expired / missing", source),
        "Delinquent": (bad_label, "Delinquent", source),
        "Late payments found": ("Review", "Late payments found", source),
        "Low / insufficient": ("Review", "Low / insufficient", source),
        "Need review": ("Review", "", source),
        "Not applicable": ("Not Applicable", "", source),
    }
    return mapping.get(choice, ("Pending", "", source))


def checklist_blank_export_values() -> Dict[str, str]:
    return {
        "sold_loan_status": "Need review",
        "loan_buyer_or_cap_partner": "",
        "next_payment_due": "",
        "late_payment_check": "Need review",
        "maturity_date": "",
        "tax_status": "Need review",
        "supplier_code": "",
        "insurance_status": "Need review",
        "remaining_value_status": "Need review",
    }


def fetch_account_by_id(account_id: str):
    if not account_id:
        return None

    fields = [
        "Id",
        "Name",
        "Yardi_Vendor_Code__c",
        "Phone",
        "Website",
    ]
    where = f"Id = {soql_quote(account_id)}"
    rows, _used, _soql = try_query_drop_missing(sf, "Account", fields, where, limit=1)
    if not rows:
        return None
    r = rows[0].copy()
    r.pop("attributes", None)
    return r


def fetch_business_entity_by_id(entity_id: str):
    if not entity_id:
        return None

    fields = [
        "Id",
        "Name",
        "Borrower_Email_Address__c",
        "Business_Tax_ID_EIN__c",
        "Operating_Agreement_Date__c",
    ]
    where = f"Id = {soql_quote(entity_id)}"
    rows, _used, _soql = try_query_drop_missing(sf, "Business_Entity__c", fields, where, limit=1)
    if not rows:
        return None
    r = rows[0].copy()
    r.pop("attributes", None)
    return r


def fetch_checklist_opportunity_by_deal_number(deal_number: str):
    dn_digits = digits_only((deal_number or "").strip())
    if not dn_digits:
        return None

    fields = [
        "Id",
        "Name",
        "Deal_Loan_Number__c",
        "AccountId",
        "Borrower_Entity__c",
        "Intended_Capital_Partner__c",
        "Updated_Loan_Maturity_Date__c",
        "Next_Payment_Date__c",
        "Construction_Comments__c",
        "CloseDate",
        "Servicer_Commitment_Id__c",
        "Warehouse_Line__c",
    ]
    where = (
        "("
        f"Deal_Loan_Number__c = {soql_quote(dn_digits)}"
        f" OR Deal_Loan_Number__c LIKE {soql_quote('%' + dn_digits + '%')}"
        ")"
    )
    rows, _used, _soql = try_query_drop_missing(sf, "Opportunity", fields, where, limit=10, order_by="CloseDate DESC")
    if not rows:
        return None
    r = rows[0].copy()
    r.pop("attributes", None)
    return r


def fetch_checklist_properties_for_deal(opp_id: str):
    lk = choose_first_existing("Property__c", ["Deal__c", "Opportunity__c", "Deal_Id__c", "OpportunityId", "DealId"])
    if not lk:
        return []

    fields = [
        "Id",
        "Name",
        lk,
        "Property_Name__c",
        "Full_Address__c",
        "Next_Payment_Date__c",
        "Updated_Asset_Maturity_Date__c",
        "Tax_Payment_Next_Due_Date__c",
        "Insurance_Status__c",
        "Insurance_Expiration_Date__c",
        "Outstanding_Facility_Amount__c",
        "Value__c",
        "Updated_Value__c",
        "Current_Outstanding_Loan_Amount__c",
        "Current_UPB__c",
        "REO__c",
        "Sold_Loan_Pool__c",
        "Servicer_Loan__c",
        "Servicer_Id__c",
        "ConstructionManagementLoanId__c",
        "Warehouse_Line_New__c",
        "Warehouse_Line__c",
    ]
    where = f"{lk} = {soql_quote(opp_id)}"
    rows, _used, _soql = try_query_drop_missing(sf, "Property__c", fields, where, limit=25, order_by="CreatedDate DESC")
    cleaned = []
    for r in rows:
        rr = r.copy()
        rr.pop("attributes", None)
        cleaned.append(rr)
    return cleaned


def fetch_servicer_loans_for_deal(opp_id: str):
    lk = choose_first_existing("Servicer_Loan__c", ["Deal__c", "Opportunity__c", "Deal_Id__c", "OpportunityId", "DealId"])
    if not lk:
        return []

    fields = [
        "Id",
        "Name",
        lk,
        "Servicer_Name__c",
        "Servicer_Loan_Status__c",
        "Delinquent_30_Days__c",
        "Delinquent_60_Days__c",
        "Delinquent_90_Days__c",
        "Delinquent_120_Days__c",
        "First_Payment_Date__c",
        "Last_Payment_Date__c",
        "Servicer_Commitment_ID__c",
    ]
    where = f"{lk} = {soql_quote(opp_id)}"
    rows, _used, _soql = try_query_drop_missing(sf, "Servicer_Loan__c", fields, where, limit=25, order_by="CreatedDate DESC")
    cleaned = []
    for r in rows:
        rr = r.copy()
        rr.pop("attributes", None)
        cleaned.append(rr)
    return cleaned


def fetch_sold_loan_pools_for_deal(opp_id: str):
    lk = choose_first_existing("Sold_Loan_Pool__c", ["Deal__c", "Opportunity__c", "Deal_Id__c", "OpportunityId", "DealId"])
    if not lk:
        return []

    fields = [
        "Id",
        "Name",
        lk,
        "Status__c",
        "Servicing_Status__c",
        "Sold_To__c",
        "Sold_Date__c",
    ]
    where = f"{lk} = {soql_quote(opp_id)}"
    rows, _used, _soql = try_query_drop_missing(sf, "Sold_Loan_Pool__c", fields, where, limit=25, order_by="CreatedDate DESC")
    cleaned = []
    for r in rows:
        rr = r.copy()
        rr.pop("attributes", None)
        cleaned.append(rr)
    return cleaned


def fetch_reo_for_property(property_id: str):
    if not property_id:
        return None

    lk = choose_first_existing("REO__c", ["Property__c", "PropertyId"])
    if not lk:
        return None

    fields = [
        "Id",
        "Name",
        lk,
        "Taxes_Status__c",
        "Annual_Tax_Amount__c",
        "Current_Year_Tax_Amount_Due__c",
        "Previous_Year_Tax_Amount_Due__c",
    ]
    where = f"{lk} = {soql_quote(property_id)}"
    rows, _used, _soql = try_query_drop_missing(sf, "REO__c", fields, where, limit=5, order_by="CreatedDate DESC")
    if not rows:
        return None
    r = rows[0].copy()
    r.pop("attributes", None)
    return r


def fetch_construction_checklist_bundle(deal_number: str, lender_account_override: str = ""):
    opp = fetch_checklist_opportunity_by_deal_number(deal_number)
    if not opp:
        return None

    opp_id = opp.get("Id")
    account = fetch_account_by_id(opp.get("AccountId")) if opp.get("AccountId") else None
    business_entity = fetch_business_entity_by_id(opp.get("Borrower_Entity__c")) if opp.get("Borrower_Entity__c") else None

    cap_partner_account = fetch_account_by_id(opp.get("Intended_Capital_Partner__c")) if opp.get("Intended_Capital_Partner__c") else None
    properties = fetch_checklist_properties_for_deal(opp_id)
    primary_property = properties[0] if properties else None

    servicer_loans = fetch_servicer_loans_for_deal(opp_id)
    sold_loan_pools = fetch_sold_loan_pools_for_deal(opp_id)
    sold_to_account = None
    if sold_loan_pools and sold_loan_pools[0].get("Sold_To__c"):
        sold_to_account = fetch_account_by_id(sold_loan_pools[0].get("Sold_To__c"))

    reo = None
    if primary_property and primary_property.get("Id"):
        reo = fetch_reo_for_property(primary_property.get("Id"))

    bundle = {
        "opportunity": opp,
        "account": account,
        "business_entity": business_entity,
        "cap_partner_account": cap_partner_account,
        "properties": properties,
        "primary_property": primary_property,
        "servicer_loans": servicer_loans,
        "sold_loan_pools": sold_loan_pools,
        "sold_to_account": sold_to_account,
        "reo": reo,
    }
    bundle["fci"] = fetch_fci_bundle(bundle, lender_account_override)
    return bundle


def _first_nonblank_number(*vals):
    for v in vals:
        if v in (None, ""):
            continue
        try:
            return float(str(v).replace(',', '').replace('$', '').strip())
        except Exception:
            continue
    return None


def _parse_float(v):
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(',', '').replace('$', '').strip())
    except Exception:
        return None


def _fci_key(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", normalize_text(value)).upper()


def build_fci_candidate_keys(bundle: dict) -> list[dict]:
    opp = bundle.get("opportunity") or {}
    prop = bundle.get("primary_property") or {}
    servicer_loans = bundle.get("servicer_loans") or []
    candidates: list[dict] = []

    def add(raw_value, label: str):
        raw_value = normalize_text(raw_value)
        clean_value = _fci_key(raw_value)
        if not clean_value:
            return
        if any(item["clean"] == clean_value for item in candidates):
            return
        candidates.append({"raw": raw_value, "clean": clean_value, "label": label})

    add(prop.get("Servicer_Id__c"), "Property__c.Servicer_Id__c")
    add(opp.get("Servicer_Commitment_Id__c"), "Opportunity.Servicer_Commitment_Id__c")
    add(prop.get("ConstructionManagementLoanId__c"), "Property__c.ConstructionManagementLoanId__c")

    for idx, row in enumerate(servicer_loans, start=1):
        add(row.get("Servicer_Commitment_ID__c"), f"Servicer_Loan__c[{idx}].Servicer_Commitment_ID__c")
        add(row.get("Name"), f"Servicer_Loan__c[{idx}].Name")

    return candidates


def fetch_fci_bundle(bundle: dict, lender_account_override: str = "") -> dict:
    cfg = get_fci_config()
    out = {
        "enabled": cfg.get("enabled", False),
        "matched": False,
        "match_source": "",
        "record": None,
        "candidate_keys": build_fci_candidate_keys(bundle),
        "rows_found": 0,
        "error": "",
    }

    if not cfg.get("enabled"):
        out["error"] = "FCI is not configured in Streamlit secrets."
        return out

    fetch_result = fetch_fci_loan_information_rows(cfg["url"], cfg["api_token"])
    rows = fetch_result.get("rows") or []
    out["rows_found"] = len(rows)

    if not fetch_result.get("ok"):
        out["error"] = fetch_result.get("error") or "FCI request failed."
        return out

    rows_by_key: dict[str, list[dict]] = {}
    for row in rows:
        key = _fci_key(row.get("lenderAccount"))
        if not key:
            continue
        rows_by_key.setdefault(key, []).append(row)

    override_key = _fci_key(lender_account_override)
    if override_key:
        matches = rows_by_key.get(override_key) or []
        if len(matches) == 1:
            out["matched"] = True
            out["record"] = matches[0]
            out["match_source"] = "Manual FCI lender account override"
            return out

    for candidate in out["candidate_keys"]:
        matches = rows_by_key.get(candidate["clean"]) or []
        if len(matches) == 1:
            out["matched"] = True
            out["record"] = matches[0]
            out["match_source"] = candidate["label"]
            return out

    if len(rows) == 1:
        out["matched"] = True
        out["record"] = rows[0]
        out["match_source"] = "Single FCI row returned"
        return out

    if override_key:
        out["error"] = "The FCI lender account override did not match a single FCI row."
    else:
        out["error"] = "Could not match an FCI lender account to this deal."
    return out


def _fci_late_payment_result(fci_record: dict) -> str | None:
    if not fci_record:
        return None

    amount = _parse_float(fci_record.get("poffUnpaidLateCharges"))
    days = _parse_float(fci_record.get("lateChargesDays"))
    pct = _parse_float(fci_record.get("lateChargesPct"))

    seen_any = any(v is not None for v in [amount, days, pct])
    if not seen_any:
        return None

    if any((v or 0) > 0 for v in [amount, days, pct]):
        return "Late payments found"
    return "No issues"


def _delinquency_found(servicer_row: dict) -> bool | None:
    if not servicer_row:
        return None

    fields = [
        "Delinquent_30_Days__c",
        "Delinquent_60_Days__c",
        "Delinquent_90_Days__c",
        "Delinquent_120_Days__c",
    ]
    seen_any = False
    for field in fields:
        raw = servicer_row.get(field)
        val = _parse_float(raw)
        if val is None:
            continue
        seen_any = True
        if val > 0:
            return True

    status_text = normalize_text(servicer_row.get("Servicer_Loan_Status__c")).lower()
    if status_text:
        seen_any = True
        if any(token in status_text for token in ["delinq", "late", "default", "past due"]):
            return True

    if seen_any:
        return False
    return None


def derive_checklist_export_values(bundle: dict) -> Dict[str, str]:
    values = checklist_blank_export_values()
    if not bundle:
        return values

    opp = bundle.get("opportunity") or {}
    prop = bundle.get("primary_property") or {}
    account = bundle.get("account") or {}
    cap_partner_account = bundle.get("cap_partner_account") or {}
    servicer_loans = bundle.get("servicer_loans") or []
    sold_loan_pools = bundle.get("sold_loan_pools") or []
    sold_to_account = bundle.get("sold_to_account") or {}
    reo = bundle.get("reo") or {}
    fci = bundle.get("fci") or {}
    fci_record = fci.get("record") or {}

    sold_name = normalize_text(sold_to_account.get("Name"))
    cap_partner_name = normalize_text(cap_partner_account.get("Name"))
    buyer_name = sold_name or cap_partner_name
    has_sold_or_cap_partner = bool(buyer_name or sold_loan_pools or opp.get("Intended_Capital_Partner__c") or prop.get("Sold_Loan_Pool__c"))
    values["sold_loan_status"] = "Cap partner / sold loan" if has_sold_or_cap_partner else "Not applicable"
    values["loan_buyer_or_cap_partner"] = buyer_name

    values["next_payment_due"] = fmt_date_mmddyyyy(
        pick_first(
            fci_record.get("nextDueDate"),
            prop.get("Next_Payment_Date__c"),
            opp.get("Next_Payment_Date__c"),
        )
    )

    fci_late_result = _fci_late_payment_result(fci_record)
    if fci_late_result:
        values["late_payment_check"] = fci_late_result
    else:
        delinquency_result = None
        for servicer_row in servicer_loans:
            delinquency_result = _delinquency_found(servicer_row)
            if delinquency_result is True:
                break
            if delinquency_result is False:
                break
        if delinquency_result is True:
            values["late_payment_check"] = "Late payments found"
        elif delinquency_result is False:
            values["late_payment_check"] = "No issues"

    values["maturity_date"] = fmt_date_mmddyyyy(
        pick_first(
            fci_record.get("maturityDate"),
            prop.get("Updated_Asset_Maturity_Date__c"),
            opp.get("Updated_Loan_Maturity_Date__c"),
        )
    )

    taxes_status = normalize_text(reo.get("Taxes_Status__c")).lower()
    if taxes_status:
        if "delinq" in taxes_status or "late" in taxes_status:
            values["tax_status"] = "Delinquent"
        else:
            values["tax_status"] = "Current / good"
    elif normalize_text(prop.get("Tax_Payment_Next_Due_Date__c")):
        values["tax_status"] = "Current / good"

    values["supplier_code"] = normalize_text(
        pick_first(
            prop.get("Warehouse_Line_New__c"),
            prop.get("Warehouse_Line__c"),
            opp.get("Warehouse_Line__c"),
        )
    )

    insurance_status = normalize_text(prop.get("Insurance_Status__c"))
    insurance_status_lower = insurance_status.lower()
    insurance_exp = parse_date_any(prop.get("Insurance_Expiration_Date__c"))
    today = date.today()
    if insurance_status_lower:
        if any(token in insurance_status_lower for token in ["current", "in-force", "in force", "active"]):
            values["insurance_status"] = "Current"
        elif any(token in insurance_status_lower for token in ["expired", "cancel", "cancelled", "canceled", "lapse", "missing"]):
            values["insurance_status"] = "Expired / missing"
    elif insurance_exp:
        values["insurance_status"] = "Current" if insurance_exp >= today else "Expired / missing"

    remaining_commitment = _first_nonblank_number(prop.get("Outstanding_Facility_Amount__c"))
    current_balance = _first_nonblank_number(
        prop.get("Current_Outstanding_Loan_Amount__c"),
        prop.get("Current_UPB__c"),
    )
    current_value = _first_nonblank_number(
        prop.get("Updated_Value__c"),
        prop.get("Value__c"),
    )
    if remaining_commitment is not None:
        values["remaining_value_status"] = "Enough remaining value" if remaining_commitment > 0 else "Low / insufficient"
    elif current_value is not None and current_balance is not None:
        values["remaining_value_status"] = "Enough remaining value" if current_value > current_balance else "Low / insufficient"

    return values


def build_checklist_auto_answers(form_values: dict) -> Dict[int, dict]:
    answers: Dict[int, dict] = {}

    sold_status = normalize_text(form_values.get("sold_loan_status"))
    loan_buyer = normalize_text(form_values.get("loan_buyer_or_cap_partner"))
    if sold_status == "Not applicable":
        answers[2] = {"status": "Not Applicable", "value": "", "source": "Salesforce"}
    elif sold_status == "Cap partner / sold loan":
        answers[2] = {
            "status": "Complete" if loan_buyer else "Review",
            "value": loan_buyer,
            "source": "Salesforce" if loan_buyer else "Manual review needed",
        }
    else:
        answers[2] = {"status": "Review", "value": loan_buyer, "source": "Manual review needed"}

    answers[3] = {
        "status": "Complete" if normalize_text(form_values.get("next_payment_due")) else "Review",
        "value": normalize_text(form_values.get("next_payment_due")),
        "source": "FCI / Salesforce",
    }

    status, value, source = checklist_answer_from_choice(normalize_text(form_values.get("late_payment_check")), "FCI / Servicer_Loan__c")
    answers[4] = {"status": status, "value": value, "source": source}

    answers[5] = {
        "status": "Complete" if normalize_text(form_values.get("maturity_date")) else "Review",
        "value": normalize_text(form_values.get("maturity_date")),
        "source": "FCI / Salesforce",
    }

    status, value, source = checklist_answer_from_choice(normalize_text(form_values.get("tax_status")), "REO__c / Property__c")
    answers[6] = {"status": status, "value": value, "source": source}

    supplier_code = normalize_text(form_values.get("supplier_code"))
    answers[7] = {
        "status": "Complete" if supplier_code else "Review",
        "value": supplier_code,
        "source": "Salesforce Warehouse Line" if supplier_code else "Manual review needed",
    }

    status, value, source = checklist_answer_from_choice(normalize_text(form_values.get("insurance_status")), "Property__c")
    answers[8] = {"status": status, "value": value, "source": source}

    status, value, source = checklist_answer_from_choice(
        normalize_text(form_values.get("remaining_value_status")),
        "Property__c",
        good_label="Complete",
        bad_label="Review",
    )
    answers[32] = {"status": status, "value": value, "source": source}

    return answers


def apply_checklist_auto_answers(base_df: pd.DataFrame, answers: Dict[int, dict]) -> pd.DataFrame:
    df = base_df.copy()
    for row_number, payload in answers.items():
        mask = df["row_number"] == row_number
        if not mask.any():
            continue
        for col in ["status", "value", "source"]:
            df.loc[mask, col] = payload.get(col, "")
        if row_number in CHECKLIST_AUTO_ROW_HELP:
            df.loc[mask, "notes"] = f"Starter rule: {CHECKLIST_AUTO_ROW_HELP[row_number]}"
    return df


def build_checklist_output_workbook(template_bytes: bytes, edited_rows: pd.DataFrame) -> bytes:
    wb = load_workbook(io.BytesIO(template_bytes))
    ws = wb[wb.sheetnames[0]]

    ws["C1"] = "Status"
    ws["D1"] = "Value / Date"
    ws["E1"] = "Source / Notes"

    header_font = Font(bold=True, color="FF000000")
    for cell_ref in ["C1", "D1", "E1"]:
        ws[cell_ref].font = header_font

    ws.column_dimensions["C"].width = 16
    ws.column_dimensions["D"].width = 24
    ws.column_dimensions["E"].width = 42

    for _, row in edited_rows.iterrows():
        r = int(row["row_number"])
        ws[f"C{r}"] = row["status"]
        ws[f"D{r}"] = row["value"]

        note_parts = []
        if str(row.get("source", "")).strip():
            note_parts.append(str(row["source"]).strip())
        if str(row.get("notes", "")).strip():
            note_parts.append(str(row["notes"]).strip())
        ws[f"E{r}"] = " | ".join(note_parts)

        for cell_ref in [f"C{r}", f"D{r}", f"E{r}"]:
            ws[cell_ref].font = Font(color="FF000000")

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def build_checklist_export_rows(export_values: dict) -> pd.DataFrame:
    rows = []
    for spec in CHECKLIST_EXPORT_SPECS:
        val = normalize_text(export_values.get(spec["field"]))
        rows.append(
            {
                "order": spec["order"],
                "checklist_row": spec["row_number"],
                "export_field": spec["field"],
                "checklist_item": spec["label"],
                "value": val,
                "source_hint": spec["source_hint"],
                "ready": "Yes" if val else "Review",
            }
        )
    return pd.DataFrame(rows)


def build_checklist_export_excel_bytes(export_df: pd.DataFrame, deal_number: str) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Red Fields Export"

    headers = ["Deal Number", "Checklist Row", "Export Field", "Checklist Item", "Value", "Source Hint", "Ready"]
    ws.append(headers)

    for cell in ws[1]:
        cell.font = Font(bold=True, color="FF000000")

    for _, row in export_df.iterrows():
        ws.append(
            [
                deal_number,
                row["checklist_row"],
                row["export_field"],
                row["checklist_item"],
                row["value"],
                row["source_hint"],
                row["ready"],
            ]
        )

    widths = {
        "A": 16,
        "B": 14,
        "C": 28,
        "D": 34,
        "E": 28,
        "F": 56,
        "G": 12,
    }
    for col, width in widths.items():
        ws.column_dimensions[col].width = width

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def render_checklist_export_summary(export_df: pd.DataFrame):
    filled = int((export_df["ready"] == "Yes").sum())
    review = int((export_df["ready"] == "Review").sum())
    c1, c2 = st.columns(2)
    c1.metric("Red export fields ready", filled)
    c2.metric("Need review", review)


def run_construction_checklist_page():
    ensure_default("checklist_deal_number_input", "")
    ensure_default("checklist_fci_lender_account_override", "")
    ensure_default("checklist_bundle", None)
    ensure_default("checklist_export_values", checklist_blank_export_values())

    st.subheader("Construction Checklist")
    st.caption(
        "Pull the top red-font checklist fields from Salesforce and FCI, review them, then export a red-field list or a completed checklist workbook."
    )

    uploaded_template = st.file_uploader(
        "Upload the construction checklist template (optional if the file is already in the repo folder)",
        type=["xlsx"],
        key="construction_template_upload",
    )

    template_bytes, template_name = pick_checklist_template_bytes(uploaded_template)
    if template_bytes is None:
        st.warning("Add the checklist Excel file next to this app or upload it above to enable workbook export.")

    st.markdown('<div class="soft-card">', unsafe_allow_html=True)
    c1, c2, c3 = st.columns([1.8, 1.8, 1.0])
    with c1:
        deal_input = st.text_input(
            "Deal Number",
            key="checklist_deal_number_input",
            placeholder="Type the deal number for the construction checklist",
        )
    with c2:
        lender_account_override = st.text_input(
            "FCI lender account override (optional)",
            key="checklist_fci_lender_account_override",
            placeholder="Use only if the automatic FCI match misses",
        )
    with c3:
        pull_btn = st.button("Pull red checklist fields", type="primary", use_container_width=True)
    st.markdown('</div>', unsafe_allow_html=True)

    if pull_btn:
        st.session_state.checklist_bundle = None
        st.session_state.checklist_export_values = checklist_blank_export_values()
        bundle = fetch_construction_checklist_bundle(deal_input, lender_account_override)
        if not bundle:
            st.error("No deal found for that number. Double-check the deal number and try again.")
        else:
            st.session_state.checklist_bundle = bundle
            st.session_state.checklist_export_values = derive_checklist_export_values(bundle)

    bundle = st.session_state.get("checklist_bundle")
    export_values = st.session_state.get("checklist_export_values") or checklist_blank_export_values()

    if bundle:
        opp = bundle.get("opportunity") or {}
        prop = bundle.get("primary_property") or {}
        account = bundle.get("account") or {}

        st.markdown(
            f"""
<div class="soft-card">
  <div class="big"><b>{normalize_text(opp.get('Deal_Loan_Number__c')) or normalize_text(opp.get('Name'))}</b> — {normalize_text(opp.get('Name')) or 'Deal'}</div>
  <div class="muted">Borrower account: {normalize_text(account.get('Name')) or '—'}</div>
  <div style="margin-top:8px;">
    <span class="pill">Property: <b>{normalize_text(prop.get('Property_Name__c') or prop.get('Name')) or '—'}</b></span>
    <span class="pill">Address: <b>{normalize_text(prop.get('Full_Address__c')) or '—'}</b></span>
    <span class="pill">Properties found: <b>{len(bundle.get('properties') or [])}</b></span>
  </div>
</div>
""",
            unsafe_allow_html=True,
        )

        fci = bundle.get("fci") or {}
        fci_record = fci.get("record") or {}
        with st.expander("FCI match details", expanded=bool(fci_record)):
            if not fci.get("enabled"):
                st.info("FCI is not configured yet. Add an [fci] section to Streamlit secrets to enable next due date, maturity date, and late-charge matching.")
            elif fci_record:
                candidate_labels = ", ".join(item["raw"] for item in (fci.get("candidate_keys") or []) if item.get("raw")) or "—"
                fci_df = pd.DataFrame(
                    [
                        {"Field": "Matched by", "Value": normalize_text(fci.get("match_source")) or "—"},
                        {"Field": "Lender account", "Value": normalize_text(fci_record.get("lenderAccount")) or "—"},
                        {"Field": "Next due date", "Value": fmt_date_mmddyyyy(fci_record.get("nextDueDate")) or "—"},
                        {"Field": "Maturity date", "Value": fmt_date_mmddyyyy(fci_record.get("maturityDate")) or "—"},
                        {"Field": "Late charges days", "Value": normalize_text(fci_record.get("lateChargesDays")) or "0"},
                        {"Field": "Unpaid late charges", "Value": normalize_text(fci_record.get("poffUnpaidLateCharges")) or "0"},
                        {"Field": "Note rate", "Value": normalize_text(fci_record.get("noteRate")) or "—"},
                        {"Field": "Salesforce candidate keys", "Value": candidate_labels},
                    ]
                )
                st.dataframe(fci_df, use_container_width=True, hide_index=True)
            else:
                candidate_labels = ", ".join(item["raw"] for item in (fci.get("candidate_keys") or []) if item.get("raw")) or "—"
                st.warning(normalize_text(fci.get("error")) or "FCI did not return a matched row for this deal.")
                st.caption(f"Salesforce candidate keys checked: {candidate_labels}")

        export_df = build_checklist_export_rows(export_values)
        st.markdown("### Red-field export list")
        render_checklist_export_summary(export_df)
        st.dataframe(
            export_df[["checklist_row", "export_field", "checklist_item", "value", "source_hint", "ready"]],
            use_container_width=True,
            hide_index=True,
        )

        deal_number_for_file = normalize_text(opp.get("Deal_Loan_Number__c")) or normalize_text(st.session_state.get("checklist_deal_number_input")) or "deal"
        export_csv = export_df.drop(columns=["order"]).to_csv(index=False).encode("utf-8")
        export_xlsx = build_checklist_export_excel_bytes(export_df, deal_number_for_file)

        d1, d2 = st.columns(2)
        with d1:
            st.download_button(
                "Download red-field export (CSV)",
                data=export_csv,
                file_name=f"construction_red_field_export_{re.sub(r'[^0-9A-Za-z_-]+', '_', deal_number_for_file)}.csv",
                mime="text/csv",
                use_container_width=True,
            )
        with d2:
            st.download_button(
                "Download red-field export (Excel)",
                data=export_xlsx,
                file_name=f"construction_red_field_export_{re.sub(r'[^0-9A-Za-z_-]+', '_', deal_number_for_file)}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )

        if template_bytes is not None:
            base_df = extract_checklist_template_rows(template_bytes)
            red_count = int(base_df["is_red"].sum())
            c1, c2, c3 = st.columns(3)
            c1.metric("Checklist items", int(len(base_df)))
            c2.metric("Red-font items", red_count)
            c3.metric("Template", template_name)

            working_df = apply_checklist_auto_answers(base_df, build_checklist_auto_answers(export_values))
            show_only_red = st.toggle("Show only red-font checklist rows first", value=True, key="construction_show_only_red")

            editor_df = working_df.copy()
            if show_only_red:
                editor_df = editor_df[editor_df["is_red"]].copy()

            editor_df = editor_df[
                ["row_number", "section", "item", "helper", "status", "value", "source", "notes"]
            ].reset_index(drop=True)

            st.markdown("### Checklist review")
            edited_df = st.data_editor(
                editor_df,
                use_container_width=True,
                hide_index=True,
                num_rows="fixed",
                disabled=["row_number", "section", "item", "helper"],
                key="construction_checklist_editor",
                column_config={
                    "row_number": st.column_config.NumberColumn("Row", disabled=True),
                    "section": st.column_config.TextColumn("Section", disabled=True),
                    "item": st.column_config.TextColumn("Checklist item", width="large", disabled=True),
                    "helper": st.column_config.TextColumn("Template helper text", width="large", disabled=True),
                    "status": st.column_config.SelectboxColumn("Status", options=CHECKLIST_STATUS_OPTIONS, required=True),
                    "value": st.column_config.TextColumn("Value / Date", width="medium"),
                    "source": st.column_config.TextColumn("Source", width="medium"),
                    "notes": st.column_config.TextColumn("Notes", width="large"),
                },
            )

            completed_count = int((edited_df["status"] == "Complete").sum())
            review_count = int((edited_df["status"] == "Review").sum())
            c1, c2 = st.columns(2)
            c1.metric("Completed in current view", completed_count)
            c2.metric("Needs review in current view", review_count)

            if not show_only_red:
                download_source_df = edited_df.copy()
            else:
                download_source_df = working_df.copy()
                for _, row in edited_df.iterrows():
                    mask = download_source_df["row_number"] == int(row["row_number"])
                    for col in ["status", "value", "source", "notes"]:
                        download_source_df.loc[mask, col] = row[col]

            output_bytes = build_checklist_output_workbook(template_bytes, download_source_df)
            st.download_button(
                "Download completed checklist workbook",
                data=output_bytes,
                file_name=f"construction_checklist_completed_{re.sub(r'[^0-9A-Za-z_-]+', '_', deal_number_for_file)}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="primary",
                use_container_width=True,
            )
        else:
            st.info("Upload the checklist template above if you also want the completed workbook export.")
    else:
        st.info("Enter a deal number and pull the red checklist fields to populate the export list.")

def ensure_default(key, val):
    if key not in st.session_state:
        st.session_state[key] = val



def run_hud_generator_page():
    ensure_default("deal_number_input", "")
    ensure_default("precheck_ran", False)
    ensure_default("precheck_payload", None)
    ensure_default("allow_override", False)

    ensure_default("inp_advance_amount", "")
    ensure_default("inp_holdback_pct", "")
    ensure_default("inp_advance_date", date.today())
    ensure_default("inp_inspection_fee", "")
    ensure_default("inp_wire_fee", "")
    ensure_default("inp_construction_mgmt_fee", "")
    ensure_default("inp_title_fee", "")

    # -----------------------------
    # Troubleshooting expander
    # -----------------------------
    with st.expander("Troubleshooting (optional)", expanded=False):
        st.write("Insurance file:", osc_path_used, "✅" if osc_err is None else "❌")
        if osc_err:
            st.code(osc_err)
        st.write("Payment file:", caf_path_used, "✅" if caf_err is None else "❌")
        if caf_err:
            st.code(caf_err)
        st.write("HUD template loaded:", "✅" if TEMPLATE_PATH.exists() else "❌")
        if st.session_state.debug_last_sf_error:
            st.markdown("Salesforce error details:")
            st.code(st.session_state.debug_last_sf_error.get("soql", ""))
            st.code(st.session_state.debug_last_sf_error.get("error", ""))

    # -----------------------------
    # UI — DEAL INPUT + PRECHECKS
    # -----------------------------
    st.markdown('<div class="soft-card">', unsafe_allow_html=True)
    c1, c2 = st.columns([2.4, 1.2])
    with c1:
        deal_input = st.text_input("Deal Number", key="deal_number_input", placeholder="Type the deal number")
    with c2:
        run_btn = st.button("Run checks", type="primary", use_container_width=True)
    st.markdown("</div>", unsafe_allow_html=True)

    if run_btn:
        st.session_state.precheck_ran = False
        st.session_state.precheck_payload = None
        st.session_state.allow_override = False

        with st.spinner("Finding your deal..."):
            opp = fetch_opportunity_by_deal_number(deal_input)

        if not opp:
            st.error("No deal found for that number. Double-check the deal number and try again.")
            st.stop()

        opp_id = opp.get("Id")

        # FIX: Loan__c is non-blocking (permissions won't crash whole app)
        with st.spinner("Pulling related info..."):
            prop = fetch_property_for_deal(opp_id) if opp_id else None
            try:
                loan = fetch_loan_for_deal(opp_id) if opp_id else None
            except Exception:
                loan = None
                st.warning("⚠️ Could not pull Loan details (permissions). Continuing without Loan__c.")
            advances = fetch_advances_for_deal(opp_id) if opp_id else []

        with st.spinner("Running checks..."):
            payload = run_prechecks(opp, prop, loan, deal_input)

        st.session_state.precheck_payload = {"opp": opp, "prop": prop, "loan": loan, "advances": advances, "payload": payload}
        st.session_state.precheck_ran = True

    # -----------------------------
    # SHOW CHECK RESULTS + ADDRESS VIEW
    # -----------------------------
    if st.session_state.precheck_ran and st.session_state.precheck_payload:
        opp = st.session_state.precheck_payload["opp"]
        prop = st.session_state.precheck_payload.get("prop") or {}
        payload = st.session_state.precheck_payload["payload"]

        st.subheader("Check results")
        st.markdown(
            f"""
    <div class="soft-card">
      <div class="big"><b>{payload['deal_number']}</b> — {payload['deal_name']}</div>
      <div class="muted">{payload['account_name']}</div>
      <div style="margin-top:8px;">
        <span class="pill">Servicer Identifier: <b>{payload['servicer_key'] if payload['servicer_key'] else '—'}</b></span>
        <span class="pill">Borrower (SF): <b>{(prop.get('Borrower_Name__c') or '') or '—'}</b></span>
      </div>
    </div>
    """,
            unsafe_allow_html=True,
        )

        df_checks = pd.DataFrame(payload["checks"])[["Check", "Value", "Result", "Note"]]
        st.dataframe(df_checks, use_container_width=True, hide_index=True)

        st.markdown("### Address comparison")
        a1, a2, a3 = st.columns(3)
        with a1:
            st.markdown("**Address from our system**")
            st.code(payload.get("sf_address") or "(blank)")
        with a2:
            st.markdown("**Insurance record address (used on HUD)**")
            st.code(payload.get("osc_address") or "(blank)")
        with a3:
            st.markdown("**Payment record address (optional)**")
            st.code(payload.get("caf_address") or "(blank)")
            st.caption(f"How it matched: {payload.get('caf_method') or '(not matched)'}")

        if payload["overall_ok"]:
            st.success("✅ Required checks passed. You can continue to build the HUD.")
            st.session_state.allow_override = True
        else:
            st.error("🚫 Required checks did not pass — HUD should NOT be created yet.")
            st.session_state.allow_override = st.checkbox("Override and continue anyway", value=False)

    # -----------------------------
    # HUD INPUTS (ONLY AFTER REQUIRED CHECKS)
    # -----------------------------
    if st.session_state.precheck_ran and st.session_state.precheck_payload and st.session_state.allow_override:
        opp = st.session_state.precheck_payload["opp"]
        prop = st.session_state.precheck_payload.get("prop") or {}
        advances = st.session_state.precheck_payload.get("advances") or []
        payload = st.session_state.precheck_payload["payload"]

        borrower_default = (pick_first(prop.get("Borrower_Name__c"), opp.get("Account_Name__c")) or "").strip().upper()
        address_default = payload.get("hud_address_disp") or ""

        st.subheader("HUD inputs")
        st.caption("Type amounts like `1200` or `$1,200` (leave blank for $0). Dates are mm/dd/yyyy.")

        with st.form("hud_form", clear_on_submit=False):
            cA, cB, cC = st.columns([1.2, 1.0, 1.2])

            with cA:
                st.markdown("**Borrower info**")
                borrower_val = st.text_input("Borrower (for the form)", value=borrower_default, key="inp_borrower_disp")
                addr_val = st.text_input("Address (for the form)", value=address_default, key="inp_address_disp")

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

            # -----------------------------
            # FALLBACK LOGIC USING YOUR FIELD LIST
            # -----------------------------
            # Total Loan Amount (Commitment): Advance__c.LOC_Commitment__c -> Property__c.LOC_Commitment__c -> Opp LOC_Commitment__c -> Opp Amount
            adv_loc_val = None
            for a in advances:
                _f, adv_loc_val = pick_first_nonblank_field(a, ["LOC_Commitment__c"])
                if adv_loc_val is not None:
                    break
            total_loan_amount_val = pick_first(
                adv_loc_val,
                prop.get("LOC_Commitment__c"),
                opp.get("LOC_Commitment__c"),
                opp.get("Final_Loan_Amount__c"),
                opp.get("Current_Loan_Amount__c"),
                opp.get("Amount"),
            )
            sf_total_loan_amount = parse_money(total_loan_amount_val)

            # Initial Advance: Property__c.Initial_Disbursement_Used__c -> Property__c.Initial_Disbursement__c -> Advance__c.Initial_Disbursement_Total__c
            adv_init_val = None
            for a in advances:
                _f, adv_init_val = pick_first_nonblank_field(a, ["Initial_Disbursement_Total__c"])
                if adv_init_val is not None:
                    break
            initial_advance_val = pick_first(
                prop.get("Initial_Disbursement_Used__c"),
                prop.get("Initial_Disbursement__c"),
                prop.get("Total_Initial_Disbursement__c"),
                adv_init_val,
            )
            sf_initial_advance = parse_money(initial_advance_val)

            # Total Reno Drawn: Property__c.Renovation_Advance_Amount_Used__c -> Advance__c.Renovation_Reserve_Total__c -> Property__c.Approved_Renovation_Holdback__c -> Opp.Total_Amount_Advances__c
            adv_reno_val = None
            for a in advances:
                _f, adv_reno_val = pick_first_nonblank_field(a, ["Renovation_Reserve_Total__c"])
                if adv_reno_val is not None:
                    break
            total_reno_val = pick_first(
                prop.get("Renovation_Advance_Amount_Used__c"),
                adv_reno_val,
                prop.get("Approved_Renovation_Holdback__c"),
                opp.get("Total_Amount_Advances__c"),
            )
            sf_total_reno = parse_money(total_reno_val)

            # Interest Reserve: Property__c.Interest_Allocation__c -> Opp Interest_Reserves__c -> Opp Current_Interest_Reserves_Remaining__c -> Advance__c Interest_Reserve_Total__c -> Advance__c Total_Interest_Reserves_andStub_Interest__c
            adv_int_val = None
            for a in advances:
                _f, adv_int_val = pick_first_nonblank_field(
                    a,
                    ["Interest_Reserve_Total__c", "Total_Interest_Reserves_andStub_Interest__c", "Interest_Reserve_Subtotal__c"],
                )
                if adv_int_val is not None:
                    break
            interest_reserve_val = pick_first(
                prop.get("Interest_Allocation__c"),
                opp.get("Interest_Reserves__c"),
                opp.get("Current_Interest_Reserves_Remaining__c"),
                opp.get("Current_Interest_Reserves_Paid__c"),
                adv_int_val,
            )
            sf_interest_reserve = parse_money(interest_reserve_val)

            # Borrower + Address (prefer Property__c)
            borrower_final = (st.session_state.get("inp_borrower_disp") or "").strip().upper()
            address_final = (st.session_state.get("inp_address_disp") or "").strip().upper()

            # Deal # (Loan ID cell)
            deal_number_final = normalize_text(opp.get("Deal_Loan_Number__c")) or normalize_text(payload.get("deal_number")) or normalize_text(deal_input)

            # -----------------------------
            # Build ctx
            # -----------------------------
            ctx = {
                "deal_number": deal_number_final,
                "total_loan_amount": float(sf_total_loan_amount),
                "initial_advance": float(sf_initial_advance),
                "total_reno_drawn": float(sf_total_reno),
                "interest_reserve": float(sf_interest_reserve),

                "advance_amount": float(advance_amount),
                "holdback_pct": hb,
                "advance_date": st.session_state["inp_advance_date"].strftime("%m/%d/%Y"),

                "borrower_disp": borrower_final,
                "address_disp": address_final,

                "inspection_fee": float(inspection_fee),
                "wire_fee": float(wire_fee),
                "construction_mgmt_fee": float(construction_mgmt_fee),
                "title_fee": float(title_fee),
            }

            # Preview with source transparency (helps you validate fallbacks quickly)
            st.markdown("### Preview")
            prev = pd.DataFrame(
                [
                    ["Deal # (Loan ID cell)", ctx["deal_number"]],
                    ["Total Loan Amount", fmt_money(ctx["total_loan_amount"])],
                    ["Initial Advance", fmt_money(ctx["initial_advance"])],
                    ["Total Reno Drawn", fmt_money(ctx["total_reno_drawn"])],
                    ["Interest Reserve", fmt_money(ctx["interest_reserve"])],
                    ["Advance Amount", fmt_money(ctx["advance_amount"])],
                    ["Advance Date", ctx["advance_date"]],
                ],
                columns=["Field", "Value"],
            )
            st.dataframe(prev, use_container_width=True, hide_index=True)

            try:
                xbytes = build_hud_excel_bytes_from_template(ctx)
            except Exception as e:
                st.error("Could not build the HUD from the template.")
                st.code(str(e))
                st.stop()

            out_name = f"HUD_{re.sub(r'[^0-9A-Za-z_-]+','_', ctx['deal_number'] or 'Deal')}.xlsx"
            st.download_button(
                "Download HUD Excel",
                data=xbytes,
                file_name=out_name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )



def run_app():
    workflow = st.sidebar.radio(
        "Workflow",
        ["HUD Generator", "Construction Checklist"],
        index=0,
    )

    if workflow == "HUD Generator":
        run_hud_generator_page()
    else:
        run_construction_checklist_page()


run_app()
