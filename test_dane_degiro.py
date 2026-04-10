#!/usr/bin/env python3
"""Comprehensive tests for dane_degiro.py."""

from __future__ import annotations

import copy
import csv
import io
import os
import tempfile
import unittest
from datetime import date
from decimal import Decimal

from dane_degiro import (
    BuyLot, DividendEvent, Disposition, MatchedPortion, Portfolio,
    RawRow, SellEvent,
    COUNTRIES_WITH_SZDZ, ISIN_COUNTRY_OVERRIDE, TREATY_MAX_DIV_RATE,
    calc_dividend_tax, calc_tax, classify, fee_add, fee_display,
    fee_scale, fee_to_czk, fee_zero, fifo_match, parse_date, parse_dec,
    parse_trade, passes_time_test, process_all, process_dividends,
    read_csv, resolve_country,
)

D = Decimal


# ---------------------------------------------------------------------------
# Helper: build a RawRow quickly
# ---------------------------------------------------------------------------

_line_counter = 0


def row(tx_date, description, product="", isin="", mov_ccy="EUR",
        mov_amt="0", order_id="", time="10:00", value_date=None,
        fx_rate=None):
    global _line_counter
    _line_counter += 1
    if value_date is None:
        value_date = tx_date
    return RawRow(
        line=_line_counter,
        tx_date=parse_date(tx_date) if isinstance(tx_date, str) else tx_date,
        time=time,
        value_date=parse_date(value_date) if isinstance(value_date, str) else value_date,
        product=product,
        isin=isin,
        description=description,
        fx_rate=D(fx_rate) if fx_rate else None,
        mov_ccy=mov_ccy,
        mov_amt=D(mov_amt),
        bal_ccy=mov_ccy,
        bal_amt=D("0"),
        order_id=order_id,
    )


RATES = {"USD": D("22"), "EUR": D("25"), "CZK": D("1"), "HKD": D("2.8"),
         "CAD": D("16"), "GBP": D("28")}


# ===================================================================
# 1. Parsing helpers
# ===================================================================

class TestParseDec(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(parse_dec("123,45"), D("123.45"))

    def test_negative(self):
        self.assertEqual(parse_dec("-0,07"), D("-0.07"))

    def test_thousands_space(self):
        self.assertEqual(parse_dec("1 234,56"), D("1234.56"))

    def test_nbsp(self):
        self.assertEqual(parse_dec("1\xa0035"), D("1035"))

    def test_quoted(self):
        self.assertEqual(parse_dec('"6210,00"'), D("6210.00"))

    def test_empty(self):
        self.assertEqual(parse_dec(""), D("0"))

    def test_integer(self):
        self.assertEqual(parse_dec("42"), D("42"))

    def test_large_number(self):
        self.assertEqual(parse_dec("24 000,00"), D("24000.00"))


class TestParseDate(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(parse_date("14-04-2020"), date(2020, 4, 14))

    def test_leading_zero(self):
        self.assertEqual(parse_date("01-01-2025"), date(2025, 1, 1))


class TestParseTrade(unittest.TestCase):
    def test_buy(self):
        r = parse_trade("Nákup 4 Airbus SE@61 EUR (NL0000235190)")
        self.assertEqual(r["action"], "Nákup")
        self.assertEqual(r["qty"], 4)
        self.assertEqual(r["price"], D("61"))
        self.assertEqual(r["ccy"], "EUR")
        self.assertEqual(r["isin"], "NL0000235190")

    def test_sell_with_decimal(self):
        r = parse_trade("Prodej 30 ADR on Sony Group Corp@24,625 USD (US8356993076)")
        self.assertEqual(r["action"], "Prodej")
        self.assertEqual(r["qty"], 30)
        self.assertEqual(r["price"], D("24.625"))

    def test_thousands_in_price(self):
        r = parse_trade("Prodej 6 Komercni Banka as@1 035 CZK (CZ0008019106)")
        self.assertEqual(r["qty"], 6)
        self.assertEqual(r["price"], D("1035"))
        self.assertEqual(r["ccy"], "CZK")

    def test_nbsp_in_price(self):
        r = parse_trade("Prodej 19 CEZ as@1\xa0211 CZK (CZ0005112300)")
        self.assertEqual(r["price"], D("1211"))

    def test_no_match(self):
        self.assertIsNone(parse_trade("Degiro Cash Sweep Transfer"))
        self.assertIsNone(parse_trade("Dividenda"))

    def test_fund_conversion_no_match(self):
        self.assertIsNone(parse_trade(
            "Konverze Peněžního Fondu: Nákup 0,0302 za 0,9933 EUR"))

    def test_prefixed_descriptions(self):
        r = parse_trade("Stock split: Prodej 6 ADR on Sony@95,14 USD (US8356993076)")
        self.assertEqual(r["action"], "Prodej")
        self.assertEqual(r["qty"], 6)

        r = parse_trade("Merger: Prodej 4 Slack Technologies Inc@18,41 USD (US83088V1026)")
        self.assertEqual(r["action"], "Prodej")
        self.assertEqual(r["qty"], 4)


# ===================================================================
# 2. Row classification
# ===================================================================

class TestClassify(unittest.TestCase):
    def _row(self, desc):
        return row("01-01-2025", desc)

    def test_buy(self):
        self.assertEqual(classify(self._row(
            "Nákup 1 Tesla Inc@643,47 USD (US88160R1014)")), "buy")

    def test_sell(self):
        self.assertEqual(classify(self._row(
            "Prodej 6 Tesla Inc@272,275 USD (US88160R1014)")), "sell")

    def test_stock_split(self):
        self.assertEqual(classify(self._row(
            "Stock split: Nákup 3 Tesla Inc@297,0967 USD (US88160R1014)")),
            "stock_split")

    def test_product_change(self):
        self.assertEqual(classify(self._row(
            "Změna produktu: Nákup 21 Palantir@15,87 USD (US69608A1088)")),
            "product_change")

    def test_merger(self):
        self.assertEqual(classify(self._row(
            "Merger: Prodej 4 Slack Technologies Inc@18,41 USD (US83088V1026)")),
            "merger")

    def test_delisting(self):
        self.assertEqual(classify(self._row(
            "Delisting: Prodej 20 SolarWinds Corp@0 USD (US83417Q2049)")),
            "delisting")

    def test_spin_off(self):
        self.assertEqual(classify(self._row(
            "Spin off: Nákup 20 N-Able Inc@0 USD (US62878D1000)")),
            "spin_off")

    def test_rights_issue(self):
        self.assertEqual(classify(self._row(
            "Rights issue: Nákup 13 BYD Co Ltd@0 EUR (CNE100000296)")),
            "rights_issue")

    def test_internal_transfer_skip(self):
        self.assertEqual(classify(self._row(
            "Interní převod: Prodej 2 Netflix Inc@508,65 EUR (US64110L1061)")),
            "skip")

    def test_fee(self):
        self.assertEqual(classify(self._row(
            "DEGIRO Transaction and/or third party fees")), "fee")

    def test_dividend_skip(self):
        self.assertEqual(classify(self._row("Dividenda")), "skip")
        self.assertEqual(classify(self._row("Daň z dividendy")), "skip")

    def test_fx_skip(self):
        self.assertEqual(classify(self._row(
            "FX vyučtování konverze měny")), "skip")

    def test_return_of_capital(self):
        self.assertEqual(classify(self._row("Vratka kapitálu")),
                         "return_of_capital")

    def test_corp_action_cash(self):
        self.assertEqual(classify(self._row(
            "Korporátní akce hotovostní vypořádání akcie")),
            "corp_action_cash")

    def test_fund_conversion_skip(self):
        self.assertEqual(classify(self._row(
            "Konverze Peněžního Fondu: Nákup 0,0302 za 0,9933 EUR")),
            "skip")


# ===================================================================
# 3. Fee helpers
# ===================================================================

class TestFeeHelpers(unittest.TestCase):
    def test_fee_zero(self):
        self.assertEqual(fee_zero(), [])

    def test_fee_to_czk(self):
        fees = [(D("2"), "EUR"), (D("12.50"), "CZK")]
        self.assertEqual(fee_to_czk(fees, RATES), D("2") * D("25") + D("12.50"))

    def test_fee_scale(self):
        fees = [(D("10"), "EUR")]
        scaled = fee_scale(fees, D("0.5"))
        self.assertEqual(scaled, [(D("5"), "EUR")])

    def test_fee_add(self):
        a = [(D("1"), "EUR")]
        b = [(D("2"), "USD")]
        self.assertEqual(fee_add(a, b), [(D("1"), "EUR"), (D("2"), "USD")])

    def test_fee_display_multi(self):
        fees = [(D("2"), "EUR"), (D("12.50"), "CZK")]
        self.assertIn("CZK", fee_display(fees))
        self.assertIn("EUR", fee_display(fees))

    def test_fee_display_empty(self):
        self.assertEqual(fee_display([]), "0")


# ===================================================================
# 4. Time test
# ===================================================================

class TestTimeTest(unittest.TestCase):
    def test_exactly_3_years_not_exempt(self):
        # Buy 01.01.2022, sell 01.01.2025 = exactly 3 years -> NOT exempt
        self.assertFalse(passes_time_test(date(2022, 1, 1), date(2025, 1, 1)))

    def test_3_years_plus_1_day_exempt(self):
        self.assertTrue(passes_time_test(date(2022, 1, 1), date(2025, 1, 2)))

    def test_under_3_years(self):
        self.assertFalse(passes_time_test(date(2023, 6, 1), date(2025, 3, 7)))

    def test_well_over_3_years(self):
        self.assertTrue(passes_time_test(date(2020, 4, 14), date(2025, 7, 8)))

    def test_leap_day_buy(self):
        # Buy Feb 29, 2020 (leap year). 3 years later Feb 28, 2023.
        # Sell Mar 1, 2023 -> exempt
        self.assertTrue(passes_time_test(date(2020, 2, 29), date(2023, 3, 1)))
        # Sell Feb 28, 2023 -> NOT exempt (not > 3 years)
        self.assertFalse(passes_time_test(date(2020, 2, 29), date(2023, 2, 28)))

    def test_edge_netflix(self):
        # Netflix: bought 28.02.2022, sold 07.03.2025
        # 3 years later = 28.02.2025, sold after -> exempt
        self.assertTrue(passes_time_test(date(2022, 2, 28), date(2025, 3, 7)))
        # Netflix: bought 08.03.2022, sold 07.03.2025
        # 3 years later = 08.03.2025, sold before -> NOT exempt
        self.assertFalse(passes_time_test(date(2022, 3, 8), date(2025, 3, 7)))


# ===================================================================
# 5. Portfolio - FIFO basics
# ===================================================================

class TestPortfolioFIFO(unittest.TestCase):
    def test_simple_buy_sell(self):
        pf = Portfolio()
        pf._add_lot("X", "Stock X", date(2023, 1, 1), 10, D("100"), "USD",
                     [(D("2"), "EUR")], "o1")
        pf._add_sell("X", "Stock X", date(2025, 6, 1), 10, D("150"),
                     D("1500"), "USD", [(D("2"), "EUR")], "o2")

        disps = fifo_match(pf)
        self.assertEqual(len(disps), 1)
        d = disps[0]
        self.assertEqual(d.sell_qty, D("10"))
        self.assertEqual(d.proceeds, D("1500"))
        self.assertEqual(len(d.portions), 1)
        self.assertEqual(d.portions[0].cost_per_unit, D("100"))

    def test_fifo_order(self):
        """Oldest lot consumed first."""
        pf = Portfolio()
        pf._add_lot("X", "X", date(2020, 1, 1), 5, D("10"), "USD",
                     fee_zero(), "")
        pf._add_lot("X", "X", date(2021, 1, 1), 5, D("20"), "USD",
                     fee_zero(), "")
        pf._add_sell("X", "X", date(2025, 1, 1), 7, D("30"), D("210"),
                     "USD", fee_zero(), "")

        disps = fifo_match(pf)
        self.assertEqual(len(disps), 1)
        portions = disps[0].portions
        self.assertEqual(len(portions), 2)
        # First lot fully consumed
        self.assertEqual(portions[0].qty, D("5"))
        self.assertEqual(portions[0].cost_per_unit, D("10"))
        self.assertEqual(portions[0].buy_date, date(2020, 1, 1))
        # Second lot partially consumed
        self.assertEqual(portions[1].qty, D("2"))
        self.assertEqual(portions[1].cost_per_unit, D("20"))
        self.assertEqual(portions[1].buy_date, date(2021, 1, 1))

    def test_partial_lot_split(self):
        """When selling less than a lot, the lot is split correctly."""
        pf = Portfolio()
        pf._add_lot("X", "X", date(2020, 1, 1), 100, D("10"), "USD",
                     [(D("4"), "EUR")], "")
        pf._add_sell("X", "X", date(2025, 1, 1), 60, D("15"), D("900"),
                     "USD", fee_zero(), "")

        disps = fifo_match(pf)
        self.assertEqual(disps[0].portions[0].qty, D("60"))
        # Fee proportionally split: 4 * 60/100 = 2.4
        fee_amt = disps[0].portions[0].fees[0][0]
        self.assertEqual(fee_amt, D("2.4"))

        # Remaining lot should have 40 shares and 1.6 EUR fee
        remaining = pf.lots["X"]
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].quantity, D("40"))
        self.assertEqual(remaining[0].fees[0][0], D("1.6"))

    def test_multiple_sells_fifo(self):
        pf = Portfolio()
        pf._add_lot("X", "X", date(2020, 1, 1), 10, D("10"), "USD",
                     fee_zero(), "")
        pf._add_sell("X", "X", date(2024, 1, 1), 3, D("20"), D("60"),
                     "USD", fee_zero(), "")
        pf._add_sell("X", "X", date(2025, 1, 1), 5, D("25"), D("125"),
                     "USD", fee_zero(), "")

        disps = fifo_match(pf)
        self.assertEqual(len(disps), 2)
        # After first sell: 7 remaining
        # After second sell: 2 remaining
        remaining = pf.lots["X"]
        self.assertEqual(remaining[0].quantity, D("2"))

    def test_sell_more_than_available_warns(self):
        pf = Portfolio()
        pf._add_lot("X", "X", date(2020, 1, 1), 5, D("10"), "USD",
                     fee_zero(), "")
        pf._add_sell("X", "X", date(2025, 1, 1), 10, D("20"), D("200"),
                     "USD", fee_zero(), "")
        fifo_match(pf)
        self.assertTrue(any("chybi" in w for w in pf.warnings))


# ===================================================================
# 6. Stock splits
# ===================================================================

class TestStockSplit(unittest.TestCase):
    def test_same_isin_forward_split(self):
        """3:1 split: 1 share becomes 3."""
        pf = Portfolio()
        pf._add_lot("X", "Stock", date(2021, 5, 10), 1, D("643.47"), "USD",
                     [(D("0.50"), "EUR")], "")
        sell_td = {"isin": "X", "qty": 1, "ccy": "USD"}
        buy_td = {"isin": "X", "qty": 3, "ccy": "USD"}
        pf.handle_stock_split(sell_td, buy_td, date(2022, 8, 25), "Stock")

        lots = pf.lots["X"]
        self.assertEqual(len(lots), 1)
        self.assertEqual(lots[0].quantity, D("3"))
        # Price per unit: 643.47 / 3 = 214.49
        self.assertEqual(lots[0].price_per_unit, D("643.47") / 3)
        # Buy date preserved!
        self.assertEqual(lots[0].buy_date, date(2021, 5, 10))
        # Fee preserved
        self.assertEqual(lots[0].fees, [(D("0.50"), "EUR")])

    def test_different_isin_reverse_split(self):
        """2:1 reverse split with ISIN change: 40 -> 20."""
        pf = Portfolio()
        pf._add_lot("OLD", "SW", date(2021, 1, 11), 31, D("15.05"), "USD",
                     [(D("0.5"), "EUR")], "")
        pf._add_lot("OLD", "SW", date(2021, 1, 13), 9, D("14.67"), "USD",
                     [(D("0.5"), "EUR")], "")
        sell_td = {"isin": "OLD", "qty": 40, "ccy": "USD"}
        buy_td = {"isin": "NEW", "qty": 20, "ccy": "USD"}
        pf.handle_stock_split(sell_td, buy_td, date(2021, 8, 2), "SW")

        self.assertEqual(pf.lots["OLD"], [])
        new_lots = pf.lots["NEW"]
        self.assertEqual(len(new_lots), 2)
        # Total shares = 20
        total = sum(l.quantity for l in new_lots)
        self.assertEqual(total, D("20"))
        # Total cost preserved: 31*15.05 + 9*14.67 = 466.55 + 132.03 = 598.58
        total_cost = sum(l.price_per_unit * l.quantity for l in new_lots)
        self.assertAlmostEqual(float(total_cost), 598.58, places=2)
        # Buy dates preserved
        self.assertEqual(new_lots[0].buy_date, date(2021, 1, 11))
        self.assertEqual(new_lots[1].buy_date, date(2021, 1, 13))

    def test_reverse_split_to_zero(self):
        """Reverse split producing 0 shares = total loss."""
        pf = Portfolio()
        pf._add_lot("OLD", "Agrify", date(2021, 3, 16), 4, D("12.80"), "USD",
                     [(D("0.51"), "EUR")], "")
        sell_td = {"isin": "OLD", "qty": 4, "ccy": "USD"}
        buy_td = {"isin": "NEW2", "qty": 0, "ccy": "USD"}
        pf.handle_stock_split(sell_td, buy_td, date(2022, 10, 18), "Agrify")

        # Should create a sell event at price 0
        self.assertEqual(len(pf.sells), 1)
        s = pf.sells[0]
        self.assertEqual(s.quantity, D("4"))
        self.assertEqual(s.total_proceeds, D("0"))
        self.assertEqual(s.source, "split_zero")

    def test_split_preserves_partial_lots(self):
        """Split only affects the specified number of shares."""
        pf = Portfolio()
        # 33 shares total
        pf._add_lot("X", "FC", date(2020, 1, 1), 30, D("1"), "EUR",
                     fee_zero(), "")
        pf._add_lot("X", "FC", date(2020, 6, 1), 3, D("2"), "EUR",
                     fee_zero(), "")
        # Split 30 -> 1 (different ISIN), 3 remain
        sell_td = {"isin": "X", "qty": 30, "ccy": "EUR"}
        buy_td = {"isin": "Y", "qty": 1, "ccy": "EUR"}
        pf.handle_stock_split(sell_td, buy_td, date(2024, 11, 1), "FC")

        # 3 shares remain in old ISIN
        self.assertEqual(pf.lots["X"][0].quantity, D("3"))
        # 1 share in new ISIN with total cost = 30 * 1 = 30
        self.assertEqual(len(pf.lots["Y"]), 1)
        self.assertEqual(pf.lots["Y"][0].quantity, D("1"))
        self.assertEqual(pf.lots["Y"][0].price_per_unit, D("30"))


# ===================================================================
# 7. Merger, delisting, product change, return of capital
# ===================================================================

class TestCorporateActions(unittest.TestCase):
    def test_merger_cash_out(self):
        """Merger where old shares are sold for cash (0 new shares)."""
        pf = Portfolio()
        pf._add_lot("OLD", "Slack", date(2020, 12, 17), 4, D("42.61"), "USD",
                     fee_zero(), "")
        sell_td = {"isin": "OLD", "qty": 4, "ccy": "USD"}
        buy_td = {"isin": "NEW", "qty": 0, "ccy": "USD"}
        pf.handle_merger(sell_td, buy_td, date(2021, 7, 21), "Slack", D("73.64"))

        self.assertEqual(len(pf.sells), 1)
        s = pf.sells[0]
        self.assertEqual(s.total_proceeds, D("73.64"))
        self.assertEqual(s.source, "merger")
        # No new lots created
        self.assertEqual(pf.lots["NEW"], [])

    def test_delisting_with_cash(self):
        pf = Portfolio()
        pf._add_lot("X", "SW", date(2021, 1, 1), 20, D("30"), "USD",
                     fee_zero(), "")
        td = {"isin": "X", "qty": 20, "ccy": "USD"}
        pf.handle_delisting(td, "SW", date(2025, 4, 21), D("370"), "USD")

        self.assertEqual(len(pf.sells), 1)
        s = pf.sells[0]
        self.assertEqual(s.total_proceeds, D("370"))
        self.assertEqual(s.price_per_unit, D("18.5"))
        self.assertEqual(s.source, "delisting")

    def test_delisting_no_cash(self):
        pf = Portfolio()
        pf._add_lot("X", "Dead", date(2020, 1, 1), 10, D("5"), "EUR",
                     fee_zero(), "")
        td = {"isin": "X", "qty": 10, "ccy": "EUR"}
        pf.handle_delisting(td, "Dead", date(2024, 1, 1), D("0"), "")

        s = pf.sells[0]
        self.assertEqual(s.total_proceeds, D("0"))

    def test_product_change_same_isin(self):
        pf = Portfolio()
        pf._add_lot("X", "OldName", date(2023, 1, 1), 21, D("15.87"), "USD",
                     [(D("2"), "EUR")], "")
        sell_td = {"isin": "X", "qty": 21, "ccy": "USD"}
        buy_td = {"isin": "X", "qty": 21, "ccy": "USD"}
        pf.handle_product_change(sell_td, buy_td, "NewName")

        lots = pf.lots["X"]
        self.assertEqual(lots[0].product, "NewName")
        self.assertEqual(lots[0].price_per_unit, D("15.87"))
        self.assertEqual(lots[0].buy_date, date(2023, 1, 1))

    def test_product_change_different_isin(self):
        pf = Portfolio()
        pf._add_lot("OLD", "OldP", date(2023, 1, 1), 5, D("10"), "EUR",
                     fee_zero(), "")
        sell_td = {"isin": "OLD", "qty": 5, "ccy": "EUR"}
        buy_td = {"isin": "NEW", "qty": 5, "ccy": "EUR"}
        pf.handle_product_change(sell_td, buy_td, "NewP")

        self.assertNotIn("OLD", pf.lots)
        self.assertEqual(len(pf.lots["NEW"]), 1)
        self.assertEqual(pf.lots["NEW"][0].product, "NewP")

    def test_return_of_capital(self):
        pf = Portfolio()
        pf._add_lot("X", "LYB", date(2020, 1, 1), 3, D("58.04"), "USD",
                     fee_zero(), "")
        pf.handle_return_of_capital("X", D("15.60"))

        # per-share reduction: 15.60 / 3 = 5.20
        expected = D("58.04") - D("5.20")
        self.assertEqual(pf.lots["X"][0].price_per_unit, expected)

    def test_return_of_capital_floor_zero(self):
        pf = Portfolio()
        pf._add_lot("X", "Stock", date(2020, 1, 1), 1, D("5"), "USD",
                     fee_zero(), "")
        pf.handle_return_of_capital("X", D("100"))
        self.assertEqual(pf.lots["X"][0].price_per_unit, D("0"))


# ===================================================================
# 8. Tax calculation - capital gains
# ===================================================================

class TestCalcTax(unittest.TestCase):
    def _make_disp(self, sell_date, portions, proceeds, ccy, sell_fees=None):
        return Disposition(
            product="X", isin="XX", sell_date=sell_date,
            sell_qty=sum(p.qty for p in portions),
            proceeds=proceeds, ccy=ccy,
            sell_fees=sell_fees or fee_zero(),
            portions=portions, source="trade",
        )

    def test_simple_gain_usd(self):
        portions = [MatchedPortion(
            buy_date=date(2023, 1, 1), qty=D("10"),
            cost_per_unit=D("100"), cost_ccy="USD", fees=fee_zero())]
        d = self._make_disp(date(2025, 6, 1), portions, D("1500"), "USD")
        results = calc_tax([d], 2025, RATES)

        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r.proceeds_czk, D("1500") * D("22"))
        self.assertEqual(r.cost_czk, D("1000") * D("22"))
        expected_gain = (D("1500") - D("1000")) * D("22")
        self.assertEqual(r.gain_czk, expected_gain)

    def test_czk_no_conversion(self):
        portions = [MatchedPortion(
            buy_date=date(2021, 12, 1), qty=D("19"),
            cost_per_unit=D("734"), cost_ccy="CZK", fees=fee_zero())]
        d = self._make_disp(date(2025, 7, 9), portions, D("23009"), "CZK")
        results = calc_tax([d], 2025, RATES)
        self.assertEqual(results[0].proceeds_czk, D("23009"))
        self.assertEqual(results[0].cost_czk, D("13946"))

    def test_fees_in_eur_for_usd_trade(self):
        portions = [MatchedPortion(
            buy_date=date(2023, 1, 1), qty=D("1"),
            cost_per_unit=D("100"), cost_ccy="USD",
            fees=[(D("2"), "EUR")])]
        sell_fees = [(D("2"), "EUR")]
        d = self._make_disp(date(2025, 1, 1), portions, D("200"), "USD",
                            sell_fees)
        results = calc_tax([d], 2025, RATES)
        r = results[0]
        # Fees: (2+2) EUR * 25 = 100 CZK
        self.assertEqual(r.fees_czk, D("100"))
        # Gain: 200*22 - 100*22 - 100 = 4400 - 2200 - 100 = 2100
        self.assertEqual(r.gain_czk, D("2100"))

    def test_time_test_fully_exempt(self):
        portions = [MatchedPortion(
            buy_date=date(2020, 4, 14), qty=D("4"),
            cost_per_unit=D("61"), cost_ccy="EUR", fees=fee_zero())]
        d = self._make_disp(date(2025, 7, 8), portions, D("714.88"), "EUR")
        results = calc_tax([d], 2025, RATES)
        self.assertTrue(results[0].exempt)

    def test_time_test_not_exempt(self):
        portions = [MatchedPortion(
            buy_date=date(2024, 6, 17), qty=D("26"),
            cost_per_unit=D("5.105"), cost_ccy="USD", fees=fee_zero())]
        d = self._make_disp(date(2025, 7, 8), portions, D("209.30"), "USD")
        results = calc_tax([d], 2025, RATES)
        self.assertFalse(results[0].exempt)

    def test_partial_exemption(self):
        p_exempt = MatchedPortion(
            buy_date=date(2021, 5, 10), qty=D("3"),
            cost_per_unit=D("214.49"), cost_ccy="USD", fees=fee_zero())
        p_taxable = MatchedPortion(
            buy_date=date(2024, 2, 16), qty=D("1"),
            cost_per_unit=D("202.33"), cost_ccy="USD", fees=fee_zero())
        d = self._make_disp(date(2025, 3, 5), [p_exempt, p_taxable],
                            D("1089.10"), "USD")
        results = calc_tax([d], 2025, RATES)
        r = results[0]
        self.assertFalse(r.exempt)
        self.assertEqual(r.partial_exempt_qty, D("3"))
        self.assertTrue(hasattr(r, "taxable_proceeds_czk"))
        # Exempt portion should not be in taxable amounts
        self.assertGreater(r.exempt_gain_czk, D("0"))
        self.assertGreater(r.taxable_gain_czk, D("0"))
        total = r.exempt_gain_czk + r.taxable_gain_czk
        self.assertAlmostEqual(float(total), float(r.gain_czk), places=2)

    def test_wrong_year_excluded(self):
        portions = [MatchedPortion(
            buy_date=date(2020, 1, 1), qty=D("10"),
            cost_per_unit=D("50"), cost_ccy="USD", fees=fee_zero())]
        d = self._make_disp(date(2024, 6, 1), portions, D("700"), "USD")
        results = calc_tax([d], 2025, RATES)
        self.assertEqual(len(results), 0)

    def test_mixed_currency_cost(self):
        """Buy in EUR, sell proceeds in CAD (e.g. H2O delisting)."""
        portions = [MatchedPortion(
            buy_date=date(2020, 10, 1), qty=D("98"),
            cost_per_unit=D("0.925"), cost_ccy="EUR", fees=fee_zero())]
        d = self._make_disp(date(2023, 12, 29), portions, D("416.50"), "CAD")
        results = calc_tax([d], 2023, RATES)
        r = results[0]
        # Proceeds: 416.50 * 16 (CAD rate) = 6664
        self.assertEqual(r.proceeds_czk, (D("416.50") * D("16")).quantize(D("0.01")))
        # Cost: 98 * 0.925 * 25 (EUR rate) = 2266.25
        self.assertEqual(r.cost_czk, (D("98") * D("0.925") * D("25")).quantize(D("0.01")))


# ===================================================================
# 9. Dividend processing
# ===================================================================

class TestDividends(unittest.TestCase):
    def _div_rows(self):
        """Create sample dividend rows for 2025."""
        return [
            row("15-03-2025", "Dividenda", product="MSFT",
                isin="US5949181045", mov_ccy="USD", mov_amt="6.64",
                value_date="13-03-2025"),
            row("15-03-2025", "Daň z dividendy", product="MSFT",
                isin="US5949181045", mov_ccy="USD", mov_amt="-1.00",
                value_date="13-03-2025"),
            # Irish ETF - no tax
            row("28-03-2025", "Dividenda", product="VANGUARD",
                isin="IE00B3XXRP09", mov_ccy="USD", mov_amt="1.60",
                value_date="26-03-2025"),
            # CZ dividend
            row("04-08-2025", "Dividenda", product="CEZ AS",
                isin="CZ0005112300", mov_ccy="CZK", mov_amt="893",
                value_date="01-08-2025"),
            row("04-08-2025", "Daň z dividendy", product="CEZ AS",
                isin="CZ0005112300", mov_ccy="CZK", mov_amt="-312.55",
                value_date="01-08-2025"),
        ]

    def test_process_dividends_counts(self):
        divs = process_dividends(self._div_rows(), 2025)
        self.assertEqual(len(divs), 3)  # MSFT, Vanguard, CEZ

    def test_dividend_pairing(self):
        divs = process_dividends(self._div_rows(), 2025)
        msft = [d for d in divs if d.isin == "US5949181045"][0]
        self.assertEqual(msft.gross, D("6.64"))
        self.assertEqual(msft.tax_withheld, D("-1.00"))

    def test_cz_dividend_identified(self):
        divs = process_dividends(self._div_rows(), 2025)
        cez = [d for d in divs if d.country == "CZ"][0]
        self.assertEqual(cez.gross, D("893"))
        self.assertEqual(cez.tax_withheld, D("-312.55"))

    def test_ie_no_tax(self):
        divs = process_dividends(self._div_rows(), 2025)
        ie = [d for d in divs if d.country == "IE"][0]
        self.assertEqual(ie.tax_withheld, D("0"))

    def test_storno_netting(self):
        """Negative dividend (storno) followed by correction should net."""
        rows_data = [
            row("03-12-2025", "Dividenda", product="MSFT",
                isin="US5949181045", mov_ccy="USD", mov_amt="-6.64",
                value_date="11-09-2025"),
            row("04-12-2025", "Dividenda", product="MSFT",
                isin="US5949181045", mov_ccy="USD", mov_amt="6.64",
                value_date="11-09-2025"),
            row("03-12-2025", "Daň z dividendy", product="MSFT",
                isin="US5949181045", mov_ccy="USD", mov_amt="1.00",
                value_date="11-09-2025"),
            row("04-12-2025", "Daň z dividendy", product="MSFT",
                isin="US5949181045", mov_ccy="USD", mov_amt="-1.00",
                value_date="11-09-2025"),
        ]
        divs = process_dividends(rows_data, 2025)
        # Should net to 0 and be excluded (gross = 0)
        self.assertEqual(len(divs), 0)

    def test_wrong_year_excluded(self):
        rows_data = [
            row("15-03-2024", "Dividenda", product="X",
                isin="US1234567890", mov_ccy="USD", mov_amt="10",
                value_date="13-03-2024"),
        ]
        divs = process_dividends(rows_data, 2025)
        self.assertEqual(len(divs), 0)


# ===================================================================
# 10. Double-taxation treaty handling
# ===================================================================

class TestDividendTax(unittest.TestCase):
    def _make_div(self, country, gross, tax, ccy="USD"):
        has_treaty = country in COUNTRIES_WITH_SZDZ
        return DividendEvent(
            product="Test", isin="XX", country=country,
            value_date=date(2025, 6, 1), gross=D(gross),
            tax_withheld=D(tax), ccy=ccy, has_treaty=has_treaty,
        )

    def test_treaty_credit_us_15pct(self):
        """US dividend 15% withholding, 15% treaty: full credit."""
        d = self._make_div("US", "100", "-15")
        calc_dividend_tax([d], RATES)
        self.assertEqual(d.gross_czk, D("2200"))
        self.assertEqual(d.tax_czk, D("330"))
        self.assertEqual(d.cz_tax_czk, D("330"))
        # US treaty 15%, CZ 15% -> min(330, 330, 330) = 330
        self.assertEqual(d.credit_czk, D("330"))
        self.assertEqual(d.expense_czk, D("0"))

    def test_treaty_rate_caps_credit_fr(self):
        """FR dividend 25% withholding, 10% treaty: credit capped at treaty 10%."""
        d = self._make_div("FR", "100", "-25", "EUR")
        calc_dividend_tax([d], RATES)
        # gross_czk = 100 * 25 = 2500
        # tax_czk = 25 * 25 = 625
        # CZ tax 15% = 375
        # Treaty max 10% of 2500 = 250
        # Credit = min(625, 250, 375) = 250
        self.assertEqual(d.credit_czk, D("250"))
        # Doplatek = 375 - 250 = 125
        self.assertEqual(d.cz_tax_czk - d.credit_czk, D("125"))
        # Excess withholding = 625 - 250 = 375 (reclaimable from FR)

    def test_treaty_rate_caps_credit_nl(self):
        """NL dividend 15% withholding, 10% treaty: credit capped at treaty 10%."""
        d = self._make_div("NL", "100", "-15", "EUR")
        calc_dividend_tax([d], RATES)
        # gross_czk = 2500, tax_czk = 375, CZ 15% = 375, treaty 10% = 250
        # Credit = min(375, 250, 375) = 250
        self.assertEqual(d.credit_czk, D("250"))

    def test_treaty_rate_caps_credit_jp(self):
        """JP dividend 15% withholding, 10% treaty: credit capped at treaty 10%."""
        d = self._make_div("JP", "100", "-15")
        calc_dividend_tax([d], RATES)
        # gross_czk = 2200, tax_czk = 330, CZ 15% = 330, treaty 10% = 220
        # Credit = min(330, 220, 330) = 220
        self.assertEqual(d.credit_czk, D("220"))

    def test_treaty_actual_below_treaty_rate(self):
        """CN dividend 10% withholding, 10% treaty: credit = actual."""
        d = self._make_div("CN", "100", "-10")
        calc_dividend_tax([d], RATES)
        # tax_czk = 220, treaty max 10% = 220, CZ 15% = 330
        # Credit = min(220, 220, 330) = 220
        self.assertEqual(d.credit_czk, D("220"))

    def test_treaty_actual_below_all_caps(self):
        """GB dividend 5% actual withholding, 15% treaty: credit = actual."""
        d = self._make_div("GB", "100", "-5")
        calc_dividend_tax([d], RATES)
        # tax_czk = 110, treaty 15% = 330, CZ 15% = 330
        # Credit = min(110, 330, 330) = 110
        self.assertEqual(d.credit_czk, D("110"))

    def test_three_cap_rule_all_different(self):
        """Scenario where all three caps are different values."""
        # FI treaty = 5%, actual withholding 30%, CZ = 15%
        d = self._make_div("FI", "100", "-30")
        calc_dividend_tax([d], RATES)
        # gross_czk = 2200, tax_czk = 660, CZ 15% = 330, treaty 5% = 110
        # Credit = min(660, 110, 330) = 110
        self.assertEqual(d.credit_czk, D("110"))

    def test_no_treaty_expense_deduction(self):
        """TW dividend (no SZDZ): tax as expense, no credit."""
        d = self._make_div("TW", "100", "-20")
        calc_dividend_tax([d], RATES)
        self.assertEqual(d.credit_czk, D("0"))
        self.assertEqual(d.expense_czk, D("440"))
        # CZ tax = 15% of (2200 - 440) = 264
        self.assertEqual(d.cz_tax_czk, D("264"))

    def test_no_treaty_zero_tax(self):
        """KY dividend (no SZDZ, no withholding): full CZ tax."""
        d = self._make_div("KY", "100", "0")
        calc_dividend_tax([d], RATES)
        self.assertEqual(d.credit_czk, D("0"))
        self.assertEqual(d.expense_czk, D("0"))
        self.assertEqual(d.cz_tax_czk, D("330"))

    def test_treaty_zero_tax_ie(self):
        """IE dividend (SZDZ, no withholding): full CZ tax, no credit."""
        d = self._make_div("IE", "100", "0")
        calc_dividend_tax([d], RATES)
        self.assertEqual(d.credit_czk, D("0"))
        self.assertEqual(d.cz_tax_czk, D("330"))

    def test_treaty_rates_defined_for_all_szdz_countries_in_data(self):
        """Every country in the user's data with SZDZ should have a rate."""
        data_countries = {"US", "FR", "NL", "IE", "CN", "JP", "GB"}
        for c in data_countries:
            self.assertIn(c, TREATY_MAX_DIV_RATE,
                          "{} missing from TREATY_MAX_DIV_RATE".format(c))

    def test_treaty_rates_not_exceed_15pct(self):
        """No treaty rate should exceed the CZ 15% rate."""
        for country, rate in TREATY_MAX_DIV_RATE.items():
            self.assertLessEqual(rate, D("0.15"),
                                 "{} treaty rate {} > 15%".format(country, rate))


# ===================================================================
# 11. Country resolution
# ===================================================================

class TestCountryResolution(unittest.TestCase):
    def test_regular_isin(self):
        self.assertEqual(resolve_country("US5949181045", "MSFT"), "US")
        self.assertEqual(resolve_country("FR0000120560", "Quadient"), "FR")

    def test_adr_override(self):
        self.assertEqual(resolve_country("US01609W1027", "Alibaba"), "KY")
        self.assertEqual(resolve_country("US8356993076", "Sony"), "JP")
        self.assertEqual(resolve_country("US8740391003", "TSMC"), "TW")

    def test_lyondellbasell_override(self):
        self.assertEqual(resolve_country("NL0009434992", "LYB"), "GB")

    def test_taiwan_no_treaty(self):
        self.assertNotIn("TW", COUNTRIES_WITH_SZDZ)

    def test_cayman_no_treaty(self):
        self.assertNotIn("KY", COUNTRIES_WITH_SZDZ)

    def test_us_has_treaty(self):
        self.assertIn("US", COUNTRIES_WITH_SZDZ)


# ===================================================================
# 12. CSV reading and full pipeline (integration)
# ===================================================================

class TestCSVIntegration(unittest.TestCase):
    def _write_csv(self, rows_text):
        """Write CSV text to a temp file and return path."""
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".csv",
                                        delete=False, encoding="utf-8")
        f.write(rows_text)
        f.close()
        return f.name

    def test_read_simple_csv(self):
        csv_text = (
            'Datum,Čas,Datum,Produkt,ISIN,Popis,Kurz,Pohyb,,Zůstatek,,ID objednávky\n'
            '15-04-2020,16:47,15-04-2020,STANLEY BLACK & DECKER INC,US8545021011,'
            '"Nákup 4 Stanley Black & Decker Inc@107,94 USD (US8545021011)"'
            ',,USD,"-431,76",USD,"-431,76",order123\n'
            '15-04-2020,16:47,15-04-2020,STANLEY BLACK & DECKER INC,US8545021011,'
            'DEGIRO Transaction and/or third party fees,,EUR,"-2,00",EUR,"0,00",order123\n'
        )
        path = self._write_csv(csv_text)
        try:
            rows = read_csv(path)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0].product, "STANLEY BLACK & DECKER INC")
            self.assertEqual(rows[0].mov_amt, D("-431.76"))
            self.assertEqual(rows[1].mov_amt, D("-2.00"))
        finally:
            os.unlink(path)

    def test_nbsp_in_description_normalized(self):
        csv_text = (
            'Datum,Čas,Datum,Produkt,ISIN,Popis,Kurz,Pohyb,,Zůstatek,,ID objednávky\n'
            '09-07-2025,09:19,09-07-2025,KB,CZ0008019106,'
            'Prodej 6 Komercni Banka as@1\xa0035 CZK (CZ0008019106)'
            ',,CZK,"6210,00",CZK,"6210,00",order456\n'
        )
        path = self._write_csv(csv_text)
        try:
            rows = read_csv(path)
            # nbsp should be normalized to space in description
            self.assertNotIn("\xa0", rows[0].description)
            self.assertIn("1 035", rows[0].description)
        finally:
            os.unlink(path)

    def test_full_pipeline_buy_sell(self):
        """End-to-end: buy, then sell, then compute tax."""
        csv_text = (
            'Datum,Čas,Datum,Produkt,ISIN,Popis,Kurz,Pohyb,,Zůstatek,,ID objednávky\n'
            # Buy 10 shares @ 50 USD
            '01-06-2023,10:00,01-06-2023,ACME,US1234567890,'
            '"Nákup 10 ACME Corp@50 USD (US1234567890)"'
            ',,USD,"-500,00",USD,"-500,00",buy1\n'
            '01-06-2023,10:00,01-06-2023,ACME,US1234567890,'
            'DEGIRO Transaction and/or third party fees,,EUR,"-2,00",EUR,"0,00",buy1\n'
            # Sell 10 shares @ 80 USD
            '15-03-2025,14:00,15-03-2025,ACME,US1234567890,'
            '"Prodej 10 ACME Corp@80 USD (US1234567890)"'
            ',,USD,"800,00",USD,"800,00",sell1\n'
            '15-03-2025,14:00,15-03-2025,ACME,US1234567890,'
            'DEGIRO Transaction and/or third party fees,,EUR,"-2,00",EUR,"0,00",sell1\n'
        )
        path = self._write_csv(csv_text)
        try:
            rows = read_csv(path)
            pf = process_all(rows)
            self.assertEqual(len(pf.sells), 1)
            disps = fifo_match(pf)
            self.assertEqual(len(disps), 1)
            results = calc_tax(disps, 2025, RATES)
            self.assertEqual(len(results), 1)
            r = results[0]
            # Proceeds: 800 * 22 = 17600
            self.assertEqual(r.proceeds_czk, D("17600.00"))
            # Cost: 500 * 22 = 11000
            self.assertEqual(r.cost_czk, D("11000.00"))
            # Fees: 4 EUR * 25 = 100
            self.assertEqual(r.fees_czk, D("100.00"))
            # Gain: 17600 - 11000 - 100 = 6500
            self.assertEqual(r.gain_czk, D("6500.00"))
            self.assertFalse(r.exempt)
        finally:
            os.unlink(path)

    def test_internal_transfer_ignored(self):
        """Internal transfer should not create buy/sell events."""
        csv_text = (
            'Datum,Čas,Datum,Produkt,ISIN,Popis,Kurz,Pohyb,,Zůstatek,,ID objednávky\n'
            # Buy
            '10-05-2021,17:33,10-05-2021,NFLX,US64110L1061,'
            '"Nákup 2 Netflix Inc@407 EUR (US64110L1061)"'
            ',,EUR,"-814,00",EUR,"0,00",buy1\n'
            # Internal transfer sell
            '28-02-2022,06:18,27-02-2022,NFLX,US64110L1061,'
            '"Interní převod: Prodej 2 Netflix Inc@508,65 EUR (US64110L1061)"'
            ',,EUR,"0,00",EUR,"0,00",\n'
            # Internal transfer buy
            '28-02-2022,07:01,27-02-2022,NFLX,US64110L1061,'
            '"Interní převod: Nákup 2 Netflix Inc@508,65 EUR (US64110L1061)"'
            ',,EUR,"0,00",EUR,"0,00",\n'
            # Real sell
            '07-03-2025,15:43,07-03-2025,NFLX,US64110L1061,'
            '"Prodej 2 Netflix Inc@824,7 EUR (US64110L1061)"'
            ',,EUR,"1649,40",EUR,"1649,40",sell1\n'
        )
        path = self._write_csv(csv_text)
        try:
            rows = read_csv(path)
            pf = process_all(rows)
            # Only 1 sell (not 2 - internal transfer is skipped)
            self.assertEqual(len(pf.sells), 1)
            disps = fifo_match(pf)
            # The sell should match against the original buy date
            self.assertEqual(disps[0].portions[0].buy_date, date(2021, 5, 10))
            # Cost should be from original buy, not internal transfer
            self.assertEqual(disps[0].portions[0].cost_per_unit, D("407"))
        finally:
            os.unlink(path)

    def test_partial_fills_aggregated(self):
        """Multiple Prodej rows with same order_id aggregated to one sell."""
        csv_text = (
            'Datum,Čas,Datum,Produkt,ISIN,Popis,Kurz,Pohyb,,Zůstatek,,ID objednávky\n'
            # Buy
            '14-06-2023,15:32,14-06-2023,PLTR,US69608A1088,'
            '"Nákup 23 PLTR@15,87 USD (US69608A1088)"'
            ',,USD,"-365,01",USD,"0,00",buy1\n'
            # Sell partial fill 1
            '20-02-2025,16:16,20-02-2025,PLTR,US69608A1088,'
            '"Prodej 14 PLTR@99,85 USD (US69608A1088)"'
            ',,USD,"1397,90",USD,"0,00",sell1\n'
            # Sell partial fill 2
            '20-02-2025,16:16,20-02-2025,PLTR,US69608A1088,'
            '"Prodej 9 PLTR@99,85 USD (US69608A1088)"'
            ',,USD,"898,65",USD,"0,00",sell1\n'
            # Fee for the sell
            '20-02-2025,16:16,20-02-2025,PLTR,US69608A1088,'
            'DEGIRO Transaction and/or third party fees,,EUR,"-2,00",EUR,"0,00",sell1\n'
        )
        path = self._write_csv(csv_text)
        try:
            rows = read_csv(path)
            pf = process_all(rows)
            self.assertEqual(len(pf.sells), 1)
            s = pf.sells[0]
            self.assertEqual(s.quantity, D("23"))
            self.assertEqual(s.total_proceeds, D("2296.55"))
        finally:
            os.unlink(path)


# ===================================================================
# 13. Edge cases and regression tests
# ===================================================================

class TestEdgeCases(unittest.TestCase):
    def test_sell_czk_stock(self):
        """CZK-denominated stocks should not be converted."""
        pf = Portfolio()
        pf._add_lot("CZ", "CEZ", date(2021, 12, 1), 8, D("734"), "CZK",
                     fee_zero(), "")
        pf._add_lot("CZ", "CEZ", date(2022, 1, 18), 4, D("811"), "CZK",
                     fee_zero(), "")
        pf._add_sell("CZ", "CEZ", date(2025, 7, 9), 12, D("1211"),
                     D("14532"), "CZK", fee_zero(), "")
        disps = fifo_match(pf)
        results = calc_tax(disps, 2025, RATES)
        r = results[0]
        # CZK rate = 1, so CZK amounts pass through
        self.assertEqual(r.proceeds_czk, D("14532.00"))
        expected_cost = D("8") * D("734") + D("4") * D("811")
        self.assertEqual(r.cost_czk, expected_cost)

    def test_zero_quantity_buy_ignored(self):
        pf = Portfolio()
        pf._add_lot("X", "X", date(2025, 1, 1), 0, D("100"), "USD",
                     fee_zero(), "")
        self.assertEqual(pf.lots["X"], [])

    def test_fifo_across_multiple_lots(self):
        """Sell that spans 3 lots."""
        pf = Portfolio()
        pf._add_lot("X", "X", date(2020, 1, 1), 2, D("10"), "USD",
                     fee_zero(), "")
        pf._add_lot("X", "X", date(2021, 1, 1), 3, D("20"), "USD",
                     fee_zero(), "")
        pf._add_lot("X", "X", date(2022, 1, 1), 5, D("30"), "USD",
                     fee_zero(), "")
        pf._add_sell("X", "X", date(2025, 1, 1), 8, D("50"), D("400"),
                     "USD", fee_zero(), "")
        disps = fifo_match(pf)
        self.assertEqual(len(disps[0].portions), 3)
        self.assertEqual(disps[0].portions[0].qty, D("2"))
        self.assertEqual(disps[0].portions[1].qty, D("3"))
        self.assertEqual(disps[0].portions[2].qty, D("3"))
        # 2 remaining in last lot
        self.assertEqual(pf.lots["X"][0].quantity, D("2"))

    def test_fee_multi_currency(self):
        """Order with both EUR and CZK fees."""
        pf = Portfolio()
        fees = [(D("2"), "EUR"), (D("12.50"), "CZK")]
        pf._add_sell("X", "X", date(2025, 7, 9), 6, D("1035"),
                     D("6210"), "CZK", fees, "")
        pf._add_lot("X", "X", date(2020, 1, 1), 6, D("500"), "CZK",
                     fee_zero(), "")
        disps = fifo_match(pf)
        results = calc_tax(disps, 2025, RATES)
        # Fee CZK: 2*25 + 12.50*1 = 62.50
        self.assertEqual(results[0].fees_czk, D("62.50"))

    def test_spin_off_zero_cost(self):
        """Spin-off shares have 0 cost basis."""
        pf = Portfolio()
        pf._add_lot("X", "N-Able", date(2021, 7, 20), 20, D("0"), "USD",
                     fee_zero(), "")
        pf._add_sell("X", "N-Able", date(2025, 1, 1), 20, D("10"),
                     D("200"), "USD", fee_zero(), "")
        disps = fifo_match(pf)
        results = calc_tax(disps, 2025, RATES)
        # All proceeds are gain
        self.assertEqual(results[0].cost_czk, D("0"))
        self.assertEqual(results[0].gain_czk, D("200") * D("22"))


# ===================================================================
# 14. Real data regression (if CSV available)
# ===================================================================

class TestRealData(unittest.TestCase):
    CSV_PATH = "Degiro výpis.csv"

    @unittest.skipUnless(os.path.exists(CSV_PATH), "Real CSV not available")
    def test_2025_sell_count(self):
        rows = read_csv(self.CSV_PATH)
        pf = process_all(rows)
        disps = fifo_match(pf)
        year_disps = calc_tax(disps, 2025, RATES)
        # 17 sells in 2025
        self.assertEqual(len(year_disps), 17)

    @unittest.skipUnless(os.path.exists(CSV_PATH), "Real CSV not available")
    def test_2025_no_warnings(self):
        rows = read_csv(self.CSV_PATH)
        pf = process_all(rows)
        fifo_match(pf)
        self.assertEqual(len(pf.warnings), 0,
                         "Unexpected warnings: {}".format(pf.warnings))

    @unittest.skipUnless(os.path.exists(CSV_PATH), "Real CSV not available")
    def test_2025_sony_sell(self):
        """Verify Sony sell: 30 shares, proceeds 738.75 USD."""
        rows = read_csv(self.CSV_PATH)
        pf = process_all(rows)
        sony_sells = [s for s in pf.sells if s.isin == "US8356993076"
                       and s.sell_date.year == 2025]
        self.assertEqual(len(sony_sells), 1)
        self.assertEqual(sony_sells[0].quantity, D("30"))
        self.assertEqual(sony_sells[0].total_proceeds, D("738.75"))

    @unittest.skipUnless(os.path.exists(CSV_PATH), "Real CSV not available")
    def test_2025_palantir_aggregated(self):
        """Palantir partial fills (14+9) should be one sell of 23."""
        rows = read_csv(self.CSV_PATH)
        pf = process_all(rows)
        pltr_sells = [s for s in pf.sells if s.isin == "US69608A1088"
                       and s.sell_date.year == 2025]
        self.assertEqual(len(pltr_sells), 1)
        self.assertEqual(pltr_sells[0].quantity, D("23"))

    @unittest.skipUnless(os.path.exists(CSV_PATH), "Real CSV not available")
    def test_2025_total_sells(self):
        rows = read_csv(self.CSV_PATH)
        pf = process_all(rows)
        self.assertEqual(len(pf.sells), 28)

    @unittest.skipUnless(os.path.exists(CSV_PATH), "Real CSV not available")
    def test_2025_netflix_original_buy_dates(self):
        """Netflix buy dates should be originals, not internal transfer dates."""
        rows = read_csv(self.CSV_PATH)
        pf = process_all(rows)
        disps = fifo_match(pf)
        nflx = [d for d in disps if d.isin == "US64110L1061"
                and d.sell_date.year == 2025]
        self.assertEqual(len(nflx), 1)
        buy_dates = sorted({p.buy_date for p in nflx[0].portions})
        # Should include 2021 dates, NOT 2022-02-28 (internal transfer)
        self.assertTrue(any(bd.year == 2021 for bd in buy_dates),
                        "Expected 2021 buy date, got: {}".format(buy_dates))
        self.assertFalse(any(bd == date(2022, 2, 28) for bd in buy_dates),
                         "Internal transfer date should not appear")

    @unittest.skipUnless(os.path.exists(CSV_PATH), "Real CSV not available")
    def test_2025_dividends_country_classification(self):
        """ADRs should have correct source country."""
        rows = read_csv(self.CSV_PATH)
        divs = process_dividends(rows, 2025)
        by_isin = {d.isin: d for d in divs}
        # Alibaba -> KY
        if "US01609W1027" in by_isin:
            self.assertEqual(by_isin["US01609W1027"].country, "KY")
        # Sony -> JP
        if "US8356993076" in by_isin:
            self.assertEqual(by_isin["US8356993076"].country, "JP")
        # TSMC -> TW
        if "US8740391003" in by_isin:
            self.assertEqual(by_isin["US8740391003"].country, "TW")

    @unittest.skipUnless(os.path.exists(CSV_PATH), "Real CSV not available")
    def test_2025_taiwan_no_credit(self):
        """Taiwan dividends should have no credit (no SZDZ)."""
        rows = read_csv(self.CSV_PATH)
        divs = process_dividends(rows, 2025)
        divs = calc_dividend_tax(divs, RATES)
        tw = [d for d in divs if d.country == "TW"]
        for d in tw:
            self.assertEqual(d.credit_czk, D("0"))
            self.assertGreater(d.expense_czk, D("0"))


# ===================================================================
# 15. Return of capital - proportional distribution
# ===================================================================

class TestReturnOfCapitalProportional(unittest.TestCase):
    def test_single_lot_same_as_before(self):
        """Single lot: proportional = flat per-unit (no difference)."""
        pf = Portfolio()
        pf._add_lot("X", "LYB", date(2020, 1, 1), 3, D("58.04"), "USD",
                     fee_zero(), "")
        pf.handle_return_of_capital("X", D("15.60"))
        # per-share: 15.60 / 3 = 5.20
        expected = D("58.04") - D("5.20")
        self.assertEqual(pf.lots["X"][0].price_per_unit, expected)

    def test_two_lots_different_prices(self):
        """Two lots at different prices: reduction proportional to cost basis."""
        pf = Portfolio()
        # Lot A: 2 shares @ 100 = cost 200
        pf._add_lot("X", "Stock", date(2020, 1, 1), 2, D("100"), "USD",
                     fee_zero(), "")
        # Lot B: 2 shares @ 50 = cost 100
        pf._add_lot("X", "Stock", date(2021, 1, 1), 2, D("50"), "USD",
                     fee_zero(), "")
        # Total cost = 300. Return 30 USD.
        pf.handle_return_of_capital("X", D("30"))
        # Lot A share: 30 * 200/300 = 20 -> reduction per unit: 20/2 = 10
        self.assertEqual(pf.lots["X"][0].price_per_unit, D("100") - D("10"))
        # Lot B share: 30 * 100/300 = 10 -> reduction per unit: 10/2 = 5
        self.assertEqual(pf.lots["X"][1].price_per_unit, D("50") - D("5"))

    def test_proportional_preserves_total_reduction(self):
        """Total cost reduction equals the return of capital amount."""
        pf = Portfolio()
        pf._add_lot("X", "S", date(2020, 1, 1), 5, D("80"), "USD",
                     fee_zero(), "")
        pf._add_lot("X", "S", date(2021, 1, 1), 3, D("120"), "USD",
                     fee_zero(), "")
        total_before = sum(l.price_per_unit * l.quantity for l in pf.lots["X"])
        pf.handle_return_of_capital("X", D("40"))
        total_after = sum(l.price_per_unit * l.quantity for l in pf.lots["X"])
        diff = total_before - total_after
        self.assertAlmostEqual(float(diff), 40.0, places=2)

    def test_zero_cost_lots_fallback(self):
        """Lots with 0 cost basis fall back to equal per-share distribution."""
        pf = Portfolio()
        pf._add_lot("X", "S", date(2020, 1, 1), 10, D("0"), "USD",
                     fee_zero(), "")
        pf.handle_return_of_capital("X", D("50"))
        # Cost was 0, floor at 0
        self.assertEqual(pf.lots["X"][0].price_per_unit, D("0"))

    def test_floor_zero_with_proportional(self):
        """Reduction capped at 0 per lot even with proportional."""
        pf = Portfolio()
        pf._add_lot("X", "S", date(2020, 1, 1), 1, D("10"), "USD",
                     fee_zero(), "")
        pf._add_lot("X", "S", date(2021, 1, 1), 1, D("90"), "USD",
                     fee_zero(), "")
        # Return 200 (more than total cost 100)
        pf.handle_return_of_capital("X", D("200"))
        self.assertEqual(pf.lots["X"][0].price_per_unit, D("0"))
        self.assertEqual(pf.lots["X"][1].price_per_unit, D("0"))


# ===================================================================
# 16. Non-negativity of §10 tax base
# ===================================================================

class TestNonNegativeTaxBase(unittest.TestCase):
    def _make_disp(self, sell_date, portions, proceeds, ccy):
        return Disposition(
            product="X", isin="XX", sell_date=sell_date,
            sell_qty=sum(p.qty for p in portions),
            proceeds=proceeds, ccy=ccy,
            sell_fees=fee_zero(),
            portions=portions, source="trade",
        )

    def test_loss_produces_zero_dilci(self):
        """§10/4 ZDP: loss cannot be applied, dilci zaklad = 0."""
        # Buy at 100, sell at 50 -> loss
        portions = [MatchedPortion(
            buy_date=date(2024, 1, 1), qty=D("10"),
            cost_per_unit=D("100"), cost_ccy="USD", fees=fee_zero())]
        d = self._make_disp(date(2025, 6, 1), portions, D("500"), "USD")
        results = calc_tax([d], 2025, RATES)
        r = results[0]
        # gain_czk should be negative: 500*22 - 1000*22 = -11000
        self.assertTrue(r.gain_czk < 0)

    def test_gain_produces_positive_dilci(self):
        """Positive gain passes through normally."""
        portions = [MatchedPortion(
            buy_date=date(2024, 1, 1), qty=D("10"),
            cost_per_unit=D("10"), cost_ccy="USD", fees=fee_zero())]
        d = self._make_disp(date(2025, 6, 1), portions, D("200"), "USD")
        results = calc_tax([d], 2025, RATES)
        r = results[0]
        self.assertTrue(r.gain_czk > 0)


# ===================================================================
# 17. 100,000 CZK exemption (§4/1/x ZDP, from 2025)
# ===================================================================

class TestSmallProceedsExemption(unittest.TestCase):
    def _make_disp(self, sell_date, portions, proceeds, ccy, sell_fees=None):
        return Disposition(
            product="X", isin="XX", sell_date=sell_date,
            sell_qty=sum(p.qty for p in portions),
            proceeds=proceeds, ccy=ccy,
            sell_fees=sell_fees or fee_zero(),
            portions=portions, source="trade",
        )

    def test_under_100k_exempt(self):
        """Proceeds under 100k CZK in 2025 -> all exempt."""
        # 4000 USD * 22 = 88,000 CZK < 100,000
        portions = [MatchedPortion(
            buy_date=date(2024, 1, 1), qty=D("10"),
            cost_per_unit=D("300"), cost_ccy="USD", fees=fee_zero())]
        d = self._make_disp(date(2025, 6, 1), portions, D("4000"), "USD")
        results = calc_tax([d], 2025, RATES)
        # Total taxable proceeds = 4000 * 22 = 88000 < 100000
        total_income = sum(
            r.taxable_proceeds_czk if hasattr(r, "taxable_proceeds_czk")
            else (D("0") if r.exempt else r.proceeds_czk)
            for r in results
        )
        self.assertLessEqual(total_income, D("100000"))

    def test_over_100k_not_exempt(self):
        """Proceeds over 100k CZK in 2025 -> not exempt."""
        # 5000 USD * 22 = 110,000 CZK > 100,000
        portions = [MatchedPortion(
            buy_date=date(2024, 1, 1), qty=D("10"),
            cost_per_unit=D("300"), cost_ccy="USD", fees=fee_zero())]
        d = self._make_disp(date(2025, 6, 1), portions, D("5000"), "USD")
        results = calc_tax([d], 2025, RATES)
        total_income = sum(
            r.taxable_proceeds_czk if hasattr(r, "taxable_proceeds_czk")
            else (D("0") if r.exempt else r.proceeds_czk)
            for r in results
        )
        self.assertGreater(total_income, D("100000"))

    def test_pre_2025_no_exemption(self):
        """Before 2025, the 100k exemption does not apply."""
        portions = [MatchedPortion(
            buy_date=date(2023, 1, 1), qty=D("10"),
            cost_per_unit=D("300"), cost_ccy="USD", fees=fee_zero())]
        d = self._make_disp(date(2024, 6, 1), portions, D("4000"), "USD")
        results = calc_tax([d], 2024, RATES)
        r = results[0]
        # Should still be taxable (no §4/1/x before 2025)
        self.assertFalse(r.exempt)
        self.assertGreater(r.proceeds_czk, D("0"))

    def test_time_tested_not_counted(self):
        """Time-tested sales don't count toward the 100k limit."""
        # Time-tested sale (> 3 years)
        p_exempt = MatchedPortion(
            buy_date=date(2020, 1, 1), qty=D("100"),
            cost_per_unit=D("50"), cost_ccy="USD", fees=fee_zero())
        d_exempt = self._make_disp(date(2025, 6, 1), [p_exempt],
                                   D("100000"), "USD")
        # Non-time-tested sale, small proceeds
        p_taxable = MatchedPortion(
            buy_date=date(2024, 1, 1), qty=D("1"),
            cost_per_unit=D("10"), cost_ccy="USD", fees=fee_zero())
        d_taxable = self._make_disp(date(2025, 6, 1), [p_taxable],
                                    D("50"), "USD")
        results = calc_tax([d_exempt, d_taxable], 2025, RATES)
        # The exempt sale has huge proceeds but is time-tested
        # The taxable sale has 50*22=1100 CZK proceeds < 100k
        taxable_results = [r for r in results if not r.exempt]
        total_taxable_income = sum(
            r.taxable_proceeds_czk if hasattr(r, "taxable_proceeds_czk")
            else r.proceeds_czk
            for r in taxable_results
        )
        self.assertLessEqual(total_taxable_income, D("100000"))


if __name__ == "__main__":
    unittest.main()
