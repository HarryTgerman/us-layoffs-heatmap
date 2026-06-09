#!/usr/bin/env python3
"""
build.py — generates index.html (US layoff heatmap for 2025 + 2026).

Run after refreshing input data (CSVs, GeoJSON, h1b_overrides.json).
"""

import csv, io, json, math, re
from collections import defaultdict
from rapidfuzz import fuzz
from shapely.geometry import shape, Point
import geonamescache

# ============================================================================
# 1. Static reference data
# ============================================================================

print("[1] Loading reference geo data...")
with open("counties.geojson") as f:
    counties_geo = json.load(f)
with open("states.geojson") as f:
    states_geo = json.load(f)

fips_meta, state_to_fipsabbr = {}, {}
with open("county_names.txt") as f:
    next(f)
    for line in f:
        parts = line.strip().split("|")
        if len(parts) < 5: continue
        sa, sfp, cfp, _, cname = parts[:5]
        fips_meta[sfp+cfp] = {"state": sa, "name": cname}
        state_to_fipsabbr[sa] = sfp

STATE_NAME_TO_ABBR = {"Alabama":"AL","Alaska":"AK","Arizona":"AZ","Arkansas":"AR","California":"CA","Colorado":"CO","Connecticut":"CT","Delaware":"DE","District of Columbia":"DC","Florida":"FL","Georgia":"GA","Hawaii":"HI","Idaho":"ID","Illinois":"IL","Indiana":"IN","Iowa":"IA","Kansas":"KS","Kentucky":"KY","Louisiana":"LA","Maine":"ME","Maryland":"MD","Massachusetts":"MA","Michigan":"MI","Minnesota":"MN","Mississippi":"MS","Missouri":"MO","Montana":"MT","Nebraska":"NE","Nevada":"NV","New Hampshire":"NH","New Jersey":"NJ","New Mexico":"NM","New York":"NY","North Carolina":"NC","North Dakota":"ND","Ohio":"OH","Oklahoma":"OK","Oregon":"OR","Pennsylvania":"PA","Rhode Island":"RI","South Carolina":"SC","South Dakota":"SD","Tennessee":"TN","Texas":"TX","Utah":"UT","Vermont":"VT","Virginia":"VA","Washington":"WA","West Virginia":"WV","Wisconsin":"WI","Wyoming":"WY","Puerto Rico":"PR"}
ABBR_TO_STATE_NAME = {v:k for k,v in STATE_NAME_TO_ABBR.items()}

def norm_county(s):
    if not s: return ""
    s = s.lower()
    for suf in [" county"," parish"," borough"," census area"," municipality"," city and borough"," city"]:
        s = s.replace(suf, "")
    return s.strip()
county_lookup = {(m["state"], norm_county(m["name"])): f for f,m in fips_meta.items()}

# ----- State combined effective tax rate (federal 22-24% bracket + state income + FICA 7.65%)
# Source: 2024 state income tax tables, federal 2024 brackets
STATE_EFF_TAX = {
    "AL":0.298,"AK":0.250,"AZ":0.272,"AR":0.296,"CA":0.355,"CO":0.292,"CT":0.318,
    "DE":0.318,"DC":0.345,"FL":0.250,"GA":0.302,"HI":0.330,"ID":0.308,"IL":0.297,
    "IN":0.281,"IA":0.292,"KS":0.305,"KY":0.290,"LA":0.290,"ME":0.318,"MD":0.336,
    "MA":0.300,"MI":0.293,"MN":0.348,"MS":0.292,"MO":0.298,"MT":0.308,"NE":0.308,
    "NV":0.250,"NH":0.250,"NJ":0.337,"NM":0.308,"NY":0.358,"NC":0.295,"ND":0.275,
    "OH":0.285,"OK":0.298,"OR":0.348,"PA":0.281,"RI":0.310,"SC":0.314,"SD":0.250,
    "TN":0.250,"TX":0.250,"UT":0.296,"VT":0.326,"VA":0.308,"WA":0.250,"WV":0.298,
    "WI":0.303,"WY":0.250,"PR":0.250,
}
def tax_rate_for_fips(fips):
    sfp = fips[:2]
    for sa, s in state_to_fipsabbr.items():
        if s == sfp: return STATE_EFF_TAX.get(sa, 0.28)
    return 0.28

# ----- BEA 2024 county GDP
print("[1b] Loading BEA county GDP 2024...")
county_gdp = {}
state_gdp = defaultdict(int)
with open("county_gdp_2024.csv") as f:
    for r in csv.DictReader(f):
        try:
            g = int(r["gdp_2024"]); fips = r["fips"]
            county_gdp[fips] = g
            state_gdp[fips[:2]] += g
        except: pass
print(f"  Counties with GDP: {len(county_gdp)}, states: {len(state_gdp)}")

# ----- QCEW 2024 county × NAICS sector
print("[2] Loading BLS QCEW 2024 (county × NAICS)...")
qcew_emp = {}    # (fips, naics) -> employment
qcew_pay = {}    # (fips, naics) -> avg_annual_pay
qcew_estabs = {} # (fips, naics) -> establishments
naics_total_emp = defaultdict(int)  # national NAICS -> total employment
naics_county_emp = defaultdict(list) # naics -> [(fips, emp)]
with open("qcew_county_naics.csv") as f:
    for r in csv.DictReader(f):
        if len(r["fips"]) != 5 or r["fips"].endswith("999"): continue
        key = (r["fips"], r["naics"])
        emp = int(r["employment"]) if r["employment"] else 0
        pay = int(r["avg_pay"]) if r["avg_pay"] else 0
        est = int(r["estabs"]) if r["estabs"] else 0
        qcew_emp[key] = emp
        if pay > 0: qcew_pay[key] = pay
        qcew_estabs[key] = est
        naics_total_emp[r["naics"]] += emp
        naics_county_emp[r["naics"]].append((r["fips"], emp))

# layoffhedge industry -> NAICS sector code
INDUSTRY_NAICS = {
    "Tech":"51", "Telecommunications":"51", "Media & Entertainment":"51",
    "Financial Services":"52", "Banking":"52",
    "Healthcare & Pharma":"62", "Biotech":"54",
    "Consumer Goods":"31-33", "Retail":"44-45",
    "Manufacturing":"31-33", "Aerospace & Defense":"31-33",
    "Automotive & Transportation":"48-49", "Logistics":"48-49",
    "Energy":"22", "Real Estate":"53", "Construction":"23",
    "Education":"61",
}

def naics_for(industry):
    return INDUSTRY_NAICS.get(industry, "54")  # default professional services

# Industry national mean wage (fallback when QCEW county pay is missing)
INDUSTRY_WAGE = {"Tech":115000,"Financial Services":105000,"Banking":93000,"Healthcare & Pharma":95000,"Biotech":115000,"Consumer Goods":45000,"Retail":42000,"Manufacturing":68000,"Media & Entertainment":90000,"Telecommunications":80000,"Aerospace & Defense":95000,"Logistics":52000,"Automotive & Transportation":65000,"Energy":95000,"Real Estate":72000,"Government":72000,"Education":62000,"Construction":72000}

# Per-company salary multipliers (applied on top of industry baseline when H1B data isn't available)
COMPANY_MULT = {"Meta":1.85,"Google":1.70,"Alphabet":1.70,"Apple":1.65,"Microsoft":1.55,"Netflix":1.80,"LinkedIn":1.55,"Salesforce":1.45,"Oracle":1.30,"Intel":1.15,"Cisco":1.30,"IBM":1.10,"Dell":1.00,"HP":1.00,"Snap":1.50,"eBay":1.30,"Block":1.45,"Epic Games":1.30,"ZoomInfo":1.10,"Goldman Sachs":1.70,"JPMorgan Chase":1.45,"Morgan Stanley":1.55,"Citigroup":1.30,"Wells Fargo":1.10,"Bank of America":1.20,"Cognizant":0.60,"Kyndryl":0.85,"Accenture":0.95,"Walmart":0.65,"Chick-fil-A":0.45,"Starbucks":0.55,"UPS":0.75,"FedEx":0.75,"Reyes Coca-Cola Bottling":0.75,"Compass":0.50,"Disney":1.20,"Sony Pictures":1.30,"Streamland Media":1.10,"Takeda":1.30,"Pfizer":1.25,"Gilead Sciences":1.40,"Replimune":1.30,"Gossamer Bio":1.35,"General Motors":1.10,"Ford":1.10,"Spirit Airlines":0.85,"Transdev":0.65,"GXO Logistics":0.80,"Electrolux":0.95,"Republic National Distributing":0.80,"Alan Ritchey":0.75,"Fresh Venture Foods":0.55}
US_SHARE = {"Cognizant":0.15,"Kyndryl":0.45,"Accenture":0.35,"Microsoft":0.55,"Citigroup":0.65,"Oracle":0.78,"Amazon":0.80,"Meta":0.78,"Intel":0.65,"Dell":0.65,"Cisco":0.70,"Google":0.55,"Alphabet":0.55,"Apple":0.55,"IBM":0.30,"HP":0.35,"Takeda":0.30,"Pfizer":0.55,"Electrolux":0.20,"UPS":0.85,"FedEx":0.85,"Disney":0.85,"Starbucks":0.85,"Nike":0.45,"JPMorgan Chase":0.75,"Goldman Sachs":0.70,"Morgan Stanley":0.65,"Wells Fargo":0.95,"Bank of America":0.90,"GXO Logistics":0.40,"Spirit Airlines":1.0,"General Motors":0.50,"Ford":0.55,"Compass":0.30,"_default":0.65}

# Curated per-company HQ table. Used when no WARN or H1B data is available for the company.
HQ_FALLBACK = {
    "Microsoft":[("53033",0.55),("06085",0.20),("48453",0.10),("06075",0.05),("36061",0.10)],
    "Amazon":[("53033",0.55),("06037",0.10),("06075",0.08),("36061",0.10),("51059",0.10),("48453",0.07)],
    "Meta":[("06081",0.55),("06085",0.20),("36061",0.10),("53033",0.05),("48453",0.10)],
    "Alphabet":[("06085",0.60),("36061",0.15),("53033",0.05),("48453",0.10),("06075",0.10)],
    "Google":[("06085",0.60),("36061",0.15),("53033",0.05),("48453",0.10),("06075",0.10)],
    "Apple":[("06085",0.70),("48453",0.10),("36061",0.05),("06075",0.10),("53033",0.05)],
    "Oracle":[("48453",0.42),("06085",0.30),("36061",0.10),("53033",0.05),("12086",0.13)],
    "Intel":[("41067",0.40),("06085",0.20),("04013",0.15),("48453",0.10),("06075",0.05),("36061",0.10)],
    "Cisco":[("06085",0.55),("48453",0.10),("36061",0.10),("53033",0.05),("06037",0.10),("12086",0.10)],
    "Salesforce":[("06075",0.55),("36061",0.15),("48453",0.10),("06085",0.10),("53033",0.10)],
    "IBM":[("36119",0.30),("36061",0.20),("48453",0.15),("13121",0.10),("06085",0.10),("12086",0.15)],
    "Dell":[("48453",0.55),("48201",0.15),("12086",0.10),("36061",0.10),("06085",0.10)],
    "HP":[("48201",0.35),("06085",0.30),("48453",0.15),("36061",0.10),("12086",0.10)],
    "LinkedIn":[("06081",0.55),("36061",0.15),("48453",0.10),("53033",0.10),("06075",0.10)],
    "Snap":[("06037",0.65),("06075",0.15),("36061",0.10),("06085",0.10)],
    "eBay":[("06085",0.60),("48453",0.10),("36061",0.10),("12086",0.10),("06075",0.10)],
    "ZoomInfo":[("53061",0.55),("53033",0.15),("36061",0.10),("06075",0.10),("48453",0.10)],
    "Epic Games":[("37183",0.55),("53033",0.15),("06075",0.10),("36061",0.10),("06085",0.10)],
    "Block":[("06075",0.55),("36061",0.15),("06037",0.10),("48453",0.10),("13121",0.10)],
    "Disney":[("06037",0.50),("12095",0.30),("36061",0.10),("06075",0.10)],
    "Sony Pictures":[("06037",0.85),("36061",0.15)],
    "Cognizant":[("36119",0.20),("48201",0.20),("13121",0.15),("36061",0.10),("04013",0.10),("12086",0.10),("17031",0.15)],
    "Kyndryl":[("36061",0.30),("36119",0.15),("48201",0.15),("13121",0.10),("06037",0.10),("17031",0.20)],
    "Accenture":[("17031",0.20),("48201",0.15),("36061",0.20),("13121",0.10),("04013",0.10),("12086",0.10),("06037",0.15)],
    "JPMorgan Chase":[("36061",0.45),("48201",0.15),("17031",0.10),("13121",0.10),("06037",0.10),("12086",0.10)],
    "Wells Fargo":[("06075",0.25),("37119",0.20),("48201",0.15),("36061",0.10),("06037",0.10),("19153",0.20)],
    "Goldman Sachs":[("36061",0.70),("48201",0.10),("17031",0.10),("13121",0.10)],
    "Citigroup":[("36061",0.55),("48201",0.10),("12086",0.10),("13121",0.10),("06037",0.15)],
    "Morgan Stanley":[("36061",0.65),("48201",0.10),("13121",0.10),("17031",0.15)],
    "Bank of America":[("37119",0.35),("36061",0.20),("48201",0.15),("06037",0.10),("13121",0.10),("12086",0.10)],
    "Takeda":[("25017",0.50),("17031",0.15),("06073",0.10),("12086",0.10),("36061",0.15)],
    "Pfizer":[("36061",0.30),("25017",0.15),("17031",0.10),("36103",0.20),("06073",0.10),("42071",0.15)],
    "Gilead Sciences":[("06081",0.65),("06073",0.10),("25017",0.10),("36061",0.15)],
    "Starbucks":[("53033",0.70),("06037",0.10),("36061",0.10),("48201",0.10)],
    "Nike":[("41067",0.55),("41051",0.15),("36061",0.10),("06037",0.10),("48201",0.10)],
    "Walmart":[("05007",0.40),("48201",0.15),("12086",0.10),("06037",0.10),("13121",0.15),("17031",0.10)],
    "Target":[("27053",0.55),("48201",0.10),("06037",0.10),("12086",0.10),("13121",0.15)],
    "Chick-fil-A":[("13121",0.50),("48201",0.10),("37119",0.10),("12086",0.10),("06037",0.10),("48453",0.10)],
    "UPS":[("13121",0.30),("21111",0.15),("48201",0.10),("06037",0.10),("36061",0.10),("17031",0.10),("12086",0.15)],
    "FedEx":[("47157",0.35),("48201",0.15),("17031",0.10),("06037",0.10),("13121",0.15),("12086",0.15)],
    "General Motors":[("26163",0.50),("26125",0.20),("48201",0.10),("39035",0.10),("36029",0.10)],
    "Ford":[("26163",0.50),("26125",0.20),("48201",0.10),("39035",0.10),("37119",0.10)],
    "Spirit Airlines":[("12011",0.32),("12086",0.18),("48201",0.15),("36059",0.10),("13121",0.10),("12095",0.15)],
}

# ----- H1B overrides (from DOL LCA disclosures via h1bdata.info) -----
import os
H1B_OVERRIDES = {}
if os.path.exists("h1b_overrides.json"):
    with open("h1b_overrides.json") as f:
        H1B_OVERRIDES = json.load(f)

def worksite_to_fips_dist(worksites):
    """Convert [('MENLO PARK, CA', 0.45), ...] to [(FIPS, weight), ...] with renormalization."""
    out = []
    for site_str, w in worksites:
        # Parse "CITY, ST"
        m = re.match(r"^(.+?),\s*([A-Z]{2})$", site_str)
        if not m: continue
        city, sa = m.group(1).strip(), m.group(2).strip()
        # html entities like O&#39;FALLON
        city = city.replace("&#39;", "'").replace("&amp;", "&")
        # try city_to_fips with original; else clean punctuation
        f = city_to_fips(sa, city.title())
        if not f:
            # try cleaner version
            f = city_to_fips(sa, re.sub(r"[^A-Za-z\s'-]", "", city).title())
        if f:
            out.append((f, w))
    if not out: return None
    # Renormalize remaining weights to 1
    total = sum(w for _, w in out)
    return [(f, w/total) for f, w in out]

# Build company-specific overrides (only applied if usable)
H1B_HQ = {}     # company -> list of (fips, weight)
H1B_SALARY = {} # company -> median annual salary
for co, info in H1B_OVERRIDES.items():
    if info["n_filings"] >= 5 and info["median_salary"] > 0:
        H1B_SALARY[co] = info["median_salary"]
    # Need at least 20 filings AND >=3 distinct worksites for a reliable geo distribution
    if info["n_filings"] >= 20 and len(info["worksites"]) >= 2:
        d = None
        pass  # build later (city_to_fips not yet defined when constants are loaded)

# Precompute NAICS-weighted top-30 distribution for industry fallback
def naics_distribution(naics, top_n=30):
    """Return list of (fips, weight) summing to 1, for top counties by employment in that NAICS."""
    counties = naics_county_emp.get(naics, [])
    if not counties: return [("36061", 1.0)]  # fallback to NYC
    counties = sorted(counties, key=lambda x: -x[1])[:top_n]
    total = sum(e for _, e in counties)
    if total == 0: return [("36061", 1.0)]
    return [(f, e/total) for f, e in counties]

# Tech-equity heuristic
TECH_INDS = {"Tech","Telecommunications","Biotech","Media & Entertainment"}
def equity_per_head(ind):
    return 40000 if ind in TECH_INDS else 6000

def us_share(co): return US_SHARE.get(co, US_SHARE["_default"])

def avg_salary_for(industry, company, fips):
    """Priority: H1B median (real per-company tech salary) > QCEW county avg > industry baseline × multiplier."""
    if company in H1B_SALARY and industry in TECH_INDS:
        return H1B_SALARY[company]
    naics = naics_for(industry)
    qcew = qcew_pay.get((fips, naics))
    base = qcew if qcew else INDUSTRY_WAGE.get(industry, 70000)
    return base * COMPANY_MULT.get(company, 1.0)

# ============================================================================
# 3. City → FIPS geocoder for WARN rows with no county
# ============================================================================

print("[3] Building city -> FIPS geocoder...")
gc = geonamescache.GeonamesCache()
county_shapes_by_state = defaultdict(list)
for feat in counties_geo["features"]:
    fid = feat["id"]
    if fid in fips_meta:
        county_shapes_by_state[fips_meta[fid]["state"]].append((fid, shape(feat["geometry"])))

us_cities = {}
for c in gc.get_cities().values():
    if c["countrycode"] != "US": continue
    sa = c.get("admin1code","")
    if not sa: continue
    k = (sa, c["name"].lower())
    p = c.get("population",0)
    if k not in us_cities or p > us_cities[k][2]:
        us_cities[k] = (c["latitude"], c["longitude"], p)

def _city_to_fips_impl(sa, city):
    if not city or not sa: return None
    city = city.split(",")[0].strip()
    m = re.search(r"([A-Za-z][A-Za-z\s\.\-']{2,}?)\s+[A-Z]{2}\s+\d{5}", city)
    if m: city = m.group(1).strip()
    if (sa, city.lower()) not in us_cities:
        toks = city.split()
        for n in (3,2,1):
            if len(toks) >= n:
                cand = " ".join(toks[-n:])
                if (sa, cand.lower()) in us_cities:
                    city = cand; break
    coords = us_cities.get((sa, city.lower()))
    if not coords: return None
    pt = Point(coords[1], coords[0])
    for fips, shp in county_shapes_by_state.get(sa, []):
        if shp.contains(pt): return fips
    return None
city_to_fips = _city_to_fips_impl

# Build H1B-derived geographic distributions now that city_to_fips is available
for co, info in H1B_OVERRIDES.items():
    if info["n_filings"] >= 20 and len(info["worksites"]) >= 2:
        d = worksite_to_fips_dist(info["worksites"])
        if d and len(d) >= 1:
            H1B_HQ[co] = d
print(f"  H1B-derived geo distributions: {len(H1B_HQ)} companies")
print(f"  H1B-derived salaries: {len(H1B_SALARY)} companies")

# ============================================================================
# 4. Year processor
# ============================================================================

def parse_workers(s):
    try: return int(re.sub(r"[^\d]","",s))
    except: return 0

def norm_co(s):
    s = s.lower(); s = re.sub(r"[^a-z0-9 ]"," ",s)
    s = re.sub(r"\b(inc|incorporated|llc|ltd|lp|llp|corp|corporation|company|co|the|plc|sa|group|holdings|holding|usa|america|north|us|services|service|solutions|systems|technologies|technology|enterprises|enterprise|industries|industry)\b","",s)
    return re.sub(r"\s+"," ",s).strip()

def load_warn(path):
    """Load WARN CSV, return list of {company, fips, workers}."""
    with open(path) as f:
        rows = list(csv.DictReader(f))
    sites = []
    for w in rows:
        sa = STATE_NAME_TO_ABBR.get(w["State"])
        if not sa: continue
        workers = parse_workers(w["Number of Workers"])
        if workers <= 0: continue
        cf = w.get("County","").strip()
        city = w.get("City","").strip()
        co = w["Company"]
        if cf:
            cns = [c.strip() for c in cf.split(",") if c.strip()]
            fl = [county_lookup.get((sa, norm_county(c))) for c in cns]
            fl = [f for f in fl if f]
            if fl:
                split = workers // len(fl); rem = workers - split*len(fl)
                for i, f in enumerate(fl):
                    sites.append({"company": co, "fips": f, "workers": split + (1 if i<rem else 0)})
                continue
        f = city_to_fips(sa, city)
        if f: sites.append({"company": co, "fips": f, "workers": workers})
    return sites

def fuzzy_match(warn_sites, lh_rows):
    """Fuzzy-match WARN sites to layoffhedge companies."""
    lh_norm = [(r, norm_co(r["Company"])) for r in lh_rows]
    matched = defaultdict(list)
    unmatched = []
    for s in warn_sites:
        wcn = norm_co(s["company"])
        if not wcn: continue
        best, bestS = None, 0
        for lhrow, lhn in lh_norm:
            if not lhn: continue
            score = 0
            if wcn == lhn:
                score = 100
            elif wcn.startswith(lhn+" ") or wcn.endswith(" "+lhn):
                score = 98
            elif lhn in wcn.split():
                score = 95
            else:
                # First-word match + token-set similarity
                wfirst, lfirst = wcn.split()[0] if wcn else "", lhn.split()[0] if lhn else ""
                if wfirst == lfirst and len(wfirst) >= 4:
                    score = max(score, fuzz.token_set_ratio(wcn, lhn))
                elif len(lhn) >= 6 and lhn in wcn:
                    score = max(score, 90)
            if len(lhn) < 4: continue  # too short to safely match
            if score > bestS: bestS, best = score, lhrow
        if best and bestS >= 85:
            matched[best["Company"]].append(s)
        else:
            unmatched.append(s)
    return matched, unmatched

def process_year(year_label, lh_rows, warn_sites, use_estimation):
    """Aggregate per-county. If use_estimation=False, only use WARN-measured."""
    matched, unmatched = fuzzy_match(warn_sites, lh_rows)
    agg = defaultdict(lambda: {"people":0.0,"wages":0.0,"tax":0.0,"equity":0.0,"total":0.0,"companies":[]})

    company_counties = defaultdict(set)  # company -> {fips,...} for office count

    if use_estimation:
        for row in lh_rows:
            co = row["Company"]
            people_cut = parse_workers(str(row.get("People Cut","0")))
            if people_cut <= 0: continue
            us_total = round(people_cut * us_share(co))
            ind = row.get("Industry","")
            eq = equity_per_head(ind)
            measured = matched.get(co, [])
            mtot = sum(s["workers"] for s in measured)
            dist = []
            if mtot > 0 and mtot >= us_total * 0.7:
                scale = us_total / mtot
                for s in measured:
                    dist.append((s["fips"], round(s["workers"]*scale), "measured"))
            elif mtot > 0:
                for s in measured:
                    dist.append((s["fips"], s["workers"], "measured"))
                rem = us_total - mtot
                if rem > 0:
                    hq = HQ_FALLBACK.get(co)
                    if not hq:
                        hq = naics_distribution(naics_for(ind), top_n=20)
                    for f, w in hq: dist.append((f, round(rem*w), "estimated"))
            else:
                hq = H1B_HQ.get(co) or HQ_FALLBACK.get(co)
                if not hq:
                    hq = naics_distribution(naics_for(ind), top_n=20)
                for f, w in hq: dist.append((f, round(us_total*w), "estimated"))
            for f, w, src in dist:
                if w <= 0: continue
                sal = avg_salary_for(ind, co, f)
                wages = w * sal
                tax = wages * tax_rate_for_fips(f)
                equity = w * eq
                tot = wages + tax + equity
                agg[f]["people"] += w
                agg[f]["wages"] += wages
                agg[f]["tax"] += tax
                agg[f]["equity"] += equity
                agg[f]["total"] += tot
                agg[f]["companies"].append({"co":co,"people":w,"wages":wages,"tax":tax,"equity":equity,"total":tot,"source":src})
                company_counties[co].add(f)
        # Unmatched WARN sites (not in layoffhedge) — still real layoffs
        for s in unmatched:
            co, w, f = s["company"], s["workers"], s["fips"]
            # Best-guess industry from QCEW: not knowing, use professional-services baseline
            sal = qcew_pay.get((f, "54")) or 70000
            wages = w*sal; tax = wages*tax_rate_for_fips(f); eq = w*6000
            tot = wages+tax+eq
            agg[f]["people"] += w; agg[f]["wages"] += wages; agg[f]["tax"] += tax
            agg[f]["equity"] += eq; agg[f]["total"] += tot
            agg[f]["companies"].append({"co":co+" (WARN only)","people":w,"wages":wages,"tax":tax,"equity":eq,"total":tot,"source":"measured"})
            company_counties[co+" (WARN only)"].add(f)
    else:
        # WARN-only (2025)
        for s in warn_sites:
            co, w, f = s["company"], s["workers"], s["fips"]
            sal = qcew_pay.get((f, "54")) or 70000  # default to professional-services
            wages = w*sal; tax = wages*tax_rate_for_fips(f); eq = w*6000
            tot = wages+tax+eq
            agg[f]["people"] += w; agg[f]["wages"] += wages; agg[f]["tax"] += tax
            agg[f]["equity"] += eq; agg[f]["total"] += tot
            agg[f]["companies"].append({"co":co,"people":w,"wages":wages,"tax":tax,"equity":eq,"total":tot,"source":"measured"})
            company_counties[co].add(f)

    # Office count per company (distinct counties)
    co_office_count = {c: len(s) for c, s in company_counties.items()}

    # Aggregate by state
    state_agg = defaultdict(lambda: {"people":0.0,"wages":0.0,"tax":0.0,"equity":0.0,"total":0.0,"companies":defaultdict(lambda:{"people":0,"wages":0,"tax":0,"equity":0,"total":0,"source":"estimated"})})
    for f, d in agg.items():
        sfp = f[:2]
        sa = next((k for k,v in state_to_fipsabbr.items() if v==sfp), None)
        if not sa: continue
        for k in ("people","wages","tax","equity","total"): state_agg[sa][k] += d[k]
        for c in d["companies"]:
            e = state_agg[sa]["companies"][c["co"]]
            for k in ("people","wages","tax","equity","total"): e[k] += c[k]
            if c["source"]=="measured": e["source"]="measured"

    return agg, state_agg, co_office_count

# ============================================================================
# 5. Run for both years
# ============================================================================

print("[4] Loading 2026 layoffhedge + WARN...")
with open("layoffs-2026.csv") as f:
    raw = [l for l in f if not l.startswith("#")]
lh2026 = list(csv.DictReader(io.StringIO("".join(raw))))
warn2026 = load_warn("warn_2026.csv")
print(f"  layoffhedge 2026: {len(lh2026)} companies, WARN 2026: {len(warn2026)} sites")

print("[5] Loading 2025 WARN...")
warn2025 = load_warn("warn_2025.csv")
print(f"  WARN 2025: {len(warn2025)} sites")

print("[6] Processing 2026 (WARN + estimation)...")
agg_2026, state_agg_2026, off_2026 = process_year("2026", lh2026, warn2026, use_estimation=True)
print(f"  2026: {len(agg_2026)} counties, {len(state_agg_2026)} states")

print("[7] Processing 2025 (WARN-only, all measured)...")
agg_2025, state_agg_2025, off_2025 = process_year("2025", [], warn2025, use_estimation=False)
print(f"  2025: {len(agg_2025)} counties, {len(state_agg_2025)} states")

# ============================================================================
# 6. Build per-year arrays & panel data for the HTML
# ============================================================================

metrics = ["people","wages","tax","equity","total","gdp_share"]
metric_labels = {"people":"People","wages":"Wages","tax":"Tax Lost","equity":"Equity","total":"Total Impact","gdp_share":"% of Local GDP"}

all_fips = sorted([feat["id"] for feat in counties_geo["features"]])
county_state_fips = [f[:2] for f in all_fips]
county_name = [f"{fips_meta.get(f,{'name':'?'})['name']}, {ABBR_TO_STATE_NAME.get(fips_meta.get(f,{'state':'?'})['state'], '?')}" for f in all_fips]

# QCEW context per county for hover
def qcew_context(fips, industry):
    naics = naics_for(industry)
    return {"estabs": qcew_estabs.get((fips, naics), 0),
            "avg_pay": qcew_pay.get((fips, naics), 0)}

state_geo_ids = [feat.get("id") for feat in states_geo["features"]]
state_bbox, state_center = {}, {}
for feat in states_geo["features"]:
    fid = feat.get("id")
    if not fid: continue
    try:
        shp = shape(feat["geometry"]); minx, miny, maxx, maxy = shp.bounds
        state_bbox[fid] = [round(miny,3), round(minx,3), round(maxy,3), round(maxx,3)]
        rp = shp.representative_point()
        state_center[fid] = [round(rp.y,3), round(rp.x,3)]
    except: pass

state_name_by_fips = {}
for sfp in state_geo_ids:
    sa = next((k for k,v in state_to_fipsabbr.items() if v==sfp), None)
    state_name_by_fips[sfp] = ABBR_TO_STATE_NAME.get(sa, sa or "")

def build_year_payload(agg, state_agg, off_count):
    # Compute gdp_share = total / local_gdp * 100 (percent)
    def gdp_share_for_county(fips, total):
        g = county_gdp.get(fips)
        return (total / g * 100) if g and g > 0 else 0
    def gdp_share_for_state(sfp, total):
        g = state_gdp.get(sfp)
        return (total / g * 100) if g and g > 0 else 0

    # Per-county arrays
    county_raw = {m: [] for m in metrics}
    for f in all_fips:
        d = agg.get(f)
        for m in metrics:
            if m == "gdp_share":
                v = gdp_share_for_county(f, d["total"]) if d else 0
            else:
                v = d[m] if d else 0
            county_raw[m].append(round(v, 3) if m == "gdp_share" else round(v))

    # Per-state arrays
    state_raw = {m: [] for m in metrics}
    for sfp in state_geo_ids:
        sa = next((k for k,v in state_to_fipsabbr.items() if v==sfp), None)
        d = state_agg.get(sa) if sa else None
        for m in metrics:
            if m == "gdp_share":
                v = gdp_share_for_state(sfp, d["total"]) if d else 0
            else:
                v = d[m] if d else 0
            state_raw[m].append(round(v, 3) if m == "gdp_share" else round(v))

    # Linear color ranges: calibrated to data quantiles to give visible discrimination across the range.
    # State view (~50 entries): use min/max with a 5% margin.
    # County view (3000+ entries): clip at p98 so a single outlier doesn't compress everyone else.
    def calibrated_range(values, is_county):
        vals = sorted([v for v in values if v and v > 0])
        if not vals: return (0, 1)
        if is_county and len(vals) > 30:
            zmin = vals[int(len(vals) * 0.05)]
            zmax = vals[int(len(vals) * 0.98)]
        else:
            zmin = vals[0]
            zmax = vals[-1]
        def magnitude(x):
            # 10^(order-of-magnitude minus 1), works for x<1 too
            return 10 ** (math.floor(math.log10(abs(x))) - 1)
        def nice_floor(x):
            if x <= 0: return 0
            mag = magnitude(x)
            return math.floor(x / mag) * mag
        # zmax: most-rounded nice number that is >= x and <= x * 1.20
        def nice_ceil_capped(x, cap_ratio=1.20):
            if x <= 0: return 0
            mag = magnitude(x)
            upper = x * cap_ratio
            best = math.ceil(x / mag) * mag
            for step in (2, 5, 10, 25):
                cand = math.ceil(x / (step * mag)) * (step * mag)
                if cand >= x and cand <= upper and cand > best:
                    best = cand
            return best
        return (nice_floor(zmin), nice_ceil_capped(zmax))

    zranges_county = {m: calibrated_range(county_raw[m], True) for m in metrics}
    zranges_state = {m: calibrated_range(state_raw[m], False) for m in metrics}

    # Panels
    def co_list(companies):
        return [{"co":c["co"],"people":round(c["people"]),"wages":round(c["wages"]),"tax":round(c["tax"]),"equity":round(c["equity"]),"total":round(c["total"]),"source":c["source"],"offices":off_count.get(c["co"], 1)} for c in sorted(companies, key=lambda x:-x["total"])]

    panel_county = {}
    for f, d in agg.items():
        if not d["companies"]: continue
        m_ = fips_meta.get(f, {"name":"?","state":"?"})
        # QCEW context for top industry in this county (use the dominant company's industry)
        # For simplicity report county-level total private employment
        emp_total = sum(qcew_emp.get((f, n), 0) for n in ["51","52","54","31-33","44-45","48-49","62"])
        panel_county[f] = {
            "name": f"{m_['name']}, {ABBR_TO_STATE_NAME.get(m_['state'], m_['state'])}",
            "totals": {k: round(d[k]) for k in ("people","wages","tax","equity","total")},
            "companies": co_list(d["companies"]),
            "measured_pct": round(100 * sum(c["people"] for c in d["companies"] if c["source"]=="measured") / max(d["people"], 1)),
            "qcew_emp": emp_total,
            "tax_rate": round(tax_rate_for_fips(f) * 100, 1),
        }
    panel_state = {}
    for sa, d in state_agg.items():
        if not d["companies"]: continue
        sfp = state_to_fipsabbr.get(sa)
        if not sfp: continue
        cl = [{**v,"co":k} for k,v in d["companies"].items()]
        panel_state[sfp] = {
            "name": ABBR_TO_STATE_NAME.get(sa, sa),
            "totals": {k: round(d[k]) for k in ("people","wages","tax","equity","total")},
            "companies": co_list(cl),
            "measured_pct": round(100 * sum(c["people"] for c in cl if c["source"]=="measured") / max(d["people"], 1)),
            "tax_rate": STATE_EFF_TAX.get(sa, 0.28) * 100,
        }

    grand_totals = {m: sum(county_raw[m]) for m in metrics}
    total_measured = sum(c["people"] for f,d in agg.items() for c in d["companies"] if c["source"]=="measured")
    measured_pct = round(100 * total_measured / max(grand_totals["people"],1))

    # panel pct maps (for hover labels)
    panel_pct_county = {f: panel_county[f]["measured_pct"] for f in panel_county}
    panel_pct_state = {f: panel_state[f]["measured_pct"] for f in panel_state}

    return {
        "county_raw": county_raw, "state_raw": state_raw,
        "zranges_county": zranges_county, "zranges_state": zranges_state,
        "panel_county": panel_county, "panel_state": panel_state,
        "panel_pct_county": panel_pct_county, "panel_pct_state": panel_pct_state,
        "grand_totals": grand_totals, "measured_pct": measured_pct,
    }

payload_2025 = build_year_payload(agg_2025, state_agg_2025, off_2025)
payload_2026 = build_year_payload(agg_2026, state_agg_2026, off_2026)

# ============================================================================
# 6b. Chart-page aggregates — national per-company totals + state ranking
# ============================================================================

chart_metrics = ["people", "wages", "tax", "equity", "total"]

def aggregate_companies(agg):
    """Sum each company across every county it touches -> national per-company totals."""
    comp = {}
    for d in agg.values():
        for c in d["companies"]:
            e = comp.setdefault(c["co"], {"co": c["co"], "people": 0, "wages": 0, "tax": 0,
                                          "equity": 0, "total": 0, "measured_people": 0})
            for k in chart_metrics:
                e[k] += c[k]
            if c["source"] == "measured":
                e["measured_people"] += c["people"]
    out = []
    for v in comp.values():
        row = {"co": v["co"]}
        for k in chart_metrics:
            row[k] = round(v[k])
        row["source"] = "measured" if v["measured_people"] >= 0.5 * max(v["people"], 1) else "estimated"
        out.append(row)
    out.sort(key=lambda x: -x["total"])
    return out

def top_states(payload):
    rows = [{"name": v["name"], **{k: v["totals"][k] for k in chart_metrics}} for v in payload["panel_state"].values()]
    rows.sort(key=lambda x: -x["total"])
    return rows

def year_chart_block(agg, payload):
    return {
        "grand_totals": payload["grand_totals"],
        "measured_pct": payload["measured_pct"],
        "companies": aggregate_companies(agg)[:25],
        "states": top_states(payload)[:25],
        "n_companies": len({c["co"] for d in agg.values() for c in d["companies"]}),
        "n_states": len(payload["panel_state"]),
    }

CHART_DATA = {
    "metrics": chart_metrics,
    "metric_labels": {m: metric_labels[m] for m in chart_metrics},
    "years": {
        "2025": year_chart_block(agg_2025, payload_2025),
        "2026": year_chart_block(agg_2026, payload_2026),
    },
}

# ============================================================================
# 7. Plotly figure + HTML
# ============================================================================

print("[8] Generating HTML...")
import plotly.graph_objects as go

m0 = "total"
zmin, zmax = payload_2026["zranges_state"][m0]

HOVER_TEMPLATE = (
    "<b>%{text}</b><br>"
    "<span style='font-size:11px;color:#888'>%{customdata[7]}% WARN-measured</span><br>"
    "People: <b>%{customdata[1]:,}</b><br>"
    "Wages: <b>$%{customdata[2]:,.0f}</b><br>"
    "Tax lost: <b>$%{customdata[3]:,.0f}</b><br>"
    "Equity: <b>$%{customdata[4]:,.0f}</b><br>"
    "Total: <b>$%{customdata[5]:,.0f}</b><br>"
    "% of local GDP: <b>%{customdata[6]:.3f}%</b>"
    "<extra></extra>"
)
NODATA_TEMPLATE = (
    "<b>%{text}</b><br>"
    "<span style='font-size:11px;color:#aaa'>No layoff data</span>"
    "<extra></extra>"
)

fig = go.Figure()

# Trace 0: state outlines (transparent fill but hover still works on empty states)
fig.add_trace(go.Choroplethmapbox(
    geojson=states_geo, locations=state_geo_ids,
    z=[0]*len(state_geo_ids),
    text=[state_name_by_fips.get(g, "") for g in state_geo_ids],
    colorscale=[[0,"rgba(0,0,0,0)"],[1,"rgba(0,0,0,0)"]], zmin=0, zmax=1,
    marker=dict(line=dict(color="rgba(150,150,150,0.6)", width=0.5), opacity=1),
    hovertemplate="<b>%{text}</b><br><span style='font-size:11px;color:#aaa'>Not in the dataset</span><extra></extra>",
    showscale=False, name="states-empty", visible=True,
))

# Trace 1: state choropleth (only states WITH data)
fig.add_trace(go.Choroplethmapbox(
    geojson=states_geo, locations=state_geo_ids,
    z=[v if v and v > 0 else None for v in payload_2026["state_raw"][m0]],
    customdata=[[g] + [payload_2026["state_raw"][m][i] for m in metrics] + [payload_2026["panel_pct_state"].get(g, 0)] for i, g in enumerate(state_geo_ids)],
    text=[state_name_by_fips.get(g, "") for g in state_geo_ids],
    colorscale="YlOrRd", zmin=zmin, zmax=zmax,
    marker=dict(line=dict(color="rgba(80,80,80,0.6)", width=0.7), opacity=0.92),
    hovertemplate=HOVER_TEMPLATE,
    colorbar=dict(title=dict(text=f"<b>{metric_labels[m0]}</b>",font=dict(size=11)),
                  thickness=12, len=0.55, x=0.99, y=0.5, tickfont=dict(size=10),
                  tickformat="~s"),
    name="states-data", visible=True,
))

# Trace 2: empty counties (grey)
fig.add_trace(go.Choroplethmapbox(
    geojson=counties_geo, locations=[], z=[], text=[],
    colorscale=[[0,"#e8e8e8"],[1,"#e8e8e8"]], zmin=0, zmax=1,
    marker=dict(line=dict(color="rgba(120,120,120,0.5)", width=0.4), opacity=0.85),
    hovertemplate=NODATA_TEMPLATE, showscale=False,
    name="counties-empty", visible=True,
))
# Trace 3: data counties
fig.add_trace(go.Choroplethmapbox(
    geojson=counties_geo, locations=[], z=[], customdata=[], text=[],
    colorscale="YlOrRd", zmin=zmin, zmax=zmax,
    marker=dict(line=dict(color="rgba(60,60,60,0.7)", width=0.5), opacity=0.95),
    hovertemplate=HOVER_TEMPLATE, showscale=False,
    colorbar=dict(tickformat="~s"),
    name="counties-data", visible=True,
))

fig.update_layout(
    mapbox=dict(style="carto-positron", center=dict(lat=39.5, lon=-98.35), zoom=3.4),
    margin=dict(l=0,r=0,t=0,b=0), paper_bgcolor="white", plot_bgcolor="white", autosize=True,
)
plot_div = fig.to_html(include_plotlyjs="cdn", full_html=False, div_id="map",
                       config={"responsive": True, "displaylogo": False, "scrollZoom": True})

ALL_DATA = {
    "metrics": metrics, "metric_labels": metric_labels,
    "all_fips": all_fips, "county_state_fips": county_state_fips, "county_name": county_name,
    "state_geo_ids": state_geo_ids, "state_name": [state_name_by_fips.get(g,"") for g in state_geo_ids],
    "state_bbox": state_bbox, "state_center": state_center,
    "years": {"2025": payload_2025, "2026": payload_2026},
}

html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>US Layoffs Heatmap — 2025 & 2026</title>
<style>
*{{box-sizing:border-box}}html,body{{margin:0;padding:0;height:100%;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#fafafa}}
body{{display:flex;flex-direction:column;height:100vh;overflow:hidden}}
header{{background:white;border-bottom:1px solid #e0e0e0;padding:10px 20px 8px;flex-shrink:0;position:relative}}
.attrib{{position:absolute;top:10px;right:20px;display:flex;gap:10px;align-items:center;font-size:11px;color:#999}}
.attrib a{{color:#888;text-decoration:none;display:inline-flex;align-items:center;gap:4px;padding:2px 4px;border-radius:3px;transition:all 0.15s}}
.attrib a:hover{{color:#1a4480;background:#f3f6fb}}
.attrib svg{{width:13px;height:13px;fill:currentColor}}
.attrib .sep{{color:#ddd}}
h1{{margin:0 0 4px;font-size:17px;font-weight:600;color:#222}}
.subtitle{{font-size:11px;color:#666;margin-bottom:8px}}
.totals{{display:flex;gap:18px;flex-wrap:wrap;font-size:12px;color:#444;margin-bottom:8px}}
.totals span b{{color:#b00;font-weight:600}}
.controls{{display:flex;gap:6px;align-items:center;flex-wrap:wrap}}
button.metric,button.year{{background:#f0f0f0;border:1px solid #d8d8d8;color:#333;padding:5px 12px;font-size:12px;border-radius:4px;cursor:pointer}}
button.metric:hover,button.year:hover{{background:#e8e8e8}}
button.metric.active,button.year.active{{background:#b00;color:white;border-color:#b00}}
button.year{{background:#1a4480;color:white;border-color:#1a4480}}
button.year:hover{{background:#0d2c5a}}
button.year.active{{background:#0d2c5a;border-color:#0d2c5a}}
.divider{{width:1px;height:20px;background:#ccc;margin:0 8px}}
#breadcrumb{{display:none;margin-left:14px;font-size:12px}}
#zoom-hint{{margin-left:14px;font-size:11px;color:#888}}#zoom-hint b{{color:#444}}
#disclaimer-bar{{background:#fff8e1;border-bottom:1px solid #f0d878;padding:6px 20px;font-size:11px;color:#5a4a00;flex-shrink:0;cursor:pointer}}
#disclaimer-bar b{{color:#b00}}#disclaimer-bar .toggle{{float:right;color:#888}}
#disclaimer-body{{display:none;padding:10px 20px;background:#fffbea;border-bottom:1px solid #f0d878;font-size:11px;color:#555;line-height:1.45;flex-shrink:0}}
#disclaimer-body.open{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}}
#disclaimer-body h4{{margin:0 0 4px;font-size:11px;color:#222}}#disclaimer-body ul{{margin:0;padding-left:14px}}
main{{flex:1;display:flex;min-height:0}}
#map-wrap{{flex:1;position:relative;min-width:0;min-height:0}}
#map{{position:absolute!important;inset:0!important;width:100%!important;height:100%!important}}
#panel{{width:0;transition:width 0.2s;background:white;border-left:1px solid #ccc;overflow:hidden;flex-shrink:0}}
#panel.open{{width:560px}}
#panel-inner{{padding:14px 16px;height:100%;overflow-y:auto;width:560px}}
#panel h2{{margin:0 0 4px;font-size:16px;color:#222}}
#panel .meta{{font-size:11px;color:#666;margin-bottom:8px}}
#panel .pct-bar{{display:inline-block;height:6px;background:#eee;width:80px;vertical-align:middle;border-radius:3px;overflow:hidden;margin-left:6px}}
#panel .pct-bar > span{{display:block;height:100%;background:#28a745}}
#panel .totals2{{background:#f8f8f8;border-radius:4px;padding:6px 10px;font-size:11px;margin-bottom:10px;line-height:1.5}}
#panel table{{width:100%;border-collapse:collapse;font-size:11px}}
#panel th{{text-align:left;padding:6px 6px;background:#fafafa;color:#444;font-weight:600;cursor:pointer;user-select:none;border-bottom:2px solid #c8c8c8;position:sticky;top:0;z-index:5;box-shadow:0 2px 4px -2px rgba(0,0,0,0.12)}}
#panel th:hover{{background:#eee;color:#1a4480}}#panel th.right{{text-align:right}}
#panel td{{padding:4px 6px;border-bottom:1px solid #f0f0f0}}#panel td.right{{text-align:right;font-variant-numeric:tabular-nums}}
#panel td.co{{font-weight:500;color:#222}}
#panel .src-m{{display:inline-block;width:7px;height:7px;border-radius:50%;background:#28a745;margin-right:5px;vertical-align:middle}}
#panel .src-e{{display:inline-block;width:7px;height:7px;border-radius:50%;background:#aaa;margin-right:5px;vertical-align:middle}}
.panel-header{{display:flex;align-items:flex-start;justify-content:space-between;gap:8px;margin-bottom:6px}}
.panel-header h2{{margin:0;flex:1;min-width:0}}
#panel-close{{background:#f0f0f0;border:1px solid #ddd;border-radius:4px;width:24px;height:24px;line-height:20px;text-align:center;padding:0;font-size:16px;cursor:pointer;color:#666;flex-shrink:0}}
#panel-close:hover{{background:#e0e0e0;color:#222}}
</style></head>
<body>
<header>
  <div class="attrib">
    <a href="charts.html" title="Charts &amp; rankings"><svg viewBox="0 0 24 24"><path d="M3 13h4v8H3v-8zm7-9h4v17h-4V4zm7 5h4v12h-4V9z"/></svg>Charts</a>
    <span class="sep">·</span>
    <a href="https://deflation.ai" target="_blank" rel="noopener" title="deflation.ai">deflation.ai</a>
    <span class="sep">·</span>
    <a href="https://x.com/TrippelHarry" target="_blank" rel="noopener" title="@TrippelHarry on X"><svg viewBox="0 0 24 24"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z"/></svg>@TrippelHarry</a>
    <span class="sep">·</span>
    <a href="https://github.com/HarryTgerman/us-layoffs-heatmap" target="_blank" rel="noopener" title="source on GitHub"><svg viewBox="0 0 24 24"><path d="M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12"/></svg>repo</a>
  </div>
  <h1>US Layoffs Heatmap</h1>
  <div class="subtitle">Sources: layoffhedge.com · WARN filings · BLS QCEW · BEA county GDP · DOL H-1B disclosures</div>
  <div class="totals" id="totals-row"></div>
  <div class="controls">
    <button class="year active" data-year="2026">2026</button>
    <button class="year" data-year="2025">2025</button>
    <span class="divider"></span>
    <button class="metric" data-metric="people">People</button>
    <button class="metric" data-metric="wages">Wages</button>
    <button class="metric" data-metric="tax">Tax</button>
    <button class="metric" data-metric="equity">Equity</button>
    <button class="metric active" data-metric="total">Total</button>
    <button class="metric" data-metric="gdp_share">% of GDP</button>
    <span id="breadcrumb">
      <button id="breadcrumb-back" style="background:#1a4480;color:white;border:1px solid #1a4480;border-radius:4px;padding:4px 10px;cursor:pointer;font-size:11px;font-weight:600;box-shadow:0 1px 2px rgba(0,0,0,0.15)">← All states</button>
      <span style="margin-left:8px;color:#444"><b id="breadcrumb-state"></b></span>
    </span>
    <span id="zoom-hint">View: <b id="view-label">States</b></span>
  </div>
</header>
<div id="disclaimer-bar" onclick="document.getElementById('disclaimer-body').classList.toggle('open')">
  <b>Methodology</b> — <span id="measured-summary"></span><span class="toggle">▼</span>
</div>
<div id="disclaimer-body">
  <div><h4>Data sources</h4><ul><li>layoffhedge.com (company totals)</li><li>WARN filings (location + worker count)</li><li>BLS QCEW (county wages)</li><li>H1B disclosures (per-company salary + offices)</li></ul></div>
  <div><h4>Measured (●)</h4><ul><li>Layoff locations (WARN)</li><li>Per-company salaries (H1B median)</li><li>Office geography (H1B worksites)</li><li>County wages (BLS QCEW)</li></ul></div>
  <div><h4>Estimated (●)</h4><ul><li>Geography for companies w/o WARN or H1B</li><li>US share of global headcount</li><li>Unvested equity per worker</li></ul></div>
  <div><h4>2025 vs 2026</h4><ul><li><b>2025</b>: WARN-only — 100% measured</li><li><b>2026</b>: WARN + layoffhedge with estimation for unmatched</li></ul></div>
</div>
<main>
  <div id="map-wrap">{plot_div}</div>
  <div id="panel"><div id="panel-inner"></div></div>
</main>
<script>
const DATA = {json.dumps(ALL_DATA)};
const gd = document.getElementById('map');
let year = '2026';
let metric = 'total';
let viewLevel = 'state';
let focusedState = null;
let sortKey = 'total', sortDir = -1;
const USA_CENTER = {{lat: 39.5, lon: -98.35}};
const USA_ZOOM = 3.4;

function P(){{ return DATA.years[year]; }}

function fmtMoney(x){{if(x>=1e9)return '$'+(x/1e9).toFixed(2)+'B';if(x>=1e6)return '$'+(x/1e6).toFixed(1)+'M';if(x>=1e3)return '$'+(x/1e3).toFixed(0)+'K';return '$'+Math.round(x);}}
function fmtNum(x){{return Math.round(x).toLocaleString();}}

function renderTotals(){{
  const t = P().grand_totals;
  document.getElementById('totals-row').innerHTML =
    '<span>Year: <b>'+year+'</b></span>'+
    '<span>People: <b>'+fmtNum(t.people)+'</b></span>'+
    '<span>Wages: <b>'+fmtMoney(t.wages)+'</b></span>'+
    '<span>Tax: <b>'+fmtMoney(t.tax)+'</b></span>'+
    '<span>Equity: <b>'+fmtMoney(t.equity)+'</b></span>'+
    '<span>Total: <b>'+fmtMoney(t.total)+'</b></span>';
  document.getElementById('measured-summary').textContent =
    year + ': ' + P().measured_pct + '% WARN-measured. Wages from BLS QCEW county-level data. Tax = per-state effective rate.';
}}

function buildHoverCustom(rawIdx, fips, isCounty){{
  const py = P();
  const raws = isCounty ? py.county_raw : py.state_raw;
  const pctMap = isCounty ? py.panel_pct_county : py.panel_pct_state;
  return [fips, raws.people[rawIdx]||0, raws.wages[rawIdx]||0, raws.tax[rawIdx]||0,
          raws.equity[rawIdx]||0, raws.total[rawIdx]||0, raws.gdp_share[rawIdx]||0, pctMap[fips]||0];
}}

function tickFmtFor(m){{
  return m === 'gdp_share' ? '.3f' : '~s';
}}

function applyStateLayer(){{
  const zr = P().zranges_state[metric];
  // Show only states with data; null out the rest so grey backdrop shows through
  const z = P().state_raw[metric].map(v => (v && v > 0) ? v : null);
  const cd = DATA.state_geo_ids.map((g,i) => buildHoverCustom(i, g, false));
  Plotly.restyle(gd, {{
    z: [z], customdata: [cd],
    zmin: zr[0], zmax: zr[1],
    'colorbar.title.text': '<b>'+DATA.metric_labels[metric]+'</b>',
    'colorbar.tickformat': tickFmtFor(metric),
  }}, [1]);
}}

function applyCountyLayers(stateFips){{
  if (!stateFips){{
    Plotly.restyle(gd, {{locations:[[]], z:[[]], text:[[]]}}, [2]);
    Plotly.restyle(gd, {{locations:[[]], z:[[]], text:[[]], customdata:[[]]}}, [3]);
    return;
  }}
  const emptyLocs=[], emptyText=[];
  const dataLocs=[], dataZ=[], dataText=[], dataCd=[];
  const py = P();
  for (let i=0; i<DATA.all_fips.length; i++){{
    if (DATA.county_state_fips[i] !== stateFips) continue;
    const f = DATA.all_fips[i];
    const v = py.county_raw[metric][i];
    if (v === null || v === undefined || v === 0){{
      emptyLocs.push(f); emptyText.push(DATA.county_name[i]);
    }} else {{
      dataLocs.push(f); dataZ.push(v); dataText.push(DATA.county_name[i]);
      dataCd.push(buildHoverCustom(i, f, true));
    }}
  }}
  Plotly.restyle(gd, {{locations:[emptyLocs], z:[emptyLocs.map(()=>0.5)], text:[emptyText]}}, [2]);
  const zr = py.zranges_county[metric];
  Plotly.restyle(gd, {{locations:[dataLocs], z:[dataZ], text:[dataText], customdata:[dataCd],
    zmin: zr[0], zmax: zr[1], 'colorbar.tickformat': tickFmtFor(metric)}}, [3]);
}}

function zoomForBbox(b){{
  const span = Math.max(b[3]-b[1], (b[2]-b[0])*1.6);
  return Math.max(3.5, Math.min(8, Math.log2(360/span) - 0.4));
}}

function enterStateView(){{
  viewLevel='state'; focusedState=null;
  Plotly.restyle(gd, {{visible:true}}, [0]);
  Plotly.restyle(gd, {{visible:true, showscale:true}}, [1]);
  applyStateLayer();
  applyCountyLayers(null);
  Plotly.restyle(gd, {{visible:false}}, [2]);
  Plotly.restyle(gd, {{visible:false, showscale:false}}, [3]);
  Plotly.relayout(gd, {{'mapbox.center': USA_CENTER, 'mapbox.zoom': USA_ZOOM}});
  document.getElementById('breadcrumb').style.display = 'none';
  document.getElementById('view-label').textContent = 'States — click any state to drill into counties';
}}

function enterCountyView(stateFips){{
  viewLevel='county'; focusedState=stateFips;
  Plotly.restyle(gd, {{visible:false}}, [0]);
  Plotly.restyle(gd, {{visible:false, showscale:false}}, [1]);
  applyCountyLayers(stateFips);
  Plotly.restyle(gd, {{visible:true}}, [2]);
  Plotly.restyle(gd, {{visible:true, showscale:true}}, [3]);
  const bbox = DATA.state_bbox[stateFips]; const ctr = DATA.state_center[stateFips];
  if (bbox && ctr) Plotly.relayout(gd, {{'mapbox.center':{{lat:ctr[0], lon:ctr[1]}}, 'mapbox.zoom': zoomForBbox(bbox)}});
  const sn = DATA.state_name[DATA.state_geo_ids.indexOf(stateFips)] || '';
  document.getElementById('breadcrumb').style.display = 'inline-block';
  document.getElementById('breadcrumb-state').textContent = sn;
  document.getElementById('view-label').textContent = 'Counties of ' + sn;
}}

function setMetric(m){{
  metric = m;
  document.querySelectorAll('button.metric').forEach(b => b.classList.toggle('active', b.dataset.metric === m));
  applyStateLayer();
  if (viewLevel === 'county') applyCountyLayers(focusedState);
}}

function setYear(y){{
  year = y;
  document.querySelectorAll('button.year').forEach(b => b.classList.toggle('active', b.dataset.year === y));
  renderTotals();
  applyStateLayer();
  if (viewLevel === 'county') applyCountyLayers(focusedState);
}}

renderTotals();
enterStateView();

document.querySelectorAll('button.metric').forEach(b => b.onclick = () => setMetric(b.dataset.metric));
document.querySelectorAll('button.year').forEach(b => b.onclick = () => setYear(b.dataset.year));
document.getElementById('breadcrumb-back').onclick = (e) => {{ e.stopPropagation(); enterStateView(); }};

gd.on('plotly_click', (ev) => {{
  if (!ev.points || !ev.points.length) return;
  const pt = ev.points[0];
  if (viewLevel === 'state'){{
    // Either backdrop (0) or data state (1) — both should drill
    if (pt.curveNumber !== 0 && pt.curveNumber !== 1) return;
    const sfp = pt.location;
    const sd = P().panel_state[sfp];
    if (sd){{
      renderPanel(sd, false);
      document.getElementById('panel').classList.add('open');
    }}
    enterCountyView(sfp);
    setTimeout(() => Plotly.Plots.resize(gd), 220);
  }} else {{
    if (pt.curveNumber !== 3) return;
    const fips = pt.customdata && pt.customdata[0];
    if (!fips) return;
    const d = P().panel_county[fips];
    if (!d) return;
    renderPanel(d, true);
    document.getElementById('panel').classList.add('open');
    setTimeout(() => Plotly.Plots.resize(gd), 220);
  }}
}});

function closePanel(){{
  document.getElementById('panel').classList.remove('open');
  setTimeout(() => Plotly.Plots.resize(gd), 220);
}}
window.closePanel = closePanel;

function renderPanel(d, isCounty){{
  const sorted = [...d.companies].sort((a,b)=>sortDir*(a[sortKey]>b[sortKey]?1:a[sortKey]<b[sortKey]?-1:0));
  const rows = sorted.map(c => {{
    const dot = c.source==='measured' ? '<span class="src-m" title="WARN"></span>' : '<span class="src-e" title="Estimated"></span>';
    return '<tr><td class="co">'+dot+c.co+'</td>'+
      '<td class="right">'+fmtNum(c.people)+'</td>'+
      '<td class="right">'+fmtMoney(c.wages)+'</td>'+
      '<td class="right">'+fmtMoney(c.tax)+'</td>'+
      '<td class="right">'+fmtMoney(c.equity)+'</td>'+
      '<td class="right">'+fmtMoney(c.total)+'</td>'+
      '<td class="right">'+(c.offices||1)+'</td></tr>';
  }}).join('');
  const t = d.totals;
  const ctx = isCounty
    ? ('<div style="font-size:11px;color:#666;margin-bottom:6px">QCEW employment: '+fmtNum(d.qcew_emp||0)+' private workers in major sectors · Tax rate: '+(d.tax_rate||0)+'%</div>')
    : ('<div style="font-size:11px;color:#666;margin-bottom:6px">State effective tax rate: '+(d.tax_rate||0).toFixed(1)+'%</div>');
  document.getElementById('panel-inner').innerHTML =
    '<div class="panel-header">'+
      '<h2>'+d.name+' <span style="font-weight:400;font-size:12px;color:#888">· '+year+'</span></h2>'+
      '<button id="panel-close" onclick="closePanel()" title="close">×</button>'+
    '</div>'+
    '<div class="meta">'+d.measured_pct+'% WARN-measured'+
    '<span class="pct-bar"><span style="width:'+d.measured_pct+'%"></span></span></div>'+
    ctx +
    '<div class="totals2"><b>People:</b> '+fmtNum(t.people)+' &nbsp; <b>Wages:</b> '+fmtMoney(t.wages)+' &nbsp; <b>Tax:</b> '+fmtMoney(t.tax)+'<br><b>Equity:</b> '+fmtMoney(t.equity)+' &nbsp; <b>Total:</b> '+fmtMoney(t.total)+'</div>'+
    '<table><thead><tr><th data-sk="co">Company</th><th class="right" data-sk="people">People</th><th class="right" data-sk="wages">Wages</th><th class="right" data-sk="tax">Tax</th><th class="right" data-sk="equity">Equity</th><th class="right" data-sk="total">Total</th><th class="right" data-sk="offices" title="distinct counties in this company\\'s footprint">Sites</th></tr></thead><tbody>'+rows+'</tbody></table>';
  document.querySelectorAll('#panel th').forEach(th => {{
    th.onclick = () => {{
      const sk = th.dataset.sk;
      if (sortKey===sk) sortDir = -sortDir; else {{sortKey=sk; sortDir = sk==='co'?1:-1;}}
      renderPanel(d, isCounty);
    }};
  }});
}}

window.addEventListener('resize', () => Plotly.Plots.resize(gd));
</script>
</body></html>"""

with open("index.html", "w") as f:
    f.write(html)

# ============================================================================
# 9. Charts subpage (charts.html) — company/state rankings + year comparison
# ============================================================================

charts_template = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>US Layoffs — Charts (2025 &amp; 2026)</title>
<style>
*{box-sizing:border-box}
html,body{margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#fafafa;color:#222}
header{background:white;border-bottom:1px solid #e0e0e0;padding:10px 20px 8px;position:relative}
.attrib{position:absolute;top:10px;right:20px;display:flex;gap:10px;align-items:center;font-size:11px;color:#999}
.attrib a{color:#888;text-decoration:none;display:inline-flex;align-items:center;gap:4px;padding:2px 4px;border-radius:3px;transition:all 0.15s}
.attrib a:hover{color:#1a4480;background:#f3f6fb}
.attrib svg{width:13px;height:13px;fill:currentColor}
.attrib .sep{color:#ddd}
h1{margin:0 0 4px;font-size:17px;font-weight:600;color:#222}
.subtitle{font-size:11px;color:#666;margin-bottom:8px}
.controls{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
button.metric,button.year{background:#f0f0f0;border:1px solid #d8d8d8;color:#333;padding:5px 12px;font-size:12px;border-radius:4px;cursor:pointer}
button.metric:hover,button.year:hover{background:#e8e8e8}
button.metric.active{background:#b00;color:white;border-color:#b00}
button.year{background:#1a4480;color:white;border-color:#1a4480}
button.year:hover{background:#0d2c5a}
button.year.active{background:#0d2c5a;border-color:#0d2c5a}
.divider{width:1px;height:20px;background:#ccc;margin:0 8px}
.wrap{max-width:1000px;margin:0 auto;padding:18px 20px 60px}
.kpis{display:flex;gap:12px;flex-wrap:wrap;margin:6px 0}
.kpi{background:white;border:1px solid #e6e6e6;border-radius:8px;padding:11px 15px;flex:1;min-width:120px}
.kpi .v{font-size:21px;font-weight:700;color:#b00;font-variant-numeric:tabular-nums}
.kpi .l{font-size:11px;color:#777;margin-top:2px}
section{background:white;border:1px solid #e8e8e8;border-radius:10px;padding:16px 18px;margin-top:18px}
section h2{margin:0 0 2px;font-size:15px;color:#222}
.hint{font-size:11px;color:#888;margin-bottom:12px}
.legend{float:right;font-size:11px;color:#888;font-weight:400}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin:0 4px 0 10px;vertical-align:middle}
.dot.m{background:#28a745}.dot.e{background:#bbb}
.bar-row{display:grid;grid-template-columns:215px 1fr 108px;align-items:center;gap:10px;margin-bottom:5px;font-size:12px}
.bar-label{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#333}
.bar-label .rank{display:inline-block;width:20px;color:#bbb;text-align:right;margin-right:7px;font-variant-numeric:tabular-nums}
.src-m,.src-e{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px;vertical-align:middle;flex-shrink:0}
.src-m{background:#28a745}.src-e{background:#bbb}
.bar-track{background:#f1f1f1;border-radius:4px;height:17px;overflow:hidden}
.bar-fill{height:100%;border-radius:4px;background:linear-gradient(90deg,#ffb24d,#b00)}
.bar-fill.g{background:#cfcfcf}
.bar-val{text-align:right;font-variant-numeric:tabular-nums;color:#444}
.cmp-row{display:grid;grid-template-columns:110px 1fr 64px;align-items:center;gap:14px;margin-bottom:11px;font-size:12px}
.cmp-lbl{color:#333;font-weight:600}
.cmp-line{display:grid;grid-template-columns:34px 1fr 92px;align-items:center;gap:8px;margin:2px 0}
.cmp-line .yr{font-size:10px;color:#999}
.cmp-v{text-align:right;font-variant-numeric:tabular-nums;color:#444}
.cmp-delta{text-align:right;font-weight:600;font-variant-numeric:tabular-nums}
.cmp-delta.up{color:#b00}.cmp-delta.down{color:#28a745}
@media(max-width:640px){.bar-row{grid-template-columns:128px 1fr 76px}}
</style></head>
<body>
<header>
  <div class="attrib">
    <a href="index.html" title="Back to the map"><svg viewBox="0 0 24 24"><path d="M20.5 3l-.16.03L15 5.1 9 3 3.36 4.9c-.21.07-.36.25-.36.48V20.5c0 .28.22.5.5.5l.16-.03L9 18.9l6 2.1 5.64-1.9c.21-.07.36-.25.36-.48V3.5c0-.28-.22-.5-.5-.5zM15 19l-6-2.11V5l6 2.11V19z"/></svg>Map</a>
    <span class="sep">&middot;</span>
    <a href="https://deflation.ai" target="_blank" rel="noopener" title="deflation.ai">deflation.ai</a>
    <span class="sep">&middot;</span>
    <a href="https://x.com/TrippelHarry" target="_blank" rel="noopener" title="@TrippelHarry on X"><svg viewBox="0 0 24 24"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z"/></svg>@TrippelHarry</a>
    <span class="sep">&middot;</span>
    <a href="https://github.com/HarryTgerman/us-layoffs-heatmap" target="_blank" rel="noopener" title="source on GitHub"><svg viewBox="0 0 24 24"><path d="M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12"/></svg>repo</a>
  </div>
  <h1>US Layoffs &mdash; Charts</h1>
  <div class="subtitle">Company &amp; state rankings &middot; <a href="index.html" style="color:#1a4480;text-decoration:none">open the interactive map &rarr;</a></div>
  <div class="controls">
    <button class="year active" data-year="2026">2026</button>
    <button class="year" data-year="2025">2025</button>
    <span class="divider"></span>
    <button class="metric" data-metric="people">People</button>
    <button class="metric" data-metric="wages">Wages</button>
    <button class="metric" data-metric="tax">Tax</button>
    <button class="metric" data-metric="equity">Equity</button>
    <button class="metric active" data-metric="total">Total</button>
  </div>
</header>
<div class="wrap">
  <div class="kpis" id="kpis"></div>
  <section>
    <h2>Top companies <span class="legend"><span class="dot m"></span>WARN-measured<span class="dot e"></span>estimated</span></h2>
    <div class="hint" id="co-hint"></div>
    <div id="co-chart"></div>
  </section>
  <section>
    <h2>Top states</h2>
    <div class="hint" id="st-hint"></div>
    <div id="st-chart"></div>
  </section>
  <section>
    <h2>2025 vs 2026</h2>
    <div class="hint">National totals for both years. &Delta; is 2026 vs 2025 &mdash; red = higher in 2026, green = lower.</div>
    <div id="cmp-chart"></div>
  </section>
</div>
<script>
const CHART = __CHART_DATA__;
let year = '2026', metric = 'total';

function fmtMoney(x){if(x>=1e9)return '$'+(x/1e9).toFixed(2)+'B';if(x>=1e6)return '$'+(x/1e6).toFixed(1)+'M';if(x>=1e3)return '$'+(x/1e3).toFixed(0)+'K';return '$'+Math.round(x);}
function fmtNum(x){return Math.round(x).toLocaleString();}
function fmtMetric(v,m){return m==='people'?fmtNum(v):fmtMoney(v);}
function Y(){return CHART.years[year];}

function renderKpis(){
  const t = Y().grand_totals;
  const items = [['people','People'],['total','Total Impact'],['wages','Wages'],['tax','Tax Lost'],['equity','Equity']];
  let html = items.map(it => '<div class="kpi"><div class="v">'+fmtMetric(t[it[0]],it[0])+'</div><div class="l">'+it[1]+'</div></div>').join('');
  html += '<div class="kpi"><div class="v">'+Y().measured_pct+'%</div><div class="l">WARN-measured</div></div>';
  document.getElementById('kpis').innerHTML = html;
}

function barChart(el, rows, labelFn, withDot){
  const max = Math.max.apply(null, rows.map(r => r[metric]).concat([1]));
  el.innerHTML = rows.map((r,i) => {
    const w = (r[metric] / max * 100).toFixed(1);
    const dot = withDot && r.source ? '<span class="src-'+(r.source==='measured'?'m':'e')+'" title="'+(r.source==='measured'?'WARN-measured':'estimated')+'"></span>' : '';
    return '<div class="bar-row"><div class="bar-label"><span class="rank">'+(i+1)+'</span>'+dot+labelFn(r)+'</div>'+
      '<div class="bar-track"><div class="bar-fill" style="width:'+w+'%"></div></div>'+
      '<div class="bar-val">'+fmtMetric(r[metric],metric)+'</div></div>';
  }).join('');
}

function renderCompanies(){
  const rows = Y().companies.slice().sort((a,b)=>b[metric]-a[metric]).slice(0,20);
  document.getElementById('co-hint').textContent = 'Top 20 of '+Y().n_companies+' companies by '+CHART.metric_labels[metric]+', '+year+'.';
  barChart(document.getElementById('co-chart'), rows, r=>r.co, true);
}

function renderStates(){
  const rows = Y().states.slice().sort((a,b)=>b[metric]-a[metric]).slice(0,15);
  document.getElementById('st-hint').textContent = 'Top 15 of '+Y().n_states+' states by '+CHART.metric_labels[metric]+', '+year+'.';
  barChart(document.getElementById('st-chart'), rows, r=>r.name, false);
}

function renderCompare(){
  const a = CHART.years['2025'].grand_totals, b = CHART.years['2026'].grand_totals;
  document.getElementById('cmp-chart').innerHTML = CHART.metrics.map(m => {
    const va = a[m]||0, vb = b[m]||0, mx = Math.max(va,vb,1);
    const pct = va>0 ? Math.round((vb-va)/va*100) : null;
    const fmt = v => fmtMetric(v,m);
    const sign = pct===null ? '\\u2014' : (pct>=0?'+':'')+pct+'%';
    return '<div class="cmp-row"><div class="cmp-lbl">'+CHART.metric_labels[m]+'</div><div>'+
      '<div class="cmp-line"><span class="yr">2025</span><div class="bar-track"><div class="bar-fill g" style="width:'+(va/mx*100).toFixed(1)+'%"></div></div><span class="cmp-v">'+fmt(va)+'</span></div>'+
      '<div class="cmp-line"><span class="yr">2026</span><div class="bar-track"><div class="bar-fill" style="width:'+(vb/mx*100).toFixed(1)+'%"></div></div><span class="cmp-v">'+fmt(vb)+'</span></div>'+
      '</div><div class="cmp-delta '+(pct>=0?'up':'down')+'">'+sign+'</div></div>';
  }).join('');
}

function renderAll(){ renderKpis(); renderCompanies(); renderStates(); renderCompare(); }

document.querySelectorAll('button.year').forEach(b => b.onclick = () => {
  year = b.dataset.year;
  document.querySelectorAll('button.year').forEach(x => x.classList.toggle('active', x===b));
  renderAll();
});
document.querySelectorAll('button.metric').forEach(b => b.onclick = () => {
  metric = b.dataset.metric;
  document.querySelectorAll('button.metric').forEach(x => x.classList.toggle('active', x===b));
  renderAll();
});
renderAll();
</script>
</body></html>"""
charts_html = charts_template.replace("__CHART_DATA__", json.dumps(CHART_DATA))
with open("charts.html", "w") as f:
    f.write(charts_html)

print(f"  Done. index.html: {len(html)/1024/1024:.1f} MB")
print(f"  Done. charts.html: {len(charts_html)/1024:.0f} KB")
print(f"  2026: {len(agg_2026)} counties, totals {payload_2026['grand_totals']['people']:,} people, {payload_2026['measured_pct']}% measured")
print(f"  2025: {len(agg_2025)} counties, totals {payload_2025['grand_totals']['people']:,} people, {payload_2025['measured_pct']}% measured")
