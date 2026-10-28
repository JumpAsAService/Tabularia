// The reports' charts, drawn by ECharts as SVG on the server: no DOM, no canvas, no browser.
//
//   node render.mjs --serve 8090 [--host 127.0.0.1]   POST /render {width, height, option} → SVG, GET /healthz
//   node render.mjs <specs.json> <outdir>             every {name, width, height, option} to <outdir>/<name>.svg
//
// The engine's workers call it while rendering a report (backend/app/reports/charts.py builds the
// options; REPORTS__CHART_URL says where this listens): on Kubernetes a sidecar of the worker pod on
// 127.0.0.1, on Compose an internal service. It runs from the frontend image, which has Node and
// ECharts already.
//
// Options arrive as JSON, so they carry no functions, and nothing in them is evaluated. The few
// formatters a chart needs are NAMED, {"$fmt": "compact", "locale": "it"}, and only under a
// `formatter` key: this file turns the names of its closed list into functions — the only code that
// runs is ours, the report only names it. No images: a `symbol` or `icon` that points somewhere, an
// `image` or a `graphic`, is refused. Text is measured with the advance widths of the PDF's font
// (font-widths.json, made by font_widths.py), so a label is cut where the PDF would cut it.
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import http from 'node:http';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);

// the single-file bundle loads in ~90 ms (the package's ES modules in ~700): the production image
// carries it alone in vendor/, development reads it from the frontend's node_modules
function loadEcharts() {
  try {
    return require('./vendor/echarts.min.js');
  } catch (e) {
    if (e.code !== 'MODULE_NOT_FOUND') throw e;
    return require('echarts/dist/echarts.min');
  }
}
const echarts = loadEcharts();

const WIDTHS = JSON.parse(readFileSync(new URL('./font-widths.json', import.meta.url), 'utf8'));
// a character the font lacks: a wide one (CJK…) takes an em, as in a browser — zrender also takes
// the height of a line from the width of «国», so at 0.6 em the lines of a label overlapped
const fallback = (ch) => (ch.codePointAt(0) >= 0x2e80 ? 1 : 0.6);
echarts.setPlatformAPI({
  measureText(text, font) {
    const size = Number((/(\d+(?:\.\d+)?)px/.exec(font || '') || [0, 12])[1]);
    const weight = /\b(bold|bolder|[7-9]00)\b/.test(font || '') ? '700' : /\b(500|600)\b/.test(font || '') ? '600' : '400';
    const table = WIDTHS[weight];
    let w = 0;
    for (const ch of String(text ?? '')) w += (table[ch] ?? fallback(ch)) * size;
    return { width: w };
  },
});

const LOCALES = new Set(['en', 'it', 'de', 'es', 'fr']);
const locale = (a) => (LOCALES.has(a.locale) ? a.locale : 'en');
const decimals = (a) => (Number.isInteger(a.decimals) ? Math.min(Math.max(a.decimals, 0), 6) : 0);
// a point's value: a number, {value}, or the last of [x, y, value] (heatmap, scatter)
const valueOf = (p) => {
  const v = p && typeof p === 'object' && !Array.isArray(p) && 'value' in p ? p.value : p;
  return Array.isArray(v) ? v[v.length - 1] : v;
};
const textOf = (p) => (p && p.data && typeof p.data.t === 'string' ? p.data.t : null);
// a name cut to `max` characters with «…» (no `max`: whole), so a wrapped label stays a few lines
const cut = (a) => {
  const max = Number.isInteger(a.max) && a.max > 1 ? a.max : Infinity;
  return (text) => {
    const chars = [...String(text ?? '')];
    return chars.length > max ? `${chars.slice(0, max - 1).join('').trimEnd()}…` : chars.join('');
  };
};

const FORMATTERS = {
  label: () => (p) => textOf(p) ?? String(valueOf(p) ?? ''),
  short: (a) => {
    const name = cut(a);
    return (v) => name(valueOf(v));
  },
  nameLabel: (a) => {
    const sep = a.sep === ' ' ? ' ' : '\n';
    const name = cut(a);
    return (p) => (textOf(p) ? `${name(p.name)}${sep}${textOf(p)}` : name(p.name));
  },
  // the share on a line of its own: a long name wraps or is cut, the share never
  namePercent: (a) => {
    const f = new Intl.NumberFormat(locale(a), { style: 'percent', maximumFractionDigits: 1 });
    const name = cut(a);
    return (p) => `${name(p.name)}\n${f.format((Number(p.percent) || 0) / 100)}`;
  },
  // three significant digits: the ticks of a small scale (0.05, 0.15) are not all «0.1»
  compact: (a) => {
    const f = new Intl.NumberFormat(locale(a), { notation: 'compact', maximumSignificantDigits: 3 });
    return (v) => f.format(Number(valueOf(v)));
  },
  percent: (a) => {
    const f = new Intl.NumberFormat(locale(a), {
      style: 'percent', minimumFractionDigits: decimals(a), maximumFractionDigits: decimals(a),
    });
    return (v) => f.format(Number(valueOf(v)));
  },
  const: (a) => {
    const text = String(a.text ?? '');
    return () => text;
  },
};
const REFUSED_KEYS = new Set(['image', 'graphic']);
const SYMBOL_KEYS = new Set(['symbol', 'icon']);

function hydrate(node, key) {
  if (Array.isArray(node)) return node.map((x) => hydrate(x, null));
  if (node && typeof node === 'object') {
    if (key === 'formatter' && typeof node.$fmt === 'string') {
      if (!Object.hasOwn(FORMATTERS, node.$fmt)) throw new Error(`unknown formatter «${node.$fmt}»`);
      return FORMATTERS[node.$fmt](node);
    }
    const out = {};
    for (const [k, v] of Object.entries(node)) {
      if (k === '__proto__' || k === 'constructor' || k === 'prototype') continue;
      if (REFUSED_KEYS.has(k)) throw new Error(`«${k}» is not allowed in a chart`);
      out[k] = hydrate(v, k);
    }
    return out;
  }
  if (typeof node === 'string' && SYMBOL_KEYS.has(key) && /^(image|path):\/\//i.test(node)) {
    throw new Error(`«${key}»: no images or paths in a chart`);
  }
  return node;
}

const size = (v, what) => {
  const n = Number(v);
  if (!Number.isFinite(n) || n < 10 || n > 4000) throw new Error(`${what} must be between 10 and 4000 px`);
  return Math.round(n);
};

export function render(spec) {
  if (!spec || typeof spec !== 'object' || !spec.option || typeof spec.option !== 'object') {
    throw new Error('expected {width, height, option}');
  }
  const chart = echarts.init(null, null, {
    renderer: 'svg', ssr: true, width: size(spec.width, 'width'), height: size(spec.height, 'height'),
  });
  try {
    chart.setOption(hydrate(spec.option, null));
    return chart.renderToSVGString();
  } finally {
    chart.dispose();
  }
}

const MAX_BODY = 16 * 1024 * 1024;

function serve(port, host) {
  const server = http.createServer((req, res) => {
    if (req.method === 'GET' && req.url === '/healthz') {
      res.writeHead(200, { 'content-type': 'text/plain' }).end('ok');
      return;
    }
    if (req.method !== 'POST' || req.url !== '/render') {
      res.writeHead(404, { 'content-type': 'text/plain' }).end('not found');
      return;
    }
    const chunks = [];
    let received = 0;
    req.on('data', (c) => {
      received += c.length;
      if (received > MAX_BODY) {
        res.writeHead(413, { 'content-type': 'text/plain' }).end('the chart is too large');
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on('end', () => {
      if (res.writableEnded) return;
      try {
        const svg = render(JSON.parse(Buffer.concat(chunks).toString('utf8')));
        res.writeHead(200, { 'content-type': 'image/svg+xml; charset=utf-8' }).end(svg);
      } catch (e) {
        res.writeHead(400, { 'content-type': 'text/plain; charset=utf-8' }).end(String(e?.message ?? e).slice(0, 500));
      }
    });
  });
  server.requestTimeout = 30_000;
  server.listen(port, host, () => console.error(`chart renderer on ${host}:${port}`));
  const stop = () => server.close(() => process.exit(0));
  process.on('SIGTERM', stop);
  process.on('SIGINT', stop);
}

const args = process.argv.slice(2);
if (args[0] === '--serve') {
  const host = args.includes('--host') ? args[args.indexOf('--host') + 1] : '127.0.0.1';
  serve(Number(args[1] || 8090), host);
} else if (args.length === 2) {
  const specs = JSON.parse(readFileSync(args[0], 'utf8'));
  mkdirSync(args[1], { recursive: true });
  for (const s of specs) writeFileSync(`${args[1]}/${s.name}.svg`, render(s));
} else if (args.length) {
  console.error('usage: node render.mjs --serve PORT [--host HOST] | node render.mjs SPECS.json OUTDIR');
  process.exit(2);
}
