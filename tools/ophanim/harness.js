// OPHANIM v3.5 jsdom harness: boots the whole file, runs the built-in SelfTest,
// then exercises the new Overwatch module against a fake map + fake AIS socket.
const fs = require('fs');
const { JSDOM, VirtualConsole } = require('jsdom');

const file = process.argv[2] || 'index.html';
const html = fs.readFileSync(file, 'utf8');
const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', e => { const m = String(e && (e.message || e)); if (!/Could not load script|Not implemented|ECONNREFUSED|fetch/i.test(m)) errors.push('jsdomError: ' + m); });
vc.on('error', (...a) => errors.push('console.error: ' + a.join(' ')));

const sockets = [];
const opened = [];
const dom = new JSDOM(html, {
  runScripts: 'dangerously', pretendToBeVisual: true, url: 'https://milos67-collab.github.io/ophanim-/', virtualConsole: vc,
  beforeParse(w) {
    w.fetch = () => Promise.reject(new Error('offline harness'));
    w.prompt = () => null;
    w.open = (u, n) => { opened.push(u); return {}; };
    w.HTMLCanvasElement.prototype.getContext = () => null;
    class FakeWS { constructor(u) { this.url = u; this.readyState = 0; this.sent = []; sockets.push(this); }
      send(d) { this.sent.push(d); } close() { this.readyState = 3; this.onclose && this.onclose(); } }
    w.WebSocket = FakeWS;
    w.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} });
  },
});
const w = dom.window;
const results = [];
const T = (name, fn) => { try { const r = fn(); results.push([name, !!r, r === true ? '' : r]); } catch (e) { results.push([name, false, e.message]); } };

setTimeout(() => {
  // ---------- built-in SelfTest ----------
  let st = [];
  try { st = w.SelfTest ? w.SelfTest.run() : []; } catch (e) { errors.push('SelfTest threw: ' + e.message); }
  const stFail = st.filter(x => !x.ok);
  T('SelfTest present and ran', () => st.length > 0 || 'no SelfTest results');
  T('SelfTest all green (' + st.length + ')', () => stFail.length === 0 || stFail.map(x => x.name + (x.info ? ' — ' + x.info : '')).join(' | '));

  // ---------- old God's Eye fully gone ----------
  T('window.GodsEye removed', () => w.GodsEye === undefined);
  T('no street-view / cam overlays in file', () => !/sv-overlay|cam-overlay|svembed/.test(html));

  // ---------- Overwatch mount on a fake map ----------
  const handlers = {};
  const fakeMap = { getCenter: () => ({ lat: 29.7604, lng: -95.3698 }), getZoom: () => 9, on: (e, f) => (handlers[e] = f),
    getSource: () => null, addSource() {}, addLayer() {}, getLayer: () => null, setLayoutProperty() {}, getBounds: () => ({ getSouth: () => 0, getWest: () => 0, getNorth: () => 1, getEast: () => 1 }) };
  let wrap = w.document.getElementById('sat-wrap');
  T('#sat-wrap exists', () => !!wrap);
  let ingests = [];
  if (w.Chokepoint) { const orig = w.Chokepoint.ingest; w.Chokepoint.ingest = (...a) => { ingests.push(a); try { return orig.apply(w.Chokepoint, a); } catch (_) {} }; }
  T('Overwatch.mount runs', () => { w.Overwatch.mount(fakeMap); return true; });
  const $ = s => w.document.querySelector(s);
  T('panel rendered with 6 controls', () => { const n = w.document.querySelectorAll('#ge-panel button').length; return n === 6 || 'buttons=' + n; });
  T('WarWatch/Chokepoint/Datacenter toggles kept', () => ['ww', 'cp', 'dc'].every(k => $('#ge-panel [data-ge="' + k + '"]')));
  T('theater heat strip kept', () => !!$('#ww-heat'));
  T('GEV not linked by default', () => /NOT LINKED/.test($('#ges-gev').textContent) || $('#ges-gev').textContent);

  // ---------- GEV link maths ----------
  T('normBase adds scheme + slash', () => w.Overwatch.normBase('127.0.0.1:42003') === 'http://127.0.0.1:42003/' || w.Overwatch.normBase('127.0.0.1:42003'));
  T('normBase strips old hash', () => w.Overwatch.normBase('http://127.0.0.1:42003/#lat=1&lon=2') === 'http://127.0.0.1:42003/');
  T('normBase rejects junk', () => w.Overwatch.normBase('') === '');
  T('altitude from zoom sane', () => { const a3 = w.Overwatch.altFromZoom(3), a15 = w.Overwatch.altFromZoom(15); return (a3 > 1e6 && a15 < 2000 && a15 >= 300) || [a3, a15].join(','); });
  T('link carries lat/lon/alt', () => { const u = w.Overwatch.gevLink('http://127.0.0.1:42003/', { lat: 29.7604, lon: -95.3698, zoom: 9 });
    return /^http:\/\/127\.0\.0\.1:42003\/#lat=29\.7604&lon=-95\.3698&alt=\d+&heading=0&pitch=-45$/.test(u) || u; });

  // ---------- GEV open flow ----------
  w.Store.set('gev.url', 'http://127.0.0.1:42003/');
  $('#gev-open').click();
  T('GEV opens at current globe spot', () => (opened.length === 1 && opened[0].startsWith('http://127.0.0.1:42003/#lat=29.7604&lon=-95.3698')) || JSON.stringify(opened));

  // ---------- AIS feed ----------
  const before = sockets.length;
  w.Store.set('ais.key', 'TESTKEY');
  $('#ge-panel [data-ge="ais"]').click();
  const s = sockets[sockets.length - 1];
  T('AIS toggle opens aisstream socket', () => (sockets.length === before + 1 && s.url === 'wss://stream.aisstream.io/v0/stream') || 'sockets=' + sockets.length);
  s.readyState = 1; s.onopen && s.onopen();
  T('subscribes with key, whole globe', () => { const m = JSON.parse(s.sent[0] || '{}'); return (m.APIKey === 'TESTKEY' && m.BoundingBoxes[0][0][0] === -85) || s.sent[0]; });
  ingests = [];
  s.onmessage({ data: JSON.stringify({ MessageType: 'PositionReport', MetaData: { MMSI: 123, latitude: 30.5, longitude: 32.4 }, Message: { PositionReport: { Sog: 0.4 } } }) });
  s.onmessage({ data: JSON.stringify({ MessageType: 'ShipStaticData', MetaData: { MMSI: 9 } }) });
  s.onmessage({ data: 'not json' });
  T('position report → Chokepoint.ingest', () => (ingests.length === 1 && ingests[0][0] === 123 && ingests[0][3] === 0.4) || JSON.stringify(ingests));
  T('AIS state persisted', () => w.Store.get('ais.on') === true);
  $('#ge-panel [data-ge="ais"]').click();
  T('AIS toggle off closes socket', () => s.readyState === 3 && w.Store.get('ais.on') === false);

  // ---------- report ----------
  T('no runtime errors', () => errors.length === 0 || errors.slice(0, 5).join(' || '));
  let pass = 0;
  for (const [n, ok, info] of results) { console.log((ok ? '  ✔ ' : '  ✘ ') + n + (ok ? '' : '  → ' + info)); if (ok) pass++; }
  console.log(`\n${pass}/${results.length} ${pass === results.length ? 'ALL GREEN' : 'FAILURES'}`);
  process.exit(pass === results.length ? 0 : 1);
}, 2500);
