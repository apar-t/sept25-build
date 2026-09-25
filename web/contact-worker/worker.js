// Contact form backend for the landing page. Commits one JSON file per submission
// to a private GitHub repo. Secret: GITHUB_TOKEN (fine-grained, Contents read/write on GITHUB_REPO only).
const MAX = {name: 200, email: 200, message: 5000};
const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function cors(req, env) {
  const origin = req.headers.get('Origin') || '';
  const allowed = env.ALLOWED_ORIGINS.split(',').map(s => s.trim());
  return allowed.includes(origin) ? {
    'Access-Control-Allow-Origin': origin,
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
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

export default {
  async fetch(req, env) {
    const h = cors(req, env);
    if (!h) return json(403, {ok: false, error: 'origin not allowed'});
    if (req.method === 'OPTIONS') return new Response(null, {status: 204, headers: h});
    if (req.method !== 'POST') return json(405, {ok: false, error: 'POST only'}, h);
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
