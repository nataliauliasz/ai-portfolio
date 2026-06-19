const initializeMetricCharts = (root = document) => {
  root.querySelectorAll("[data-metric-chart]").forEach((chart) => {
    if (chart.dataset.metricChartInitialized === "true") {
      return;
    }
    chart.dataset.metricChartInitialized = "true";

    const toggleButtons = Array.from(chart.querySelectorAll("[data-series-toggle]"));
    let refreshActiveHover = null;

    const setSeriesState = (seriesKey, isActive) => {
      toggleButtons.forEach((button) => {
        if (button.dataset.seriesToggle !== seriesKey || button.disabled) {
          return;
        }
        button.setAttribute("aria-pressed", isActive ? "true" : "false");
        button.classList.toggle("is-active", isActive);
      });

      chart.querySelectorAll(`[data-series-key="${seriesKey}"]`).forEach((element) => {
        element.classList.toggle("is-hidden", !isActive);
      });

      if (typeof refreshActiveHover === "function") {
        refreshActiveHover();
      }
    };

    if (!toggleButtons.length) {
      return;
    }

    toggleButtons.forEach((button) => {
      if (button.disabled) {
        return;
      }

      setSeriesState(button.dataset.seriesToggle, button.getAttribute("aria-pressed") !== "false");
      button.addEventListener("click", () => {
        const isActive = button.getAttribute("aria-pressed") === "true";
        setSeriesState(button.dataset.seriesToggle, !isActive);
      });
    });

    chart.querySelectorAll("[data-series-action]").forEach((button) => {
      button.addEventListener("click", () => {
        const shouldEnable = button.dataset.seriesAction === "all";
        toggleButtons.forEach((toggleButton) => {
          if (toggleButton.disabled) {
            return;
          }
          setSeriesState(toggleButton.dataset.seriesToggle, shouldEnable);
        });
      });
    });

    const metricPlot = chart.querySelector(".metric-plot[data-hover-points]");
    const hoverOverlay = metricPlot?.querySelector("[data-metric-hover]");
    if (!metricPlot || !hoverOverlay) {
      return;
    }

    let hoverPoints = [];
    try {
      hoverPoints = JSON.parse(metricPlot.dataset.hoverPoints || "[]");
    } catch (_error) {
      hoverPoints = [];
    }
    if (!hoverPoints.length) {
      return;
    }

    const hoverGuide = hoverOverlay.querySelector(".metric-hover-guide");
    const hoverTooltip = hoverOverlay.querySelector(".metric-hover-tooltip");
    const hoverTime = hoverOverlay.querySelector("[data-hover-time]");
    const hoverStatus = hoverOverlay.querySelector("[data-hover-status]");
    const hoverValueTargets = Object.fromEntries(
      Array.from(hoverOverlay.querySelectorAll("[data-hover-value-key]")).map((element) => [
        element.dataset.hoverValueKey,
        element,
      ]),
    );
    const hoverMarkers = Object.fromEntries(
      Array.from(hoverOverlay.querySelectorAll("[data-hover-point]")).map((element) => [
        element.dataset.hoverPoint,
        element,
      ]),
    );

    let activeSample = null;

    const renderHover = (sample) => {
      activeSample = sample;
      hoverOverlay.hidden = false;
      hoverGuide.style.left = `${sample.x_pct}%`;
      hoverTime.textContent = sample.time_label || "";
      hoverStatus.textContent = sample.status_label || "N/A";
      Object.entries(hoverValueTargets).forEach(([seriesKey, target]) => {
        target.textContent = sample.value_labels?.[seriesKey] || "n/a";
      });

      Object.entries(hoverMarkers).forEach(([seriesKey, marker]) => {
        if (!marker) {
          return;
        }

        const toggleButton = toggleButtons.find((button) => button.dataset.seriesToggle === seriesKey);
        const isActive = !toggleButton || toggleButton.getAttribute("aria-pressed") === "true";
        const yPct = sample.series_positions?.[seriesKey];
        const shouldShow = isActive && typeof yPct === "number";
        marker.classList.toggle("is-hidden", !shouldShow);
        if (shouldShow) {
          marker.style.left = `${sample.x_pct}%`;
          marker.style.top = `${yPct}%`;
        }
      });

      requestAnimationFrame(() => {
        const plotRect = metricPlot.getBoundingClientRect();
        const tooltipWidth = hoverTooltip.offsetWidth;
        const plotWidth = plotRect.width;
        const anchorPx = (sample.x_pct / 100) * plotWidth;
        const desiredLeft = Math.min(Math.max(anchorPx + 12, 8), Math.max(plotWidth - tooltipWidth - 8, 8));
        hoverTooltip.style.left = `${desiredLeft}px`;
      });
    };

    const hideHover = () => {
      activeSample = null;
      hoverOverlay.hidden = true;
      Object.values(hoverMarkers).forEach((marker) => marker?.classList.add("is-hidden"));
    };

    const findNearestPoint = (xPct) => {
      let nearestPoint = hoverPoints[0];
      let smallestDistance = Math.abs((nearestPoint?.x_pct ?? 0) - xPct);

      hoverPoints.forEach((point) => {
        const distance = Math.abs((point?.x_pct ?? 0) - xPct);
        if (distance < smallestDistance) {
          nearestPoint = point;
          smallestDistance = distance;
        }
      });

      return nearestPoint;
    };

    refreshActiveHover = () => {
      if (activeSample) {
        renderHover(activeSample);
      }
    };

    metricPlot.addEventListener("mousemove", (event) => {
      const rect = metricPlot.getBoundingClientRect();
      const xPct = Math.max(0, Math.min(100, ((event.clientX - rect.left) / rect.width) * 100));
      renderHover(findNearestPoint(xPct));
    });
    metricPlot.addEventListener("mouseleave", hideHover);
  });
};

const initializeAsyncAnalysis = () => {
  const panel = document.querySelector("[data-analysis-panel]");
  if (!panel || panel.dataset.analysisActive !== "true") {
    return;
  }
  if (panel.dataset.analysisAutostart !== "true") {
    return;
  }

  const endpoint = panel.dataset.analysisUrl;
  const loadingState = panel.querySelector("[data-analysis-loading]");
  const contentTarget = panel.querySelector("[data-analysis-content]");
  const statusCopy = panel.querySelector("[data-analysis-status-copy]");
  const retryButton = panel.querySelector("[data-analysis-retry]");
  const configuredPollInterval = Number(panel.dataset.analysisPollIntervalMs || "1000");
  const pollIntervalMs = Number.isFinite(configuredPollInterval) && configuredPollInterval > 0
    ? configuredPollInterval
    : 1000;
  const defaultPendingMessage =
    "Liczenie odbywa sie w tle. Widok pojawi sie automatycznie po zakonczeniu obliczen.";
  let pollTimer = null;
  let requestInFlight = false;

  if (!endpoint || !loadingState || !contentTarget || !statusCopy) {
    return;
  }

  const setStatusMessage = (message) => {
    statusCopy.textContent = message;
  };

  const scheduleNextPoll = (delayMs = pollIntervalMs) => {
    window.clearTimeout(pollTimer);
    pollTimer = window.setTimeout(loadAnalysis, delayMs);
  };

  const showLoading = (message) => {
    loadingState.hidden = false;
    contentTarget.hidden = true;
    setStatusMessage(message);
  };

  const showRetry = (message) => {
    showLoading(message);
    if (retryButton) {
      retryButton.hidden = false;
    }
  };

  const renderAnalysis = (html) => {
    window.clearTimeout(pollTimer);
    contentTarget.innerHTML = html;
    contentTarget.hidden = false;
    loadingState.hidden = true;
    if (retryButton) {
      retryButton.hidden = true;
    }
    initializeMetricCharts(contentTarget);
  };

  const loadAnalysis = async () => {
    if (requestInFlight) {
      return;
    }
    requestInFlight = true;

    try {
      const response = await fetch(endpoint, {
        cache: "no-store",
        headers: {
          Accept: "application/json",
        },
      });
      const payload = await response.json().catch(() => null);

      if (!payload) {
        throw new Error("Brak odpowiedzi JSON z endpointu analizy.");
      }

      if (payload.status === "pending") {
        if (retryButton) {
          retryButton.hidden = true;
        }
        showLoading(payload.message || defaultPendingMessage);
        scheduleNextPoll(payload.poll_interval_ms || pollIntervalMs);
        return;
      }

      if (typeof payload.html === "string") {
        renderAnalysis(payload.html);
        return;
      }

      throw new Error("Endpoint analizy nie zwrocil gotowego widoku.");
    } catch (_error) {
      showRetry("Nie udalo sie pobrac wyniku analizy. Mozesz sprobowac ponownie.");
    } finally {
      requestInFlight = false;
    }
  };

  retryButton?.addEventListener("click", () => {
    retryButton.hidden = true;
    showLoading(defaultPendingMessage);
    loadAnalysis();
  });

  showLoading(defaultPendingMessage);
  loadAnalysis();
};

document.addEventListener("DOMContentLoaded", () => {
  initializeMetricCharts(document);
  initializeAsyncAnalysis();
});
