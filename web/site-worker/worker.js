// Serves the GitHub Pages copy of the landing page at nightswatch.app, so Pages stays the only thing we publish to.
const ORIGIN = 'https://vroy2008.github.io/nights-watch';

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (url.hostname !== env.CANONICAL_HOST) {
      url.hostname = env.CANONICAL_HOST;
      return Response.redirect(url.toString(), 301);
    }
    if (req.method !== 'GET' && req.method !== 'HEAD') return new Response('GET only', {status: 405});
    const r = await fetch(ORIGIN + url.pathname + url.search, {method: req.method, redirect: 'manual'});
    const res = new Response(r.body, r);
    res.headers.set('Strict-Transport-Security', 'max-age=31536000');
    return res;
  },
};
