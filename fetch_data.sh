#!/usr/bin/env bash
# Refresh all source datasets. Run before `python build.py` to regenerate index.html.

set -e
cd "$(dirname "$0")"

echo "[1/5] layoffhedge 2026 CSV..."
curl -fsSL "https://layoffhedge.com/data/layoffs-2026.csv" -o layoffs-2026.csv

echo "[2/5] WARN 2026 (layoffdata.com Google Sheet)..."
curl -fsSL "https://docs.google.com/spreadsheets/d/1q47pIyvmtY7GtF3-7mHOrqBe_0uot_G944XELZ_3raU/export?format=csv" -o warn_2026.csv

echo "[3/5] WARN pre-2026 (extract 2025 subset)..."
curl -fsSL "https://docs.google.com/spreadsheets/d/1B1CYZFyJ1ghK1ApuXEeGKo3mLYWzLwONvmWV8Plkav8/export?format=csv" -o warn_pre2026.csv
python3 -c "
import csv
out = []
with open('warn_pre2026.csv') as f:
    rows = list(csv.DictReader(f))
hdr = rows[0].keys() if rows else []
out = [r for r in rows if r.get('WARN Received Date','').endswith('/2025')]
with open('warn_2025.csv','w',newline='') as f:
    w = csv.DictWriter(f, fieldnames=hdr); w.writeheader(); w.writerows(out)
print(f'  warn_2025.csv: {len(out)} rows')
"

echo "[4/5] BLS QCEW 2024 county × NAICS (75 MB)..."
curl -fsSL -A "Mozilla/5.0" "https://data.bls.gov/cew/data/files/2024/csv/2024_annual_singlefile.zip" -o qcew_2024.zip
python3 -c "
import csv, zipfile, io
out = open('qcew_county_naics.csv','w')
out.write('fips,naics,estabs,employment,avg_pay\n')
with zipfile.ZipFile('qcew_2024.zip') as z, z.open('2024.annual.singlefile.csv') as f:
    for r in csv.DictReader(io.TextIOWrapper(f, encoding='utf-8')):
        if r['agglvl_code']=='74' and r['own_code']=='5':
            emp = r['annual_avg_emplvl']
            if emp and int(emp) > 0:
                out.write(f\"{r['area_fips']},{r['industry_code']},{r['annual_avg_estabs']},{emp},{r['avg_annual_pay']}\n\")
print('  qcew_county_naics.csv ready')
"

echo "[5/5] BEA county GDP 2024..."
curl -fsSL -A "Mozilla/5.0" "https://apps.bea.gov/regional/zip/CAGDP1.zip" -o cagdp1.zip
python3 -c "
import csv, zipfile, io
out = {}
with zipfile.ZipFile('cagdp1.zip') as z, z.open('CAGDP1__ALL_AREAS_2001_2024.csv') as f:
    for r in csv.DictReader(io.TextIOWrapper(f, encoding='latin-1')):
        if r.get('LineCode') != '3': continue
        fips = r.get('GeoFIPS','').strip().strip('\"').strip()
        if len(fips) != 5 or fips.endswith('000'): continue
        try: out[fips] = int(r.get('2024','').strip()) * 1000
        except: pass
with open('county_gdp_2024.csv','w') as f:
    f.write('fips,gdp_2024\n')
    for fips, gdp in sorted(out.items()): f.write(f'{fips},{gdp}\n')
print(f'  county_gdp_2024.csv: {len(out)} counties')
"

echo
echo "Done. Run: python3 build.py"
