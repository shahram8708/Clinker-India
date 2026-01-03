(function () {
  const palette = {
    blue: '#60a5fa',
    teal: '#34d399',
    amber: '#f59e0b',
    red: '#f87171',
    purple: '#a78bfa',
    slate: '#94a3b8',
    emerald: '#10b981',
  };

  const defaultOptions = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: {
        labels: { color: '#e5e7eb' },
      },
      tooltip: {
        backgroundColor: '#0f172a',
        borderColor: 'rgba(255,255,255,0.08)',
        borderWidth: 1,
      },
    },
    scales: {
      x: {
        grid: { color: 'rgba(255,255,255,0.05)' },
        ticks: { color: '#cbd5e1', maxRotation: 45, minRotation: 0 },
      },
      y: {
        grid: { color: 'rgba(255,255,255,0.05)' },
        ticks: { color: '#cbd5e1' },
      },
    },
  };

  function safeValues(obj) {
    return Array.isArray(obj) ? obj : [];
  }

  function doughnutConfig(labels, data, colors) {
    return {
      type: 'doughnut',
      data: {
        labels,
        datasets: [
          {
            data,
            backgroundColor: colors,
            borderWidth: 0,
          },
        ],
      },
      options: {
        ...defaultOptions,
        cutout: '62%',
        plugins: {
          ...defaultOptions.plugins,
          legend: { display: false },
        },
      },
    };
  }

  function makeLineConfig(labels, data, label, color) {
    return {
      type: 'line',
      data: {
        labels,
        datasets: [
          {
            label,
            data,
            fill: true,
            backgroundColor: color + '33',
            borderColor: color,
            tension: 0.35,
            pointRadius: 0,
          },
        ],
      },
      options: {
        ...defaultOptions,
        plugins: {
          ...defaultOptions.plugins,
          legend: { display: false },
        },
      },
    };
  }

  function makeBarConfig(labels, datasets) {
    return {
      type: 'bar',
      data: { labels, datasets },
      options: {
        ...defaultOptions,
        plugins: {
          ...defaultOptions.plugins,
          legend: { display: datasets.length > 1 },
        },
      },
    };
  }

  window.renderAnalyticsDashboard = function renderAnalyticsDashboard(opts) {
    const analytics = (opts && opts.data) || {};

    const growth = analytics.users?.growth || { labels: [], values: [] };
    const userGrowthEl = document.getElementById('userGrowthChart');
    if (userGrowthEl) {
      new Chart(userGrowthEl.getContext('2d'), makeLineConfig(growth.labels, growth.values, 'New users', palette.blue));
    }

    const roles = analytics.users?.roles || {};
    const roleLabels = Object.keys(roles);
    const roleData = roleLabels.map((key) => roles[key]);
    const roleEl = document.getElementById('roleChart');
    if (roleEl) {
      new Chart(roleEl.getContext('2d'), doughnutConfig(roleLabels, roleData, [palette.blue, palette.teal, palette.amber]));
    }

    const inviteVelocity = analytics.invites?.velocity || { labels: [], values: [] };
    const inviteStatus = analytics.invites?.status || {};
    const inviteEl = document.getElementById('inviteChart');
    if (inviteEl) {
      const statusLabels = Object.keys(inviteStatus);
      const statusData = statusLabels.map((key) => inviteStatus[key]);
      new Chart(inviteEl.getContext('2d'), makeBarConfig(statusLabels, [
        {
          label: 'Invites',
          data: statusData,
          backgroundColor: palette.purple,
          borderRadius: 8,
        },
      ]));
    }

    const inventory = analytics.operations?.inventory || {};
    const inventoryEl = document.getElementById('inventoryChart');
    if (inventoryEl) {
      const labels = ['Safe', 'Alerts'];
      const data = [inventory.safe || 0, inventory.alerts || 0];
      new Chart(inventoryEl.getContext('2d'), makeBarConfig(labels, [
        { label: 'Count', data, backgroundColor: [palette.teal, palette.amber], borderRadius: 10 },
      ]));
    }

    const routes = analytics.operations?.routes || {};
    const routesEl = document.getElementById('routesChart');
    if (routesEl) {
      const modeLabels = Object.keys(routes.by_mode || {});
      const modeData = modeLabels.map((key) => routes.by_mode[key]);
      new Chart(routesEl.getContext('2d'), makeBarConfig(modeLabels, [
        { label: 'Routes', data: modeData, backgroundColor: palette.blue, borderRadius: 8 },
      ]));
    }

    const scenarios = analytics.operations?.scenarios || {};
    const scenarioEl = document.getElementById('scenarioChart');
    if (scenarioEl) {
      const labels = safeValues(scenarios.costs).map((c) => c.label);
      const data = safeValues(scenarios.costs).map((c) => c.value);
      new Chart(scenarioEl.getContext('2d'), makeLineConfig(labels, data, 'Result cost', palette.blue));
    }

    const runtime = analytics.operations?.optimization?.runtime || [];
    const runtimeEl = document.getElementById('runtimeChart');
    if (runtimeEl) {
      const labels = runtime.map((r) => r.label);
      const data = runtime.map((r) => r.runtime);
      new Chart(runtimeEl.getContext('2d'), makeBarConfig(labels, [
        { label: 'Seconds', data, backgroundColor: palette.emerald, borderRadius: 8 },
      ]));
    }

    const activity = analytics.activity || {};
    const activityEl = document.getElementById('activityChart');
    if (activityEl) {
      const labels = activity.trend?.labels || [];
      const data = activity.trend?.values || [];
      new Chart(activityEl.getContext('2d'), makeBarConfig(labels, [
        { label: 'Events', data, backgroundColor: palette.blue, borderRadius: 6 },
      ]));
    }

    const notes = analytics.notifications || {};
    const notesEl = document.getElementById('notificationsChart');
    if (notesEl) {
      const labels = Object.keys(notes);
      const data = labels.map((key) => notes[key]);
      new Chart(notesEl.getContext('2d'), doughnutConfig(labels, data, [palette.blue, palette.amber, palette.red, palette.teal]));
    }
  };

  window.renderSuperAdminAnalytics = function renderSuperAdminAnalytics(opts) {
    const analytics = (opts && opts.data) || {};

    function kv(obj) {
      const safe = obj && typeof obj === 'object' ? obj : {};
      const labels = Object.keys(safe);
      return { labels, values: labels.map((k) => safe[k]) };
    }

    const orgGrowth = analytics.orgs?.growth || { labels: [], values: [] };
    const orgGrowthEl = document.getElementById('platformOrgGrowthChart');
    if (orgGrowthEl) {
      new Chart(orgGrowthEl.getContext('2d'), makeLineConfig(orgGrowth.labels, orgGrowth.values, 'Organizations', palette.blue));
    }

    const orgStatus = kv(analytics.orgs?.status);
    const orgStatusEl = document.getElementById('platformOrgStatusChart');
    if (orgStatusEl) {
      new Chart(orgStatusEl.getContext('2d'), doughnutConfig(orgStatus.labels, orgStatus.values, [palette.blue, palette.red, palette.amber, palette.teal]));
    }

    const userGrowth = analytics.users?.growth || { labels: [], values: [] };
    const userGrowthEl = document.getElementById('platformUserGrowthChart');
    if (userGrowthEl) {
      new Chart(userGrowthEl.getContext('2d'), makeLineConfig(userGrowth.labels, userGrowth.values, 'Users', palette.teal));
    }

    const roleMix = kv(analytics.users?.roles);
    const roleEl = document.getElementById('platformRoleChart');
    if (roleEl) {
      new Chart(roleEl.getContext('2d'), doughnutConfig(roleMix.labels, roleMix.values, [palette.blue, palette.teal, palette.amber, palette.purple]));
    }

    const revenueTrend = analytics.billing?.revenue_trend || { labels: [], values: [] };
    const revenueEl = document.getElementById('platformRevenueChart');
    if (revenueEl) {
      new Chart(revenueEl.getContext('2d'), makeBarConfig(revenueTrend.labels, [
        {
          label: 'Revenue',
          data: revenueTrend.values,
          backgroundColor: palette.emerald,
          borderRadius: 10,
        },
      ]));
    }

    const seatStatus = kv(analytics.seats?.status);
    const seatEl = document.getElementById('platformSeatChart');
    if (seatEl) {
      new Chart(seatEl.getContext('2d'), doughnutConfig(seatStatus.labels, seatStatus.values, [palette.blue, palette.teal, palette.amber, palette.red]));
    }

    const activity = kv(analytics.security?.activity);
    const activityEl = document.getElementById('platformActivityChart');
    if (activityEl) {
      new Chart(activityEl.getContext('2d'), makeBarConfig(activity.labels, [
        { label: 'Events', data: activity.values, backgroundColor: palette.blue, borderRadius: 8 },
      ]));
    }

    const notifications = kv(analytics.security?.notifications);
    const notificationsEl = document.getElementById('platformNotificationsChart');
    if (notificationsEl) {
      new Chart(notificationsEl.getContext('2d'), doughnutConfig(notifications.labels, notifications.values, [palette.blue, palette.amber, palette.red, palette.teal]));
    }

    const routes = kv(analytics.operations?.routes?.by_mode);
    const routeEl = document.getElementById('platformRouteChart');
    if (routeEl) {
      new Chart(routeEl.getContext('2d'), makeBarConfig(routes.labels, [
        { label: 'Routes', data: routes.values, backgroundColor: palette.blue, borderRadius: 8 },
      ]));
    }

    const scenarios = analytics.operations?.scenarios?.costs || [];
    const scenarioEl = document.getElementById('platformScenarioChart');
    if (scenarioEl) {
      const labels = scenarios.map((s) => s.label);
      const values = scenarios.map((s) => s.value);
      new Chart(scenarioEl.getContext('2d'), makeLineConfig(labels, values, 'Scenario cost', palette.purple));
    }
  };
})();
