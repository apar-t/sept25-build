// Backend for nightswatch.app. Two jobs:
// 1. POST /  contact form: commits one JSON file per submission to GITHUB_REPO.
// 2. Live company checks for the demo page, stored in D1 (schema.sql):
//      POST /check {url}          start one (or reuse a result from the last 24 h), returns {id}
//      GET  /check/<id>           status, live steps, and the result when done
//      POST /check/<id>/report    progress/result from the GitHub Action (LIVE_REPO), Bearer REPORT_SECRET
// Secrets: GITHUB_TOKEN (fine-grained: Contents read/write on GITHUB_REPO, Actions read/write on LIVE_REPO),
//          REPORT_SECRET (shared with LIVE_REPO's Actions secrets).
const MAX = {name: 200, email: 200, message: 5000};
const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function cors(req, env) {
  const origin = req.headers.get('Origin') || '';
  const allowed = env.ALLOWED_ORIGINS.split(',').map(s => s.trim());
  return allowed.includes(origin) ? {
    'Access-Control-Allow-Origin': origin,
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Vary': 'Origin',
  } : null;
}

const json = (status, body, headers) =>
  new Response(JSON.stringify(body), {status, headers: {...headers, 'Content-Type': 'application/json'}});

function b64(s) {
  let bin = '';
  for (const b of new TextEncoder().encode(s)) bin += String.fromCharCode(b);
  return btoa(bin);
}

const REUSE_MS = 24 * 3600e3, STALE_MS = 8 * 60e3, PER_HOUR = 20;
const gh = (env, path, init = {}) => fetch(`${env.GITHUB_API}${path}`, {...init, headers: {
  'Authorization': `Bearer ${env.GITHUB_TOKEN}`, 'Accept': 'application/vnd.github+json',
  'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'nights-watch-live', ...(init.headers || {})}});

// "Stripe.com/legal/" -> {url: "https://stripe.com/legal", key: "stripe.com/legal"}; null if it isn't a plain public site
function target(raw) {
  let u;
  try { u = new URL(/^https?:\/\//i.test(raw) ? raw : 'https://' + raw); } catch { return null; }
  const host = u.hostname.toLowerCase().replace(/^www\./, '');
  if (!/^([a-z0-9-]+\.)+[a-z]{2,}$/.test(host) || u.port || u.username) return null;
  const path = u.pathname.replace(/\/+$/, '');
  if (!/^[\w./-]{0,120}$/.test(path)) return null;
  return {url: `https://${host}${path}`, key: host + path};
}

async function startCheck(req, env, h) {
  let d;
  try { d = await req.json(); } catch { return json(400, {error: 'bad json'}, h); }
  const t = target(String(d.url || '').trim());
  if (!t) return json(400, {error: 'Enter a company website, like stripe.com'}, h);
  const now = Date.now(), db = env.DB;
  const recent = await db.prepare(`SELECT id, status FROM checks WHERE host = ? AND created_at > ?
      AND (status = 'done' OR (status = 'running' AND updated_at > ?)) ORDER BY created_at DESC LIMIT 1`)
    .bind(t.key, now - REUSE_MS, now - STALE_MS).first();
  if (recent) return json(200, {id: recent.id, reused: true}, h);
  const {n} = await db.prepare('SELECT COUNT(*) AS n FROM checks WHERE created_at > ?').bind(now - 3600e3).first();
  if (n >= PER_HOUR) return json(429, {error: 'Lots of people are checking companies right now. Try again in a few minutes.'}, h);
  const id = crypto.randomUUID().replace(/-/g, '');
  await db.prepare(`INSERT INTO checks (id, host, url, status, steps, created_at, updated_at) VALUES (?, ?, ?, 'running', ?, ?, ?)`)
    .bind(id, t.key, t.url, JSON.stringify([{text: 'Starting a live check on GitHub', state: 'active', detail: ''}]), now, now).run();
  const r = await gh(env, `/repos/${env.LIVE_REPO}/actions/workflows/check.yml/dispatches`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ref: 'main', inputs: {url: t.url, id}})});
  if (!r.ok) {
    console.log('dispatch', r.status, (await r.text()).slice(0, 300));
    await db.prepare(`UPDATE checks SET status = 'failed', error = ?, updated_at = ? WHERE id = ?`)
      .bind('Could not start the check.', Date.now(), id).run();
    return json(502, {error: 'Could not start the check. Try again in a minute.'}, h);
  }
  return json(200, {id}, h);
}

async function getCheck(id, env, h) {
  const row = await env.DB.prepare('SELECT * FROM checks WHERE id = ?').bind(id).first();
  if (!row) return json(404, {error: 'no such check'}, h);
  if (row.status === 'running' && Date.now() - row.updated_at > STALE_MS) {
    row.status = 'failed'; row.error = 'The check took too long.';
    await env.DB.prepare(`UPDATE checks SET status = 'failed', error = ?, updated_at = ? WHERE id = ?`).bind(row.error, Date.now(), id).run();
  }
  return json(200, {id, url: row.url, status: row.status, error: row.error, steps: JSON.parse(row.steps || '[]'),
    result: row.result ? JSON.parse(row.result) : null}, {...h, 'Cache-Control': 'no-store'});
}

async function report(req, id, env) {
  const auth = req.headers.get('Authorization') || '';
  if (!env.REPORT_SECRET || auth !== `Bearer ${env.REPORT_SECRET}`) return json(401, {error: 'unauthorized'});
  if (+(req.headers.get('Content-Length') || 0) > 2e6) return json(413, {error: 'too large'});
  let d;
  try { d = await req.json(); } catch { return json(400, {error: 'bad json'}); }
  const status = ['running', 'done', 'failed'].includes(d.status) ? d.status : 'running';
  const r = await env.DB.prepare(`UPDATE checks SET status = ?, steps = COALESCE(?, steps), result = COALESCE(?, result),
      error = COALESCE(?, error), updated_at = ? WHERE id = ? AND status = 'running'`)
    .bind(status, d.steps ? JSON.stringify(d.steps) : null, d.result ? JSON.stringify(d.result) : null,
          d.error ? String(d.error).slice(0, 500) : null, Date.now(), id).run();
  return json(200, {ok: true, updated: r.meta.changes});
}

export default {
  async fetch(req, env) {
    const route = new URL(req.url).pathname;
    const rep = route.match(/^\/check\/([a-f0-9]{32})\/report$/);
    if (rep && req.method === 'POST') return report(req, rep[1], env);  // from GitHub Actions: no browser Origin
    const h = cors(req, env);
    if (!h) return json(403, {ok: false, error: 'origin not allowed'});
    if (req.method === 'OPTIONS') return new Response(null, {status: 204, headers: h});
    if (route === '/check' && req.method === 'POST') return startCheck(req, env, h);
    const one = route.match(/^\/check\/([a-f0-9]{32})$/);
    if (one && req.method === 'GET') return getCheck(one[1], env, h);
    if (req.method !== 'POST' || route !== '/') return json(405, {ok: false, error: 'not found'}, h);
    if (+(req.headers.get('Content-Length') || 0) > 20000) return json(413, {ok: false, error: 'too large'}, h);

    let d;
    try { d = await req.json(); } catch { return json(400, {ok: false, error: 'bad json'}, h); }
    // Honeypot: people never see the "website" field, bots fill it. Pretend it worked.
    if (d.website) return json(200, {ok: true}, h);
    const name = String(d.name || '').trim(), email = String(d.email || '').trim(), message = String(d.message || '').trim();
    if (!name || name.length > MAX.name) return json(400, {ok: false, error: 'name'}, h);
    if (!EMAIL.test(email) || email.length > MAX.email) return json(400, {ok: false, error: 'email'}, h);
    if (message.length > MAX.message) return json(400, {ok: false, error: 'message'}, h);

    const now = new Date().toISOString();
    const path = `contacts/${now.slice(0, 10)}/${now.replace(/[:.]/g, '-')}-${crypto.randomUUID().slice(0, 8)}.json`;
    const body = JSON.stringify({name, email, message, received_at: now, page: req.headers.get('Origin')}, null, 2) + '\n';
    const r = await fetch(`${env.GITHUB_API}/repos/${env.GITHUB_REPO}/contents/${path}`, {
      method: 'PUT',
      headers: {
        'Authorization': `Bearer ${env.GITHUB_TOKEN}`,
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'nights-watch-contact',
      },
      body: JSON.stringify({message: `Contact: ${name.slice(0, 60)}`, content: b64(body)}),
    });
    if (!r.ok) {
      console.log('github', r.status, (await r.text()).slice(0, 300));
      return json(502, {ok: false, error: 'store failed'}, h);
    }
    return json(200, {ok: true}, h);
  },
};
