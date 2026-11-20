/* Policy dashboard: fetches the read-only API and draws the charts.
 *
 * The page intentionally has no build step: Chart.js is vendored under
 * ./vendor and everything else is plain ES2020 in this file.
 */

"use strict";

const PALETTE = [
  "#0072B2",
  "#E69F00",
  "#009E73",
  "#CC79A7",
  "#56B4E9",
  "#D55E00",
  "#8C6D31",
  "#6B7280",
];

const EUR = new Intl.NumberFormat("it-IT", {
  style: "currency",
  currency: "EUR",
  maximumFractionDigits: 0,
});

const EUR2 = new Intl.NumberFormat("it-IT", {
  style: "currency",
  currency: "EUR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const PCT = new Intl.NumberFormat("it-IT", {
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});

const PERCENT = new Intl.NumberFormat("it-IT", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const NAV = new Intl.NumberFormat("it-IT", {
  minimumFractionDigits: 3,
  maximumFractionDigits: 3,
});

const NAV_AXIS = new Intl.NumberFormat("it-IT", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const DATE = new Intl.DateTimeFormat("it-IT", {
  day: "2-digit",
  month: "short",
  year: "numeric",
});

const MONTH_YEAR = new Intl.DateTimeFormat("it-IT", {
  month: "short",
  year: "2-digit",
});

const state = {
  series: [],
  analysis: null,
  basis: "gross",
  daily: [],
  dailyView: "total",
  dailyCache: new Map(),
  market: null,
  marketCache: new Map(),
};

const charts = Object.create(null);

document.addEventListener("DOMContentLoaded", () => {
  init().catch((error) => showError(error));
});

async function init() {
  if (!window.Chart) {
    throw new Error(
      "Chart.js non è disponibile. Verifica che src/assetlens/web/vendor/chart.umd.min.js esista.",
    );
  }

  configureChartDefaults();
  bindBasisToggle();
  bindViewToggle();
  document.getElementById("snapshot").addEventListener("change", onSnapshotChange);
  document.getElementById("start-date").addEventListener("change", loadDaily);
  document.getElementById("market-range").addEventListener("change", loadMarket);

  const [series, analysis] = await Promise.all([
    fetchJSON("/policy/snapshots"),
    fetchJSON("/policy/analysis"),
  ]);

  state.series = series;
  state.analysis = analysis;

  populateSnapshots(series, analysis.snapshot.as_of);
  populateStartDates(series, analysis.snapshot.as_of);
  document.getElementById("market-range").disabled = false;
  renderAll();
  await Promise.all([loadDaily(), loadMarket()]);
}

function configureChartDefaults() {
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.font.size = 12;
  Chart.defaults.color = "#52606f";
  Chart.defaults.borderColor = "rgba(148, 163, 184, 0.25)";
  Chart.defaults.animation = window.matchMedia("(prefers-reduced-motion: reduce)").matches
    ? false
    : { duration: 450 };
}

/* Draw exact amounts next to bars: the tooltip stays available for details. */
const valueLabels = {
  id: "valueLabels",
  afterDatasetsDraw(chart) {
    const ctx = chart.ctx;
    const horizontal = chart.config.options?.indexAxis === "y";
    ctx.save();
    ctx.font = `600 11px ${Chart.defaults.font.family}`;
    ctx.fillStyle = "#334155";
    ctx.textBaseline = horizontal ? "middle" : "bottom";
    ctx.textAlign = horizontal ? "left" : "center";
    chart.getDatasetMeta(0).data.forEach((bar, index) => {
      const label = EUR.format(chart.data.datasets[0].data[index]);
      if (horizontal) {
        ctx.fillText(label, Math.min(bar.x + 6, chart.chartArea.right - 2), bar.y);
      } else {
        ctx.fillText(label, bar.x, Math.max(bar.y - 5, chart.chartArea.top + 11));
      }
    });
    ctx.restore();
  },
};

function fetchJSON(url) {
  return fetch(url, { headers: { Accept: "application/json" } }).then(async (response) => {
    if (response.ok) {
      return response.json();
    }
    let detail = `Errore HTTP ${response.status}`;
    try {
      const payload = await response.json();
      if (payload && payload.detail) {
        detail = payload.detail;
      }
    } catch (_error) {
      /* keep the generic message */
    }
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  });
}

function populateSnapshots(series, selected) {
  const select = document.getElementById("snapshot");
  select.textContent = "";

  const ordered = [...series].sort((a, b) => b.as_of.localeCompare(a.as_of));
  for (const snapshot of ordered) {
    const option = document.createElement("option");
    option.value = snapshot.as_of;
    option.textContent = DATE.format(parseDate(snapshot.as_of));
    option.selected = snapshot.as_of === selected;
    select.append(option);
  }

  // Keep it selectable even with a single date: the user can still see and
  // copy the valuation date, and the control is ready for future snapshots.
  select.disabled = ordered.length === 0;
}

function populateStartDates(series, selected) {
  const select = document.getElementById("start-date");
  select.textContent = "";

  const premiums = state.analysis.snapshot.premiums;
  const today = new Date();
  const candidates = [
    {
      value: selected,
      label: `Dalla rilevazione (${DATE.format(parseDate(selected))})`,
    },
    {
      value: premiums[0].paid_on,
      label: `Dal primo premio (${DATE.format(parseDate(premiums[0].paid_on))})`,
    },
    {
      value: shiftDate(today, -365).toISOString().slice(0, 10),
      label: "Ultimi 12 mesi",
    },
    {
      value: shiftDate(today, -90).toISOString().slice(0, 10),
      label: "Ultimi 3 mesi",
    },
  ];

  const seen = new Set();
  for (const candidate of candidates) {
    if (seen.has(candidate.value)) {
      continue;
    }
    seen.add(candidate.value);
    const option = document.createElement("option");
    option.value = candidate.value;
    option.textContent = candidate.label;
    option.selected = candidate.value === selected;
    select.append(option);
  }

  select.disabled = seen.size === 0;
}

function shiftDate(date, days) {
  const shifted = new Date(date.getTime());
  shifted.setDate(shifted.getDate() + days);
  return shifted;
}

function bindBasisToggle() {
  const buttons = document.querySelectorAll("[data-basis]");
  for (const button of buttons) {
    button.addEventListener("click", () => {
      if (button.dataset.basis === state.basis) {
        return;
      }
      state.basis = button.dataset.basis;
      for (const other of buttons) {
        other.setAttribute("aria-pressed", String(other === button));
      }
      renderTranchesChart();
      renderTranchesTable();
    });
  }
}

function bindViewToggle() {
  const buttons = document.querySelectorAll("[data-view]");
  for (const button of buttons) {
    button.addEventListener("click", () => {
      if (button.dataset.view === state.dailyView) {
        return;
      }
      state.dailyView = button.dataset.view;
      for (const other of buttons) {
        other.setAttribute("aria-pressed", String(other === button));
      }
      renderDailyChart();
    });
  }
}

async function loadDaily() {
  const select = document.getElementById("start-date");
  const start = select.value;
  if (!start) {
    return;
  }

  const note = document.getElementById("daily-note");
  select.disabled = true;
  note.hidden = false;
  note.textContent = "Caricamento della serie giornaliera…";

  try {
    if (!state.dailyCache.has(start)) {
      state.dailyCache.set(start, await fetchJSON(`/policy/daily?start=${encodeURIComponent(start)}`));
    }
    state.daily = state.dailyCache.get(start);
    renderDailyChart();
  } catch (error) {
    showError(error);
  } finally {
    select.disabled = false;
  }
}

async function loadMarket() {
  const select = document.getElementById("market-range");
  const range = select.value;
  if (!range) {
    return;
  }

  const note = document.getElementById("funds-note");
  select.disabled = true;
  note.hidden = false;
  note.textContent = "Caricamento dei valori di quota…";

  try {
    if (!state.marketCache.has(range)) {
      state.marketCache.set(
        range,
        await fetchJSON(`/market/history?range=${encodeURIComponent(range)}`),
      );
    }
    state.market = state.marketCache.get(range);
    renderFundsChart();
    renderGesavChart();
  } catch (error) {
    showError(error);
  } finally {
    select.disabled = false;
  }
}

function shortFundName(name) {
  return name.replace(/^generali\s+/i, "").trim();
}

async function onSnapshotChange(event) {
  const select = event.target;
  select.disabled = true;
  try {
    state.analysis = await fetchJSON(
      `/policy/analysis?as_of=${encodeURIComponent(select.value)}`,
    );
    populateStartDates(state.series, state.analysis.snapshot.as_of);
    renderAll();
    await loadDaily();
  } catch (error) {
    showError(error);
  } finally {
    select.disabled = state.series.length === 0;
  }
}

function renderAll() {
  hideError();
  renderKpis();
  renderPremiumsChart();
  renderTranchesChart();
  renderDailyChart();
  renderHoldingsChart();
  renderTranchesTable();
  renderHoldingsTable();
}

/* ------------------------------------------------------------------ helpers */

function parseDate(iso) {
  return new Date(`${iso}T00:00:00`);
}

function year(iso) {
  return iso.slice(0, 4);
}

function compactEUR(value) {
  if (Math.abs(value) >= 1000) {
    return `${(value / 1000).toLocaleString("it-IT", {
      maximumFractionDigits: 1,
    })}k €`;
  }
  return `${value.toLocaleString("it-IT", { maximumFractionDigits: 0 })} €`;
}

function formatPercent(value) {
  if (value === null || value === undefined) {
    return "–";
  }
  const sign = value > 0 ? "+" : "";
  return `${sign}${PERCENT.format(value)}%`;
}

function signedAmount(value) {
  const sign = value > 0 ? "+" : value < 0 ? "−" : "";
  return `${sign}${EUR2.format(Math.abs(value))}`;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text !== undefined) {
    node.textContent = text;
  }
  return node;
}

function showError(error) {
  const notice = document.getElementById("notice");
  let message = error && error.message ? error.message : "Errore inatteso.";
  if (error && error.status === 404) {
    message =
      "Nessuno snapshot in archivio. Esegui `uv run assetlens policy compute` e ricarica la pagina.";
  }
  notice.hidden = false;
  notice.className = "notice error";
  notice.textContent =
    error && error.status && error.status !== 404
      ? `${message} · verifica che \`uv run assetlens api\` sia in esecuzione su questa porta.`
      : message;
}

function hideError() {
  const notice = document.getElementById("notice");
  notice.hidden = true;
  notice.textContent = "";
}

function currentTranches() {
  return state.basis === "net"
    ? state.analysis.net_tranches
    : state.analysis.gross_tranches;
}

function destroyChart(name) {
  if (charts[name]) {
    charts[name].destroy();
    delete charts[name];
  }
}

/* --------------------------------------------------------------------- KPIs */

function renderKpis() {
  const snapshot = state.analysis.snapshot;
  const container = document.getElementById("kpis");
  container.textContent = "";

  const cards = [
    {
      label: "Capitale investito",
      value: EUR2.format(snapshot.total_premiums),
      hint: `${snapshot.premiums.length} premi versati`,
    },
    {
      label: "Valore lordo",
      value: EUR2.format(snapshot.gross_value),
      hint: "GESAV + comparti",
    },
    {
      label: "Valore netto",
      value: EUR2.format(snapshot.net_value),
      hint: `dopo ${EUR2.format(snapshot.tax_amount)} di imposta`,
    },
    {
      label: "Plusvalenza",
      value: signedAmount(snapshot.capital_gain),
      tone: snapshot.capital_gain >= 0 ? "positive" : "negative",
      hint: `${formatPercent(snapshot.capital_gain !== 0 ? (snapshot.capital_gain / snapshot.total_premiums) * 100 : 0)} sul versato`,
    },
    {
      label: "TIR lordo",
      value: formatPercent(snapshot.gross_irr_pct),
      hint: "rendimento annuo composto",
    },
    {
      label: "TIR netto",
      value: formatPercent(snapshot.net_irr_pct),
      hint: "dopo l'imposta sostitutiva",
    },
  ];

  for (const card of cards) {
    const box = element("article", "kpi");
    box.append(element("p", "kpi-label", card.label));
    const value = element("p", "kpi-value", card.value);
    if (card.tone) {
      value.classList.add(card.tone);
    }
    box.append(value);
    if (card.hint) {
      box.append(element("p", "kpi-hint", card.hint));
    }
    container.append(box);
  }
}

/* ------------------------------------------------------------------- charts */

function renderPremiumsChart() {
  destroyChart("premiums");
  const tranches = state.analysis.gross_tranches;

  charts.premiums = new Chart(document.getElementById("premiums-chart"), {
    type: "bar",
    plugins: [valueLabels],
    data: {
      labels: tranches.map(
        (tranche) => `${year(tranche.paid_on)} · ${PCT.format(tranche.weight_pct)}%`,
      ),
      datasets: [
        {
          label: "Premio versato",
          data: tranches.map((tranche) => tranche.amount),
          backgroundColor: tranches.map((_, index) => PALETTE[index % PALETTE.length]),
          borderRadius: 6,
          maxBarThickness: 56,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: (items) => DATE.format(parseDate(tranches[items[0].dataIndex].paid_on)),
            label: (item) => {
              const tranche = tranches[item.dataIndex];
              return [
                `Versato ${EUR2.format(tranche.amount)}`,
                `${PCT.format(tranche.weight_pct)}% del capitale investito`,
              ];
            },
          },
        },
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: { maxRotation: 0, autoSkip: false },
        },
        y: {
          beginAtZero: true,
          ticks: { callback: (value) => compactEUR(value) },
          grid: { drawBorder: false },
        },
      },
    },
  });
}

function renderTranchesChart() {
  destroyChart("tranches");
  const tranches = currentTranches();
  const snapshot = state.analysis.snapshot;
  const total = state.basis === "net" ? snapshot.net_value : snapshot.gross_value;
  const basisLabel = state.basis === "net" ? "netto" : "lordo";

  charts.tranches = new Chart(document.getElementById("tranches-chart"), {
    type: "bar",
    plugins: [valueLabels],
    data: {
      labels: tranches.map(
        (tranche) => `${year(tranche.paid_on)} · ${PCT.format(tranche.value_weight_pct)}%`,
      ),
      datasets: [
        {
          label: `Valore ${basisLabel} attribuito`,
          data: tranches.map((tranche) => tranche.projected_value),
          backgroundColor: tranches.map((_, index) => PALETTE[index % PALETTE.length]),
          borderRadius: 6,
          maxBarThickness: 40,
        },
      ],
    },
    options: {
      indexAxis: "y",
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: (items) => DATE.format(parseDate(tranches[items[0].dataIndex].paid_on)),
            label: (item) => {
              const tranche = tranches[item.dataIndex];
              return [
                `Valore ${EUR2.format(tranche.projected_value)} (${PCT.format(
                  tranche.value_weight_pct,
                )}% del totale)`,
                `Guadagno ${signedAmount(tranche.gain)}`,
              ];
            },
            footer: () => `Totale ${basisLabel}: ${EUR2.format(total)}`,
          },
        },
      },
      scales: {
        x: {
          beginAtZero: true,
          ticks: { callback: (value) => compactEUR(value) },
          grid: { drawBorder: false },
        },
        y: {
          grid: { display: false },
          ticks: { autoSkip: false },
        },
      },
    },
  });
}

function renderDailyReadout(index) {
  const container = document.getElementById("daily-readout");
  const point = state.daily[index];
  if (!point) {
    return;
  }
  if (container.dataset.index === String(index) && container.dataset.view === state.dailyView) {
    return;
  }
  container.dataset.index = String(index);
  container.dataset.view = state.dailyView;
  container.textContent = "";

  const headline = element("p", "readout-head", DATE.format(parseDate(point.date)));
  const grid = element("div", "readout-grid");
  const rows =
    state.dailyView === "parts"
      ? [
          ["Capitale investito", point.invested, "#64748b"],
          ["GESAV", point.gesav_value, "#E69F00"],
          ["Fondi", point.funds_value, "#56B4E9"],
          ["Totale lordo", point.gross_value, "#009E73"],
        ]
      : [
          ["Capitale investito", point.invested, "#64748b"],
          ["Valore lordo", point.gross_value, "#009E73"],
          ["Valore netto", point.net_value, "#0072B2"],
          ["Plusvalenza", point.gross_value - point.invested, "#009E73"],
          ["Imposta", point.gross_value - point.net_value, "#D55E00"],
        ];

  for (const [label, value, color] of rows) {
    const item = element("span", "readout-item");
    const dot = element("span", "readout-dot");
    dot.style.backgroundColor = color;
    item.append(dot, element("span", "readout-label", label), element("span", "readout-value", EUR2.format(value)));
    grid.append(item);
  }
  container.append(headline, grid);
}

function renderDailyChart() {
  destroyChart("daily");
  const readout = document.getElementById("daily-readout");
  readout.textContent = "";
  readout.removeAttribute("data-index");
  const hint = element(
    "span",
    "readout-hint",
    "Passa il mouse sul grafico per leggere i valori esatti di ogni giorno.",
  );
  readout.append(hint);
  const note = document.getElementById("daily-note");

  if (!state.daily.length) {
    note.hidden = false;
    note.textContent = "Nessun valore giornaliero nel periodo selezionato.";
    return;
  }

  const points = state.daily;
  const line = (label, field, color, options = {}) => ({
    label,
    data: points.map((point) => ({ x: parseDate(point.date).getTime(), y: point[field] })),
    borderColor: color,
    backgroundColor: color,
    borderWidth: 2,
    pointRadius: 0,
    pointHoverRadius: 4,
    tension: 0,
    fill: false,
    ...options,
  });

  const datasets = [
    line("Capitale investito", "invested", "#64748b", { borderDash: [6, 4] }),
  ];
  if (state.dailyView === "parts") {
    datasets.push(line("GESAV", "gesav_value", "#E69F00"));
    datasets.push(line("Fondi", "funds_value", "#56B4E9"));
  } else {
    datasets.push(line("Valore lordo", "gross_value", "#009E73"));
    datasets.push(line("Valore netto", "net_value", "#0072B2"));
  }

  charts.daily = new Chart(document.getElementById("daily-chart"), {
    type: "line",
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      parsing: false,
      normalized: true,
      interaction: { mode: "index", intersect: false },
      onHover: (_event, elements) => {
        if (elements.length) {
          renderDailyReadout(elements[0].index);
        }
      },
      onClick: (_event, elements) => {
        if (elements.length) {
          renderDailyReadout(elements[0].index);
        }
      },
      plugins: {
        legend: { position: "top", labels: { usePointStyle: true, boxWidth: 8 } },
        tooltip: {
          callbacks: {
            title: (items) => DATE.format(new Date(items[0].parsed.x)),
            label: (item) => `${item.dataset.label}: ${EUR2.format(item.parsed.y)}`,
          },
        },
      },
      scales: {
        x: {
          type: "linear",
          ticks: {
            maxTicksLimit: 9,
            autoSkip: true,
            callback: (value) => MONTH_YEAR.format(new Date(value)),
          },
          grid: { display: false },
        },
        y: {
          // A line chart can start above zero: that makes the daily changes
          // readable. Bars in the other cards still start at zero.
          beginAtZero: false,
          grace: "12%",
          ticks: { callback: (value) => compactEUR(value) },
          grid: { drawBorder: false },
        },
      },
    },
  });

  note.hidden = false;
  note.textContent = `${points.length} giorni dal ${DATE.format(parseDate(points[0].date))} al ${DATE.format(parseDate(points.at(-1).date))}. L'asse euro non parte da zero per rendere visibili le variazioni: i valori esatti sono nel riquadro sopra e nei tooltip. Ricostruzione indicativa: la composizione della rilevazione è considerata costante nel periodo.`;
}

function renderFundsChart() {
  destroyChart("funds");
  const note = document.getElementById("funds-note");
  const funds = (state.market?.funds || []).filter((fund) => fund.points.length > 0);
  if (!funds.length) {
    note.hidden = false;
    note.textContent = "Nessun valore di fondo nel periodo selezionato.";
    return;
  }

  const showPoints = state.market.range === "premiums";
  const datasets = funds.map((fund, index) => {
    const color = PALETTE[index % PALETTE.length];
    return {
      label: shortFundName(fund.name),
      data: fund.points.map((point) => ({
        x: parseDate(point.date).getTime(),
        y: point.value,
      })),
      borderColor: color,
      backgroundColor: color,
      borderWidth: 2,
      pointRadius: showPoints ? 2 : 0,
      pointHoverRadius: 5,
      tension: 0,
      spanGaps: false,
      fill: false,
    };
  });

  charts.funds = new Chart(document.getElementById("funds-chart"), {
    type: "line",
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      parsing: false,
      normalized: true,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { position: "top", labels: { usePointStyle: true, boxWidth: 8 } },
        tooltip: {
          callbacks: {
            title: (items) => DATE.format(new Date(items[0].parsed.x)),
            label: (item) => `${item.dataset.label}: ${NAV.format(item.parsed.y)} (quota)`,
          },
        },
      },
      scales: {
        x: {
          type: "linear",
          ticks: {
            maxTicksLimit: 9,
            autoSkip: true,
            callback: (value) => MONTH_YEAR.format(new Date(value)),
          },
          grid: { display: false },
        },
        y: {
          beginAtZero: false,
          grace: "10%",
          ticks: { callback: (value) => NAV_AXIS.format(value) },
          grid: { drawBorder: false },
        },
      },
    },
  });

  const dates = state.market.premium_dates.map((day) => DATE.format(parseDate(day))).join(", ");
  note.hidden = false;
  note.textContent = showPoints
    ? `${funds.length} fondi · ${funds[0].points.length} osservazioni solo nei mesi dei premi (${dates}). I valori sono quote NAV, non importi in euro.`
    : `${funds.length} fondi · ${funds[0].points.length} osservazioni per fondo. I valori sono quote NAV, non importi in euro.`;
}

function renderGesavChart() {
  destroyChart("gesav");
  const note = document.getElementById("gesav-note");
  const points = state.market?.gesav || [];
  if (!points.length) {
    note.hidden = false;
    note.textContent = "Nessun rendimento GESAV nel periodo selezionato.";
    return;
  }

  charts.gesav = new Chart(document.getElementById("gesav-chart"), {
    type: "line",
    data: {
      datasets: [
        {
          label: "Rendimento annuo",
          data: points.map((point) => ({
            x: parseDate(point.date).getTime(),
            y: point.value,
          })),
          borderColor: "#E69F00",
          backgroundColor: "#E69F00",
          borderWidth: 2,
          pointRadius: state.market.range === "premiums" ? 4 : 0,
          pointHoverRadius: 5,
          tension: 0,
          spanGaps: false,
          fill: false,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      parsing: false,
      normalized: true,
      interaction: { mode: "nearest", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: (items) => DATE.format(new Date(items[0].parsed.x)),
            label: (item) => `Rendimento annuo: ${PERCENT.format(item.parsed.y)}%`,
          },
        },
      },
      scales: {
        x: {
          type: "linear",
          ticks: {
            maxTicksLimit: 9,
            autoSkip: true,
            callback: (value) => MONTH_YEAR.format(new Date(value)),
          },
          grid: { display: false },
        },
        y: {
          beginAtZero: true,
          ticks: { callback: (value) => `${PCT.format(value)}%` },
          grid: { drawBorder: false },
        },
      },
    },
  });

  const latest = points.at(-1);
  note.hidden = false;
  note.textContent = `${points.length} osservazioni · ultimo rendimento annuo ${PERCENT.format(latest.value)}% (${DATE.format(parseDate(latest.date))}).`;
}

function renderHoldingsChart() {
  destroyChart("holdings");
  const snapshot = state.analysis.snapshot;
  const holdings = [...snapshot.holdings].sort((a, b) => b.value - a.value);
  const total = snapshot.gross_value || 1;

  charts.holdings = new Chart(document.getElementById("holdings-chart"), {
    type: "bar",
    plugins: [valueLabels],
    data: {
      labels: holdings.map(
        (holding) => `${holding.name} · ${PCT.format((holding.value / total) * 100)}%`,
      ),
      datasets: [
        {
          label: "Valore",
          data: holdings.map((holding) => holding.value),
          backgroundColor: holdings.map((holding) =>
            holding.name === "GESAV" ? "#E69F00" : "#0072B2",
          ),
          borderRadius: 6,
          maxBarThickness: 34,
        },
      ],
    },
    options: {
      indexAxis: "y",
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (item) => {
              const holding = holdings[item.dataIndex];
              const share = (holding.value / total) * 100;
              const details = [`Valore ${EUR2.format(holding.value)} (${PCT.format(share)}%)`];
              if (holding.quote !== null && holding.quote !== undefined) {
                details.push(`Quote ${holding.quote.toLocaleString("it-IT")}`);
              }
              if (holding.nav !== null && holding.nav !== undefined) {
                details.push(`NAV ${EUR2.format(holding.nav)}`);
              }
              return details;
            },
          },
        },
      },
      scales: {
        x: {
          beginAtZero: true,
          ticks: { callback: (value) => compactEUR(value) },
          grid: { drawBorder: false },
        },
        y: {
          grid: { display: false },
          ticks: { autoSkip: false },
        },
      },
    },
  });
}

/* -------------------------------------------------------------------- table */

function renderTranchesTable() {
  const snapshot = state.analysis.snapshot;
  const tranches = currentTranches();
  const total = state.basis === "net" ? snapshot.net_value : snapshot.gross_value;
  const body = document.querySelector("#tranches-table tbody");
  body.textContent = "";

  for (const tranche of tranches) {
    const row = document.createElement("tr");
    row.append(
      cell(DATE.format(parseDate(tranche.paid_on))),
      cell(EUR2.format(tranche.amount), "num"),
      cell(`${PCT.format(tranche.weight_pct)}%`, "num"),
      cell(PCT.format(tranche.years_invested), "num"),
      cell(EUR2.format(tranche.projected_value), "num"),
      cell(`${PCT.format(tranche.value_weight_pct)}%`, "num"),
      cell(signedAmount(tranche.gain), "num"),
    );
    body.append(row);
  }

  const footer = document.createElement("tr");
  footer.className = "total-row";
  footer.append(
    cell("Totale"),
    cell(EUR2.format(snapshot.total_premiums), "num"),
    cell("100,0%", "num"),
    cell("", "num"),
    cell(EUR2.format(total), "num"),
    cell("100,0%", "num"),
    cell(signedAmount(total - snapshot.total_premiums), "num"),
  );
  body.append(footer);
}

function renderHoldingsTable() {
  const snapshot = state.analysis.snapshot;
  const holdings = [...snapshot.holdings].sort((a, b) => b.value - a.value);
  const total = snapshot.gross_value || 1;
  const body = document.querySelector("#holdings-table tbody");
  body.textContent = "";

  for (const holding of holdings) {
    const row = document.createElement("tr");
    row.append(
      cell(holding.name),
      cell(
        holding.quote !== null && holding.quote !== undefined
          ? holding.quote.toLocaleString("it-IT", {
              minimumFractionDigits: 5,
              maximumFractionDigits: 5,
            })
          : "–",
        "num",
      ),
      cell(
        holding.nav !== null && holding.nav !== undefined ? EUR2.format(holding.nav) : "–",
        "num",
      ),
      cell(EUR2.format(holding.value), "num"),
      cell(`${PCT.format((holding.value / total) * 100)}%`, "num"),
    );
    body.append(row);
  }

  const footer = document.createElement("tr");
  footer.className = "total-row";
  footer.append(
    cell("Totale"),
    cell("", "num"),
    cell("", "num"),
    cell(EUR2.format(snapshot.gross_value), "num"),
    cell("100,0%", "num"),
  );
  body.append(footer);
}

function cell(text, className) {
  return element("td", className || "", text);
}
