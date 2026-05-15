# US Layoffs Heatmap

Interactive US county-level heatmap of 2025 and 2026 layoffs, with measured and estimated economic impact (people, wages, tax revenue, unvested equity, % of local GDP).

**Live demo:** https://harrytgerman.github.io/us-layoffs-heatmap/

![Screenshot of the heatmap](screenshot.png)

## What it does

- Map of US states colored by layoff impact for the selected metric and year.
- Click a state to drill into its counties.
- Click a county (or state) to open a side panel listing every affected company with people, wages, tax lost, equity lost, total impact, and number of sites.
- Toggle between 2025 (WARN-measured) and 2026 (WARN + estimated) views.
- Methodology disclaimer collapses/expands at the top.

## Metrics

| Metric | Definition |
|---|---|
| **People** | Workers laid off |
| **Wages** | Annual wages no longer paid (county × industry, from BLS QCEW) |
| **Tax** | Income/payroll tax revenue lost (per-state effective rate) |
| **Equity** | Unvested stock-based comp (industry baseline) |
| **Total** | Wages + Tax + Equity |
| **% of GDP** | Total impact as a fraction of county or state 2024 GDP (BEA) |

## Data sources

| Source | Use | Refresh |
|---|---|---|
| [layoffhedge.com](https://layoffhedge.com) | Company-level layoff totals (2026) | Daily/weekly |
| [layoffdata.com](https://layoffdata.com) | WARN Act filings (site-level locations + worker counts, 2025 + 2026) | Monthly |
| [BLS QCEW 2024](https://www.bls.gov/cew/) | County × NAICS-sector employment & wages | Annual |
| [BEA CAGDP1](https://apps.bea.gov/regional/) | County GDP (2024) | Annual |
| [DOL H-1B LCA disclosures (via h1bdata.info)](https://h1bdata.info) | Real per-company salaries + office locations | Quarterly |
| [US Census](https://www.census.gov) | County GeoJSON + FIPS codes | Static |

Approximately 32% of 2026 workers and 100% of 2025 workers are placed with WARN-measured locations; the remainder of 2026 is distributed using H-1B worksite frequencies and a curated HQ table.

## Build

```bash
# 1. Install Python deps
pip install -r requirements.txt

# 2. Refresh data (optional — sources change over time)
./fetch_data.sh

# 3. Build index.html
python build.py
```

Open `index.html` in a browser, or commit and push for GitHub Pages to serve it.

## Repository layout

```
build.py                 Pipeline that aggregates data and renders index.html
parse_h1b.py             Parses cached H1B HTML pages into h1b_overrides.json
fetch_data.sh            Re-downloads source datasets
index.html               Final interactive map (this is what's served)

# Inputs (small, committed)
layoffs-2026.csv         Company-level 2026 layoffs
warn_2025.csv            WARN site-level filings (2025)
warn_2026.csv            WARN site-level filings (2026)
qcew_county_naics.csv    Extracted BLS QCEW county × NAICS table
county_gdp_2024.csv      Extracted BEA county GDP
county_names.txt         US Census FIPS -> county name
counties.geojson         US Census county polygons
states.geojson           US Census state polygons
h1b_overrides.json       Parsed H-1B salary + worksite distribution per company
```

## How estimation works (2026)

For each layoffhedge company:
1. **If WARN filings match the company**, use the measured site-level worker counts as the geographic distribution. Scale to fit the company's reported US headcount.
2. **If no WARN match but H-1B disclosures exist**, distribute workers across the company's H-1B worksite cities, and use the H-1B median base salary as the per-worker wage (tech industries only).
3. **Else fall back** to a curated HQ table or to QCEW national employment distribution for the company's industry (top 20 counties for that NAICS sector).

Wages, tax, and equity are computed per-county using local QCEW pay (when available), per-state effective tax rates, and an industry-based equity baseline ($40k tech / $6k other).

## Limitations

- WARN filings only trigger for layoffs of ≥50 workers at a single site, so smaller layoffs and remote/dispersed cuts are absent from the "measured" dataset.
- Estimated geo distribution (H-1B or HQ table) overstates well-known hubs; long-tail companies cluster at industry-typical cities.
- US share of global headcount is a per-company assumption (e.g. 78% for Meta, 15% for Cognizant) — not measured.
- Tax rates are 2024 state averages, not progressive brackets.

## License

[MIT](LICENSE). Underlying datasets remain the property of their respective sources.
