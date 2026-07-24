/** Install features/modeling/profiles commands. */
export function installFeaturesModelingProfiles(app) {
  function _generateFilamentSlug(manufacturer, colorName) {
    return (manufacturer + " " + colorName)
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-|-$/g, "");
  }

  function closeFilamentBuilderPanel() {
    // No-op stub — the filament builder panel was removed.
    // Kept as a safe no-op for any remaining calls during mode switches.
  }

  function showProfileToast(message) {
    let toast = document.getElementById("profileToast");
    if (!toast) {
      toast = document.createElement("div");
      toast.id = "profileToast";
      toast.className = "profile-toast";
      document.body.appendChild(toast);
    }
    toast.textContent = message;
    toast.classList.add("is-visible");
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => toast.classList.remove("is-visible"), 2000);
  }

  function isProfileFitJobRunning() {
    return (
      app.state.modeling.profileFitJobState.running ||
      app.state.modeling.profileFitJobState.status?.status === "queued" ||
      app.state.modeling.profileFitJobState.status?.status === "running"
    );
  }

  function profileFitResultFromJob(job) {
    const progress = job?.progress || {};
    const summary = progress.summary || {};
    return {
      fitted: Number(summary.fitted || 0),
      failed: Number(summary.failed || 0),
      skipped: Number(summary.skipped || 0),
      results: job?.results || [],
      pair_corrections: job?.pair_corrections || null,
      pair_corrections_error: job?.pair_corrections_error || null,
    };
  }

  function profileFitToastMessage(result) {
    let msg = `Fitted ${result.fitted} profiles (${result.failed} failed, ${result.skipped} skipped)`;
    if (result.pair_corrections) {
      msg += ` \u00B7 ${result.pair_corrections.n_pairs} pair corrections`;
    } else if (result.pair_corrections_error) {
      msg += ` \u00B7 pair corrections failed: ${result.pair_corrections_error}`;
    }
    return msg;
  }

  function renderProfileFitProgressPanel() {
    const job = app.state.modeling.profileFitJobState.status;
    if (!job) return "";

    const progress = job.progress || {};
    const summary = progress.summary || {};
    const total = Math.max(1, Number(progress.total || 1));
    const current = Math.min(total, Math.max(0, Number(progress.current || 0)));
    const pct = Math.max(0, Math.min(100, Number(progress.percent || 0)));
    const status =
      job.status ||
      (app.state.modeling.profileFitJobState.running ? "running" : "queued");
    const statusClass =
      status === "completed"
        ? "is-complete"
        : status === "failed"
          ? "is-failed"
          : "is-running";
    const title =
      status === "completed"
        ? "Profile Fitting Complete"
        : status === "failed"
          ? "Profile Fitting Failed"
          : "Fitting Profiles";
    const message = progress.message || "Fitting profiles";
    const phase = progress.phase
      ? app.commands.escapeHtml(String(progress.phase).replace(/_/g, " "))
      : "";
    const target = progress.target
      ? `<span class="mono">${app.commands.escapeHtml(progress.target)}</span>`
      : "";

    return `
      <div class="profile-fit-progress-panel ${statusClass}">
        <div class="profile-fit-progress-head">
          <strong>${title}</strong>
          <span class="small-copy mono">${current} / ${total} &middot; ${pct}%</span>
        </div>
        <div class="profile-progress-track">
          <div class="profile-progress-fill profile-fit-progress-fill" style="width:${pct}%"></div>
        </div>
        <div class="profile-fit-progress-meta">
          <span>${app.commands.escapeHtml(message)}</span>
          ${target}
        </div>
        <div class="profile-fit-progress-meta profile-fit-progress-summary mono">
          <span>${Number(summary.fitted || 0)} fitted</span>
          <span>${Number(summary.failed || 0)} failed</span>
          <span>${Number(summary.skipped || 0)} skipped</span>
          ${phase ? `<span>${phase}</span>` : ""}
        </div>
      </div>
    `;
  }

  function renderProfileFitSurfaces() {
    if (app.state.navigation.currentMode === "profiles") {
      app.commands.renderModelsView();
    }
  }

  function clearProfileFitCaches() {
    app.state.modeling.modelFittingState.predictionCache = {};
    app.state.modeling.profilesState.profileCache = {};
    app.state.modeling.profilesState.curveCache = {};
    app.state.modeling.profilesState.swatchCache = {};
    app.state.modeling.profilesState.errorCache = {};
  }

  async function pollProfileFitJob(jobId) {
    while (true) {
      const status = await app.api.fetchFitAllProfilesJobStatus(jobId);
      app.state.modeling.profileFitJobState.status = status;
      app.state.modeling.profileFitJobState.running =
        status.status === "queued" || status.status === "running";
      app.state.modeling.modelFittingState.isFittingAll =
        app.state.modeling.profileFitJobState.running;
      app.commands.renderProfileFitSurfaces();
      if (status.status === "completed") return status;
      if (status.status === "failed") {
        throw new Error(status.error || "Fit all failed");
      }
      await app.commands.sleep(700);
    }
  }

  async function runFitAllProfilesWithProgress() {
    if (app.commands.isProfileFitJobRunning()) {
      app.commands.showProfileToast("Profile fitting is already running");
      return app.state.modeling.profileFitJobState.status;
    }

    app.state.modeling.profileFitJobState.running = true;
    app.state.modeling.profileFitJobState.error = null;
    app.state.modeling.modelFittingState.isFittingAll = true;
    app.commands.renderProfileFitSurfaces();

    try {
      const job = await app.api.startFitAllProfilesJob();
      if (!job?.job_id) throw new Error("Fit job did not return a job id");
      app.state.modeling.profileFitJobState.jobId = job.job_id;
      app.state.modeling.profileFitJobState.status = job;
      app.commands.renderProfileFitSurfaces();

      const finalJob = await app.commands.pollProfileFitJob(job.job_id);
      const result = app.commands.profileFitResultFromJob(finalJob);
      app.state.modeling.profileFitJobState.lastResult = result;
      app.commands.clearProfileFitCaches();
      if (typeof app.commands.handleRefresh === "function")
        await app.commands.handleRefresh();
      app.commands.showProfileToast(
        app.commands.profileFitToastMessage(result),
      );
      return result;
    } catch (err) {
      app.state.modeling.profileFitJobState.error = err.message;
      app.commands.showProfileToast("Fit all failed: " + err.message);
      throw err;
    } finally {
      app.state.modeling.profileFitJobState.running = false;
      app.state.modeling.modelFittingState.isFittingAll = false;
      app.commands.renderProfileFitSurfaces();
    }
  }

  function selectProfileFilament(filamentId) {
    app.state.modeling.profilesState.selectedFilamentId = filamentId;
    app.state.modeling.profilesState.detailSection = "chart"; // reset to chart on selection
    const fil = app.state.session.data.filaments.find(
      (f) => f.filament_id === filamentId,
    );
    const needsLoad =
      fil &&
      fil.has_profile &&
      !app.state.modeling.profilesState.profileCache[filamentId];
    const hasStrips = fil && (fil.has_strips || fil.processed_count > 0);

    if (
      needsLoad ||
      (hasStrips && !app.state.modeling.profilesState.curveCache[filamentId])
    ) {
      app.state.modeling.profilesState.loadingProfile = true;
      app.commands.renderProfilesView();
      const fetches = [];
      if (
        fil.has_profile &&
        !app.state.modeling.profilesState.profileCache[filamentId]
      ) {
        fetches.push(
          app.api
            .fetchProfileDetail(filamentId)
            .then((profile) => {
              app.state.modeling.profilesState.profileCache[filamentId] =
                profile;
            })
            .catch(() => {}),
        );
      }
      if (!app.state.modeling.profilesState.curveCache[filamentId]) {
        fetches.push(
          app.api
            .fetchProfileCurve(filamentId)
            .then((curveData) => {
              app.state.modeling.profilesState.curveCache[filamentId] =
                curveData;
            })
            .catch(() => {}),
        );
      }
      if (!app.state.modeling.profilesState.swatchCache[filamentId]) {
        fetches.push(
          app.api
            .fetchProfileSwatches(filamentId)
            .then((swData) => {
              app.state.modeling.profilesState.swatchCache[filamentId] = swData;
            })
            .catch(() => {}),
        );
      }
      if (!app.state.modeling.profilesState.errorCache[filamentId]) {
        fetches.push(
          app.api
            .fetchProfileErrors(filamentId)
            .then((errData) => {
              app.state.modeling.profilesState.errorCache[filamentId] = errData;
            })
            .catch(() => {}),
        );
      }
      Promise.all(fetches).then(() => {
        app.state.modeling.profilesState.loadingProfile = false;
        app.commands.renderProfilesView();
        app.commands._drawProfileCanvasCharts(filamentId);
      });
    } else {
      app.commands.renderProfilesView();
      if (fil && fil.has_profile)
        app.commands._drawProfileCanvasCharts(filamentId);
    }
  }

  function renderProfileCoverageBar() {
    const total = app.state.session.data.filaments.length;
    const profiled = app.state.session.data.filaments.filter(
      (f) => f.has_profile,
    ).length;
    const withStrips = app.state.session.data.filaments.filter(
      (f) => f.has_strips || f.processed_count > 0,
    ).length;
    const stale = app.state.session.data.filaments.filter((f) => {
      const p = app.state.modeling.profilesState.profileCache[f.filament_id];
      return p && p.stale;
    }).length;
    const pct = total > 0 ? Math.round((profiled / total) * 100) : 0;
    const staleNote =
      stale > 0 ? `<span class="prof-stale-count">${stale} stale</span>` : "";
    return `
      <div class="profile-coverage-bar">
        <div class="profile-coverage-stats">
          <span>Coverage: <strong>${profiled}/${total}</strong> filaments profiled</span>
          <span class="muted-line">${withStrips} with strip data ${staleNote}</span>
        </div>
        <div class="profile-progress-track">
          <div class="profile-progress-fill" style="width:${pct}%"></div>
        </div>
      </div>
    `;
  }

  function _profileStatus(fil) {
    const p = app.state.modeling.profilesState.profileCache[fil.filament_id];
    if (p && p.stale) return "stale";
    if (fil.has_profile) return "fitted";
    if (fil.has_strips || fil.processed_count > 0) return "strips_only";
    return "none";
  }

  function renderProfileSidebar() {
    const filaments = [...app.state.session.data.filaments].sort((a, b) => {
      const order = { stale: 0, strips_only: 1, fitted: 2, none: 3 };
      const sa = order[app.commands._profileStatus(a)] ?? 4;
      const sb = order[app.commands._profileStatus(b)] ?? 4;
      if (sa !== sb) return sa - sb;
      return (a.color_name || "").localeCompare(b.color_name || "");
    });

    return filaments
      .map((fil) => {
        const selected =
          app.state.modeling.profilesState.selectedFilamentId ===
          fil.filament_id;
        const status = app.commands._profileStatus(fil);
        let icon, iconClass;
        if (status === "stale") {
          icon = "&#9888;";
          iconClass = "profile-icon-stale";
        } else if (status === "fitted") {
          icon = "&#10003;";
          iconClass = "profile-icon-ok";
        } else if (status === "strips_only") {
          icon = "&#9679;";
          iconClass = "profile-icon-strips";
        } else {
          icon = "&#10007;";
          iconClass = "profile-icon-missing";
        }
        const stripCount = fil.strip_count || fil.processed_count || 0;
        const countLabel =
          stripCount > 0
            ? `<span class="prof-sidebar-count">${stripCount}</span>`
            : "";
        return `
        <div class="profile-filament-item${selected ? " is-selected" : ""}" data-profile-filament="${fil.filament_id}">
          <span class="color-chip" style="background:${fil.hex || "#ddd"}"></span>
          <span class="profile-filament-name">${fil.color_name || fil.filament_id}</span>
          ${countLabel}
          <span class="profile-status-icon ${iconClass}">${icon}</span>
        </div>
      `;
      })
      .join("");
  }

  function _drawProfileCanvasCharts(filamentId) {
    const canvas = document.getElementById("profTransmissionCanvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;

    const cached = app.state.modeling.profilesState.profileCache[filamentId];
    const curveData = app.state.modeling.profilesState.curveCache?.[filamentId];
    if (!cached && !curveData) return;

    const W = canvas.clientWidth;
    const H = canvas.clientHeight;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    ctx.scale(dpr, dpr);

    const PAD_L = 44,
      PAD_R = 12,
      PAD_T = 12,
      PAD_B = 28;
    const plotW = W - PAD_L - PAD_R;
    const plotH = H - PAD_T - PAD_B;

    const knots = cached?.knots_mm || curveData?.spline?.knots || [];
    const allDs = [];
    if (curveData?.sources) {
      for (const pts of Object.values(curveData.sources)) {
        for (const pt of pts || []) allDs.push(pt.d);
      }
    }
    for (const k of knots) allDs.push(k);
    const rawDMax = allDs.length > 0 ? Math.max(...allDs) : 2.0;
    const dMax = Math.min(Math.max(rawDMax, 0.5), 4.0);

    const xScale = (d) => PAD_L + (d / dMax) * plotW;
    const yScale = (t) => PAD_T + (1 - Math.max(0, Math.min(1, t))) * plotH;

    // Clear
    ctx.clearRect(0, 0, W, H);

    // Background
    ctx.fillStyle = "#fff";
    ctx.fillRect(PAD_L, PAD_T, plotW, plotH);

    // Gridlines
    ctx.strokeStyle = "#e4e4df";
    ctx.lineWidth = 0.5;
    ctx.font = "9px 'Segoe UI', Arial, sans-serif";
    ctx.textAlign = "right";
    ctx.fillStyle = "#888";
    for (let t = 0; t <= 1.0; t += 0.2) {
      const y = yScale(t);
      ctx.beginPath();
      ctx.moveTo(PAD_L, y);
      ctx.lineTo(W - PAD_R, y);
      ctx.stroke();
      ctx.fillText(t.toFixed(1), PAD_L - 4, y + 3);
    }
    // X axis labels
    ctx.textAlign = "center";
    const xStep = dMax <= 1.5 ? 0.2 : dMax <= 2.5 ? 0.5 : 1.0;
    for (let d = 0; d <= dMax + 0.001; d += xStep) {
      const x = xScale(d);
      ctx.fillText(d.toFixed(1), x, H - 6);
    }

    // Axis labels
    ctx.save();
    ctx.font = "10px 'Segoe UI', Arial, sans-serif";
    ctx.fillStyle = "#666";
    ctx.textAlign = "center";
    ctx.fillText("thickness (mm)", PAD_L + plotW / 2, H - 0);
    ctx.restore();

    ctx.save();
    ctx.font = "10px 'Segoe UI', Arial, sans-serif";
    ctx.fillStyle = "#666";
    ctx.textAlign = "center";
    ctx.translate(10, PAD_T + plotH / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.fillText("T (transmission)", 0, 0);
    ctx.restore();

    // Noise floor line
    const noiseFloor = curveData?.noise_floor || cached?.noise_floor_T;
    if (noiseFloor && noiseFloor > 0) {
      const nfY = yScale(noiseFloor);
      ctx.setLineDash([4, 3]);
      ctx.strokeStyle = "#999";
      ctx.lineWidth = 0.7;
      ctx.beginPath();
      ctx.moveTo(PAD_L, nfY);
      ctx.lineTo(W - PAD_R, nfY);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.font = "8px 'Segoe UI', Arial, sans-serif";
      ctx.fillStyle = "#999";
      ctx.textAlign = "right";
      ctx.fillText("noise floor", W - PAD_R - 2, nfY - 3);
    }

    // Plot border
    ctx.strokeStyle = "#ccc";
    ctx.lineWidth = 0.5;
    ctx.strokeRect(PAD_L, PAD_T, plotW, plotH);

    const COLORS = { r: "#d32f2f", g: "#388e3c", b: "#1976d2" };
    const CHANNEL_KEYS = ["T_r", "T_g", "T_b"];
    const CHANNEL_COLORS = [COLORS.r, COLORS.g, COLORS.b];

    // Draw measured data points from strip sources
    if (curveData?.sources) {
      const categories = Object.entries(curveData.sources);
      for (const [category, points] of categories) {
        const isCrosscal =
          category === "thin" ||
          category === "fixed_role" ||
          category === "crosscal";
        for (const pt of points || []) {
          if (pt.d > dMax) continue;
          const vals = [pt.T_r, pt.T_g, pt.T_b];
          for (let ci = 0; ci < 3; ci++) {
            if (vals[ci] == null) continue;
            const x = xScale(pt.d);
            const y = yScale(vals[ci]);
            ctx.beginPath();
            if (isCrosscal) {
              // Diamond shape for crosscal points
              ctx.moveTo(x, y - 3);
              ctx.lineTo(x + 3, y);
              ctx.lineTo(x, y + 3);
              ctx.lineTo(x - 3, y);
              ctx.closePath();
              ctx.fillStyle = CHANNEL_COLORS[ci];
              ctx.globalAlpha = 0.45;
              ctx.fill();
            } else {
              // Circle for solo points
              ctx.arc(x, y, 2.5, 0, Math.PI * 2);
              ctx.fillStyle = CHANNEL_COLORS[ci];
              ctx.globalAlpha = 0.55;
              ctx.fill();
              ctx.globalAlpha = 0.3;
              ctx.strokeStyle = "#fff";
              ctx.lineWidth = 0.8;
              ctx.stroke();
            }
            ctx.globalAlpha = 1.0;
          }
        }
      }
    }

    // Draw spline curves
    if (curveData?.spline) {
      const sp = curveData.spline;
      const denseChannels = [
        { d: sp.d, vals: sp.T_r, color: COLORS.r },
        { d: sp.d, vals: sp.T_g, color: COLORS.g },
        { d: sp.d, vals: sp.T_b, color: COLORS.b },
      ];
      for (const ch of denseChannels) {
        ctx.beginPath();
        ctx.strokeStyle = ch.color;
        ctx.lineWidth = 1.8;
        ctx.globalAlpha = 0.9;
        let started = false;
        for (let i = 0; i < ch.d.length; i++) {
          if (ch.d[i] > dMax) break;
          const x = xScale(ch.d[i]);
          const y = yScale(ch.vals[i]);
          if (!started) {
            ctx.moveTo(x, y);
            started = true;
          } else ctx.lineTo(x, y);
        }
        ctx.stroke();
        ctx.globalAlpha = 1.0;
      }
      // Knot markers
      const knotDs = sp.knots || [];
      const knotChannels = [
        { vals: sp.knot_T_r, color: COLORS.r },
        { vals: sp.knot_T_g, color: COLORS.g },
        { vals: sp.knot_T_b, color: COLORS.b },
      ];
      for (const ch of knotChannels) {
        if (!ch.vals) continue;
        for (let i = 0; i < knotDs.length; i++) {
          if (knotDs[i] > dMax) continue;
          const x = xScale(knotDs[i]);
          const y = yScale(ch.vals[i]);
          ctx.beginPath();
          ctx.arc(x, y, 3, 0, Math.PI * 2);
          ctx.fillStyle = ch.color;
          ctx.globalAlpha = 0.7;
          ctx.fill();
          ctx.strokeStyle = "#fff";
          ctx.lineWidth = 1;
          ctx.stroke();
          ctx.globalAlpha = 1.0;
        }
      }
    } else if (cached?.knots_mm) {
      // Fallback: knot-to-knot lines
      for (let ci = 0; ci < 3; ci++) {
        const vals = cached[CHANNEL_KEYS[ci]];
        if (!vals) continue;
        ctx.beginPath();
        ctx.strokeStyle = CHANNEL_COLORS[ci];
        ctx.lineWidth = 1.5;
        for (let i = 0; i < knots.length; i++) {
          if (knots[i] > dMax) break;
          const x = xScale(knots[i]);
          const y = yScale(vals[i]);
          if (i === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        }
        ctx.stroke();
      }
    }

    // Draw error bar chart on secondary canvas
    app.commands._drawErrorBarChart(filamentId);
  }

  function _drawErrorBarChart(filamentId) {
    const canvas = document.getElementById("profErrorCanvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;

    const errData = app.state.modeling.profilesState.errorCache?.[filamentId];
    if (!errData || !errData.bars || errData.bars.length === 0) {
      canvas.width = canvas.clientWidth * dpr;
      canvas.height = canvas.clientHeight * dpr;
      ctx.scale(dpr, dpr);
      ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
      ctx.font = "12px 'Segoe UI', Arial, sans-serif";
      ctx.fillStyle = "#888";
      ctx.textAlign = "center";
      ctx.fillText(
        "No error data available",
        canvas.clientWidth / 2,
        canvas.clientHeight / 2,
      );
      return;
    }

    const W = canvas.clientWidth;
    const H = canvas.clientHeight;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, W, H);

    const PAD_L = 44,
      PAD_R = 12,
      PAD_T = 12,
      PAD_B = 28;
    const plotW = W - PAD_L - PAD_R;
    const plotH = H - PAD_T - PAD_B;

    const bars = errData.bars;
    const maxDe = Math.max(10, ...bars.map((b) => b.dE));
    const barW = Math.max(2, Math.min(12, (plotW - 4) / bars.length - 1));
    const gap = Math.max(1, (plotW - bars.length * barW) / (bars.length + 1));

    const yScale = (de) => PAD_T + plotH - (de / maxDe) * plotH;

    // Background
    ctx.fillStyle = "#fff";
    ctx.fillRect(PAD_L, PAD_T, plotW, plotH);

    // Threshold lines
    const thresholds = [
      { val: 2.0, label: "good", color: "#2e7d3244" },
      { val: 5.0, label: "ok", color: "#b26a0044" },
      { val: 10.0, label: "bad", color: "#d32f2f44" },
    ];
    for (const th of thresholds) {
      if (th.val > maxDe) continue;
      const y = yScale(th.val);
      ctx.setLineDash([3, 3]);
      ctx.strokeStyle = th.color.slice(0, 7);
      ctx.lineWidth = 0.7;
      ctx.beginPath();
      ctx.moveTo(PAD_L, y);
      ctx.lineTo(W - PAD_R, y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.font = "8px 'Segoe UI', Arial, sans-serif";
      ctx.fillStyle = th.color.slice(0, 7);
      ctx.textAlign = "right";
      ctx.fillText(`dE ${th.val}`, W - PAD_R - 2, y - 2);
    }

    // Y axis labels
    ctx.font = "9px 'Segoe UI', Arial, sans-serif";
    ctx.fillStyle = "#888";
    ctx.textAlign = "right";
    const yStep = maxDe <= 10 ? 2 : 5;
    for (let de = 0; de <= maxDe; de += yStep) {
      const y = yScale(de);
      ctx.fillText(de.toFixed(0), PAD_L - 4, y + 3);
      ctx.strokeStyle = "#e8e8e4";
      ctx.lineWidth = 0.3;
      ctx.beginPath();
      ctx.moveTo(PAD_L, y);
      ctx.lineTo(W - PAD_R, y);
      ctx.stroke();
    }

    // Draw bars
    const severityColors = {
      good: "#4caf50",
      ok: "#ff9800",
      bad: "#f44336",
      awful: "#b71c1c",
    };
    for (let i = 0; i < bars.length; i++) {
      const b = bars[i];
      const x = PAD_L + gap + i * (barW + gap);
      const barH = (b.dE / maxDe) * plotH;
      const y = PAD_T + plotH - barH;
      ctx.fillStyle = severityColors[b.severity] || "#999";
      ctx.globalAlpha = 0.8;
      ctx.fillRect(x, y, barW, barH);
      ctx.globalAlpha = 1.0;
    }

    // Axis labels
    ctx.font = "10px 'Segoe UI', Arial, sans-serif";
    ctx.fillStyle = "#666";
    ctx.textAlign = "center";
    ctx.fillText("swatches (sorted by thickness)", PAD_L + plotW / 2, H - 2);

    // Border
    ctx.strokeStyle = "#ccc";
    ctx.lineWidth = 0.5;
    ctx.strokeRect(PAD_L, PAD_T, plotW, plotH);
  }

  function renderProfileMetadata(filamentId) {
    const cached = app.state.modeling.profilesState.profileCache[filamentId];
    if (app.state.modeling.profilesState.loadingProfile) {
      return `<div class="profile-meta-block"><span class="muted-line">Loading profile data...</span></div>`;
    }
    if (!cached) {
      return `
        <div class="profile-meta-block">
          <p class="panel-kicker">Profile Metadata</p>
          <div class="profile-meta-grid">
            <div class="profile-meta-item"><span class="sidebar-label">Knots</span><strong>&mdash;</strong></div>
            <div class="profile-meta-item"><span class="sidebar-label">D range</span><strong>&mdash;</strong></div>
            <div class="profile-meta-item"><span class="sidebar-label">Fit date</span><strong>&mdash;</strong></div>
            <div class="profile-meta-item"><span class="sidebar-label">Data sources</span><strong>&mdash;</strong></div>
          </div>
        </div>
      `;
    }
    const knotCount = cached.knots_mm
      ? cached.knots_mm.length
      : cached.n_knots || "&mdash;";
    const dMin = cached.knots_mm
      ? Number(cached.knots_mm[0]).toFixed(2)
      : "0.00";
    const dMax = cached.knots_mm
      ? Number(cached.knots_mm[cached.knots_mm.length - 1]).toFixed(2)
      : "&mdash;";
    const fitDate = cached.fit_date || cached.created || "&mdash;";
    const sourceStrips = cached.source_strips
      ? cached.source_strips.length
      : "&mdash;";
    const noiseFloor =
      cached.noise_floor_T != null
        ? cached.noise_floor_T.toFixed(4)
        : "&mdash;";
    const truncated = cached.n_truncated || 0;
    const isActive = cached.active !== false;
    const isStale = cached.stale === true;

    let staleBanner = "";
    if (isStale) {
      const reason = cached.stale_reason || "Profile data may be outdated";
      staleBanner = `
        <div class="prof-stale-banner">
          <strong>Stale profile</strong> &mdash; ${reason}.
          Refit recommended.
        </div>`;
    }

    return `
      ${staleBanner}
      <div class="profile-meta-block">
        <p class="panel-kicker">Fit Summary</p>
        <div class="profile-meta-grid">
          <div class="profile-meta-item"><span class="sidebar-label">Knots</span><strong>${knotCount}</strong></div>
          <div class="profile-meta-item"><span class="sidebar-label">D range</span><strong>${dMin} &ndash; ${dMax} mm</strong></div>
          <div class="profile-meta-item"><span class="sidebar-label">Noise floor</span><strong>${noiseFloor}</strong></div>
          <div class="profile-meta-item"><span class="sidebar-label">Source strips</span><strong>${sourceStrips}</strong></div>
          <div class="profile-meta-item"><span class="sidebar-label">Truncated</span><strong>${truncated} pts</strong></div>
          <div class="profile-meta-item"><span class="sidebar-label">Fit date</span><strong>${fitDate}</strong></div>
          <div class="profile-meta-item"><span class="sidebar-label">Active</span><strong>${isActive ? "Yes" : "No"}</strong></div>
          <div class="profile-meta-item"><span class="sidebar-label">Model</span><strong>PCHIP spline</strong></div>
        </div>
      </div>
    `;
  }

  function renderSwatchComparisonTable(filamentId) {
    const swatchData =
      app.state.modeling.profilesState.swatchCache?.[filamentId];
    if (!swatchData?.swatches || swatchData.swatches.length === 0) {
      return `<div class="profile-crosscal-block"><p class="panel-kicker">Swatch Comparison</p><span class="muted-line">No swatch data available</span></div>`;
    }

    const sortKey = app.state.modeling.profilesState.crosscalSortKey;
    const sortDir = app.state.modeling.profilesState.crosscalSortDir;

    const rows = [...swatchData.swatches].sort((a, b) => {
      if (sortKey === "dE")
        return sortDir === "asc" ? a.dE - b.dE : b.dE - a.dE;
      if (sortKey === "d") return sortDir === "asc" ? a.d - b.d : b.d - a.d;
      return sortDir === "asc"
        ? String(a.strip_label).localeCompare(String(b.strip_label))
        : String(b.strip_label).localeCompare(String(a.strip_label));
    });

    const arrow = (key) => {
      if (sortKey !== key) return "";
      return sortDir === "asc" ? " &#9650;" : " &#9660;";
    };

    const rowsHtml = rows
      .map((sw) => {
        let sevClass = "";
        if (sw.dE < 2.0) sevClass = "prof-de-good";
        else if (sw.dE < 5.0) sevClass = "prof-de-ok";
        else if (sw.dE < 10.0) sevClass = "prof-de-bad";
        else sevClass = "prof-de-awful";

        return `
        <tr>
          <td class="mono">${Number(sw.d).toFixed(2)}</td>
          <td><span class="prof-swatch-chip" style="background:${sw.measured_hex}"></span> ${sw.measured_hex}</td>
          <td><span class="prof-swatch-chip" style="background:${sw.predicted_hex}"></span> ${sw.predicted_hex}</td>
          <td class="mono ${sevClass}">${sw.dE.toFixed(2)}</td>
          <td class="small-copy">${sw.strip_label || ""}</td>
        </tr>`;
      })
      .join("");

    const meanDe = rows.reduce((s, r) => s + r.dE, 0) / rows.length;
    const maxDe = Math.max(...rows.map((r) => r.dE));

    return `
      <div class="profile-crosscal-block">
        <div class="prof-section-head">
          <p class="panel-kicker">Swatch Comparison</p>
          <span class="muted-line">${rows.length} swatches &middot; mean dE ${meanDe.toFixed(2)} &middot; max dE ${maxDe.toFixed(1)}</span>
        </div>
        <table class="data-table compact-table profile-crosscal-table">
          <thead>
            <tr>
              <th class="sortable" data-crosscal-sort="d">d (mm)${arrow("d")}</th>
              <th>Measured</th>
              <th>Predicted</th>
              <th class="sortable" data-crosscal-sort="dE">dE${arrow("dE")}</th>
              <th class="sortable" data-crosscal-sort="strip_label">Strip${arrow("strip_label")}</th>
            </tr>
          </thead>
          <tbody>${rowsHtml}</tbody>
        </table>
      </div>
    `;
  }

  function renderStripErrorSummary(filamentId) {
    const swatchData =
      app.state.modeling.profilesState.swatchCache?.[filamentId];
    if (!swatchData?.swatches || swatchData.swatches.length === 0) return "";

    const byStrip = {};
    for (const sw of swatchData.swatches) {
      const label = sw.strip_label || "unknown";
      if (!byStrip[label]) byStrip[label] = [];
      byStrip[label].push(sw.dE || 0);
    }

    const stripRows = Object.entries(byStrip)
      .map(([label, des]) => {
        const mean = des.reduce((a, b) => a + b, 0) / des.length;
        const max = Math.max(...des);
        let sevClass = "";
        if (mean < 2.0) sevClass = "prof-de-good";
        else if (mean < 5.0) sevClass = "prof-de-ok";
        else sevClass = "prof-de-bad";
        return `
        <tr>
          <td>${label}</td>
          <td class="mono">${des.length}</td>
          <td class="mono ${sevClass}">${mean.toFixed(2)}</td>
          <td class="mono">${max.toFixed(1)}</td>
        </tr>`;
      })
      .join("");

    return `
      <div class="profile-crosscal-block" style="margin-top:0">
        <p class="panel-kicker">Per-Strip Summary</p>
        <table class="data-table compact-table profile-crosscal-table">
          <thead>
            <tr><th>Strip</th><th>Swatches</th><th>Mean dE</th><th>Max dE</th></tr>
          </thead>
          <tbody>${stripRows}</tbody>
        </table>
      </div>
    `;
  }

  function renderBatchAuditSummary() {
    const allDes = [];
    const stripDes = {};
    for (const [fid, swData] of Object.entries(
      app.state.modeling.profilesState.swatchCache || {},
    )) {
      if (!swData?.swatches) continue;
      for (const sw of swData.swatches) {
        if (sw.dE != null) {
          allDes.push(sw.dE);
          const label = sw.strip_label || fid;
          if (!stripDes[label]) stripDes[label] = [];
          stripDes[label].push(sw.dE);
        }
      }
    }

    if (allDes.length === 0) {
      return `
        <div class="profile-batch-audit">
          <p class="panel-kicker">Batch Audit Summary</p>
          <div class="profile-batch-stats">
            <span class="muted-line">Select profiled filaments to populate audit data</span>
          </div>
        </div>
      `;
    }

    const mean = allDes.reduce((a, b) => a + b, 0) / allDes.length;
    let worstLabel = "&mdash;",
      worstDe = 0;
    for (const [label, des] of Object.entries(stripDes)) {
      const stripMean = des.reduce((a, b) => a + b, 0) / des.length;
      if (stripMean > worstDe) {
        worstDe = stripMean;
        worstLabel = label;
      }
    }
    const nStrips = Object.keys(stripDes).length;
    const nOver5 = Object.values(stripDes).filter((des) => {
      const m = des.reduce((a, b) => a + b, 0) / des.length;
      return m > 5;
    }).length;

    return `
      <div class="profile-batch-audit">
        <p class="panel-kicker">Batch Audit Summary</p>
        <div class="profile-batch-stats">
          <div class="profile-batch-stat"><span class="sidebar-label">Overall mean dE</span><strong>${mean.toFixed(2)}</strong></div>
          <div class="profile-batch-stat"><span class="sidebar-label">Worst strip</span><strong>${worstLabel} (dE ${worstDe.toFixed(2)})</strong></div>
          <div class="profile-batch-stat"><span class="sidebar-label">Strips measured</span><strong>${nStrips}</strong></div>
          <div class="profile-batch-stat"><span class="sidebar-label">Strips > dE 5</span><strong>${nOver5}</strong></div>
        </div>
      </div>
    `;
  }

  function renderProfileDetailPanel() {
    const filamentId = app.state.modeling.profilesState.selectedFilamentId;
    if (!filamentId) {
      return `
        <div class="profile-detail-empty">
          <p class="muted-line">Select a filament from the sidebar to view its profile.</p>
        </div>
      `;
    }

    const fil = app.state.session.data.filaments.find(
      (f) => f.filament_id === filamentId,
    );
    if (!fil) return "";

    const stripCount = fil.strip_count || fil.processed_count || 0;
    const hasProfile = fil.has_profile;
    const hasStrips = fil.has_strips || stripCount > 0;
    const cachedProfile =
      app.state.modeling.profilesState.profileCache[filamentId] || {};
    const isActive = cachedProfile.active !== false;
    const profileFitBusy = app.commands.isProfileFitJobRunning();
    const profileFitDisabled = profileFitBusy ? "disabled" : "";

    // Header with filament info
    const statusMeta = app.commands.profilePillMeta(filamentId);
    const statusPill = statusMeta
      ? `<span class="status-pill ${statusMeta.cls}">${statusMeta.label}</span>`
      : "";

    const headerHtml = `
      <div class="profile-detail-header">
        <span class="color-chip large-chip" style="background:${fil.hex || "#ddd"}"></span>
        <div style="flex:1;min-width:0">
          <div style="display:flex;align-items:center;gap:8px">
            <h3 style="margin:0">${fil.display_name || fil.color_name}</h3>
            ${statusPill}
          </div>
          <span class="muted-line">${fil.manufacturer || ""} &middot; ${fil.filament_id} &middot; ${stripCount} strip${stripCount !== 1 ? "s" : ""}</span>
        </div>
      </div>
    `;

    if (!hasProfile && !hasStrips) {
      return `
        <div class="profile-detail-content">
          ${headerHtml}
          <div class="profile-no-profile-panel">
            <strong>No data available</strong>
            <p class="small-copy">This filament has no processed strip data and no profile. Process some samples first.</p>
          </div>
        </div>
      `;
    }

    if (!hasProfile && hasStrips) {
      return `
        <div class="profile-detail-content">
          ${headerHtml}
          <div class="profile-no-profile-panel">
            <strong>No profile fitted</strong>
            <p class="small-copy">${stripCount} strip${stripCount !== 1 ? "s" : ""} available for fitting.</p>
            <button class="primary-button small profile-action-btn" data-profile-action="fit" ${profileFitDisabled}>Fit Profile</button>
          </div>
        </div>
      `;
    }

    // Has profile — show detail tabs
    const section = app.state.modeling.profilesState.detailSection || "chart";
    const tabActive = (t) => (t === section ? "is-active" : "");

    const chartSection = `
      <div class="prof-chart-block">
        <div class="prof-section-head">
          <p class="panel-kicker">Transmission vs Thickness</p>
          <div class="profile-chart-legend" style="margin:0;padding:0">
            <span class="profile-legend-item"><span class="profile-legend-line" style="background:#d32f2f"></span> R</span>
            <span class="profile-legend-item"><span class="profile-legend-line" style="background:#388e3c"></span> G</span>
            <span class="profile-legend-item"><span class="profile-legend-line" style="background:#1976d2"></span> B</span>
            <span class="profile-legend-item" style="opacity:0.6"><span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:#666;margin-right:3px"></span> data</span>
            <span class="profile-legend-item" style="opacity:0.6"><span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:#666;border:2px solid #fff;margin-right:3px"></span> knot</span>
          </div>
        </div>
        <canvas id="profTransmissionCanvas" class="prof-canvas" style="height:240px"></canvas>
      </div>
      <div class="prof-chart-block">
        <div class="prof-section-head">
          <p class="panel-kicker">Per-Swatch Fit Error (dE)</p>
        </div>
        <canvas id="profErrorCanvas" class="prof-canvas" style="height:140px"></canvas>
      </div>
    `;

    let detailBody = "";
    if (section === "chart") {
      detailBody = `
        ${chartSection}
        ${app.commands.renderProfileMetadata(filamentId)}
      `;
    } else if (section === "swatches") {
      detailBody = `
        ${app.commands.renderSwatchComparisonTable(filamentId)}
        ${app.commands.renderStripErrorSummary(filamentId)}
      `;
    } else if (section === "data") {
      detailBody = app.commands.renderProfileDataSources(filamentId);
    }

    return `
      <div class="profile-detail-content">
        ${headerHtml}
        <div class="prof-detail-tabs">
          <button class="prof-tab-btn ${tabActive("chart")}" data-prof-tab="chart">Charts</button>
          <button class="prof-tab-btn ${tabActive("swatches")}" data-prof-tab="swatches">Swatches</button>
          <button class="prof-tab-btn ${tabActive("data")}" data-prof-tab="data">Data Sources</button>
        </div>
        <div class="prof-detail-body">
          ${detailBody}
        </div>
        <div class="profile-action-row">
          <button class="primary-button small profile-action-btn" data-profile-action="refit-single" ${profileFitDisabled}>Refit</button>
          <button class="${isActive ? "profile-deactivate-btn" : "profile-activate-btn"} profile-action-btn" data-profile-action="${isActive ? "deactivate" : "activate"}" ${profileFitDisabled}>${isActive ? "Deactivate" : "Activate"}</button>
        </div>
      </div>
    `;
  }

  function renderProfileDataSources(filamentId) {
    const curveData = app.state.modeling.profilesState.curveCache?.[filamentId];
    if (!curveData?.sources) {
      return `<div class="profile-meta-block"><span class="muted-line">No source data loaded</span></div>`;
    }

    const categories = [
      {
        key: "solo",
        label: "Solo strips",
        desc: "Single-filament strips (primary fit data)",
      },
      {
        key: "thin",
        label: "Thin / crosscal strips",
        desc: "Multi-filament stacked strips",
      },
      {
        key: "fixed_role",
        label: "Fixed role data",
        desc: "Data from fixed-layer positions",
      },
      {
        key: "crosscal",
        label: "Cross-calibration",
        desc: "Cross-calibration pair data",
      },
    ];

    let html = "";
    for (const cat of categories) {
      const pts = curveData.sources[cat.key];
      if (!pts || pts.length === 0) continue;

      // Group by strip label
      const byStrip = {};
      for (const pt of pts) {
        const label = pt.strip_label || "unknown";
        if (!byStrip[label]) byStrip[label] = [];
        byStrip[label].push(pt);
      }

      const stripBlocks = Object.entries(byStrip)
        .map(([label, points]) => {
          const rowsHtml = points
            .map(
              (pt) => `
          <tr>
            <td class="mono">${Number(pt.d).toFixed(2)}</td>
            <td class="mono">${pt.T_r != null ? pt.T_r.toFixed(4) : "&mdash;"}</td>
            <td class="mono">${pt.T_g != null ? pt.T_g.toFixed(4) : "&mdash;"}</td>
            <td class="mono">${pt.T_b != null ? pt.T_b.toFixed(4) : "&mdash;"}</td>
          </tr>
        `,
            )
            .join("");
          return `
          <div class="prof-source-strip">
            <div class="prof-source-strip-head">${label} <span class="muted-line">(${points.length} pts)</span></div>
            <table class="data-table compact-table">
              <thead><tr><th>d (mm)</th><th>T_r</th><th>T_g</th><th>T_b</th></tr></thead>
              <tbody>${rowsHtml}</tbody>
            </table>
          </div>`;
        })
        .join("");

      html += `
        <div class="profile-crosscal-block">
          <div class="prof-section-head">
            <p class="panel-kicker">${cat.label}</p>
            <span class="muted-line">${pts.length} points from ${Object.keys(byStrip).length} strip${Object.keys(byStrip).length !== 1 ? "s" : ""}</span>
          </div>
          <p class="small-copy" style="margin-bottom:8px">${cat.desc}</p>
          ${stripBlocks}
        </div>`;
    }

    if (!html) {
      html = `<div class="profile-meta-block"><span class="muted-line">No source data found for this filament</span></div>`;
    }

    return html;
  }

  function bindProfileActions() {
    const container = document.getElementById("tableContainer");
    if (!container) return;

    // Sidebar filament selection
    container.querySelectorAll("[data-profile-filament]").forEach((item) => {
      item.addEventListener("click", () => {
        app.commands.selectProfileFilament(item.dataset.profileFilament);
      });
    });

    // Detail tab switching
    container.querySelectorAll("[data-prof-tab]").forEach((btn) => {
      btn.addEventListener("click", () => {
        app.state.modeling.profilesState.detailSection = btn.dataset.profTab;
        app.commands.renderProfilesView();
        const fid = app.state.modeling.profilesState.selectedFilamentId;
        if (fid && app.state.modeling.profilesState.detailSection === "chart") {
          requestAnimationFrame(() =>
            app.commands._drawProfileCanvasCharts(fid),
          );
        }
      });
    });

    // Sort headers in swatch table
    container.querySelectorAll("[data-crosscal-sort]").forEach((th) => {
      th.addEventListener("click", () => {
        const key = th.dataset.crosscalSort;
        if (app.state.modeling.profilesState.crosscalSortKey === key) {
          app.state.modeling.profilesState.crosscalSortDir =
            app.state.modeling.profilesState.crosscalSortDir === "asc"
              ? "desc"
              : "asc";
        } else {
          app.state.modeling.profilesState.crosscalSortKey = key;
          app.state.modeling.profilesState.crosscalSortDir = [
            "d",
            "strip_label",
            "pair",
          ].includes(key)
            ? "asc"
            : "desc";
        }
        app.commands.renderProfilesView();
        const fid = app.state.modeling.profilesState.selectedFilamentId;
        if (fid && app.state.modeling.profilesState.detailSection === "chart") {
          requestAnimationFrame(() =>
            app.commands._drawProfileCanvasCharts(fid),
          );
        }
      });
    });

    // Profile action buttons
    document.querySelectorAll(".profile-action-btn").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const action = btn.dataset.profileAction;
        const fid = app.state.modeling.profilesState.selectedFilamentId;
        const profileWriteActions = [
          "refit-all",
          "refit-single",
          "fit",
          "activate",
          "deactivate",
        ];
        if (
          app.commands.isProfileFitJobRunning() &&
          profileWriteActions.includes(action)
        ) {
          app.commands.showProfileToast("Profile fitting is already running");
          return;
        }

        if (action === "refit-all") {
          try {
            await app.commands.runFitAllProfilesWithProgress();
          } catch (err) {
            // The shared runner has already surfaced the failure.
          }
          return;
        }

        if (action === "refit-single" || action === "fit") {
          if (!fid) {
            app.commands.showProfileToast("Select a filament first");
            return;
          }
          btn.disabled = true;
          btn.textContent = "Fitting...";
          try {
            const result = await app.api.fitProfile(fid);
            app.commands.showProfileToast(
              `Fitted ${fid}: ${result.n_knots} knots`,
            );
            delete app.state.modeling.profilesState.profileCache[fid];
            delete app.state.modeling.profilesState.curveCache[fid];
            delete app.state.modeling.profilesState.swatchCache[fid];
            delete app.state.modeling.profilesState.errorCache[fid];
            if (typeof app.commands.handleRefresh === "function")
              await app.commands.handleRefresh();
            app.commands.renderProfilesView();
            app.commands.selectProfileFilament(fid);
          } catch (err) {
            app.commands.showProfileToast("Fit failed: " + err.message);
          } finally {
            btn.disabled = false;
            btn.textContent = action === "fit" ? "Fit Profile" : "Refit";
          }
          return;
        }

        if (action === "activate") {
          if (!fid) {
            app.commands.showProfileToast("Select a filament first");
            return;
          }
          try {
            await app.api.activateProfile(fid);
            app.commands.showProfileToast(`${fid} activated for Prisma`);
            delete app.state.modeling.profilesState.profileCache[fid];
            app.commands.selectProfileFilament(fid);
          } catch (err) {
            app.commands.showProfileToast("Activate failed: " + err.message);
          }
          return;
        }

        if (action === "deactivate") {
          if (!fid) {
            app.commands.showProfileToast("Select a filament first");
            return;
          }
          try {
            await app.api.deactivateProfile(fid);
            app.commands.showProfileToast(`${fid} deactivated`);
            delete app.state.modeling.profilesState.profileCache[fid];
            app.commands.selectProfileFilament(fid);
          } catch (err) {
            app.commands.showProfileToast("Deactivate failed: " + err.message);
          }
          return;
        }

        if (action === "refit-selected") {
          app.commands.showProfileToast(
            "Refit Selected -- batch selection coming soon",
          );
          return;
        }

        app.commands.showProfileToast("Coming soon");
      });
    });
  }

  function renderProfilesView() {
    app.dom.tableToolbar.className = "toolbar-inline";

    const profiled = app.state.session.data.filaments.filter(
      (f) => f.has_profile,
    ).length;
    const withStrips = app.state.session.data.filaments.filter(
      (f) => f.has_strips || f.processed_count > 0,
    ).length;
    app.dom.tableSummary.textContent = `${profiled} profiled / ${withStrips} with data / ${app.state.session.data.filaments.length} total`;
    const fitAllRunning = app.commands.isProfileFitJobRunning();
    app.dom.tableToolbar.innerHTML = `
      <button class="primary-button small profile-action-btn" data-profile-action="refit-all" ${fitAllRunning ? "disabled" : ""}>${fitAllRunning ? "Fitting\u2026" : "Refit All"}</button>
    `;

    app.dom.tableContainer.innerHTML = `
      ${app.commands.renderProfileFitProgressPanel()}
      ${app.commands.renderProfileCoverageBar()}
      <div class="profile-layout">
        <div class="profile-sidebar-list">
          <div class="prof-sidebar-header">
            <p class="panel-kicker" style="margin:0">Filaments</p>
          </div>
          ${app.commands.renderProfileSidebar()}
          <div class="profile-sidebar-legend">
            <span class="profile-icon-ok">&#10003;</span> fitted &nbsp;
            <span class="profile-icon-stale">&#9888;</span> stale &nbsp;
            <span class="profile-icon-strips">&#9679;</span> strips &nbsp;
            <span class="profile-icon-missing">&#10007;</span> none
          </div>
        </div>
        <div class="profile-detail-panel panel inset-panel">
          ${app.commands.renderProfileDetailPanel()}
        </div>
      </div>
      ${app.commands.renderBatchAuditSummary()}
    `;

    app.commands.bindProfileActions();
  }

  Object.assign(app.commands, {
    _generateFilamentSlug,
    closeFilamentBuilderPanel,
    showProfileToast,
    isProfileFitJobRunning,
    profileFitResultFromJob,
    profileFitToastMessage,
    renderProfileFitProgressPanel,
    renderProfileFitSurfaces,
    clearProfileFitCaches,
    pollProfileFitJob,
    runFitAllProfilesWithProgress,
    selectProfileFilament,
    renderProfileCoverageBar,
    _profileStatus,
    renderProfileSidebar,
    _drawProfileCanvasCharts,
    _drawErrorBarChart,
    renderProfileMetadata,
    renderSwatchComparisonTable,
    renderStripErrorSummary,
    renderBatchAuditSummary,
    renderProfileDetailPanel,
    renderProfileDataSources,
    bindProfileActions,
    renderProfilesView,
  });
}
