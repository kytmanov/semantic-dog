async function fetchJson(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    return null;
  }
  return response.json();
}

async function refreshDashboard() {
  const runtimeStatus = document.getElementById("runtime-status");
  if (!runtimeStatus) {
    return;
  }

  const status = await fetchJson("/status");
  if (status) {
    runtimeStatus.textContent = status.status;
    const filesIndexed = document.getElementById("files-indexed");
    if (filesIndexed) {
      filesIndexed.textContent = status.files_indexed ?? 0;
    }

    const okCount = document.getElementById("count-ok");
    const corruptCount = document.getElementById("count-corrupt");
    const unreadableCount = document.getElementById("count-unreadable");
    if (okCount) {
      okCount.textContent = status.by_status?.ok ?? 0;
    }
    if (corruptCount) {
      corruptCount.textContent = status.by_status?.corrupt ?? 0;
    }
    if (unreadableCount) {
      unreadableCount.textContent = status.by_status?.unreadable ?? 0;
    }
  }

  const scanState = await fetchJson("/api/scan/current");
  if (scanState) {
    const snapshot = scanState.current || scanState.last;
    const currentScan = document.getElementById("current-scan");
    const processed = document.getElementById("scan-processed");
    const total = document.getElementById("scan-total");
    const rate = document.getElementById("scan-rate");
    if (currentScan) {
      currentScan.textContent = snapshot ? snapshot.scan_id : "none";
    }
    if (processed) {
      processed.textContent = snapshot ? snapshot.processed : 0;
    }
    if (total) {
      total.textContent = snapshot ? snapshot.discovered_total : 0;
    }
    if (rate) {
      rate.textContent = snapshot ? Number(snapshot.files_per_sec || 0).toFixed(1) : "0.0";
    }
  }

  const appState = await fetchJson("/api/app");
  if (appState) {
    const bannerState = document.getElementById("banner-state");
    const bannerDetail = document.getElementById("banner-detail");
    if (bannerState && bannerDetail) {
      if (!appState.ready) {
        bannerState.textContent = "Configuration needed";
        bannerDetail.textContent = "The server is not fully configured yet.";
      } else if (scanState?.current) {
        bannerState.textContent = "Scan running";
        bannerDetail.textContent = "A background scan is active. Progress updates refresh automatically.";
      }
    }
  }
}

refreshDashboard();
setInterval(refreshDashboard, 5000);
