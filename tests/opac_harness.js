/*
 * A small DOM and network for exercising widget.js on whole OPAC pages.
 *
 * widget_harness.js covers the record-page panel, where nothing needs
 * finding. The homepage, the "for you" page and the search-by-meaning
 * page do: they look for placeholders and menus, insert at the top of a
 * column, and talk to two different servers. This harness builds the
 * page described in its input, answers each request by address, and
 * prints what a patron would see plus every request made.
 *
 * Usage: node opac_harness.js '<json>'
 *   {path, search, attributes, page: [{tag, id, class, parent, text}],
 *    routes: {"<url prefix>": {status, body}}}
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const input = JSON.parse(process.argv[2] || "{}");

function Node(tag) {
  this.tagName = tag;
  this.children = [];
  this.parentNode = null;
  this.className = "";
  this.id = "";
  this._text = "";
  this.attributes = {};
}
Object.defineProperty(Node.prototype, "firstChild", {
  get: function () { return this.children[0] || null; }
});
Object.defineProperty(Node.prototype, "textContent", {
  get: function () {
    return this._text + this.children.map(function (c) { return c.textContent; }).join("");
  },
  set: function (v) { this._text = String(v); this.children = []; }
});
Node.prototype.appendChild = function (child) {
  child.parentNode = this; this.children.push(child); return child;
};
Node.prototype.insertBefore = function (child, ref) {
  child.parentNode = this;
  const at = ref ? this.children.indexOf(ref) : -1;
  if (at === -1) { this.children.push(child); } else { this.children.splice(at, 0, child); }
  return child;
};
Node.prototype.removeChild = function (child) {
  const at = this.children.indexOf(child);
  if (at === -1) { throw new Error("removeChild: not a child"); }
  this.children.splice(at, 1); child.parentNode = null; return child;
};
Node.prototype.getAttribute = function (n) { return n in this.attributes ? this.attributes[n] : null; };
Node.prototype.setAttribute = function (n, v) { this.attributes[n] = String(v); };

function matches(node, simple) {
  if (simple[0] === "#") { return node.id === simple.slice(1); }
  if (simple[0] === ".") { return (" " + node.className + " ").indexOf(" " + simple.slice(1) + " ") !== -1; }
  if (/^[a-z]+$/.test(simple)) { return node.tagName === simple; }
  throw new Error("harness: unsupported selector " + simple);
}
function descendants(root) {
  const out = [];
  (function walk(n) { n.children.forEach(function (c) { out.push(c); walk(c); }); })(root);
  return out;
}
function select(root, selector) {
  for (const alternative of selector.split(",").map(function (s) { return s.trim(); })) {
    const steps = alternative.split(/\s+/);
    let scope = [root];
    for (const step of steps) {
      const next = [];
      scope.forEach(function (s) { descendants(s).forEach(function (d) {
        if (matches(d, step) && next.indexOf(d) === -1) { next.push(d); } }); });
      scope = next;
    }
    if (scope.length) { return scope[0]; }
  }
  return null;
}

const html = new Node("html");
const head = html.appendChild(new Node("head"));
const body = html.appendChild(new Node("body"));
const byName = { body: body };
(input.page || []).forEach(function (spec) {
  const n = new Node(spec.tag || "div");
  n.id = spec.id || ""; n.className = spec.class || "";
  // Text written in a page is a text node, as in a browser: clearing a
  // placeholder removes it.
  if (spec.text) { const t = new Node("#text"); t._text = spec.text; n.appendChild(t); }
  (byName[spec.parent] || body).appendChild(n);
  if (spec.name) { byName[spec.name] = n; }
  if (spec.id) { byName["#" + spec.id] = n; }
});

const script = new Node("script");
script.attributes = Object.assign({ "data-api": "http://bookrs.test" }, input.attributes || {});

const document = {
  currentScript: script, readyState: "complete", head: head, body: body,
  createElement: function (tag) { return new Node(tag); },
  getElementById: function (id) { return select(html, "#" + id); },
  querySelector: function (sel) { return select(html, sel); },
  querySelectorAll: function () { return []; },
  addEventListener: function () {},
};

const requests = [];
function fetchStub(url, options) {
  requests.push({ url: url, credentials: (options || {}).credentials || null });
  const routes = input.routes || {};
  const key = Object.keys(routes).filter(function (k) { return url.indexOf(k) === 0; })
    .sort(function (a, b) { return b.length - a.length; })[0];
  if (!key) { return Promise.reject(new Error("harness: no route for " + url)); }
  const r = routes[key];
  if (r.network_error) { return Promise.reject(new TypeError("Failed to fetch")); }
  return Promise.resolve({
    ok: r.status >= 200 && r.status < 300, status: r.status,
    json: function () { return Promise.resolve(r.body); },
  });
}

const sandbox = {
  document: document,
  window: { location: { pathname: input.path || "/", search: input.search || "" } },
  fetch: fetchStub, Promise: Promise, Error: Error, TypeError: TypeError,
  encodeURIComponent: encodeURIComponent, decodeURIComponent: decodeURIComponent,
  parseInt: parseInt, parseFloat: parseFloat, Math: Math, console: console,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "bookrs", "api", "static", "widget.js"), "utf8"),
                sandbox, { filename: "widget.js" });

function dump(n) {
  const out = { tag: n.tagName };
  if (n.id) { out.id = n.id; }
  if (n.className) { out.class = n.className; }
  if (n._text) { out.text = n._text; }
  if (n.href) { out.href = n.href; }
  if (Object.keys(n.attributes).length) { out.attrs = n.attributes; }
  if (n.children.length) { out.children = n.children.map(dump); }
  return out;
}
setTimeout(function () {
  process.stdout.write(JSON.stringify({ body: dump(body), requests: requests }));
}, 20);
