/* ==========================================================================
   AAE4203 GNSS Benchmark Challenge - shared front-end helpers
   ========================================================================== */
'use strict';

const BENCH = (() => {

  async function api(path, options) {
    const response = await fetch(path, Object.assign({ cache: 'no-store' }, options || {}));
    if (!response.ok) {
      let detail = response.statusText;
      try {
        const body = await response.json();
        detail = body.error || detail;
      } catch (err) { /* ignore */ }
      throw new Error(`${response.status}: ${detail}`);
    }
    return response.json();
  }

  /** Subscribe to the server's SSE feed. Calls onEvent for every message. */
  function connect(onEvent, onStatus) {
    let source = null;
    let retry = 0;

    // ?nosse disables the live feed - used by the headless screenshot tests and
    // handy when a proxy or kiosk mode blocks long-lived connections.
    if (/[?&]nosse(=1|&|$)/.test(location.search)) {
      onStatus && onStatus(true);
      return () => {};
    }

    function open() {
      try {
        source = new EventSource('/api/events');
      } catch (err) {
        onStatus && onStatus(false);
        return;
      }
      source.onopen = () => { retry = 0; onStatus && onStatus(true); };
      source.onmessage = (event) => {
        let payload = null;
        try { payload = JSON.parse(event.data); } catch (err) { return; }
        onEvent(payload);
      };
      source.onerror = () => {
        onStatus && onStatus(false);
        if (source) source.close();
        retry = Math.min(retry + 1, 6);
        setTimeout(open, 800 * retry + 500);
      };
    }
    open();
    return () => { if (source) source.close(); };
  }

  function fmt(value, digits) {
    if (value === null || value === undefined || value === '' || Number.isNaN(value)) return '–';
    const d = digits === undefined ? 2 : digits;
    return Number(value).toFixed(d);
  }

  function esc(text) {
    return String(text === null || text === undefined ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function metricTone(value, goodMax, okMax) {
    if (value === null || value === undefined) return 'mute';
    if (value <= goodMax) return 'good';
    if (value <= okMax) return 'warn';
    return 'bad';
  }

  const CHART_FONT = 'Inter, "Segoe UI", system-ui, sans-serif';
  const CHART_INK = '#475467';
  const CHART_LINE = '#e4e7ec';

  function baseChart(extra) {
    return Object.assign({
      textStyle: { fontFamily: CHART_FONT, color: CHART_INK },
      grid: { left: 48, right: 18, top: 30, bottom: 42 },
      tooltip: {
        backgroundColor: '#101828', borderWidth: 0, textStyle: { color: '#fff', fontSize: 12 },
        padding: [7, 10],
      },
    }, extra || {});
  }

  const axisStyle = {
    axisLine: { lineStyle: { color: CHART_LINE } },
    axisTick: { show: false },
    axisLabel: { color: '#98a2b3', fontSize: 11 },
    splitLine: { lineStyle: { color: '#f2f4f7' } },
  };

  /** Mount a chart and keep it sized to its container. */
  function chart(element) {
    const instance = echarts.init(element, null, { renderer: 'canvas' });
    const observer = new ResizeObserver(() => instance.resize());
    observer.observe(element);
    return instance;
  }

  function debounce(fn, wait) {
    let timer = null;
    return function (...args) {
      clearTimeout(timer);
      timer = setTimeout(() => fn.apply(this, args), wait);
    };
  }

  return { api, connect, fmt, esc, metricTone, baseChart, axisStyle, chart,
           debounce, CHART_INK, CHART_LINE };
})();
