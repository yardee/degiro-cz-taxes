# Dane - Degiro Tax Calculator

Czech capital gains and dividend tax calculator for Degiro broker account statements.

## Project overview

Single-file Python tool (`dane_degiro.py`) that calculates capital gains tax (§10 ZDP) and dividend tax with double-taxation credit (§8 ZDP) for Czech tax returns from Degiro Account Statement CSV exports. No external dependencies - stdlib only (Python 3.9+).

### Usage

```bash
python3 dane_degiro.py "Degiro výpis.csv" <rok>
```

Output can be saved: `python3 dane_degiro.py "Degiro výpis.csv" 2025 > "Podklady FU 2025.txt" 2>&1`

## Key design decisions

- **FIFO** method for matching sells to buys (Czech tax law requirement)
- **Jednotny kurz CNB** (unified exchange rate) calculated automatically from CNB daily rates API - arithmetic average of last-day-of-month rates for Jan-Dec
- **3-year time test** (§4/1/w ZDP): per-lot check using calendar years, supports partial exemption (some lots exempt, some not within one sell)
- Fees stored as `list[tuple[Decimal, str]]` to handle multi-currency fees (EUR + CZK)
- All monetary math uses `Decimal` - no floats

## Input format

Degiro Account Statement CSV (Czech localization), comma-delimited, European decimal format. Must contain **complete history from account opening** for correct FIFO matching.

Header: `Datum,Cas,Datum,Produkt,ISIN,Popis,Kurz,Pohyb,,Zustatek,,ID objednavky`

## Supported transaction types

Classified from the `Popis` (description) column:

| Prefix/pattern | Classification | Handling |
|---|---|---|
| `Nakup N Product@Price CCY (ISIN)` | buy | Create FIFO lot |
| `Prodej N Product@Price CCY (ISIN)` | sell | FIFO match against lots |
| `Stock split: Nakup/Prodej` | stock_split | Adjust lots (same/different ISIN, reverse splits to 0) |
| `Zmena produktu: Nakup/Prodej` | product_change | Transfer lots, no tax event |
| `Merger: Nakup/Prodej` | merger | Sell old shares, optionally buy new |
| `Delisting: Prodej` | delisting | Sell at corporate action cash price |
| `Spin off: Nakup` | spin_off | New lot at cost 0 |
| `Rights issue: Nakup` | rights_issue | New lot at cost 0 |
| `Interni prevod: Nakup/Prodej` | skip | Internal transfer, no tax event |
| `Vratka kapitalu` | return_of_capital | Reduce cost basis proportionally |
| `DEGIRO Transaction and/or third party fees` | fee | Attach to order by order_id |
| `Korporatni akce hotovostni vyporadani akcie` | corp_action_cash | Cash from delisting |
| Dividenda, FX, transfers, fund conversions | skip | Not relevant for capital gains |

## Processing pipeline

### Capital gains (§10)
1. Parse CSV, normalize non-breaking spaces in descriptions
2. Pre-build: fees per order, corporate action cash, paired events (splits/mergers/changes)
3. Process chronologically (oldest first): build FIFO lots, handle corporate actions
4. FIFO match all sells against lots
5. Calculate CZK values using unified rate, apply time test per portion
6. Output detail + summary

### Dividends (§8)
1. Collect all "Dividenda" and "Daň z dividendy" rows for the tax year
2. Pair by (ISIN, value_date), net stornos (negative entries)
3. Separate CZ dividends (srážková daň - final, not reported) from foreign
4. Convert to CZK using unified rate
5. Calculate double-taxation credit (metoda prostého zápočtu):
   - CZ tax = 15% of gross dividend in CZK
   - Credit = min(foreign tax paid, CZ tax on that income)
   - Doplatek = CZ tax - credit
6. Output per-dividend detail, summary by country (for Příloha č. 3), grand total

## Output for tax return

### Capital gains -> Příloha č. 2, oddíl 2 (§10 ZDP):
- `Zdanitelne prijmy`: taxable sell proceeds in CZK
- `Vydaje`: acquisition costs + fees in CZK
- `Dilci zaklad dane`: taxable gain (difference)
- Exempt sales (time test) are excluded from both income and expenses

### Dividends -> §8 ZDP + Příloha č. 3 (zápočet):
- Foreign dividends: gross income (§8), per-country credit calculation (Příloha č. 3)
- CZ dividends: srážková daň is final (§36), not included in tax return
- Double-taxation credit limited to CZ 15% rate per dividend

## Known edge cases handled

- Partial order fills (same order_id, multiple Prodej rows) - aggregated
- Stock splits changing ISIN (e.g., SolarWinds US83417Q1058 -> US83417Q2049)
- Reverse splits to 0 shares (Agrify) - treated as disposal at price 0
- Delisting with cash settlement in different currency than original buys (H2O: bought EUR, settled CAD)
- Non-breaking space (`\xa0`) in CZK prices (e.g., "1 035 CZK")
- Multiple fee rows per order (old Degiro format)
- Partial time test exemption within a single sell (different buy dates)

## Files

- `dane_degiro.py` - the calculator (single file, ~1000 lines)
- `Degiro vypis.csv` - Account Statement input (full history)
- `Degiro Transactions.csv` - Transactions export (not used, available for cross-reference)
- `Podklady FU <rok>.txt` - generated tax documentation
