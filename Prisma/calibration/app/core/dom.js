/** Populate stable Calibration shell references. */
export function initializeDom(app, root = document) {
  app.dom.summaryRail = root.getElementById("summaryRail") || {
    set innerHTML(_) {},
  };
  app.dom.modeSwitch = root.getElementById("modeSwitch");
  app.dom.subtabContainer = root.getElementById("subtabContainer");
  app.dom.subtabRow = root.getElementById("subtabRow");
  app.dom.workspaceRoot = root.querySelector(".workspace");
  app.dom.statusSummary = root.getElementById("statusSummary") || {
    set innerHTML(_) {},
  };
  app.dom.tableSummary = root.getElementById("tableSummary");
  app.dom.tableToolbar = root.getElementById("tableToolbar");
  app.dom.tableContainer = root.getElementById("tableContainer");
  app.dom.detailHeading = root.getElementById("detailHeading");
  app.dom.detailSidebar = root.getElementById("detailSidebar");
  app.dom.detailActionArea = root.getElementById("detailActionArea");
  app.dom.detailWindowArea = root.getElementById("detailWindowArea");
  app.dom.recordDrawer = root.getElementById("recordDrawer");
  app.dom.closeRecordDrawer = root.getElementById("closeRecordDrawer");
  app.dom.drawerStatusPill = root.getElementById("drawerStatusPill");
  app.dom.linkedSampleDrawer = root.getElementById("linkedSampleDrawer");
  app.dom.linkedSampleHeading = root.getElementById("linkedSampleHeading");
  app.dom.linkedSampleStatusPill = root.getElementById(
    "linkedSampleStatusPill",
  );
  app.dom.linkedSampleActionArea = root.getElementById(
    "linkedSampleActionArea",
  );
  app.dom.linkedSampleWindowArea = root.getElementById(
    "linkedSampleWindowArea",
  );
  app.dom.linkedSampleSidebar = root.getElementById("linkedSampleSidebar");
  app.dom.closeLinkedSampleDrawerBtn = root.getElementById(
    "closeLinkedSampleDrawer",
  );
  app.dom.stepBuilderDrawer = root.getElementById("stepBuilderDrawer");
  app.dom.stepBuilderBody = root.getElementById("stepBuilderBody");
  app.dom.stepBundleOptions = root.getElementById("stepBundleOptions");
  app.dom.bundleMgmtDrawer = root.getElementById("bundleMgmtDrawer");
  app.dom.bundleMgmtBody = root.getElementById("bundleMgmtBody");
  app.dom.imageLightboxOverlay = root.getElementById("imageLightboxOverlay");
  app.dom.imageLightboxTitle = root.getElementById("imageLightboxTitle");
  app.dom.imageLightboxImg = root.getElementById("imageLightboxImg");
  app.dom.closeImageLightbox = root.getElementById("closeImageLightbox");
  app.dom.refreshDataBtn = root.getElementById("refreshDataBtn");
  app.dom.maintenanceBtn = root.getElementById("maintenanceBtn");
  app.dom.backupRestoreBtn = root.getElementById("backupRestoreBtn");
  app.dom.publishModelsBtn = root.getElementById("publishModelsBtn");
  app.dom.manualProcCanvas = root.getElementById("manualProcCanvas");
  app.dom.mpResetBtn = root.getElementById("manualProcReset");
  app.dom.mpExtractBtn = root.getElementById("manualProcExtract");
  app.dom.mpCancelBtn = root.getElementById("manualProcCancel");
  app.dom.mpAcceptBtn = root.getElementById("manualProcAccept");
  app.dom.mpRetryBtn = root.getElementById("manualProcRetry");
  app.dom.mpCloseBtn = root.getElementById("closeManualProc");
  app.dom.mpOverlay = root.getElementById("manualProcOverlay");
  return app.dom;
}
