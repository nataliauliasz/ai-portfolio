// --- AUTO-SESSION (wklej NA SAMĄ GÓRĘ app.js) ---
(() => {
  const origFetch = window.fetch;
  window.fetch = async (...args) => {
    const resp = await origFetch(...args);
    try {
      const url = typeof args[0] === "string" ? args[0] : (args[0]?.url || "");
      if (resp.ok && url.includes("/api/upload")) {
        const clone = resp.clone();
        clone.json().then(d => {
          if (d && d.sessionId) {
            window.__SESSION_ID = d.sessionId;
            // opcjonalnie: console.debug("SESSION", window.__SESSION_ID);
          }
        }).catch(() => {});
      }
    } catch {}
    return resp;
  };
})();
(() => {
    const state = {
        sessionId: null,
        pageCount: 0,
        currentPage: 0,
        lineSpacing: 5,
        header: 0,
        footer: 0,
        lastPageData: null,
        jobId: null,
        pollTimer: null,
        clientRecords: [],
        activeClientId: null,
        clientSearch: "",
    };

    const elements = {
        openButton: document.getElementById('openButton'),
        manageClientsButton: document.getElementById('manageClientsButton'),
        fileInput: document.getElementById('fileInput'),
        lineSpacingInput: document.getElementById('lineSpacingInput'),
        headerInput: document.getElementById('headerInput'),
        footerInput: document.getElementById('footerInput'),
        anonymizeButton: document.getElementById('anonymizeButton'),
        prevButton: document.getElementById('prevButton'),
        nextButton: document.getElementById('nextButton'),
        pageIndicator: document.getElementById('pageIndicator'),
        canvasContainer: document.getElementById('canvasContainer'),
        pdfCanvas: document.getElementById('pdfCanvas'),
        overlayCanvas: document.getElementById('overlayCanvas'),
        progressOverlay: document.getElementById('progressOverlay'),
        progressText: document.getElementById('progressText'),
        progressFill: document.getElementById('progressFill'),
        downloadLink: document.getElementById('downloadLink'),
        cancelJobButton: document.getElementById('cancelJobButton'),
        clientsOverlay: document.getElementById('clientsOverlay'),
        closeClientsButton: document.getElementById('closeClientsButton'),
        clientSearchInput: document.getElementById('clientSearchInput'),
        clientsList: document.getElementById('clientsList'),
        clientCanonicalInput: document.getElementById('clientCanonicalInput'),
        clientAliasesInput: document.getElementById('clientAliasesInput'),
        clientPatternsInput: document.getElementById('clientPatternsInput'),
        clientCaseCheckbox: document.getElementById('clientCaseCheckbox'),
        newClientButton: document.getElementById('newClientButton'),
        saveClientButton: document.getElementById('saveClientButton'),
        testClientButton: document.getElementById('testClientButton'),
        clientTestResult: document.getElementById('clientTestResult'),
    };

    const pdfCtx = elements.pdfCanvas.getContext('2d');
    const overlayCtx = elements.overlayCanvas.getContext('2d');

    function clearCanvases() {
        pdfCtx.clearRect(0, 0, elements.pdfCanvas.width, elements.pdfCanvas.height);
        overlayCtx.clearRect(0, 0, elements.overlayCanvas.width, elements.overlayCanvas.height);
    }

    function setControlsEnabled(enabled) {
        elements.lineSpacingInput.disabled = !enabled;
        elements.headerInput.disabled = !enabled;
        elements.footerInput.disabled = !enabled;
        elements.anonymizeButton.disabled = !enabled;
        elements.prevButton.disabled = true;
        elements.nextButton.disabled = true;
        if (elements.manageClientsButton) {
            elements.manageClientsButton.disabled = false;
        }
        if (!enabled) {
            elements.pageIndicator.textContent = 'No document loaded';
            clearCanvases();
        }
    }

    function updateNavigation() {
        const hasDocument = Boolean(state.sessionId);
        elements.prevButton.disabled = !hasDocument || state.currentPage <= 0;
        elements.nextButton.disabled = !hasDocument || state.currentPage >= state.pageCount - 1;
        if (hasDocument) {
            elements.pageIndicator.textContent = `Page ${state.currentPage + 1} / ${state.pageCount}`;
        }
    }

    function parseNumericInput(input, min, max, fallback) {
        const value = Number(input.value);
        if (!Number.isFinite(value)) {
            input.value = fallback;
            return fallback;
        }
        const clamped = Math.min(Math.max(Math.round(value), min), max);
        input.value = clamped;
        return clamped;
    }

    async function startUpload(file) {
        try {
            if (state.sessionId) {
                fetch(`/api/session/${state.sessionId}`, { method: 'DELETE' }).catch(() => {});
            }

            const formData = new FormData();
            formData.append('file', file);

            const response = await fetch('/api/upload', {
                method: 'POST',
                body: formData,
            });
            if (!response.ok) {
                throw new Error(await response.text());
            }
            const data = await response.json();
// zapamiętaj sessionId dla testu klientów w /api/clients/test
            window.__SESSION_ID = data.sessionId;

            state.sessionId = data.sessionId;
            state.pageCount = data.pageCount;
            state.currentPage = 0;
            state.lineSpacing = Number.isFinite(data.lineSpacing) ? data.lineSpacing : 5;
            state.header = Number.isFinite(data.header) ? data.header : 0;
            state.footer = Number.isFinite(data.footer) ? data.footer : 0;
            state.lastPageData = null;

            elements.lineSpacingInput.value = state.lineSpacing;
            elements.headerInput.value = state.header;
            elements.footerInput.value = state.footer;

            setControlsEnabled(true);
            await loadPage(0);
        } catch (error) {
            console.error(error);
            alert(`Failed to upload PDF: ${error.message || error}`);
            resetState();
        }
    }

    function resetState() {
        state.sessionId = null;
        state.pageCount = 0;
        state.currentPage = 0;
        state.lastPageData = null;
        if (state.pollTimer) {
            clearInterval(state.pollTimer);
            state.pollTimer = null;
        }
        state.jobId = null;
        elements.fileInput.value = '';
        setControlsEnabled(false);
    }

    function sortClients(records) {
        return [...records].sort((a, b) => {
            const nameA = (a?.canonical || '').toLowerCase();
            const nameB = (b?.canonical || '').toLowerCase();
            if (nameA < nameB) return -1;
            if (nameA > nameB) return 1;
            return 0;
        });
    }

    function updateClientsListActive() {
        const items = elements.clientsList.querySelectorAll('li');
        items.forEach((item) => {
            if (!item.dataset || !item.dataset.id) {
                item.classList.remove('active');
                return;
            }
            if (item.dataset.id === state.activeClientId) {
                item.classList.add('active');
            } else {
                item.classList.remove('active');
            }
        });
    }

    function clearClientForm() {
        state.activeClientId = null;
        elements.clientCanonicalInput.value = '';
        elements.clientAliasesInput.value = '';
        elements.clientPatternsInput.value = '';
        elements.clientCaseCheckbox.checked = true;
        elements.clientTestResult.textContent = '';
        updateClientsListActive();
    }

    function populateClientForm(record) {
        if (!record) {
            clearClientForm();
            return;
        }
        state.activeClientId = record.id || null;
        elements.clientCanonicalInput.value = record.canonical || '';
        elements.clientAliasesInput.value = Array.isArray(record.aliases) ? record.aliases.join('\n') : '';
        elements.clientPatternsInput.value = Array.isArray(record.patterns) ? record.patterns.join('\n') : '';
        elements.clientCaseCheckbox.checked = record.caseInsensitive !== false;
        elements.clientTestResult.textContent = '';
        updateClientsListActive();
    }

    function renderClientsList() {
        const listElement = elements.clientsList;
        listElement.innerHTML = '';
        const query = state.clientSearch.trim().toLowerCase();
        const approved = state.clientRecords.filter((rec) => (rec.status ?? 'approved') === 'approved');
        const filtered = !query
            ? approved
            : approved.filter((rec) => {
                const terms = [rec.canonical || '', ...(rec.aliases || [])].join(' ').toLowerCase();
                return terms.includes(query);
            });

        if (!filtered.length) {
            const emptyItem = document.createElement('li');
            emptyItem.textContent = 'No clients found';
            emptyItem.classList.add('empty');
            listElement.appendChild(emptyItem);
            return;
        }

        filtered.forEach((rec) => {
            const item = document.createElement('li');
            item.dataset.id = rec.id || '';
            if (rec.id === state.activeClientId) {
                item.classList.add('active');
            }

            const title = document.createElement('div');
            title.textContent = rec.canonical || '(Unnamed client)';
            item.appendChild(title);

            if (rec.aliases && rec.aliases.length) {
                const aliases = document.createElement('div');
                aliases.className = 'client-aliases';
                aliases.textContent = rec.aliases.join(' | ');
                item.appendChild(aliases);
            }

            item.addEventListener('click', () => {
                selectClient(rec.id);
            });

            listElement.appendChild(item);
        });
    }

    async function loadClients() {
        try {
            const response = await fetch('/api/clients');
            if (!response.ok) {
                throw new Error(await response.text());
            }
            const data = await response.json();
            const records = Array.isArray(data.clients) ? data.clients : [];
            state.clientRecords = sortClients(records);
            renderClientsList();
            if (state.activeClientId) {
                const existing = state.clientRecords.find((item) => item.id === state.activeClientId);
                if (existing) {
                    populateClientForm(existing);
                    return;
                }
            }
            if (state.clientRecords.length) {
                populateClientForm(state.clientRecords[0]);
            } else {
                clearClientForm();
            }
        } catch (error) {
            console.error(error);
            alert(`Failed to load clients: ${error.message || error}`);
            state.clientRecords = [];
            renderClientsList();
            clearClientForm();
        }
    }

    function selectClient(clientId) {
        const record = state.clientRecords.find((item) => item.id === clientId);
        populateClientForm(record || null);
    }

    function collectClientForm() {
        const canonical = elements.clientCanonicalInput.value.trim();
        if (!canonical) {
            throw new Error('Canonical name cannot be empty.');
        }
        const aliases = elements.clientAliasesInput.value
            .split(/\r?\n/)
            .map((line) => line.trim())
            .filter((line, index, array) => line && array.indexOf(line) === index);
        const patterns = elements.clientPatternsInput.value
            .split(/\r?\n/)
            .map((line) => line.trim())
            .filter((line, index, array) => line && array.indexOf(line) === index);

        return {
            id: state.activeClientId,
            canonical,
            aliases,
            patterns,
            caseInsensitive: elements.clientCaseCheckbox.checked,
        };
    }

    async function saveClient() {
        let payload;
        try {
            payload = collectClientForm();
        } catch (error) {
            alert(error.message || 'Please complete the required fields.');
            return;
        }

        try {
            const response = await fetch('/api/clients', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json; charset=UTF-8' },
                body: JSON.stringify(payload),
            });
            if (!response.ok) {
                throw new Error(await response.text());
            }
            const data = await response.json();
            const record = data.client;
            state.clientRecords = sortClients([
                ...state.clientRecords.filter((item) => item.id !== record.id),
                record,
            ]);
            state.activeClientId = record.id;
            renderClientsList();
            populateClientForm(record);
            elements.clientTestResult.textContent = 'Client saved.';
        } catch (error) {
            console.error(error);
            alert(`Failed to save client: ${error.message || error}`);
        }
    }

    async function testClient() {
        if (!state.sessionId) {
            alert('Open a PDF before testing a client.');
            return;
        }

        let payload;
        try {
            payload = collectClientForm();
        } catch (error) {
            alert(error.message || 'Please complete the required fields.');
            return;
        }

        elements.clientTestResult.textContent = 'Testing...';
        try {
            const response = await fetch('/api/clients/test', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json; charset=UTF-8' },
                body: JSON.stringify({ ...payload, sessionId: state.sessionId }),
            });
            if (!response.ok) {
                throw new Error(await response.text());
            }
            const data = await response.json();
            elements.clientTestResult.textContent = `Matches found: ${data.matches ?? 0}`;
        } catch (error) {
            console.error(error);
            elements.clientTestResult.textContent = '';
            alert(`Failed to test client: ${error.message || error}`);
        }
    }

    function showClientsDialog() {
        elements.clientsOverlay.classList.remove('hidden');
        elements.clientTestResult.textContent = '';
        elements.clientSearchInput.value = state.clientSearch;
        loadClients();
    }

    function hideClientsDialog() {
        elements.clientsOverlay.classList.add('hidden');
        elements.clientTestResult.textContent = '';
    }

    async function loadPage(pageIndex) {
        if (!state.sessionId) {
            return;
        }
        try {
            const params = new URLSearchParams({
                lineSpacing: String(state.lineSpacing),
                header: String(state.header),
                footer: String(state.footer),
            });
            const response = await fetch(`/api/page/${state.sessionId}/${pageIndex}?${params.toString()}`);
            if (!response.ok) {
                throw new Error(await response.text());
            }
            const data = await response.json();
            state.currentPage = data.page;
            state.pageCount = data.pageCount;
            if (Number.isFinite(data.lineSpacing)) {
                state.lineSpacing = data.lineSpacing;
                elements.lineSpacingInput.value = state.lineSpacing;
            }
            state.lastPageData = data;
            renderPage(data);
            updateNavigation();
        } catch (error) {
            console.error(error);
            alert(`Failed to load page: ${error.message || error}`);
        }
    }

    function renderPage(data) {
        if (!data) {
            clearCanvases();
            return;
        }
        const { width, height } = data;
        if (!width || !height) {
            clearCanvases();
            return;
        }

        const img = new Image();
        img.onload = () => {
            const containerWidth = elements.canvasContainer.clientWidth;
            const containerHeight = elements.canvasContainer.clientHeight;
            const scale = Math.min(containerWidth / width, containerHeight / height) || 1;
            const canvasWidth = Math.round(width * scale);
            const canvasHeight = Math.round(height * scale);

            elements.pdfCanvas.width = canvasWidth;
            elements.pdfCanvas.height = canvasHeight;
            elements.overlayCanvas.width = canvasWidth;
            elements.overlayCanvas.height = canvasHeight;

            elements.pdfCanvas.style.width = `${canvasWidth}px`;
            elements.pdfCanvas.style.height = `${canvasHeight}px`;
            elements.overlayCanvas.style.width = `${canvasWidth}px`;
            elements.overlayCanvas.style.height = `${canvasHeight}px`;

            const offsetX = Math.max((containerWidth - canvasWidth) / 2, 0);
            const offsetY = Math.max((containerHeight - canvasHeight) / 2, 0);
            elements.pdfCanvas.style.left = `${offsetX}px`;
            elements.pdfCanvas.style.top = `${offsetY}px`;
            elements.overlayCanvas.style.left = `${offsetX}px`;
            elements.overlayCanvas.style.top = `${offsetY}px`;


            pdfCtx.clearRect(0, 0, canvasWidth, canvasHeight);
            overlayCtx.clearRect(0, 0, canvasWidth, canvasHeight);
            pdfCtx.drawImage(img, 0, 0, canvasWidth, canvasHeight);

            overlayCtx.lineWidth = Math.max(1, scale * 1.5);
            overlayCtx.strokeStyle = '#ff4d4d';
            overlayCtx.setLineDash([]);
            data.rects.forEach((rect) => {
                overlayCtx.strokeRect(
                    rect.x * scale,
                    rect.y * scale,
                    rect.width * scale,
                    rect.height * scale,
                );
            });

            overlayCtx.strokeStyle = '#2b7de1';
            overlayCtx.setLineDash([8 * scale, 6 * scale]);
            if (Number.isFinite(data.header)) {
                overlayCtx.beginPath();
                overlayCtx.moveTo(0, data.header * scale);
                overlayCtx.lineTo(canvasWidth, data.header * scale);
                overlayCtx.stroke();
            }
            if (Number.isFinite(data.footer)) {
                overlayCtx.beginPath();
                overlayCtx.moveTo(0, data.footer * scale);
                overlayCtx.lineTo(canvasWidth, data.footer * scale);
                overlayCtx.stroke();
            }
        };
        img.src = `data:image/png;base64,${data.image}`;
    }

    function parsePageSpecification(input) {
        const selected = new Set();
        for (const fragment of input.split(',')) {
            const part = fragment.trim();
            if (!part) {
                continue;
            }
            if (part.includes('-')) {
                const [startStr, endStr] = part.split('-', 2).map((value) => value.trim());
                const start = Number(startStr);
                const end = Number(endStr);
                if (!Number.isInteger(start) || !Number.isInteger(end) || start <= 0 || end <= 0 || end < start) {
                    throw new Error('Invalid page range');
                }
                for (let page = start; page <= end; page += 1) {
                    selected.add(page - 1);
                }
            } else {
                const value = Number(part);
                if (!Number.isInteger(value) || value <= 0) {
                    throw new Error('Invalid page number');
                }
                selected.add(value - 1);
            }
        }
        if (!selected.size) {
            throw new Error('No pages selected');
        }
        const pages = Array.from(selected).sort((a, b) => a - b);
        for (const page of pages) {
            if (page < 0 || page >= state.pageCount) {
                throw new Error('Page index out of range');
            }
        }
        return pages;
    }

    function updateProgressBar(value) {
        const limited = Math.min(Math.max(Number(value) || 0, 0), 100);
        elements.progressFill.style.width = `${limited}%`;
    }

    function showProgressOverlay() {
        elements.progressOverlay.classList.remove('hidden');
        elements.progressText.textContent = 'Anonymization in progress...';
        updateProgressBar(0);
        elements.downloadLink.classList.add('hidden');
        elements.downloadLink.href = '#';
        elements.cancelJobButton.disabled = false;
        elements.cancelJobButton.textContent = 'Cancel';
        elements.cancelJobButton.dataset.role = 'cancel';
    }

    function hideProgressOverlay() {
        if (state.pollTimer) {
            clearInterval(state.pollTimer);
            state.pollTimer = null;
        }
        state.jobId = null;
        elements.progressOverlay.classList.add('hidden');
        elements.downloadLink.classList.add('hidden');
        elements.downloadLink.href = '#';
        elements.cancelJobButton.disabled = false;
        elements.cancelJobButton.textContent = 'Cancel';
        elements.cancelJobButton.dataset.role = 'cancel';
    }

    async function pollJob(jobId) {
        try {
            const response = await fetch(`/api/anonymize/${jobId}`);
            if (!response.ok) {
                throw new Error(await response.text());
            }
            const data = await response.json();
            updateProgressBar(data.progress ?? 0);
            switch (data.status) {
                case 'completed':
                    onJobCompleted(data);
                    break;
                case 'error':
                    onJobError(data.error);
                    break;
                case 'cancelled':
                    onJobCancelled();
                    break;
                default:
                    break;
            }
        } catch (error) {
            console.error(error);
            onJobError(error.message || 'Failed to check job status');
        }
    }

    function stopJobPolling() {
        if (state.pollTimer) {
            clearInterval(state.pollTimer);
            state.pollTimer = null;
        }
        state.jobId = null;
    }

    function onJobCompleted(data) {
        stopJobPolling();
        updateProgressBar(100);
        elements.progressText.textContent = 'Anonymization completed.';
        elements.cancelJobButton.textContent = 'Close';
        elements.cancelJobButton.dataset.role = 'close';
        elements.cancelJobButton.disabled = false;
        if (data.downloadUrl) {
            elements.downloadLink.href = data.downloadUrl;
            elements.downloadLink.classList.remove('hidden');
        }
        loadPage(state.currentPage).catch((error) => console.warn('Failed to refresh page', error));
    }

    function onJobError(message) {
        stopJobPolling();
        elements.progressText.textContent = message ? `Error: ${message}` : 'An error occurred during anonymization.';
        elements.cancelJobButton.textContent = 'Close';
        elements.cancelJobButton.dataset.role = 'close';
        elements.cancelJobButton.disabled = false;
        elements.downloadLink.classList.add('hidden');
    }

    function onJobCancelled() {
        stopJobPolling();
        elements.progressText.textContent = 'Anonymization cancelled.';
        elements.cancelJobButton.textContent = 'Close';
        elements.cancelJobButton.dataset.role = 'close';
        elements.cancelJobButton.disabled = false;
        elements.downloadLink.classList.add('hidden');
    }

    async function startAnonymization(pages) {
        if (!state.sessionId) {
            return;
        }
        showProgressOverlay();
        try {
            const response = await fetch('/api/anonymize', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json; charset=UTF-8' },
                body: JSON.stringify({
                    sessionId: state.sessionId,
                    pages,
                    lineSpacing: state.lineSpacing,
                    header: state.header,
                    footer: state.footer,
                }),
            });
            if (!response.ok) {
                throw new Error(await response.text());
            }
            const data = await response.json();
            state.jobId = data.jobId;
            state.pollTimer = setInterval(() => {
                if (state.jobId) {
                    pollJob(state.jobId);
                }
            }, 1000);
            await pollJob(data.jobId);
        } catch (error) {
            console.error(error);
            hideProgressOverlay();
            alert(`Failed to start anonymization: ${error.message || error}`);
        }
    }

    function attachEventListeners() {
        elements.openButton.addEventListener('click', () => {
            elements.fileInput.click();
        });

        elements.manageClientsButton.addEventListener('click', () => {
            showClientsDialog();
        });

        elements.closeClientsButton.addEventListener('click', () => {
            hideClientsDialog();
        });

        elements.clientsOverlay.addEventListener('click', (event) => {
            if (event.target === elements.clientsOverlay) {
                hideClientsDialog();
            }
        });

        elements.clientSearchInput.addEventListener('input', (event) => {
            state.clientSearch = event.target.value;
            renderClientsList();
        });

        elements.newClientButton.addEventListener('click', () => {
            clearClientForm();
            elements.clientCanonicalInput.focus();
        });

        elements.saveClientButton.addEventListener('click', () => {
            saveClient();
        });

        elements.testClientButton.addEventListener('click', () => {
            testClient();
        });

        elements.fileInput.addEventListener('change', (event) => {
            const [file] = event.target.files;
            if (file) {
                startUpload(file);
            }
            event.target.value = '';
        });

        elements.lineSpacingInput.addEventListener('change', () => {
            if (!state.sessionId) {
                return;
            }
            state.lineSpacing = parseNumericInput(elements.lineSpacingInput, 0, 50, state.lineSpacing);
            loadPage(state.currentPage);
        });

        elements.headerInput.addEventListener('change', () => {
            if (!state.sessionId) {
                return;
            }
            state.header = parseNumericInput(elements.headerInput, 0, 2000, state.header);
            loadPage(state.currentPage);
        });

        elements.footerInput.addEventListener('change', () => {
            if (!state.sessionId) {
                return;
            }
            state.footer = parseNumericInput(elements.footerInput, 0, 2000, state.footer);
            loadPage(state.currentPage);
        });

        elements.prevButton.addEventListener('click', () => {
            if (state.sessionId && state.currentPage > 0) {
                loadPage(state.currentPage - 1);
            }
        });

        elements.nextButton.addEventListener('click', () => {
            if (state.sessionId && state.currentPage < state.pageCount - 1) {
                loadPage(state.currentPage + 1);
            }
        });

        elements.anonymizeButton.addEventListener('click', () => {
            if (!state.sessionId) {
                return;
            }
            const input = window.prompt('Enter pages to export (e.g. 1-3,5):');
            if (input === null) {
                return;
            }
            try {
                const pages = parsePageSpecification(input);
                startAnonymization(pages);
            } catch (error) {
                alert(error.message || 'Invalid page specification');
            }
        });

        elements.cancelJobButton.addEventListener('click', async () => {
            const role = elements.cancelJobButton.dataset.role;
            if (role === 'close' || !state.jobId) {
                hideProgressOverlay();
                return;
            }
            elements.cancelJobButton.disabled = true;
            elements.cancelJobButton.textContent = 'Cancelling...';
            try {
                await fetch(`/api/anonymize/${state.jobId}/cancel`, { method: 'POST' });
            } catch (error) {
                console.warn('Failed to cancel job', error);
            }
        });

        window.addEventListener('resize', () => {
            if (state.lastPageData) {
                renderPage(state.lastPageData);
            }
        });
    }

    function init() {
        setControlsEnabled(false);
        attachEventListeners();
    }

    init();
})();

// ==== Manage Clients: bezpieczny patch (doklejany na koniec pliku) ====
(() => {
  if (window.__clientsPatchBound) return; // zapobiega podwójnemu doklejaniu
  window.__clientsPatchBound = true;

  const $ = (id) => document.getElementById(id);

  let clientsCache = [];
  let activeClientId = null;

  function openClients() {
    const overlay = $("clientsOverlay");
    if (overlay) {
      overlay.classList.remove("hidden");
      loadClients();
    }
  }
  function closeClients() {
    const overlay = $("clientsOverlay");
    if (overlay) overlay.classList.add("hidden");
  }

  async function loadClients() {
    try {
      const res = await fetch("/api/clients");
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      clientsCache = data.clients || [];
      renderClientsList();
    } catch (e) {
      console.error(e);
      alert("Nie udało się pobrać listy klientów.");
    }
  }

  function renderClientsList() {
    const list = $("clientsList");
    if (!list) return;

    list.innerHTML = "";
    const q = ($("clientSearchInput")?.value || "").trim().toLowerCase();

    const items = clientsCache.filter((c) => {
      const aliases = (c.aliases || []).join(" | ");
      const row = `${c.canonical} ${aliases}`.toLowerCase();
      return q ? row.includes(q) : true;
    });

    if (!items.length) {
      const li = document.createElement("li");
      li.textContent = "No items";
      li.className = "empty";
      list.appendChild(li);
      return;
    }

    items.forEach((c) => {
      const li = document.createElement("li");
      const name = document.createElement("div");
      name.textContent = c.canonical;

      const aliases = document.createElement("div");
      aliases.className = "client-aliases";
      aliases.textContent = (c.aliases || []).join(" | ");

      li.appendChild(name);
      li.appendChild(aliases);

      li.addEventListener("click", () => {
        Array.from(list.querySelectorAll("li")).forEach(el => el.classList.remove("active"));
        li.classList.add("active");
        loadClientIntoForm(c);
        activeClientId = c.id || null;
      });

      list.appendChild(li);
    });
  }

  function loadClientIntoForm(c) {
    $("clientCanonicalInput").value = c.canonical || "";
    $("clientAliasesInput").value = (c.aliases || []).join("\n");
    $("clientPatternsInput").value = (c.patterns || []).join("\n");
    $("clientCaseCheckbox").checked = !!(c.caseInsensitive ?? true);
  }

  function clearClientForm() {
    activeClientId = null;
    $("clientCanonicalInput").value = "";
    $("clientAliasesInput").value = "";
    $("clientPatternsInput").value = "";
    $("clientCaseCheckbox").checked = true;
  }

  async function saveClient() {
    const payload = {
      id: activeClientId || undefined,
      canonical: $("clientCanonicalInput").value.trim(),
      aliases: $("clientAliasesInput").value.split("\n").map(s => s.trim()).filter(Boolean),
      patterns: $("clientPatternsInput").value.split("\n").map(s => s.trim()).filter(Boolean),
      caseInsensitive: $("clientCaseCheckbox").checked,
      status: "approved"
    };
    if (!payload.canonical) {
      alert("Canonical nie może być puste.");
      return;
    }
    try {
      const res = await fetch("/api/clients", {
        method: "POST",
        headers: { "Content-Type": "application/json; charset=UTF-8" },
        body: JSON.stringify(payload)
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      await loadClients();
      alert("Zapisano klienta.");
    } catch (e) {
      console.error(e);
      alert("Błąd zapisu klienta.");
    }
  }

  async function testClient() {
    const sessionId = window.__SESSION_ID; // ustawiane po /api/upload
    if (!sessionId) {
      alert("Najpierw załaduj PDF (brak sessionId).");
      return;
    }
    const payload = {
      sessionId,
      canonical: $("clientCanonicalInput").value.trim(),
      aliases: $("clientAliasesInput").value.split("\n").map(s => s.trim()).filter(Boolean),
      patterns: $("clientPatternsInput").value.split("\n").map(s => s.trim()).filter(Boolean),
      caseInsensitive: $("clientCaseCheckbox").checked
    };
    if (!payload.canonical) {
      alert("Canonical nie może być puste.");
      return;
    }
    try {
      const res = await fetch("/api/clients/test", {
        method: "POST",
        headers: { "Content-Type": "application/json; charset=UTF-8" },
        body: JSON.stringify(payload)
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      const out = $("clientTestResult");
      if (out) out.textContent = `Matches found: ${data.matches}`;
    } catch (e) {
      console.error(e);
      alert("Błąd testu klienta.");
    }
  }

  // pomocnik do bezpiecznego podpinania eventów
  function once(el, ev, fn) {
    if (!el) return;
    const key = `__bound_${ev}`;
    if (el[key]) return;
    el.addEventListener(ev, fn);
    el[key] = true;
  }

  // Podpięcia zgodnie z Twoim index.html
  once($("manageClientsButton"), "click", openClients);
  once($("closeClientsButton"), "click", closeClients);
  once($("clientSearchInput"), "input", renderClientsList);
  once($("newClientButton"), "click", clearClientForm);
  once($("saveClientButton"), "click", saveClient);
  once($("testClientButton"), "click", testClient);

  // Tooltipy działają CSS-em: .tooltip-target + data-tooltip (patrz index.html/styles.css)
})();
// === PATCH: auto-pozycjonowanie tooltipów dla Chrome/Firefox ===
(() => {
  const CANDIDATES = [
    "manageClientsButton",
    "openButton",
    "anonymizeButton",
    "prevButton",
    "nextButton"
  ];

  function adjustTooltipPlacement(el) {
    if (!el) return;
    const r = el.getBoundingClientRect();
    // jeśli element jest bardzo blisko góry -> pokaż tooltip POD elementem
    const nearTop = r.top < 40;
    el.classList.toggle("tooltip-bottom", !!nearTop);

    // jeśli element jest bardzo blisko lewej krawędzi -> lepiej prawo
    const nearLeft = r.left < 140;
    el.classList.toggle("tooltip-right", !!nearLeft);
  }

  function updateAll() {
    CANDIDATES.forEach(id => adjustTooltipPlacement(document.getElementById(id)));
  }

  // Reaguj na zmiany okna/przewijania i interakcje
  ["resize", "scroll"].forEach(ev => window.addEventListener(ev, updateAll, { passive: true }));
  CANDIDATES.forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    ["mouseenter", "focus"].forEach(ev => el.addEventListener(ev, () => adjustTooltipPlacement(el)));
  });

  // pierwsze wywołanie
  updateAll();
})();
// === PATCH A: auto-pozycjonowanie tooltipów ===
(() => {
  const IDS = ["manageClientsButton","openButton","anonymizeButton","prevButton","nextButton"];

  function adjust(el) {
    if (!el) return;
    const r = el.getBoundingClientRect();
    const nearTop  = r.top  < 40;
    const nearLeft = r.left < 140;
    el.classList.toggle("tooltip-bottom", nearTop);
    el.classList.toggle("tooltip-right",  nearLeft);
  }

  function updateAll() { IDS.forEach(id => adjust(document.getElementById(id))); }
  ["resize","scroll"].forEach(ev => window.addEventListener(ev, updateAll, { passive:true }));
  IDS.forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    ["mouseenter","focus"].forEach(ev => el.addEventListener(ev, () => adjust(el)));
  });
  updateAll();
})();
// === PATCH B: otwieranie/zamykanie dialogu bez ucinania góry ===
(function attachClientsOverlayHelpers(){
  const openBtn  = document.getElementById("manageClientsButton");
  const closeBtn = document.getElementById("closeClientsButton");
  const overlay  = document.getElementById("clientsOverlay");

  async function openClientsOverlay() {
    if (!overlay) return;
    document.body.classList.add("no-scroll");
    overlay.classList.remove("hidden");
    // przewiń overlay i wnętrze dialogu na samą górę (gdyby wcześniej było przewinięte)
    overlay.scrollTop = 0;
    const dialog = overlay.querySelector(".clients-dialog");
    if (dialog) dialog.scrollTop = 0;

    // (jeśli masz funkcję loadClients z wcześniejszego patcha – możesz ją tu zawołać)
    if (typeof loadClients === "function") {
      try { await loadClients(); } catch {}
    }
  }

  function closeClientsOverlay() {
    if (!overlay) return;
    overlay.classList.add("hidden");
    document.body.classList.remove("no-scroll");
  }

  if (openBtn && !openBtn.__boundOpen) {
    openBtn.addEventListener("click", openClientsOverlay);
    openBtn.__boundOpen = true;
  }
  if (closeBtn && !closeBtn.__boundClose) {
    closeBtn.addEventListener("click", closeClientsOverlay);
    closeBtn.__boundClose = true;
  }
})();
