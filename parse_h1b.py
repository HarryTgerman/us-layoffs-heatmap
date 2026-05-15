#!/usr/bin/env python3
"""Parse cached h1bdata.info HTML and write h1b_overrides.json with per-company
median salary + worksite (city, state) distribution."""

import os, re, json, csv, io
from collections import Counter, defaultdict

# Map layoffhedge company -> filter substring(s) that must appear in H1B employer name
# (uppercase). Multiple = OR.
FILTERS = {
    "Volkswagen Group": ["VOLKSWAGEN"],
    "Oracle": ["ORACLE AMERICA", "ORACLE FINANCIAL", "ORACLE HEALTH", "ORACLE NETSUITE"],
    "UPS": ["UNITED PARCEL SERVICE", "UPS"],
    "HSBC": ["HSBC"],
    "Citigroup": ["CITIGROUP", "CITIBANK", "CITICORP"],
    "Nestlé": ["NESTLE"],
    "Meta": ["META PLATFORMS"],
    "Amazon": ["AMAZON.COM", "AMAZON WEB", "AMAZON CORPORATE", "AMAZON DEVELOPMENT", "AMAZON FULFILLMENT", "AMAZON DATA", "AMAZON ROBOTICS"],
    "Cognizant": ["COGNIZANT"],
    "Intel": ["INTEL CORPORATION", "INTEL CORP"],
    "Spirit Airlines": ["SPIRIT AIRLINES"],
    "Nokia": ["NOKIA"],
    "Dell": ["DELL TECHNOLOGIES", "DELL INC", "DELL FEDERAL", "DELL MARKETING"],
    "Accenture": ["ACCENTURE"],
    "Kyndryl": ["KYNDRYL"],
    "Microsoft": ["MICROSOFT CORPORATION", "MICROSOFT CORP"],
    "Chevron": ["CHEVRON"],
    "Commerzbank": ["COMMERZBANK"],
    "Heineken": ["HEINEKEN"],
    "Telefonica": ["TELEFONICA"],
    "Tyson Foods": ["TYSON FOODS"],
    "PayPal": ["PAYPAL"],
    "Takeda": ["TAKEDA"],
    "Dow Chemical": ["DOW CHEMICAL", "DOW INC"],
    "Cisco": ["CISCO SYSTEMS", "CISCO TECHNOLOGY"],
    "Block": ["BLOCK INC", "BLOCK, INC"],
    "Viatris": ["VIATRIS"],
    "Republic National Distributing": ["REPUBLIC NATIONAL"],
    "Morgan Stanley": ["MORGAN STANLEY"],
    "Porsche": ["PORSCHE"],
    "Electrolux": ["ELECTROLUX"],
    "BBC": ["BBC"],
    "WiseTech Global": ["WISETECH"],
    "BioNTech": ["BIONTECH"],
    "ASML": ["ASML"],
    "Atlassian": ["ATLASSIAN"],
    "Ericsson": ["ERICSSON"],
    "Goldman Sachs": ["GOLDMAN SACHS"],
    "Nike": ["NIKE"],
    "Mastercard": ["MASTERCARD"],
    "Ultium Cells": ["ULTIUM"],
    "Claire's": ["CLAIRE"],
    "General Motors": ["GENERAL MOTORS", "GM"],
    "Cloudflare": ["CLOUDFLARE"],
    "Capital One": ["CAPITAL ONE"],
    "Walmart": ["WAL-MART", "WALMART"],
    "Macy's": ["MACY"],
    "Snap": ["SNAP INC"],
    "Disney": ["DISNEY"],
    "Epic Games": ["EPIC GAMES"],
}

def slugify(s):
    return re.sub(r'[^a-z0-9]', '_', s.lower())

def parse_html(path, filters):
    if not os.path.exists(path): return [], []
    html = open(path, encoding='utf-8', errors='ignore').read()
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)
    salaries = []
    cities = []
    for r in rows[1:]:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', r)
        if len(cells) < 4: continue
        emp = re.sub(r'<[^>]+>', '', cells[0]).strip().upper()
        if not any(f in emp for f in filters): continue
        sal_str = re.sub(r'<[^>]+>', '', cells[2]).strip().replace(',', '')
        try: sal = int(sal_str)
        except: continue
        if sal < 30000 or sal > 1000000: continue
        salaries.append(sal)
        city = re.sub(r'<[^>]+>', '', cells[3]).strip().upper()
        cities.append(city)
    return salaries, cities

def median(xs):
    xs = sorted(xs); n = len(xs)
    if not n: return 0
    return xs[n//2]

# Output structure
overrides = {}
n_total = 0
for company, filters in FILTERS.items():
    slug = slugify(company)
    path = f"h1b_cache/{slug}.html"
    sals, cities = parse_html(path, filters)
    if not sals:
        continue
    n_total += len(sals)
    # Salary stats
    med = median(sals)
    # Worksite distribution: (CITY, STATE) -> count
    city_dist = Counter(cities).most_common(20)
    total = sum(c for _, c in city_dist)
    worksites = [(c, round(n/total, 4)) for c, n in city_dist if n/total >= 0.01]
    overrides[company] = {
        "median_salary": med,
        "mean_salary": sum(sals)//len(sals),
        "n_filings": len(sals),
        "worksites": worksites,
    }
    print(f"{company:<32} n={len(sals):>5} median=${med:>7,} top={city_dist[0][0]:<20}")

print(f"\nTotal H1B filings parsed: {n_total:,}")
with open("h1b_overrides.json", "w") as f:
    json.dump(overrides, f, indent=2)
print(f"Wrote h1b_overrides.json: {len(overrides)} companies")
