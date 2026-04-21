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
  }

  const scanState = await fetchJson("/api/scan/current");
  if (scanState) {
    const currentScan = document.getElementById("current-scan");
    if (currentScan) {
      currentScan.textContent = scanState.current ? scanState.current.scan_id : "none";
    }
  }
}

refreshDashboard();
setInterval(refreshDashboard, 5000);
