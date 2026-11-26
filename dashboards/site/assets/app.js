const DATA_FILES = {
  nightly: "data/nightly.jsonl",
  weekly: "data/weekly.jsonl",
  repo: "data/repo.jsonl",
  releases: "data/releases.jsonl",
  ci: "data/ci.jsonl",
};

const REPO_URL = "https://github.com/Jianyu010/TeamUp";
const STATUS_VALUES = ["pass", "fail", "error", "skip"];
const PALETTE = [
  "#2f6fed",
  "#0f9d8b",
  "#c2410c",
  "#7c3aed",
  "#b42318",
  "#0f766e",
  "#a16207",
  "#475569",
];

const METRIC_COPY = {
  agent_turns: {
    title: "Agent Turns per Scenario",
    description: "Counts the turns taken by all agents in each scenario run.",
  },
  dm_delivery_latency: {
    title: "Mention Delivery Time",
    description: "Measures the time from sending a mention to the receiving client reporting the notice.",
  },
  history_db_size: {
    title: "Message History Database Size",
    description: "Shows the database file size at each message history checkpoint.",
  },
  history_full_scan: {
    title: "Full History Scan Time",
    description: "Measures how long it takes to read the full message history page by page.",
  },
  history_latest_page_latency: {
    title: "Latest Messages Load Time",
    description: "Measures how long it takes to fetch the latest 100 messages at each history size.",
  },
  history_post_latency: {
    title: "Posting Time by History Size",
    description: "Measures how long posting a message takes as stored history grows.",
  },
  history_read_latency: {
    title: "Message History Read Time",
    description: "Measures how long reading messages takes at each history size.",
  },
  llm_tokens: {
    title: "Model Token Usage",
    description: "Shows the total model tokens used across all agent turns in each scenario run.",
  },
  model_calls: {
    title: "Model Steps per Scenario",
    description: "Counts completed model steps across all agent turns in each scenario run.",
  },
  room_errors: {
    title: "Failed Room Message Posts",
    description: "Counts failed attempts to post a room message at each load level.",
  },
  room_fanout_latency: {
    title: "Room Message Delivery Time",
    description: "Measures how long room messages take to reach other members at each load level.",
  },
  room_post_latency: {
    title: "Room Message Posting Time",
    description: "Measures how long the server takes to accept a room message at each load level.",
  },
  room_throughput: {
    title: "Room Messages per Second",
    description: "Shows how many room messages the server accepts per second at each load level.",
  },
  room_undelivered: {
    title: "Missed Room Message Deliveries",
    description: "Counts expected recipient deliveries that were still missing at the end of each run.",
  },
  teamup_tool_calls: {
    title: "TeamUp Tool Calls",
    description: "Counts TeamUp tool calls across all agent turns in each scenario run.",
  },
  wake_to_reply_latency: {
    title: "Time to Agent Reply",
    description: "Measures the time from the start of a wake turn to the agent sending its first message.",
  },
  wall_time: {
    title: "Scenario Run Time",
    description: "Measures elapsed time from the start to the end of each scenario run.",
  },
};

const metricValueFormat = new Intl.NumberFormat("en", { maximumSignificantDigits: 4 });

function create(tag, className, text) {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text !== undefined && text !== null) {
    node.textContent = String(text);
  }
  return node;
}

async function loadJsonl(path) {
  try {
    const response = await fetch(path, { cache: "no-store" });
    if (!response.ok) {
      return [];
    }
    const text = await response.text();
    const rows = [];
    for (const line of text.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) {
        continue;
      }
      try {
        rows.push(JSON.parse(trimmed));
      } catch (error) {
        console.warn(`Skipping invalid JSON line in ${path}`, error);
      }
    }
    return rows;
  } catch (error) {
    console.warn(`Cannot load ${path}`, error);
    return [];
  }
}

function numeric(value) {
  if (value === null || value === undefined || value === "") {
    return null;
  }
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function formatInt(value) {
  const number = numeric(value);
  return number === null ? "—" : number.toLocaleString("zh-CN");
}

function shortSha(sha) {
  return typeof sha === "string" && sha ? sha.slice(0, 7) : "—";
}

function runTimestamp(row) {
  if (!row) {
    return 0;
  }
  const raw = row.finished_at || row.started_at || row.date || "";
  const parsed = Date.parse(raw);
  return Number.isFinite(parsed) ? parsed : 0;
}

function runDate(row) {
  if (!row) {
    return "—";
  }
  const raw = row.started_at || row.finished_at || row.date || "";
  return typeof raw === "string" && raw ? raw.slice(0, 10) : "—";
}

function sortedRuns(rows) {
  return rows.slice().sort((a, b) => runTimestamp(a) - runTimestamp(b));
}

function latestRun(rows) {
  return rows.reduce((latest, row) => {
    if (!latest) {
      return row;
    }
    return runTimestamp(row) >= runTimestamp(latest) ? row : latest;
  }, null);
}

function suiteStatus(run) {
  const scenarios = run && Array.isArray(run.scenarios) ? run.scenarios : [];
  if (!scenarios.length) {
    return "skip";
  }
  if (scenarios.some((scenario) => scenario.status === "error")) {
    return "error";
  }
  if (scenarios.some((scenario) => scenario.status === "fail")) {
    return "fail";
  }
  if (scenarios.every((scenario) => scenario.status === "skip")) {
    return "skip";
  }
  return "pass";
}

function setBadge(node, status) {
  if (!node) {
    return;
  }
  const value = STATUS_VALUES.includes(status) ? status : "skip";
  node.className = `status status-${value}`;
  node.textContent = value;
}

function statusBadge(status) {
  const node = create("span");
  setBadge(node, status);
  return node;
}

function setText(id, value) {
  const node = document.getElementById(id);
  if (node) {
    node.textContent = value === null || value === undefined ? "—" : String(value);
  }
}

function showChartEmpty(id, message) {
  const canvas = document.getElementById(id);
  const note = document.querySelector(`[data-empty="${id}"]`);
  if (canvas) {
    canvas.hidden = true;
  }
  if (note) {
    note.textContent = message || "暂无数据。";
    note.hidden = false;
  }
}

function showChartCanvas(id) {
  const canvas = document.getElementById(id);
  const note = document.querySelector(`[data-empty="${id}"]`);
  if (canvas) {
    canvas.hidden = false;
  }
  if (note) {
    note.hidden = true;
  }
}

function chartTheme() {
  const styles = getComputedStyle(document.documentElement);
  return {
    muted: styles.getPropertyValue("--muted").trim(),
    grid: styles.getPropertyValue("--grid").trim(),
  };
}

function initChartDefaults() {
  if (!window.Chart) {
    return;
  }
  const theme = chartTheme();
  window.Chart.defaults.color = theme.muted;
  window.Chart.defaults.borderColor = theme.grid;
  window.Chart.defaults.font.family =
    'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
}

function chartOptions({ yTitle, stacked = false, maxY, wholeNumbers = false } = {}) {
  const theme = chartTheme();
  const yScale = {
    type: "linear",
    beginAtZero: true,
    stacked,
    grid: { color: theme.grid },
    ticks: { maxTicksLimit: 6 },
  };
  if (wholeNumbers) {
    yScale.ticks.precision = 0;
  }
  if (yTitle) {
    yScale.title = { display: true, text: yTitle };
  }
  if (maxY !== undefined) {
    yScale.max = maxY;
  }
  return {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: {
        position: "bottom",
        labels: { boxWidth: 12, usePointStyle: true },
      },
      tooltip: { mode: "index", intersect: false },
    },
    scales: {
      x: {
        grid: { color: theme.grid },
        ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 8 },
      },
      y: yScale,
    },
  };
}

function makeChart(id, config, hasData) {
  if (!hasData) {
    showChartEmpty(id);
    return null;
  }
  if (!window.Chart) {
    showChartEmpty(id, "图表库未加载。");
    return null;
  }
  const canvas = document.getElementById(id);
  if (!canvas) {
    return null;
  }
  showChartCanvas(id);
  return new window.Chart(canvas, config);
}

function lineDataset(label, data, color) {
  return {
    label,
    data,
    borderColor: color,
    backgroundColor: color,
    tension: 0.25,
    pointRadius: 2,
    spanGaps: false,
  };
}

function barDataset(label, data, color) {
  return {
    label,
    data,
    backgroundColor: color,
    borderColor: color,
    borderWidth: 0,
    borderRadius: 3,
  };
}

function renderSuiteSummary(prefix, run) {
  const statusNode = document.getElementById(`${prefix}-status`);
  const dateNode = document.getElementById(`${prefix}-date`);
  const shaNode = document.getElementById(`${prefix}-sha`);
  if (!run) {
    setBadge(statusNode, "skip");
    if (dateNode) {
      dateNode.textContent = "—";
    }
    if (shaNode) {
      shaNode.hidden = true;
    }
    return;
  }
  setBadge(statusNode, suiteStatus(run));
  if (dateNode) {
    dateNode.textContent = runDate(run);
  }
  if (shaNode) {
    if (run.sha) {
      shaNode.hidden = false;
      shaNode.textContent = shortSha(run.sha);
      shaNode.href = `${REPO_URL}/commit/${run.sha}`;
    } else {
      shaNode.hidden = true;
    }
  }
}

async function initIndex() {
  const [nightlyRows, weeklyRows, repoRows] = await Promise.all([
    loadJsonl(DATA_FILES.nightly),
    loadJsonl(DATA_FILES.weekly),
    loadJsonl(DATA_FILES.repo),
  ]);

  renderSuiteSummary("nightly", latestRun(nightlyRows));
  renderSuiteSummary("weekly", latestRun(weeklyRows));

  const repo = repoRows
    .slice()
    .sort((a, b) => String(a.date || "").localeCompare(String(b.date || "")));
  const latest = repo[repo.length - 1];
  const repoEmpty = document.getElementById("repo-empty");

  if (!latest) {
    if (repoEmpty) {
      repoEmpty.hidden = false;
    }
    return;
  }
  if (repoEmpty) {
    repoEmpty.hidden = true;
  }

  setText("stat-stars", formatInt(latest.stars));
  setText("stat-watchers", formatInt(latest.watchers));
  setText("stat-forks", formatInt(latest.forks));
  setText("stat-open-issues", formatInt(latest.open_issues));
  setText("stat-open-prs", formatInt(latest.open_prs));
  setText("stat-downloads", formatInt(latest.release_downloads_total));
  setText("release-tag", latest.latest_release || "—");

  const days = numeric(latest.days_since_release);
  const daysNode = document.getElementById("release-days");
  if (daysNode) {
    daysNode.textContent = days === null ? "无发布数据" : `${days} 天前`;
  }
}

async function initRepo() {
  const [repoRows, releaseRows] = await Promise.all([
    loadJsonl(DATA_FILES.repo),
    loadJsonl(DATA_FILES.releases),
  ]);
  const rows = repoRows
    .slice()
    .sort((a, b) => String(a.date || "").localeCompare(String(b.date || "")));
  const labels = rows.map((row) => row.date || "—");
  const hasRows = rows.length > 0;

  makeChart(
    "popularity-chart",
    {
      type: "line",
      data: {
        labels,
        datasets: [
          lineDataset("Stars", rows.map((row) => numeric(row.stars)), PALETTE[0]),
          lineDataset(
            "Watchers",
            rows.map((row) => numeric(row.watchers)),
            PALETTE[1],
          ),
          lineDataset("Forks", rows.map((row) => numeric(row.forks)), PALETTE[2]),
        ],
      },
      options: chartOptions({ yTitle: "数量", wholeNumbers: true }),
    },
    hasRows,
  );

  makeChart(
    "open-chart",
    {
      type: "line",
      data: {
        labels,
        datasets: [
          lineDataset(
            "开放 Issue",
            rows.map((row) => numeric(row.open_issues)),
            PALETTE[3],
          ),
          lineDataset(
            "开放 PR",
            rows.map((row) => numeric(row.open_prs)),
            PALETTE[1],
          ),
        ],
      },
      options: chartOptions({ yTitle: "数量", wholeNumbers: true }),
    },
    hasRows,
  );

  makeChart(
    "activity-chart",
    {
      type: "bar",
      data: {
        labels,
        datasets: [
          barDataset(
            "新建 Issue",
            rows.map((row) => numeric(row.issues_opened_1d)),
            PALETTE[0],
          ),
          barDataset(
            "关闭 Issue",
            rows.map((row) => numeric(row.issues_closed_1d)),
            PALETTE[1],
          ),
          barDataset(
            "新建 PR",
            rows.map((row) => numeric(row.prs_opened_1d)),
            PALETTE[2],
          ),
          barDataset(
            "合并 PR",
            rows.map((row) => numeric(row.prs_merged_1d)),
            PALETTE[3],
          ),
        ],
      },
      options: chartOptions({ yTitle: "数量", wholeNumbers: true }),
    },
    hasRows,
  );

  const releaseAge = rows.map((row) => numeric(row.days_since_release));
  makeChart(
    "release-age-chart",
    {
      type: "line",
      data: {
        labels,
        datasets: [lineDataset("距上次发布（天）", releaseAge, PALETTE[4])],
      },
      options: chartOptions({ yTitle: "天" }),
    },
    releaseAge.some((value) => value !== null),
  );

  const downloads = rows.map((row) => numeric(row.release_downloads_total));
  const clones = rows.map((row) => numeric(row.clones_14d));
  const views = rows.map((row) => numeric(row.views_14d));
  makeChart(
    "traffic-chart",
    {
      type: "line",
      data: {
        labels,
        datasets: [
          lineDataset("发布下载", downloads, PALETTE[0]),
          lineDataset("14 天 Clone", clones, PALETTE[1]),
          lineDataset("14 天 Views", views, PALETTE[2]),
        ],
      },
      options: chartOptions({ yTitle: "数量", wholeNumbers: true }),
    },
    [downloads, clones, views].some((series) =>
      series.some((value) => value !== null),
    ),
  );

  renderReleases(releaseRows);
}

function renderReleases(releases) {
  const table = document.getElementById("releases-table");
  const body = document.getElementById("releases-body");
  const empty = document.getElementById("releases-empty");
  if (!table || !body || !empty) {
    return;
  }
  body.replaceChildren();

  const rows = releases
    .filter((release) => release && release.tag)
    .sort((a, b) =>
      String(b.published_at || "").localeCompare(String(a.published_at || "")),
    );
  if (!rows.length) {
    table.hidden = true;
    empty.hidden = false;
    return;
  }
  table.hidden = false;
  empty.hidden = true;

  for (const release of rows) {
    const tr = document.createElement("tr");
    tr.append(create("td", null, release.tag));
    tr.append(create("td", null, release.kind || "—"));
    const published =
      typeof release.published_at === "string"
        ? release.published_at.slice(0, 10)
        : "—";
    tr.append(create("td", null, published));
    const prs = Array.isArray(release.prs) ? release.prs.length : 0;
    tr.append(create("td", null, prs));
    body.append(tr);
  }
}

function labelText(labels) {
  if (!labels || typeof labels !== "object") {
    return "default";
  }
  const keys = Object.keys(labels).sort();
  if (!keys.length) {
    return "default";
  }
  return keys.map((key) => `${key}=${labels[key]}`).join(", ");
}

function metricCopy(name) {
  const key = name === "history_db_bytes" ? "history_db_size" : name;
  if (Object.hasOwn(METRIC_COPY, key)) {
    return METRIC_COPY[key];
  }
  const title = name.replaceAll("_", " ");
  return {
    title: title.charAt(0).toUpperCase() + title.slice(1),
    description: "Shows the selected statistic for each benchmark run.",
  };
}

function metricScale(name, unit) {
  if (unit === "bytes") {
    return { yTitle: "GB", divisor: 1e9 };
  }
  if (name === "history_full_scan" && unit === "ms") {
    return { yTitle: "s", divisor: 1e3 };
  }
  if (unit === "tokens") {
    return { yTitle: "k tokens", divisor: 1e3 };
  }
  return {
    yTitle: unit === "count" ? "Count" : unit,
    divisor: 1,
    wholeNumbers: unit === "count",
  };
}

function renderMetricCharts(runs, stat, charts) {
  const container = document.getElementById("metric-charts");
  const empty = document.getElementById("metric-empty");
  if (!container || !empty) {
    return;
  }
  container.replaceChildren();

  const names = new Map();
  runs.forEach((run, runIndex) => {
    const metrics = run && Array.isArray(run.metrics) ? run.metrics : [];
    for (const metric of metrics) {
      if (!metric || typeof metric.name !== "string" || !metric.name) {
        continue;
      }
      let entry = names.get(metric.name);
      if (!entry) {
        entry = { unit: metric.unit || "", series: new Map() };
        names.set(metric.name, entry);
      }
      if (!entry.unit && metric.unit) {
        entry.unit = metric.unit;
      }
      const key = labelText(metric.labels);
      if (!entry.series.has(key)) {
        entry.series.set(key, new Map());
      }
      const stats = metric.stats && typeof metric.stats === "object"
        ? metric.stats
        : {};
      entry.series.get(key).set(runIndex, numeric(stats[stat]));
    }
  });

  if (!names.size) {
    empty.hidden = false;
    return;
  }
  if (!window.Chart) {
    empty.textContent = "图表库未加载。";
    empty.hidden = false;
    return;
  }
  empty.hidden = true;

  const labels = runs.map(runDate);
  for (const [name, entry] of [...names.entries()].sort(([a], [b]) =>
    a.localeCompare(b),
  )) {
    const { title, description } = metricCopy(name);
    const { yTitle, divisor, wholeNumbers } = metricScale(name, entry.unit);
    const card = create("article", "card chart-card");
    card.lang = "en";
    card.append(create("h2", null, title));
    card.append(create("p", "chart-note", description));
    const frame = create("div", "chart-frame");
    const canvas = document.createElement("canvas");
    frame.append(canvas);
    card.append(frame);
    container.append(card);

    const datasets = [];
    let colorIndex = 0;
    for (const [key, values] of entry.series) {
      const color = PALETTE[colorIndex % PALETTE.length];
      const data = runs.map((_, index) => {
        const value = values.get(index);
        return value == null ? null : value / divisor;
      });
      datasets.push(lineDataset(key, data, color));
      colorIndex += 1;
    }

    const options = chartOptions({ yTitle, wholeNumbers });
    options.plugins.tooltip.callbacks = {
      title(items) {
        const index = items[0].dataIndex;
        const run = runs[index] || {};
        return `${labels[index]} · ${shortSha(run.sha)}`;
      },
      label(item) {
        const value = item.parsed.y;
        if (value == null) {
          return `${item.dataset.label}: —`;
        }
        const unit = yTitle ? ` ${yTitle}` : "";
        return `${item.dataset.label}: ${metricValueFormat.format(value)}${unit}`;
      },
    };

    charts.push(
      new window.Chart(canvas, {
        type: "line",
        data: { labels, datasets },
        options,
      }),
    );
  }
}

function initMetricSection(runs) {
  const select = document.getElementById("metric-stat");
  const charts = [];
  const draw = () => {
    for (const chart of charts.splice(0)) {
      chart.destroy();
    }
    renderMetricCharts(runs, select ? select.value : "p50", charts);
  };
  if (select) {
    select.addEventListener("change", draw);
  }
  draw();
}

function renderLatestScenarios(run) {
  const table = document.getElementById("latest-table");
  const body = document.getElementById("latest-body");
  const empty = document.getElementById("latest-empty");
  if (!table || !body || !empty) {
    return;
  }
  body.replaceChildren();
  const scenarios =
    run && Array.isArray(run.scenarios) ? run.scenarios : [];

  if (!scenarios.length) {
    table.hidden = true;
    empty.hidden = false;
    return;
  }
  table.hidden = false;
  empty.hidden = true;

  for (const scenario of scenarios) {
    const tr = document.createElement("tr");
    tr.append(create("td", null, scenario.id || "—"));
    tr.append(create("td", null, scenario.title || "—"));
    const statusCell = document.createElement("td");
    statusCell.append(statusBadge(scenario.status));
    tr.append(statusCell);
    const duration = numeric(scenario.duration_s);
    tr.append(
      create("td", null, duration === null ? "—" : `${duration.toFixed(1)} s`),
    );
    body.append(tr);
  }
}

function renderPassRate(runs) {
  const stats = runs.map((run) => {
    const scenarios = Array.isArray(run.scenarios) ? run.scenarios : [];
    const total = scenarios.length;
    const passed = scenarios.filter(
      (scenario) => scenario.status === "pass",
    ).length;
    return {
      total,
      passed,
      rate: total ? Math.round((passed / total) * 1000) / 10 : null,
    };
  });
  const hasData = stats.some((stat) => stat.total > 0);
  const options = chartOptions({ yTitle: "%", maxY: 100 });
  options.datasets = { bar: { grouped: false } };
  options.plugins.tooltip.callbacks = {
    label(item) {
      const stat = stats[item.dataIndex];
      if (!stat || !stat.total) {
        return "无场景";
      }
      return `通过 ${stat.passed}/${stat.total}（${stat.rate}%）`;
    },
  };

  makeChart(
    "pass-rate-chart",
    {
      type: "bar",
      data: {
        labels: runs.map(runDate),
        datasets: [
          {
            label: "通过率",
            data: stats.map((stat) => stat.rate),
            backgroundColor: PALETTE[1],
            borderRadius: 3,
          },
        ],
      },
      options,
    },
    hasData,
  );
}

function renderStatusGrid(runs) {
  const table = document.getElementById("status-grid-table");
  const empty = document.getElementById("status-grid-empty");
  if (!table || !empty) {
    return;
  }
  const head = table.querySelector("thead");
  const body = table.querySelector("tbody");
  if (!head || !body) {
    return;
  }
  head.replaceChildren();
  body.replaceChildren();

  const columns = runs.filter(
    (run) => Array.isArray(run.scenarios) && run.scenarios.length,
  );
  if (!columns.length) {
    table.hidden = true;
    empty.hidden = false;
    return;
  }
  table.hidden = false;
  empty.hidden = true;

  const ids = [];
  for (const run of columns.slice().reverse()) {
    for (const scenario of run.scenarios) {
      if (scenario && scenario.id && !ids.includes(scenario.id)) {
        ids.push(scenario.id);
      }
    }
  }

  const headRow = document.createElement("tr");
  headRow.append(create("th", null, "场景"));
  for (const run of columns) {
    headRow.append(create("th", null, runDate(run)));
  }
  head.append(headRow);

  const lookups = columns.map((run) => {
    const map = new Map();
    for (const scenario of run.scenarios || []) {
      if (scenario && scenario.id) {
        map.set(scenario.id, scenario.status);
      }
    }
    return map;
  });

  for (const id of ids) {
    const tr = document.createElement("tr");
    const rowHeader = document.createElement("th");
    rowHeader.scope = "row";
    rowHeader.textContent = id;
    tr.append(rowHeader);

    for (const lookup of lookups) {
      const cell = document.createElement("td");
      cell.className = "status-cell";
      const status = lookup.get(id);
      if (status) {
        const value = STATUS_VALUES.includes(status) ? status : "skip";
        cell.classList.add(`status-${value}`);
        cell.textContent = value;
      } else {
        cell.classList.add("status-missing");
        cell.textContent = "—";
      }
      tr.append(cell);
    }
    body.append(tr);
  }
}

async function initSuite(kind) {
  const rows = await loadJsonl(DATA_FILES[kind]);
  const runs = sortedRuns(rows);
  renderLatestScenarios(latestRun(runs));
  renderPassRate(runs);
  initMetricSection(runs);
  if (kind === "weekly") {
    renderStatusGrid(runs);
  }
}

// ---- CI health page (data/ci.jsonl + suite runs) ----

function rateText(passed, judged) {
  if (!judged) {
    return "—";
  }
  return `${Math.round((passed / judged) * 1000) / 10}%`;
}

function windowCounts(rows, key, days) {
  const recent = rows.slice(-days);
  let passed = 0;
  let failed = 0;
  let runs = 0;
  for (const row of recent) {
    const series = row && row[key];
    if (!series) {
      continue;
    }
    passed += numeric(series.success) || 0;
    failed += numeric(series.failure) || 0;
    runs += numeric(series.runs) || 0;
  }
  return { passed, judged: passed + failed, runs };
}

function suiteRunRate(run) {
  const scenarios = Array.isArray(run.scenarios) ? run.scenarios : [];
  const judged = scenarios.filter((item) => item.status !== "skip");
  const passed = judged.filter((item) => item.status === "pass").length;
  return judged.length ? Math.round((passed / judged.length) * 1000) / 10 : null;
}

function renderSuiteTile(prefix, runs, count) {
  const recent = runs.slice(-count);
  const judged = recent.filter((run) => suiteStatus(run) !== "skip");
  const green = judged.filter((run) => suiteStatus(run) === "pass").length;
  setText(`ci-${prefix}-rate`, rateText(green, judged.length));
  setText(
    `ci-${prefix}-count`,
    judged.length ? `${green}/${judged.length} 次全绿` : "暂无运行",
  );
}

function seriesRate(row, key, field) {
  const series = row && row[key];
  if (!series) {
    return null;
  }
  if (field === "first") {
    const judged = (numeric(series.success) || 0) + (numeric(series.failure) || 0);
    return judged
      ? Math.round(((numeric(series.first_attempt_success) || 0) / judged) * 1000) / 10
      : null;
  }
  return numeric(series.pass_rate);
}

function renderFlaky(nightly, weekly) {
  const table = document.getElementById("ci-flaky-table");
  const empty = document.getElementById("ci-flaky-empty");
  if (!table) {
    return;
  }
  const body = table.querySelector("tbody");
  body.replaceChildren();
  const stats = new Map();
  const add = (runs, suite) => {
    for (const run of runs) {
      for (const scenario of Array.isArray(run.scenarios) ? run.scenarios : []) {
        if (!scenario || !scenario.id) {
          continue;
        }
        const key = `${suite}:${scenario.id}`;
        const entry = stats.get(key) || {
          id: scenario.id,
          title: scenario.title || "",
          suite,
          passed: 0,
          judged: 0,
          last: "skip",
        };
        if (scenario.status !== "skip") {
          entry.judged += 1;
          if (scenario.status === "pass") {
            entry.passed += 1;
          }
        }
        entry.last = scenario.status;
        entry.title = scenario.title || entry.title;
        stats.set(key, entry);
      }
    }
  };
  add(nightly.slice(-30), "nightly");
  add(weekly.slice(-12), "weekly");
  const rows = [...stats.values()].sort((a, b) => {
    const ra = a.judged ? a.passed / a.judged : 2;
    const rb = b.judged ? b.passed / b.judged : 2;
    return ra - rb || a.id.localeCompare(b.id);
  });
  if (empty) {
    empty.hidden = rows.length > 0;
  }
  table.hidden = rows.length === 0;
  for (const entry of rows) {
    const tr = create("tr");
    tr.append(
      create("td", null, `${entry.id} ${entry.title}`.trim()),
      create("td", null, entry.suite),
      create("td", null, rateText(entry.passed, entry.judged)),
      create("td", null, `${entry.passed} / ${entry.judged}`),
    );
    const last = create("td");
    last.append(statusBadge(entry.last));
    tr.append(last);
    body.append(tr);
  }
}

async function initCi() {
  const [ciRows, nightlyRows, weeklyRows] = await Promise.all([
    loadJsonl(DATA_FILES.ci),
    loadJsonl(DATA_FILES.nightly),
    loadJsonl(DATA_FILES.weekly),
  ]);
  const days = ciRows.slice().sort((a, b) => String(a.date).localeCompare(String(b.date)));
  const nightly = sortedRuns(nightlyRows);
  const weekly = sortedRuns(weeklyRows);

  for (const [key, id] of [
    ["pr_checks", "pr"],
    ["main_checks", "main"],
  ]) {
    const counts = windowCounts(days, key, 7);
    setText(`ci-${id}-rate`, rateText(counts.passed, counts.judged));
    setText(
      `ci-${id}-count`,
      counts.judged ? `${counts.passed}/${counts.judged} 次通过` : "暂无判定",
    );
  }
  renderSuiteTile("nightly", nightly, 14);
  renderSuiteTile("weekly", weekly, 8);

  const week = days.slice(-7);
  const merged = week.reduce((sum, row) => sum + (numeric(row.prs_merged) || 0), 0);
  const hours = week.map((row) => numeric(row.median_hours_to_merge)).filter((v) => v !== null);
  setText("ci-merged", week.length ? formatInt(merged) : "—");
  if (hours.length) {
    const sorted = hours.slice().sort((a, b) => a - b);
    const mid = sorted[Math.floor(sorted.length / 2)];
    setText("ci-merge-hours", `中位合入耗时约 ${mid} 小时`);
  } else {
    setText("ci-merge-hours", "暂无合入");
  }
  const latest = days[days.length - 1];
  const freezeNode = document.getElementById("ci-freeze");
  if (latest && numeric(latest.freeze_open) !== null) {
    const frozen = numeric(latest.freeze_open) > 0;
    setBadge(freezeNode, frozen ? "fail" : "pass");
    if (freezeNode) {
      freezeNode.textContent = frozen ? "已冻结" : "正常";
    }
    setText("ci-freeze-note", `截至 ${latest.date}`);
  } else {
    setText("ci-freeze-note", "暂无数据");
  }

  const labels = days.map((row) => row.date);
  const dashed = (dataset) => ({ ...dataset, borderDash: [5, 4], pointRadius: 0 });
  makeChart(
    "ci-rate-chart",
    {
      type: "line",
      data: {
        labels,
        datasets: [
          lineDataset("CI · PR", days.map((r) => seriesRate(r, "pr_checks")), PALETTE[0]),
          dashed(lineDataset("CI · PR 首次", days.map((r) => seriesRate(r, "pr_checks", "first")), PALETTE[0])),
          lineDataset("main", days.map((r) => seriesRate(r, "main_checks")), PALETTE[1]),
          lineDataset("AI Review", days.map((r) => seriesRate(r, "review")), PALETTE[3]),
        ],
      },
      options: chartOptions({ yTitle: "%", maxY: 100 }),
    },
    days.length > 0,
  );

  const pr = (field) => days.map((r) => numeric((r.pr_checks || {})[field]));
  makeChart(
    "ci-runs-chart",
    {
      type: "bar",
      data: {
        labels,
        datasets: [
          barDataset("通过", pr("success"), PALETTE[1]),
          barDataset("失败", pr("failure"), PALETTE[4]),
          barDataset("无判定", pr("no_verdict"), PALETTE[7]),
        ],
      },
      options: chartOptions({ yTitle: "Runs", stacked: true, wholeNumbers: true }),
    },
    days.length > 0,
  );

  const suiteRuns = [
    ...nightly.map((run) => ({ run, suite: "nightly" })),
    ...weekly.map((run) => ({ run, suite: "weekly" })),
  ].sort((a, b) => runTimestamp(a.run) - runTimestamp(b.run));
  const options = chartOptions({ yTitle: "%", maxY: 100 });
  options.datasets = { bar: { grouped: false } };
  options.plugins.tooltip.callbacks = {
    afterLabel(item) {
      const entry = suiteRuns[item.dataIndex];
      return entry ? `${entry.suite} · ${shortSha(entry.run.sha)} · ${suiteStatus(entry.run)}` : "";
    },
  };
  makeChart(
    "ci-suite-chart",
    {
      type: "bar",
      data: {
        labels: suiteRuns.map((entry) => runDate(entry.run)),
        datasets: [
          barDataset(
            "nightly",
            suiteRuns.map((e) => (e.suite === "nightly" ? suiteRunRate(e.run) : null)),
            PALETTE[0],
          ),
          barDataset(
            "weekly",
            suiteRuns.map((e) => (e.suite === "weekly" ? suiteRunRate(e.run) : null)),
            PALETTE[2],
          ),
        ],
      },
      options,
    },
    suiteRuns.length > 0,
  );

  const mergeOptions = chartOptions({ yTitle: "PRs", wholeNumbers: true });
  mergeOptions.scales.y1 = {
    type: "linear",
    position: "right",
    beginAtZero: true,
    grid: { drawOnChartArea: false },
    title: { display: true, text: "小时" },
    ticks: { maxTicksLimit: 6 },
  };
  makeChart(
    "ci-merge-chart",
    {
      type: "bar",
      data: {
        labels,
        datasets: [
          barDataset("合入 PR", days.map((r) => numeric(r.prs_merged)), PALETTE[1]),
          {
            ...lineDataset("中位合入耗时（小时）", days.map((r) => numeric(r.median_hours_to_merge)), PALETTE[2]),
            type: "line",
            yAxisID: "y1",
          },
        ],
      },
      options: mergeOptions,
    },
    days.length > 0,
  );

  renderFlaky(nightly, weekly);
}

function initPage() {
  initChartDefaults();
  const page = document.body.dataset.page;
  if (page === "index") {
    return initIndex();
  }
  if (page === "repo") {
    return initRepo();
  }
  if (page === "performance") {
    return initSuite("nightly");
  }
  if (page === "agents") {
    return initSuite("weekly");
  }
  if (page === "ci") {
    return initCi();
  }
  return Promise.resolve();
}

document.addEventListener("DOMContentLoaded", () => {
  initPage().catch((error) => {
    console.error("Dashboard failed to initialise", error);
  });
});
