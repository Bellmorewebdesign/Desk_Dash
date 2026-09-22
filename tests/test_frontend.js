// Dependency-free render smoke test for the tablet interface.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const nodes = new Map();
const intervals = [];
function node(id) {
  if (!nodes.has(id)) nodes.set(id, {innerHTML: '', textContent: '', value: '', classList: {classes: new Set(), toggle(name, force) { if (force) this.classes.add(name); else this.classes.delete(name); }, add(name) {this.classes.add(name);}, remove(name) {this.classes.delete(name);}}, getAttribute(name) { return this[name]; }});
  return nodes.get(id);
}
const listeners = {};
const nav = ['overview', 'systems', 'storage', 'websites', 'services', 'network', 'activity', 'controls'].map(page => ({classList: {toggle() {}}, getAttribute() { return page; }}));
const temp = (celsius, level) => ({celsius, level});
const host = {ts: 1000, hostname: 'Capo-Bot', cpu: {model: 'AMD Ryzen 5 7600', percent: 12, mhz: 5000, temperature: temp(46, 'GOOD'), load: [0.2, 0.3, 0.4]}, gpu: {model: 'RTX 3080', temperature: temp(35, 'GOOD'), utilization: 5, vram_used_mib: 1000, vram_total_mib: 10240, power_w: 60, fan_percent: 35}, ram: {used_bytes: 10000000000, total_bytes: 32000000000}, uptime_seconds: 4000, process_count: 100, filesystems: [], network_interfaces: {}, auxiliary_sensors: [], drives: [{device: 'nvme0n1', model: 'Lexar NM790', capacity_bytes: 1000000000000, mounts: [], smart: {health: 'GOOD', temperature: temp(35, 'GOOD'), unsafe_shutdowns: 7, percentage_used: 0, data_read_bytes: 4630000000, data_written_bytes: 186530000000}}]};
const remote = {id: 'second-pc', name: 'SECOND PC', status: 'OFFLINE', system: null, last_success: 1000, last_polled: 1100};
const snapshot = {system: host, machines: [{id: 'local', name: 'CAPO-BOT', status: 'ONLINE', system: host, last_success: 1000}, remote], remotes: [remote], sites: [{id: 'bwd', name: 'Bellmore Web Design', url: 'https://bellmorewebdesign.com', status: 'ONLINE', ms: 100, http_status: 200, checked_at: 1000, last_success: 1000, uptime_percent: 99.9, consecutive_failures: 0, checks: []}, {id: 'coursen', name: 'Coursen', url: '', status: 'UNCONFIGURED', checks: []}], services: [], events: [], controls: {services: [], wake_targets: [], reboot: false}, storage_test_configured: false, server_time: 1000};
const responses = {'/api/auth': {authenticated: false, password_required: false, controls_enabled: false}, '/api/snapshot': snapshot, '/api/history': {samples: []}, '/api/history?machine=second-pc': {samples: []}};
const context = {document: {getElementById: node, addEventListener(name, callback) {listeners[name] = callback;}, querySelectorAll() {return nav;}}, location: {hash: ''}, fetch: async path => ({ok: true, json: async () => responses[path]}), setInterval(callback, ms) {intervals.push({callback, ms});}, setTimeout() {}, clearTimeout() {}, Date, console};
const source = fs.readFileSync('static/app.js', 'utf8');
vm.runInNewContext(source, context);

// Exercise the real background updater with a small DOM tree: values change,
// while the card and chart nodes keep their identity and animation state.
function element(tag, attributes = {}, children = []) {
  const attrs = Object.entries(attributes).map(([name, value]) => ({name, value}));
  return {nodeType: 1, tagName: tag.toUpperCase(), attributes: attrs, childNodes: children,
    hasAttribute(name) {return this.attributes.some(attr => attr.name === name);},
    getAttribute(name) {return this.attributes.find(attr => attr.name === name)?.value ?? null;},
    setAttribute(name, value) {this.removeAttribute(name); this.attributes.push({name, value});},
    removeAttribute(name) {this.attributes = this.attributes.filter(attr => attr.name !== name);},
    cloneNode() {return element(tag, Object.fromEntries(this.attributes.map(attr => [attr.name, attr.value])), this.childNodes.map(child => child.cloneNode(true)));},
    appendChild(child) {this.childNodes.push(child);},
    replaceChild(next, current) {this.childNodes[this.childNodes.indexOf(current)] = next;},
    removeChild(child) {this.childNodes.splice(this.childNodes.indexOf(child), 1);},
    get lastChild() {return this.childNodes.at(-1);}};
}
function textNode(value) {return {nodeType: 3, nodeValue: value, cloneNode() {return textNode(this.nodeValue);}};}
const updater = source.slice(source.indexOf('  function patchNodes('), source.indexOf('  function render('));
const patchNodes = vm.runInNewContext(updater + '\npatchNodes;');
const label = element('span', {}, [textNode('CPU 46°')]);
const chart = element('path', {d: 'M0,20'});
const card = element('article', {class: 'card'}, [label, chart]);
const root = element('div', {}, [card]);
const incoming = element('div', {}, [element('article', {class: 'card warm'}, [element('span', {}, [textNode('CPU 47°')]), element('path', {d: 'M0,19'})])]);
patchNodes(root, incoming);
assert.equal(root.childNodes[0], card);
assert.equal(card.childNodes[0], label);
assert.equal(card.childNodes[1], chart);
assert.equal(label.childNodes[0].nodeValue, 'CPU 47°');
assert.equal(chart.getAttribute('d'), 'M0,19');
assert.equal(card.getAttribute('class'), 'card warm');
patchNodes(root, element('div', {}, [element('article', {class: 'card offline'}, [element('div', {}, [textNode('OFFLINE · Last successful report')])])]));
assert.equal(card.childNodes.length, 1);
assert.equal(card.childNodes[0].tagName, 'DIV');
assert.equal(card.childNodes[0].childNodes[0].nodeValue, 'OFFLINE · Last successful report');

function navigate(page) {
  listeners.click({target: {closest(selector) {return selector === '[data-page]' ? {getAttribute() {return page;}} : null;}}});
  return node('content').innerHTML;
}

setImmediate(() => {
  assert.deepEqual(intervals.map(interval => interval.ms), [30000, 60000, 180000]);
  assert.equal(node('content').classList.classes.has('quiet-update'), true);
  let html = node('content').innerHTML;
  assert.match(html, /CAPO-BOT/);
  assert.match(html, /SECOND PC/);
  assert.match(html, /OFFLINE/);
  assert.match(html, /Website health/);
  assert.doesNotMatch(html, /98°/); // Never render an invented old remote temperature.
  html = navigate('systems');
  assert.equal(node('content').classList.classes.has('quiet-update'), false);
  assert.match(html, /Last successful report/);
  assert.match(html, /Live temperatures are hidden/);
  html = navigate('storage');
  assert.match(html, /Unsafe shutdowns/);
  assert.match(html, /informational/);
  html = navigate('websites');
  assert.match(html, /Bellmore Web Design/);
  assert.match(html, /Coursen/);
  console.log('Frontend overview, systems, storage and websites render passed.');
});
