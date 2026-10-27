"""Price check app v2 (Streamlit in Snowflake).

All history, cost, trend and deal size logic lives in SQL functions in
VENDOR_PORTFOLIO.REPORTING. This app collects inputs, runs what-if
arithmetic, draws charts and exports.
"""
import io

import altair as alt
import pandas as pd
import streamlit as st
from snowflake.snowpark.context import get_active_session

st.set_page_config(page_title="Price check", layout="wide")
session = get_active_session()
R = "VENDOR_PORTFOLIO.REPORTING."
MONEY = "$%.2f"


def q(fn, skus, order):
    d = session.sql(f"SELECT * FROM TABLE({R}{fn}(?)) ORDER BY {order}", params=[skus]).to_pandas()
    n = d.select_dtypes("number").columns
    d[n] = d[n].astype(float)
    return d


st.title("Price check")
st.caption(
    "AU invoices net of credits, AUD ex GST, foreign currency excluded. 12M = rolling 12 months. "
    "Decision cost = BC unit cost, falling back to kit component cost, then last direct cost. "
    "Kits expand into their BOM components."
)

c1, c2 = st.columns([3, 1])
sku_text = c1.text_area("SKUs (paste from Excel, comma or one per line)", height=130,
                        placeholder="LIS-LS-90-J2\nLIS-LT-84")
target_gp = c2.number_input("Target GP %", min_value=0.0, max_value=90.0, value=30.0, step=0.5)
run = c2.button("Run", type="primary", use_container_width=True)

if run and sku_text.strip():
    with st.spinner("Querying Snowflake"):
        st.session_state.df = q("F_PRICE_CHECK", sku_text, "GROUP_SKU, ROLE DESC, SKU")
        st.session_state.mon = q("F_PRICE_CHECK_MONTHLY", sku_text, "GROUP_SKU, SKU, MONTH_START")
        st.session_state.deal = q("F_PRICE_CHECK_DEALSIZE", sku_text, "GROUP_SKU, SKU, BAND_ORDER")
        st.session_state.hist = q("F_PRICE_CHECK_HISTORY", sku_text, "GROUP_SKU, SKU, FY_HALF")

if "df" not in st.session_state:
    st.info("Paste SKUs and click Run.")
    st.stop()

df = st.session_state.df.copy()
mon = st.session_state.mon.copy()
deal = st.session_state.deal.copy()
hist = st.session_state.hist.copy()
if df.empty:
    st.warning("No rows returned. Check the SKU numbers.")
    st.stop()

t = target_gp / 100.0
df["FLOOR_AT_TARGET"] = (df["DECISION_COST"] / (1 - t)).round(2)
df["BASE_GP_UNIT"] = df["ASP_12M"] - df["DECISION_COST"]
df["UNITS_PER_ORDER"] = df["UNITS_12M"] / df["ORDERS_12M"]
sku_order = df["SKU"].tolist()

# ===== 1. Summary ===========================================================
st.subheader("1. What we sell it for, and what it costs")
summary_cols = {
    "GROUP_SKU": "Group", "ROLE": "Role", "SKU": "SKU", "DESCRIPTION": "Description",
    "QTY_PER": "Qty per kit", "UNITS_12M": "Units 12M", "REVENUE_12M": "Revenue 12M",
    "ASP_12M": "ASP 12M", "LOWEST_PRICE_12M": "Lowest 12M", "MEDIAN_PRICE_12M": "Median 12M",
    "TRADE_PRICE_EX": "Trade", "ASP_DISC_TO_TRADE": "ASP disc to trade",
    "INVOICE_GP_PCT_12M": "Invoice GP 12M", "DECISION_COST": "Decision cost",
    "GP_PCT_ASP_AT_CURRENT_COST": "GP at ASP, current cost",
    "FLOOR_AT_TARGET": f"Floor at {target_gp:g}% GP",
    "CUSTOMERS_12M": "Customers 12M", "TOP_CUSTOMER_SHARE_12M": "Top customer share",
    "SHARE_OF_BRAND_REV_12M": "Share of brand rev", "SHARE_OF_BRAND_GP_12M": "Share of brand GP",
    "DATA_FLAG": "Flag",
}
pct = ["ASP disc to trade", "Invoice GP 12M", "GP at ASP, current cost", "Top customer share",
       "Share of brand rev", "Share of brand GP"]
view = df[list(summary_cols)].rename(columns=summary_cols)
for c in pct:
    view[c] = view[c] * 100
cfg = {c: st.column_config.NumberColumn(format="%.1f%%") for c in pct}
for c in ["Revenue 12M", "ASP 12M", "Lowest 12M", "Median 12M", "Trade", "Decision cost",
          f"Floor at {target_gp:g}% GP"]:
    cfg[c] = st.column_config.NumberColumn(format=MONEY)
st.dataframe(view, hide_index=True, use_container_width=True, column_config=cfg)

kits = df[df["ROLE"] == "KIT"]
kv = pd.DataFrame()
if not kits.empty:
    st.markdown("**Kit versus buying the components separately**")
    rows = []
    for _, k in kits.iterrows():
        comp = df[(df["GROUP_SKU"] == k["SKU"]) & (df["ROLE"] == "COMPONENT")]
        comp_asp = (comp["QTY_PER"] * comp["ASP_12M"]).sum(min_count=len(comp))
        comp_trade = (comp["QTY_PER"] * comp["TRADE_PRICE_EX"]).sum(min_count=len(comp))
        rows.append({
            "Kit": k["SKU"], "Kit ASP 12M": k["ASP_12M"], "Components at their ASP": comp_asp,
            "Kit premium vs components ASP": (k["ASP_12M"] / comp_asp - 1) * 100 if comp_asp else None,
            "Kit trade": k["TRADE_PRICE_EX"], "Components at trade": comp_trade,
            "Kit disc vs components trade": (1 - k["TRADE_PRICE_EX"] / comp_trade) * 100 if comp_trade else None,
        })
    kv = pd.DataFrame(rows)
    st.dataframe(kv, hide_index=True, use_container_width=True, column_config={
        "Kit ASP 12M": st.column_config.NumberColumn(format=MONEY),
        "Components at their ASP": st.column_config.NumberColumn(format=MONEY),
        "Kit premium vs components ASP": st.column_config.NumberColumn(format="%.1f%%"),
        "Kit trade": st.column_config.NumberColumn(format=MONEY),
        "Components at trade": st.column_config.NumberColumn(format=MONEY),
        "Kit disc vs components trade": st.column_config.NumberColumn(format="%.1f%%"),
    })

# ===== 2. Trend =============================================================
st.subheader("2. Trend: growing or shrinking at the price we set?")
st.caption(
    "Compares the latest 12 complete months with the 12 months before. The signal is a prompt "
    "for a question, not a verdict. Sales history only shows what we shipped: stock-outs, one large "
    "project or a kit replacing its components can all move volume without any change in demand."
)
mon["MONTH_START"] = pd.to_datetime(mon["MONTH_START"])
full = mon[~mon["IS_PARTIAL"].astype(bool)]
last_m = full["MONTH_START"].max()
prior_m = last_m - pd.DateOffset(months=12)
st.caption(f"Latest 12M window ends {last_m + pd.offsets.MonthEnd(0):%d/%m/%Y}.")


def at(month):
    return full[full["MONTH_START"] == month].set_index(["GROUP_SKU", "SKU"])


now, prev = at(last_m), at(prior_m)


def signal(du, da, top):
    if pd.isna(du):
        s = "Under 24 months of history, no comparison"
    elif du >= 5 and (pd.isna(da) or da >= -2):
        s = "Growing at a held price: price is not the constraint"
    elif du >= 5:
        s = "Growing, but with a lower price: growth may be bought"
    elif du <= -5 and (pd.isna(da) or da >= -2):
        s = "Volume down, price held: check competition, stock-outs, kit cannibalisation"
    elif du <= -5:
        s = "Volume down despite a lower price: price may not be the lever"
    else:
        s = "Stable"
    if pd.notna(top) and top > 0.4:
        s += f" (lumpy: top customer {top * 100:.0f}% of units)"
    return s


tr = []
for _, r in df.iterrows():
    key = (r["GROUP_SKU"], r["SKU"])
    n = now.loc[key] if key in now.index else None
    p = prev.loc[key] if key in prev.index else None
    un, up = (n["R12_UNITS"] if n is not None else None), (p["R12_UNITS"] if p is not None else None)
    an, ap = (n["R12_ASP"] if n is not None else None), (p["R12_ASP"] if p is not None else None)
    gn, gp = (n["R12_GP_PCT"] if n is not None else None), (p["R12_GP_PCT"] if p is not None else None)
    du = (un / up - 1) * 100 if up and pd.notna(up) and up > 0 else float("nan")
    da = (an / ap - 1) * 100 if ap and pd.notna(ap) and ap > 0 else float("nan")
    tr.append({
        "SKU": r["SKU"], "Role": r["ROLE"], "Units prior 12M": up, "Units latest 12M": un,
        "Units change": du, "ASP prior 12M": ap, "ASP latest 12M": an, "ASP change": da,
        "Invoice GP prior": gp * 100 if gp is not None and pd.notna(gp) else None,
        "Invoice GP latest": gn * 100 if gn is not None and pd.notna(gn) else None,
        "Signal": signal(du, da, r["TOP_CUSTOMER_SHARE_12M"]),
    })
trend = pd.DataFrame(tr)
st.dataframe(trend, hide_index=True, use_container_width=True, column_config={
    "Units change": st.column_config.NumberColumn(format="%+.0f%%"),
    "ASP change": st.column_config.NumberColumn(format="%+.1f%%"),
    "ASP prior 12M": st.column_config.NumberColumn(format=MONEY),
    "ASP latest 12M": st.column_config.NumberColumn(format=MONEY),
    "Invoice GP prior": st.column_config.NumberColumn(format="%.1f%%"),
    "Invoice GP latest": st.column_config.NumberColumn(format="%.1f%%"),
})

fam = pd.DataFrame()
if not kits.empty:
    st.markdown("**Family view: component units sold on their own plus units shipped inside the kit**")
    st.caption("When a kit launches, standalone component sales fall because buyers switch to the kit. "
               "The family total shows whether demand for the component is really falling.")
    frows = []
    for _, k in kits.iterrows():
        kk = (k["SKU"], k["SKU"])
        kn = now.loc[kk, "R12_UNITS"] if kk in now.index else 0.0
        kp = prev.loc[kk, "R12_UNITS"] if kk in prev.index else 0.0
        kn, kp = (0.0 if pd.isna(kn) else kn), (0.0 if pd.isna(kp) else kp)
        for _, c in df[(df["GROUP_SKU"] == k["SKU"]) & (df["ROLE"] == "COMPONENT")].iterrows():
            ck = (k["SKU"], c["SKU"])
            sn = now.loc[ck, "R12_UNITS"] if ck in now.index else 0.0
            sp = prev.loc[ck, "R12_UNITS"] if ck in prev.index else 0.0
            sn, sp = (0.0 if pd.isna(sn) else sn), (0.0 if pd.isna(sp) else sp)
            tn, tp = sn + kn * c["QTY_PER"], sp + kp * c["QTY_PER"]
            frows.append({
                "Kit": k["SKU"], "Component": c["SKU"], "Qty per kit": c["QTY_PER"],
                "Standalone prior": sp, "Standalone latest": sn,
                "In kit prior": kp * c["QTY_PER"], "In kit latest": kn * c["QTY_PER"],
                "Family prior": tp, "Family latest": tn,
                "Family change": (tn / tp - 1) * 100 if tp else None,
            })
    fam = pd.DataFrame(frows)
    st.dataframe(fam, hide_index=True, use_container_width=True, column_config={
        "Family change": st.column_config.NumberColumn(format="%+.0f%%")})

# ---- chart ----------------------------------------------------------------
cc1, cc2 = st.columns([2, 1])
pick = cc1.selectbox("Chart SKU", list(dict.fromkeys(sku_order)))
mode = cc2.radio("View", ["By quarter", "Rolling 12 months"], horizontal=True)
m1 = mon[mon["SKU"] == pick].drop_duplicates(["SKU", "MONTH_START"])
if mode == "By quarter":
    g = m1.groupby("FY_QUARTER").agg(
        START=("MONTH_START", "min"), MONTHS=("MONTH_START", "count"), UNITS=("UNITS", "sum"),
        REVENUE=("REVENUE", "sum"), PRICED=("PRICED_UNITS", "sum"), PART=("IS_PARTIAL", "max"),
    ).reset_index().sort_values("START")
    g["ASP"] = g["REVENUE"] / g["PRICED"].where(g["PRICED"] > 0)
    g["PARTIAL"] = g["PART"].astype(bool) | (g["MONTHS"] < 3)
    g["PERIOD"] = g["FY_QUARTER"] + g["PARTIAL"].map({True: " (part)", False: ""})
    base = alt.Chart(g).encode(x=alt.X("PERIOD:N", sort=g["PERIOD"].tolist(), title=None))
    bars = base.mark_bar().encode(
        y=alt.Y("UNITS:Q", title="Units"),
        opacity=alt.condition(alt.datum.PARTIAL == True, alt.value(0.35), alt.value(0.85)),  # noqa: E712
        tooltip=["PERIOD", alt.Tooltip("UNITS:Q", format=",.0f"), alt.Tooltip("ASP:Q", format="$,.2f")])
    line = base.mark_line(point=True, color="#d62728").encode(
        y=alt.Y("ASP:Q", title="ASP (AUD)", scale=alt.Scale(zero=False)))
else:
    g = m1[m1["R12_UNITS"].notna() & ~m1["IS_PARTIAL"].astype(bool)]
    base = alt.Chart(g).encode(x=alt.X("MONTH_START:T", title=None))
    bars = base.mark_area(opacity=0.4).encode(
        y=alt.Y("R12_UNITS:Q", title="Units, rolling 12M"),
        tooltip=[alt.Tooltip("MONTH_START:T", format="%m/%Y"),
                 alt.Tooltip("R12_UNITS:Q", format=",.0f"), alt.Tooltip("R12_ASP:Q", format="$,.2f")])
    line = base.mark_line(color="#d62728").encode(
        y=alt.Y("R12_ASP:Q", title="ASP rolling 12M (AUD)", scale=alt.Scale(zero=False)))
st.altair_chart(alt.layer(bars, line).resolve_scale(y="independent"), use_container_width=True)
st.caption("Bars or shaded area = units. Red line = ASP.")

d1 = deal[deal["SKU"] == pick]
if not d1.empty:
    st.markdown(f"**Price by order size, last 24 months: {pick}**")
    st.caption("How much discount bigger orders have actually received. Order = BC sales order, partial shipments combined.")
    dv = d1[["QTY_BAND", "ORDERS", "UNITS", "SHARE_OF_UNITS", "ASP", "MEDIAN_PRICE", "GP_PCT"]].copy()
    dv["SHARE_OF_UNITS"] *= 100
    dv["GP_PCT"] *= 100
    st.dataframe(dv, hide_index=True, use_container_width=True, column_config={
        "QTY_BAND": "Order size (units)", "ORDERS": "Orders", "UNITS": "Units",
        "SHARE_OF_UNITS": st.column_config.NumberColumn("Share of units", format="%.0f%%"),
        "ASP": st.column_config.NumberColumn("ASP", format=MONEY),
        "MEDIAN_PRICE": st.column_config.NumberColumn("Median price", format=MONEY),
        "GP_PCT": st.column_config.NumberColumn("Invoice GP", format="%.1f%%")})

with st.expander("ASP, units and GP by FY half"):
    if not hist.empty:
        for label, col, fmt, mult in [("ASP", "ASP", "${:,.2f}", 1), ("Units", "UNITS", "{:,.0f}", 1),
                                      ("Invoice GP %", "INVOICE_GP_PCT", "{:.1f}%", 100)]:
            st.markdown(f"*{label}*")
            pv = hist.pivot_table(index="SKU", columns="FY_HALF", values=col, aggfunc="first") * mult
            st.dataframe(pv.style.format(fmt, na_rep=""), use_container_width=True)
        st.caption("FY24 H2 starts 04/09/2023 and its invoice cost is incomplete, so its GP reads high.")


# ===== 3. What-if ===========================================================
st.subheader("3. What if we change the price")
st.caption(
    "Enter a proposed price per SKU, or change all prices by a percentage. A price typed against a SKU "
    "overrides the percentage. Expected volume change is your call: the tool does not assume a price cut "
    "creates volume. Leave it at 0 for the same-volume case."
)
b1, b2 = st.columns([1, 2])
bulk = b1.number_input("Change all prices by %", min_value=-90.0, max_value=100.0, value=0.0, step=1.0,
                       help="Applies to every SKU without its own proposed price. Negative = price cut.")
incl = b2.checkbox("Include auto-added kit components in the blended figures", value=False,
                   help="Off: blend only the SKUs you entered. On: also blend the kit components the tool added.")

wi = df[["GROUP_SKU", "SKU", "ROLE", "UNITS_12M", "ASP_12M", "LOWEST_PRICE_12M", "DECISION_COST",
         "FLOOR_AT_TARGET"]].copy()
wi["PROPOSED_PRICE"] = float("nan")
wi["VOLUME_CHANGE"] = 0.0
wi["COMPETITOR_PRICE"] = float("nan")
wi["DEAL_QTY"] = float("nan")
locked = ["GROUP_SKU", "SKU", "ROLE", "UNITS_12M", "ASP_12M", "LOWEST_PRICE_12M", "DECISION_COST", "FLOOR_AT_TARGET"]
edited = st.data_editor(
    wi, hide_index=True, use_container_width=True, key="whatif", disabled=locked,
    column_order=["SKU", "ROLE", "UNITS_12M", "ASP_12M", "LOWEST_PRICE_12M", "DECISION_COST", "FLOOR_AT_TARGET",
                  "PROPOSED_PRICE", "VOLUME_CHANGE", "DEAL_QTY", "COMPETITOR_PRICE"],
    column_config={
        "UNITS_12M": st.column_config.NumberColumn("Units 12M", format="%.0f"),
        "ASP_12M": st.column_config.NumberColumn("ASP 12M", format=MONEY),
        "LOWEST_PRICE_12M": st.column_config.NumberColumn("Lowest 12M", format=MONEY),
        "DECISION_COST": st.column_config.NumberColumn("Decision cost", format=MONEY),
        "FLOOR_AT_TARGET": st.column_config.NumberColumn(f"Floor at {target_gp:g}% GP", format=MONEY),
        "PROPOSED_PRICE": st.column_config.NumberColumn("Proposed price", format=MONEY, min_value=0.0),
        "VOLUME_CHANGE": st.column_config.NumberColumn("Expected volume change %", format="%+.0f%%",
                                                       min_value=-100.0, max_value=500.0, step=5.0),
        "COMPETITOR_PRICE": st.column_config.NumberColumn("Competitor price", format=MONEY, min_value=0.0),
        "DEAL_QTY": st.column_config.NumberColumn("Deal qty (optional)", format="%.0f", min_value=0.0, step=1.0),
    },
)

e = edited.copy()
for c in ["REVENUE_12M", "INVOICE_GP_PCT_12M", "UNITS_PER_ORDER"]:
    e[c] = df[c].values
auto = e["ASP_12M"] * (1 + bulk / 100) if bulk != 0 else pd.Series(float("nan"), index=e.index)
e["PRICE_SOURCE"] = "Unchanged"
e.loc[auto.notna(), "PRICE_SOURCE"] = f"All prices {bulk:+g}%"
e.loc[e["PROPOSED_PRICE"].notna(), "PRICE_SOURCE"] = "Typed"
e["EFF_PRICE"] = e["PROPOSED_PRICE"].fillna(auto)
p, cost, asp, u = e["EFF_PRICE"], e["DECISION_COST"], e["ASP_12M"], e["UNITS_12M"]
vol = 1 + e["VOLUME_CHANGE"].fillna(0) / 100
e["Actual GP $ 12M"] = e["REVENUE_12M"] * e["INVOICE_GP_PCT_12M"]
e["Baseline GP $"] = u * (asp - cost)
e["Same vol revenue"] = u * p
e["Same vol GP $"] = u * (p - cost)
e["Same vol GP $ change"] = e["Same vol GP $"] - e["Baseline GP $"]
e["Scenario units"] = u * vol
e["Scenario revenue"] = e["Scenario units"] * p
e["Scenario GP $"] = e["Scenario units"] * (p - cost)
e["Scenario GP $ change"] = e["Scenario GP $"] - e["Baseline GP $"]
e["GP % at proposed"] = (1 - cost / p) * 100
e["Break-even volume"] = ((asp - cost) / (p - cost) - 1) * 100
e.loc[(p - cost) <= 0, "Break-even volume"] = float("nan")
e["Extra units needed"] = u * e["Break-even volume"] / 100
e["Extra orders needed"] = e["Extra units needed"] / e["UNITS_PER_ORDER"]
e["Gap to competitor"] = p - e["COMPETITOR_PRICE"]
e["Status"] = "Above target floor"
e.loc[p < e["FLOOR_AT_TARGET"], "Status"] = "BELOW TARGET FLOOR"
e.loc[p <= cost, "Status"] = "AT OR BELOW COST"


def band_of(qv):
    if pd.isna(qv) or qv <= 0:
        return None
    return "1" if qv <= 1 else "2 to 4" if qv <= 4 else "5 to 9" if qv <= 9 else "10 to 24" if qv <= 24 else "25+"


def hist_band_asp(dealdf, sku, qv):
    b = band_of(qv)
    m = dealdf[(dealdf["SKU"] == sku) & (dealdf["QTY_BAND"] == b)]
    return m["ASP"].iloc[0] if not m.empty else float("nan")


dq = e["DEAL_QTY"]
pdeal = p.fillna(asp)
e["Hist ASP this order size"] = [hist_band_asp(deal, s_, q_) for s_, q_ in zip(e["SKU"], dq)]
e["Deal revenue"] = dq * pdeal
e["Deal GP $"] = dq * (pdeal - cost)
e["Deal GP $ at ASP"] = dq * (asp - cost)
e["Deal GP %"] = (1 - cost / pdeal) * 100
e.loc[dq.isna(), ["Deal GP %"]] = float("nan")
res = e[p.notna() | dq.notna()]
if not res.empty:
    out_cols = ["SKU", "PRICE_SOURCE", "EFF_PRICE", "GP % at proposed", "Status", "Actual GP $ 12M",
                "Baseline GP $", "Same vol revenue", "Same vol GP $", "Same vol GP $ change", "VOLUME_CHANGE",
                "Scenario units", "Scenario revenue", "Scenario GP $", "Scenario GP $ change",
                "Break-even volume", "Extra units needed", "Extra orders needed", "Gap to competitor",
                "DEAL_QTY", "Hist ASP this order size", "Deal revenue", "Deal GP $", "Deal GP %", "Deal GP $ at ASP"]
    res = res[out_cols]
    mcfg = {c: st.column_config.NumberColumn(format="$%.0f") for c in
            ["Actual GP $ 12M", "Baseline GP $", "Same vol revenue", "Same vol GP $", "Same vol GP $ change",
             "Scenario revenue", "Scenario GP $", "Scenario GP $ change", "Deal revenue", "Deal GP $",
             "Deal GP $ at ASP"]}
    mcfg.update({
        "DEAL_QTY": st.column_config.NumberColumn("Deal qty", format="%.0f"),
        "Hist ASP this order size": st.column_config.NumberColumn("Past ASP, orders this size", format=MONEY),
        "Deal GP %": st.column_config.NumberColumn(format="%.1f%%"),
        "PRICE_SOURCE": "Price from", "EFF_PRICE": st.column_config.NumberColumn("Proposed", format=MONEY),
        "GP % at proposed": st.column_config.NumberColumn(format="%.1f%%"),
        "VOLUME_CHANGE": st.column_config.NumberColumn("Your volume change", format="%+.0f%%"),
        "Scenario units": st.column_config.NumberColumn(format="%.0f"),
        "Break-even volume": st.column_config.NumberColumn("Volume needed to hold GP $", format="%+.0f%%"),
        "Extra units needed": st.column_config.NumberColumn(format="%.0f"),
        "Extra orders needed": st.column_config.NumberColumn(format="%.0f"),
        "Gap to competitor": st.column_config.NumberColumn(format=MONEY),
    })
    st.dataframe(res, hide_index=True, use_container_width=True, column_config=mcfg)
    st.caption("Actual GP $ uses the cost on past invoices. Baseline and scenarios use today's decision cost, "
               "so Baseline is what the last 12 months would have made at today's cost. Extra orders needed = "
               "extra units divided by average units per order in the last 12 months. Deal columns price one quote: the typed or % price if set, otherwise current ASP. Past ASP for orders this size shows what similar-sized orders actually paid in the last 24 months.")

deal_tbl = pd.DataFrame()
# ---- blended GP across the SKUs entered -----------------------------------
st.markdown("**Blended GP across the SKUs in the blend**")
in_blend = (e["GROUP_SKU"] == e["SKU"]) | incl
bl = e[in_blend & (u.fillna(0) > 0) & asp.notna() & cost.notna()].drop_duplicates("SKU").copy()
dropped = sorted(set(e.loc[in_blend, "SKU"]) - set(bl["SKU"]))
blend_tbl = pd.DataFrame()
if bl.empty:
    st.info("No SKU in the blend has 12M sales, an ASP and a cost.")
else:
    bp = bl["EFF_PRICE"].fillna(bl["ASP_12M"])
    bu, ba, bc = bl["UNITS_12M"], bl["ASP_12M"], bl["DECISION_COST"]
    bv = 1 + bl["VOLUME_CHANGE"].fillna(0) / 100

    def line(name, rev, gp):
        return {"Case": name, "Revenue": rev, "GP $": gp, "Blended GP %": gp / rev * 100 if rev else None}

    base_rev, base_gp = (bu * ba).sum(), (bu * (ba - bc)).sum()
    rows = [
        line("Actual, last 12M (invoice cost)", bl["REVENUE_12M"].sum(), bl["Actual GP $ 12M"].sum()),
        line("Last 12M volume at today's cost (baseline)", base_rev, base_gp),
        line("New prices, same volume", (bu * bp).sum(), (bu * (bp - bc)).sum()),
        line("New prices, your volume change", (bu * bv * bp).sum(), (bu * bv * (bp - bc)).sum()),
    ]
    blend_tbl = pd.DataFrame(rows)
    blend_tbl["GP $ change vs baseline"] = blend_tbl["GP $"] - base_gp
    blend_tbl["Revenue change vs baseline"] = blend_tbl["Revenue"] - base_rev
    blend_tbl.loc[0, ["GP $ change vs baseline", "Revenue change vs baseline"]] = float("nan")
    same_gp = rows[2]["GP $"]
    need = (base_gp / same_gp - 1) * 100 if same_gp > 0 else float("nan")

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Actual blended GP, last 12M", f"{rows[0]['Blended GP %']:.1f}%")
    k2.metric("Blended GP at today's cost", f"{rows[1]['Blended GP %']:.1f}%")
    k3.metric("Blended GP at new prices", f"{rows[2]['Blended GP %']:.1f}%",
              delta=f"{rows[2]['Blended GP %'] - rows[1]['Blended GP %']:+.1f} pts", delta_color="normal")
    k4.metric("Volume needed to hold GP $", "n/a" if pd.isna(need) else f"{need:+.0f}%")
    st.dataframe(blend_tbl, hide_index=True, use_container_width=True, column_config={
        "Revenue": st.column_config.NumberColumn(format="$%.0f"),
        "GP $": st.column_config.NumberColumn(format="$%.0f"),
        "Blended GP %": st.column_config.NumberColumn(format="%.1f%%"),
        "GP $ change vs baseline": st.column_config.NumberColumn(format="$%.0f"),
        "Revenue change vs baseline": st.column_config.NumberColumn(format="$%.0f"),
    })
    st.caption(f"Blend: {', '.join(bl['SKU'])}. Weighted by each SKU's last 12 months of sales, so the mix is "
               "assumed to stay the same. SKUs with no new price stay at their current ASP."
               + (f" Excluded (no 12M sales, ASP or cost): {', '.join(dropped)}." if dropped else ""))

    bd = bl[bl["DEAL_QTY"].fillna(0) > 0]
    deal_tbl = pd.DataFrame()
    if not bd.empty:
        dqb, dpa, dpn, dcs = bd["DEAL_QTY"], bd["ASP_12M"], bd["EFF_PRICE"].fillna(bd["ASP_12M"]), bd["DECISION_COST"]
        r_a, g_a = (dqb * dpa).sum(), (dqb * (dpa - dcs)).sum()
        r_n, g_n = (dqb * dpn).sum(), (dqb * (dpn - dcs)).sum()
        deal_tbl = pd.DataFrame([
            {"Case": "This deal at current ASP", "Revenue": r_a, "GP $": g_a, "Blended GP %": g_a / r_a * 100},
            {"Case": "This deal at new prices", "Revenue": r_n, "GP $": g_n, "Blended GP %": g_n / r_n * 100},
        ])
        deal_tbl["GP $ change"] = [float("nan"), g_n - g_a]
        st.markdown("**This deal (SKUs with a deal qty)**")
        st.dataframe(deal_tbl, hide_index=True, use_container_width=True, column_config={
            "Revenue": st.column_config.NumberColumn(format="$%.0f"),
            "GP $": st.column_config.NumberColumn(format="$%.0f"),
            "Blended GP %": st.column_config.NumberColumn(format="%.1f%%"),
            "GP $ change": st.column_config.NumberColumn(format="$%.0f")})

# ---- sensitivity grid -----------------------------------------------------
st.markdown("**Sensitivity: change in annual GP $ versus today, by price cut and volume change**")
BLEND = "Blended: all SKUs in the blend"
opts = ([BLEND] if len(bl) > 1 else []) + list(dict.fromkeys(sku_order))
gs = st.selectbox("Grid", opts, key="grid_sku")
if gs == BLEND:
    gd = bl[["UNITS_12M", "ASP_12M", "DECISION_COST"]]
else:
    gd = df[df["SKU"] == gs].head(1)[["UNITS_12M", "ASP_12M", "DECISION_COST"]]
gd = gd.dropna()
gd = gd[gd["UNITS_12M"] > 0]
if gd.empty:
    st.info("Not enough data (needs 12M sales, ASP and a cost).")
else:
    gu, ga, gc = gd["UNITS_12M"], gd["ASP_12M"], gd["DECISION_COST"]
    base_gp = (gu * (ga - gc)).sum()
    cuts, vols = [0, 5, 10, 15, 20, 25], [0, 10, 20, 30, 50, 75, 100]
    vcols = [f"+{v}% vol" for v in vols]
    recs, labels = [], []
    for c in cuts:
        price = ga * (1 - c / 100)
        gp_same = (gu * (price - gc)).sum()
        gp_pct = (1 - (gu * gc).sum() / (gu * price).sum()) * 100
        cl = f"-{c}%" if c else "Today"
        labels.append(f"{cl}  (GP {gp_pct:.1f}%)" if gs == BLEND else
                      f"{cl}  (${price.iloc[0]:,.0f}, GP {gp_pct:.1f}%)")
        rec = {"Volume to break even": (base_gp / gp_same - 1) * 100 if gp_same > 0 else float("nan")}
        for v, col in zip(vols, vcols):
            rec[col] = (gu * (1 + v / 100) * (price - gc)).sum() - base_gp
        recs.append(rec)
    grid = pd.DataFrame(recs, index=labels)
    grid.index.name = "Price cut vs ASP"
    tol = 0.02 * abs(base_gp)

    def cell(x):
        if pd.isna(x):
            return ""
        if abs(x) <= tol:
            return "background-color: #f9a825; color: #000000; font-weight: 600"
        if x > 0:
            return "background-color: #1b5e20; color: #ffffff; font-weight: 600"
        return "background-color: #b71c1c; color: #ffffff; font-weight: 600"

    fmt = {col: "{:+,.0f}" for col in vcols}
    fmt["Volume to break even"] = "{:+.0f}%"
    sty = grid.style.format(fmt, na_rep="never")
    sty = sty.map(cell, subset=vcols) if hasattr(sty, "map") else sty.applymap(cell, subset=vcols)
    st.dataframe(sty, use_container_width=True)
    st.caption(f"Today's annual GP at today's cost: ${base_gp:,.0f}. Dark green = more GP $ than today. "
               "Amber = within 2% of today (about break-even). Dark red = less. 'Volume to break even' is the "
               "extra volume each price cut needs just to stand still. "
               + ("Blended grid applies the same % cut to every SKU in the blend." if gs == BLEND else ""))


# ===== 4. Bundle builder ====================================================
st.subheader("4. Bundle builder")
st.caption(
    "Propose a bundle: add SKUs, the quantity of each in one bundle, and a discount off trade per line. "
    "Or set a target bundle price and the discount is spread evenly across all lines. Any SKU can be "
    "added, not only the ones entered above."
)
BB_COLS = ["SKU", "QTY", "DISC_PCT"]
seed_opts = ["Empty", "SKUs entered above"] + [f"BOM of {k}" for k in kits["SKU"]]
s1, s2 = st.columns([3, 1])
seed = s1.selectbox("Start from", seed_opts, key="bb_seed")
if s2.button("Load into builder", use_container_width=True):
    if seed == "SKUs entered above":
        rows = [{"SKU": x, "QTY": 1.0, "DISC_PCT": 0.0} for x in df.loc[df["GROUP_SKU"] == df["SKU"], "SKU"].unique()]
    elif seed.startswith("BOM of "):
        kk = seed.replace("BOM of ", "")
        comps = df[(df["GROUP_SKU"] == kk) & (df["ROLE"] == "COMPONENT")]
        rows = [{"SKU": r.SKU, "QTY": r.QTY_PER, "DISC_PCT": 0.0} for r in comps.itertuples()]
    else:
        rows = []
    st.session_state.bb_rows = pd.DataFrame(rows, columns=BB_COLS)
    st.session_state.bb_ver = st.session_state.get("bb_ver", 0) + 1
if "bb_rows" not in st.session_state:
    st.session_state.bb_rows = pd.DataFrame({"SKU": pd.Series(dtype="str"), "QTY": pd.Series(dtype="float"),
                                             "DISC_PCT": pd.Series(dtype="float")})
bb = st.data_editor(
    st.session_state.bb_rows, num_rows="dynamic", hide_index=True, use_container_width=True,
    key=f"bb_editor_{st.session_state.get('bb_ver', 0)}",
    column_config={
        "SKU": st.column_config.TextColumn("SKU", required=True),
        "QTY": st.column_config.NumberColumn("Qty per bundle", min_value=0.0, step=1.0, default=1.0, format="%.0f"),
        "DISC_PCT": st.column_config.NumberColumn("Discount off trade %", min_value=0.0, max_value=90.0,
                                                  step=0.5, default=0.0, format="%.1f%%"),
    },
)
o1, o2, o3 = st.columns(3)
nb = o1.number_input("Number of bundles", min_value=1, value=10, step=1,
                     help="Expected bundles per year, or for one deal. Used for totals and order-size history.")
tgt_price = o2.number_input("Target bundle price, AUD (optional)", min_value=0.0, value=0.0, step=10.0,
                            help="0 = use the line discounts. Above 0 = one even discount across all lines to hit this price.")
cannib = o3.slider("Bundle buyers who would have bought the parts anyway", 0, 100, 50, format="%d%%",
                   help="Your estimate. These customers would have paid our normal (ASP) price for the parts, "
                        "so the bundle discount on them is GP given away.")

bbc = bb.copy()
bbc["SKU"] = bbc["SKU"].astype("string").str.strip().str.upper()
bbc = bbc[bbc["SKU"].notna() & (bbc["SKU"] != "")]
bbc["QTY"] = bbc["QTY"].fillna(1.0)
bbc["DISC_PCT"] = bbc["DISC_PCT"].fillna(0.0)
bundle_lines, bundle_tot, price_tbl, gp_tbl = pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
if bbc.empty:
    st.info("Add SKUs to the builder, or load a starting point above.")
else:
    key_ = tuple(sorted(set(bbc["SKU"])))
    if st.session_state.get("bb_key") != key_:
        lst = ",".join(key_)
        with st.spinner("Fetching bundle SKUs"):
            st.session_state.bb_items = q("F_PRICE_CHECK", lst, "SKU")
            st.session_state.bb_deal = q("F_PRICE_CHECK_DEALSIZE", lst, "SKU, BAND_ORDER")
        st.session_state.bb_key = key_
    items = st.session_state.bb_items
    items = items[items["GROUP_SKU"] == items["SKU"]].drop_duplicates("SKU")
    L = bbc.merge(items[["SKU", "DESCRIPTION", "TRADE_PRICE_EX", "ASP_12M", "DECISION_COST", "UNITS_12M"]],
                  on="SKU", how="left")
    bad = L[L["TRADE_PRICE_EX"].isna() | L["DECISION_COST"].isna()]
    if not bad.empty:
        st.warning("Left out (SKU not found, or no trade price or cost): " + ", ".join(bad["SKU"]))
    L = L[L["TRADE_PRICE_EX"].notna() & L["DECISION_COST"].notna()].copy()
    if not L.empty:
        L["NORMAL_PRICE"] = L["ASP_12M"].fillna(L["TRADE_PRICE_EX"])
        trade_tot = (L["QTY"] * L["TRADE_PRICE_EX"]).sum()
        if tgt_price > 0 and trade_tot > 0:
            L["DISC_PCT"] = (1 - tgt_price / trade_tot) * 100
        L["BUNDLE_PRICE"] = L["TRADE_PRICE_EX"] * (1 - L["DISC_PCT"] / 100)
        L["GP_TRADE"] = (1 - L["DECISION_COST"] / L["TRADE_PRICE_EX"]) * 100
        L["GP_NORMAL"] = (1 - L["DECISION_COST"] / L["NORMAL_PRICE"]) * 100
        L["GP_BUNDLE"] = (1 - L["DECISION_COST"] / L["BUNDLE_PRICE"]) * 100
        L["VS_NORMAL"] = (L["BUNDLE_PRICE"] / L["NORMAL_PRICE"] - 1) * 100
        L["ORDER_QTY"] = L["QTY"] * nb
        L["PAST_ASP_SIZE"] = [hist_band_asp(st.session_state.bb_deal, s_, q_) for s_, q_ in zip(L["SKU"], L["ORDER_QTY"])]
        L["FLOOR_T"] = L["DECISION_COST"] / (1 - t)
        L["LINE_TRADE"] = L["QTY"] * L["TRADE_PRICE_EX"]
        L["LINE_BUNDLE"] = L["QTY"] * L["BUNDLE_PRICE"]
        L["LINE_GP_NORMAL"] = L["QTY"] * (L["NORMAL_PRICE"] - L["DECISION_COST"])
        L["LINE_GP_BUNDLE"] = L["QTY"] * (L["BUNDLE_PRICE"] - L["DECISION_COST"])
        L["Status"] = "OK"
        L.loc[L["BUNDLE_PRICE"] < L["FLOOR_T"], "Status"] = f"Below {target_gp:g}% floor"
        L.loc[L["BUNDLE_PRICE"] <= L["DECISION_COST"], "Status"] = "At or below cost"
        L.loc[L["ASP_12M"].isna(), "Status"] = L.loc[L["ASP_12M"].isna(), "Status"] + " (no 12M sales: normal = trade)"
        bundle_lines = L[["SKU", "DESCRIPTION", "QTY", "TRADE_PRICE_EX", "NORMAL_PRICE", "DECISION_COST",
                          "GP_TRADE", "GP_NORMAL", "DISC_PCT", "BUNDLE_PRICE", "GP_BUNDLE", "VS_NORMAL",
                          "ORDER_QTY", "PAST_ASP_SIZE", "LINE_TRADE", "LINE_BUNDLE", "LINE_GP_NORMAL",
                          "LINE_GP_BUNDLE", "Status"]]
        # ---- bundle totals ----
        cost_b = (L["QTY"] * L["DECISION_COST"]).sum()
        rev_t, rev_n, rev_b = trade_tot, (L["QTY"] * L["NORMAL_PRICE"]).sum(), (L["QTY"] * L["BUNDLE_PRICE"]).sum()
        g_t, g_n, g_b = rev_t - cost_b, rev_n - cost_b, rev_b - cost_b
        price_at_target = cost_b / (1 - t)

        def tline(name, rev, gp):
            return {"Case": name, "Price per bundle": rev, "Cost per bundle": cost_b, "GP $ per bundle": gp,
                    "Blended GP %": gp / rev * 100 if rev else None,
                    "Disc off trade": (1 - rev / rev_t) * 100 if rev_t else None,
                    f"GP $ for {nb} bundles": gp * nb, f"Revenue for {nb} bundles": rev * nb}

        bundle_tot = pd.DataFrame([tline("Parts at trade (list)", rev_t, g_t),
                                   tline("Parts at actual invoiced price (12M ASP)", rev_n, g_n),
                                   tline("Proposed bundle price", rev_b, g_b)])

        # ---- headline: the decision ----
        h1, h2, h3, h4 = st.columns(4)
        h1.metric("Proposed bundle price", f"${rev_b:,.2f}")
        h2.metric("vs standard trade", f"{'-' if rev_b < rev_t else '+'}${abs(rev_b - rev_t):,.2f}", delta=f"{(rev_b / rev_t - 1) * 100:+.1f}%",
                  delta_color="normal")
        h3.metric("vs actual invoiced", f"{'-' if rev_b < rev_n else '+'}${abs(rev_b - rev_n):,.2f}", delta=f"{(rev_b / rev_n - 1) * 100:+.1f}%",
                  delta_color="normal")
        h4.metric("Blended GP proposed", f"{g_b / rev_b * 100:.1f}%" if rev_b else "n/a",
                  delta=f"{(g_b / rev_b - g_n / rev_n) * 100:+.1f} pts vs invoiced" if rev_b else None,
                  delta_color="normal")

        # ---- comparison tables: each SKU (qty applied) plus blended bundle row ----
        cmp = pd.DataFrame({
            "SKU": L["SKU"] + L["ASP_12M"].isna().map({True: " *", False: ""}),
            "Qty": L["QTY"],
            "Trade": L["QTY"] * L["TRADE_PRICE_EX"],
            "Invoiced": L["QTY"] * L["NORMAL_PRICE"],
            "Proposed": L["QTY"] * L["BUNDLE_PRICE"],
            "Cost": L["QTY"] * L["DECISION_COST"],
        })
        tot = cmp[["Trade", "Invoiced", "Proposed", "Cost"]].sum()
        cmp = pd.concat([cmp, pd.DataFrame([{"SKU": "BUNDLE (blended)", "Qty": float("nan"), **tot.to_dict()}])],
                        ignore_index=True)
        for k in ["Trade", "Invoiced", "Proposed"]:
            cmp[f"GP $ {k}"] = cmp[k] - cmp["Cost"]
            cmp[f"GP % {k}"] = cmp[f"GP $ {k}"] / cmp[k] * 100

        price_tbl = pd.DataFrame({
            "SKU": cmp["SKU"], "Qty": cmp["Qty"],
            "Standard trade": cmp["Trade"], "Actual invoiced": cmp["Invoiced"], "Proposed": cmp["Proposed"],
            "Proposed vs trade $": cmp["Proposed"] - cmp["Trade"],
            "Proposed vs trade %": (cmp["Proposed"] / cmp["Trade"] - 1) * 100,
            "Proposed vs invoiced $": cmp["Proposed"] - cmp["Invoiced"],
            "Proposed vs invoiced %": (cmp["Proposed"] / cmp["Invoiced"] - 1) * 100,
        })
        gp_tbl = pd.DataFrame({
            "SKU": cmp["SKU"],
            "GP $ at trade": cmp["GP $ Trade"], "GP $ invoiced": cmp["GP $ Invoiced"],
            "GP $ proposed": cmp["GP $ Proposed"],
            "GP $ proposed vs invoiced": cmp["GP $ Proposed"] - cmp["GP $ Invoiced"],
            "GP % at trade": cmp["GP % Trade"], "GP % invoiced": cmp["GP % Invoiced"],
            "GP % proposed": cmp["GP % Proposed"],
            "GP pts vs trade": cmp["GP % Proposed"] - cmp["GP % Trade"],
            "GP pts vs invoiced": cmp["GP % Proposed"] - cmp["GP % Invoiced"],
        })

        def neg_red(v):
            return "color: #e53935" if isinstance(v, (int, float)) and pd.notna(v) and v < 0 else ""

        def bold_total(row):
            return ["font-weight: 700" if str(row["SKU"]).startswith("BUNDLE") else "" for _ in row]

        def show(tbl, money, pctc, ptsc, signed):
            f = {c: "${:,.2f}" for c in money}
            f.update({c: "{:+,.2f}" for c in signed})
            f.update({c: "{:+.1f}%" for c in pctc if "vs" in c})
            f.update({c: "{:.1f}%" for c in pctc if "vs" not in c})
            f.update({c: "{:+.1f}" for c in ptsc})
            if "Qty" in tbl:
                f["Qty"] = "{:.0f}"
            sty = tbl.style.format(f, na_rep="")
            red = [c for c in signed + ptsc + pctc if "vs" in c]
            sty = (sty.map(neg_red, subset=red) if hasattr(sty, "map") else sty.applymap(neg_red, subset=red))
            sty = sty.apply(bold_total, axis=1)
            st.dataframe(sty, hide_index=True, use_container_width=True)

        st.markdown("**Price: standard trade vs actual invoiced vs proposed** (line value = qty x unit price)")
        show(price_tbl, ["Standard trade", "Actual invoiced", "Proposed"],
             ["Proposed vs trade %", "Proposed vs invoiced %"], [],
             ["Proposed vs trade $", "Proposed vs invoiced $"])
        st.markdown("**Gross profit: at trade vs actual invoiced vs proposed**")
        show(gp_tbl, ["GP $ at trade", "GP $ invoiced", "GP $ proposed"],
             ["GP % at trade", "GP % invoiced", "GP % proposed"], ["GP pts vs trade", "GP pts vs invoiced"],
             ["GP $ proposed vs invoiced"])
        st.caption("Actual invoiced = what we really sold each SKU for over the last 12 months (ASP), so the "
                   "'vs invoiced' columns show the real change from today; 'vs trade' shows the change on paper. "
                   "Cost = today's decision cost in all three cases, so GP differences come from price only. "
                   "Red = lower than the comparison. * = no sales in 12 months, invoiced shown at trade. "
                   f"Bundle price that gives {target_gp:g}% GP: ${price_at_target:,.2f}. "
                   f"For {nb} bundles: revenue ${rev_b * nb:,.0f}, GP ${g_b * nb:,.0f} "
                   f"({(g_b - g_n) * nb:+,.0f} vs selling the same parts at invoiced price).")

        with st.expander("Line detail: floors, order-size history, status"):
            st.dataframe(bundle_lines, hide_index=True, use_container_width=True, column_config={
                "DESCRIPTION": "Description", "QTY": st.column_config.NumberColumn("Qty", format="%.0f"),
                "TRADE_PRICE_EX": st.column_config.NumberColumn("Trade", format=MONEY),
                "NORMAL_PRICE": st.column_config.NumberColumn("Invoiced (12M ASP)", format=MONEY),
                "DECISION_COST": st.column_config.NumberColumn("Cost", format=MONEY),
                "GP_TRADE": st.column_config.NumberColumn("GP at trade", format="%.1f%%"),
                "GP_NORMAL": st.column_config.NumberColumn("GP invoiced", format="%.1f%%"),
                "DISC_PCT": st.column_config.NumberColumn("Disc off trade", format="%.1f%%"),
                "BUNDLE_PRICE": st.column_config.NumberColumn("Proposed unit price", format=MONEY),
                "GP_BUNDLE": st.column_config.NumberColumn("GP proposed", format="%.1f%%"),
                "VS_NORMAL": st.column_config.NumberColumn("vs invoiced", format="%+.1f%%"),
                "ORDER_QTY": st.column_config.NumberColumn("Units across all bundles", format="%.0f"),
                "PAST_ASP_SIZE": st.column_config.NumberColumn("Past ASP, orders that size", format=MONEY),
                "LINE_TRADE": st.column_config.NumberColumn("Line at trade", format=MONEY),
                "LINE_BUNDLE": st.column_config.NumberColumn("Line proposed", format=MONEY),
                "LINE_GP_NORMAL": st.column_config.NumberColumn("Line GP $ invoiced", format=MONEY),
                "LINE_GP_BUNDLE": st.column_config.NumberColumn("Line GP $ proposed", format=MONEY),
            })
            st.caption("Past ASP for orders that size = what orders of the same total units paid in the last "
                       "24 months. If the proposed price is well below it, the bundle gives more away than bulk "
                       "buyers already get.")
            st.dataframe(bundle_tot, hide_index=True, use_container_width=True, column_config={
                **{c: st.column_config.NumberColumn(format=MONEY) for c in
                   ["Price per bundle", "Cost per bundle", "GP $ per bundle"]},
                **{c: st.column_config.NumberColumn(format="$%.0f") for c in
                   [f"GP $ for {nb} bundles", f"Revenue for {nb} bundles"]},
                "Blended GP %": st.column_config.NumberColumn(format="%.1f%%"),
                "Disc off trade": st.column_config.NumberColumn(format="%.1f%%"),
            })

        # ---- cannibalisation ----
        st.markdown("**Does the bundle make money overall?**")
        c_ = cannib / 100
        new_gp = nb * (1 - c_) * g_b
        given = nb * c_ * (g_n - g_b)
        net = new_gp - given
        no_limit = g_b >= g_n
        max_c = 100.0 if no_limit else (g_b / g_n * 100 if g_n > 0 and g_b > 0 else 0.0)
        n1, n2, n3, n4 = st.columns(4)
        n1.metric("GP $ from new business", f"${new_gp:,.0f}", help=f"{nb * (1 - c_):.0f} bundles that would not have happened")
        n2.metric("GP $ given away to existing buyers", f"${given:,.0f}",
                  help=f"{nb * c_:.0f} bundles bought by customers who would have paid normal price for the parts")
        n3.metric("Net GP $ effect", f"${net:,.0f}", delta="gain" if net >= 0 else "loss",
                  delta_color="normal" if net >= 0 else "inverse")
        n4.metric("Max share of existing buyers to break even", "No limit" if no_limit else f"{max_c:.0f}%",
                  help="If more than this share of bundle buyers would have bought the parts anyway, the bundle loses GP $.")
        st.caption(f"GP per bundle at normal price ${g_n:,.2f}, at bundle price ${g_b:,.2f}. Each bundle sold to a "
                   f"customer who would have bought anyway costs ${g_n - g_b:,.2f}. Each genuinely new bundle earns "
                   f"${g_b:,.2f}. " + ("The bundle is priced at or above what customers pay for the parts today, so existing "
                   "buyers cost nothing, but expect few takers." if no_limit else f"So at most {max_c:.0f}% of bundle "
                   "buyers can be existing demand before the bundle loses money.") + " Our cost does not change with quantity (no vendor volume pricing; rebates excluded).")

# ===== 5. Detail and export =================================================
with st.expander("Full detail (all columns)"):
    st.dataframe(df, hide_index=True, use_container_width=True)

stamp = f"{pd.Timestamp.now(tz='Australia/Sydney'):%Y%m%d_%H%M}"
sheets = {"Summary": view, "Kit vs components": kv, "Trend": trend, "Family": fam,
          "What-if": res, "Blended": blend_tbl, "This deal": deal_tbl, "Bundle lines": bundle_lines, "Bundle totals": bundle_tot, "Bundle price compare": price_tbl, "Bundle GP compare": gp_tbl, "Monthly": mon, "Order size": deal, "FY half": hist, "Detail": df}
try:
    import openpyxl  # noqa: F401
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, frame in sheets.items():
            if frame is not None and not frame.empty:
                frame.to_excel(xw, sheet_name=name, index=False)
    st.download_button("Download Excel", data=buf.getvalue(), file_name=f"price_check_{stamp}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
except ImportError:
    cols = st.columns(3)
    for i, (name, frame) in enumerate([("summary", view), ("trend", trend), ("whatif", res)]):
        if frame is not None and not frame.empty:
            cols[i].download_button(f"Download {name} (CSV)", frame.to_csv(index=False),
                                    file_name=f"price_check_{name}_{stamp}.csv", mime="text/csv")
st.caption("Open issues: credits not matched to original invoices (ETC most affected). "
           "No vendor price book yet, so floors use BC average cost. Stock-outs are not yet overlaid on the trend.")
