const { createRemoteWindow } = require("./remote-window.cjs");

const landing = new URL("https://jianyu010.github.io/TeamUp/");
const pages = new Set(["", "index.html", "ci.html", "repo.html", "performance.html", "agents.html"]
  .map((path) => new URL(path, landing).href));
const siteAssets = new Map([
  ["assets/style.css", "stylesheet"],
  ["assets/app.js", "script"],
]);
const chartScript = "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js";
const resources = new Map([
  ...siteAssets,
  ...["nightly", "weekly", "repo", "releases", "ci"].map((name) => [`data/${name}.jsonl`, "xhr"]),
  [chartScript, "script"],
].map(([path, type]) => [new URL(path, landing).href, type]));

function allowsCiDashboardPage(url) {
  return pages.has(URL.parse(url)?.href);
}

function allowsCiDashboardRequest({ url, method, resourceType, initiatorOrigin }) {
  if (method !== "GET") return false;
  if (resourceType === "mainFrame") return allowsCiDashboardPage(url);
  if (initiatorOrigin !== landing.origin) return false;
  const type = resources.get(URL.parse(url)?.href);
  return type !== undefined && resourceType === type;
}

function createCiDashboards({ BrowserWindow, dialog, ciSession }) {
  return createRemoteWindow({
    BrowserWindow,
    dialog,
    remoteSession: ciSession,
    title: "CI Dashboards",
    retryHint: "Check the connection to the public TeamUp CI Dashboards, then try again.",
    resolveView: () => ({
      landing,
      requiredAssets: Array.from(siteAssets.keys()),
      allowsRequest: allowsCiDashboardRequest,
      allowsNavigation: allowsCiDashboardPage,
    }),
  });
}

module.exports = { allowsCiDashboardRequest, createCiDashboards };
