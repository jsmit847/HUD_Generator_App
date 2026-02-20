# =========================
# HUD GENERATOR (APP.PY) — SALESFORCE VERSION (Hayden removed)
# =========================
import re
import html
import textwrap
from datetime import datetime
import pandas as pd
import streamlit as st

from simple_salesforce import Salesforce
import keyring
import truststore

# =========================
# PAGE CONFIG
# =========================
st.set_page_config(
    page_title="HUD Generator",
    page_icon="🏗️",
    layout="wide",
)

# =========================
# AUTH / SALESFORCE CLIENT
# =========================
truststore.inject_into_ssl()

SERVICE = "salesforce_prod_oauth"  # same as your notebook
instance_url = keyring.get_password(SERVICE, "instance_url")
access_token = keyring.get_password(SERVICE, "access_token")

if not instance_url or not access_token:
    st.error("Missing Salesforce token in keyring. Run your OAuth flow first.")
    st.stop()

sf = Salesforce(instance_url=instance_url, session_id=access_token)

# =========================
# HELPERS
# =========================
def norm(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = (
        df.columns.astype(str)
        .str.strip()
        .str.lower()
        .str.replace(r"\s+", "_", regex=True)
    )
    return df

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

def ratio_to_pct_str(x) -> str:
    """
    Salesforce ratio/percent display:
    - If 0 < x <= 1 => ratio => *100
    - If x > 1 => already percent-like
    """
    if x is None or str(x).strip() == "":
        return ""
    try:
        v = float(x)
    except Exception:
        return str(x).strip()
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

def safe_first(df: pd.DataFrame, col: str, default=""):
    if df is None or df.empty:
        return default
    if col not in df.columns:
        return default
    return df[col].iloc[0]

def first_present_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None

def require_cols(df: pd.DataFrame, cols: list[str], dataset_name: str):
    missing = [c for c in cols if c not in df.columns]
    if missing:
        st.error(
            f"Missing expected column(s) in **{dataset_name}**: "
            + ", ".join([f"`{m}`" for m in missing])
        )
        st.stop()

def recompute(ctx: dict) -> dict:
    # Allocated Loan Amount = Advance Amount + Total Reno Drawn
    ctx["allocated_loan_amount"] = float(ctx.get("advance_amount", 0.0)) + float(ctx.get("total_reno_drawn", 0.0))

    # Construction Advance Amount: keep as the "Advance Amount" (manual)
    ctx["construction_advance_amount"] = float(ctx.get("advance_amount", 0.0))

    # Fees
    fee_keys = ["inspection_fee", "wire_fee", "construction_mgmt_fee", "title_fee"]
    ctx["total_fees"] = sum(float(ctx.get(k, 0.0)) for k in fee_keys)

    # Optional late charges line item
    include_lates = bool(ctx.get("include_late_charges", False))
    late_charges = float(ctx.get("accrued_late_charges_amt", 0.0))
    ctx["late_charges_line_item"] = late_charges if include_lates else 0.0

    # Net Amount to Borrower = Advance Amount - (Fees + Optional Lates)
    ctx["net_amount_to_borrower"] = ctx["construction_advance_amount"] - ctx["total_fees"] - ctx["late_charges_line_item"]

    # Available Balance rule (your test rule)
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

    borrower_disp = html.escape(str(ctx.get("borrower_disp", "") or ""))
    address_disp = html.escape(str(ctx.get("address_disp", "") or ""))
    workday_sup_code = html.escape(str(ctx.get("workday_sup_code", "") or ""))
    advance_date = html.escape(str(ctx.get("advance_date", "") or ""))

    # Holdbacks
    hb_current = html.escape(str(ctx.get("holdback_current", "") or ""))
    hb_closing = html.escape(str(ctx.get("holdback_closing", "") or ""))

    # Yardi vendor code (new)
    yardi_vendor_code = html.escape(str(ctx.get("yardi_vendor_code", "") or ""))

    show_lates = bool(ctx.get("include_late_charges", False))

    html_str = f"""
<style>
  .hud-page {{
    width: 980px;
    font-family: Arial, Helvetica, sans-serif;
    font-size: 13px;
    color: #000;
  }}
  .hud-top {{
    text-align: center;
    margin-bottom: 10px;
    line-height: 1.25;
  }}
  .hud-top .c1 {{ font-weight: 700; }}
  .hud-top .c3 {{ font-weight: 800; font-size: 16px; }}
  .hud-box {{
    border: 2px solid #000;
    padding: 10px;
  }}
  table.hud {{
    width: 100%;
    border-collapse: collapse;
    table-layout: fixed;
  }}
  table.hud td {{
    border: 0;
    padding: 4px 6px;
    vertical-align: middle;
  }}
  .grid {{ border: 1px solid #d0d0d0; }}
  .lbl {{ font-weight: 700; text-align: left; width: 24%; }}
  .val {{ text-align: right; width: 26%; white-space: nowrap; }}
  .rlbl {{ font-weight: 700; text-align: left; width: 24%; }}
  .rval {{ text-align: right; width: 26%; white-space: nowrap; }}
  .borrower-line {{
    border-top: 2px solid #000;
    margin-top: 10px;
    padding-top: 8px;
  }}
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
        <td class="rlbl">Workday SUP Code:</td><td class="rval grid">{workday_sup_code}</td>
      </tr>
      <tr>
        <td class="lbl">Yardi Vendor Code:</td><td class="val grid">{yardi_vendor_code}</td>
        <td class="rlbl">Advance Date:</td><td class="rval grid"><span class="bold">{advance_date}</span></td>
      </tr>
    </table>

    <div class="borrower-line">
      <div><span class="bold">Borrower:</span> {borrower_disp}</div>
      <div class="addr-line"><span class="bold">Address:</span> {address_disp}</div>
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
    {"<tr><td>Accrued Late Charges</td><td>" + fmt_money(ctx.get("late_charges_line_item", 0.0)) + "</td></tr>" if show_lates else ""}
    <tr class="tot"><td>Total Fees</td><td>{fmt_money(ctx.get("total_fees", 0.0) + (ctx.get("late_charges_line_item",0.0) if show_lates else 0.0))}</td></tr>
    <tr class="tot"><td>Reimbursement to Borrower</td><td>{fmt_money(ctx.get("net_amount_to_borrower", 0.0))}</td></tr>
  </table>
</div>
"""
    return textwrap.dedent(html_str).strip()

# =========================
# LOAD NON-SF FILES (keep these as-is for now)
# =========================
st.title("🏗️ HUD Generator")
st.caption("Upload once • Validate deals • Generate HUD • Edit after preview • Export HTML")

with st.sidebar:
    st.header("📂 Files (upload once)")

    fci_file = st.file_uploader("FCI Loan Detail_RSLD (CSV)", type=["csv"], key="fci_upl")
    ice_file = st.file_uploader("ICE Updated Taxes (XLSX)", type=["xlsx"], key="ice_upl")
    osc_file = st.file_uploader("OSC ZStatus (XLSX)", type=["xlsx"], key="osc_upl")

    cA, cB = st.columns(2)
    load_clicked = cA.button("✅ Load / Reload", use_container_width=True)
    clear_clicked = cB.button("🧹 Clear", use_container_width=True)

    st.divider()
    st.caption("Tip: After you load files once, you can run multiple deals without re-uploading.")

if "data" not in st.session_state:
    st.session_state.data = None

if clear_clicked:
    st.session_state.data = None
    st.success("Cleared loaded data. Re-upload files when ready.")
    st.stop()

@st.cache_data(show_spinner=False)
def load_all(fci_upl, ice_upl, osc_upl):
    try:
        fci_df = norm(pd.read_csv(fci_upl, sep="|", engine="python", dtype=str, na_filter=False))
    except Exception as e:
        raise RuntimeError(f"Could not read FCI CSV. Is it pipe-delimited (|)?\n\nDetails: {e}")

    try:
        ice_df = norm(pd.read_excel(ice_upl, sheet_name="Detail2", skiprows=2, dtype=str))
    except Exception as e:
        raise RuntimeError(
            "Could not read ICE workbook.\n"
            "Expected sheet: 'Detail2' with header row starting at Excel row 3.\n\n"
            f"Details: {e}"
        )

    try:
        osc_df = norm(pd.read_excel(osc_upl, sheet_name="COREVEST", dtype=str))
    except Exception as e:
        raise RuntimeError(
            "Could not read OSC workbook.\n"
            "Expected sheet: 'COREVEST'.\n\n"
            f"Details: {e}"
        )

    return {"fci": fci_df, "ice": ice_df, "osc": osc_df}

if load_clicked:
    if not all([fci_file, ice_file, osc_file]):
        st.sidebar.error("Please upload FCI + ICE + OSC before clicking Load.")
    else:
        try:
            st.session_state.data = load_all(fci_file, ice_file, osc_file)
            st.sidebar.success("Files loaded. You can now generate HUDs.")
        except Exception as e:
            st.sidebar.error(str(e))

if st.session_state.data is None:
    st.info("Upload **FCI + ICE + OSC** in the sidebar, then click **Load / Reload**.")
    st.stop()

fci = st.session_state.data["fci"]
ice = st.session_state.data["ice"]
osc = st.session_state.data["osc"]

require_cols(fci, ["account"], "FCI")
require_cols(osc, ["account_number", "primary_status"], "OSC")

# =========================
# SALESFORCE FETCHERS
# =========================
def sf_money(x) -> float:
    try:
        if x is None or x == "":
            return 0.0
        return float(x)
    except Exception:
        return 0.0

def sf_text(x) -> str:
    return ("" if x is None else str(x)).strip()

def fetch_property_by_yardi(yardi_id: str) -> dict | None:
    # Try with Account__c first; if your org uses a different lookup name, we fall back.
    fields_try = [
        "Id",
        "Borrower_Name__c",
        "Full_Address__c",
        "Yardi_Id__c",
        "Initial_Disbursement_Used__c",
        "Renovation_Advance_Amount_Used__c",
        "Interest_Allocation__c",
        "Holdback_To_Rehab_Ratio__c",
        "Account__c",
    ]

    while True:
        soql = f"SELECT {', '.join(fields_try)} FROM Property__c WHERE Yardi_Id__c = '{yardi_id}' LIMIT 1"
        try:
            rows = sf.query_all(soql).get("records", [])
            return rows[0] if rows else None
        except Exception as e:
            msg = str(e)
            m = re.search(r"No such column '([^']+)'", msg)
            if m:
                bad = m.group(1)
                if bad in fields_try:
                    fields_try.remove(bad)
                    continue
            raise

def fetch_latest_advance_for_property(property_id: str) -> dict:
    # you can add more fields as you discover them
    soql = f"""
    SELECT Id, LOC_Commitment__c, Wire_Date__c, CreatedDate
    FROM Advance__c
    WHERE Property__c = '{property_id}'
    ORDER BY CreatedDate DESC
    LIMIT 1
    """.strip()
    rows = sf.query_all(soql).get("records", [])
    return rows[0] if rows else {}

def fetch_account_vendor_code(account_id: str) -> str:
    if not account_id:
        return ""
    soql = f"SELECT Id, Yardi_Vendor_Code__c FROM Account WHERE Id = '{account_id}' LIMIT 1"
    rows = sf.query_all(soql).get("records", [])
    if not rows:
        return ""
    return sf_text(rows[0].get("Yardi_Vendor_Code__c"))

# =========================
# MAIN UI
# =========================
tab_inputs, tab_results = st.tabs(["🧾 Inputs", "📄 Results / Export"])

with tab_inputs:
    st.subheader("Deal Inputs")

    with st.form("inputs_form"):
        deal_number = st.text_input("Deal Number (Yardi ID)", placeholder="58439")
        advance_amount = st.number_input("Advance Amount", min_value=0.0, step=0.01, format="%.2f")

        c1, c2, c3 = st.columns(3)
        holdback_current_raw = c1.text_input("Holdback % Current (optional override)", placeholder="(leave blank to use SF)")
        holdback_closing_raw = c2.text_input("Holdback % at Closing (manual for now)", placeholder="100")
        advance_date_raw = c3.text_input("Advance Date (optional override)", placeholder="MM/DD/YYYY")

        st.markdown("**Fees (manual):**")
        f1, f2, f3, f4 = st.columns(4)
        inspection_fee = f1.number_input("3rd party Inspection Fee", min_value=0.0, step=0.01, format="%.2f")
        wire_fee = f2.number_input("Wire Fee", min_value=0.0, step=0.01, format="%.2f")
        construction_mgmt_fee = f3.number_input("Construction Management Fee", min_value=0.0, step=0.01, format="%.2f")
        title_fee = f4.number_input("Title Fee", min_value=0.0, step=0.01, format="%.2f")

        include_late_charges = st.checkbox("Include FCI accrued late charges as a HUD line item", value=False)

        submitted = st.form_submit_button("Generate HUD ✅")

    if not submitted:
        st.stop()

    deal_number = str(deal_number).strip()
    if deal_number == "":
        st.error("Deal Number (Yardi ID) is required.")
        st.stop()

    # =========================
    # SALESFORCE LOOKUP (replaces Hayden)
    # =========================
    prop = fetch_property_by_yardi(deal_number)
    if not prop:
        st.error("Deal Number (Yardi ID) not found in Salesforce Property__c.Yardi_Id__c.")
        st.stop()

    adv = fetch_latest_advance_for_property(prop["Id"])

    account_id = prop.get("Account__c")  # may be missing if your lookup is named differently
    yardi_vendor_code = fetch_account_vendor_code(account_id) if account_id else ""

    # =========================
    # Servicer ID replacement strategy (for now)
    # =========================
    # Your old flow keyed FCI/OSC off servicer_id. Until we find the Salesforce equivalent,
    # use Yardi ID as the "servicer_id" placeholder so the joins still run.
    servicer_id = deal_number

    # FCI match (do NOT block on late charges)
    fci_match = fci[fci["account"].astype(str).str.strip() == servicer_id]
    if fci_match.empty:
        st.error("No matching FCI record found for this ID (currently using Yardi ID as Account key).")
        st.stop()

    next_payment_due = safe_first(fci_match, "nextpaymentdue", "")
    accrued_late_charges_raw = safe_first(fci_match, "accruedlatecharges", "")
    status_enum = safe_first(fci_match, "statusenum", "")
    property_street = safe_first(fci_match, "propertystreet", "")

    accrued_late_charges_amt = parse_money(accrued_late_charges_raw)

    # OSC match (policy check)
    osc_match = osc[osc["account_number"].astype(str).str.strip() == servicer_id]
    if osc_match.empty:
        st.error("No OSC record found for this ID (currently using Yardi ID as Account Number key).")
        st.stop()

    primary_status = safe_first(osc_match, "primary_status", "")
    if primary_status != "Outside Policy In-Force":
        st.error("🚨 OSC Primary Status is NOT Outside Policy In-Force — reach out to the borrower.")
        st.stop()

    # Address display: prefer Salesforce Full_Address__c; if blank, fall back to OSC parts like before
    sf_full_addr = sf_text(prop.get("Full_Address__c"))
    if sf_full_addr:
        address_disp = sf_full_addr.upper()
    else:
        street = str(safe_first(osc_match, "property_street", "")).strip()
        city = str(safe_first(osc_match, "property_city", "")).strip()
        state = str(safe_first(osc_match, "property_state", "")).strip()
        zipc = str(safe_first(osc_match, "property_zip", "")).strip()
        address_disp = " ".join([p for p in [street, city, state, zipc] if p]).strip().upper()

    # ICE optional check (best-effort match)
    ice_addr_col = first_present_col(ice, ["property_address", "propertyaddress", "address", "site_address"])
    ice_status = {}
    if ice_addr_col and str(property_street).strip():
        addr = str(property_street).lower().strip()
        ice_addr = ice[ice_addr_col].astype(str).str.lower().str.strip()
        ice_match = ice[ice_addr.str.contains(re.escape(addr), na=False)]
        if not ice_match.empty:
            for col in ["inst_1_payment_status", "inst_2_payment_status", "inst_3_payment_status"]:
                if col in ice_match.columns:
                    ice_status[col] = ice_match[col].iloc[0]

    # =========================
    # BUILD CONTEXT (Salesforce replaces Hayden fields)
    # =========================
    total_loan_amount = sf_money(adv.get("LOC_Commitment__c"))                # Loan Commitment -> Advance__c.LOC_Commitment__c
    initial_advance   = sf_money(prop.get("Initial_Disbursement_Used__c"))    # Initial Disbursement Funded -> Property__c.Initial_Disbursement_Used__c
    total_reno_drawn  = sf_money(prop.get("Renovation_Advance_Amount_Used__c"))  # Total Reno Drawn -> Property__c.Renovation_Advance_Amount_Used__c
    interest_reserve  = sf_money(prop.get("Interest_Allocation__c"))          # Interest Reserve -> Property__c.Interest_Allocation__c

    sf_holdback_current = ratio_to_pct_str(prop.get("Holdback_To_Rehab_Ratio__c"))  # Holdback % Current -> ratio field
    holdback_current = normalize_pct(holdback_current_raw) if str(holdback_current_raw).strip() else sf_holdback_current

    holdback_closing = normalize_pct(holdback_closing_raw)

    # Advance date: default from latest Advance__c.Wire_Date__c; allow manual override
    sf_advance_date = parse_date_to_mmddyyyy(sf_text(adv.get("Wire_Date__c")))
    advance_date = parse_date_to_mmddyyyy(advance_date_raw) if str(advance_date_raw).strip() else sf_advance_date

    # Borrower name from SF; fallback empty
    borrower_disp = sf_text(prop.get("Borrower_Name__c")).upper()

    # Workday SUP Code: not found yet → keep blank placeholder
    workday_sup_code = ""

    ctx = {
        "deal_number": deal_number,
        "servicer_id": servicer_id,

        "total_loan_amount": total_loan_amount,
        "initial_advance": initial_advance,
        "total_reno_drawn": total_reno_drawn,
        "interest_reserve": interest_reserve,

        "advance_amount": float(advance_amount),

        "holdback_current": holdback_current,
        "holdback_closing": holdback_closing,
        "advance_date": advance_date,

        "workday_sup_code": workday_sup_code,
        "yardi_vendor_code": yardi_vendor_code,

        "borrower_disp": borrower_disp,
        "address_disp": address_disp,

        "inspection_fee": float(inspection_fee),
        "wire_fee": float(wire_fee),
        "construction_mgmt_fee": float(construction_mgmt_fee),
        "title_fee": float(title_fee),

        "accrued_late_charges_raw": str(accrued_late_charges_raw),
        "accrued_late_charges_amt": float(accrued_late_charges_amt),
        "include_late_charges": bool(include_late_charges),
    }

    ctx = recompute(ctx)

    st.session_state["last_ctx"] = ctx
    st.session_state["last_snapshot"] = {
        "primary_status": primary_status,
        "next_payment_due": next_payment_due,
        "status_enum": status_enum,
        "accrued_late_charges_raw": accrued_late_charges_raw,
        "ice_status": ice_status,

        # debug / audit
        "sf_property_id": prop.get("Id"),
        "sf_advance_id": adv.get("Id"),
        "sf_account_id": account_id,
    }

with tab_results:
    if "last_ctx" not in st.session_state:
        st.info("Generate a HUD from the Inputs tab first.")
        st.stop()

    ctx = st.session_state["last_ctx"]
    snap = st.session_state.get("last_snapshot", {})

    st.subheader("Validation Snapshot")
    a, b, c, d = st.columns(4)
    a.metric("Deal (Yardi ID)", ctx.get("deal_number", ""))
    b.metric("Servicer ID (temp)", ctx.get("servicer_id", ""))
    c.metric("SF Property Id", snap.get("sf_property_id", ""))
    d.metric("OSC Primary Status", snap.get("primary_status", ""))

    e, f, g = st.columns(3)
    e.metric("FCI Next Payment Due", snap.get("next_payment_due", ""))
    f.metric("FCI Status Enum", snap.get("status_enum", ""))
    g.metric("FCI Accrued Late Charges", snap.get("accrued_late_charges_raw", "") or "0")

    if snap.get("ice_status"):
        with st.expander("ICE Installment Status (best-effort match)"):
            st.json(snap["ice_status"])

    with st.expander("Debug (Salesforce IDs)"):
        st.json({
            "sf_property_id": snap.get("sf_property_id"),
            "sf_advance_id": snap.get("sf_advance_id"),
            "sf_account_id": snap.get("sf_account_id"),
            "yardi_vendor_code": ctx.get("yardi_vendor_code"),
        })

    st.divider()

    st.subheader("HUD Preview")
    st.markdown(render_hud_html(ctx), unsafe_allow_html=True)

    st.divider()

    st.subheader("Edit After Preview (updates HUD)")
    st.caption("Edit values below, click **Apply Edits**, then re-download the HTML.")

    editable_rows = [
        ("Total Loan Amount", ctx["total_loan_amount"], "money"),
        ("Initial Advance", ctx["initial_advance"], "money"),
        ("Total Reno Drawn", ctx["total_reno_drawn"], "money"),
        ("Advance Amount", ctx["advance_amount"], "money"),
        ("Interest Reserve", ctx["interest_reserve"], "money"),
        ("Holdback % Current", ctx.get("holdback_current", ""), "text"),
        ("Holdback % at Closing", ctx.get("holdback_closing", ""), "text"),
        ("Advance Date", ctx.get("advance_date", ""), "text"),
        ("Workday SUP Code", ctx.get("workday_sup_code", ""), "text"),
        ("Yardi Vendor Code", ctx.get("yardi_vendor_code", ""), "text"),
        ("Borrower", ctx.get("borrower_disp", ""), "text"),
        ("Address", ctx.get("address_disp", ""), "text"),
        ("3rd party Inspection Fee", ctx["inspection_fee"], "money"),
        ("Wire Fee", ctx["wire_fee"], "money"),
        ("Construction Management Fee", ctx["construction_mgmt_fee"], "money"),
        ("Title Fee", ctx["title_fee"], "money"),
        ("Include Late Charges Line Item", bool(ctx.get("include_late_charges", False)), "bool"),
    ]
    editor_df = pd.DataFrame(editable_rows, columns=["Field", "Value", "Type"])

    edited = st.data_editor(
        editor_df,
        hide_index=True,
        use_container_width=True,
        column_config={
            "Field": st.column_config.TextColumn(disabled=True),
            "Type": st.column_config.TextColumn(disabled=True),
        },
        key="hud_editor",
    )

    if st.button("Apply Edits ✅"):
        by_field = dict(zip(edited["Field"], edited["Value"]))

        ctx["total_loan_amount"] = parse_money(by_field.get("Total Loan Amount"))
        ctx["initial_advance"] = parse_money(by_field.get("Initial Advance"))
        ctx["total_reno_drawn"] = parse_money(by_field.get("Total Reno Drawn"))
        ctx["advance_amount"] = float(parse_money(by_field.get("Advance Amount")))
        ctx["interest_reserve"] = parse_money(by_field.get("Interest Reserve"))

        ctx["holdback_current"] = normalize_pct(by_field.get("Holdback % Current"))
        ctx["holdback_closing"] = normalize_pct(by_field.get("Holdback % at Closing"))
        ctx["advance_date"] = parse_date_to_mmddyyyy(by_field.get("Advance Date"))

        ctx["workday_sup_code"] = str(by_field.get("Workday SUP Code", "")).strip()
        ctx["yardi_vendor_code"] = str(by_field.get("Yardi Vendor Code", "")).strip()

        ctx["borrower_disp"] = str(by_field.get("Borrower", "")).strip().upper()
        ctx["address_disp"] = str(by_field.get("Address", "")).strip().upper()

        ctx["inspection_fee"] = float(parse_money(by_field.get("3rd party Inspection Fee")))
        ctx["wire_fee"] = float(parse_money(by_field.get("Wire Fee")))
        ctx["construction_mgmt_fee"] = float(parse_money(by_field.get("Construction Management Fee")))
        ctx["title_fee"] = float(parse_money(by_field.get("Title Fee")))

        ctx["include_late_charges"] = bool(by_field.get("Include Late Charges Line Item", False))

        ctx = recompute(ctx)
        st.session_state["last_ctx"] = ctx

        st.success("Edits applied.")
        st.markdown(render_hud_html(ctx), unsafe_allow_html=True)

    st.divider()

    st.download_button(
        "⬇️ Download HUD as HTML",
        data=render_hud_html(ctx),
        file_name=f"HUD_{ctx.get('deal_number','')}.html",
        mime="text/html",
    )

    st.caption("You can run another deal from the Inputs tab without re-uploading files.")
