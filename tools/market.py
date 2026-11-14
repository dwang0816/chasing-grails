#!/usr/bin/env python3
"""Chasing Grails: look up TCGplayer market prices for the Analytics products.

Usage: python3 market.py wanted.json market.json [old_market.json]
  wanted.json    list of {key, game, set, type, lang, name}: the products the website wants priced
                 (window.CG.wanted = the Analytics top 40 + every live deal), printed by: node wanted.js <site url> [data dir]
  market.json    written: one row per product key that has a TCGplayer match
  old_market.json  optional previous file; a row checked in the last 3 days is kept when this lookup finds nothing
                   (that's how eBay rows the scan added by hand survive until it re-checks them)

Only the public TCGplayer search API is used (mp-search-api.tcgplayer.com), read-only.
"""
import json, re, subprocess, sys, time, urllib.parse
from datetime import datetime, timezone

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130 Safari/537.36'
LINES = {
    'Pokémon': {'': ['pokemon'], 'Japanese': ['pokemon-japan']},
    'One Piece': {'': ['one-piece-card-game']},
    'Riftbound': {'': ['riftbound-league-of-legends-trading-card-game']},
    'Weiss Schwarz': {'': ['weiss-schwarz']},
    'Other Bandai': {'': ['union-arena', 'dragon-ball-super-fusion-world', 'gundam-card-game', 'digimon-card-game']},
    'Cyberpunk': {'': [None]},  # no known line slug: search all lines, keep Cyberpunk results
    'Divine Cross': {'': [None]},
}
# What the product name must (and must not) contain for each product type.
TYPE_RULES = [
    (r'booster case|case', r'booster (box|display) case', r'elite trainer|bundle|pack case'),
    (r'collector booster', r'collector booster (box|display)', r'case|pack$'),
    (r'play booster', r'play booster (box|display)', r'case|pack$'),
    (r'booster box|booster display|extra booster|premium booster|precious booster', r'booster (box|display)', r'case|half|pack$|sleeved|bundle'),
    (r'elite trainer box', r'elite trainer box', r'case|pok[eé]mon center'),
    (r'ultra[- ]premium collection', r'(ultra|super)[- ]premium collection', r'case'),
    (r'premium collection', r'premium collection', r'case|ultra|super|figure'),
    (r'booster bundle', r'booster bundle', r'case|display'),
    (r'double pack', r'double pack', r'display|case'),
    (r'sleeved', r'sleeved', r'display|case|box'),
    (r'mini tins?', r'mini tin', r'display|case'),
    (r'\btin\b', r'\btin\b', r'display|case|mini'),
]
# Multi-packs, retailer combos and lots are never the single product's market price.
NEVER_ANY = r'set of|\b\d+ ?-?pack\b|costco|sam s club|dollar general|art bundle|\band\b.*\b(box|bundle|pokeball)|\blot\b'
STOP = {'the', 'of', 'and', 'a', 'an', 'vol', 'edition', 'set', 'booster', 'box', 'collection', 'pokemon', 'one', 'piece'}

def norm(s):
    s = (s or '').lower().replace('é', 'e')
    return re.sub(r'[^a-z0-9]+', ' ', s).strip()

def search(q, line):
    term = {'productTypeName': ['Sealed Products']}
    if line: term['productLineName'] = [line]
    body = {'algorithm': 'sales_dismax', 'from': 0, 'size': 24, 'filters': {'term': term, 'range': {}, 'match': {}},
            'listingSearch': {'context': {'cart': {}}, 'filters': {'term': {'sellerStatus': 'Live', 'channelId': 0}, 'range': {'quantity': {'gte': 1}}, 'exclude': {'channelExclusion': 0}}},
            'context': {'cart': {}, 'shippingCountry': 'US', 'userProfile': {}}, 'settings': {'useFuzzySearch': True, 'didYouMean': {}}, 'sort': {}}
    url = 'https://mp-search-api.tcgplayer.com/v1/search/request?q=' + urllib.parse.quote(q) + '&isList=false'
    for attempt in range(3):
        r = subprocess.run(['curl', '-s', '-m', '25', '-X', 'POST', '-H', 'Content-Type: application/json', '-A', UA, url, '--data', json.dumps(body)], capture_output=True, text=True)
        try:
            return json.loads(r.stdout)['results'][0]['results']
        except Exception:
            time.sleep(3 * (attempt + 1))
    return None

def type_rule(ptype):
    t = norm(ptype)
    for key, must, never in TYPE_RULES:
        if re.search(key, t):
            return must, never
    # Other sealed products (figure / poster / premium collection, battle deck, blister...): name must contain the type words.
    return r'\b' + r'\s+'.join(re.escape(w) for w in t.split()) + r's?\b', r'case|display'

def queries(p):
    # Several phrasings: TCGplayer's fuzzy search ranks some sets' bundles and kits above the box.
    t = re.sub(r'(?i)\bbox\b', '', p['type']).strip()
    out = [p['set'] + ' ' + p['type'], p['set'] + ' ' + t]
    short = re.sub(r'(?i)^(gundam|union arena|starter deck)\s*[-:]?\s*', '', p['set']).strip(' -')
    if short != p['set']: out.append(short + ' ' + p['type'])
    seen = []
    for q in out:
        if q not in seen: seen.append(q)
    return seen

def match(p):
    lang = p.get('lang') or ''
    lines = LINES.get(p['game'], {}).get(lang) or ([] if lang else [None])
    if not lines:
        return None, 'no TCGplayer line for %s %s' % (p['game'], lang or 'English')
    must, never = type_rule(p['type'])
    set_toks = [w for w in norm(p['set']).split() if (len(w) > 1 or w.isdigit()) and w not in STOP] or norm(p['set']).split()
    best = []
    for line in lines:
        res = []
        for q in queries(p):
            got = search(q, line)
            if got is None:
                return None, 'search failed'
            res += got
        for r in {int(x['productId']): x for x in res}.values():
            name = norm(r.get('productName')); hay = name + ' ' + norm(r.get('setName')) + ' ' + norm(r.get('productLineName'))
            if line is None and p['game'] not in (r.get('productLineName') or ''):
                continue
            words = hay.split()
            if not all(t in words if t.isdigit() else (t in words or t in hay) for t in set_toks):
                continue
            if re.search(NEVER_ANY, name):
                continue
            if not re.search(must, name) or (never and re.search(never, name)):
                continue
            if r.get('marketPrice') is None:
                continue
            best.append(r)
        if best:
            break
    if not best:
        return None, 'no match'
    best.sort(key=lambda r: r['marketPrice'])
    # Versions of the same product (Day/Night UPC, one tin per Pokémon) price close together; something far pricier is another product.
    best = [r for r in best if r['marketPrice'] <= best[0]['marketPrice'] * 1.25]
    return best, None

def main():
    prods = json.load(open(sys.argv[1]))
    old = {}
    if len(sys.argv) > 3:
        try:
            old = {r['key']: r for r in json.load(open(sys.argv[3]))}
        except Exception:
            old = {}
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    out, report = [], []
    for p in prods:
        best, why = match(p)
        if best:
            top = best[0]
            out.append({'key': p['key'], 'name': p.get('name'), 'source': 'TCGplayer', 'market_price': round(float(top['marketPrice']), 2),
                        'url': 'https://www.tcgplayer.com/product/%d' % int(top['productId']), 'product': top['productName'],
                        'market_max': round(max(float(r['marketPrice']) for r in best[:4]), 2),
                        'variants': [{'name': r['productName'], 'market': round(float(r['marketPrice']), 2), 'url': 'https://www.tcgplayer.com/product/%d' % int(r['productId'])} for r in best[:4]],
                        'checked_at': now})
            report.append('OK   %-60s $%-8.2f %s' % (p.get('name', p['key'])[:60], top['marketPrice'], top['productName']))
        else:
            prev = old.get(p['key'])
            fresh = prev and (datetime.now(timezone.utc) - datetime.fromisoformat(prev.get('checked_at', '2000-01-01T00:00:00Z').replace('Z', '+00:00'))).days < 3
            if fresh:
                out.append(prev)
                report.append('KEPT %-60s (%s; kept previous)' % (p.get('name', p['key'])[:60], why))
            else:
                report.append('--   %-60s (%s)' % (p.get('name', p['key'])[:60], why))
        time.sleep(0.4)
    json.dump(out, open(sys.argv[2], 'w'), ensure_ascii=False)
    print('\n'.join(report))
    print('matched %d of %d' % (len(out), len(prods)))

if __name__ == '__main__':
    main()
