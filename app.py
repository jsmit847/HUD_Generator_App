import re
from datetime import datetime

import pandas as pd
import streamlit as st

# -------------------------
# Page config + styling
# -------------------------
st.set_page_config(page_title="HUD Generator", layout="wide")

APP_CSS = """
<style>
:root {
  --bg: #0b1220;
  --card: #111b2e;
  --muted: #93a4c7;
  --text: #e8eefc;
  --accent: #4f8cff;
  --line: rgba(255,255,255,0.12);
}
html, body, [class*="css"]  { color: var(--text); }
.block-container { padding-top: 1.25rem; }
h1, h2, h3 { letter-spacing: 0.3px; }
.small-muted { color: var(--muted); font-size: 0.92rem; }
.card {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 14px;
  padding: 14px 16px;
}
.pill {
  display:inline-block;
  padding: 4px 10px;
  border-radius: 999px;
  background: rgba(79,140,255,0.18);
  border: 1px solid rgba(79,140,255,0.35);
  color: var(--text);
  font-size: 0.88rem;
  margin-left: 8px;
}
.hud-wrap {
  background: white;
  color: #111;
  border-radius: 10px;
  padding: 18px;
  border: 1px solid #ddd;
  font-family: Arial, Helvetica, sans-serif;
}
.hud-title {
  text-align:center;
  font-weight: 800;
  font-size: 18px;
  margin-bottom: 10px;
}
.hud-grid {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.hud-grid td {
  padding: 6px 8px;
  vertical-align: top;
}
.hud-label { font-weight: 700; }
.hud-val { text-align: right; white-space: nowrap; }
.hud-divider td {
  padding: 8px 0;
  border-top: 2px solid #000;
}
.hud-subhead td {
  font-weight: 800;
  border-bottom: 1px solid #000;
  padding-top: 12px;
}
.hud-line td {
  border-bottom: 1px solid #ddd;
}
.hud-right { text-align: right; }
</style>
"""
st.markdown(APP_CSS, unsafe_allow_html=True)

st.title("HUD Generator")
st.markdown('<div class="small-muted">Upload once, validate deals, generate the final settlement statement, export HTML.</div>', unsafe_allow_html=True)

# -------------------------
# Helpers
# -------------------------
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
    s = str(val).strip()
    if s == "" or s.lower() == "nan":
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
    if t == "" or t.lower() == "nan":
        return ""
    t = t.replace("%", "")
    try:
        v = float(t)
    except Exception:
        return ""
    if 0 < v <= 1:
        v *= 100
    return f"{v:.0f}%"

def parse_date_to_mmddyyyy(s: str) -> str:
    t = str(s).strip()
    if t == "" or t.lower() == "nan":
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
        dt = pd.to_datetime(t)
        return dt.strftime("%m/%d/%Y")
    except Exception:
        return ""

def require_cols(df: pd.DataFrame, cols: list[str], name: str):
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise KeyError(f"{name} is missing required column(s): {missing}")

def first_row(df: pd.DataFrame) -> pd.Series:
    return df.iloc[0] if not df.empty else pd.Series(dtype=object)

# -------------------------
# Session state
# -------------------------
if "files_loaded" not in st.session_state:
    st.session_state.files_loaded = False

# -------------------------
# Sidebar uploads
# -------------------------
with st.sidebar:
    st.header("Files")
    fci_file = st.file_uploader("FCI Loan Detail (CSV)", type=["csv"])
    hayden_file = st.file_uploader("Hayden Active Loans (XLSX)", type=["xlsx"])
    ice_file = st.file_uploader("ICE Updated Taxes (XLSX)", type=["xlsx"])
    osc_file = st.file_uploader("OSC ZStatus (XLSX)", type=["xlsx"])

    colA, colB = st.columns(2)
    load_clicked = colA.button("Load")
    clear_clicked = colB.button("Reset")

    if clear_clicked:
        st.session_state.files_loaded = False
        st.cache_data.clear()
        st.rerun()

    if load_clicked:
        if not all([fci_file, hayden_file, ice_file, osc_file]):
            st.error("All four files are required.")
        else:
            st.session_state.files_loaded = True
            st.success("Files loaded.")

if not st.session_state.files_loaded:
    st.info("Upload all four files, then click Load.")
    st.stop()

@st.cache_data(show_spinner=False)
def load_all(fci, hayden, ice, osc):
    fci_df = norm(pd.read_csv(fci, sep="|", dtype=str, na_filter=False))
    asset = norm(pd.read_excel(hayden, sheet_name="Bridge Asset", skiprows=3, dtype=str))
    loan = norm(pd.read_excel(hayden, sheet_name="Bridge Loan", skiprows=3, dtype=str))
    ice_df = norm(pd.read_excel(ice, sheet_name="Detail2", skiprows=2, dtype=str))
    osc_df = norm(pd.read_excel(osc, sheet_name="COREVEST", dtype=str))
    return fci_df, asset, loan, ice_df, osc_df

try:
    fci, bridge_asset, bridge_loan, ice, osc = load_all(fci_file, hayden_file, ice_file, osc_file)
except Exception as e:
    st.error(f"Failed to load files: {e}")
    st.stop()

# -------------------------
# Inputs
# -------------------------
st.markdown('<div class="card">', unsafe_allow_html=True)
st.subheader("Deal Inputs")

with st.form("inputs"):
    deal_number = st.text_input("Deal Number").strip()

    allow_cents = st.checkbox("Allow cents", value=False)
    step = 0.01 if allow_cents else 1.0
    fmt = "%.2f" if allow_cents else "%.0f"

    c1, c2, c3, c4 = st.columns(4)
    advance_amount = c1.number_input("Advance Amount", min_value=0.0, step=step, format=fmt)
    advance_date_raw = c2.text_input("Advance Date (MM/DD/YYYY)")
    holdback_current_raw = c3.text_input("Holdback % Current")
    same_holdback = c4.checkbox("Holdback Closing = Current", value=True)

    holdback_closing_raw = ""
    if not same_holdback:
        holdback_closing_raw = st.text_input("Holdback % Closing")

    st.markdown("Fees (manual)")
    f1, f2, f3, f4 = st.columns(4)
    inspection_fee = f1.number_input("3rd party Inspection Fee", min_value=0.0, step=step, format=fmt)
    wire_fee = f2.number_input("Wire Fee", min_value=0.0, step=step, format=fmt)
    construction_mgmt_fee = f3.number_input("Construction Management Fee", min_value=0.0, step=step, format=fmt)
    title_fee = f4.number_input("Title Fee", min_value=0.0, step=step, format=fmt)

    submitted = st.form_submit_button("Generate")

st.markdown("</div>", unsafe_allow_html=True)

if not submitted:
    st.stop()

if not deal_number:
    st.error("Deal Number is required.")
    st.stop()

# -------------------------
# Lookups + validations
# -------------------------
try:
    # Hayden required columns (normalized)
    require_cols(bridge_loan, ["deal_number", "servicer_id"], "Hayden (Bridge Loan)")
    require_cols(bridge_asset, ["deal_number", "servicer_id"], "Hayden (Bridge Asset)")

    # Prefer Bridge Loan, fallback Bridge Asset
    loan_hit = bridge_loan.loc[bridge_loan["deal_number"] == deal_number]
    asset_hit = bridge_asset.loc[bridge_asset["deal_number"] == deal_number]

    if not loan_hit.empty:
        hayden_row = loan_hit.iloc[0]
        hayden_source = "Bridge Loan"
    elif not asset_hit.empty:
        hayden_row = asset_hit.iloc[0]
        hayden_source = "Bridge Asset"
    else:
        st.error("Deal Number not found in Hayden (Bridge Loan or Bridge Asset).")
        st.stop()

    servicer_id = str(hayden_row.get("servicer_id", "")).strip()
    if not servicer_id:
        st.error(f"Servicer ID is blank for Deal Number {deal_number} in {hayden_source}.")
        st.stop()

    # FCI
    require_cols(fci, ["account", "nextpaymentdue", "accruedlatecharges", "statusenum"], "FCI")
    fci_match = fci[fci["account"].astype(str).str.strip() == servicer_id]
    if fci_match.empty:
        st.error("No matching FCI record for this Servicer ID.")
        st.stop()

    fci_row = first_row(fci_match)
    next_payment_due = str(fci_row.get("nextpaymentdue", "")).strip()
    accrued_late_charges = parse_money(fci_row.get("accruedlatecharges", "0"))
    status_enum = str(fci_row.get("statusenum", "")).strip()

    # OSC
    require_cols(osc, ["account_number", "primary_status", "property_street", "property_city", "property_state", "property_zip"], "OSC (COREVEST)")
    osc_match = osc[osc["account_number"].astype(str).str.strip() == servicer_id]
    if osc_match.empty:
        st.error("No matching OSC record for this Servicer ID.")
        st.stop()

    osc_row = first_row(osc_match)
    primary_status = str(osc_row.get("primary_status", "")).strip()
    if primary_status != "Outside Policy In-Force":
        st.error("Primary Status is not 'Outside Policy In-Force'. Reach out to the borrower.")
        st.stop()

    borrower_name = str(hayden_row.get("borrower_name", "")).strip()

    address = " ".join([
        str(osc_row.get("property_street", "")).strip(),
        str(osc_row.get("property_city", "")).strip(),
        str(osc_row.get("property_state", "")).strip(),
        str(osc_row.get("property_zip", "")).strip(),
    ]).strip()

except KeyError as e:
    st.error(f"Column mapping issue: {e}")
    st.stop()
except Exception as e:
    st.error(f"Unexpected error during lookup: {e}")
    st.stop()

# -------------------------
# Compute HUD values (per your current rules)
# NOTE: Hayden columns are normalized here, so you must match the normalized names.
# If you want to keep original English column names, remove norm() for Hayden and update keys.
# -------------------------
ctx = {}
ctx["loan_id"] = deal_number
ctx["borrower"] = borrower_name if borrower_name else "(blank in Hayden)"
ctx["address"] = address if address else "(blank in OSC)"

ctx["total_loan_amount"] = parse_money(hayden_row.get("loan_commitment"))
ctx["initial_advance"] = parse_money(hayden_row.get("initial_disbursement_funded"))
ctx["total_reno_drawn"] = parse_money(hayden_row.get("renovation_hb_funded"))
ctx["interest_reserve"] = parse_money(hayden_row.get("interest_allocation_funded"))

ctx["advance_amount"] = float(advance_amount)

ctx["holdback_current"] = normalize_pct(holdback_current_raw)
ctx["holdback_closing"] = normalize_pct(holdback_current_raw if same_holdback else holdback_closing_raw)
ctx["advance_date"] = parse_date_to_mmddyyyy(advance_date_raw)

ctx["inspection_fee"] = float(inspection_fee)
ctx["wire_fee"] = float(wire_fee)
ctx["construction_mgmt_fee"] = float(construction_mgmt_fee)
ctx["title_fee"] = float(title_fee)

# per your correction earlier: Allocated Loan Amount = Advance Amount + Total Reno Drawn
ctx["allocated_loan_amount"] = ctx["advance_amount"] + ctx["total_reno_drawn"]

ctx["total_fees"] = (
    ctx["inspection_fee"]
    + ctx["wire_fee"]
    + ctx["construction_mgmt_fee"]
    + ctx["title_fee"]
)

# Net amount to borrower in your prior logic
ctx["net_amount_to_borrower"] = ctx["advance_amount"] - ctx["total_fees"]

# Available balance test: total loan amount minus everything below it (your request)
ctx["available_balance"] = (
    ctx["total_loan_amount"]
    - ctx["initial_advance"]
    - ctx["total_reno_drawn"]
    - ctx["advance_amount"]
    - ctx["interest_reserve"]
    - ctx["total_fees"]
)

# Construction Advance Amount (keep aligned with your statement flow)
ctx["construction_advance_amount"] = ctx["advance_amount"]

# -------------------------
# Display validation summary (late charges shown here, not line items)
# -------------------------
left, mid, right = st.columns(3)
left.markdown('<div class="card">', unsafe_allow_html=True)
left.subheader("Validation")
left.write(f"Servicer ID: {servicer_id}")
left.write(f"FCI Status: {status_enum}")
left.write(f"Next Payment Due: {next_payment_due}")
left.write(f"Accrued Late Charges: {fmt_money(accrued_late_charges)}")
left.markdown("</div>", unsafe_allow_html=True)

mid.markdown('<div class="card">', unsafe_allow_html=True)
mid.subheader("Borrower / Property")
mid.write(f"Borrower: {ctx['borrower']}")
mid.write(f"Address: {ctx['address']}")
mid.markdown("</div>", unsafe_allow_html=True)

right.markdown('<div class="card">', unsafe_allow_html=True)
right.subheader("Key Totals")
right.metric("Net Amount to Borrower", fmt_money(ctx["net_amount_to_borrower"]))
right.metric("Available Balance", fmt_money(ctx["available_balance"]))
right.markdown("</div>", unsafe_allow_html=True)

# -------------------------
# Build HUD HTML (matches your “Final Settlement Statement” intent)
# -------------------------
hud_html = f"""
<div class="hud-wrap">
  <div class="hud-title">FINAL SETTLEMENT STATEMENT</div>

  <table class="hud-grid">
    <tr class="hud-line">
      <td class="hud-label">Total Loan Amount:</td><td class="hud-val">{fmt_money(ctx["total_loan_amount"])}</td>
      <td class="hud-label">Loan ID:</td><td class="hud-val">{ctx["loan_id"]}</td>
    </tr>
    <tr class="hud-line">
      <td class="hud-label">Initial Advance:</td><td class="hud-val">{fmt_money(ctx["initial_advance"])}</td>
      <td class="hud-label">Holdback % Current:</td><td class="hud-val">{ctx["holdback_current"] or ""}</td>
    </tr>
    <tr class="hud-line">
      <td class="hud-label">Total Reno Drawn:</td><td class="hud-val">{fmt_money(ctx["total_reno_drawn"])}</td>
      <td class="hud-label">Holdback % at Closing:</td><td class="hud-val">{ctx["holdback_closing"] or ""}</td>
    </tr>
    <tr class="hud-line">
      <td class="hud-label">Advance Amount:</td><td class="hud-val">{fmt_money(ctx["advance_amount"])}</td>
      <td class="hud-label">Allocated Loan Amount:</td><td class="hud-val">{fmt_money(ctx["allocated_loan_amount"])}</td>
    </tr>
    <tr class="hud-line">
      <td class="hud-label">Interest Reserve:</td><td class="hud-val">{fmt_money(ctx["interest_reserve"])}</td>
      <td class="hud-label">Net Amount to Borrower:</td><td class="hud-val">{fmt_money(ctx["net_amount_to_borrower"])}</td>
    </tr>
    <tr class="hud-line">
      <td class="hud-label">Available Balance:</td><td class="hud-val">{fmt_money(ctx["available_balance"])}</td>
      <td class="hud-label">Advance Date:</td><td class="hud-val">{ctx["advance_date"]}</td>
    </tr>

    <tr class="hud-divider"><td colspan="4"></td></tr>

    <tr class="hud-line">
      <td colspan="2" class="hud-label">Borrower:</td><td colspan="2">{ctx["borrower"]}</td>
    </tr>
    <tr class="hud-line">
      <td colspan="2" class="hud-label">Address:</td><td colspan="2">{ctx["address"]}</td>
    </tr>

    <tr class="hud-subhead"><td colspan="3">Charge Description</td><td class="hud-right">Amount</td></tr>

    <tr class="hud-line"><td colspan="3">Construction Advance Amount</td><td class="hud-val">{fmt_money(ctx["construction_advance_amount"])}</td></tr>
    <tr class="hud-line"><td colspan="3">3rd party Inspection Fee</td><td class="hud-val">{fmt_money(ctx["inspection_fee"])}</td></tr>
    <tr class="hud-line"><td colspan="3">Wire Fee</td><td class="hud-val">{fmt_money(ctx["wire_fee"])}</td></tr>
    <tr class="hud-line"><td colspan="3">Construction Management Fee</td><td class="hud-val">{fmt_money(ctx["construction_mgmt_fee"])}</td></tr>
    <tr class="hud-line"><td colspan="3">Title Fee</td><td class="hud-val">{fmt_money(ctx["title_fee"])}</td></tr>

    <tr class="hud-line"><td colspan="3" class="hud-label">Total Fees</td><td class="hud-val">{fmt_money(ctx["total_fees"])}</td></tr>
    <tr class="hud-line"><td colspan="3" class="hud-label">Reimbursement to Borrower</td><td class="hud-val">{fmt_money(ctx["net_amount_to_borrower"])}</td></tr>
  </table>
</div>
"""

st.markdown('<div class="card">', unsafe_allow_html=True)
st.subheader("Settlement Statement Preview")
st.markdown('<div class="small-muted">If something looks off, update inputs above and click Generate again. You do not need to re-upload files.</div>', unsafe_allow_html=True)
st.markdown(hud_html, unsafe_allow_html=True)
st.markdown("</div>", unsafe_allow_html=True)

st.download_button(
    "Download as HTML",
    data=hud_html,
    file_name=f"HUD_{deal_number}.html",
    mime="text/html",
)
