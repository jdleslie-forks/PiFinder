% include("header.tpl", title="Debug")

<style>
  .debug-container {
    margin-bottom: 20px;
  }
  .debug-card {
    background-color: #424242;
    border-radius: 4px;
    padding: 15px;
    margin-bottom: 15px;
  }
  .debug-image-container {
    background-color: #1e1e1e;
    padding: 15px;
    border-radius: 4px;
    text-align: center;
  }
  .debug-image {
    max-width: 100%;
    height: auto;
    border: 1px solid #666;
  }
  .chart-container {
    position: relative;
    height: 300px;
    background-color: #1e1e1e;
    padding: 15px;
    border-radius: 4px;
  }
  .metric-value {
    font-size: 1.5em;
    font-weight: bold;
    color: #4db6ac;
  }
  .metric-label {
    color: #9e9e9e;
    font-size: 0.9em;
  }
  .control-group {
    margin-bottom: 15px;
  }
  .refresh-controls {
    display: flex;
    gap: 10px;
    align-items: center;
    flex-wrap: wrap;
  }
  .refresh-controls select {
    height: 36px;
    margin: 0;
    padding: 0 10px;
    width: auto;
    min-width: 120px;
  }
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 15px;
  }
  .stat-box {
    background-color: #1e1e1e;
    padding: 15px;
    border-radius: 4px;
    text-align: center;
  }
</style>

<div class="row valign-wrapper" style="margin: 0px;">
  <div class="col s12">
    <h5 class="grey-text">Debug Dashboard</h5>
  </div>
</div>

<!-- Controls -->
<div class="card grey darken-2 debug-card">
  <div class="card-content">
    <div class="control-group">
      <div class="refresh-controls">
        <span class="grey-text">Auto-refresh:</span>
        <select id="refreshInterval" class="browser-default">
          <option value="0">Manual</option>
          <option value="5000">5 seconds</option>
          <option value="10000" selected>10 seconds</option>
          <option value="15000">15 seconds</option>
          <option value="30000">30 seconds</option>
        </select>
        <button id="manualRefresh" class="btn">
          <i class="material-icons left">refresh</i>Refresh Now
        </button>
        <span class="grey-text" id="lastUpdate">Last update: Never</span>
      </div>
    </div>
  </div>
</div>

<!-- System Metrics -->
<div class="card grey darken-2 debug-card">
  <div class="card-content">
    <span class="card-title grey-text">System Metrics</span>
    <div class="stats-grid">
      <div class="stat-box">
        <div class="metric-label">Total Memory</div>
        <div class="metric-value" id="totalMemory">--</div>
      </div>
      <div class="stat-box">
        <div class="metric-label">Available Memory</div>
        <div class="metric-value" id="availableMemory">--</div>
      </div>
      <div class="stat-box">
        <div class="metric-label">PiFinder Memory</div>
        <div class="metric-value" id="pifinderMemory">--</div>
      </div>
      <div class="stat-box">
        <div class="metric-label">GPU Memory</div>
        <div class="metric-value" id="gpuMemory">--</div>
      </div>
      <div class="stat-box">
        <div class="metric-label">Effective Free</div>
        <div class="metric-value" id="swapUsed">--</div>
      </div>
      <div class="stat-box">
        <div class="metric-label">ZRAM Ratio</div>
        <div class="metric-value" id="zramRatio">--</div>
      </div>
      <div class="stat-box">
        <div class="metric-label">Uptime</div>
        <div class="metric-value" id="uptime">--</div>
      </div>
      <div class="stat-box">
        <div class="metric-label">GPS Lock</div>
        <div class="metric-value" id="gpsLock">--</div>
      </div>
    </div>
  </div>
</div>

<!-- Last Solved Frame -->
<div class="card grey darken-2 debug-card">
  <div class="card-content">
    <span class="card-title grey-text">Last Solved Frame</span>
    <div class="row">
      <div class="col s12 m6">
        <div class="debug-image-container">
          <img id="solvedImage" class="debug-image" src="" alt="No solved frame available">
          <div class="grey-text" style="margin-top: 10px;">
            <span id="centroidCount">0 centroids detected</span>
          </div>
        </div>
      </div>
      <div class="col s12 m6">
        <div class="stat-box" style="margin-bottom: 10px;">
          <div class="metric-label">RA / Dec</div>
          <div class="metric-value" style="font-size: 1.2em;" id="solveRaDec">-- / --</div>
        </div>
        <div class="stat-box" style="margin-bottom: 10px;">
          <div class="metric-label">Alt / Az</div>
          <div class="metric-value" style="font-size: 1.2em;" id="solveAltAz">-- / --</div>
        </div>
        <div class="stat-box" style="margin-bottom: 10px;">
          <div class="metric-label">FOV</div>
          <div class="metric-value" style="font-size: 1.2em;" id="solveFOV">--°</div>
        </div>
        <div class="stat-box">
          <div class="metric-label">Constellation</div>
          <div class="metric-value" style="font-size: 1.2em;" id="solveConstellation">--</div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Solver Timing Charts -->
<div class="card grey darken-2 debug-card">
  <div class="card-content">
    <span class="card-title grey-text">Solver Timing (Last 10 Solves)</span>
    <div class="chart-container">
      <canvas id="timingChart"></canvas>
    </div>
  </div>
</div>

<!-- Memory Usage Chart -->
<div class="card grey darken-2 debug-card">
  <div class="card-content">
    <span class="card-title grey-text">Memory Usage History</span>
    <div class="chart-container">
      <canvas id="memoryChart"></canvas>
    </div>
  </div>
</div>

<!-- Chart.js CDN -->
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>

<script>
let refreshInterval = null;
let timingChart = null;
let memoryChart = null;
let memoryHistory = [];
const MAX_HISTORY = 20;

// Initialize charts
function initCharts() {
  const timingCtx = document.getElementById('timingChart').getContext('2d');
  timingChart = new Chart(timingCtx, {
    type: 'bar',
    data: {
      labels: [],
      datasets: [
        {
          label: 'Extract (ms)',
          data: [],
          backgroundColor: 'rgba(77, 182, 172, 0.8)',
          borderColor: 'rgba(77, 182, 172, 1)',
          borderWidth: 1
        },
        {
          label: 'Solve (ms)',
          data: [],
          backgroundColor: 'rgba(255, 193, 7, 0.8)',
          borderColor: 'rgba(255, 193, 7, 1)',
          borderWidth: 1
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: {
          stacked: true,
          grid: { color: 'rgba(255, 255, 255, 0.1)' },
          ticks: { color: '#9e9e9e' }
        },
        y: {
          stacked: true,
          beginAtZero: true,
          grid: { color: 'rgba(255, 255, 255, 0.1)' },
          ticks: { color: '#9e9e9e' }
        }
      },
      plugins: {
        legend: {
          labels: { color: '#9e9e9e' }
        },
        tooltip: {
          callbacks: {
            footer: function(items) {
              const index = items[0].dataIndex;
              const extract = timingChart.data.datasets[0].data[index] || 0;
              const solve = timingChart.data.datasets[1].data[index] || 0;
              return 'Total: ' + (extract + solve).toFixed(1) + ' ms';
            }
          }
        }
      }
    }
  });

  const memoryCtx = document.getElementById('memoryChart').getContext('2d');
  memoryChart = new Chart(memoryCtx, {
    type: 'line',
    data: {
      labels: [],
      datasets: [
        {
          label: 'PiFinder Memory',
          data: [],
          borderColor: 'rgba(77, 182, 172, 1)',
          backgroundColor: 'rgba(77, 182, 172, 0.7)',
          tension: 0.3,
          fill: true,
          stack: 'memory'
        },
        {
          label: 'GPU Memory',
          data: [],
          borderColor: 'rgba(156, 39, 176, 1)',
          backgroundColor: 'rgba(156, 39, 176, 0.7)',
          tension: 0.3,
          fill: true,
          stack: 'memory'
        },
        {
          label: 'OS + Other Processes',
          data: [],
          borderColor: 'rgba(255, 152, 0, 1)',
          backgroundColor: 'rgba(255, 152, 0, 0.7)',
          tension: 0.3,
          fill: true,
          stack: 'memory'
        },
        {
          label: 'Available',
          data: [],
          borderColor: 'rgba(76, 175, 80, 1)',
          backgroundColor: 'rgba(76, 175, 80, 0.7)',
          tension: 0.3,
          fill: true,
          stack: 'memory'
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: {
        mode: 'index'
      },
      scales: {
        x: {
          grid: { color: 'rgba(255, 255, 255, 0.1)' },
          ticks: { color: '#9e9e9e' }
        },
        y: {
          stacked: true,
          beginAtZero: true,
          grid: { color: 'rgba(255, 255, 255, 0.1)' },
          ticks: {
            color: '#9e9e9e',
            callback: function(value) {
              return value + ' MB';
            }
          }
        }
      },
      plugins: {
        legend: {
          labels: { color: '#9e9e9e' }
        },
        tooltip: {
          callbacks: {
            footer: function(items) {
              const index = items[0].dataIndex;
              const entry = memoryHistory[index];
              if (entry) {
                return [
                  'Total System Memory: ' + entry.total + ' MB',
                  'Used: ' + (entry.pifinder + entry.gpu + entry.other) + ' MB',
                  'Free: ' + entry.available + ' MB',
                  'Effective (w/zram): ' + (entry.effectiveFree || entry.available) + ' MB'
                ];
              }
              return '';
            }
          }
        }
      }
    }
  });
}

// Update all debug data
async function updateDebugData() {
  try {
    // Fetch metrics
    const metricsResponse = await fetch('/api/metrics');
    const metrics = await metricsResponse.json();

    // Update system metrics
    document.getElementById('totalMemory').textContent =
      (metrics.memory?.physical_mb || '--') + ' MB';
    document.getElementById('availableMemory').textContent =
      (metrics.memory?.available_mb || '--') + ' MB';
    document.getElementById('pifinderMemory').textContent =
      (metrics.memory?.pifinder_total_mb || '--') + ' MB';
    document.getElementById('gpuMemory').textContent =
      (metrics.memory?.gpu_mem_mb !== null && metrics.memory?.gpu_mem_mb !== undefined
        ? metrics.memory.gpu_mem_mb + ' MB'
        : 'Unknown');
    document.getElementById('swapUsed').textContent =
      (metrics.memory?.effective_free_mb || '--') + ' MB';
    document.getElementById('zramRatio').textContent =
      (metrics.memory?.zram?.ratio ? metrics.memory.zram.ratio + 'x' : 'N/A');

    // Format uptime
    const uptimeHours = Math.floor(metrics.uptime_seconds / 3600);
    const uptimeMinutes = Math.floor((metrics.uptime_seconds % 3600) / 60);
    document.getElementById('uptime').textContent =
      uptimeHours + 'h ' + uptimeMinutes + 'm';

    // GPS lock
    const gpsLock = metrics.gps?.locked ? 'Yes' : 'No';
    document.getElementById('gpsLock').textContent = gpsLock;
    document.getElementById('gpsLock').style.color = metrics.gps?.locked ? '#4db6ac' : '#ff6b6b';

    // Update memory history
    const timestamp = new Date().toLocaleTimeString();
    const totalMem = metrics.memory.physical_mb;
    const pifinderMem = metrics.memory.pifinder_total_mb;
    const gpuMem = metrics.memory.gpu_mem_mb || 0; // Dynamic from vcgencmd
    const availableMem = metrics.memory.available_mb;
    const effectiveFree = metrics.memory.effective_free_mb || availableMem;
    const otherMem = Math.max(0, totalMem - pifinderMem - gpuMem - availableMem);

    memoryHistory.push({
      timestamp: timestamp,
      pifinder: Math.round(pifinderMem),
      gpu: gpuMem,
      other: Math.round(otherMem),
      available: Math.round(availableMem),
      total: Math.round(totalMem),
      effectiveFree: Math.round(effectiveFree)
    });

    // Keep last MAX_HISTORY entries
    if (memoryHistory.length > MAX_HISTORY) {
      memoryHistory.shift();
    }

    // Update memory chart (stacked from bottom: PiFinder, GPU, Other, Available)
    memoryChart.data.labels = memoryHistory.map(h => h.timestamp);
    memoryChart.data.datasets[0].data = memoryHistory.map(h => h.pifinder);
    memoryChart.data.datasets[1].data = memoryHistory.map(h => h.gpu);
    memoryChart.data.datasets[2].data = memoryHistory.map(h => h.other);
    memoryChart.data.datasets[3].data = memoryHistory.map(h => h.available);
    memoryChart.update('none'); // No animation for smoother updates

    // Update timing chart from windowed stats
    if (metrics.solver?.timing_windows?.last_10) {
      const last10 = metrics.solver.timing_windows.last_10;
      const labels = [];
      const extractData = [];
      const solveData = [];

      // Get individual solve timings (reverse to show oldest to newest)
      if (last10.individual_solves) {
        last10.individual_solves.reverse().forEach((solve, index) => {
          labels.push(`#${index + 1}`);
          extractData.push(solve.extract_ms || 0);
          solveData.push(solve.solve_ms || 0);
        });
      }

      timingChart.data.labels = labels;
      timingChart.data.datasets[0].data = extractData;
      timingChart.data.datasets[1].data = solveData;
      timingChart.update('none');
    }

    // Fetch solved frame data
    const solvedResponse = await fetch('/api/solved_frame/data');
    const solvedData = await solvedResponse.json();

    if (!solvedData.error) {
      // Update solved frame image
      document.getElementById('solvedImage').src =
        '/api/solved_frame/image?t=' + new Date().getTime();

      // Update centroid count
      document.getElementById('centroidCount').textContent =
        (solvedData.num_centroids || 0) + ' centroids detected';

      // Update solution data
      const sol = solvedData.solution || {};
      document.getElementById('solveRaDec').textContent =
        (sol.RA ? sol.RA.toFixed(2) : '--') + ' / ' +
        (sol.Dec ? sol.Dec.toFixed(2) : '--');
      document.getElementById('solveAltAz').textContent =
        (sol.Alt ? sol.Alt.toFixed(1) : '--') + '° / ' +
        (sol.Az ? sol.Az.toFixed(1) : '--') + '°';
      document.getElementById('solveFOV').textContent =
        (sol.FOV ? sol.FOV.toFixed(2) : '--') + '°';
      document.getElementById('solveConstellation').textContent =
        sol.constellation || '--';
    }

    // Update last update time
    document.getElementById('lastUpdate').textContent =
      'Last update: ' + new Date().toLocaleTimeString();

  } catch (error) {
    console.error('Error fetching debug data:', error);
    document.getElementById('lastUpdate').textContent =
      'Last update: Error - ' + error.message;
  }
}

// Setup refresh interval
function setupRefreshInterval() {
  if (refreshInterval) {
    clearInterval(refreshInterval);
    refreshInterval = null;
  }

  const interval = parseInt(document.getElementById('refreshInterval').value);
  if (interval > 0) {
    refreshInterval = setInterval(updateDebugData, interval);
  }
}

// Event listeners
document.getElementById('refreshInterval').addEventListener('change', setupRefreshInterval);
document.getElementById('manualRefresh').addEventListener('click', updateDebugData);

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
  initCharts();
  updateDebugData();
  setupRefreshInterval();
});

// Cleanup on page unload
window.addEventListener('beforeunload', () => {
  if (refreshInterval) {
    clearInterval(refreshInterval);
  }
});
</script>

% include("footer.tpl")
