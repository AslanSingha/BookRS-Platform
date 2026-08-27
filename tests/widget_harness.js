/*
 * A minimal DOM for exercising widget.js outside a browser.
 *
 * The widget is the only code in this project a patron reads, and its
 * heading now makes a conditional claim about the library's own
 * circulation. That is worth testing, and testing at the level a patron
 * experiences -- the rendered heading -- rather than by extracting the
 * rule into a helper and asserting on the helper.
 *
 * No npm. The widget touches a small, fixed part of the DOM API, so the
 * stub below is complete enough to run it unmodified. If widget.js
 * starts using something absent here, this file throws rather than
 * silently rendering nothing, which is the failure mode worth avoiding.
 *
 * Usage:  node widget_harness.js '<json>'
 * where json is {attributes: {...}, results: [...]}
 * Prints the rendered panel as JSON.
 */

"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const input = JSON.parse(process.argv[2] || "{}");
const attributes = input.attributes || {};
const results = input.results || [];

function Node(tag) {
  this.tagName = tag;
  this.children = [];
  this.className = "";
  this.textContent = "";
  this.attributes = {};
}
Node.prototype.appendChild = function (child) {
  this.children.push(child);
  return child;
};
Node.prototype.getAttribute = function (name) {
  return name in this.attributes ? this.attributes[name] : null;
};
Node.prototype.setAttribute = function (name, value) {
  this.attributes[name] = value;
};

const body = new Node("body");
const head = new Node("head");

const currentScript = new Node("script");
currentScript.attributes = attributes;

const document = {
  currentScript: currentScript,
  readyState: "complete",
  body: body,
  head: head,
  createElement: function (tag) { return new Node(tag); },
  getElementById: function () { return null; },
  querySelector: function () { return null; },
  addEventListener: function () {},
};

// Resolves in FIFO order: first the record lookup, then the similar call.
const responses = [
  { id: 1 },
  { work_id: 1, count: results.length, results: results },
];
function fetchStub() {
  const payload = responses.shift();
  return Promise.resolve({
    ok: true,
    status: 200,
    json: function () { return Promise.resolve(payload); },
  });
}

const sandbox = {
  document: document,
  window: { location: { search: "?biblionumber=345" } },
  fetch: fetchStub,
  encodeURIComponent: encodeURIComponent,
  parseInt: parseInt,
  Promise: Promise,
  Error: Error,
  console: console,
};
sandbox.globalThis = sandbox;

const source = fs.readFileSync(
  path.join(__dirname, "..", "bookrs", "api", "static", "widget.js"),
  "utf8"
);
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: "widget.js" });

// The widget renders after two resolved promises; drain the microtask
// queue before reading the result.
setTimeout(function () {
  const panel = body.children.find(function (n) {
    return n.className === "bookrs-panel";
  });
  if (!panel) {
    process.stdout.write(JSON.stringify({ rendered: false }));
    return;
  }
  const heading = panel.children.find(function (n) {
    return n.className === "bookrs-heading";
  });
  const list = panel.children.find(function (n) {
    return n.className === "bookrs-list";
  });
  const markers = (list ? list.children : []).map(function (item) {
    const badge = item.children.find(function (n) {
      return n.className === "bookrs-signal";
    });
    return badge ? badge.textContent : null;
  });
  process.stdout.write(JSON.stringify({
    rendered: true,
    heading: heading ? heading.textContent : null,
    markers: markers,
  }));
}, 0);
