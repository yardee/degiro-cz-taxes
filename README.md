# Degiro CZ Taxes

Výpočet daně z příjmů z investic přes Degiro pro české daňové přiznání.

Program zpracuje CSV výpis z Degiro a vypočítá:
- **Kapitálové zisky** (§10 ZDP) - FIFO párování nákupů a prodejů, časový test
- **Dividendy** (§8 ZDP) - zápočet zahraniční daně, řešení dvojího zdanění

## Rychlý start

```bash
python3 dane_degiro.py "Degiro výpis.csv" 2025
```

Uložení podkladů pro FÚ:

```bash
python3 dane_degiro.py "Degiro výpis.csv" 2025 > "Podklady FU 2025.txt" 2>&1
```

### Požadavky

- Python 3.9+
- Žádné externí závislosti (pouze stdlib)
- Přístup k internetu (stahování kurzů z ČNB)

### Vstup

Export **Account Statement** z Degiro (CSV, česká lokalizace). Musí obsahovat **kompletní historii od založení účtu** - program potřebuje všechny nákupy pro správné FIFO párování.

Export v Degiro: *Aktivita* → *Výpis z účtu* → vybrat celé období → stáhnout CSV.

## Co program počítá

### Kapitálové zisky (§10)

- **FIFO** párování prodejů k nákupům (zákonná metoda)
- **Jednotný kurz ČNB** - automaticky stažen a vypočítán (průměr kurzů posledních pracovních dnů měsíců)
- **Časový test 3 roky** (§4/1/w ZDP) - osvobození od daně, včetně částečného osvobození v rámci jednoho prodeje
- Správné zpracování korporátních akcí: stock splity, mergery, delistingy, spin-offy, rights issues, změny produktu, vratky kapitálu

### Dividendy (§8)

- Párování hrubé dividendy a srážkové daně podle ISIN a data
- **České dividendy** - srážková daň dle §36 (konečná, neuvádí se do přiznání)
- **Zahraniční dividendy** - metoda prostého zápočtu daně:
  - Země **se SZDZ** (smlouva o zamezení dvojího zdanění): zápočet = min(zahraniční daň, CZ 15%)
  - Země **bez SZDZ** (např. Tchaj-wan, Kajmanské ostrovy): zahraniční daň jako odčitatelný výdaj dle §24/2/ch
- Správná klasifikace zemí u ADR (Alibaba→KY, Sony→JP, TSMC→TW)

## Kam do daňového přiznání

### Kapitálové zisky → Příloha č. 2, oddíl 2

| Pole | Hodnota z výstupu |
|---|---|
| Druh příjmu | Příjmy z úplatného převodu cenných papírů §10/1/b/1 |
| Příjmy | `Zdanitelne prijmy (p10)` |
| Výdaje | `Vydaje` |

Osvobozené prodeje (časový test) se **neuvádějí**.

### Dividendy → řádek 38 + Příloha č. 3

| Kam | Co |
|---|---|
| Řádek 38 | Hrubé zahraniční dividendy (dílčí základ daně §8) |
| Příloha č. 3 | Zápočet daně podle zemí (tabulka ze souhrnu) |

České dividendy se srážkovou daní se do přiznání **neuvádějí** (§36 - daň je konečná).

## Podporované typy transakcí

| Popis v CSV | Zpracování |
|---|---|
| `Nákup N Produkt@Cena Měna (ISIN)` | Vytvoření FIFO lotu |
| `Prodej N Produkt@Cena Měna (ISIN)` | Prodej - FIFO párování |
| `Stock split: Nákup/Prodej` | Úprava lotů (zachovává datum nákupu) |
| `Změna produktu: Nákup/Prodej` | Přejmenování, žádná daňová událost |
| `Merger: Nákup/Prodej` | Prodej starých akcií |
| `Delisting: Prodej` + `Korporátní akce...` | Prodej za hotovostní vypořádání |
| `Spin off: Nákup` | Nový lot s cenou 0 |
| `Rights issue: Nákup` | Nový lot s cenou 0 |
| `Interní převod: Nákup/Prodej` | Přeskočeno (není daňová událost) |
| `Vratka kapitálu` | Snížení nabývací ceny |
| `Dividenda` / `Daň z dividendy` | Zpracování dividend |

## Testy

```bash
python3 -m unittest test_dane_degiro -v
```

103 testů pokrývajících parsování, FIFO, stock splity, korporátní akce, časový test, dividendy, dvojí zdanění a regresi na reálných datech.

## Omezení

- Program počítá **pouze** s výpisem Account Statement z Degiro (česká lokalizace)
- Jednotný kurz ČNB vyžaduje připojení k internetu
- Pro ADR je nutné ručně doplnit mapování země v `ISIN_COUNTRY_OVERRIDE` pokud přibude nové ADR
- Program nepočítá celkovou daňovou povinnost - pro Přílohu č. 3 je potřeba znát celkový základ daně ze všech příjmů

## Disclaimer

Tento program slouží pouze jako **pomocný nástroj** pro výpočet podkladů k daňovému přiznání. Autor neručí za správnost výpočtů ani za soulad s aktuální legislativou. Výstupy programu **nenahrazují odborné daňové poradenství**. Před podáním daňového přiznání doporučujeme výsledky ověřit s daňovým poradcem. Za správnost údajů v daňovém přiznání odpovídá vždy poplatník.

## Licence

MIT
