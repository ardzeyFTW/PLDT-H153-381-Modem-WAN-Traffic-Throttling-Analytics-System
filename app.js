/**
 * PLDT H153-381 Modem WAN Traffic & Throttling Analyzer
 * Target Topology: Internet --> Modem 192.168.1.1 --> MR600 Bridge 192.168.0.1 --> PC
 * Data Source: Huawei internal modem API (CurrentDownloadRate / CurrentUploadRate)
 */

document.addEventListener('DOMContentLoaded', () => {
  // DOM Elements
  const systemStatusPill = document.getElementById('systemStatusPill');
  const liveStatusBadge = document.getElementById('liveStatusBadge');
  const headerTodayUsage = document.getElementById('headerTodayUsage');
  const headerDbRecords = document.getElementById('headerDbRecords');
  const currentDateBadge = document.getElementById('currentDateBadge');
  const clearLogsBtn = document.getElementById('clearLogsBtn');
  const terminalLog = document.getElementById('terminalLog');

  // Gauge & Meter Elements
  const dlSpeedVal = document.getElementById('dlSpeedVal');
  const dlAvgVal = document.getElementById('dlAvgVal');
  const dlPeakVal = document.getElementById('dlPeakVal');
  const dlMeterFill = document.getElementById('dlMeterFill');

  const ulSpeedVal = document.getElementById('ulSpeedVal');
  const ulAvgVal = document.getElementById('ulAvgVal');
  const ulPeakVal = document.getElementById('ulPeakVal');
  const ulMeterFill = document.getElementById('ulMeterFill');

  // Today Summary Tiles
  const todayDlVal = document.getElementById('todayDlVal');
  const todayDlBytesSub = document.getElementById('todayDlBytesSub');
  const todayUlVal = document.getElementById('todayUlVal');
  const todayUlBytesSub = document.getElementById('todayUlBytesSub');
  const lifetimeDlVal = document.getElementById('lifetimeDlVal');
  const midnightCountdown = document.getElementById('midnightCountdown');

  // Active Speed Probe & History Tables
  const nextTestCountdownBadge = document.getElementById('nextTestCountdownBadge');
  const runSpeedTestBtn = document.getElementById('runSpeedTestBtn');
  const speedProbeTableBody = document.getElementById('speedProbeTableBody');
  const dailyHistoryTableBody = document.getElementById('dailyHistoryTableBody');
  const exportCsvBtn = document.getElementById('exportCsvBtn');
  const exportDaysSelect = document.getElementById('exportDaysSelect');
  const resetDbBtn = document.getElementById('resetDbBtn');

  // Window Summary Elements
  const wDlVal = document.getElementById('wDlVal');
  const wUlVal = document.getElementById('wUlVal');
  const wPeakVal = document.getElementById('wPeakVal');
  const wAvgVal = document.getElementById('wAvgVal');

  // Canvas Element
  const canvas = document.getElementById('speedTimeChart');
  const ctx = canvas.getContext('2d');

  // Determine API Origin
  const apiBase = window.location.origin.startsWith('http') ? window.location.origin : 'http://localhost:8085';

  // State
  let isConnected = false;
  let activeRange = 'today';
  let historySamples = [];
  let dailyRecords = {};
  let peakDlSession = 0.0;
  let peakUlSession = 0.0;
  let ispThrottleCapMbps = 5.0;
  let nextTestTs = 0;
  let isRunningTest = false;

  // Formatting helpers
  const formatBytes = (bytes) => {
    if (!bytes || bytes === 0) return '0.00 MB';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
  };

  const formatBytesToGB = (bytes) => {
    if (!bytes || bytes === 0) return '0.00 GB';
    return (bytes / (1024 ** 3)).toFixed(2) + ' GB';
  };

  const formatNumberWithCommas = (x) => {
    return x.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  };

  const logMessage = (msg, type = 'info') => {
    const line = document.createElement('div');
    line.className = `log-line ${type}`;
    const timestamp = new Date().toLocaleTimeString();
    line.innerText = `[${timestamp}] ${msg}`;
    terminalLog.appendChild(line);
    terminalLog.scrollTop = terminalLog.scrollHeight;
  };

  // --- Countdown Timers (Midnight & Speed Test) ---
  const updateCountdowns = () => {
    // 1. Midnight countdown
    const now = new Date();
    const midnight = new Date(now);
    midnight.setHours(24, 0, 0, 0);
    const diffMs = midnight - now;
    const totalSecs = Math.max(0, Math.floor(diffMs / 1000));
    const hrs = String(Math.floor(totalSecs / 3600)).padStart(2, '0');
    const mins = String(Math.floor((totalSecs % 3600) / 60)).padStart(2, '0');
    const secs = String(totalSecs % 60).padStart(2, '0');

    midnightCountdown.innerText = `${hrs}:${mins}:${secs}`;
    currentDateBadge.innerText = now.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });

    // 2. Next speed test countdown
    if (nextTestCountdownBadge && nextTestTs > 0) {
      const nowSec = Math.floor(Date.now() / 1000);
      const targetSec = Math.floor(nextTestTs);
      const remSec = Math.max(0, targetSec - nowSec);
      const pMins = String(Math.floor(remSec / 60)).padStart(2, '0');
      const pSecs = String(Math.floor(remSec % 60)).padStart(2, '0');

      if (isRunningTest || remSec === 0) {
        nextTestCountdownBadge.innerText = 'Speed Test Running...';
        nextTestCountdownBadge.style.color = 'var(--amber-glow)';
      } else {
        nextTestCountdownBadge.innerText = `Next test in: ${pMins}:${pSecs}`;
        nextTestCountdownBadge.style.color = 'var(--cyan-glow)';
      }
    }
  };

  setInterval(updateCountdowns, 1000);
  updateCountdowns();

  // --- Canvas Mouse Hover & Drag-to-Zoom Interactivity State ---
  let hoverState = null;
  let customZoomRange = null; // { minTs, maxTs } or null
  let isDragging = false;
  let dragStartX = null;
  let dragCurrentX = null;

  const getTimeWindow = () => {
    if (customZoomRange) {
      return {
        minTs: customZoomRange.minTs,
        maxTs: customZoomRange.maxTs,
        timeSpan: Math.max(10, customZoomRange.maxTs - customZoomRange.minTs)
      };
    }

    const nowSec = Math.floor(Date.now() / 1000);
    let maxTs = historySamples.length ? Math.max(nowSec, historySamples[historySamples.length - 1].ts) : nowSec;
    let minTs = maxTs - 86400; // Default 24h

    const rangeSecondsMap = {
      '1min': 60,
      '5m': 300,
      '30m': 1800,
      '1h': 3600,
      '6h': 21600,
      '12h': 43200,
      '1d': 86400,
      '3d': 259200,
      '1w': 604800,
      '1m': 2592000,
    };

    if (activeRange === 'today') {
      const now = new Date();
      const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, 0, 0);
      minTs = Math.floor(midnight.getTime() / 1000);
    } else if (activeRange === 'lifetime') {
      minTs = historySamples.length ? historySamples[0].ts : (maxTs - 86400);
    } else if (rangeSecondsMap[activeRange]) {
      minTs = maxTs - rangeSecondsMap[activeRange];
    }

    if (historySamples.length && activeRange === 'lifetime') {
      minTs = historySamples[0].ts;
    }

    return { minTs, maxTs, timeSpan: Math.max(10, maxTs - minTs) };
  };

  canvas.addEventListener('mousedown', (e) => {
    const rect = canvas.getBoundingClientRect();
    const mouseX = e.clientX - rect.left;
    const paddingLeft = 54;
    const paddingRight = 58;
    const chartW = canvas.clientWidth - paddingLeft - paddingRight;

    if (historySamples.length > 0 && mouseX >= paddingLeft && mouseX <= paddingLeft + chartW) {
      isDragging = true;
      dragStartX = mouseX;
      dragCurrentX = mouseX;
    }
  });

  canvas.addEventListener('mousemove', (e) => {
    const rect = canvas.getBoundingClientRect();
    const mouseX = e.clientX - rect.left;
    const mouseY = e.clientY - rect.top;
    const paddingLeft = 54;
    const paddingRight = 58;
    const chartW = canvas.clientWidth - paddingLeft - paddingRight;

    if (isDragging) {
      dragCurrentX = Math.max(paddingLeft, Math.min(paddingLeft + chartW, mouseX));
      renderSpeedTimeChart();
      return;
    }

    if (historySamples.length > 0 && mouseX >= paddingLeft && mouseX <= paddingLeft + chartW) {
      const { minTs, maxTs } = getTimeWindow();
      const mouseRatio = (mouseX - paddingLeft) / chartW;
      const targetTs = minTs + mouseRatio * (maxTs - minTs);

      let closestIdx = 0;
      let minDiff = Infinity;
      for (let i = 0; i < historySamples.length; i++) {
        const diff = Math.abs(historySamples[i].ts - targetTs);
        if (diff < minDiff) {
          minDiff = diff;
          closestIdx = i;
        }
      }

      const closestSample = historySamples[closestIdx];
      const timeSpan = maxTs - minTs;
      const maxAllowedGap = (timeSpan <= 3600 || ['1min', '5m', '30m', '1h'].includes(activeRange)) ? 30 : 600;

      const isSampleOffline = (s) => s && (s.status_label === 'No Internet' || s.status_label === 'Connecting' || s.status_label === 'POLLER OFFLINE');

      let isOfflinePoint = false;

      // 1. If closest sample itself is explicitly offline
      if (isSampleOffline(closestSample)) {
        isOfflinePoint = true;
      }
      // 2. If targetTs is far before the first sample or far after the last sample
      else if (closestIdx === 0 && targetTs < historySamples[0].ts - maxAllowedGap) {
        isOfflinePoint = true;
      }
      else if (closestIdx === historySamples.length - 1 && targetTs > historySamples[historySamples.length - 1].ts + maxAllowedGap) {
        isOfflinePoint = true;
      }
      // 3. If targetTs falls between adjacent samples, check if that specific gap exceeds maxAllowedGap or if either neighbor is offline
      else {
        let prevSample = null;
        let nextSample = null;

        if (targetTs >= closestSample.ts) {
          prevSample = closestSample;
          nextSample = (closestIdx < historySamples.length - 1) ? historySamples[closestIdx + 1] : null;
        } else {
          prevSample = (closestIdx > 0) ? historySamples[closestIdx - 1] : null;
          nextSample = closestSample;
        }

        if (prevSample && nextSample) {
          const gap = nextSample.ts - prevSample.ts;
          if (gap > maxAllowedGap || isSampleOffline(prevSample) || isSampleOffline(nextSample)) {
            isOfflinePoint = true;
          }
        }
      }

      let sampleToUse;
      if (isOfflinePoint) {
        sampleToUse = {
          ts: Math.round(targetTs),
          dl_mbps: 0,
          ul_mbps: 0,
          today_dl_gb: closestSample ? (closestSample.today_dl_gb || 0) : 0,
          today_ul_gb: closestSample ? (closestSample.today_ul_gb || 0) : 0,
          status_label: 'No Internet',
          is_synthetic: true
        };
      } else {
        sampleToUse = closestSample;
      }

      hoverState = { mouseX, mouseY, sample: sampleToUse, targetTs };
    } else {
      hoverState = null;
    }
    renderSpeedTimeChart();
  });

  // --- Window Statistics Updater ---
  let currentServerStats = null;

  const updateWindowStats = (minTs, maxTs, samples, serverStats = null) => {
    if (!wDlVal || !wUlVal || !wPeakVal || !wAvgVal) return;

    const visibleSamples = (samples || []).filter(s => s.ts >= minTs && s.ts <= maxTs);

    if (visibleSamples.length > 0) {
      let sumDlBytes = 0;
      let sumUlBytes = 0;

      const hasDeltas = visibleSamples.some(s => s.dl_bytes_delta !== undefined && s.dl_bytes_delta !== null);
      if (hasDeltas) {
        sumDlBytes = visibleSamples.reduce((acc, s) => acc + (s.dl_bytes_delta || 0), 0);
        sumUlBytes = visibleSamples.reduce((acc, s) => acc + (s.ul_bytes_delta || 0), 0);
      } else if (visibleSamples.length >= 2) {
        const first = visibleSamples[0];
        const last = visibleSamples[visibleSamples.length - 1];
        if (last.lifetime_dl_gb !== undefined && first.lifetime_dl_gb !== undefined) {
          sumDlBytes = Math.max(0, (last.lifetime_dl_gb - first.lifetime_dl_gb) * (1024 ** 3));
          sumUlBytes = Math.max(0, (last.lifetime_ul_gb - first.lifetime_ul_gb) * (1024 ** 3));
        } else if (last.today_dl_gb !== undefined && first.today_dl_gb !== undefined) {
          sumDlBytes = Math.max(0, (last.today_dl_gb - first.today_dl_gb) * (1024 ** 3));
          sumUlBytes = Math.max(0, (last.today_ul_gb - first.today_ul_gb) * (1024 ** 3));
        }
      }

      if (sumDlBytes === 0 && serverStats && serverStats.window_dl_bytes !== undefined && !customZoomRange) {
        sumDlBytes = serverStats.window_dl_bytes;
        sumUlBytes = serverStats.window_ul_bytes;
      }

      const peakDl = Math.max(...visibleSamples.map(s => s.dl_mbps || 0));
      const sumDl = visibleSamples.reduce((acc, s) => acc + (s.dl_mbps || 0), 0);
      const avgDl = sumDl / visibleSamples.length;

      wDlVal.innerText = formatBytes(sumDlBytes);
      wUlVal.innerText = formatBytes(sumUlBytes);
      wPeakVal.innerText = `${peakDl.toFixed(2)} Mbps`;
      wAvgVal.innerText = `${avgDl.toFixed(2)} Mbps`;
    } else if (serverStats && !customZoomRange) {
      wDlVal.innerText = formatBytes(serverStats.window_dl_bytes || 0);
      wUlVal.innerText = formatBytes(serverStats.window_ul_bytes || 0);
      wPeakVal.innerText = `${(serverStats.peak_dl_mbps || 0).toFixed(2)} Mbps`;
      wAvgVal.innerText = `${(serverStats.avg_dl_mbps || 0).toFixed(2)} Mbps`;
    } else {
      wDlVal.innerText = '0.00 MB';
      wUlVal.innerText = '0.00 MB';
      wPeakVal.innerText = '0.00 Mbps';
      wAvgVal.innerText = '0.00 Mbps';
    }
  };

  const handleDragEnd = () => {
    if (isDragging && dragStartX !== null && dragCurrentX !== null) {
      const dragDist = Math.abs(dragCurrentX - dragStartX);
      if (dragDist > 10) {
        const paddingLeft = 54;
        const paddingRight = 58;
        const chartW = canvas.clientWidth - paddingLeft - paddingRight;

        const { minTs, maxTs } = getTimeWindow();
        const r1 = Math.max(0, Math.min(1, (Math.min(dragStartX, dragCurrentX) - paddingLeft) / chartW));
        const r2 = Math.max(0, Math.min(1, (Math.max(dragStartX, dragCurrentX) - paddingLeft) / chartW));

        let selMinTs = Math.round(minTs + r1 * (maxTs - minTs));
        let selMaxTs = Math.round(minTs + r2 * (maxTs - minTs));
        const MIN_ZOOM_SPAN_SEC = 30; // Minimum 30s zoom window to prevent excessive/unreadable zoom

        if (selMaxTs - selMinTs < MIN_ZOOM_SPAN_SEC) {
          const mid = Math.round((selMinTs + selMaxTs) / 2);
          selMinTs = Math.max(minTs, mid - 15);
          selMaxTs = Math.min(maxTs, selMinTs + MIN_ZOOM_SPAN_SEC);
          if (selMaxTs - selMinTs < MIN_ZOOM_SPAN_SEC) {
            selMinTs = Math.max(minTs, selMaxTs - MIN_ZOOM_SPAN_SEC);
          }
        }

        if (selMaxTs - selMinTs >= 10) {
          customZoomRange = { minTs: selMinTs, maxTs: selMaxTs };

          if (timeRangeSelect) {
            let customOpt = timeRangeSelect.querySelector('option[value="custom"]');
            if (!customOpt) {
              customOpt = document.createElement('option');
              customOpt.value = 'custom';
              customOpt.text = 'Custom Range (Zoomed)';
              timeRangeSelect.add(customOpt, 0);
            }
            timeRangeSelect.value = 'custom';
            activeRange = 'custom';
          }
          fetchLiveHistory();
        }
      }
    }
    isDragging = false;
    dragStartX = null;
    dragCurrentX = null;
    renderSpeedTimeChart();
  };

  canvas.addEventListener('mouseup', handleDragEnd);
  canvas.addEventListener('mouseleave', () => {
    handleDragEnd();
    hoverState = null;
    renderSpeedTimeChart();
  });

  canvas.addEventListener('dblclick', () => {
    customZoomRange = null;
    if (timeRangeSelect) {
      const customOpt = timeRangeSelect.querySelector('option[value="custom"]');
      if (customOpt) customOpt.remove();
      timeRangeSelect.value = 'today';
      activeRange = 'today';
    }
    fetchLiveHistory();
  });

  // --- Render Speed (Mbps) vs Time Canvas Chart ---
  const renderSpeedTimeChart = () => {
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;

    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }

    ctx.clearRect(0, 0, width, height);

    const paddingLeft   = 54;
    const paddingRight  = 58;
    const paddingBottom = 32;
    const chartW = width - paddingLeft - paddingRight;
    const chartH = height - paddingBottom;

    const { minTs, maxTs, timeSpan } = getTimeWindow();
    const toX = ts => paddingLeft + Math.min(chartW, Math.max(0, ((ts - minTs) / timeSpan) * chartW));

    // Synchronize window stats bar above chart
    updateWindowStats(minTs, maxTs, historySamples, currentServerStats);

    // --- Render Dynamic Status Background Tint Segments ---
    if (historySamples.length > 0) {
      const maxSliceGap = 30; // Max 30s forward per sample slice

      for (let i = 0; i < historySamples.length; i++) {
        const sample = historySamples[i];
        const x1 = toX(sample.ts);
        
        let nextTs = (i < historySamples.length - 1) ? historySamples[i + 1].ts : Math.min(maxTs, sample.ts + 15);
        if (nextTs - sample.ts > maxSliceGap) {
          nextTs = sample.ts + maxSliceGap;
        }

        const x2 = toX(nextTs);
        const sliceW = Math.max(1, x2 - x1);

        let tint = null; // Default to no background color for 'No Internet' or offline
        if (sample.status_label === 'Throttled' || sample.status_label === 'THROTTLED' || sample.is_throttled === 1) {
          tint = 'rgba(255, 0, 85, 0.12)';    // Subtle red tint for Throttled
        } else if (sample.status_label === 'Unthrottled' || sample.status_label === 'UNTHROTTLED' || !sample.status_label) {
          tint = 'rgba(0, 255, 136, 0.04)';   // Subtle green tint for Unthrottled
        }

        if (tint) {
          ctx.fillStyle = tint;
          ctx.fillRect(x1, 0, sliceW, chartH);
        }
      }
    }

    const maxSpeedSample = historySamples.length
      ? Math.max(...historySamples.map(s => Math.max(s.dl_mbps || 0, s.ul_mbps || 0)))
      : 0;
    const maxSpeed = Math.max(ispThrottleCapMbps * 1.3, maxSpeedSample * 1.15, 10.0);

    const maxGbSample = historySamples.length
      ? Math.max(...historySamples.map(s => (s.today_dl_gb || 0) + (s.today_ul_gb || 0)))
      : 1;
    const maxGb = Math.max(maxGbSample * 1.2, 0.5);

    // Grid lines & left Mbps axis
    const rows = 4;
    for (let i = 0; i <= rows; i++) {
      const y = (chartH / rows) * i;
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.05)';
      ctx.lineWidth = 1;
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.moveTo(paddingLeft, y);
      ctx.lineTo(paddingLeft + chartW, y);
      ctx.stroke();

      const val = (maxSpeed * (rows - i) / rows).toFixed(0);
      ctx.fillStyle = '#8b949e';
      ctx.font = '10px "JetBrains Mono", monospace';
      ctx.textAlign = 'right';
      ctx.fillText(`${val}M`, paddingLeft - 8, y + 4);
    }

    // Right GB axis
    for (let i = 0; i <= rows; i++) {
      const y = (chartH / rows) * i;
      const gbVal = (maxGb * (rows - i) / rows).toFixed(1);
      ctx.fillStyle = '#ffb703';
      ctx.font = '10px "JetBrains Mono", monospace';
      ctx.textAlign = 'left';
      ctx.fillText(`${gbVal}G`, paddingLeft + chartW + 8, y + 4);
    }

    // Bottom X-Axis Axis Line & Hourly Time Tick Labels
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.12)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(paddingLeft, chartH);
    ctx.lineTo(paddingLeft + chartW, chartH);
    ctx.stroke();

    const maxTicks = 6;
    for (let i = 0; i < maxTicks; i++) {
      const tickTs = minTs + (i / (maxTicks - 1)) * (maxTs - minTs);
      const x = toX(tickTs);
      const dateObj = new Date(tickTs * 1000);
      
      let timeStr;
      if (timeSpan <= 600) {
        // Include seconds when zoomed in (<= 10m) so tick labels don't repeat (e.g. 10:40:00 AM)
        timeStr = dateObj.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      } else if (['3d', '1w', '1m', 'lifetime'].includes(activeRange)) {
        timeStr = `${dateObj.getMonth() + 1}/${dateObj.getDate()} ${dateObj.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
      } else {
        timeStr = dateObj.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      }

      ctx.fillStyle = '#8b949e';
      ctx.font = '10px "JetBrains Mono", monospace';
      ctx.textAlign = i === 0 ? 'left' : (i === maxTicks - 1 ? 'right' : 'center');
      ctx.fillText(timeStr, x, chartH + 18);

      ctx.strokeStyle = 'rgba(255, 255, 255, 0.15)';
      ctx.beginPath();
      ctx.moveTo(x, chartH);
      ctx.lineTo(x, chartH + 4);
      ctx.stroke();
    }

    if (!historySamples.length) return;

    const maxAllowedGap = (timeSpan <= 3600 || ['1min', '5m', '30m', '1h'].includes(activeRange)) ? 30 : 600;

    // Daily GB Overlay Line (Amber)
    ctx.lineWidth = 2;
    ctx.strokeStyle = '#ffb703';
    ctx.setLineDash([4, 3]);

    let inSegment = false;
    for (let i = 0; i < historySamples.length; i++) {
      const sample = historySamples[i];
      const todayTotal = (sample.today_dl_gb || 0) + (sample.today_ul_gb || 0);
      const x = toX(sample.ts);
      const y = chartH - Math.min(chartH, (todayTotal / maxGb) * chartH);

      const prevSample = i > 0 ? historySamples[i - 1] : null;
      if (prevSample && (sample.date !== prevSample.date || sample.ts - prevSample.ts > maxAllowedGap)) {
        ctx.stroke();
        inSegment = false;
      }

      if (!inSegment) {
        ctx.beginPath();
        ctx.moveTo(x, y);
        inSegment = true;
      } else {
        ctx.lineTo(x, y);
      }
    }
    if (inSegment) ctx.stroke();

    // Upload Speed Line (Violet) — With Gap / Cut for No Internet & missing time spans
    ctx.setLineDash([]);
    ctx.lineWidth = 2;
    ctx.strokeStyle = '#9d4edd';
    let inPathUl = false;
    let lastTsUl = 0;

    historySamples.forEach((sample) => {
      const isOffline = (sample.status_label === 'No Internet' || sample.status_label === 'Connecting' || sample.status_label === 'POLLER OFFLINE');
      const gapExceeded = lastTsUl > 0 && (sample.ts - lastTsUl > maxAllowedGap);

      if (isOffline) {
        if (inPathUl) {
          ctx.stroke();
          inPathUl = false;
        }
        lastTsUl = sample.ts;
        return;
      }

      if (gapExceeded) {
        if (inPathUl) {
          ctx.stroke();
          inPathUl = false;
        }
      }

      const x = toX(sample.ts);
      const y = chartH - Math.min(chartH, ((sample.ul_mbps || 0) / maxSpeed) * chartH);
      if (!inPathUl) {
        ctx.beginPath();
        ctx.moveTo(x, y);
        inPathUl = true;
      } else {
        ctx.lineTo(x, y);
      }
      lastTsUl = sample.ts;
    });
    if (inPathUl) ctx.stroke();

    // Download Speed Line (Cyan) — With Gap / Cut for No Internet & missing time spans
    ctx.lineWidth = 2.5;
    ctx.strokeStyle = '#00f0ff';
    let inPathDl = false;
    let lastTsDl = 0;

    historySamples.forEach((sample) => {
      const isOffline = (sample.status_label === 'No Internet' || sample.status_label === 'Connecting' || sample.status_label === 'POLLER OFFLINE');
      const gapExceeded = lastTsDl > 0 && (sample.ts - lastTsDl > maxAllowedGap);

      if (isOffline) {
        if (inPathDl) {
          ctx.stroke();
          inPathDl = false;
        }
        lastTsDl = sample.ts;
        return;
      }

      if (gapExceeded) {
        if (inPathDl) {
          ctx.stroke();
          inPathDl = false;
        }
      }

      const x = toX(sample.ts);
      const y = chartH - Math.min(chartH, ((sample.dl_mbps || 0) / maxSpeed) * chartH);
      if (!inPathDl) {
        ctx.beginPath();
        ctx.moveTo(x, y);
        inPathDl = true;
      } else {
        ctx.lineTo(x, y);
      }
      lastTsDl = sample.ts;
    });
    if (inPathDl) ctx.stroke();

    // Render Sample Dots when zoomed in tight (timeSpan <= 300s / 5 minutes)
    if (timeSpan <= 300) {
      historySamples.forEach((sample) => {
        if (sample.ts < minTs || sample.ts > maxTs) return;
        const isOffline = (sample.status_label === 'No Internet' || sample.status_label === 'Connecting' || sample.status_label === 'POLLER OFFLINE');
        if (isOffline) return;

        const x = toX(sample.ts);
        const dlY = chartH - Math.min(chartH, ((sample.dl_mbps || 0) / maxSpeed) * chartH);
        const ulY = chartH - Math.min(chartH, ((sample.ul_mbps || 0) / maxSpeed) * chartH);

        // Download dot (Cyan)
        ctx.fillStyle = '#00f0ff';
        ctx.beginPath();
        ctx.arc(x, dlY, 3, 0, Math.PI * 2);
        ctx.fill();

        // Upload dot (Violet)
        ctx.fillStyle = '#9d4edd';
        ctx.beginPath();
        ctx.arc(x, ulY, 2.5, 0, Math.PI * 2);
        ctx.fill();
      });
    }
    // Render Drag Selection Overlay Box (Grafana style)
    if (isDragging && dragStartX !== null && dragCurrentX !== null) {
      const selX1 = Math.min(dragStartX, dragCurrentX);
      const selX2 = Math.max(dragStartX, dragCurrentX);
      const selW = selX2 - selX1;

      ctx.fillStyle = 'rgba(0, 240, 255, 0.18)';
      ctx.fillRect(selX1, 0, selW, chartH);

      ctx.strokeStyle = '#00f0ff';
      ctx.lineWidth = 1.5;
      ctx.setLineDash([4, 2]);
      ctx.strokeRect(selX1, 0, selW, chartH);
      ctx.setLineDash([]);
    }

    // Render Hover Crosshair & Floating Tooltip Card if hovering
    if (hoverState) {
      const sample = hoverState.sample;
      const hX = hoverState.mouseX; // Follow mouse cursor smoothly
      const isOffline = (sample.status_label === 'No Internet' || sample.status_label === 'Connecting' || sample.status_label === 'POLLER OFFLINE' || sample.is_synthetic);

      // Vertical Crosshair Line
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.35)';
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(hX, 0);
      ctx.lineTo(hX, chartH);
      ctx.stroke();
      ctx.setLineDash([]);

      // Point Highlights — only draw speed dots if online
      if (!isOffline) {
        const dlY = chartH - Math.min(chartH, ((sample.dl_mbps || 0) / maxSpeed) * chartH);
        const ulY = chartH - Math.min(chartH, ((sample.ul_mbps || 0) / maxSpeed) * chartH);
        const todayTotal = (sample.today_dl_gb || 0) + (sample.today_ul_gb || 0);
        const gbY = chartH - Math.min(chartH, (todayTotal / maxGb) * chartH);

        // Download Dot (Cyan)
        ctx.fillStyle = '#00f0ff';
        ctx.beginPath();
        ctx.arc(hX, dlY, 5, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = '#ffffff';
        ctx.lineWidth = 1.5;
        ctx.stroke();

        // Upload Dot (Violet)
        ctx.fillStyle = '#9d4edd';
        ctx.beginPath();
        ctx.arc(hX, ulY, 4, 0, Math.PI * 2);
        ctx.fill();

        // Today GB Dot (Amber)
        ctx.fillStyle = '#ffb703';
        ctx.beginPath();
        ctx.arc(hX, gbY, 4, 0, Math.PI * 2);
        ctx.fill();
      }

      // Render Floating Tooltip Box
      const dateObj = new Date((hoverState.targetTs || sample.ts) * 1000);
      const timeLabel = dateObj.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      const dateLabel = dateObj.toLocaleDateString([], { month: 'short', day: 'numeric' });
      const todayTotal = (sample.today_dl_gb || 0) + (sample.today_ul_gb || 0);

      const ttLines = [
        `${dateLabel} ${timeLabel}`,
        `DL: ${(sample.dl_mbps || 0).toFixed(2)} Mbps`,
        `UL: ${(sample.ul_mbps || 0).toFixed(2)} Mbps`,
        `Today: ${todayTotal.toFixed(2)} GB`,
        `Status: ${isOffline ? 'No Internet' : (sample.status_label || 'Unthrottled')}`
      ];

      ctx.font = '11px "JetBrains Mono", monospace';
      let maxTxtW = 0;
      ttLines.forEach(l => {
        const w = ctx.measureText(l).width;
        if (w > maxTxtW) maxTxtW = w;
      });

      const boxW = maxTxtW + 20;
      const boxH = ttLines.length * 16 + 12;
      let boxX = hX + 12;
      if (boxX + boxW > width - paddingRight) boxX = hX - boxW - 12;
      let boxY = Math.min(chartH - boxH, Math.max(10, hoverState.mouseY - 20));

      // Background Box
      ctx.fillStyle = 'rgba(10, 12, 18, 0.94)';
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.2)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      if (ctx.roundRect) {
        ctx.roundRect(boxX, boxY, boxW, boxH, 8);
      } else {
        ctx.rect(boxX, boxY, boxW, boxH);
      }
      ctx.fill();
      ctx.stroke();

      // Text Lines
      let curY = boxY + 18;
      ctx.textAlign = 'left';
      
      // Line 0: Timestamp Header
      ctx.fillStyle = '#ffffff';
      ctx.font = 'bold 11px "JetBrains Mono", monospace';
      ctx.fillText(ttLines[0], boxX + 10, curY);

      // Line 1: DL Speed (Cyan or Muted)
      curY += 16;
      ctx.fillStyle = isOffline ? '#8b949e' : '#00f0ff';
      ctx.font = '11px "JetBrains Mono", monospace';
      ctx.fillText(ttLines[1], boxX + 10, curY);

      // Line 2: UL Speed (Violet or Muted)
      curY += 16;
      ctx.fillStyle = isOffline ? '#8b949e' : '#9d4edd';
      ctx.fillText(ttLines[2], boxX + 10, curY);

      // Line 3: Today Data (Amber)
      curY += 16;
      ctx.fillStyle = '#ffb703';
      ctx.fillText(ttLines[3], boxX + 10, curY);

      // Line 4: Throttling Status (Red / Green / Muted)
      curY += 16;
      let stColor = '#00ff88';
      if (isOffline) {
        stColor = '#8b949e';
      } else if (sample.status_label === 'Throttled' || sample.status_label === 'THROTTLED' || sample.is_throttled === 1) {
        stColor = '#ff0055';
      }
      ctx.fillStyle = stColor;
      ctx.fillText(ttLines[4], boxX + 10, curY);
    }
  };

  // --- Render Daily Usage History Table ---
  const renderDailyTable = (dailyMap) => {
    dailyHistoryTableBody.innerHTML = '';
    const dates = Object.keys(dailyMap).sort().reverse();

    if (dates.length === 0) {
      dailyHistoryTableBody.innerHTML = '<tr><td colspan="5" class="loading-td">No historical daily logs recorded yet.</td></tr>';
      return;
    }

    dates.forEach(dateStr => {
      const entry = dailyMap[dateStr];
      const dlGB = (entry.download_bytes / (1024 ** 3)).toFixed(2);
      const ulGB = (entry.upload_bytes / (1024 ** 3)).toFixed(2);
      const activeCount = entry.active_sample_count || 0;
      const avgLabel = activeCount > 0 ? `${entry.avg_dl_mbps.toFixed(2)} Mbps` : '&mdash;';

      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><strong>${dateStr}</strong></td>
        <td><span class="highlight-cyan">${dlGB} GB</span></td>
        <td><span class="highlight-violet">${ulGB} GB</span></td>
        <td>${entry.peak_dl_mbps.toFixed(2)} Mbps</td>
        <td>${avgLabel} <span style="opacity:0.4;font-size:0.72em">(active)</span></td>
      `;
      dailyHistoryTableBody.appendChild(tr);
    });
  };

  // --- Fetch & Render Active Speed Probe Logs with Pagination ---
  let allProbeLogs = [];
  let currentProbePage = 1;
  const probePageSize = 5;

  const probePageInfo = document.getElementById('probePageInfo');
  const probePrevBtn = document.getElementById('probePrevBtn');
  const probeNextBtn = document.getElementById('probeNextBtn');

  const renderProbeTablePage = () => {
    if (!speedProbeTableBody) return;

    if (allProbeLogs.length === 0) {
      speedProbeTableBody.innerHTML = '<tr><td colspan="7" class="loading-td">No diagnostic logs or active stress tests recorded yet.</td></tr>';
      if (probePageInfo) probePageInfo.innerText = 'Showing 0 of 0 logs';
      if (probePrevBtn) probePrevBtn.disabled = true;
      if (probeNextBtn) probeNextBtn.disabled = true;
      return;
    }

    const totalPages = Math.ceil(allProbeLogs.length / probePageSize);
    if (currentProbePage > totalPages) currentProbePage = totalPages;
    if (currentProbePage < 1) currentProbePage = 1;

    const startIdx = (currentProbePage - 1) * probePageSize;
    const endIdx = Math.min(startIdx + probePageSize, allProbeLogs.length);
    const pageItems = allProbeLogs.slice(startIdx, endIdx);

    if (probePageInfo) {
      probePageInfo.innerText = `Showing ${startIdx + 1}–${endIdx} of ${allProbeLogs.length} logs (Page ${currentProbePage} of ${totalPages})`;
    }

    if (probePrevBtn) {
      probePrevBtn.disabled = (currentProbePage === 1);
      probePrevBtn.style.opacity = (currentProbePage === 1) ? '0.4' : '1';
    }
    if (probeNextBtn) {
      probeNextBtn.disabled = (currentProbePage === totalPages);
      probeNextBtn.style.opacity = (currentProbePage === totalPages) ? '0.4' : '1';
    }

    speedProbeTableBody.innerHTML = '';
    pageItems.forEach(p => {
      const dtStr = new Date(p.ts * 1000).toLocaleString([], {
        month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit'
      });
      
      let stBadge = '<span class="status-pill active" style="font-size:0.75em"><span class="dot"></span> UNTHROTTLED</span>';
      if (p.is_throttled === 1) {
        stBadge = '<span class="status-pill disconnected" style="font-size:0.75em"><span class="dot" style="background:#ff0055"></span> THROTTLED</span>';
      } else if (p.is_throttled === 2 || (p.status_desc && (p.status_desc.includes('ERROR') || p.status_desc.includes('LOCKOUT')))) {
        stBadge = '<span class="status-pill disconnected" style="font-size:0.75em;background:rgba(255,183,3,0.15);color:#ffb703;border-color:rgba(255,183,3,0.3)"><span class="dot" style="background:#ffb703"></span> AUTH ERROR</span>';
      }

      const todayDlUl = `${(p.today_dl_gb || 0).toFixed(2)} GB / ${(p.today_ul_gb || 0).toFixed(2)} GB`;
      const lifeDlUl  = `${(p.lifetime_dl_gb || 0).toFixed(2)} GB / ${(p.lifetime_ul_gb || 0).toFixed(2)} GB`;
      const providerLabel = p.provider || '—';

      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><strong>${dtStr}</strong></td>
        <td><strong style="color:${p.dl_mbps > 0 ? 'var(--cyan-glow)' : 'var(--text-muted)'};font-size:1.05em">${(p.dl_mbps || 0).toFixed(2)} Mbps</strong></td>
        <td style="font-size:0.82em;color:var(--text-muted)">${providerLabel}</td>
        <td>${todayDlUl}</td>
        <td>${lifeDlUl}</td>
        <td>${stBadge}</td>
        <td style="font-size:0.85em;opacity:0.85">${p.status_desc || ''}</td>
      `;
      speedProbeTableBody.appendChild(tr);
    });
  };

  if (probePrevBtn) {
    probePrevBtn.addEventListener('click', () => {
      if (currentProbePage > 1) {
        currentProbePage--;
        renderProbeTablePage();
      }
    });
  }

  if (probeNextBtn) {
    probeNextBtn.addEventListener('click', () => {
      const totalPages = Math.ceil(allProbeLogs.length / probePageSize);
      if (currentProbePage < totalPages) {
        currentProbePage++;
        renderProbeTablePage();
      }
    });
  }

  const fetchSpeedProbes = async () => {
    try {
      const resp = await fetch(`${apiBase}/api/speed_probes`);
      if (resp.ok) {
        const data = await resp.json();
        allProbeLogs = data.probes || [];
        renderProbeTablePage();
      }
    } catch (err) {
      // Handled silently
    }
  };

  // --- Run Speed Test On-Demand Handler ---
  let speedTestPollTimer = null;

  const showSpeedTestRunning = () => {
    isRunningTest = true;
    if (runSpeedTestBtn) {
      // Don't disable - just show running state visually
      runSpeedTestBtn.innerHTML = ' Test Running...';
      runSpeedTestBtn.style.opacity = '0.7';
      runSpeedTestBtn.style.cursor = 'not-allowed';
    }
    if (nextTestCountdownBadge) {
      nextTestCountdownBadge.innerText = 'Speed Test Running...';
      nextTestCountdownBadge.style.color = 'var(--amber-glow)';
    }
  };

  const hideSpeedTestRunning = () => {
    isRunningTest = false;
    if (runSpeedTestBtn) {
      runSpeedTestBtn.innerHTML = 'Run Speed Test Now';
      runSpeedTestBtn.style.opacity = '';
      runSpeedTestBtn.style.cursor = '';
    }
  };

  const pollForProbeCompletion = () => {
    // Poll /api/traffic every 1s to detect when probe_running becomes false
    let polls = 0;
    let dotCount = 0;
    speedTestPollTimer = setInterval(async () => {
      polls++;
      dotCount = (dotCount + 1) % 4;
      const dots = '.'.repeat(dotCount + 1);
      // Keep button text animated while running
      if (runSpeedTestBtn) {
        runSpeedTestBtn.innerHTML = ` Test Running${dots}`;
      }
      try {
        const r = await fetch(`${apiBase}/api/traffic`);
        if (r.ok) {
          const d = await r.json();
          const stillRunning = d.probe_running === true;
          // Minimum 3 polls grace period before trusting probe_running=false
          // (avoids false-stop due to thread startup race)
          if ((!stillRunning && polls >= 3) || polls > 60) {
            clearInterval(speedTestPollTimer);
            speedTestPollTimer = null;
            hideSpeedTestRunning();
            await fetchSpeedProbes();
            await fetchLiveMetrics();
            logMessage(`[SPEED TEST] Probe complete! Check Speed Tests table for results.`, 'success');
          }
        }
      } catch (e) {
        if (polls > 60) {
          clearInterval(speedTestPollTimer);
          speedTestPollTimer = null;
          hideSpeedTestRunning();
        }
      }
    }, 1000);
  };

  if (runSpeedTestBtn) {
    runSpeedTestBtn.addEventListener('click', async () => {
      if (isRunningTest) {
        logMessage('[SPEED TEST] Test is already running — please wait.', 'warn');
        return;
      }
      showSpeedTestRunning();
      logMessage('[SPEED TEST] Launching active sequential speed test probe (trying providers in order)...', 'warn');
      try {
        const resp = await fetch(`${apiBase}/api/run_speed_test`);
        if (resp.ok) {
          const res = await resp.json();
          if (res.status === 'running') {
            logMessage('[SPEED TEST] A probe is already running in the background.', 'info');
            pollForProbeCompletion();
          } else {
            logMessage(`[SPEED TEST] ${res.message}`, 'info');
            pollForProbeCompletion();
          }
        } else {
          logMessage(`[SPEED TEST] Server error ${resp.status}.`, 'error');
          hideSpeedTestRunning();
        }
      } catch (err) {
        logMessage(`[SPEED TEST ERROR] ${err.message}`, 'error');
        hideSpeedTestRunning();
      }
    });
  }

  // --- CSV Export Handler ---
  if (exportCsvBtn) {
    exportCsvBtn.addEventListener('click', () => {
      const days = exportDaysSelect ? exportDaysSelect.value : 30;
      const downloadUrl = `${apiBase}/api/export/csv?days=${days}`;
      logMessage(`[EXPORT] Requesting CSV traffic export for last ${days === '0' ? 'all' : days} days...`, 'success');
      window.open(downloadUrl, '_blank');
    });
  }

  // --- Reset Database Handler ---
  if (resetDbBtn) {
    resetDbBtn.addEventListener('click', async () => {
      const userInput = prompt('Type "RESET DATABASE" to confirm wiping all historical traffic records:');
      if (userInput === 'RESET DATABASE') {
        logMessage('[DB RESET] Confirmed. Clearing SQLite database tables...', 'warn');
        try {
          const resp = await fetch(`${apiBase}/api/reset_db`);
          if (resp.ok) {
            logMessage('[DB RESET SUCCESS] Database reset complete.', 'success');
            fetchLiveMetrics();
            fetchLiveHistory();
            fetchSpeedProbes();
          }
        } catch (err) {
          logMessage(`[DB RESET ERROR] ${err.message}`, 'error');
        }
      } else if (userInput !== null) {
        logMessage('[DB RESET CANCELLED] Incorrect confirmation text. Database was not reset.', 'warn');
      }
    });
  }

  // --- Poll Live Traffic APIs from snmp_poller.py ---
  const fetchLiveMetrics = async () => {
    try {
      const resp = await fetch(`${apiBase}/api/traffic`);
      if (resp.ok) {
        const data = await resp.json();

        if (headerDbRecords) {
          const nRecs = data.db_total_samples || 0;
          headerDbRecords.innerText = nRecs > 0 ? formatNumberWithCommas(nRecs) : '—';
        }

        if (data.next_test_ts) {
          nextTestTs = data.next_test_ts;
        }

        // System Status Badge logic
        const sysStatus = data.status || 'Unthrottled';
        if (sysStatus === 'Throttled') {
          systemStatusPill.className = 'status-pill disconnected';
          systemStatusPill.innerHTML = '<span class="dot" style="background:#ff0055"></span> THROTTLED';
        } else if (sysStatus === 'No Internet') {
          systemStatusPill.className = 'status-pill warn';
          systemStatusPill.innerHTML = '<span class="dot" style="background:#ffb703"></span> NO INTERNET';
        } else {
          systemStatusPill.className = 'status-pill active';
          systemStatusPill.innerHTML = '<span class="dot"></span> UNTHROTTLED';
        }

        if (data.modem_status === 'online') {
          if (!isConnected) {
            logMessage(`[MODEM API] Connected to PLDT H153-381 — All-Device WAN Traffic (${data.network_type || 'LTE/5G'})`, 'success');
            isConnected = true;
          }
          const sig = data.signal_icon || '0';
          const net = data.network_type || '';
          liveStatusBadge.innerText = `Modem API — WAN Traffic (${net}, Signal ${sig}/5)`;
        } else {
          liveStatusBadge.innerText = 'Reconnecting to modem...';
        }

        const dl = data.dl_mbps || 0.0;
        const ul = data.ul_mbps || 0.0;

        if (dl > peakDlSession) peakDlSession = dl;
        if (ul > peakUlSession) peakUlSession = ul;

        dlSpeedVal.innerText = dl.toFixed(2);
        dlPeakVal.innerText = `${peakDlSession.toFixed(2)} Mbps`;
        const dlMeterPercent = Math.min(100, (dl / Math.max(100.0, peakDlSession * 1.1)) * 100);
        dlMeterFill.style.width = `${dlMeterPercent}%`;

        ulSpeedVal.innerText = ul.toFixed(2);
        ulPeakVal.innerText = `${peakUlSession.toFixed(2)} Mbps`;
        const ulMeterPercent = Math.min(100, (ul / Math.max(30.0, peakUlSession * 1.1)) * 100);
        ulMeterFill.style.width = `${ulMeterPercent}%`;

        const todayDlBytes = data.today_download_bytes || 0;
        const todayUlBytes = data.today_upload_bytes || 0;
        const lifeDlBytes  = data.lifetime_download_bytes || 0;
        const lifeUlBytes  = data.lifetime_upload_bytes || 0;

        todayDlVal.innerText = formatBytesToGB(todayDlBytes);
        todayDlBytesSub.innerText = `${formatNumberWithCommas(todayDlBytes)} Bytes`;

        todayUlVal.innerText = formatBytesToGB(todayUlBytes);
        todayUlBytesSub.innerText = `${formatNumberWithCommas(todayUlBytes)} Bytes`;

        if (lifetimeDlVal) {
          lifetimeDlVal.innerText = `${formatBytesToGB(lifeDlBytes)} DL / ${formatBytesToGB(lifeUlBytes)} UL`;
        }

        const todayTotalGB = ((todayDlBytes + todayUlBytes) / (1024 ** 3)).toFixed(2);
        headerTodayUsage.innerText = `${todayTotalGB} GB`;

      } else {
        throw new Error('API server returned error');
      }
    } catch (err) {
      if (isConnected) {
        logMessage(`[TRAFFIC DISCONNECTED] Waiting for backend poller on ${apiBase}...`, 'error');
        isConnected = false;
      }
      systemStatusPill.className = 'status-pill warn';
      systemStatusPill.innerHTML = '<span class="dot" style="background:#ffb703"></span> POLLER OFFLINE';
      liveStatusBadge.innerText = 'Poller Offline';
    }
  };

  const fetchLiveHistory = async () => {
    try {
      let historyUrl = `${apiBase}/api/history?range=${activeRange}`;
      if (customZoomRange) {
        historyUrl = `${apiBase}/api/history?range=custom&min_ts=${customZoomRange.minTs}&max_ts=${customZoomRange.maxTs}`;
      }

      const resp = await fetch(historyUrl);
      if (resp.ok) {
        const data = await resp.json();
        historySamples = data.samples || [];
        dailyRecords = data.daily || {};
        ispThrottleCapMbps = data.throttle_cap_mbps || 5.0;
        currentServerStats = data.stats || null;

        if (historySamples.length > 0) {
          const sumDl = historySamples.reduce((acc, s) => acc + s.dl_mbps, 0);
          const sumUl = historySamples.reduce((acc, s) => acc + s.ul_mbps, 0);
          dlAvgVal.innerText = `${(sumDl / historySamples.length).toFixed(2)} Mbps`;
          ulAvgVal.innerText = `${(sumUl / historySamples.length).toFixed(2)} Mbps`;
        }

        renderSpeedTimeChart();
        renderDailyTable(dailyRecords);
      }
    } catch (err) {
      // Handled in traffic poller loop
    }
  };

  // Time Range Dropdown
  const timeRangeSelect = document.getElementById('timeRangeSelect');
  if (timeRangeSelect) {
    timeRangeSelect.addEventListener('change', (e) => {
      if (e.target.value !== 'custom') {
        customZoomRange = null;
        const customOpt = timeRangeSelect.querySelector('option[value="custom"]');
        if (customOpt) customOpt.remove();
      }
      activeRange = e.target.value;
      fetchLiveHistory();
    });
  }

  // Event Listeners
  clearLogsBtn.addEventListener('click', () => {
    terminalLog.innerHTML = '';
    logMessage(`[LOGS] Diagnostics log cleared.`, 'system');
  });

  // Polling intervals
  setInterval(fetchLiveMetrics, 2000);
  setInterval(fetchLiveHistory, 4000);
  setInterval(fetchSpeedProbes, 5000);

  // Initial calls
  fetchLiveMetrics();
  fetchLiveHistory();
  fetchSpeedProbes();
});
