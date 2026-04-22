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
import json
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
OSC_CANDIDATES = [
    "OSC_Zstatus_COREVEST_2026-04-14_202850.xlsx",
    "OSC_Zstatus_COREVEST_2026-03-24_064223.xlsx",
]
CAF_CANDIDATES = [
    "Corevest_CAF National 52874_3.9.26.xlsx",
]

def first_existing_path(candidates):
    checked = set()
    search_roots = [Path.cwd(), APP_DIR, Path("/mnt/data")]

    for candidate in candidates:
        raw_path = Path(candidate)
        raw_key = str(raw_path.resolve()) if raw_path.is_absolute() and raw_path.exists() else str(raw_path)
        if raw_key not in checked and raw_path.exists():
            return str(raw_path)
        checked.add(raw_key)

        for root in search_roots:
            probe = root / candidate
            probe_key = str(probe)
            if probe_key in checked:
                continue
            checked.add(probe_key)
            if probe.exists():
                return str(probe)

    return str(APP_DIR / candidates[0])

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

CHECKLIST_NOT_FOUND = "Not found"

CHECKLIST_AUTO_ROW_HELP = {
    2: "Sold loan or cap partner",
    3: "Next payment due date",
    4: "Other late payments",
    5: "Maturity date",
    6: "Taxes not delinquent",
    7: "Supplier code",
    8: "Property insurance current",
}

CHECKLIST_EXPORT_SPECS = [
    {
        "order": 1,
        "field": "sold_loan_or_cap_partner",
        "label": "NLB (No Loan Balance) or Cap Partner aka Sold loan",
        "row_number": 2,
    },
    {
        "order": 2,
        "field": "next_payment_due",
        "label": "Next payment due date",
        "row_number": 3,
    },
    {
        "order": 3,
        "field": "late_payment_check",
        "label": "Check FCI to see if borrowing entity is late for any other payments",
        "row_number": 4,
    },
    {
        "order": 4,
        "field": "maturity_date",
        "label": "Maturity Date",
        "row_number": 5,
    },
    {
        "order": 5,
        "field": "tax_status",
        "label": "Taxes Not Delinquent",
        "row_number": 6,
    },
    {
        "order": 6,
        "field": "supplier_code",
        "label": "Workday Vendor Set Up - Supplier Code",
        "row_number": 7,
    },
    {
        "order": 7,
        "field": "insurance_status",
        "label": "Property Insurance Current",
        "row_number": 8,
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
        "sold_loan_or_cap_partner": CHECKLIST_NOT_FOUND,
        "next_payment_due": CHECKLIST_NOT_FOUND,
        "late_payment_check": CHECKLIST_NOT_FOUND,
        "maturity_date": CHECKLIST_NOT_FOUND,
        "tax_status": CHECKLIST_NOT_FOUND,
        "supplier_code": CHECKLIST_NOT_FOUND,
        "insurance_status": CHECKLIST_NOT_FOUND,
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


def _raw_checklist_value(val) -> str:
    s = normalize_text(val)
    if not s:
        return ""
    if s.lower() in {"need review", "review"}:
        return ""
    return s


def _normalize_checklist_value(val) -> str:
    s = _raw_checklist_value(val)
    return s or CHECKLIST_NOT_FOUND


def _is_checklist_missing(val) -> bool:
    return _normalize_checklist_value(val).lower() == CHECKLIST_NOT_FOUND.lower()


def derive_expected_next_payment_due_from_close_date(close_date_value) -> str:
    close_date = parse_date_any(close_date_value)
    if not close_date:
        return ""

    cutoff = date(2025, 7, 1)
    due_day = 1 if close_date > cutoff else 10

    if close_date.month == 12:
        due_year = close_date.year + 1
        due_month = 1
    else:
        due_year = close_date.year
        due_month = close_date.month + 1

    try:
        return date(due_year, due_month, due_day).strftime("%m/%d/%Y")
    except Exception:
        return ""


def get_checklist_servicer_key(bundle: dict) -> str:
    opp = bundle.get("opportunity") or {}
    prop = bundle.get("primary_property") or {}
    servicer_loans = bundle.get("servicer_loans") or []

    servicer_commitment = ""
    for row in servicer_loans:
        servicer_commitment = normalize_text(row.get("Servicer_Commitment_ID__c"))
        if servicer_commitment:
            break

    return pick_first(
        prop.get("Servicer_Id__c"),
        opp.get("Servicer_Commitment_Id__c"),
        servicer_commitment,
    )


def interpret_tax_status_from_row(row: dict) -> str:
    if not row:
        return CHECKLIST_NOT_FOUND

    cols = list(row.keys())
    priority_cols = [c for c in cols if ("tax" in c.lower() or "delinq" in c.lower())]
    status_cols = [c for c in cols if ("status" in c.lower() and c not in priority_cols)]

    for col in priority_cols + status_cols:
        raw = normalize_text(row.get(col))
        low = raw.lower()
        if not low:
            continue
        if "delinq" in col.lower():
            if low in {"n", "no", "false"}:
                return "Not delinquent"
            if low in {"y", "yes", "true"}:
                return "Delinquent"
        if any(token in low for token in ["not delinquent", "current", "paid", "clear"]):
            return "Not delinquent"
        if any(token in low for token in ["delinq", "delinquent", "late", "past due", "unpaid"]):
            return "Delinquent"

    return CHECKLIST_NOT_FOUND


def _sold_loan_or_cap_partner_value(bundle: dict) -> str:
    opp = bundle.get("opportunity") or {}
    prop = bundle.get("primary_property") or {}
    sold_loan_pools = bundle.get("sold_loan_pools") or []
    sold_to_account = bundle.get("sold_to_account") or {}
    cap_partner_account = bundle.get("cap_partner_account") or {}

    sold_name = normalize_text(sold_to_account.get("Name"))
    cap_partner_name = normalize_text(cap_partner_account.get("Name"))
    if sold_name or cap_partner_name:
        return sold_name or cap_partner_name

    current_balance = _first_nonblank_number(
        prop.get("Current_Outstanding_Loan_Amount__c"),
        prop.get("Current_UPB__c"),
        opp.get("Current_UPB__c"),
        opp.get("Current_Loan_Amount__c"),
    )
    if current_balance is not None and current_balance <= 0:
        return "No loan balance"

    if sold_loan_pools or opp.get("Intended_Capital_Partner__c") or prop.get("Sold_Loan_Pool__c"):
        return "Sold loan / cap partner found"

    return CHECKLIST_NOT_FOUND


def derive_checklist_export_values(bundle: dict) -> Dict[str, str]:
    values = checklist_blank_export_values()
    if not bundle:
        return values

    opp = bundle.get("opportunity") or {}
    prop = bundle.get("primary_property") or {}
    servicer_loans = bundle.get("servicer_loans") or []
    fci = bundle.get("fci") or {}
    fci_record = fci.get("record") or {}

    values["sold_loan_or_cap_partner"] = _sold_loan_or_cap_partner_value(bundle)

    next_due = fmt_date_mmddyyyy(
        pick_first(
            fci_record.get("nextDueDate"),
            prop.get("Next_Payment_Date__c"),
            opp.get("Next_Payment_Date__c"),
        )
    ) or derive_expected_next_payment_due_from_close_date(opp.get("CloseDate"))
    values["next_payment_due"] = _normalize_checklist_value(next_due)

    late_value = CHECKLIST_NOT_FOUND
    fci_late_result = _fci_late_payment_result(fci_record)
    if fci_late_result == "No issues":
        late_value = "No late payments found"
    elif fci_late_result == "Late payments found":
        late_value = "Late payments found"
    else:
        delinquency_result = None
        for servicer_row in servicer_loans:
            delinquency_result = _delinquency_found(servicer_row)
            if delinquency_result is True:
                break
            if delinquency_result is False:
                break
        if delinquency_result is True:
            late_value = "Late payments found"
        elif delinquency_result is False:
            late_value = "No late payments found"
    values["late_payment_check"] = _normalize_checklist_value(late_value)

    maturity_date = fmt_date_mmddyyyy(
        pick_first(
            fci_record.get("maturityDate"),
            prop.get("Updated_Asset_Maturity_Date__c"),
            opp.get("Updated_Loan_Maturity_Date__c"),
        )
    )
    values["maturity_date"] = _normalize_checklist_value(maturity_date)

    servicer_key = get_checklist_servicer_key(bundle)
    osc = osc_lookup(servicer_key)
    osc_addr = ""
    insurance_value = CHECKLIST_NOT_FOUND
    if osc.get("found"):
        row = osc.get("row") or {}
        osc_primary = normalize_text(row.get("primary_status"))
        osc_addr = " ".join([
            normalize_text(row.get("property_street")),
            normalize_text(row.get("property_city")),
            normalize_text(row.get("property_state")),
            normalize_text(row.get("property_zip")),
        ]).strip()
        if osc_primary.strip().lower() == TARGET_INSURANCE_OK:
            insurance_value = "Current"
        elif osc_primary:
            insurance_value = "Not current"
    values["insurance_status"] = _normalize_checklist_value(insurance_value)

    caf = caf_try_match_by_deal_id(normalize_text(opp.get("Deal_Loan_Number__c")))
    if not caf.get("found"):
        caf = caf_try_match_by_address(normalize_text(prop.get("Full_Address__c")), osc_addr)
    tax_value = interpret_tax_status_from_row((caf.get("row") or {})) if caf.get("found") else CHECKLIST_NOT_FOUND
    values["tax_status"] = _normalize_checklist_value(tax_value)

    supplier_code = pick_first(
        prop.get("Warehouse_Line_New__c"),
        prop.get("Warehouse_Line__c"),
        opp.get("Warehouse_Line__c"),
    )
    values["supplier_code"] = _normalize_checklist_value(supplier_code)

    return {key: _normalize_checklist_value(val) for key, val in values.items()}


def build_checklist_auto_answers(form_values: dict) -> Dict[int, dict]:
    answers: Dict[int, dict] = {}

    sold_value = _normalize_checklist_value(form_values.get("sold_loan_or_cap_partner"))
    answers[2] = {
        "status": "Complete" if not _is_checklist_missing(sold_value) else "Review",
        "value": sold_value,
        "source": "",
    }

    next_due = _normalize_checklist_value(form_values.get("next_payment_due"))
    answers[3] = {
        "status": "Complete" if not _is_checklist_missing(next_due) else "Review",
        "value": next_due,
        "source": "",
    }

    late_value = _normalize_checklist_value(form_values.get("late_payment_check"))
    late_lower = late_value.lower()
    if late_lower == "no late payments found":
        late_status = "Complete"
    elif late_lower == "late payments found":
        late_status = "Review"
    else:
        late_status = "Review"
    answers[4] = {"status": late_status, "value": late_value, "source": ""}

    maturity_value = _normalize_checklist_value(form_values.get("maturity_date"))
    answers[5] = {
        "status": "Complete" if not _is_checklist_missing(maturity_value) else "Review",
        "value": maturity_value,
        "source": "",
    }

    tax_value = _normalize_checklist_value(form_values.get("tax_status"))
    tax_lower = tax_value.lower()
    if tax_lower == "not delinquent":
        tax_status = "Complete"
    elif tax_lower == "delinquent":
        tax_status = "Review"
    else:
        tax_status = "Review"
    answers[6] = {"status": tax_status, "value": tax_value, "source": ""}

    supplier_code = _normalize_checklist_value(form_values.get("supplier_code"))
    answers[7] = {
        "status": "Complete" if not _is_checklist_missing(supplier_code) else "Review",
        "value": supplier_code,
        "source": "",
    }

    insurance_value = _normalize_checklist_value(form_values.get("insurance_status"))
    insurance_lower = insurance_value.lower()
    if insurance_lower == "current":
        insurance_status = "Complete"
    elif insurance_lower == "not current":
        insurance_status = "Review"
    else:
        insurance_status = "Review"
    answers[8] = {"status": insurance_status, "value": insurance_value, "source": ""}

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
    ws["D1"] = "Value"

    header_font = Font(bold=True, color="FF000000")
    for cell_ref in ["C1", "D1"]:
        ws[cell_ref].font = header_font

    ws.column_dimensions["C"].width = 16
    ws.column_dimensions["D"].width = 34

    for _, row in edited_rows.iterrows():
        r = int(row["row_number"])
        ws[f"C{r}"] = row["status"]
        ws[f"D{r}"] = row["value"]
        for cell_ref in [f"C{r}", f"D{r}"]:
            ws[cell_ref].font = Font(color="FF000000")

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def build_checklist_export_rows(export_values: dict) -> pd.DataFrame:
    rows = []
    for spec in CHECKLIST_EXPORT_SPECS:
        value = _normalize_checklist_value(export_values.get(spec["field"]))
        rows.append(
            {
                "order": spec["order"],
                "checklist_row": spec["row_number"],
                "field": spec["field"],
                "checklist_item": spec["label"],
                "value": value,
                "found": "Yes" if not _is_checklist_missing(value) else "No",
            }
        )
    return pd.DataFrame(rows)


def build_checklist_export_excel_bytes(export_df: pd.DataFrame, deal_number: str) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Checklist Values"

    headers = ["Deal Number", "Checklist Item", "Value"]
    ws.append(headers)

    for cell in ws[1]:
        cell.font = Font(bold=True, color="FF000000")

    for _, row in export_df.iterrows():
        ws.append([deal_number, row["checklist_item"], row["value"]])

    widths = {"A": 16, "B": 42, "C": 28}
    for col, width in widths.items():
        ws.column_dimensions[col].width = width

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def render_checklist_export_summary(export_df: pd.DataFrame):
    found = int((export_df["found"] == "Yes").sum())
    missing = int((export_df["found"] == "No").sum())
    c1, c2 = st.columns(2)
    c1.metric("Values found", found)
    c2.metric("Still missing", missing)



def _export_df_to_values(export_df: pd.DataFrame) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for _, row in export_df.iterrows():
        out[str(row["field"])] = _normalize_checklist_value(row["value"])
    return out


def run_construction_checklist_page():
    ensure_default("checklist_deal_number_input", "")
    ensure_default("checklist_fci_lender_account_override", "")
    ensure_default("checklist_bundle", None)
    ensure_default("checklist_export_values", checklist_blank_export_values())

    st.subheader("Construction Checklist")
    st.caption("Enter a deal number to pull the checklist values, review them, and export the completed checklist.")

    with st.expander("Admin options", expanded=False):
        uploaded_template = st.file_uploader(
            "Checklist template (optional if already in the repo folder)",
            type=["xlsx"],
            key="construction_template_upload",
        )
        lender_account_override = st.text_input(
            "FCI lender account override (optional)",
            key="checklist_fci_lender_account_override",
            placeholder="Use only if the automatic FCI match misses",
        )

    template_bytes, template_name = pick_checklist_template_bytes(uploaded_template)

    st.markdown('<div class="soft-card">', unsafe_allow_html=True)
    c1, c2 = st.columns([2.2, 1.0])
    with c1:
        deal_input = st.text_input(
            "Deal Number",
            key="checklist_deal_number_input",
            placeholder="Type the deal number",
        )
    with c2:
        pull_btn = st.button("Get checklist values", type="primary", use_container_width=True)
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
  <div class="big"><b>{normalize_text(opp.get('Deal_Loan_Number__c')) or normalize_text(opp.get('Name'))}</b></div>
  <div class="muted">Borrower: {normalize_text(account.get('Name')) or '—'}</div>
  <div style="margin-top:8px;">
    <span class="pill">Property: <b>{normalize_text(prop.get('Property_Name__c') or prop.get('Name')) or '—'}</b></span>
    <span class="pill">Address: <b>{normalize_text(prop.get('Full_Address__c')) or '—'}</b></span>
  </div>
</div>
""",
            unsafe_allow_html=True,
        )

        export_df = build_checklist_export_rows(export_values)
        st.markdown("### Checklist values")
        render_checklist_export_summary(export_df)

        editor_view = export_df[["checklist_item", "value"]].copy()
        edited_view = st.data_editor(
            editor_view,
            use_container_width=True,
            hide_index=True,
            num_rows="fixed",
            key="construction_checklist_values_editor",
            disabled=["checklist_item"],
            column_config={
                "checklist_item": st.column_config.TextColumn("Checklist item", width="large", disabled=True),
                "value": st.column_config.TextColumn("Value", width="medium"),
            },
        )
        export_df["value"] = edited_view["value"].map(_normalize_checklist_value)
        export_df["found"] = export_df["value"].map(lambda v: "No" if _is_checklist_missing(v) else "Yes")

        deal_number_for_file = normalize_text(opp.get("Deal_Loan_Number__c")) or normalize_text(st.session_state.get("checklist_deal_number_input")) or "deal"
        export_csv = export_df[["checklist_item", "value"]].to_csv(index=False).encode("utf-8")
        export_xlsx = build_checklist_export_excel_bytes(export_df, deal_number_for_file)

        d1, d2 = st.columns(2)
        with d1:
            st.download_button(
                "Download checklist values (CSV)",
                data=export_csv,
                file_name=f"construction_checklist_values_{re.sub(r'[^0-9A-Za-z_-]+', '_', deal_number_for_file)}.csv",
                mime="text/csv",
                use_container_width=True,
            )
        with d2:
            st.download_button(
                "Download checklist values (Excel)",
                data=export_xlsx,
                file_name=f"construction_checklist_values_{re.sub(r'[^0-9A-Za-z_-]+', '_', deal_number_for_file)}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )

        if template_bytes is not None:
            base_df = extract_checklist_template_rows(template_bytes)
            edited_values = _export_df_to_values(export_df)
            filled_df = apply_checklist_auto_answers(base_df, build_checklist_auto_answers(edited_values))
            download_rows = filled_df[filled_df["row_number"].isin([2, 3, 4, 5, 6, 7, 8])].copy()
            output_bytes = build_checklist_output_workbook(template_bytes, download_rows)
            st.download_button(
                "Download completed checklist workbook",
                data=output_bytes,
                file_name=f"construction_checklist_completed_{re.sub(r'[^0-9A-Za-z_-]+', '_', deal_number_for_file)}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="primary",
                use_container_width=True,
            )
        else:
            st.info("Add the checklist template to the repo folder or upload it in Admin options to enable workbook export.")

        with st.expander("Admin / troubleshooting", expanded=False):
            st.write("Checklist template:", template_name or "Not found")
            st.write("OSC file:", Path(osc_path_used).name if normalize_text(osc_path_used) else "Not found")
            st.write("CAF file:", Path(caf_path_used).name if normalize_text(caf_path_used) else "Not found")

            fci = bundle.get("fci") or {}
            if not fci.get("enabled"):
                st.write("FCI:", "Not configured")
            elif fci.get("record"):
                record = fci.get("record") or {}
                st.write("FCI match:", normalize_text(fci.get("match_source")) or "Matched")
                st.write("FCI next due date:", fmt_date_mmddyyyy(record.get("nextDueDate")) or CHECKLIST_NOT_FOUND)
                st.write("FCI maturity date:", fmt_date_mmddyyyy(record.get("maturityDate")) or CHECKLIST_NOT_FOUND)
            else:
                st.write("FCI:", normalize_text(fci.get("error")) or "No match found")
    else:
        st.info("Enter a deal number and get the checklist values to populate the list.")


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
