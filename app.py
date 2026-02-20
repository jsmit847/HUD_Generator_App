# ============================================================
# HUD Generator App — ONE CELL (Streamlit) — FINAL ADDRESS FIX
# Updates:
# ✅ Salesforce Property__c address uses Full_Address__c (primary)
# ✅ OSC address uses columns: Property Street/City/State/Zip (normalized -> property_street etc.)
# ✅ CAF address column is property_address (from "property address")
# ✅ CAF match normalizes ZIP+4 (strips "-####") + normalizes punctuation/case
# ✅ Shows Salesforce address + OSC address + CAF address every run
# ✅ Safe SF query (won’t crash app if Property__c query fails)
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
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment

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
st.caption("Enter a Deal Number → required checks run first → then you can generate the Excel HUD.")

if "debug_last_sf_error" not in st.session_state:
    st.session_state.debug_last_sf_error = None


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


def pick_first(*vals):
    for v in vals:
        if v is None:
            continue
        s = str(v).strip()
        if s != "":
            return s
    return ""


def digits_only(x: str) -> str:
    return re.sub(r"\D", "", x or "")


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


def make_address_line(street, city, state, zipc):
    street = normalize_text(street)
    city = normalize_text(city)
    state = normalize_text(state)
    zipc = normalize_text(zipc)
    return " ".join([street, city, state, zipc]).strip()


def strip_zip4(s: str) -> str:
    """
    Turns ZIP+4 into ZIP5 everywhere in a string:
      '92614-1234' -> '92614'
    """
    if not s:
        return ""
    return re.sub(r"(\b\d{5})-\d{4}\b", r"\1", str(s))


def normalize_address_for_match(s: str) -> str:
    """
    Aggressive normalization for matching:
    - lower
    - strip ZIP+4 to ZIP5
    - remove punctuation
    - collapse whitespace
    """
    if not s:
        return ""
    s = strip_zip4(str(s)).lower()
    s = re.sub(r"[,#.]", " ", s)          # remove common punctuation
    s = re.sub(r"[^0-9a-z\s-]", " ", s)   # keep dash (we strip ZIP+4 anyway), digits, letters
    s = s.replace("-", " ")               # normalize dashes to spaces
    s = re.sub(r"\s+", " ", s).strip()
    return s


def address_contains_match(a: str, b: str) -> bool:
    """
    True if normalized(a) contains normalized(b) or vice versa.
    """
    na = normalize_address_for_match(a)
    nb = normalize_address_for_match(b)
    if not na or not nb:
        return False
    return (nb in na) or (na in nb)


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
    st.success("✅ Authenticated")
    st.caption(f"Instance: {instance_url}")
with topc2:
    if st.button("Log out"):
        st.session_state.sf_token = None
        st.rerun()


# -----------------------------
# LOAD EXCEL CHECK FILES (repo or /mnt/data)
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
            st.session_state.debug_last_sf_error = {"obj": obj_name, "soql": soql, "error": msg}

            # If ORDER BY is causing pain, drop and retry
            if order_by and ("ORDER BY" in msg or "NULLS" in msg.upper() or "unexpected token" in msg.lower()):
                order_by = None
                continue

            # Drop invalid field
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
                    raise RuntimeError(
                        f"Salesforce query failed and no fields remain.\nSOQL:\n{soql}\n\nRaw error:\n{msg}"
                    ) from e
                continue

            raise RuntimeError(f"Salesforce query failed.\nSOQL:\n{soql}\n\nRaw error:\n{msg}") from e


# -----------------------------
# SF FETCHES
# -----------------------------
def fetch_opportunity_by_deal_number(deal_number: str):
    dn_digits = digits_only((deal_number or "").strip())
    if not dn_digits:
        return None

    opp_fields = [
        "Id", "Name", "Deal_Loan_Number__c", "Account_Name__c",
        "StageName", "CloseDate",
        "LOC_Commitment__c", "Amount",
        "Servicer_Commitment_Id__c",
        "Servicer_Status__c",
        "Next_Payment_Date__c",
        "Late_Fees_Servicer__c",
        "Initial_Disbursement_Funded__c",
        "Renovation_HB_Funded__c",
        "Interest_Allocation_Funded__c",
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
    """
    SAFE: will not crash even if Property__c query fails.
    """
    lk = choose_first_existing("Property__c", ["Deal__c", "Opportunity__c", "Deal_Id__c", "OpportunityId", "DealId"])
    if not lk:
        return None

    # Primary address field you confirmed:
    prop_fields = [
        "Id", "Name", lk,
        "Servicer_Id__c",
        "Late_Fees_Servicer__c",
        "Full_Address__c",  # ✅ your org’s field
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
        st.warning("⚠️ Could not pull Property info (Property__c query failed). Continuing without it.")
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


# -----------------------------
# ADDRESS EXTRACTION (Salesforce)
# -----------------------------
def extract_salesforce_full_address(prop: dict) -> str:
    if not prop:
        return ""
    # ✅ confirmed field
    return normalize_text(prop.get("Full_Address__c"))


# -----------------------------
# OSC + CAF LOOKUPS
# -----------------------------
def detect_osc_account_col(df: pd.DataFrame):
    """
    OSC match column can vary; keep your original 'account_number' first,
    but allow common alternates so you don't get silent blanks.
    """
    candidates = [
        "account_number",
        "accountnumber",
        "servicer_id",
        "servicerid",
        "servicer_loan_id",
        "servicerloanid",
        "loan_id",
        "loanid",
        "commitment_id",
        "commitmentid",
    ]
    for c in candidates:
        if c in df.columns:
            return c
    return None


def osc_lookup(servicer_key: str):
    if osc_df.empty:
        return {"found": False, "error": "OSC file not loaded", "row": None}

    acct_col = detect_osc_account_col(osc_df)
    if not acct_col:
        return {"found": False, "error": "OSC missing identifier column (account_number/servicer_id/etc.)", "row": None}

    # ✅ confirmed address columns (after norm they become property_street, property_city, property_state, property_zip)
    addr_cols = ["property_street", "property_city", "property_state", "property_zip"]
    missing = [c for c in addr_cols if c not in osc_df.columns]
    if missing:
        return {"found": False, "error": f"OSC missing address columns: {', '.join(missing)}", "row": None}

    key = (servicer_key or "").strip()
    if key == "":
        return {"found": False, "error": "Missing servicer identifier", "row": None}

    hit = osc_df[osc_df[acct_col].astype(str).str.strip() == key]
    if hit.empty:
        return {"found": False, "error": f"No OSC record found (matched on {acct_col})", "row": None}

    return {"found": True, "error": None, "row": hit.iloc[0].to_dict(), "acct_col": acct_col}


def detect_caf_address_col(df: pd.DataFrame):
    # ✅ you confirmed column name "property address" -> norm -> property_address
    for c in ["property_address"]:
        if c in df.columns:
            return c
    # fallback just in case
    for c in df.columns:
        if "address" in c:
            return c
    return None


def caf_lookup_by_full_address(sf_full_address: str):
    """
    Match CAF by normalized address, stripping ZIP+4 differences.
    Strategy:
      1) Try normalized contains match both directions.
      2) If no hit, try match using ZIP5 + house number + first street token.
    """
    if caf_df.empty:
        return {"found": False, "error": "CAF file not loaded", "row": None, "used_addr_col": None}

    addr_col = detect_caf_address_col(caf_df)
    if not addr_col:
        return {"found": False, "error": "CAF address column not found", "row": None, "used_addr_col": None}

    sf_addr = normalize_text(sf_full_address)
    if not sf_addr:
        return {"found": False, "error": "Salesforce Full_Address__c is blank", "row": None, "used_addr_col": addr_col}

    # Precompute normalized CAF addresses once
    caf_addr_series_raw = caf_df[addr_col].astype(str).fillna("")
    caf_addr_series_norm = caf_addr_series_raw.map(normalize_address_for_match)

    sf_norm = normalize_address_for_match(sf_addr)

    # Attempt 1: contains either way using normalized strings
    # (we do "sf_norm in caf_norm")
    mask1 = caf_addr_series_norm.str.contains(re.escape(sf_norm), na=False)
    if mask1.any():
        row = caf_df[mask1].iloc[0].to_dict()
        return {"found": True, "error": None, "row": row, "used_addr_col": addr_col}

    # Attempt 2: CAF contains SF fragment (street only) - remove city/state etc by taking first segment
    street_fragment = sf_addr.split(",")[0].strip()
    street_norm = normalize_address_for_match(street_fragment)
    if street_norm:
        mask2 = caf_addr_series_norm.str.contains(re.escape(street_norm), na=False)
        if mask2.any():
            row = caf_df[mask2].iloc[0].to_dict()
            return {"found": True, "error": None, "row": row, "used_addr_col": addr_col}

    # Attempt 3: ZIP5 + house number heuristic
    zip5 = ""
    mzip = re.search(r"\b(\d{5})\b", strip_zip4(sf_addr))
    if mzip:
        zip5 = mzip.group(1)

    house = ""
    mhouse = re.match(r"\s*(\d+)\b", sf_addr.strip())
    if mhouse:
        house = mhouse.group(1)

    if zip5 or house:
        crit = caf_addr_series_norm
        if zip5:
            crit = crit[crit.str.contains(rf"\b{re.escape(zip5)}\b", na=False)]
        if house and not crit.empty:
            crit = crit[crit.str.contains(rf"\b{re.escape(house)}\b", na=False)]
        if not crit.empty:
            # pick first hit
            idx = crit.index[0]
            row = caf_df.loc[idx].to_dict()
            return {"found": True, "error": None, "row": row, "used_addr_col": addr_col}

    return {"found": False, "error": "No CAF match found (even after ZIP+4 normalization)", "row": None, "used_addr_col": addr_col}


def pick_payment_statuses(caf_row: dict):
    out = []
    if not caf_row:
        return out
    for col in ["inst_1_payment_status", "inst_2_payment_status", "inst_3_payment_status", "inst_4_payment_status"]:
        if col in caf_row:
            v = normalize_text(caf_row.get(col))
            if v != "":
                out.append((col, v))
    if not out:
        for col in caf_row.keys():
            if "payment_status" in col and ("inst" in col or "install" in col):
                v = normalize_text(caf_row.get(col))
                if v != "":
                    out.append((col, v))
    return out


def is_payment_status_ok(val: str) -> bool:
    t = (val or "").strip().lower()
    if t == "":
        return False
    bad_words = ["delinquent", "late", "unpaid", "past due", "default", "foreclosure"]
    return not any(w in t for w in bad_words)


# -----------------------------
# PRECHECKS (with address debug)
# -----------------------------
TARGET_OSC_PRIMARY = "outside policy in-force"

def run_prechecks(opp: dict, prop: dict, loan: dict):
    deal_num = normalize_text(opp.get("Deal_Loan_Number__c"))
    deal_name = normalize_text(opp.get("Name"))
    acct_name = normalize_text(opp.get("Account_Name__c"))

    servicer_key = pick_first(
        prop.get("Servicer_Id__c") if prop else "",
        opp.get("Servicer_Commitment_Id__c"),
        loan.get("Servicer_Loan_Id__c") if loan else "",
    )

    total_loan_amount = parse_money(pick_first(opp.get("LOC_Commitment__c"), opp.get("Amount"), 0))

    # ✅ Salesforce full address
    sf_full_address = extract_salesforce_full_address(prop)
    sf_full_address_disp = strip_zip4(sf_full_address).upper() if sf_full_address else ""

    # ✅ OSC lookup and OSC address build from confirmed columns
    osc = osc_lookup(servicer_key)
    osc_primary = ""
    osc_ok = False
    osc_address_disp = ""
    if osc.get("found"):
        r = osc.get("row") or {}
        osc_primary = normalize_text(r.get("primary_status"))
        osc_ok = (osc_primary.strip().lower() == TARGET_OSC_PRIMARY)
        osc_address_disp = make_address_line(
            r.get("property_street"),
            r.get("property_city"),
            r.get("property_state"),
            strip_zip4(r.get("property_zip")),
        ).upper()

    # ✅ CAF lookup based on Salesforce Full_Address__c (ZIP+4 normalized)
    caf = caf_lookup_by_full_address(sf_full_address)
    caf_statuses = []
    caf_ok = False
    caf_address_disp = ""
    caf_used_addr_col = caf.get("used_addr_col")

    if caf.get("found"):
        row = caf.get("row") or {}
        caf_addr_raw = normalize_text(row.get(caf_used_addr_col)) if caf_used_addr_col else ""
        caf_address_disp = strip_zip4(caf_addr_raw).upper() if caf_addr_raw else ""
        caf_statuses = pick_payment_statuses(row)
        if caf_statuses:
            caf_ok = all(is_payment_status_ok(v) for (_k, v) in caf_statuses)

    # HUD prefills Salesforce first (now correct), else OSC
    hud_address_disp = pick_first(sf_full_address_disp, osc_address_disp, "")

    checks = []
    checks.append({
        "Check": "Salesforce Full_Address__c",
        "Value": sf_full_address_disp if sf_full_address_disp else "(blank)",
        "Result": "✅ OK" if sf_full_address_disp else "🚫 Stop",
        "Note": "Must exist to match CAF and prefill HUD address."
    })
    checks.append({
        "Check": "OSC address (Property Street/City/State/Zip)",
        "Value": osc_address_disp if osc_address_disp else (osc.get("error") or "(blank)"),
        "Result": "✅ OK" if osc_address_disp else "⚠️ Review",
        "Note": "OSC match uses servicer identifier; address comes from Property Street/City/State/Zip."
    })
    checks.append({
        "Check": "CAF address (property address column)",
        "Value": caf_address_disp if caf_address_disp else (caf.get("error") or "(blank)"),
        "Result": "✅ OK" if caf_address_disp else "🚫 Stop",
        "Note": f"Matched using {caf_used_addr_col or '(none)'} with ZIP+4 normalization."
    })

    # OSC required insurance check (blocking)
    if not osc.get("found"):
        checks.append({
            "Check": "OSC insurance status (required)",
            "Value": osc.get("error", "No OSC match"),
            "Result": "🚫 Stop",
            "Note": "No OSC record found — fix identifier/matching column."
        })
    else:
        checks.append({
            "Check": "OSC insurance status (required)",
            "Value": osc_primary if osc_primary else "(blank)",
            "Result": "✅ OK" if osc_ok else "🚫 Stop",
            "Note": "Must be Outside Policy In-Force."
        })

    # CAF required payment status (blocking)
    if not caf.get("found"):
        checks.append({
            "Check": "CAF installment payment status (required)",
            "Value": caf.get("error", "No CAF match"),
            "Result": "🚫 Stop",
            "Note": "No CAF row matched for Salesforce address (ZIP+4 normalized)."
        })
    else:
        if caf_statuses:
            summary = " | ".join([f"{k}: {v}" for (k, v) in caf_statuses])
            checks.append({
                "Check": "CAF installment payment status (required)",
                "Value": summary,
                "Result": "✅ OK" if caf_ok else "⚠️ Review",
                "Note": "Review any delinquent/late/past due statuses."
            })
        else:
            checks.append({
                "Check": "CAF installment payment status (required)",
                "Value": "Statuses not found in CAF row",
                "Result": "⚠️ Review",
                "Note": "CAF row matched but status columns were empty/missing."
            })

    # Eligibility (same strict logic you had)
    overall_ok = bool(servicer_key) and osc_ok and caf.get("found") and bool(caf_statuses) and caf_ok

    return {
        "deal_number": deal_num,
        "deal_name": deal_name,
        "account_name": acct_name,
        "servicer_key": servicer_key,
        "total_loan_amount": total_loan_amount,
        "checks": checks,
        "overall_ok": overall_ok,
        # Address debug values
        "sf_full_address": sf_full_address_disp,
        "osc_address": osc_address_disp,
        "caf_address": caf_address_disp,
        "caf_used_addr_col": caf_used_addr_col,
        "hud_address_disp": hud_address_disp,
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
            ws[f"B{r}"].number_format = "$#,##0.00"
        else:
            ws[f"B{r}"] = v1 if v1 is not None else ""

        if isinstance(v2, (int, float)):
            ws[f"D{r}"] = float(v2)
            ws[f"D{r}"].number_format = "$#,##0.00"
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
        ws[f"B{r}"].number_format = "$#,##0.00"
        if desc in ("Construction Advance Amount", "Total Fees", "Reimbursement to Borrower"):
            ws[f"A{r}"].font = Font(bold=True)
            ws[f"B{r}"].font = Font(bold=True)

    for row in ws.iter_rows(min_row=1, max_row=ws.max_row, min_col=1, max_col=4):
        for cell in row:
            cell.alignment = Alignment(vertical="center")

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
# TROUBLESHOOT EXPANDER
# -----------------------------
with st.expander("Data + Salesforce troubleshooting", expanded=False):
    st.write("OSC file:", osc_path_used, "✅" if osc_err is None else "❌")
    if osc_err:
        st.code(osc_err)
    st.write("CAF file:", caf_path_used, "✅" if caf_err is None else "❌")
    if caf_err:
        st.code(caf_err)

    if st.session_state.debug_last_sf_error:
        st.markdown("**Last Salesforce error (SOQL + message):**")
        st.code(st.session_state.debug_last_sf_error.get("soql", ""))
        st.code(st.session_state.debug_last_sf_error.get("error", ""))


# -----------------------------
# UI — DEAL INPUT + PRECHECKS
# -----------------------------
st.markdown('<div class="soft-card">', unsafe_allow_html=True)
c1, c2 = st.columns([2.4, 1.2])
with c1:
    deal_number = st.text_input("Deal Number", key="deal_number_input", placeholder="Type the deal number (Deal Loan Number)")
with c2:
    run_btn = st.button("Run required checks", type="primary", use_container_width=True)
st.markdown("</div>", unsafe_allow_html=True)

if run_btn:
    st.session_state.precheck_ran = False
    st.session_state.precheck_payload = None
    st.session_state.allow_override = False

    with st.spinner("Finding deal in Salesforce..."):
        opp = fetch_opportunity_by_deal_number(deal_number)

    if not opp:
        st.error("No deal found for that Deal Number. Make sure you entered the Deal Loan Number.")
        st.stop()

    opp_id = opp.get("Id")
    with st.spinner("Pulling related info from Salesforce..."):
        prop = fetch_property_for_deal(opp_id) if opp_id else None
        loan = fetch_loan_for_deal(opp_id) if opp_id else None

    with st.spinner("Running OSC + CAF checks..."):
        payload = run_prechecks(opp, prop, loan)

    st.session_state.precheck_payload = {"opp": opp, "prop": prop, "loan": loan, "payload": payload}
    st.session_state.precheck_ran = True


# -----------------------------
# SHOW CHECK RESULTS + ADDRESS DEBUG
# -----------------------------
if st.session_state.precheck_ran and st.session_state.precheck_payload:
    opp = st.session_state.precheck_payload["opp"]
    payload = st.session_state.precheck_payload["payload"]

    st.subheader("Required check results")
    st.markdown(
        f"""
<div class="soft-card">
  <div class="big"><b>{payload['deal_number']}</b> — {payload['deal_name']}</div>
  <div class="muted">{payload['account_name']}</div>
  <div style="margin-top:8px;">
    <span class="pill">Total Loan Amount: <b>{fmt_money(payload['total_loan_amount'])}</b></span>
    <span class="pill">Servicer Identifier: <b>{payload['servicer_key'] if payload['servicer_key'] else '—'}</b></span>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )

    df_checks = pd.DataFrame(payload["checks"])[["Check", "Value", "Result", "Note"]]
    st.dataframe(df_checks, use_container_width=True, hide_index=True)

    st.markdown("### Address debug (Salesforce vs OSC vs CAF)")
    d1, d2, d3 = st.columns(3)
    with d1:
        st.markdown("**Salesforce (Property__c.Full_Address__c)**")
        st.code(payload.get("sf_full_address") or "(blank)")
    with d2:
        st.markdown("**OSC address (Property Street/City/State/Zip)**")
        st.code(payload.get("osc_address") or "(blank)")
    with d3:
        st.markdown("**CAF address (property address column)**")
        st.code(payload.get("caf_address") or "(blank)")
        st.caption(f"CAF column used: {payload.get('caf_used_addr_col') or '(none)'}")

    if payload["overall_ok"]:
        st.success("✅ All required checks passed. You can continue to build the HUD.")
        st.session_state.allow_override = True
    else:
        st.error("🚫 Required checks did not pass — HUD should NOT be created yet.")
        st.session_state.allow_override = st.checkbox("Override and continue anyway", value=False)


# -----------------------------
# HUD INPUTS (ONLY AFTER CHECKS)
# -----------------------------
if st.session_state.precheck_ran and st.session_state.precheck_payload and st.session_state.allow_override:
    opp = st.session_state.precheck_payload["opp"]
    payload = st.session_state.precheck_payload["payload"]

    borrower_disp = (opp.get("Account_Name__c") or "").strip().upper()
    address_disp = payload.get("hud_address_disp") or payload.get("sf_full_address") or payload.get("osc_address") or ""

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
        prev = pd.DataFrame(
            [
                ["Total Loan Amount", fmt_money(ctx["total_loan_amount"])],
                ["Initial Advance", fmt_money(ctx["initial_advance"])],
                ["Total Reno Drawn", fmt_money(ctx["total_reno_drawn"])],
                ["Interest Reserve", fmt_money(ctx["interest_reserve"])],
                ["Advance Amount", fmt_money(ctx["advance_amount"])],
                ["Allocated Loan Amount", fmt_money(ctx["allocated_loan_amount"])],
                ["Total Fees", fmt_money(ctx["total_fees"])],
                ["Net Amount to Borrower", fmt_money(ctx["net_amount_to_borrower"])],
                ["Available Balance", fmt_money(ctx["available_balance"])],
            ],
            columns=["Field", "Value"],
        )
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
