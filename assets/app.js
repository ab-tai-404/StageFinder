"use strict";

const authTabs = Array.from(document.querySelectorAll(".auth-tab"));
const authPanels = Array.from(document.querySelectorAll(".auth-panel"));

function showAuthPanel(panelId) {
  for (const panel of authPanels) {
    const active = panel.id === panelId;
    panel.hidden = !active;
    panel.setAttribute("aria-hidden", String(!active));
  }
  for (const tab of authTabs) {
    const active = tab.getAttribute("aria-controls") === panelId;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  }
}

for (const tab of authTabs) {
  tab.addEventListener("click", () => showAuthPanel(tab.getAttribute("aria-controls")));
}

if (authTabs.length) {
  showAuthPanel(window.location.hash === "#signup" ? "signup-panel" : "login-panel");
}

const cards = Array.from(document.querySelectorAll(".internship-card"));
const searchInput = document.querySelector("#search-input");
const cityFilter = document.querySelector("#city-filter");
const categoryFilter = document.querySelector("#category-filter");
const resultsCount = document.querySelector("#results-count");
const emptyState = document.querySelector("#empty-state");

function filterInternships() {
  const query = searchInput.value.trim().toLocaleLowerCase();
  const city = cityFilter.value;
  const category = categoryFilter.value;
  let visibleCount = 0;

  for (const card of cards) {
    const matches = (!city || card.dataset.city === city)
      && (!category || card.dataset.category === category)
      && (!query || card.dataset.search.toLocaleLowerCase().includes(query));
    card.hidden = !matches;
    if (matches) visibleCount += 1;
  }

  resultsCount.textContent = `${visibleCount} offre${visibleCount === 1 ? "" : "s"} dans les données`;
  emptyState.hidden = visibleCount > 0;
}

if (searchInput && cityFilter && categoryFilter) {
  searchInput.addEventListener("input", filterInternships);
  cityFilter.addEventListener("change", filterInternships);
  categoryFilter.addEventListener("change", filterInternships);

  const params = new URLSearchParams(window.location.search);
  const requestedCity = params.get("city");
  const requestedCategory = params.get("category");
  if (requestedCity && Array.from(cityFilter.options).some(option => option.value === requestedCity)) {
    cityFilter.value = requestedCity;
  }
  if (requestedCategory && Array.from(categoryFilter.options).some(option => option.value === requestedCategory)) {
    categoryFilter.value = requestedCategory;
  }
  if (requestedCity || requestedCategory) filterInternships();
}

const chartDataElement = document.querySelector("#chart-data");
if (chartDataElement) {
  const chartData = JSON.parse(chartDataElement.textContent);
  const colors = ["#c5f36b", "#73d5ce", "#8d9cff", "#ff9d75", "#f6ce73", "#c79bdb"];
  const svgNamespace = "http://www.w3.org/2000/svg";

  function makeSvgElement(name, attributes = {}) {
    const element = document.createElementNS(svgNamespace, name);
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
    return element;
  }

  function renderBarChart(containerId, title, data, horizontal = false) {
    const container = document.getElementById(containerId);
    if (!container) return;

    const entries = Object.entries(data).slice(0, 10);
    const width = 620;
    const rowHeight = horizontal ? 36 : 220;
    const height = horizontal ? Math.max(130, entries.length * rowHeight + 50) : 320;
    const svg = makeSvgElement("svg", { viewBox: `0 0 ${width} ${height}`, role: "presentation" });
    const heading = makeSvgElement("text", { x: 14, y: 25, class: "chart-title" });
    heading.textContent = title;
    svg.append(heading);

    const maxValue = Math.max(1, ...entries.map(([, value]) => Number(value)));
    if (horizontal) {
      entries.forEach(([label, value], index) => {
        const y = 52 + index * rowHeight;
        const labelText = makeSvgElement("text", { x: 14, y: y + 13, class: "chart-label" });
        labelText.textContent = label;
        svg.append(labelText);
        svg.append(makeSvgElement("rect", {
          x: 14, y: y + 19, width: Math.max(2, (width - 100) * Number(value) / maxValue),
          height: 10, rx: 5, fill: colors[index % colors.length],
        }));
        const valueText = makeSvgElement("text", { x: width - 58, y: y + 28, class: "chart-value" });
        valueText.textContent = String(value);
        svg.append(valueText);
      });
    } else {
      const chartTop = 55;
      const chartBottom = height - 58;
      const chartHeight = chartBottom - chartTop;
      const slotWidth = (width - 48) / Math.max(entries.length, 1);
      entries.forEach(([label, value], index) => {
        const barHeight = chartHeight * Number(value) / maxValue;
        const barWidth = Math.min(42, slotWidth * 0.62);
        const x = 24 + index * slotWidth + (slotWidth - barWidth) / 2;
        const text = makeSvgElement("text", {
          x: x + barWidth / 2, y: chartBottom - barHeight - 7, class: "chart-value", "text-anchor": "middle",
        });
        text.textContent = String(value);
        svg.append(text);
        svg.append(makeSvgElement("rect", {
          x, y: chartBottom - barHeight, width: barWidth, height: barHeight,
          rx: 5, fill: colors[index % colors.length],
        }));
        const labelText = makeSvgElement("text", {
          x: x + barWidth / 2, y: chartBottom + 18, class: "chart-label", "text-anchor": "middle",
        });
        labelText.textContent = label.length > 13 ? `${label.slice(0, 12)}…` : label;
        svg.append(labelText);
      });
    }

    container.replaceChildren(svg);
  }

  function renderDonutChart(containerId, title, data) {
    const container = document.getElementById(containerId);
    if (!container) return;
    const entries = Object.entries(data);
    const total = entries.reduce((sum, [, value]) => sum + Number(value), 0);
    const svg = makeSvgElement("svg", { viewBox: "0 0 620 360", role: "presentation" });
    const heading = makeSvgElement("text", { x: 14, y: 25, class: "chart-title" });
    heading.textContent = title;
    svg.append(heading);
    let offset = 0;
    const radius = 72;
    const circumference = 2 * Math.PI * radius;
    entries.forEach(([label, value], index) => {
      const fraction = total ? Number(value) / total : 0;
      svg.append(makeSvgElement("circle", {
        cx: 175, cy: 190, r: radius, fill: "none", stroke: colors[index % colors.length],
        "stroke-width": 28, "stroke-dasharray": `${fraction * circumference} ${circumference}`,
        "stroke-dashoffset": -offset, transform: "rotate(-90 175 190)",
      }));
      offset += fraction * circumference;
      const y = 85 + index * 29;
      svg.append(makeSvgElement("circle", { cx: 315, cy: y - 4, r: 5, fill: colors[index % colors.length] }));
      const labelText = makeSvgElement("text", { x: 329, y, class: "chart-label" });
      labelText.textContent = `${label} (${value})`;
      svg.append(labelText);
    });
    container.replaceChildren(svg);
  }

  renderBarChart("city-chart", "Où trouver un stage ?", chartData.cities);
  renderDonutChart("category-chart", "Les domaines qui recrutent", chartData.categories);
  renderBarChart("business-chart", "Écosystème professionnel à Marrakech", chartData.businesses, true);
}

const profileMatchButton = document.querySelector("#match-profile");
if (profileMatchButton) {
  profileMatchButton.addEventListener("click", () => {
    const desiredCity = profileMatchButton.dataset.city;
    const desiredCategory = profileMatchButton.dataset.category;
    cityFilter.value = Array.from(cityFilter.options).some(option => option.value === desiredCity)
      ? desiredCity : "";
    categoryFilter.value = Array.from(categoryFilter.options).some(option => option.value === desiredCategory)
      ? desiredCategory : "";
    searchInput.value = "";
    filterInternships();
    window.location.hash = "offres";
  });
}

const profileForm = document.querySelector(".profile-form");
if (profileForm) {
  const watchedFields = Array.from(profileForm.querySelectorAll("input:not([type=file]), select, textarea"));
  const completionFields = ["first_name", "last_name", "email", "phone", "city", "education",
    "school", "specialty", "internship_type", "target_city", "availability", "skills", "about"];
  const completionLabel = document.querySelector("#profile-completion");
  const progressBar = document.querySelector("#profile-progress");

  function updateProfileCompletion() {
    const values = new Map(watchedFields.map(field => [field.name, field.value.trim()]));
    const percentage = Math.round(completionFields.filter(name => values.get(name)).length / completionFields.length * 100);
    completionLabel.textContent = `${percentage}%`;
    progressBar.style.width = `${percentage}%`;
  }

  watchedFields.forEach(field => field.addEventListener("input", updateProfileCompletion));
  watchedFields.forEach(field => field.addEventListener("change", updateProfileCompletion));
}
