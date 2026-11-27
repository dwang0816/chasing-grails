#!/usr/bin/env python3
"""Chasing Grails: sweep every saved Shopify store's sealed catalog for deals.

Usage (run from a folder holding market.py, sources.json and the data files):
  python3 deals.py sweep sources.json store_deals.json deal_matches.json [news.json]
      Crawls each Shopify store in sources.json (game stores, official stores, Japan imports), keeps
      sealed products of the tracked games that can be bought or pre-ordered, matches each one to its
      TCGplayer product and market price, and rewrites store_deals.json with every listing priced at or
      under market. deal_matches.json caches the TCGplayer matches between runs.
      news.json (optional) is only read, to reuse an official MSRP the scan already recorded for a product.
  python3 deals.py lookup "<listing title>" "<game>" [deal_matches.json]
      Matches one listing title (e.g. from GameNerdz or Miniature Market) and prints the TCGplayer product
      and market price as JSON, for rows the scan adds by hand.
  python3 deals.py listings listings.json "<store name>" https://<store domain> <currency> store_deals.json deal_matches.json [news.json]
      Browser fallback for a store that blocked the sweep: listings.json is the products the store's own page
      returned (Shopify products.json shape, prices already in US dollars). Replaces that store's rows.
  python3 deals.py reject "<listing title>" "<game>" deal_matches.json
      Marks a wrong match (store_title of a store_deals.json row) so later sweeps never use it again.

Read-only everywhere: public Shopify feeds (/products.json, /collections.json, /products/<handle>.js)
and TCGplayer's public search API. Nothing is bought, carted or signed in to.
"""
import json, re, subprocess, sys, time, urllib.parse
from datetime import datetime, timezone, timedelta
sys.path.insert(0, __import__('os').path.dirname(__file__) or '.')
from market import search, norm, type_rule, NEVER_ANY, LINES, UA

NOW = datetime.now(timezone.utc)
NOW_S = NOW.strftime('%Y-%m-%dT%H:%M:%SZ')
SWEEP_CHANNELS = {'Local game stores', 'Official stores', 'Japan imports'}
MAX_PRODUCTS = 3000          # per store
MATCH_RETRY_DAYS = 3         # retry a "no TCGplayer match" title after this long
PRICE_REFRESH_HOURS = 8      # refresh a cached market price after this long

GAME_RX = [
    ('Pokémon', r'pok[eé]mon'),
    ('One Piece', r'one ?piece|\b(op|eb|prb|st)-?\d{2}\b'),
    ('Riftbound', r'riftbound'),
    ('Cyberpunk', r'cyberpunk|night city|edgerunners'),
    ('Weiss Schwarz', r'weiss|schwarz'),
    ('Divine Cross', r'divine cross'),
    ('Other Bandai', r'gundam|dragon ?ball|fusion world|union arena|naruto'),
]
SKIP_GAME = r'magic: the gathering|\bmtg\b|yu-?gi-?oh|lorcana|flesh and blood|digimon|star wars|sorcery|grand archive|altered|vanguard|metazoo|sports|baseball|basketball|football|hockey'
LANG_RX = [('Japanese', r'\[jp\]|\bjapanese\b|\bjpn?\b|\(jp\)'), ('Chinese', r'\[s?t?cn\]|chinese|\bscn\b|\btcn\b'),
           ('Korean', r'\[kr\]|korean'), ('Thai', r'\bthai\b'), ('Indonesian', r'indonesian')]
# Listing title -> product type (first match wins). The type picks the TCGplayer search words and match rule.
TYPES = [
    # Displays and cases of anything but booster boxes (tin displays, ETB cases, double pack displays): not tracked.
    ('skip', r'(double pack|tins?|bundles?|etb|elite trainer box|decks?|collection|ex box)\b.*\b(display|case)\b'),
    ('booster case', r'(bundle|elite trainer box|etb|tin|pack)s? (display|case)\b|\bcase of\b'),
    ('tin', r'mini case set|mini tins?|\btins?\b|tin pack'),
    ('booster case', r'\bcases?\b|case of \d|\(\d+ (boxes|displays)\)|\d+ ?x ?booster box'),
    ('sleeved booster', r'sleeved booster'),
    ('double pack', r'double pack'),
    ('booster bundle', r'booster bundle'),
    ('elite trainer box', r'elite trainer box|\betb\b'),
    ('ultra premium collection', r'(ultra|super)[- ]premium collection|\bupc\b|\bspc\b'),
    ('collector booster box', r'collector booster (box|display)'),
    ('play booster box', r'play booster (box|display)'),
    ('booster box', r'booster (box|display)|display box|\bbooster\s*box\b|precious booster|premium booster box|extra booster.*\bbox\b|\bbox\b.*\b\d{2} packs\b|\(\d{2} packs\)'),
    ('premium collection', r'premium collection'),
    ('figure collection', r'figure collection'),
    ('poster collection', r'poster collection'),
    ('binder collection', r'binder collection'),
    ('premium card collection', r'premium card collection'),
    ('starter deck', r'starter deck'),
    ('battle deck', r'battle deck|deluxe battle deck'),
    ('build & battle', r'build (&|and) battle'),
    ('booster vault', r'booster vault'),
    ('collection', r'\bcollection\b|gift box|special set|premium set'),
]
DEAL_CLASS = {'booster case': 'box', 'collector booster box': 'box', 'play booster box': 'box', 'booster box': 'box',
              'elite trainer box': 'etb', 'ultra premium collection': 'upc', 'booster bundle': 'bundle',
              'double pack': 'double_pack', 'tin': 'tin', 'sleeved booster': 'sleeved_pack'}
NOT_SEALED = (r'set of \d|\b\d+ ?x\b(?!\s*booster box)|\bbundle of\b|\bsingle\b|\bgraded\b|\bpsa\b|\bcgc\b|\bbgs\b|\bslab|\blot\b|mystery|repack|\bbreak\b|random|opened|\bempty\b|'
              r'playmat|play mat|\bsleeves?\b(?! booster)|binder(?! collection)|deck ?box(?!es)|top ?loader|portfolio|storage|'
              r'\bdice\b|\bpins?\b(?! collection)|plush|figure(?! collection)|t-shirt|hoodie|poster(?! collection)|\bcoin\b(?! bundle)|'
              r'\bproxy\b|custom|acrylic|ultra pro|protector|magnetic|display case|case for|\bfor pok[eé]mon\b|\bpack\b(?!s)(?!.*(box|display|bundle|collection))|booster pack(?!s)|single pack|\bjumbo\b|\bslim\b')
NOISE = (r'\([^)]*(pre-?order|ships?|release|preorder|english|limit|sealed|new)[^)]*\)|\*+[^*]*\*+|'
         r'pre-?order|presale|\bships? [a-z]+ \d+.*$|\brelease date.*$|\bfactory sealed\b|\bsealed\b|\benglish\b|'
         r'pok[eé]mon (trading card game|tcg)|one piece (card game|tcg)|trading card game|\btcg\b|\bccg\b|:|–|—|\|')
STOP = {'the', 'of', 'and', 'a', 'an', 'vol', 'edition', 'set', 'booster', 'box', 'boxes', 'collection', 'pokemon', 'one', 'piece',
        'card', 'cards', 'game', 'display', 'case', 'pack', 'packs', 'new', 'japanese', 'chinese', 'korean', 'jp', 'scn', 'en',
        'elite', 'trainer', 'etb', 'bundle', 'premium', 'ultra', 'super', 'tin', 'tins', 'deck', 'decks', 'starter', 'battle',
        'sleeved', 'double', 'mini', 'figure', 'poster', 'binder', 'build', 'with', 'for', 'x', 'in', 'stock', 'riftbound',
        'cyberpunk', 'gundam', 'tcg', 'expansion', 'scarlet', 'violet', 'sv', 'mega', 'me', 'bandai', 'official', 'product',
        'products', 'collector', 'play', 'version', 'ver', 'gift', 'special', 'exclusive', 'limited', 'league', 'legends'}

class Blocked(Exception):
    pass

SHOP_GAP = 1.2   # seconds between requests to the same Shopify store (heavier crawling gets the IP blocked by Shopify)

def curl_json(url, extra=None, shop=False):
    """GET url and parse JSON. shop=True: a 403/430 means Shopify's bot protection blocked us -> raise Blocked."""
    for attempt in range(3):
        if shop:
            time.sleep(SHOP_GAP)
        args = ['curl', '-s', '-m', '30', '-A', UA, '-w', '\n%{http_code}', url] + (extra or [])
        r = subprocess.run(args, capture_output=True, text=True)
        body, _, code = r.stdout.rpartition('\n')
        if shop and code in ('403', '430'):
            raise Blocked(code)
        if code == '429':
            time.sleep(10 * (attempt + 1))
            continue
        try:
            return json.loads(body)
        except Exception:
            if body.strip() == '':
                time.sleep(4 * (attempt + 1))
                continue
            return None
    return None

def tcg_details(pid):
    d = curl_json('https://mp-search-api.tcgplayer.com/v1/product/%d/details' % pid)
    return d if isinstance(d, dict) and d.get('productName') else None

def game_of(title, rest=''):
    # The title decides; vendor/tags/type only when the title names no game (stores mis-tag vendors).
    for t in (title.lower(), rest.lower()):
        if re.search(SKIP_GAME, t):
            return None
        for g, rx in GAME_RX:
            if re.search(rx, t):
                return g
    return None

def lang_of(text):
    t = text.lower()
    for l, rx in LANG_RX:
        if re.search(rx, t):
            return l
    return ''

def type_of(title):
    t = title.lower()
    for name, rx in TYPES:
        if re.search(rx, t):
            return None if name == 'skip' else name
    return None

def _bracket(m):
    # Keep a bracket that names a variant ([Miraidon ex], [Series 3], [Enhanced]); drop language, year,
    # set-code and condition tags ([JP], [2026], [ST-35], [Sealed]).
    inner = m.group(1).strip()
    if re.fullmatch(r'(?i)(jpn?|japanese|en|eng|english|s?t?cn|chinese|kr|kor|korean|sealed|new|pre-?order|preorder|limit \d+|(19|20)\d\d|q[1-4] (19|20)\d\d|[a-z]{1,4}-?\d{1,3}[a-z]{0,3})', inner):
        return ' '
    return ' ' + inner + ' '

def clean_title(title):
    t = re.sub(r'\[([^\]]*)\]', _bracket, title)
    t = re.sub(r'\(?non[- ]?enhanced\)?', ' ', t, flags=re.I)
    # Series prefixes before a set name ("Mega Evolution: Delta Reign", "Scarlet & Violet—151"): the set name is enough.
    t = re.sub(r'(?i)^\s*(pok[eé]mon( tcg)?\s*[:\-–—]?\s*)?(mega evolution|scarlet (&|and) violet|sword (&|and) shield)\s*[:\-–—]\s*(?=\S)', ' ', t)
    t = re.sub(NOISE, ' ', t, flags=re.I)
    return re.sub(r'\s+', ' ', t).strip(' -,')

def codes(s):
    return set(m.replace('-', '').replace(' ', '') for m in re.findall(r'\b(?:op|eb|prb|st|sv|me|gd|ua|fb|ex)[- ]?\d{1,2}[a-z]?\b', s.lower()))

def tokens(s):
    return [w for w in norm(s).split() if (len(w) > 1 or w.isdigit()) and w not in STOP]

def match_title(title, game, lang, ptype, cache):
    """Return (match dict or None, reason). Cached by game|lang|type|cleaned title."""
    clean = clean_title(title)
    key = '|'.join([game, lang, ptype, norm(title)])
    hit = cache.get(key)
    if hit:
        age = NOW - datetime.fromisoformat(hit['checked_at'].replace('Z', '+00:00'))
        if hit.get('none') and (hit.get('rejected') or age < timedelta(days=MATCH_RETRY_DAYS)):
            return None, hit.get('why', 'no match (cached)')
        if not hit.get('none'):
            if age >= timedelta(hours=PRICE_REFRESH_HOURS):
                d = tcg_details(hit['id'])
                if d and d.get('marketPrice'):
                    hit.update(market=round(float(d['marketPrice']), 2), checked_at=NOW_S)
                time.sleep(0.2)
            return hit, None
    lines = LINES.get(game, {}).get(lang) or ([] if lang else [None])
    if not lines:
        cache[key] = {'none': True, 'why': 'TCGplayer has no %s %s line' % (game, lang or 'English'), 'checked_at': NOW_S}
        return None, cache[key]['why']
    must, never = type_rule(ptype)
    # Set codes (OP-18, SV2a, ME2.5, DP-13...) are checked separately below, not as name words.
    toks = tokens(re.sub(r'(?i)\b(op|eb|prb|st|sv|me|gd|ua|fb|ex|dp|m|s)[- ]?\d{1,2}(\.\d)?[a-z]?\b', ' ', clean))
    want_codes = codes(title)
    if not toks and not want_codes:
        cache[key] = {'none': True, 'why': 'title has no set name', 'checked_at': NOW_S}
        return None, cache[key]['why']
    best = None
    for line in lines:
        res = []
        for q in dict.fromkeys([clean, ' '.join(toks + [ptype])]):
            got = search(q, line)
            if got is None:
                return None, 'search failed'
            res += got
            time.sleep(0.25)
        for r in {int(x['productId']): x for x in res}.values():
            name = norm(r.get('productName')); hay = name + ' ' + norm(r.get('setName')) + ' ' + norm(r.get('productLineName'))
            if line is None and game.split()[0].lower() not in hay:
                continue
            if r.get('marketPrice') is None or re.search(NEVER_ANY, name):
                continue
            if not re.search(must, name) or (never and re.search(never, name)):
                continue
            have_codes = codes(r.get('productName', '') + ' ' + r.get('setName', ''))
            if want_codes and have_codes and not (want_codes & have_codes):
                continue
            if re.search(r'non[- ]?enhanced?', title, re.I) and 'enhanced' in name:
                continue
            # Every distinctive word in the TCGplayer name must be in the listing title (so "Enhanced",
            # "Pokemon Center", "Half" or another variant is never matched to a plain listing).
            tw = set(norm(title).split())
            extra_words = [w for w in tokens(r.get('productName', '')) if w not in tw and not codes(w) and w not in ('retail', 'ex', 'v', 'gx')]
            if extra_words:
                continue
            words = set(hay.split())
            have = sum(1 for t in toks if (t in words if t.isdigit() else (t in words or t in hay)))
            cover = have / len(toks) if toks else 1.0
            if cover < 0.75:
                continue
            # Prefer full coverage, then the name with the fewest extra words.
            score = (cover, -len(tokens(r.get('productName', ''))))
            if not best or score > best[0]:
                best = (score, r)
        if best:
            break
    if not best:
        cache[key] = {'none': True, 'why': 'no TCGplayer match', 'checked_at': NOW_S}
        return None, 'no TCGplayer match'
    r = best[1]
    cache[key] = {'id': int(r['productId']), 'product': r['productName'], 'set': r.get('setName'),
                  'market': round(float(r['marketPrice']), 2), 'url': 'https://www.tcgplayer.com/product/%d' % int(r['productId']),
                  'checked_at': NOW_S}
    return cache[key], None

def store_products(src):
    """(base, meta, products, note) for one saved store. note says why it was skipped or cut short."""
    u = urllib.parse.urlparse(src['url'] if '://' in src['url'] else 'https://' + src['url'])
    base = '%s://%s' % (u.scheme or 'https', u.netloc)
    out, seen, meta = [], set(), {}
    try:
        meta = curl_json(base + '/meta.json', shop=True)
        if not isinstance(meta, dict) or 'currency' not in meta:
            return None, None, [], 'not a Shopify store (the scan reads it in the browser)'
        count = meta.get('published_products_count') or 0
        feeds = []
        m = re.match(r'/collections/([^/?#]+)', u.path)
        if m and m.group(1) not in ('all', 'pre-orders', 'preorders'):
            feeds = ['/collections/%s/products.json' % m.group(1)]
        elif count <= 3000:
            feeds = ['/products.json']
        else:
            # Big stores (mostly singles): only their sealed collections for the tracked games.
            cols = []
            for pg in range(1, 9):
                d = curl_json(base + '/collections.json?limit=250&page=%d' % pg, shop=True)
                if not d or not d.get('collections'):
                    break
                cols += d['collections']
            for c in cols:
                t = c['title'].lower()
                if not c.get('products_count') or c['products_count'] > 1500:
                    continue
                if re.search(r'single|graded|slab|accessor|sleeve|playmat|binder|black friday|sale', t) or re.search(SKIP_GAME, t):
                    continue
                if not any(re.search(rx, t) for _, rx in GAME_RX):
                    continue
                if re.search(r'sealed|booster|box|pre-?order|collection|elite trainer|etb|bundle|tin|deck|japanese|chinese|display|case|product', t):
                    feeds.append('/collections/%s/products.json' % c['handle'])
        for f in feeds:
            for pg in range(1, 13):
                d = curl_json(base + f + '?limit=250&page=%d' % pg, shop=True)
                if not d or not d.get('products'):
                    break
                for p in d['products']:
                    if p['id'] not in seen:
                        seen.add(p['id']); out.append(p)
                if len(d['products']) < 250 or len(out) >= MAX_PRODUCTS:
                    break
            if len(out) >= MAX_PRODUCTS:
                break
    except Blocked as e:
        return base, (meta if isinstance(meta, dict) else {}), out, 'blocked by Shopify bot protection (%s); %d products read before that' % (e, len(out))
    return base, meta, out, ''

def usd_price(base, handle, variant_id):
    try:
        d = curl_json('%s/products/%s.js?currency=USD' % (base, handle), shop=True)
    except Blocked:
        return None
    if not d:
        return None
    for v in d.get('variants', []):
        if v['id'] == variant_id:
            return v['price'] / 100
    return d['price'] / 100

def load_state(out_path, cache_path, news_path):
    try:
        cache = json.load(open(cache_path))
    except Exception:
        cache = {}
    try:
        prev = {r['url']: r for r in json.load(open(out_path))}
    except Exception:
        prev = {}
    msrp_by_product = {}
    if news_path:
        try:
            for r in json.load(open(news_path)):
                if r.get('msrp') and r.get('msrp_source') and r.get('market_product'):
                    msrp_by_product[r['market_product']] = (r['msrp'], r['msrp_source'])
        except Exception:
            pass
    return cache, prev, msrp_by_product

def process(src, base, cur, prods, cache, prev, msrp_by_product, report, source_tag, usd_ready=False):
    """Turn one store's products (Shopify JSON shape) into deal rows priced at or under market."""
    rows = []
    n_cand = n_match = 0; misses = []
    account = urllib.parse.urlparse(base).netloc.replace('www.', '')
    for p in prods:
        text = ' '.join([p.get('title', ''), p.get('vendor', ''), p.get('product_type', ''), ' '.join(p.get('tags') or [])])
        game = game_of(p.get('title', ''), text)
        if not game or re.search(r'(?<!non-)(?<!non )\bsingles?\b|graded|accessor|supplies|sleeve|playmat|event|ticket', (p.get('product_type') or '').lower()):
            continue
        pre = bool(re.search(r'pre-?order|presale', text, re.I))
        for v in p.get('variants', []):
            if not v.get('available'):
                continue
            vt = v.get('title') or ''
            title = p['title'] + ('' if vt in ('Default Title', '') or re.search(r'english|default', vt, re.I) else ' ' + vt)
            tl = title.lower()
            if re.search(NOT_SEALED, tl) and not re.search(r'sleeved booster|coin bundle|double pack|tin pack|mini case', tl):
                continue
            ptype = type_of(title)
            if not ptype:
                continue
            lang = lang_of(title + ' ' + ' '.join(p.get('tags') or []))
            n_cand += 1
            m, why = match_title(title, game, lang, ptype, cache)
            if not m:
                if len(misses) < 6: misses.append('%s (%s)' % (title[:60], why))
                continue
            n_match += 1
            price = float(v['price'])
            if cur != 'USD' and not usd_ready:
                price = usd_price(base, p['handle'], v['id'])
                if price is None:
                    continue
            market = m['market']
            if not (price > 0) or price > market + 0.009:
                continue
            if price < market * 0.4:   # almost always a pack or a wrong match: never auto-publish it
                report.append('   check by hand: %s $%.2f vs %s market $%.2f' % (title[:70], price, m['product'], market))
                continue
            url = base + '/products/' + p['handle'] + ('' if p.get('n', len(p['variants'])) == 1 else '?variant=%d' % v['id'])
            name = m['product']
            if lang and lang.lower()[:5] not in name.lower():
                name += ' (%s)' % lang
            old = prev.get(url) or {}
            ms = msrp_by_product.get(m['product'])
            rows.append({
                'headline': '%s %s, $%.2f%s' % (name, 'preorder' if pre else 'in stock', price, ' (US price)' if cur != 'USD' else ''),
                'game': game, 'type': 'Preorder' if pre else 'In stock', 'where': src['name'], 'account': account,
                'at': old.get('at') if old.get('price') == round(price, 2) and old.get('at') else NOW_S,
                'happens': '', 'url': url, 'channel': 'Official' if src.get('channel') == 'Official stores' else 'Other',
                'verified': '', 'verified_by': '', 'jp_only': '', 'jp_note': '',
                'price': round(price, 2), 'msrp': ms[0] if ms else '', 'msrp_source': ms[1] if ms else '',
                'deal_class': DEAL_CLASS.get(ptype, 'sealed'), 'market_price': market, 'market_url': m['url'],
                'market_source': 'TCGplayer', 'market_product': m['product'], 'store_title': title, 'checked_at': NOW_S,
                'source': source_tag})
    report.append('%-24s %5d products read · %4d sealed listings · %4d matched · %3d at or under market' % (src['name'], len(prods), n_cand, n_match, len(rows)))
    for x in misses:
        report.append('   no match: ' + x)
    return rows

def keep_fresh(rows, prev, accounts):
    """Keep earlier rows from these store accounts while they are less than 3 days old."""
    have = {r['url'] for r in rows}
    for url, r in prev.items():
        if r.get('account') in accounts and url not in have:
            seen_at = max(r.get('at', ''), r.get('checked_at', ''))
            if seen_at and NOW - datetime.fromisoformat(seen_at.replace('Z', '+00:00')) < timedelta(days=3):
                rows.append(r)

def save(rows, out_path, cache, cache_path, report):
    rows.sort(key=lambda r: (r['market_price'] - r['price']) / r['market_price'], reverse=True)
    json.dump(rows, open(out_path, 'w'), ensure_ascii=False)
    json.dump(cache, open(cache_path, 'w'), ensure_ascii=False)
    print('\n'.join(report))
    print('store deals at or under market: %d' % len(rows))

def sweep(sources_path, out_path, cache_path, news_path=None):
    sources = json.load(open(sources_path))
    cache, prev, msrp_by_product = load_state(out_path, cache_path, news_path)
    rows, report, done, blocked = [], [], set(), []
    for src in sources:
        if src.get('channel') not in SWEEP_CHANNELS:
            continue
        base, meta, prods, note = store_products(src)
        if base is None:
            report.append('%-24s skipped: %s' % (src['name'], note))
            continue
        account = urllib.parse.urlparse(base).netloc.replace('www.', '')
        if note:   # blocked part-way: its earlier rows stay until the browser fallback (or the next run) re-checks them
            report.append('%-24s %s' % (src['name'], note))
            blocked.append('%s (%s)' % (src['name'], base))
            continue
        done.add(account)
        rows += process(src, base, (meta or {}).get('currency', 'USD'), prods, cache, prev, msrp_by_product, report, 'store sweep')
    # Rows from stores not fully read this time (blocked, browser-only stores like GameNerdz): keep while fresh.
    keep_fresh(rows, prev, {r.get('account') for r in prev.values()} - done)
    if blocked:
        report.append('BLOCKED (use the browser fallback for these): ' + '; '.join(blocked))
    save(rows, out_path, cache, cache_path, report)

def listings(listings_path, store_name, base, currency, out_path, cache_path, news_path=None):
    """Browser fallback: products a store's page returned (Shopify JSON shape, prices already in US dollars)."""
    cache, prev, msrp_by_product = load_state(out_path, cache_path, news_path)
    prods = json.load(open(listings_path))
    report = []
    src = {'name': store_name, 'channel': 'Japan imports' if 'japan' in store_name.lower() else 'Local game stores'}
    base = base.rstrip('/')
    account = urllib.parse.urlparse(base).netloc.replace('www.', '')
    new = process(src, base, currency, prods, cache, prev, msrp_by_product, report, 'browser sweep', usd_ready=True)
    rows = [r for r in prev.values() if r.get('account') != account] + new
    save(rows, out_path, cache, cache_path, report)

def lookup(title, game, cache_path=None):
    try:
        cache = json.load(open(cache_path)) if cache_path else {}
    except Exception:
        cache = {}
    ptype = type_of(title)
    if not ptype:
        print(json.dumps({'error': 'no sealed product type in the title'})); return
    m, why = match_title(title, game, lang_of(title), ptype, cache)
    if cache_path:
        json.dump(cache, open(cache_path, 'w'), ensure_ascii=False)
    print(json.dumps(m if m else {'error': why}, ensure_ascii=False))

def reject(title, game, cache_path):
    """Mark a wrong TCGplayer match for this listing title so the sweep stops using it."""
    cache = json.load(open(cache_path))
    ptype = type_of(title) or ''
    key = '|'.join([game, lang_of(title), ptype, norm(title)])
    cache[key] = {'none': True, 'why': 'wrong match, rejected by hand', 'checked_at': NOW_S, 'rejected': True}
    json.dump(cache, open(cache_path, 'w'), ensure_ascii=False)
    print('rejected', key)

if __name__ == '__main__':
    if len(sys.argv) >= 5 and sys.argv[1] == 'sweep':
        sweep(*sys.argv[2:6])
    elif len(sys.argv) >= 4 and sys.argv[1] == 'lookup':
        lookup(*sys.argv[2:5])
    elif len(sys.argv) >= 8 and sys.argv[1] == 'listings':
        listings(*sys.argv[2:9])
    elif len(sys.argv) == 5 and sys.argv[1] == 'reject':
        reject(*sys.argv[2:5])
    else:
        print(__doc__); sys.exit(1)
