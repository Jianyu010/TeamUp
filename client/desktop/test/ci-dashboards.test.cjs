const assert = require("node:assert/strict");
const test = require("node:test");
const { allowsCiDashboardRequest } = require("../ci-dashboards.cjs");

const origin = "https://jianyu010.github.io";
const pages = origin + "/TeamUp/";
const chartScript = "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js";

test("opens every published CI dashboard page", () => {
  for (const path of ["", "index.html", "ci.html", "repo.html", "performance.html", "agents.html"]) {
    const url = pages + path;
    assert.ok(allowsCiDashboardRequest({ url, method: "GET", resourceType: "mainFrame" }), url);
  }
});

test("loads only the documented site resources and the pinned Chart.js script", () => {
  for (const [url, resourceType] of [
    [pages + "assets/style.css", "stylesheet"],
    [pages + "assets/app.js", "script"],
    [pages + "data/nightly.jsonl", "xhr"],
    [pages + "data/weekly.jsonl", "xhr"],
    [pages + "data/repo.jsonl", "xhr"],
    [pages + "data/releases.jsonl", "xhr"],
    [pages + "data/ci.jsonl", "xhr"],
    [chartScript, "script"],
  ]) {
    assert.ok(allowsCiDashboardRequest({ url, method: "GET", resourceType, initiatorOrigin: origin }), url);
  }
});

test("blocks other origins, paths, resource types, and request methods in the CI window", () => {
  for (const [url, resourceType, method = "GET", initiatorOrigin = origin] of [
    [origin + "/", "mainFrame"],
    [pages + "ci.html?extra", "mainFrame"],
    [pages + "other.html", "mainFrame"],
    [pages + "agents.html", "subFrame"],
    [pages + "data/ci.jsonl", "mainFrame"],
    [pages + "data/ci.jsonl", "xhr", "POST"],
    [pages + "data/ci.jsonl?", "xhr"],
    [pages + "data/ci.jsonl", "xhr", "GET", "null"],
    [pages + "assets/app.js", "xhr"],
    [chartScript, "mainFrame"],
    [chartScript + "?extra", "script"],
    [chartScript.replace("4.4.1", "4.4.2"), "script"],
    ["https://user@jianyu010.github.io/TeamUp/", "mainFrame"],
    ["https://github.com/Jianyu010/TeamUp", "mainFrame"],
    ["https://raw.githubusercontent.com/Jianyu010/TeamUp/ci-data/data/ci.jsonl", "xhr"],
    ["https://teamup.example/v1/auth/login", "xhr", "POST"],
    ["https://teamup.example/v1/console/rooms", "xhr"],
    ["teamup://console/api/workspace", "xhr"],
  ]) {
    assert.equal(allowsCiDashboardRequest({ url, method, resourceType, initiatorOrigin }), false, url);
  }
});
