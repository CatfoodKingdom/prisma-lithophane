/** Install features/processing/manual commands. */
export function installFeaturesProcessingManual(app) {
  function openManualProcessing(sampleIds, options = {}) {
    const exps = sampleIds
      .map((id) =>
        app.state.session.data.samples.find((e) => e.sample_id === id),
      )
      .filter(Boolean);
    if (exps.length === 0) return;

    app.state.processing._manualProc.mode =
      exps.length === 1 ? "single" : "batch";
    app.state.processing._manualProc.queue = exps;
    app.state.processing._manualProc.currentIndex = 0;
    app.state.processing._manualProc.completed = new Set();
    app.state.processing._manualProc.context = options?.context
      ? { ...options }
      : null;
    app.commands._resetManualCorners();

    const overlay = document.getElementById("manualProcOverlay");
    if (overlay) {
      overlay.classList.toggle(
        "is-workflow-modal",
        app.state.processing._manualProc.context?.context ===
          "reextract-candidate",
      );
      overlay.classList.add("is-open");
      overlay.setAttribute("aria-hidden", "false");
    }
    app.commands._loadManualProcSample();
  }

  function closeManualProcessing() {
    if (
      app.state.processing._manualProc.currentJobId ||
      app.state.processing._manualProc.processing
    )
      return;
    const activeSample = app.commands._currentManualSample();
    if (
      app.state.processing._manualProc.context?.context !==
        "reextract-candidate" &&
      activeSample?.sample_id
    ) {
      app.api
        .apiDelete(
          `/process/manual/review/${encodeURIComponent(activeSample.sample_id)}`,
        )
        .catch(() => {});
    }
    const overlay = document.getElementById("manualProcOverlay");
    if (overlay) {
      overlay.classList.remove("is-open");
      overlay.classList.remove("is-workflow-modal");
      overlay.setAttribute("aria-hidden", "true");
    }

    // Batch mode: completed samples stay as "processed" (they were processed server-side).
    // Incomplete samples remain in their current state (flagged/failed).
    // The processing dashboard will re-render and pick them up correctly.
    app.state.processing._manualProc.mode = null;
    app.state.processing._manualProc.queue = [];
    app.state.processing._manualProc.sourceImage = null;
    app.state.processing._manualProc.corners = [];
    app.state.processing._manualProc.currentJobId = "";
    app.state.processing._manualProc.cancelling = false;

    const context = app.state.processing._manualProc.context;
    app.state.processing._manualProc.context = null;
    if (context?.context === "reextract-candidate") {
      if (!context.completed) context.onCandidateComplete?.();
      return;
    }

    // Refresh data and re-render to reflect any server-side status changes
    app.commands
      .handleRefresh()
      .then(() => app.commands.renderProcessingDashboard());
  }

  function _resetManualCorners() {
    app.state.processing._manualProc.corners = [];
    app.state.processing._manualProc.processing = false;
    app.state.processing._manualProc.currentJobId = "";
    app.state.processing._manualProc.cancelling = false;
  }

  function _currentManualSample() {
    return (
      app.state.processing._manualProc.queue[
        app.state.processing._manualProc.currentIndex
      ] || null
    );
  }

  function _loadManualProcSample() {
    const exp = app.commands._currentManualSample();
    if (!exp) return;

    app.commands._resetManualCorners();
    app.commands._updateManualProcUI();

    // Load source image onto the canvas
    const imgName = exp._assigned_image;
    if (!imgName) {
      app.commands._setManualInstructions(
        "No source image assigned to this sample.",
      );
      return;
    }

    const img = new Image();
    img.crossOrigin = "anonymous";
    img.onload = () => {
      app.state.processing._manualProc.sourceImage = img;
      app.commands._drawManualCanvas();
    };
    img.onerror = () => {
      app.commands._setManualInstructions("Failed to load source image.");
    };
    // Use full-size preview for accurate corner placement
    img.src = app.commands.previewUrl(imgName, { size: "full" });
  }

  function _drawManualCanvas() {
    const canvas = document.getElementById("manualProcCanvas");
    const area = canvas?.parentElement;
    if (!canvas || !app.state.processing._manualProc.sourceImage) return;

    const img = app.state.processing._manualProc.sourceImage;
    const ctx = canvas.getContext("2d");

    // Size canvas to fit the available area while preserving aspect ratio
    const areaW = area.clientWidth;
    const areaH = area.clientHeight;
    const scale = Math.min(
      areaW / img.naturalWidth,
      areaH / img.naturalHeight,
      1,
    );

    canvas.width = Math.round(img.naturalWidth * scale);
    canvas.height = Math.round(img.naturalHeight * scale);
    app.state.processing._manualProc.previewScale = scale;

    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);

    // Draw placed corners
    for (let i = 0; i < app.state.processing._manualProc.corners.length; i++) {
      const c = app.state.processing._manualProc.corners[i];
      const cx = c.x * scale;
      const cy = c.y * scale;

      ctx.beginPath();
      ctx.arc(cx, cy, 6, 0, Math.PI * 2);
      ctx.fillStyle = app.constants.CORNER_COLORS[i];
      ctx.fill();
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 2;
      ctx.stroke();

      // Label — positioned diagonally outward from quad center
      ctx.font = "bold 11px sans-serif";
      ctx.fillStyle = "#fff";
      ctx.strokeStyle = "rgba(0,0,0,0.6)";
      ctx.lineWidth = 3;
      const label = app.constants.CORNER_LABELS[i];
      const tw = ctx.measureText(label).width;
      // Offsets: TL→upper-left, TR→upper-right, BR→lower-right, BL→lower-left
      const labelOffsets = [
        { x: -tw - 8, y: -8 }, // TL: text lower-right anchored at 10:30
        { x: 8, y: -8 }, // TR: text lower-left anchored at 1:30
        { x: 8, y: 16 }, // BR: text upper-left anchored at 4:30
        { x: -tw - 8, y: 16 }, // BL: text upper-right anchored at 7:30
      ];
      const off = labelOffsets[i] || { x: 10, y: 4 };
      ctx.strokeText(label, cx + off.x, cy + off.y);
      ctx.fillText(label, cx + off.x, cy + off.y);
    }

    // Draw lines connecting corners (dark outline + bright line for visibility on any background)
    if (app.state.processing._manualProc.corners.length >= 2) {
      function _tracePath() {
        ctx.beginPath();
        for (
          let i = 0;
          i < app.state.processing._manualProc.corners.length;
          i++
        ) {
          const c = app.state.processing._manualProc.corners[i];
          const px = c.x * scale;
          const py = c.y * scale;
          if (i === 0) ctx.moveTo(px, py);
          else ctx.lineTo(px, py);
        }
        if (app.state.processing._manualProc.corners.length === 4) {
          const first = app.state.processing._manualProc.corners[0];
          ctx.lineTo(first.x * scale, first.y * scale);
        }
        ctx.stroke();
      }
      // Dark outline
      ctx.strokeStyle = "rgba(0,0,0,0.6)";
      ctx.lineWidth = 3;
      ctx.setLineDash([]);
      _tracePath();
      // Bright dashed line on top
      ctx.strokeStyle = "#00e5ff";
      ctx.lineWidth = 1.5;
      ctx.setLineDash([6, 4]);
      _tracePath();
      ctx.setLineDash([]);
    }
  }

  function _updateManualProcUI() {
    const exp = app.commands._currentManualSample();
    const title = document.getElementById("manualProcTitle");
    const subtitle = document.getElementById("manualProcSubtitle");
    const progress = document.getElementById("manualProcProgress");
    const cornerList = document.getElementById("manualProcCornerList");
    const extractBtn = document.getElementById("manualProcExtract");
    const cancelBtn = document.getElementById("manualProcCancel");
    const resetBtn = document.getElementById("manualProcReset");
    const resultBlock = document.getElementById("manualProcResultBlock");

    if (title && exp) {
      const fil = app.commands.filamentMeta(exp.variable_filament_id);
      const filName = fil
        ? fil.display_name || fil.color_name
        : exp.variable_filament_id;
      title.textContent =
        app.state.processing._manualProc.context?.context ===
        "reextract-candidate"
          ? `Manual Candidate — ${exp.sample_id}`
          : `Manual Processing — ${exp.sample_id}`;
      if (subtitle) subtitle.textContent = filName;
    }

    if (progress) {
      if (app.state.processing._manualProc.mode === "batch") {
        const done = app.state.processing._manualProc.completed.size;
        const total = app.state.processing._manualProc.queue.length;
        progress.textContent = `${app.state.processing._manualProc.currentIndex + 1} of ${total} (${done} done)`;
      } else {
        progress.textContent = "";
      }
    }

    // Corner list
    if (cornerList) {
      cornerList.innerHTML = app.constants.CORNER_LABELS.map((label, i) => {
        const isSet = i < app.state.processing._manualProc.corners.length;
        const isActive =
          i === app.state.processing._manualProc.corners.length && i < 4;
        const cls = isSet ? " is-set" : isActive ? " is-active" : "";
        const coords = isSet
          ? `(${Math.round(app.state.processing._manualProc.corners[i].x)}, ${Math.round(app.state.processing._manualProc.corners[i].y)})`
          : isActive
            ? "click to place"
            : "—";
        return `<div class="manual-proc-corner-item${cls}">
          <span class="manual-proc-corner-dot" style="background:${app.constants.CORNER_COLORS[i]}"></span>
          <span>${label}</span>
          <span class="mono small-copy" style="margin-left:auto">${coords}</span>
        </div>`;
      }).join("");
    }

    // Extract button: enable only when all 4 corners placed
    if (extractBtn) {
      extractBtn.disabled =
        app.state.processing._manualProc.corners.length < 4 ||
        app.state.processing._manualProc.processing;
      extractBtn.textContent = app.state.processing._manualProc.processing
        ? "Processing\u2026"
        : app.state.processing._manualProc.context?.context ===
            "reextract-candidate"
          ? "Extract Image"
          : "Extract & Process";
    }

    if (cancelBtn) {
      const showCancel =
        app.state.processing._manualProc.processing &&
        !!app.state.processing._manualProc.currentJobId;
      cancelBtn.style.display = showCancel ? "" : "none";
      cancelBtn.disabled = app.state.processing._manualProc.cancelling;
      cancelBtn.textContent = app.state.processing._manualProc.cancelling
        ? "Cancelling..."
        : "Cancel";
    }

    if (resetBtn) {
      resetBtn.disabled = app.state.processing._manualProc.processing;
    }

    if (app.dom.mpCloseBtn) {
      app.dom.mpCloseBtn.disabled =
        app.state.processing._manualProc.processing ||
        Boolean(app.state.processing._manualProc.currentJobId);
    }

    // Hide result block when not showing results
    if (resultBlock) resultBlock.style.display = "none";

    // Update instructions
    if (app.state.processing._manualProc.corners.length < 4) {
      app.commands._setManualInstructions(
        `Click corner ${app.state.processing._manualProc.corners.length + 1} of 4: ${app.constants.CORNER_LABELS[app.state.processing._manualProc.corners.length]}`,
      );
    } else {
      app.commands._setManualInstructions(
        app.state.processing._manualProc.context?.context ===
          "reextract-candidate"
          ? 'All corners placed. Click "Extract Image" or adjust corners.'
          : 'All corners placed. Click "Extract & Process" or adjust corners.',
      );
    }
  }

  function _setManualInstructions(text) {
    const el = document.getElementById("manualProcInstructions");
    if (el) el.textContent = text;
  }

  function _showManualResult(success, message, stripUrl) {
    const resultBlock = document.getElementById("manualProcResultBlock");
    const resultDiv = document.getElementById("manualProcResult");
    if (!resultBlock || !resultDiv) return;

    resultBlock.style.display = "";
    let html = `<span class="status-pill ${success ? "processed" : "failed"}">${success ? "Success" : "Failed"}</span>`;
    html += `<p class="small-copy" style="margin-top:4px">${message}</p>`;
    if (stripUrl) {
      html += `<div class="manual-proc-result-strip"><img src="${stripUrl}" alt="extracted strip"></div>`;
    }
    resultDiv.innerHTML = html;
  }

  async function _handleManualExtract() {
    const exp = app.commands._currentManualSample();
    if (!exp || app.state.processing._manualProc.corners.length < 4) return;

    app.state.processing._manualProc.processing = true;
    app.commands._updateManualProcUI();
    let candidateContext = null;

    try {
      const corners = app.state.processing._manualProc.corners.map((c) => ({
        x: c.x,
        y: c.y,
      }));
      const orientation =
        exp._orientation_rots != null ? exp._orientation_rots : 0;
      candidateContext =
        app.state.processing._manualProc.context?.context ===
        "reextract-candidate"
          ? app.state.processing._manualProc.context
          : null;

      if (candidateContext) {
        const started = await app.api.startManualReextractCandidateJob(
          candidateContext.candidateSetId,
          exp.sample_id,
          {
            corners,
            orientation,
            preview_width:
              app.state.processing._manualProc.sourceImage.naturalWidth,
            preview_height:
              app.state.processing._manualProc.sourceImage.naturalHeight,
          },
        );
        const jobId = String(started?.job_id || "");
        if (!jobId)
          throw new Error(
            "Manual candidate generation did not return a job id.",
          );
        app.state.processing._manualProc.currentJobId = jobId;
        app.commands._updateManualProcUI();
        const finalJob = await app.commands.pollJobUntilTerminal({
          jobId,
          fetchStatus: () => app.api.fetchReextractJobStatus(jobId),
          isTerminal: (job) =>
            ["succeeded", "failed", "cancelled"].includes(
              String(job.status || ""),
            ),
          shouldContinue: () =>
            app.state.processing._manualProc.currentJobId === jobId &&
            document
              .getElementById("manualProcOverlay")
              ?.classList.contains("is-open"),
          intervalMs: 500,
          onStatus: (job) => {
            const progress = job.progress || {};
            const statusText =
              progress.action_label ||
              job.message ||
              "Generating manual candidate";
            const percent = Number(progress.percent || 0);
            const progressEl = document.getElementById("manualProcProgress");
            if (progressEl)
              progressEl.textContent = `${statusText} · ${Math.max(0, Math.min(100, percent)).toFixed(1)}%`;
          },
          onTransientError: () => {
            const progressEl = document.getElementById("manualProcProgress");
            if (progressEl)
              progressEl.textContent =
                "Connection interrupted; retrying manual extraction status...";
          },
        });
        if (!finalJob) return;
        app.state.processing._manualProc.currentJobId = "";
        app.state.processing._manualProc.processing = false;
        app.state.processing._manualProc.cancelling = false;
        const payload = finalJob?.result || {};
        const candidate = payload?.candidate || null;
        if (finalJob?.status !== "succeeded") {
          if (candidate) {
            await candidateContext.onCandidateComplete?.(candidate);
          }
          throw new Error(
            candidate?.error ||
              finalJob?.error?.message ||
              finalJob?.message ||
              "Manual candidate generation failed.",
          );
        }
        if (candidate?.status === "failed") {
          await candidateContext.onCandidateComplete?.(candidate);
          throw new Error(
            candidate.error || "Manual candidate generation failed.",
          );
        }
        app.state.processing._manualProc.completed.add(exp.sample_id);
        candidateContext.completed = true;
        const stripUrl = candidate?.artifacts?.strip
          ? app.api.reextractCandidateArtifactUrl(
              candidateContext.candidateSetId,
              exp.sample_id,
              "strip",
            )
          : "";
        app.commands._showManualResult(
          true,
          "Manual candidate generated.",
          stripUrl,
        );
        await candidateContext.onCandidateComplete?.(candidate);
        return;
      }

      const result = await app.api.apiPost("/process/manual/extract", {
        sample_id: exp.sample_id,
        corners: corners,
        orientation: orientation,
        preview_width:
          app.state.processing._manualProc.sourceImage.naturalWidth,
        preview_height:
          app.state.processing._manualProc.sourceImage.naturalHeight,
        commit: false,
      });

      app.state.processing._manualProc.processing = false;
      app.commands._updateManualProcUI();
      const nSwatches = result.measurements?.swatches?.length || 0;
      app.commands._showManualResult(
        true,
        `Extracted ${nSwatches} swatches.`,
        `/api/process/manual/review/${encodeURIComponent(exp.sample_id)}/strip?t=${Date.now()}`,
      );
    } catch (err) {
      app.state.processing._manualProc.processing = false;
      app.state.processing._manualProc.currentJobId = "";
      app.state.processing._manualProc.cancelling = false;
      app.commands._updateManualProcUI();
      app.commands._showManualResult(
        false,
        err.message || "Extraction failed.",
      );
    }
  }

  async function _handleManualAccept() {
    const exp = app.commands._currentManualSample();
    if (
      app.state.processing._manualProc.context?.context ===
      "reextract-candidate"
    ) {
      app.commands.closeManualProcessing();
      return;
    }
    if (exp) {
      // Commit: re-run extract with commit=true to finalize as "processed", then clear flag
      try {
        const corners = app.state.processing._manualProc.corners.map((c) => ({
          x: c.x,
          y: c.y,
        }));
        const orientation =
          exp._orientation_rots != null ? exp._orientation_rots : 0;
        await app.api.apiPost("/process/manual/extract", {
          sample_id: exp.sample_id,
          corners: corners,
          orientation: orientation,
          preview_width:
            app.state.processing._manualProc.sourceImage.naturalWidth,
          preview_height:
            app.state.processing._manualProc.sourceImage.naturalHeight,
          commit: true,
        });
      } catch (err) {
        app.commands.showImportToast(
          err.message || "Failed to finalize",
          "error",
        );
        return;
      }
      app.state.processing._manualProc.completed.add(exp.sample_id);
    }

    // Move to next sample in batch, or close if single/done
    if (
      app.state.processing._manualProc.mode === "batch" &&
      app.state.processing._manualProc.currentIndex <
        app.state.processing._manualProc.queue.length - 1
    ) {
      app.state.processing._manualProc.currentIndex++;
      app.commands._resetManualCorners();
      app.commands._loadManualProcSample();
    } else {
      app.commands.closeManualProcessing();
    }
  }

  function _handleManualRetry() {
    app.commands._resetManualCorners();
    app.commands._drawManualCanvas();
    app.commands._updateManualProcUI();
    document.getElementById("manualProcResultBlock").style.display = "none";
  }

  Object.assign(app.commands, {
    openManualProcessing,
    closeManualProcessing,
    _resetManualCorners,
    _currentManualSample,
    _loadManualProcSample,
    _drawManualCanvas,
    _updateManualProcUI,
    _setManualInstructions,
    _showManualResult,
    _handleManualExtract,
    _handleManualAccept,
    _handleManualRetry,
  });
}
