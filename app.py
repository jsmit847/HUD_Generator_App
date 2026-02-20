# ============================================================
# HUD Generator App — ONE CELL (Streamlit) — CAF ORDER ID MATCH + OSC ACCOUNT NUMBER
#
# Changes requested:
# ✅ CAF matching: use Completed sheet column "Order Id" (deal number prefix before "-")
# ✅ Full_Address__c: ONLY used for HUD auto-fill (NOT for matching)
# ✅ Property__c.Name: used as fallback matching variable (if CAF Order Id match fails)
# ✅ OSC: match using column "Account Number" (servicer ID)
# ✅ Dates always mm/dd/yyyy
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

def digits_only(x: str) -> str:
    return re.sub(r"\D", "", x or "")

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

def normalize_for_contains(s: str) -> str:
    if not s:
        return ""
    s = str(s).lower()
    s = re.sub(r"[^0-9a-z\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def extract_order_id_deal_prefix(order_id_val: str) -> str:
    """
    Order Id looks like: <DEALNUMBER>-<something>
    Return the digits-only prefix.
    """
    if not order_id_val:
        return ""
    left = str(order_id_val).split("-", 1)[0].strip()
    return digits_only(left)

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
# DESCRIBE CACHES (SF)
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
                    raise RuntimeError(f"Salesforce query failed and no fields remain.\nSOQL:\n{soql}\n\nRaw error:\n{msg}") from e
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
    lk = choose_first_existing("Property__c", ["Deal__c", "Opportunity__c", "Deal_Id__c", "OpportunityId", "DealId"])
    if not lk:
        return None

    # ✅ Full_Address__c used ONLY for auto-fill
    prop_fields = ["Id", "Name", lk, "Servicer_Id__c", "Late_Fees_Servicer__c", "Full_Address__c"]
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
# OFFLINE LOOKUPS (OSC + CAF)
# -----------------------------
def osc_lookup(servicer_key: str):
    """
    OSC match uses column 'Account Number' -> norm -> account_number
    """
    if osc_df.empty:
        return {"found": False, "error": "OSC file not loaded", "row": None}

    if "account_number" not in osc_df.columns:
        return {"found": False, "error": "OSC missing 'Account Number' column", "row": None}

    # Address parts user confirmed -> norm -> property_street/city/state/zip
    needed = ["property_street", "property_city", "property_state", "property_zip"]
    missing = [c for c in needed if c not in osc_df.columns]
    if missing:
        return {"found": False, "error": f"OSC missing address columns: {', '.join(missing)}", "row": None}

    key = (servicer_key or "").strip()
    if not key:
        return {"found": False, "error": "Missing servicer identifier", "row": None}

    hit = osc_df[osc_df["account_number"].astype(str).str.strip() == key]
    if hit.empty:
        return {"found": False, "error": "No OSC record found for that Account Number", "row": None}

    return {"found": True, "error": None, "row": hit.iloc[0].to_dict()}

def caf_lookup_by_order_id_prefix(deal_number_digits: str):
    """
    CAF match uses 'Order Id' column -> norm -> order_id
    Parse left side before '-' and compare to deal number digits.
    """
    if caf_df.empty:
        return {"found": False, "error": "CAF file not loaded", "row": None, "used": "order_id"}

    if "order_id" not in caf_df.columns:
        return {"found": False, "error": "CAF missing 'Order Id' column", "row": None, "used": "order_id"}

    dn = digits_only(deal_number_digits)
    if not dn:
        return {"found": False, "error": "Missing deal number digits for CAF match", "row": None, "used": "order_id"}

    prefixes = caf_df["order_id"].astype(str).fillna("").map(extract_order_id_deal_prefix)
    hit = caf_df[prefixes == dn]
    if hit.empty:
        return {"found": False, "error": f"No CAF row found where Order Id starts with {dn}-", "row": None, "used": "order_id"}

    return {"found": True, "error": None, "row": hit.iloc[0].to_dict(), "used": "order_id"}

def caf_lookup_by_property_name(prop_name: str):
    """
    Fallback CAF match using Property__c.Name against CAF 'Property Address' (property_address).
    (Not perfect, but better than guessing with full address when it differs.)
    """
    if caf_df.empty:
        return {"found": False, "error": "CAF file not loaded", "row": None, "used": "property_name"}

    if "property_address" not in caf_df.columns:
        return {"found": False, "error": "CAF missing 'Property Address' column", "row": None, "used": "property_address"}

    pn = normalize_for_contains(prop_name)
    if not pn:
        return {"found": False, "error": "Missing Property__c.Name for fallback CAF match", "row": None, "used": "property_name"}

    ser = caf_df["property_address"].astype(str).fillna("").map(normalize_for_contains)
    # contains on full property name
    hit = caf_df[ser.str.contains(re.escape(pn), na=False)]
    if hit.empty:
        # weaker: try the first 2 tokens
        toks = pn.split()
        if len(toks) >= 2:
            weak = " ".join(toks[:2])
            hit = caf_df[ser.str.contains(re.escape(weak), na=False)]

    if hit.empty:
        return {"found": False, "error": "No CAF row matched Property__c.Name in Property Address", "row": None, "used": "property_name"}

    return {"found": True, "error": None, "row": hit.iloc[0].to_dict(), "used": "property_name"}

def pick_payment_statuses(caf_row: dict):
    out = []
    if not caf_row:
        return out
    # keep flexible
    preferred = ["inst_1_payment_status", "inst_2_payment_status", "inst_3_payment_status", "inst_4_payment_status"]
    for col in preferred:
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
# PRECHECKS (required) + DEBUG ADDRESSES
# -----------------------------
TARGET_OSC_PRIMARY = "outside policy in-force"

def run_prechecks(opp: dict, prop: dict, loan: dict):
    deal_num = normalize_text(opp.get("Deal_Loan_Number__c"))
    deal_name = normalize_text(opp.get("Name"))
    acct_name = normalize_text(opp.get("Account_Name__c"))

    # servicer identifier used for OSC "Account Number"
    servicer_key = pick_first(
        prop.get("Servicer_Id__c") if prop else "",
        opp.get("Servicer_Commitment_Id__c"),
        loan.get("Servicer_Loan_Id__c") if loan else "",
    )

    total_loan_amount = parse_money(pick_first(opp.get("LOC_Commitment__c"), opp.get("Amount"), 0))

    # ✅ Salesforce auto-fill address only
    sf_full_address = normalize_text(prop.get("Full_Address__c")) if prop else ""
    sf_full_address_disp = sf_full_address.upper() if sf_full_address else ""

    # ✅ Property__c.Name for fallback CAF match
    sf_property_name = normalize_text(prop.get("Name")) if prop else ""

    # OSC required match by Account Number
    osc = osc_lookup(servicer_key)
    osc_primary = ""
    osc_ok = False
    osc_address_disp = ""
    if osc["found"]:
        r = osc["row"] or {}
        osc_primary = normalize_text(r.get("primary_status"))
        osc_ok = (osc_primary.strip().lower() == TARGET_OSC_PRIMARY)
        osc_address_disp = (
            f"{normalize_text(r.get('property_street'))} "
            f"{normalize_text(r.get('property_city'))} "
            f"{normalize_text(r.get('property_state'))} "
            f"{normalize_text(r.get('property_zip'))}"
        ).strip().upper()

    # CAF required match by Order Id prefix FIRST
    dn_digits = digits_only(deal_num)
    caf = caf_lookup_by_order_id_prefix(dn_digits)

    # CAF fallback match by Property__c.Name if Order Id fails
    caf_used = caf.get("used")
    if not caf.get("found"):
        caf2 = caf_lookup_by_property_name(sf_property_name)
        if caf2.get("found"):
            caf = caf2
            caf_used = caf2.get("used")

    caf_address_disp = ""
    caf_statuses = []
    caf_ok = False
    if caf.get("found"):
        row = caf.get("row") or {}
        # ✅ CAF property address column explicitly used for display (you said we ignored it)
        caf_addr_raw = normalize_text(row.get("property_address"))
        caf_address_disp = caf_addr_raw.upper() if caf_addr_raw else ""
        caf_statuses = pick_payment_statuses(row)
        if caf_statuses:
            caf_ok = all(is_payment_status_ok(v) for (_k, v) in caf_statuses)

    # HUD address auto-fill = SF Full_Address__c (as requested)
    hud_address_disp = sf_full_address_disp

    checks = []

    checks.append({
        "Check": "Salesforce address (auto-fill only)",
        "Value": sf_full_address_disp if sf_full_address_disp else "(blank)",
        "Result": "✅ OK" if sf_full_address_disp else "⚠️ Blank",
        "Note": "Uses Property__c.Full_Address__c."
    })

    checks.append({
        "Check": "CAF match (required)",
        "Value": f"Matched by {caf_used}" if caf.get("found") else caf.get("error", "No CAF match"),
        "Result": "✅ OK" if caf.get("found") else "🚫 Stop",
        "Note": "Primary: Order Id prefix (deal number before '-'). Fallback: Property__c.Name contains match."
    })

    # OSC required
    if not osc["found"]:
        checks.append({
            "Check": "OSC insurance status (required)",
            "Value": osc["error"],
            "Result": "🚫 Stop",
            "Note": "Must find OSC row by Account Number = servicer ID."
        })
    else:
        checks.append({
            "Check": "OSC insurance status (required)",
            "Value": osc_primary if osc_primary else "(blank)",
            "Result": "✅ OK" if osc_ok else "🚫 Stop",
            "Note": "Must be Outside Policy In-Force."
        })

    # CAF payment statuses required (same strictness as before)
    if not caf.get("found"):
        checks.append({
            "Check": "CAF installment payment status (required)",
            "Value": caf.get("error", "No CAF match"),
            "Result": "🚫 Stop",
            "Note": "No CAF row matched."
        })
    else:
        if caf_statuses:
            summary = " | ".join([f"{k}: {v}" for (k, v) in caf_statuses])
            checks.append({
                "Check": "CAF installment payment status (required)",
                "Value": summary,
                "Result": "✅ OK" if caf_ok else "⚠️ Review",
                "Note": "Review if any delinquent/late/past due."
            })
        else:
            checks.append({
                "Check": "CAF installment payment status (required)",
                "Value": "Statuses not found in CAF row",
                "Result": "⚠️ Review",
                "Note": "CAF row matched but status columns missing/empty."
            })

    # Overall eligibility:
    overall_ok = bool(servicer_key) and osc_ok and caf.get("found") and caf_statuses and caf_ok

    return {
        "deal_number": deal_num,
        "deal_name": deal_name,
        "account_name": acct_name,
        "servicer_key": servicer_key,
        "total_loan_amount": total_loan_amount,
        "checks": checks,
        "overall_ok": overall_ok,
        # debug values
        "sf_address": sf_full_address_disp,
        "sf_property_name": sf_property_name,
        "osc_address": osc_address_disp,
        "caf_address": caf_address_disp,
        "caf_match_used": caf_used,
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
        ("Available Balance:", None, "Advance Date:", ctx["advance_date"]),  # mm/dd/yyyy string
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
with st.expander("Data + troubleshooting", expanded=False):
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
# SHOW CHECK RESULTS + DEBUG
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

    st.markdown("### Debug view (SF vs OSC vs CAF)")
    d1, d2, d3 = st.columns(3)
    with d1:
        st.markdown("**Salesforce**")
        st.caption("Property__c.Name (CAF fallback match)")
        st.code(payload.get("sf_property_name") or "(blank)")
        st.caption("Property__c.Full_Address__c (HUD auto-fill)")
        st.code(payload.get("sf_address") or "(blank)")
    with d2:
        st.markdown("**OSC**")
        st.caption("Built from Property Street/City/State/Zip")
        st.code(payload.get("osc_address") or "(blank)")
    with d3:
        st.markdown("**CAF**")
        st.caption(f"Match method: {payload.get('caf_match_used') or '(none)'}")
        st.caption("Property Address column")
        st.code(payload.get("caf_address") or "(blank)")

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

    # ✅ address auto-fill is Full_Address__c ONLY (as requested)
    address_disp = payload.get("hud_address_disp") or ""

    st.subheader("HUD inputs")
    st.caption("Type amounts like `1200` or `$1,200` (leave blank for $0). Dates are mm/dd/yyyy.")

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
            "advance_date": adv_date.strftime("%m/%d/%Y"),  # ✅ mm/dd/yyyy
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
                ["Advance Date", ctx["advance_date"]],  # ✅ mm/dd/yyyy
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
