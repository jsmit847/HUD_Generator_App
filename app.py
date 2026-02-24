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
# NOTE: Only writes into your existing Excel TEMPLATE.
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

import pandas as pd
import requests
import streamlit as st
from simple_salesforce import Salesforce
from openpyxl import load_workbook
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
st.caption("Enter a Deal Number → run checks → then generate the Excel HUD.")
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
    "advance_date": "G13",

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
# LOAD EXCEL CHECK FILES
# -----------------------------
OSC_CANDIDATES = [
    "OSC_Zstatus_COREVEST_2026-02-17_180520.xlsx",
    "OSC_Zstatus_COREVEST_2026-02-17_180520 (1).xlsx",
]
CAF_CANDIDATES = [
    "Corevest_CAF National 52874_2.10.xlsx",
    "Corevest_CAF National 52874_2.10 (1).xlsx",
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
# DESCRIBE CACHES
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
# SAFE QUERY
# -----------------------------
def sf_query_all(sf: Salesforce, soql: str):
    return sf.query_all(soql).get("records", [])

def try_query_drop_missing(sf: Salesforce, obj_name: str, fields, where_clause: str, limit=200, order_by=None):
    fields = list(dict.fromkeys([f for f in fields if f]))
    fields = filter_existing_fields(obj_name, fields)

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
                    raise RuntimeError("Salesforce query failed and no fields remain.") from e
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
    lk = choose_first_existing("Loan__c", ["Deal__c", "Opportunity__c", "Deal_Id__c", "OpportunityId", "DealId"])
    if not lk:
        return None

    loan_fields = ["Id", "Name", lk, "Servicer_Loan_Status__c", "Servicer_Loan_Id__c", "Next_Payment_Date__c"]
    where = f"{lk} = {soql_quote(opp_id)}"
    rows, _used, _soql = try_query_drop_missing(sf, "Loan__c", loan_fields, where, limit=5, order_by="CreatedDate DESC")
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
def ensure_default(key, val):
    if key not in st.session_state:
        st.session_state[key] = val

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

    with st.spinner("Pulling related info..."):
        prop = fetch_property_for_deal(opp_id) if opp_id else None
        loan = fetch_loan_for_deal(opp_id) if opp_id else None
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
        adv_loc_field = None
        adv_loc_val = None
        for a in advances:
            adv_loc_field, adv_loc_val = pick_first_nonblank_field(a, ["LOC_Commitment__c"])
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
        adv_init_field = None
        adv_init_val = None
        for a in advances:
            adv_init_field, adv_init_val = pick_first_nonblank_field(a, ["Initial_Disbursement_Total__c"])
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
        adv_reno_field = None
        adv_reno_val = None
        for a in advances:
            adv_reno_field, adv_reno_val = pick_first_nonblank_field(a, ["Renovation_Reserve_Total__c"])
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
        adv_int_field = None
        adv_int_val = None
        for a in advances:
            adv_int_field, adv_int_val = pick_first_nonblank_field(a, ["Interest_Reserve_Total__c", "Total_Interest_Reserves_andStub_Interest__c", "Interest_Reserve_Subtotal__c"])
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
        borrower_final = (borrower_val or "").strip().upper()
        address_final = (addr_val or "").strip().upper()

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
            "advance_date": adv_date.strftime("%m/%d/%Y"),

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
