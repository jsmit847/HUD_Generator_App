# =========================
# HUD GENERATOR (APP.PY)
# =========================
import re
import html
import textwrap
from datetime import datetime
!pip install simple-salesforce pandas
import pandas as pd
import streamlit as st
import streamlit as st

sf_username = st.secrets["salesforce"]["username"]
sf_client_id = st.secrets["salesforce"]["client_id"]
sf_client_secret = st.secrets["salesforce"]["client_secret"]
sf_domain = st.secrets["salesforce"].get("domain", "login")


# =========================
# PAGE CONFIG
# =========================
st.set_page_config(
    page_title="HUD Generator",
    page_icon="🏗️",
    layout="wide",
)

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
    """
    Accepts:
      12345.67
      "12,345.67"
      "$12,345.67"
      "(12,345.67)" -> negative
      "" / None -> 0.0
    """
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
    """
    Accepts:
      "100" -> "100%"
      "100%" -> "100%"
      "1" -> "100%" (interprets 0<val<=1 as ratio)
      "" -> ""
    """
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
    """
    Accepts:
      20260114 -> 01/14/2026
      01-14-2026 -> 01/14/2026
      1/14/26 -> 01/14/2026
      "" -> ""
    """
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
            + "\n\nTip: confirm the correct sheet + header row for this file."
        )
        st.stop()


def recompute(ctx: dict) -> dict:
    # Per your HUD notes: Allocated Loan Amount = Advance Amount + Total Reno Drawn
    ctx["allocated_loan_amount"] = float(ctx.get("advance_amount", 0.0)) + float(ctx.get("total_reno_drawn", 0.0))

    # Construction Advance Amount: keep as the "Advance Amount" (manual, construction team)
    ctx["construction_advance_amount"] = float(ctx.get("advance_amount", 0.0))

    # Fees
    fee_keys = ["inspection_fee", "wire_fee", "construction_mgmt_fee", "title_fee"]
    ctx["total_fees"] = sum(float(ctx.get(k, 0.0)) for k in fee_keys)

    # Optional: include late charges in HUD charges section (does NOT block generation)
    include_lates = bool(ctx.get("include_late_charges", False))
    late_charges = float(ctx.get("accrued_late_charges_amt", 0.0))
    ctx["late_charges_line_item"] = late_charges if include_lates else 0.0

    # Net Amount to Borrower = Construction Advance Amount - (Total Fees + Optional Late Charges)
    ctx["net_amount_to_borrower"] = ctx["construction_advance_amount"] - ctx["total_fees"] - ctx["late_charges_line_item"]

    # Available Balance (your new test rule)
    # Total Loan Amount minus everything below it (excluding fees to avoid double-counting,
    # because fees are part of the advance distribution)
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

    # Escape user/data text so you never accidentally break HTML
    borrower_disp = html.escape(str(ctx.get("borrower_disp", "") or ""))
    address_disp = html.escape(str(ctx.get("address_disp", "") or ""))
    workday_sup_code = html.escape(str(ctx.get("workday_sup_code", "") or ""))
    advance_date = html.escape(str(ctx.get("advance_date", "") or ""))

    # Holdbacks
    hb_current = html.escape(str(ctx.get("holdback_current", "") or ""))
    hb_closing = html.escape(str(ctx.get("holdback_closing", "") or ""))

    # Late fees display (HUD line item optional)
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
  .hud-top .c3 {{ font-weight: 800; font-size: 16px; }} /* Final Settlement Statement bold */
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
        <td class="lbl"></td><td class="val"></td>
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
    # CRITICAL FIX: remove leading indentation so Streamlit doesn't treat it like a Markdown code block.
    return textwrap.dedent(html_str).strip()


# =========================
# STATE
# =========================
if "data" not in st.session_state:
    st.session_state.data = None  # will hold dict of dataframes


# =========================
# HEADER
# =========================
st.title("🏗️ HUD Generator")
st.caption("Upload once • Validate deals • Generate HUD • Edit after preview • Export HTML")


# =========================
# SIDEBAR — FILE UPLOADS (LOAD ONCE)
# =========================
with st.sidebar:
    st.header("📂 Files (upload once)")

    fci_file = st.file_uploader("FCI Loan Detail_RSLD (CSV)", type=["csv"], key="fci_upl")
    hayden_file = st.file_uploader("Hayden Active Loans (XLSX)", type=["xlsx"], key="hayden_upl")
    ice_file = st.file_uploader("ICE Updated Taxes (XLSX)", type=["xlsx"], key="ice_upl")
    osc_file = st.file_uploader("OSC ZStatus (XLSX)", type=["xlsx"], key="osc_upl")

    cA, cB = st.columns(2)
    load_clicked = cA.button("✅ Load / Reload", use_container_width=True)
    clear_clicked = cB.button("🧹 Clear", use_container_width=True)

    st.divider()
    st.caption("Tip: After you load files once, you can run multiple deals without re-uploading.")

if clear_clicked:
    st.session_state.data = None
    st.success("Cleared loaded data. Re-upload files when ready.")
    st.stop()

@st.cache_data(show_spinner=False)
def load_all(fci_upl, hayden_upl, ice_upl, osc_upl):
    try:
        fci_df = norm(pd.read_csv(fci_upl, sep="|", engine="python", dtype=str, na_filter=False))
    except Exception as e:
        raise RuntimeError(f"Could not read FCI CSV. Is it pipe-delimited (|)?\n\nDetails: {e}")

    try:
        asset = norm(pd.read_excel(hayden_upl, sheet_name="Bridge Asset", skiprows=3, dtype=str))
        loan = norm(pd.read_excel(hayden_upl, sheet_name="Bridge Loan", skiprows=3, dtype=str))
    except Exception as e:
        raise RuntimeError(
            "Could not read Hayden workbook.\n"
            "Expected sheets: 'Bridge Asset' and 'Bridge Loan' with header starting after 3 rows.\n\n"
            f"Details: {e}"
        )

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

    return {"fci": fci_df, "bridge_asset": asset, "bridge_loan": loan, "ice": ice_df, "osc": osc_df}


if load_clicked:
    if not all([fci_file, hayden_file, ice_file, osc_file]):
        st.sidebar.error("Please upload all 4 files before clicking Load.")
    else:
        try:
            st.session_state.data = load_all(fci_file, hayden_file, ice_file, osc_file)
            st.sidebar.success("Files loaded. You can now generate HUDs.")
        except Exception as e:
            st.sidebar.error(str(e))


if st.session_state.data is None:
    st.info("Upload all 4 files in the sidebar, then click **Load / Reload**.")
    st.stop()

# Pull dfs
fci = st.session_state.data["fci"]
bridge_asset = st.session_state.data["bridge_asset"]
bridge_loan = st.session_state.data["bridge_loan"]
ice = st.session_state.data["ice"]
osc = st.session_state.data["osc"]

# =========================
# BASIC COLUMN EXPECTATIONS (FRIENDLY FAIL FAST)
# =========================
require_cols(bridge_asset, ["deal_number", "servicer_id"], "Hayden - Bridge Asset")
require_cols(bridge_loan,  ["deal_number", "servicer_id"], "Hayden - Bridge Loan")
require_cols(fci, ["account"], "FCI")
require_cols(osc, ["account_number", "primary_status"], "OSC")

# Hayden money fields may exist in one sheet but not the other; we’ll validate after selecting the row.

# =========================
# MAIN UI
# =========================
tab_inputs, tab_results = st.tabs(["🧾 Inputs", "📄 Results / Export"])

with tab_inputs:
    st.subheader("Deal Inputs")

    with st.form("inputs_form"):
        deal_number = st.text_input("Deal Number", placeholder="58439")
        advance_amount = st.number_input("Advance Amount", min_value=0.0, step=0.01, format="%.2f")

        c1, c2, c3 = st.columns(3)
        holdback_current_raw = c1.text_input("Holdback % Current", placeholder="100")
        holdback_closing_raw = c2.text_input("Holdback % at Closing", placeholder="100")
        advance_date_raw = c3.text_input("Advance Date", placeholder="MM/DD/YYYY")

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
        st.error("Deal Number is required.")
        st.stop()

    # =========================
    # LOOKUPS
    # =========================
    loan_hit = bridge_loan.loc[bridge_loan["deal_number"].astype(str).str.strip() == deal_number]
    asset_hit = bridge_asset.loc[bridge_asset["deal_number"].astype(str).str.strip() == deal_number]

    if not loan_hit.empty:
        hayden_row = loan_hit.iloc[0]
        hayden_sheet = "Bridge Loan"
    elif not asset_hit.empty:
        hayden_row = asset_hit.iloc[0]
        hayden_sheet = "Bridge Asset"
    else:
        st.error("Deal Number not found in Hayden (Bridge Loan or Bridge Asset).")
        st.stop()

    servicer_id = str(hayden_row.get("servicer_id", "")).strip()
    if servicer_id == "":
        st.error(f"Servicer ID missing for this deal in Hayden ({hayden_sheet}).")
        st.stop()

    # FCI match (do NOT block on late charges)
    fci_match = fci[fci["account"].astype(str).str.strip() == servicer_id]
    if fci_match.empty:
        st.error("No matching FCI record found for this Servicer ID (Account).")
        st.stop()

    next_payment_due = safe_first(fci_match, "nextpaymentdue", "")
    accrued_late_charges_raw = safe_first(fci_match, "accruedlatecharges", "")
    status_enum = safe_first(fci_match, "statusenum", "")
    property_street = safe_first(fci_match, "propertystreet", "")

    accrued_late_charges_amt = parse_money(accrued_late_charges_raw)

    # OSC match (policy check)
    osc_match = osc[osc["account_number"].astype(str).str.strip() == servicer_id]
    if osc_match.empty:
        st.error("No OSC record found for this Servicer ID (Account Number).")
        st.stop()

    primary_status = safe_first(osc_match, "primary_status", "")
    if primary_status != "Outside Policy In-Force":
        st.error("🚨 OSC Primary Status is NOT Outside Policy In-Force — reach out to the borrower.")
        st.stop()

    # Build Address from OSC
    street = str(safe_first(osc_match, "property_street", "")).strip()
    city = str(safe_first(osc_match, "property_city", "")).strip()
    state = str(safe_first(osc_match, "property_state", "")).strip()
    zipc = str(safe_first(osc_match, "property_zip", "")).strip()

    address_disp = " ".join([p for p in [street, city, state, zipc] if p]).strip().upper()

    # ICE optional check (best-effort)
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
    # BUILD CONTEXT (HAYDEN -> underscore columns)
    # =========================
    def hayden_money(field_under_score: str) -> float:
        return parse_money(hayden_row.get(field_under_score, ""))

    # These *may* vary by sheet; we will warn but not crash:
    needed_hayden_fields = [
        "loan_commitment",
        "initial_disbursement_funded",
        "renovation_hb_funded",
        "interest_allocation_funded",
        "borrower_name",
        "financing",
    ]
    missing_hayden = [c for c in needed_hayden_fields if c not in hayden_row.index]
    if missing_hayden:
        st.warning(
            "Some expected fields were not found in the selected Hayden sheet "
            f"(**{hayden_sheet}**): {', '.join([f'`{m}`' for m in missing_hayden])}\n\n"
            "HUD will still generate, but missing fields may show as blank/0.00."
        )

    ctx = {
        "deal_number": deal_number,
        "servicer_id": servicer_id,

        "total_loan_amount": hayden_money("loan_commitment"),
        "initial_advance": hayden_money("initial_disbursement_funded"),
        "total_reno_drawn": hayden_money("renovation_hb_funded"),
        "interest_reserve": hayden_money("interest_allocation_funded"),

        "advance_amount": float(advance_amount),

        "holdback_current": normalize_pct(holdback_current_raw),
        "holdback_closing": normalize_pct(holdback_closing_raw),
        "advance_date": parse_date_to_mmddyyyy(advance_date_raw),

        "workday_sup_code": str(hayden_row.get("financing", "")).strip(),
        "borrower_disp": str(hayden_row.get("borrower_name", "")).strip().upper(),
        "address_disp": address_disp,

        "inspection_fee": float(inspection_fee),
        "wire_fee": float(wire_fee),
        "construction_mgmt_fee": float(construction_mgmt_fee),
        "title_fee": float(title_fee),

        # Late charges (display always; include in HUD optionally)
        "accrued_late_charges_raw": str(accrued_late_charges_raw),
        "accrued_late_charges_amt": float(accrued_late_charges_amt),
        "include_late_charges": bool(include_late_charges),
    }

    ctx = recompute(ctx)

    # Store output context for Results tab
    st.session_state["last_ctx"] = ctx
    st.session_state["last_snapshot"] = {
        "hayden_sheet": hayden_sheet,
        "primary_status": primary_status,
        "next_payment_due": next_payment_due,
        "status_enum": status_enum,
        "accrued_late_charges_raw": accrued_late_charges_raw,
        "ice_status": ice_status,
    }

with tab_results:
    if "last_ctx" not in st.session_state:
        st.info("Generate a HUD from the Inputs tab first.")
        st.stop()

    ctx = st.session_state["last_ctx"]
    snap = st.session_state.get("last_snapshot", {})

    st.subheader("Validation Snapshot")
    a, b, c, d = st.columns(4)
    a.metric("Deal", ctx.get("deal_number", ""))
    b.metric("Servicer ID", ctx.get("servicer_id", ""))
    c.metric("Hayden Sheet", snap.get("hayden_sheet", ""))
    d.metric("OSC Primary Status", snap.get("primary_status", ""))

    e, f, g = st.columns(3)
    e.metric("FCI Next Payment Due", snap.get("next_payment_due", ""))
    f.metric("FCI Status Enum", snap.get("status_enum", ""))
    fci_lates_disp = snap.get("accrued_late_charges_raw", "")
    g.metric("FCI Accrued Late Charges", fci_lates_disp if fci_lates_disp else "0")

    if snap.get("ice_status"):
        with st.expander("ICE Installment Status (best-effort match)"):
            st.json(snap["ice_status"])

    st.divider()

    st.subheader("HUD Preview")
    hud_html = render_hud_html(ctx)
    st.markdown(hud_html, unsafe_allow_html=True)

    st.divider()

    st.subheader("Edit After Preview (updates HUD)")
    st.caption("Edit values below, click **Apply Edits**, then re-download the HTML.")

    # Editable key/value grid
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
        # Map edited values back into ctx safely
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
        ctx["borrower_disp"] = str(by_field.get("Borrower", "")).strip().upper()
        ctx["address_disp"] = str(by_field.get("Address", "")).strip().upper()

        ctx["inspection_fee"] = float(parse_money(by_field.get("3rd party Inspection Fee")))
        ctx["wire_fee"] = float(parse_money(by_field.get("Wire Fee")))
        ctx["construction_mgmt_fee"] = float(parse_money(by_field.get("Construction Management Fee")))
        ctx["title_fee"] = float(parse_money(by_field.get("Title Fee")))

        # Late charge toggle
        include_flag = by_field.get("Include Late Charges Line Item", False)
        ctx["include_late_charges"] = bool(include_flag)

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
