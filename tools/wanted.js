// node wanted.js <url> [local-data-dir] [--deals]
// Loads the Chasing Grails website and prints window.CG.wanted (products whose market price the scan should look up),
// or with --deals the deals the website would show. Files in local-data-dir replace the site's data/<name>.json.
const { chromium } = require('playwright');
const fs = require('fs'), path = require('path');
(async () => {
  const args = process.argv.slice(2), deals = args.includes('--deals');
  const [url, dir] = args.filter((a) => a !== '--deals');
  const b = await chromium.launch(); const p = await b.newPage();
  const errs = []; p.on('pageerror', (e) => errs.push(String(e)));
  if (dir) await p.route(/\/data\/[a-z_]+\.json/, (route) => {
    const f = path.join(dir, route.request().url().split('/data/')[1].split('?')[0]);
    if (fs.existsSync(f)) return route.fulfill({ status: 200, contentType: 'application/json', body: fs.readFileSync(f) });
    return route.continue();
  });
  await p.goto(url.replace(/#.*$/, '') + '#/analytics');
  await p.waitForFunction(() => window.CG && window.CG.ready, null, { timeout: 60000 });
  const out = await p.evaluate((d) => d ? window.CG.deals : window.CG.wanted, deals);
  if (errs.length) console.error('page errors:', errs);
  console.log(JSON.stringify(out, null, 1));
  await b.close();
})().catch((e) => { console.error(e); process.exit(1); });
