#!/usr/bin/env python3
"""
Výpočet daně z kapitálových zisků a dividend z Degiro Account Statement CSV.

Použití:
    python dane_degiro.py "Degiro výpis.csv" 2025

Podporuje:
    - FIFO párování nákupů a prodejů (§10 ZDP)
    - Dividendy a zápočet zahraniční daně (§8 ZDP)
    - Jednotný kurz ČNB (automatický výpočet z denních kurzů)
    - Časový test 3 roky (osvobození od daně)
    - Stock splity, mergery, delistingy, spin-offy, rights issues
    - Více měn (CZK, EUR, USD, HKD, CAD, ...)
"""

from __future__ import annotations

import argparse
import copy
import csv
import re
import sys
from calendar import monthrange
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, InvalidOperation
from typing import Dict, List, Optional, Tuple
from urllib.request import urlopen


# ---------------------------------------------------------------------------
# Fee type: list of (amount, currency) tuples
# ---------------------------------------------------------------------------

Fee = List[Tuple[Decimal, str]]


def fee_zero():
    # type: () -> Fee
    return []


def fee_to_czk(fees, rates):
    # type: (Fee, Dict[str, Decimal]) -> Decimal
    total = Decimal("0")
    for amt, ccy in fees:
        total += amt * rates.get(ccy, Decimal("1"))
    return total


def fee_scale(fees, factor):
    # type: (Fee, Decimal) -> Fee
    return [(amt * factor, ccy) for amt, ccy in fees]


def fee_add(a, b):
    # type: (Fee, Fee) -> Fee
    return a + b


def fee_display(fees):
    # type: (Fee) -> str
    by_ccy = defaultdict(Decimal)
    for amt, ccy in fees:
        by_ccy[ccy] += amt
    parts = ["{} {}".format(v.quantize(Decimal("0.01")), k)
             for k, v in sorted(by_ccy.items()) if v != 0]
    return " + ".join(parts) if parts else "0"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class RawRow:
    line: int
    tx_date: date
    time: str
    value_date: date
    product: str
    isin: str
    description: str
    fx_rate: Optional[Decimal]
    mov_ccy: str
    mov_amt: Decimal
    bal_ccy: str
    bal_amt: Decimal
    order_id: str


@dataclass
class BuyLot:
    isin: str
    product: str
    buy_date: date
    quantity: Decimal
    price_per_unit: Decimal
    trade_ccy: str
    fees: Fee
    order_id: str


@dataclass
class SellEvent:
    isin: str
    product: str
    sell_date: date
    quantity: Decimal
    price_per_unit: Decimal
    total_proceeds: Decimal
    trade_ccy: str
    fees: Fee
    order_id: str
    source: str  # trade / delisting / merger / split_zero


@dataclass
class MatchedPortion:
    buy_date: date
    qty: Decimal
    cost_per_unit: Decimal
    cost_ccy: str
    fees: Fee


@dataclass
class Disposition:
    product: str
    isin: str
    sell_date: date
    sell_qty: Decimal
    proceeds: Decimal
    ccy: str
    sell_fees: Fee
    portions: List[MatchedPortion]
    source: str
    # computed:
    buy_cost: Decimal = Decimal("0")
    buy_fees: Fee = field(default_factory=fee_zero)
    proceeds_czk: Decimal = Decimal("0")
    cost_czk: Decimal = Decimal("0")
    fees_czk: Decimal = Decimal("0")
    gain_czk: Decimal = Decimal("0")
    exempt: bool = False
    partial_exempt_qty: Decimal = Decimal("0")


@dataclass
class DividendEvent:
    """One dividend payment (gross + tax paired by ISIN + value_date)."""
    product: str
    isin: str
    country: str  # source country for SZDZ
    value_date: date
    gross: Decimal
    tax_withheld: Decimal  # negative = tax paid, positive = storno
    ccy: str
    has_treaty: bool = True  # SZDZ exists with this country
    # computed:
    gross_czk: Decimal = Decimal("0")
    tax_czk: Decimal = Decimal("0")
    cz_tax_czk: Decimal = Decimal("0")  # Czech 15% on gross
    credit_czk: Decimal = Decimal("0")  # recognized credit (0 if no treaty)
    expense_czk: Decimal = Decimal("0")  # deductible expense (for non-treaty)


# Source country for SZDZ (double-taxation treaty) purposes.
# For ADRs/GDRs the ISIN is US but the dividend source is the actual company country.
# Override by ISIN where ISIN prefix is misleading.
ISIN_COUNTRY_OVERRIDE = {
    # ADRs on non-US companies (US ISIN but foreign source)
    "US01609W1027": "KY",   # Alibaba (Cayman Islands)
    "US8356993076": "JP",   # Sony Group Corp
    "US8740391003": "TW",   # Taiwan Semiconductor (TSMC)
    # NL-incorporated but Degiro classifies differently
    "NL0009434992": "GB",   # LyondellBasell Industries
}

COUNTRY_NAMES = {
    "US": "USA", "CZ": "Cesko", "FR": "Francie", "NL": "Nizozemsko",
    "IE": "Irsko", "CN": "Cina", "HK": "Hongkong", "CA": "Kanada",
    "DE": "Nemecko", "GB": "Britanie", "LU": "Lucembursko",
    "NO": "Norsko", "JP": "Japonsko", "KY": "Kajmanske ostrovy",
    "TW": "Tchaj-wan",
}

# Countries with active SZDZ (double-taxation treaty) with Czech Republic.
# For countries WITH treaty: credit method (zapocet) per §38f odst. 1 ZDP.
# For countries WITHOUT treaty: no credit, but foreign tax can be deducted
# as expense per §24 odst. 2 pism. ch) ZDP.
COUNTRIES_WITH_SZDZ = {
    "US", "FR", "NL", "IE", "CN", "HK", "CA", "DE", "GB", "LU",
    "NO", "JP", "AT", "BE", "CH", "DK", "ES", "FI", "GR", "HR",
    "HU", "IN", "IT", "KR", "MX", "PL", "PT", "RO", "RU", "SE",
    "SG", "SK", "SI", "TR", "ZA", "IL", "AU", "NZ", "BG", "CY",
    "EE", "LT", "LV", "MT", "IS",
    # NOT included: KY (Cayman Islands), TW (Taiwan), PA, etc.
}

# Maximum dividend withholding rate per treaty (SZDZ) for portfolio investors.
# Used to cap the creditable foreign tax: credit = min(paid, treaty_max, CZ 15%).
# Source: individual bilateral treaties, Article 10 (Dividends).
# Rates for NON-substantial holdings (typically < 25% ownership).
TREATY_MAX_DIV_RATE = {
    "US": Decimal("0.15"),   # 15% (with W-8BEN)
    "FR": Decimal("0.10"),   # 10%
    "NL": Decimal("0.10"),   # 10%
    "IE": Decimal("0.15"),   # 15%
    "CN": Decimal("0.10"),   # 10%
    "HK": Decimal("0.05"),   # 5% (HK practically 0%)
    "CA": Decimal("0.15"),   # 15%
    "DE": Decimal("0.15"),   # 15%
    "GB": Decimal("0.15"),   # 15%
    "LU": Decimal("0.15"),   # 15%
    "NO": Decimal("0.15"),   # 15%
    "JP": Decimal("0.10"),   # 10%
    "AT": Decimal("0.10"),   # 10%
    "CH": Decimal("0.15"),   # 15%
    "SE": Decimal("0.10"),   # 10%
    "DK": Decimal("0.15"),   # 15%
    "FI": Decimal("0.05"),   # 5%
    "ES": Decimal("0.15"),   # 15%
    "IT": Decimal("0.15"),   # 15%
    "PL": Decimal("0.05"),   # 5%
    "SK": Decimal("0.05"),   # 5%
    "KR": Decimal("0.05"),   # 5%
    "IN": Decimal("0.10"),   # 10%
    "AU": Decimal("0.15"),   # 15%
    "IL": Decimal("0.05"),   # 5%
    "SG": Decimal("0.05"),   # 5%
    # Default for unlisted treaty countries: 15% (conservative)
}


def resolve_country(isin, product):
    # type: (str, str) -> str
    """Determine the source country for dividend taxation.
    Uses ISIN overrides first, then falls back to ISIN prefix."""
    if isin in ISIN_COUNTRY_OVERRIDE:
        return ISIN_COUNTRY_OVERRIDE[isin]
    return isin[:2] if len(isin) >= 2 else "??"


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_dec(s):
    # type: (str) -> Decimal
    """European number -> Decimal:  '1 234,56' -> Decimal('1234.56')."""
    s = s.strip().strip('"')
    if not s:
        return Decimal("0")
    s = s.replace("\xa0", "").replace(" ", "").replace(".", "").replace(",", ".")
    return Decimal(s)


def parse_date(s):
    # type: (str) -> date
    d, m, y = s.strip().split("-")
    return date(int(y), int(m), int(d))


TRADE_RE = re.compile(
    r"(Nákup|Prodej)\s+(\d+)\s+(.+?)@([\d ,\xa0]+)\s+(\w+)\s+\(([A-Z0-9]+)\)"
)


def parse_trade(desc):
    # type: (str) -> Optional[dict]
    m = TRADE_RE.search(desc)
    if not m:
        return None
    return {
        "action": m.group(1),
        "qty": int(m.group(2)),
        "name": m.group(3).strip(),
        "price": parse_dec(m.group(4)),
        "ccy": m.group(5),
        "isin": m.group(6),
    }


# ---------------------------------------------------------------------------
# CSV reading
# ---------------------------------------------------------------------------

def read_csv(path):
    # type: (str) -> List[RawRow]
    rows = []
    with open(path, encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader)  # header
        for ln, cols in enumerate(reader, 2):
            if len(cols) < 12 or not cols[0].strip():
                continue
            try:
                rows.append(RawRow(
                    line=ln,
                    tx_date=parse_date(cols[0]),
                    time=cols[1].strip(),
                    value_date=parse_date(cols[2]),
                    product=cols[3].strip(),
                    isin=cols[4].strip(),
                    description=cols[5].strip().replace("\xa0", " "),
                    fx_rate=parse_dec(cols[6]) if cols[6].strip() else None,
                    mov_ccy=cols[7].strip(),
                    mov_amt=parse_dec(cols[8]) if cols[8].strip() else Decimal("0"),
                    bal_ccy=cols[9].strip(),
                    bal_amt=parse_dec(cols[10]) if cols[10].strip() else Decimal("0"),
                    order_id=cols[11].strip() if len(cols) > 11 else "",
                ))
            except Exception as e:
                print("  WARN radek {}: {}".format(ln, e), file=sys.stderr)
    return rows


# ---------------------------------------------------------------------------
# Row classification
# ---------------------------------------------------------------------------

def classify(row):
    # type: (RawRow) -> str
    d = row.description
    if d.startswith("Interní převod:"):
        return "skip"
    if d.startswith("Změna produktu:"):
        return "product_change"
    if d.startswith("Stock split:"):
        return "stock_split"
    if d.startswith("Merger:"):
        return "merger"
    if d.startswith("Delisting:"):
        return "delisting"
    if d.startswith("Spin off:"):
        return "spin_off"
    if d.startswith("Rights issue:"):
        return "rights_issue"
    if d == "Korporátní akce hotovostní vypořádání akcie":
        return "corp_action_cash"
    if d == "DEGIRO Transaction and/or third party fees":
        return "fee"
    if d == "Vratka kapitálu":
        return "return_of_capital"
    if d.startswith("Konverze Peněžního Fondu:"):
        return "skip"
    if d.startswith("FX vyučtování"):
        return "skip"
    if d in ("Dividenda", "Daň z dividendy", "Reinvestice dividendy"):
        return "skip"
    if d.startswith("Náklady akcie"):
        return "skip"
    if d.startswith("ADR/GDR"):
        return "skip"
    td = parse_trade(d)
    if td:
        return "buy" if td["action"] == "Nákup" else "sell"
    return "skip"


# ---------------------------------------------------------------------------
# ČNB unified exchange rate
# ---------------------------------------------------------------------------

_cnb_cache = {}  # type: Dict[date, Dict[str, Decimal]]


def fetch_cnb_rates(d):
    # type: (date) -> Dict[str, Decimal]
    """Fetch CNB daily rates. Returns {CCY: rate_per_1_unit_in_CZK}."""
    if d in _cnb_cache:
        return _cnb_cache[d]
    url = (
        "https://www.cnb.cz/cs/financni-trhy/devizovy-trh/"
        "kurzy-devizoveho-trhu/kurzy-devizoveho-trhu/"
        "denni_kurz.txt?date={:02d}.{:02d}.{}".format(d.day, d.month, d.year)
    )
    text = urlopen(url, timeout=30).read().decode("utf-8")
    rates = {}  # type: Dict[str, Decimal]
    for line in text.strip().split("\n")[2:]:
        parts = line.split("|")
        if len(parts) < 5:
            continue
        qty = Decimal(parts[2])
        code = parts[3]
        rate = parse_dec(parts[4])
        rates[code] = rate / qty
    _cnb_cache[d] = rates
    return rates


def calc_unified_rates(year):
    # type: (int) -> Dict[str, Decimal]
    """Calculate CNB unified rate = avg of last-day-of-month rates."""
    monthly = []  # type: List[Dict[str, Decimal]]
    for month in range(1, 13):
        last = date(year, month, monthrange(year, month)[1])
        print("  {}...".format(last.strftime("%d.%m.%Y")), end="", flush=True)
        rates = fetch_cnb_rates(last)
        print(" OK ({} men)".format(len(rates)))
        monthly.append(rates)

    all_ccy = set()  # type: set
    for r in monthly:
        all_ccy.update(r.keys())

    unified = {"CZK": Decimal("1")}  # type: Dict[str, Decimal]
    for ccy in sorted(all_ccy):
        vals = [r[ccy] for r in monthly if ccy in r]
        if vals:
            unified[ccy] = sum(vals) / len(vals)
    return unified


# ---------------------------------------------------------------------------
# Portfolio / lot tracking
# ---------------------------------------------------------------------------

class Portfolio:
    def __init__(self):
        self.lots = defaultdict(list)  # type: Dict[str, List[BuyLot]]
        self.sells = []  # type: List[SellEvent]
        self.warnings = []  # type: List[str]

    def _add_lot(self, isin, product, buy_date, qty, price, ccy, fees, order_id):
        # type: (str, str, date, int, Decimal, str, Fee, str) -> None
        if qty <= 0:
            return
        self.lots[isin].append(BuyLot(
            isin=isin, product=product, buy_date=buy_date,
            quantity=Decimal(qty), price_per_unit=price,
            trade_ccy=ccy, fees=fees, order_id=order_id,
        ))

    def _add_sell(self, isin, product, sell_date, qty, price, proceeds,
                  ccy, fees, order_id, source="trade"):
        # type: (str, str, date, int, Decimal, Decimal, str, Fee, str, str) -> None
        self.sells.append(SellEvent(
            isin=isin, product=product, sell_date=sell_date,
            quantity=Decimal(qty), price_per_unit=price,
            total_proceeds=proceeds, trade_ccy=ccy,
            fees=fees, order_id=order_id, source=source,
        ))

    def _consume_lots(self, isin, qty):
        # type: (str, int) -> List[BuyLot]
        """Remove qty shares from isin (FIFO). Returns consumed lot snapshots."""
        lots = self.lots[isin]
        remaining = Decimal(qty)
        consumed = []  # type: List[BuyLot]
        while remaining > 0 and lots:
            lot = lots[0]
            if lot.quantity <= remaining:
                consumed.append(lots.pop(0))
                remaining -= lot.quantity
            else:
                snap = copy.deepcopy(lot)
                frac = remaining / lot.quantity
                snap.quantity = remaining
                snap.fees = fee_scale(lot.fees, frac)
                lot.quantity -= remaining
                lot.fees = fee_scale(lot.fees, Decimal("1") - frac)
                consumed.append(snap)
                remaining = Decimal("0")
        if remaining > 0:
            self.warnings.append(
                "Chybi {} ks pro {} pri consume_lots".format(remaining, isin)
            )
        return consumed

    # -- event handlers --

    def handle_stock_split(self, sell_td, buy_td, split_date, sell_product):
        # type: (dict, dict, date, str) -> None
        old_isin = sell_td["isin"]
        new_isin = buy_td["isin"]
        old_qty = sell_td["qty"]
        new_qty = buy_td["qty"]

        if new_qty == 0:
            # Reverse split to 0 shares -> total loss
            lots = self.lots.get(old_isin, [])
            total_held = sum(l.quantity for l in lots)
            qty_to_sell = min(Decimal(old_qty), total_held)
            ccy = lots[0].trade_ccy if lots else sell_td["ccy"]
            if qty_to_sell > 0:
                self.sells.append(SellEvent(
                    isin=old_isin, product=sell_product,
                    sell_date=split_date,
                    quantity=qty_to_sell,
                    price_per_unit=Decimal("0"),
                    total_proceeds=Decimal("0"),
                    trade_ccy=ccy, fees=fee_zero(),
                    order_id="", source="split_zero",
                ))
            return

        if old_isin == new_isin:
            ratio = Decimal(new_qty) / Decimal(old_qty)
            for lot in self.lots.get(old_isin, []):
                lot.quantity = (lot.quantity * ratio).to_integral_value(
                    rounding=ROUND_DOWN)
                lot.price_per_unit = lot.price_per_unit / ratio
        else:
            consumed = self._consume_lots(old_isin, old_qty)
            if not consumed:
                return
            assigned = Decimal("0")
            for i, c in enumerate(consumed):
                if i == len(consumed) - 1:
                    new_lot_qty = Decimal(new_qty) - assigned
                else:
                    new_lot_qty = (
                        c.quantity * Decimal(new_qty) / Decimal(old_qty)
                    ).to_integral_value()
                assigned += new_lot_qty
                if new_lot_qty <= 0:
                    continue
                cost_share = c.price_per_unit * c.quantity
                self.lots[new_isin].append(BuyLot(
                    isin=new_isin, product=c.product,
                    buy_date=c.buy_date,
                    quantity=new_lot_qty,
                    price_per_unit=cost_share / new_lot_qty,
                    trade_ccy=c.trade_ccy,
                    fees=c.fees, order_id=c.order_id,
                ))

    def handle_product_change(self, sell_td, buy_td, new_product):
        # type: (dict, dict, str) -> None
        old_isin = sell_td["isin"]
        new_isin = buy_td["isin"]
        for lot in self.lots.get(old_isin, []):
            lot.product = new_product
            if new_isin != old_isin:
                lot.isin = new_isin
        if new_isin != old_isin and old_isin in self.lots:
            self.lots[new_isin].extend(self.lots.pop(old_isin))

    def handle_merger(self, sell_td, buy_td, sell_date, sell_product,
                      total_proceeds):
        # type: (dict, Optional[dict], date, str, Decimal) -> None
        old_isin = sell_td["isin"]
        old_qty = sell_td["qty"]
        price = total_proceeds / Decimal(old_qty) if old_qty else Decimal("0")
        self._add_sell(old_isin, sell_product, sell_date,
                       old_qty, price, total_proceeds,
                       sell_td["ccy"], fee_zero(), "", "merger")
        if buy_td and buy_td["qty"] > 0:
            new_cost = total_proceeds / Decimal(buy_td["qty"])
            self._add_lot(buy_td["isin"], buy_td["name"], sell_date,
                          buy_td["qty"], new_cost, buy_td["ccy"],
                          fee_zero(), "")

    def handle_delisting(self, td, product, sell_date, cash_amount, cash_ccy):
        # type: (dict, str, date, Decimal, str) -> None
        qty = td["qty"]
        lots = self.lots.get(td["isin"], [])
        if cash_amount > 0 and cash_ccy:
            ccy = cash_ccy
        elif lots:
            ccy = lots[0].trade_ccy
        else:
            ccy = td["ccy"]
        price = cash_amount / Decimal(qty) if qty > 0 else Decimal("0")
        self._add_sell(td["isin"], product, sell_date,
                       qty, price, cash_amount,
                       ccy, fee_zero(), "", "delisting")

    def handle_return_of_capital(self, isin, amount):
        # type: (str, Decimal) -> None
        lots = self.lots.get(isin, [])
        total_qty = sum(l.quantity for l in lots)
        if total_qty == 0:
            return
        for lot in lots:
            reduction = amount / total_qty
            lot.price_per_unit = max(Decimal("0"), lot.price_per_unit - reduction)


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def process_all(rows):
    # type: (List[RawRow]) -> Portfolio
    """Process all rows chronologically and build portfolio."""
    pf = Portfolio()
    rows = list(reversed(rows))

    # Pre-build: fees per order_id as list of (amount, currency)
    fees_by_order = defaultdict(list)  # type: Dict[str, Fee]
    for r in rows:
        if classify(r) == "fee" and r.order_id:
            fees_by_order[r.order_id].append((abs(r.mov_amt), r.mov_ccy))

    # Pre-build: corporate action cash by (isin, date)
    corp_cash = {}  # type: Dict[Tuple[str, date], Tuple[Decimal, str]]
    for r in rows:
        if classify(r) == "corp_action_cash":
            corp_cash[(r.isin, r.tx_date)] = (abs(r.mov_amt), r.mov_ccy)

    # Pre-build: order groups
    order_rows = defaultdict(list)  # type: Dict[str, List[RawRow]]
    for r in rows:
        if r.order_id:
            order_rows[r.order_id].append(r)

    # Pre-build: pairs for stock_split, product_change, merger
    paired_types = {"stock_split", "product_change", "merger"}
    paired_events = {}  # type: Dict[int, RawRow]
    by_date_type = defaultdict(list)  # type: Dict[Tuple[date, str], List[RawRow]]
    for r in rows:
        c = classify(r)
        if c in paired_types:
            by_date_type[(r.tx_date, c)].append(r)
    for key, group in by_date_type.items():
        sells_list = []
        buys_list = []
        for r in group:
            td = parse_trade(r.description)
            if td and td["action"] == "Prodej":
                sells_list.append(r)
            elif td and td["action"] == "Nákup":
                buys_list.append(r)
        used_buys = set()  # type: set
        # First pass: match by ISIN (same-ISIN splits, product changes)
        for sr in sells_list:
            std = parse_trade(sr.description)
            if not std:
                continue
            for br in buys_list:
                if br.line not in used_buys:
                    btd = parse_trade(br.description)
                    if btd and std["isin"] == btd["isin"]:
                        paired_events[sr.line] = br
                        paired_events[br.line] = sr
                        used_buys.add(br.line)
                        break
        # Second pass: match remaining (ISIN-changing splits, mergers)
        for sr in sells_list:
            if sr.line in paired_events:
                continue
            for br in buys_list:
                if br.line not in used_buys:
                    if parse_trade(br.description):
                        paired_events[sr.line] = br
                        paired_events[br.line] = sr
                        used_buys.add(br.line)
                        break

    processed_lines = set()  # type: set
    processed_orders = set()  # type: set

    for row in rows:
        if row.line in processed_lines:
            continue

        cls = classify(row)

        if cls in ("skip", "fee", "corp_action_cash"):
            continue

        # -- Stock split --
        if cls == "stock_split":
            td = parse_trade(row.description)
            if not td:
                continue
            partner = paired_events.get(row.line)
            if not partner:
                continue
            ptd = parse_trade(partner.description)
            if not ptd:
                continue
            processed_lines.add(row.line)
            processed_lines.add(partner.line)
            if td["action"] == "Prodej":
                pf.handle_stock_split(td, ptd, row.tx_date, row.product)
            else:
                pf.handle_stock_split(ptd, td, row.tx_date, partner.product)

        # -- Product change --
        elif cls == "product_change":
            td = parse_trade(row.description)
            if not td:
                continue
            partner = paired_events.get(row.line)
            if not partner:
                continue
            ptd = parse_trade(partner.description)
            if not ptd:
                continue
            processed_lines.add(row.line)
            processed_lines.add(partner.line)
            if td["action"] == "Prodej":
                pf.handle_product_change(td, ptd, partner.product)
            else:
                pf.handle_product_change(ptd, td, row.product)

        # -- Merger --
        elif cls == "merger":
            td = parse_trade(row.description)
            if not td:
                continue
            partner = paired_events.get(row.line)
            ptd = parse_trade(partner.description) if partner else None
            processed_lines.add(row.line)
            if partner:
                processed_lines.add(partner.line)
            if td["action"] == "Prodej":
                pf.handle_merger(td, ptd, row.tx_date, row.product,
                                 abs(row.mov_amt))
            elif ptd and ptd["action"] == "Prodej":
                pf.handle_merger(ptd, td, row.tx_date, partner.product,
                                 abs(partner.mov_amt))

        # -- Delisting --
        elif cls == "delisting":
            td = parse_trade(row.description)
            if not td:
                continue
            processed_lines.add(row.line)
            cash_amt, cash_ccy = corp_cash.get(
                (row.isin, row.tx_date), (Decimal("0"), "")
            )
            pf.handle_delisting(td, row.product, row.tx_date,
                                cash_amt, cash_ccy)

        # -- Spin-off / Rights issue -> lot at cost 0 --
        elif cls in ("spin_off", "rights_issue"):
            td = parse_trade(row.description)
            if td and td["action"] == "Nákup" and td["qty"] > 0:
                processed_lines.add(row.line)
                pf._add_lot(td["isin"], row.product, row.tx_date,
                            td["qty"], Decimal("0"), td["ccy"],
                            fee_zero(), "")

        # -- Return of capital --
        elif cls == "return_of_capital":
            processed_lines.add(row.line)
            if row.mov_amt > 0:
                pf.handle_return_of_capital(row.isin, row.mov_amt)

        # -- Regular buy --
        elif cls == "buy":
            td = parse_trade(row.description)
            if not td:
                continue
            processed_lines.add(row.line)
            order_fees = fees_by_order.get(row.order_id, fee_zero()) if row.order_id else fee_zero()
            # Split fees among partial fills proportionally
            if row.order_id and order_fees:
                buy_rows_in_order = [
                    r for r in order_rows.get(row.order_id, [])
                    if classify(r) == "buy"
                ]
                total_order_qty = Decimal("0")
                for br in buy_rows_in_order:
                    btd = parse_trade(br.description)
                    if btd:
                        total_order_qty += btd["qty"]
                if total_order_qty > 0:
                    frac = Decimal(td["qty"]) / total_order_qty
                    my_fees = fee_scale(order_fees, frac)
                else:
                    my_fees = list(order_fees)
            else:
                my_fees = fee_zero()
            pf._add_lot(td["isin"], row.product, row.tx_date,
                        td["qty"], td["price"], td["ccy"],
                        my_fees, row.order_id)

        # -- Regular sell --
        elif cls == "sell":
            td = parse_trade(row.description)
            if not td:
                continue
            processed_lines.add(row.line)
            if row.order_id:
                if row.order_id in processed_orders:
                    continue
                processed_orders.add(row.order_id)
                sell_rows = [
                    r for r in order_rows.get(row.order_id, [])
                    if classify(r) == "sell"
                ]
                total_qty = 0
                total_proceeds = Decimal("0")
                for sr in sell_rows:
                    std = parse_trade(sr.description)
                    if std:
                        total_qty += std["qty"]
                        total_proceeds += abs(sr.mov_amt)
                    processed_lines.add(sr.line)
                order_fees = fees_by_order.get(row.order_id, fee_zero())
                price = total_proceeds / Decimal(total_qty) if total_qty else Decimal("0")
                pf._add_sell(td["isin"], row.product, row.tx_date,
                             total_qty, price, total_proceeds,
                             td["ccy"], list(order_fees), row.order_id)
            else:
                pf._add_sell(td["isin"], row.product, row.tx_date,
                             td["qty"], td["price"],
                             Decimal(td["qty"]) * td["price"],
                             td["ccy"], fee_zero(), "")

    return pf


# ---------------------------------------------------------------------------
# FIFO matching
# ---------------------------------------------------------------------------

def fifo_match(pf):
    # type: (Portfolio) -> List[Disposition]
    sells = sorted(pf.sells, key=lambda s: s.sell_date)
    disps = []  # type: List[Disposition]

    for sell in sells:
        lots = pf.lots.get(sell.isin, [])
        remaining = sell.quantity
        portions = []  # type: List[MatchedPortion]

        while remaining > 0 and lots:
            lot = lots[0]
            take = min(lot.quantity, remaining)
            if lot.quantity > 0:
                frac = take / lot.quantity
            else:
                frac = Decimal("1")
            portion_fees = fee_scale(lot.fees, frac)
            portions.append(MatchedPortion(
                buy_date=lot.buy_date,
                qty=take,
                cost_per_unit=lot.price_per_unit,
                cost_ccy=lot.trade_ccy,
                fees=portion_fees,
            ))
            if take == lot.quantity:
                lots.pop(0)
            else:
                remaining_frac = (lot.quantity - take) / lot.quantity
                lot.fees = fee_scale(lot.fees, remaining_frac)
                lot.quantity -= take
            remaining -= take

        if remaining > 0:
            pf.warnings.append(
                "FIFO: chybi {} ks pro prodej {} ({}) {}".format(
                    remaining, sell.product, sell.isin, sell.sell_date)
            )

        disps.append(Disposition(
            product=sell.product, isin=sell.isin,
            sell_date=sell.sell_date, sell_qty=sell.quantity,
            proceeds=sell.total_proceeds, ccy=sell.trade_ccy,
            sell_fees=sell.fees, portions=portions,
            source=sell.source,
        ))

    return disps


# ---------------------------------------------------------------------------
# Tax calculation
# ---------------------------------------------------------------------------

def passes_time_test(buy_date, sell_date):
    # type: (date, date) -> bool
    """True if held > 3 calendar years (Czech §4 odst. 1 písm. w ZDP)."""
    try:
        cutoff = buy_date.replace(year=buy_date.year + 3)
    except ValueError:
        # Feb 29 buy -> Feb 28 in non-leap year
        cutoff = date(buy_date.year + 3, buy_date.month, 28)
    return sell_date > cutoff


def calc_tax(disps, year, rates):
    # type: (List[Disposition], int, Dict[str, Decimal]) -> List[Disposition]
    result = []  # type: List[Disposition]
    for d in disps:
        if d.sell_date.year != year:
            continue
        r = rates.get(d.ccy, Decimal("1"))

        d.buy_cost = sum(p.cost_per_unit * p.qty for p in d.portions)
        d.buy_fees = []
        for p in d.portions:
            d.buy_fees.extend(p.fees)

        d.proceeds_czk = (d.proceeds * r).quantize(Decimal("0.01"))
        # Convert each portion's cost using its own currency rate
        d.cost_czk = sum(
            (p.cost_per_unit * p.qty * rates.get(p.cost_ccy, Decimal("1"))
             ).quantize(Decimal("0.01"))
            for p in d.portions
        )
        all_fees = fee_add(d.sell_fees, d.buy_fees)
        d.fees_czk = fee_to_czk(all_fees, rates).quantize(Decimal("0.01"))
        d.gain_czk = d.proceeds_czk - d.cost_czk - d.fees_czk

        # Time test per portion - split into exempt/taxable
        exempt_qty = Decimal("0")
        exempt_cost_czk = Decimal("0")
        exempt_buy_fees = fee_zero()  # type: Fee
        for p in d.portions:
            if passes_time_test(p.buy_date, d.sell_date):
                exempt_qty += p.qty
                exempt_cost_czk += (
                    p.cost_per_unit * p.qty
                    * rates.get(p.cost_ccy, Decimal("1"))
                ).quantize(Decimal("0.01"))
                exempt_buy_fees = fee_add(exempt_buy_fees, p.fees)

        d.partial_exempt_qty = exempt_qty
        d.exempt = exempt_qty == d.sell_qty

        if exempt_qty > 0 and exempt_qty < d.sell_qty:
            # Partial exemption: split proceeds/fees proportionally
            exempt_frac = exempt_qty / d.sell_qty
            d.exempt_proceeds_czk = (d.proceeds * r * exempt_frac).quantize(
                Decimal("0.01"))
            d.exempt_cost_czk = exempt_cost_czk
            sell_fees_exempt = fee_scale(d.sell_fees, exempt_frac)
            d.exempt_fees_czk = fee_to_czk(
                fee_add(sell_fees_exempt, exempt_buy_fees), rates
            ).quantize(Decimal("0.01"))
            d.exempt_gain_czk = (
                d.exempt_proceeds_czk - d.exempt_cost_czk
                - d.exempt_fees_czk)
            d.taxable_proceeds_czk = d.proceeds_czk - d.exempt_proceeds_czk
            d.taxable_cost_czk = d.cost_czk - d.exempt_cost_czk
            d.taxable_fees_czk = d.fees_czk - d.exempt_fees_czk
            d.taxable_gain_czk = (
                d.taxable_proceeds_czk - d.taxable_cost_czk
                - d.taxable_fees_czk)
        else:
            # Fully exempt or fully taxable
            d.exempt_proceeds_czk = d.proceeds_czk if d.exempt else Decimal("0")
            d.exempt_cost_czk = d.cost_czk if d.exempt else Decimal("0")
            d.exempt_fees_czk = d.fees_czk if d.exempt else Decimal("0")
            d.exempt_gain_czk = d.gain_czk if d.exempt else Decimal("0")
            d.taxable_proceeds_czk = Decimal("0") if d.exempt else d.proceeds_czk
            d.taxable_cost_czk = Decimal("0") if d.exempt else d.cost_czk
            d.taxable_fees_czk = Decimal("0") if d.exempt else d.fees_czk
            d.taxable_gain_czk = Decimal("0") if d.exempt else d.gain_czk

        result.append(d)
    return result


# ---------------------------------------------------------------------------
# Dividend processing
# ---------------------------------------------------------------------------

def process_dividends(rows, year):
    # type: (List[RawRow], int) -> List[DividendEvent]
    """Extract and pair dividend events for a given year."""
    # Collect raw dividend and tax rows, keyed by (isin, value_date)
    raw = defaultdict(lambda: {"div": [], "tax": [], "product": "", "ccy": ""})
    for r in rows:
        if r.value_date.year != year:
            continue
        d = r.description
        if d == "Dividenda":
            key = (r.isin, r.value_date)
            raw[key]["div"].append(r.mov_amt)
            raw[key]["product"] = r.product
            raw[key]["ccy"] = r.mov_ccy
        elif d == "Daň z dividendy":
            key = (r.isin, r.value_date)
            raw[key]["tax"].append(r.mov_amt)
            if not raw[key]["product"]:
                raw[key]["product"] = r.product
            if not raw[key]["ccy"]:
                raw[key]["ccy"] = r.mov_ccy

    events = []  # type: List[DividendEvent]
    for (isin, vdate), data in sorted(raw.items(), key=lambda x: x[0][1]):
        gross = sum(data["div"], Decimal("0"))
        tax = sum(data["tax"], Decimal("0"))
        # Skip if gross nets to 0 (full storno)
        if gross == 0 and tax == 0:
            continue
        # Skip negative gross (storno without correction)
        if gross < 0:
            continue
        country = resolve_country(isin, data["product"])
        events.append(DividendEvent(
            product=data["product"], isin=isin, country=country,
            value_date=vdate, gross=gross, tax_withheld=tax,
            ccy=data["ccy"],
            has_treaty=country in COUNTRIES_WITH_SZDZ,
        ))
    return events


def calc_dividend_tax(divs, rates):
    # type: (List[DividendEvent], Dict[str, Decimal]) -> List[DividendEvent]
    """Calculate CZK values and double-taxation credit for dividends.

    Three-cap rule for treaty countries (§38f ZDP):
        credit = min(actual_tax_paid, treaty_max_rate * gross, CZ_15% * gross)
    Non-treaty countries: no credit, tax deductible as expense (§24/2/ch).
    """
    for d in divs:
        r = rates.get(d.ccy, Decimal("1"))
        d.gross_czk = (d.gross * r).quantize(Decimal("0.01"))
        d.tax_czk = (abs(d.tax_withheld) * r).quantize(Decimal("0.01"))

        if d.has_treaty:
            # CZ tax = 15% of gross
            d.cz_tax_czk = (d.gross_czk * Decimal("0.15")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP)
            # Treaty max: cap creditable tax at the treaty rate
            treaty_rate = TREATY_MAX_DIV_RATE.get(d.country, Decimal("0.15"))
            treaty_max_czk = (d.gross_czk * treaty_rate).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP)
            # Credit = min(actual tax, treaty cap, CZ tax)
            d.credit_czk = min(d.tax_czk, treaty_max_czk, d.cz_tax_czk)
            d.expense_czk = Decimal("0")
        else:
            # No treaty: no credit, foreign tax deductible as expense
            d.expense_czk = d.tax_czk
            taxable_base = d.gross_czk - d.expense_czk
            d.cz_tax_czk = (taxable_base * Decimal("0.15")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP)
            d.credit_czk = Decimal("0")
    return divs


def print_dividend_results(divs, rates, year):
    # type: (List[DividendEvent], Dict[str, Decimal], int) -> None
    """Print dividend tax report."""
    if not divs:
        return

    print("\n{}".format("=" * 72))
    print("  DIVIDENDY (p8 ZDP) - ROK {}".format(year))
    print("{}".format("=" * 72))

    # Separate CZ (srazkova dan) from foreign
    cz_divs = [d for d in divs if d.country == "CZ"]
    foreign_divs = [d for d in divs if d.country != "CZ"]

    Q = Decimal("0.01")

    # -- Foreign dividends detail --
    if foreign_divs:
        print("\n--- Zahranicni dividendy ---")
        for i, d in enumerate(foreign_divs, 1):
            cname = COUNTRY_NAMES.get(d.country, d.country)
            if d.has_treaty:
                treaty_rate = TREATY_MAX_DIV_RATE.get(d.country, Decimal("0.15"))
                treaty_pct = int(treaty_rate * 100)
                treaty_label = "SZDZ max {}%".format(treaty_pct)
            else:
                treaty_label = "BEZ SZDZ"
            print("\n  {}. {} ({}) [{}, {}]".format(
                i, d.product, d.isin, cname, treaty_label))
            print("     Datum:       {}".format(d.value_date.strftime("%d.%m.%Y")))
            print("     Hruba div.:  {:>10} {} = {:>10} CZK".format(
                d.gross.quantize(Q), d.ccy, d.gross_czk))
            print("     Srazka:      {:>10} {} = {:>10} CZK".format(
                abs(d.tax_withheld).quantize(Q), d.ccy, d.tax_czk))
            if d.has_treaty:
                print("     CZ dan 15%:                  {:>10} CZK".format(
                    d.cz_tax_czk))
                print("     Zapocet:                     {:>10} CZK".format(
                    d.credit_czk))
                doplatek = d.cz_tax_czk - d.credit_czk
                print("     Doplatek:                    {:>10} CZK".format(
                    doplatek.quantize(Q)))
                # Warn if actual withholding exceeds treaty rate
                if d.tax_czk > d.credit_czk and d.tax_czk > d.cz_tax_czk * Decimal("0"):
                    excess = d.tax_czk - d.credit_czk
                    if excess > Decimal("0.05"):
                        print("     !! Preplatek srazky {:>6} CZK"
                              " (lze zadost o vraceni ze zdroj. statu)".format(
                                  excess.quantize(Q)))
            else:
                print("     Odpocet dane jako vydaj:     {:>10} CZK".format(
                    d.expense_czk))
                print("     Zaklad (hruba - vydaj):      {:>10} CZK".format(
                    (d.gross_czk - d.expense_czk).quantize(Q)))
                print("     CZ dan 15%:                  {:>10} CZK".format(
                    d.cz_tax_czk))

    # -- Summary by country --
    treaty_divs = [d for d in foreign_divs if d.has_treaty]
    no_treaty_divs = [d for d in foreign_divs if not d.has_treaty]

    if treaty_divs:
        print("\n--- Zeme se SZDZ - zapocet dane (Priloha c. 3) ---")
        by_country = defaultdict(lambda: {
            "gross": Decimal("0"), "tax": Decimal("0"),
            "cz_tax": Decimal("0"), "credit": Decimal("0")
        })
        for d in treaty_divs:
            c = d.country
            by_country[c]["gross"] += d.gross_czk
            by_country[c]["tax"] += d.tax_czk
            by_country[c]["cz_tax"] += d.cz_tax_czk
            by_country[c]["credit"] += d.credit_czk

        print("  {:12s} {:>12s} {:>12s} {:>12s} {:>12s} {:>12s}".format(
            "Stat", "Prijem CZK", "Dan zahr.", "CZ dan 15%",
            "Zapocet", "Doplatek"))
        total_gross = Decimal("0")
        total_tax_foreign = Decimal("0")
        total_cz_tax = Decimal("0")
        total_credit = Decimal("0")
        for c in sorted(by_country.keys()):
            s = by_country[c]
            doplatek = s["cz_tax"] - s["credit"]
            cname = COUNTRY_NAMES.get(c, c)
            print("  {:12s} {:>12} {:>12} {:>12} {:>12} {:>12}".format(
                cname,
                s["gross"].quantize(Q), s["tax"].quantize(Q),
                s["cz_tax"].quantize(Q), s["credit"].quantize(Q),
                doplatek.quantize(Q)))
            total_gross += s["gross"]
            total_tax_foreign += s["tax"]
            total_cz_tax += s["cz_tax"]
            total_credit += s["credit"]

        total_doplatek = total_cz_tax - total_credit
        print("  {:12s} {:>12} {:>12} {:>12} {:>12} {:>12}".format(
            "CELKEM",
            total_gross.quantize(Q), total_tax_foreign.quantize(Q),
            total_cz_tax.quantize(Q), total_credit.quantize(Q),
            total_doplatek.quantize(Q)))

    if no_treaty_divs:
        print("\n--- Zeme BEZ SZDZ - dan jako vydaj (p24/2/ch ZDP) ---")
        by_country = defaultdict(lambda: {
            "gross": Decimal("0"), "expense": Decimal("0"),
            "cz_tax": Decimal("0")
        })
        for d in no_treaty_divs:
            c = d.country
            by_country[c]["gross"] += d.gross_czk
            by_country[c]["expense"] += d.expense_czk
            by_country[c]["cz_tax"] += d.cz_tax_czk

        print("  {:16s} {:>12s} {:>12s} {:>12s} {:>12s}".format(
            "Stat", "Prijem CZK", "Vydaj (dan)", "Zaklad", "CZ dan 15%"))
        for c in sorted(by_country.keys()):
            s = by_country[c]
            zaklad = s["gross"] - s["expense"]
            cname = COUNTRY_NAMES.get(c, c)
            print("  {:16s} {:>12} {:>12} {:>12} {:>12}".format(
                cname,
                s["gross"].quantize(Q), s["expense"].quantize(Q),
                zaklad.quantize(Q), s["cz_tax"].quantize(Q)))

    # -- CZ dividends (srazkova dan) --
    if cz_divs:
        print("\n--- Ceske dividendy (srazkova dan dle p36 ZDP) ---")
        print("  (Neuvadeji se do danoveho priznani - dan je konecna)")
        for d in cz_divs:
            print("  {} | {:>10} {} | srazka {:>10} {} | cista {:>10} {}".format(
                d.product,
                d.gross.quantize(Q), d.ccy,
                abs(d.tax_withheld).quantize(Q), d.ccy,
                (d.gross + d.tax_withheld).quantize(Q), d.ccy))

    # -- Grand summary with tax return guidance --
    print("\n{}".format("=" * 72))
    print("  DIVIDENDY - SOUHRN PRO DANOVE PRIZNAN - ROK {}".format(year))
    print("{}".format("=" * 72))

    if cz_divs:
        cz_total = sum(d.gross for d in cz_divs)
        cz_tax_total = sum(abs(d.tax_withheld) for d in cz_divs)
        print("  Ceske dividendy: {} CZK hruba, {} CZK srazka".format(
            cz_total.quantize(Q), cz_tax_total.quantize(Q)))
        print("  -> NEUVADET do priznani (srazkova dan dle p36 je konecna)")
        print()

    if foreign_divs:
        total_gross = sum(d.gross_czk for d in foreign_divs)
        total_expense = sum(d.expense_czk for d in foreign_divs)
        total_credit = sum(d.credit_czk for d in foreign_divs)
        total_cz_tax = sum(d.cz_tax_czk for d in foreign_divs)
        total_doplatek = total_cz_tax - total_credit

        dilci_p8 = total_gross - total_expense
        print("  >>> HLAVNI FORMULAR <<<")
        print("  Radek 38 (dilci zaklad dane p8): {:>10} CZK".format(
            dilci_p8.quantize(Q)))
        if total_expense > 0:
            print("    (hrube prijmy {} - vydaje {} z zemi bez SZDZ)".format(
                total_gross.quantize(Q), total_expense.quantize(Q)))
        print()
        print("  >>> PRILOHA c. 3 - zapocet dane ze zahranici <<<")
        print("  (Vyplnit pro kazdy stat se SZDZ dle tabulky vyse)")
        print("  Celkem dan zaplacena v zahranici: {:>8} CZK".format(
            sum(d.tax_czk for d in foreign_divs).quantize(Q)))
        print("  Celkem uznany zapocet:            {:>8} CZK".format(
            total_credit.quantize(Q)))
        print("  Doplatek dane v CR:               {:>8} CZK".format(
            total_doplatek.quantize(Q)))
    print()


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

Q2 = Decimal("0.01")


def print_results(disps, rates, year):
    # type: (List[Disposition], Dict[str, Decimal], int) -> None
    print("\n{}".format("=" * 72))
    print("  VYPOCET DANE Z KAPITALOVYCH ZISKU - ROK {}".format(year))
    print("{}".format("=" * 72))

    print("\nJednotny kurz CNB {}:".format(year))
    used_ccy = sorted({d.ccy for d in disps} | {"EUR"})
    for c in used_ccy:
        if c == "CZK":
            continue
        v = rates.get(c)
        if v:
            print("  1 {} = {} CZK".format(c, v.quantize(Decimal("0.0001"))))

    print("\n{}".format("=" * 72))
    print("  DETAIL PRODEJU")
    print("{}".format("=" * 72))

    total_income = Decimal("0")
    total_expense = Decimal("0")
    total_exempt_gain = Decimal("0")
    total_taxable_gain = Decimal("0")

    for i, d in enumerate(disps, 1):
        buy_dates = sorted({p.buy_date for p in d.portions})
        bd_str = ", ".join(b.strftime("%d.%m.%Y") for b in buy_dates)
        held = [(d.sell_date - p.buy_date).days for p in d.portions]
        min_h = min(held) if held else 0
        max_h = max(held) if held else 0

        src_label = {
            "trade": "prodej",
            "delisting": "delisting",
            "merger": "merger",
            "split_zero": "split->0 ks",
        }.get(d.source, d.source)

        exempt_label = "ANO" if d.exempt else "NE"
        if not d.exempt and d.partial_exempt_qty > 0:
            exempt_label = "CASTECNE ({}/{} ks)".format(
                d.partial_exempt_qty, d.sell_qty)

        all_fees = fee_add(d.sell_fees, d.buy_fees)
        fees_str = fee_display(all_fees)

        print("\n--- {}. {} ({}) [{}] ---".format(i, d.product, d.isin, src_label))
        print("  Datum prodeje:  {}".format(d.sell_date.strftime("%d.%m.%Y")))
        print("  Datum nakupu:   {}".format(bd_str))
        print("  Pocet kusu:     {:g}".format(d.sell_qty))
        # Detect if buy and sell use different currencies
        buy_ccys = {p.cost_ccy for p in d.portions}
        mixed_ccy = len(buy_ccys) > 1 or (buy_ccys and buy_ccys != {d.ccy})
        buy_ccy_label = list(buy_ccys)[0] if len(buy_ccys) == 1 else d.ccy

        print("  Prijem:         {:>12} {}  = {:>12} CZK".format(
            d.proceeds.quantize(Q2), d.ccy, d.proceeds_czk))
        print("  Vydaj (nakup):  {:>12} {}  = {:>12} CZK".format(
            d.buy_cost.quantize(Q2), buy_ccy_label, d.cost_czk))
        print("  Poplatky:       {:>12}      = {:>12} CZK".format(
            fees_str, d.fees_czk))
        if mixed_ccy:
            print("  Zisk/Ztrata:                       {:>12} CZK".format(
                d.gain_czk))
        else:
            gain_orig = d.proceeds - d.buy_cost
            print("  Zisk/Ztrata:    {:>12} {}  = {:>12} CZK".format(
                gain_orig.quantize(Q2), d.ccy, d.gain_czk))
        print("  Drzeno:         {}-{} dni".format(min_h, max_h))
        print("  Casovy test:    {}".format(exempt_label))
        if hasattr(d, "taxable_gain_czk") and d.partial_exempt_qty > 0 and not d.exempt:
            print("    -> Osvobozeno: {:g} ks, zisk {:>10} CZK".format(
                d.partial_exempt_qty, d.exempt_gain_czk.quantize(Q2)))
            taxable_qty = d.sell_qty - d.partial_exempt_qty
            print("    -> Zdanitelne: {:g} ks, zisk {:>10} CZK".format(
                taxable_qty, d.taxable_gain_czk.quantize(Q2)))

        if hasattr(d, "taxable_proceeds_czk"):
            total_income += d.taxable_proceeds_czk
            total_expense += d.taxable_cost_czk + d.taxable_fees_czk
            total_taxable_gain += d.taxable_gain_czk
            total_exempt_gain += d.exempt_gain_czk
        elif d.exempt:
            total_exempt_gain += d.gain_czk
        else:
            total_income += d.proceeds_czk
            total_expense += d.cost_czk + d.fees_czk
            total_taxable_gain += d.gain_czk

    print("\n{}".format("=" * 72))
    print("  KAPITALOVE ZISKY - SOUHRN PRO DANOVE PRIZNAN - ROK {}".format(year))
    print("{}".format("=" * 72))
    n_total = len(disps)
    n_exempt = sum(1 for d in disps if d.exempt)
    n_taxable = n_total - n_exempt
    print("  Celkem prodeju:       {}  (zdanitelnych: {}, osvobozenych: {})".format(
        n_total, n_taxable, n_exempt))
    print("  Osvobozeno (p4/1/w):  {:>14} CZK  (neuvadi se)".format(
        total_exempt_gain.quantize(Q2)))
    print()
    print("  >>> PRILOHA c. 2, oddil 2, tabulka c. 1 (p10 ZDP) <<<")
    print("  Druh prijmu: Prijmy z uplatneho prevodu cennych papiru p10/1/b/1")
    print("  Prijmy:               {:>14} CZK".format(total_income.quantize(Q2)))
    print("  Vydaje:               {:>14} CZK".format(total_expense.quantize(Q2)))
    print()
    dilci = total_taxable_gain.quantize(Q2)
    print("  >>> HLAVNI FORMULAR <<<")
    print("  Radek 40 (dilci zaklad dane p10): {:>10} CZK".format(dilci))
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Vypocet dane z kapitalovych zisku z Degiro CSV vypisu"
    )
    parser.add_argument("csv_file", help="Cesta k Degiro Account Statement CSV")
    parser.add_argument("tax_year", type=int, help="Danovy rok (napr. 2025)")
    args = parser.parse_args()

    print("Nacitam {}...".format(args.csv_file))
    rows = read_csv(args.csv_file)
    print("  Nacteno {} radku".format(len(rows)))

    print("\nStahuji kurzy CNB pro vypocet jednotneho kurzu {}:".format(args.tax_year))
    rates = calc_unified_rates(args.tax_year)

    print("\nZpracovavam transakce...")
    pf = process_all(rows)
    n_lots = sum(len(v) for v in pf.lots.values())
    print("  Aktivnich lotu: {}".format(n_lots))
    print("  Prodeju: {}".format(len(pf.sells)))

    print("\nFIFO parovani...")
    disps = fifo_match(pf)

    print("\nVypocet dane za rok {}...".format(args.tax_year))
    year_disps = calc_tax(disps, args.tax_year, rates)

    print_results(year_disps, rates, args.tax_year)

    # Dividends
    print("Zpracovavam dividendy...")
    div_events = process_dividends(rows, args.tax_year)
    print("  Dividend: {}".format(len(div_events)))
    div_events = calc_dividend_tax(div_events, rates)
    print_dividend_results(div_events, rates, args.tax_year)

    if pf.warnings:
        print("VAROVANI:")
        for w in pf.warnings:
            print("  - {}".format(w))


if __name__ == "__main__":
    main()
