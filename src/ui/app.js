// API Base URL (running locally)
const API_BASE = "";

// Global State
let profiles = {};
let discoveredDbs = {};
let selectedDbs = [];
let statusInterval = null;
let logEventSource = null;
let autoscroll = true;

// DOM Elements
const selectProfile = document.getElementById("select-profile");
const inputProfileName = document.getElementById("input-profile-name");
const btnSaveProfile = document.getElementById("btn-save-profile");
const btnDeleteProfile = document.getElementById("btn-delete-profile");

const awsHost = document.getElementById("aws-host");
const awsPort = document.getElementById("aws-port");
const awsUser = document.getElementById("aws-user");
const awsPass = document.getElementById("aws-pass");
const awsSsl = document.getElementById("aws-ssl");
const btnTestAws = document.getElementById("btn-test-aws");

const azureHost = document.getElementById("azure-host");
const azurePort = document.getElementById("azure-port");
const azureUser = document.getElementById("azure-user");
const azurePass = document.getElementById("azure-pass");
const azureSsl = document.getElementById("azure-ssl");
const btnTestAzure = document.getElementById("btn-test-azure");

const btnDiscoverDbs = document.getElementById("btn-discover-dbs");
const dbSearch = document.getElementById("db-search");
const selectAllDbs = document.getElementById("select-all-dbs");
const dbListBody = document.getElementById("db-list-body");

const inputBatchSize = document.getElementById("input-batch-size");
const chkDryRun = document.getElementById("chk-dry-run");
const chkResume = document.getElementById("chk-resume");
const chkVerifyOnly = document.getElementById("chk-verify-only");
const chkFixMismatches = document.getElementById("chk-fix-mismatches");
const chkExcludeDirectus = document.getElementById("chk-exclude-directus");
const btnStartMigration = document.getElementById("btn-start-migration");
const btnCancelMigration = document.getElementById("btn-cancel-migration");

const globalStatusBadge = document.getElementById("global-status-badge");
const globalStatusText = document.getElementById("global-status-text");
const progressPercent = document.getElementById("progress-percent");
const progressTables = document.getElementById("progress-tables");
const progressFill = document.getElementById("progress-fill");

const statActiveDb = document.getElementById("stat-active-db");
const statActiveTable = document.getElementById("stat-active-table");
const statRowsCopied = document.getElementById("stat-rows-copied");
const statSpeed = document.getElementById("stat-speed");
const statEta = document.getElementById("stat-eta");
const statVerification = document.getElementById("stat-verification");
const btnViewReport = document.getElementById("btn-view-report");

const logTerminal = document.getElementById("log-terminal");
const btnClearLogs = document.getElementById("btn-clear-logs");
const btnAutoscroll = document.getElementById("btn-autoscroll");

const modalOverwrite = document.getElementById("modal-overwrite");
const overwriteMessage = document.getElementById("overwrite-message");
const btnConfirmOverwrite = document.getElementById("btn-confirm-overwrite");
const btnCancelOverwrite = document.getElementById("btn-cancel-overwrite");

const modalReport = document.getElementById("modal-report");
const reportIframe = document.getElementById("report-iframe");
const btnCloseReport = document.getElementById("btn-close-report");

const toast = document.getElementById("toast");
const toastMessage = document.getElementById("toast-message");

// ==========================================
// Initialization & Event Listeners
// ==========================================

document.addEventListener("DOMContentLoaded", () => {
    loadProfilesFromServer();
    setupPasswordToggles();
    setupEventListeners();
    checkActiveMigrationOnLoad();
});

function setupEventListeners() {
    selectProfile.addEventListener("change", handleProfileChange);
    btnSaveProfile.addEventListener("click", handleSaveProfile);
    btnDeleteProfile.addEventListener("click", handleDeleteProfile);
    
    btnTestAws.addEventListener("click", () => testConnectionEndpoint("aws"));
    btnTestAzure.addEventListener("click", () => testConnectionEndpoint("azure"));
    
    btnDiscoverDbs.addEventListener("click", handleDiscoverDbs);
    dbSearch.addEventListener("input", filterDatabasesTable);
    selectAllDbs.addEventListener("change", handleSelectAllDbs);
    
    btnStartMigration.addEventListener("click", () => startMigration(false));
    btnCancelMigration.addEventListener("click", cancelMigration);
    
    btnClearLogs.addEventListener("click", () => { logTerminal.innerHTML = ""; });
    btnAutoscroll.addEventListener("click", toggleAutoscroll);
    
    btnConfirmOverwrite.addEventListener("click", () => {
        modalOverwrite.classList.remove("show");
        startMigration(true);
    });
    btnCancelOverwrite.addEventListener("click", () => modalOverwrite.classList.remove("show"));
    
    btnViewReport.addEventListener("click", showReportModal);
    btnCloseReport.addEventListener("click", () => modalReport.classList.remove("show"));
}

function setupPasswordToggles() {
    document.querySelectorAll(".btn-toggle-pass").forEach(btn => {
        btn.addEventListener("click", function() {
            const input = this.previousElementSibling;
            const icon = this.querySelector("i");
            if (input.type === "password") {
                input.type = "text";
                icon.className = "fa-solid fa-eye-slash";
            } else {
                input.type = "password";
                icon.className = "fa-solid fa-eye";
            }
        });
    });
}

// ==========================================
// Toast Notifications
// ==========================================

function showToast(message, type = "info") {
    toastMessage.textContent = message;
    toast.className = `toast show ${type}`;
    
    // Auto hide after 4 seconds
    setTimeout(() => {
        toast.classList.remove("show");
    }, 4000);
}

// ==========================================
// Profiles API Logic
// ==========================================

async function loadProfilesFromServer() {
    try {
        const response = await fetch(`${API_BASE}/api/profiles`);
        profiles = await response.json();
        
        // Save current selection index
        const currentSelection = selectProfile.value;
        
        // Re-populate select dropdown
        selectProfile.innerHTML = '<option value="" disabled selected>-- Select Profile or Create New --</option>';
        Object.keys(profiles).forEach(name => {
            const opt = document.createElement("option");
            opt.value = name;
            opt.textContent = name;
            selectProfile.appendChild(opt);
        });
        
        if (currentSelection && profiles[currentSelection]) {
            selectProfile.value = currentSelection;
        }
    } catch (err) {
        showToast("Error loading profiles from server", "danger");
    }
}

function handleProfileChange() {
    const name = selectProfile.value;
    if (!name || !profiles[name]) return;
    
    const profile = profiles[name];
    inputProfileName.value = name;
    
    // AWS input values
    awsHost.value = profile.aws.host || "";
    awsPort.value = profile.aws.port || 3306;
    awsUser.value = profile.aws.user || "";
    awsPass.value = profile.aws.password || "";
    awsSsl.checked = !!profile.aws.ssl_enabled;
    
    // Azure input values
    azureHost.value = profile.azure.host || "";
    azurePort.value = profile.azure.port || 3306;
    azureUser.value = profile.azure.user || "";
    azurePass.value = profile.azure.password || "";
    azureSsl.checked = !!profile.azure.ssl_enabled;
}

async function handleSaveProfile() {
    const name = inputProfileName.value.trim();
    if (!name) {
        showToast("Please enter a profile name first", "danger");
        return;
    }
    
    const awsConfig = {
        host: awsHost.value.trim(),
        port: parseInt(awsPort.value) || 3306,
        user: awsUser.value.trim(),
        password: awsPass.value,
        ssl_enabled: awsSsl.checked
    };
    
    const azureConfig = {
        host: azureHost.value.trim(),
        port: parseInt(azurePort.value) || 3306,
        user: azureUser.value.trim(),
        password: azurePass.value,
        ssl_enabled: azureSsl.checked
    };
    
    if (!awsConfig.host || !azureConfig.host) {
        showToast("AWS and Azure hosts are required to save profile", "danger");
        return;
    }
    
    // If password input is masked and we're editing a profile, keep original password
    const selectVal = selectProfile.value;
    if (selectVal && profiles[selectVal] && name === selectVal) {
        if (awsConfig.password === "********") {
            awsConfig.password = profiles[selectVal].aws.password;
        }
        if (azureConfig.password === "********") {
            azureConfig.password = profiles[selectVal].azure.password;
        }
    }

    try {
        const response = await fetch(`${API_BASE}/api/profiles`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name, aws: awsConfig, azure: azureConfig })
        });
        
        const resData = await response.json();
        if (response.ok) {
            showToast("Connection profile saved successfully!", "success");
            await loadProfilesFromServer();
            selectProfile.value = name;
        } else {
            showToast(resData.error || "Failed to save profile", "danger");
        }
    } catch (err) {
        showToast("Error communicating with server", "danger");
    }
}

async function handleDeleteProfile() {
    const name = selectProfile.value;
    if (!name) {
        showToast("Please select a profile to delete", "danger");
        return;
    }
    
    if (!confirm(`Are you sure you want to delete profile "${name}"?`)) return;
    
    try {
        const response = await fetch(`${API_BASE}/api/profiles/${encodeURIComponent(name)}`, {
            method: "DELETE"
        });
        
        if (response.ok) {
            showToast("Profile deleted", "success");
            inputProfileName.value = "";
            awsHost.value = "";
            awsPass.value = "";
            azureHost.value = "";
            azurePass.value = "";
            await loadProfilesFromServer();
        } else {
            showToast("Failed to delete profile", "danger");
        }
    } catch (err) {
        showToast("Error communicating with server", "danger");
    }
}

// ==========================================
// Connection Test Logic
// ==========================================

async function testConnectionEndpoint(target) {
    const profileName = selectProfile.value || null;
    
    let config = null;
    if (target === "aws") {
        config = {
            host: awsHost.value.trim(),
            port: parseInt(awsPort.value) || 3306,
            user: awsUser.value.trim(),
            password: awsPass.value,
            ssl_enabled: awsSsl.checked
        };
    } else {
        config = {
            host: azureHost.value.trim(),
            port: parseInt(azurePort.value) || 3306,
            user: azureUser.value.trim(),
            password: azurePass.value,
            ssl_enabled: azureSsl.checked
        };
    }
    
    const testBtn = target === "aws" ? btnTestAws : btnTestAzure;
    testBtn.disabled = true;
    testBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Testing';
    
    try {
        const response = await fetch(`${API_BASE}/api/connect/test`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ config, profile_name: profileName, target })
        });
        
        const data = await response.json();
        if (data.success) {
            showToast(`${target.toUpperCase()} connection successful!`, "success");
        } else {
            showToast(`${target.toUpperCase()} connection failed: ${data.message}`, "danger");
        }
    } catch (err) {
        showToast("Error calling connection test API", "danger");
    } finally {
        testBtn.disabled = false;
        testBtn.innerHTML = '<i class="fa-solid fa-circle-check"></i> Test';
    }
}

// ==========================================
// Database Discovery Logic
// ==========================================

async function handleDiscoverDbs() {
    const profileName = selectProfile.value || null;
    const config = {
        host: awsHost.value.trim(),
        port: parseInt(awsPort.value) || 3306,
        user: awsUser.value.trim(),
        password: awsPass.value,
        ssl_enabled: awsSsl.checked
    };
    
    btnDiscoverDbs.disabled = true;
    btnDiscoverDbs.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Discovering';
    
    try {
        const response = await fetch(`${API_BASE}/api/databases`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ config, profile_name: profileName })
        });
        
        if (response.ok) {
            discoveredDbs = await response.json();
            renderDatabasesTable();
            showToast(`Discovered ${Object.keys(discoveredDbs).length} databases`, "success");
            btnStartMigration.disabled = false;
        } else {
            const errData = await response.json();
            showToast(errData.error || "Database discovery failed", "danger");
        }
    } catch (err) {
        showToast("Server error during database discovery", "danger");
    } finally {
        btnDiscoverDbs.disabled = false;
        btnDiscoverDbs.innerHTML = '<i class="fa-solid fa-magnifying-glass"></i> Discover';
    }
}

let loadedTables = {};

function renderDatabasesTable() {
    dbListBody.innerHTML = "";
    loadedTables = {};
    const dbNames = Object.keys(discoveredDbs);
    
    if (dbNames.length === 0) {
        dbListBody.innerHTML = `
            <tr>
                <td colspan="5" class="empty-state">
                    <i class="fa-solid fa-folder-open"></i>
                    <p>No databases discovered on host.</p>
                </td>
            </tr>
        `;
        return;
    }
    
    dbNames.forEach(name => {
        const db = discoveredDbs[name];
        const sizeMb = (db.size_bytes / (1024 * 1024)).toFixed(2);
        
        const row = document.createElement("tr");
        row.innerHTML = `
            <td><input type="checkbox" class="db-row-chk" value="${name}"></td>
            <td class="db-name-cell">
                <button class="btn-expand-db" data-db="${name}"><i class="fa-solid fa-chevron-right"></i></button>
                <strong>${name}</strong>
            </td>
            <td>${sizeMb} MB</td>
            <td>${db.table_count}</td>
            <td>${db.procedure_count + db.function_count}</td>
        `;
        
        // Collapsible nested tables subrow
        const subRow = document.createElement("tr");
        subRow.className = "db-tables-row";
        subRow.id = `tables-row-${name}`;
        subRow.style.display = "none";
        subRow.innerHTML = `
            <td></td>
            <td colspan="4">
                <div class="nested-tables-container">
                    <div class="nested-loading" id="nested-loading-${name}"><i class="fa-solid fa-spinner fa-spin"></i> Loading tables...</div>
                    <div class="nested-tables-grid" id="nested-grid-${name}" style="display: none;"></div>
                </div>
            </td>
        `;
        
        // Expand click listener
        row.querySelector(".btn-expand-db").addEventListener("click", function(e) {
            e.stopPropagation();
            toggleDatabaseTables(name, this);
        });
        
        // Checkbox click listener
        row.querySelector(".db-row-chk").addEventListener("change", handleRowCheckChange);
        
        dbListBody.appendChild(row);
        dbListBody.appendChild(subRow);
    });
}

async function toggleDatabaseTables(dbName, btn) {
    const subRow = document.getElementById(`tables-row-${dbName}`);
    if (!subRow) return;
    
    const isCollapsed = subRow.style.display === "none";
    if (isCollapsed) {
        subRow.style.display = "";
        btn.classList.add("expanded");
        btn.innerHTML = '<i class="fa-solid fa-chevron-down"></i>';
        
        if (!loadedTables[dbName]) {
            await loadDatabaseTablesFromServer(dbName);
        }
    } else {
        subRow.style.display = "none";
        btn.classList.remove("expanded");
        btn.innerHTML = '<i class="fa-solid fa-chevron-right"></i>';
    }
}

async function loadDatabaseTablesFromServer(dbName) {
    const loadingEl = document.getElementById(`nested-loading-${dbName}`);
    const gridEl = document.getElementById(`nested-grid-${dbName}`);
    const dbRowChk = dbListBody.querySelector(`tr:not(.db-tables-row) .db-row-chk[value="${dbName}"]`);
    const isDbChecked = dbRowChk ? dbRowChk.checked : false;
    
    const profileName = selectProfile.value || null;
    const config = {
        host: awsHost.value.trim(),
        port: parseInt(awsPort.value) || 3306,
        user: awsUser.value.trim(),
        password: awsPass.value,
        ssl_enabled: awsSsl.checked
    };
    
    try {
        const response = await fetch(`${API_BASE}/api/databases/${encodeURIComponent(dbName)}/tables`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ config, profile_name: profileName })
        });
        
        if (response.ok) {
            const tables = await response.json();
            loadedTables[dbName] = tables;
            
            gridEl.innerHTML = "";
            if (tables.length === 0) {
                gridEl.innerHTML = '<div class="empty-state" style="grid-column: 1/-1; padding: 10px !important;">No tables found in database.</div>';
            } else {
                tables.forEach(t => {
                    const item = document.createElement("div");
                    item.className = "nested-table-item";
                    const sizeMb = (t.size_bytes / (1024 * 1024)).toFixed(2);
                    
                    item.innerHTML = `
                        <input type="checkbox" class="table-chk" data-db="${dbName}" value="${t.name}" ${isDbChecked ? 'checked' : ''}>
                        <div class="nested-table-details">
                            <span class="nested-table-name" title="${t.name}">${t.name}</span>
                            <span class="nested-table-meta">${t.rows.toLocaleString()} rows • ${sizeMb} MB</span>
                        </div>
                    `;
                    
                    item.querySelector(".table-chk").addEventListener("change", () => handleTableCheckChange(dbName));
                    gridEl.appendChild(item);
                });
            }
            loadingEl.style.display = "none";
            gridEl.style.display = "grid";
        } else {
            const errData = await response.json();
            loadingEl.innerHTML = `<i class="fa-solid fa-triangle-exclamation" style="color: var(--danger);"></i> Failed: ${errData.error}`;
        }
    } catch (err) {
        loadingEl.innerHTML = '<i class="fa-solid fa-triangle-exclamation" style="color: var(--danger);"></i> Error loading tables.';
    }
}

function handleTableCheckChange(dbName) {
    const subRow = document.getElementById(`tables-row-${dbName}`);
    const dbRowChk = dbListBody.querySelector(`tr:not(.db-tables-row) .db-row-chk[value="${dbName}"]`);
    if (!subRow || !dbRowChk) return;
    
    const checkedTableChks = subRow.querySelectorAll(".table-chk:checked");
    if (checkedTableChks.length > 0) {
        dbRowChk.checked = true;
    } else {
        dbRowChk.checked = false;
    }
    
    updateSelectedDbsArray();
}

function filterDatabasesTable() {
    const q = dbSearch.value.toLowerCase().trim();
    const rows = dbListBody.querySelectorAll("tr:not(.db-tables-row)");
    
    rows.forEach(row => {
        const nameCell = row.querySelector(".db-name-cell strong");
        const dbName = nameCell ? nameCell.textContent.toLowerCase() : "";
        const subRow = row.nextElementSibling;
        
        if (dbName.includes(q)) {
            row.style.display = "";
            // Keep subrow expanded state matching arrow class
            const expandBtn = row.querySelector(".btn-expand-db");
            if (expandBtn && expandBtn.classList.contains("expanded")) {
                subRow.style.display = "";
            }
        } else {
            row.style.display = "none";
            subRow.style.display = "none";
        }
    });
}

function handleSelectAllDbs() {
    const checked = selectAllDbs.checked;
    const visibleChks = dbListBody.querySelectorAll("tr:not([style*='display: none']) .db-row-chk");
    
    visibleChks.forEach(chk => {
        chk.checked = checked;
        const dbName = chk.value;
        const subRow = document.getElementById(`tables-row-${dbName}`);
        if (subRow) {
            subRow.querySelectorAll(".table-chk").forEach(tChk => {
                tChk.checked = checked;
            });
        }
    });
    
    updateSelectedDbsArray();
}

function handleRowCheckChange() {
    const dbName = this.value;
    const checked = this.checked;
    const subRow = document.getElementById(`tables-row-${dbName}`);
    if (subRow) {
        subRow.querySelectorAll(".table-chk").forEach(tChk => {
            tChk.checked = checked;
        });
    }
    updateSelectedDbsArray();
}

function updateSelectedDbsArray() {
    const chks = dbListBody.querySelectorAll("tr:not(.db-tables-row) .db-row-chk");
    selectedDbs = [];
    chks.forEach(chk => {
        if (chk.checked) {
            selectedDbs.push(chk.value);
        }
    });
    btnStartMigration.disabled = selectedDbs.length === 0;
}

function buildMigrationPayload() {
    const payload = {};
    const dbRows = dbListBody.querySelectorAll("tr:not(.db-tables-row)");
    
    dbRows.forEach(row => {
        const chk = row.querySelector(".db-row-chk");
        if (!chk) return;
        const dbName = chk.value;
        
        const subRow = document.getElementById(`tables-row-${dbName}`);
        const tableChks = subRow.querySelectorAll(".table-chk");
        const checkedTableChks = subRow.querySelectorAll(".table-chk:checked");
        
        if (chk.checked || checkedTableChks.length > 0) {
            const selectedTables = [];
            // If sub-tables are loaded and not all of them are selected, capture the selected ones.
            // If all are checked or none are loaded, we pass empty array (which means migrate all tables)
            if (tableChks.length > 0 && checkedTableChks.length < tableChks.length) {
                checkedTableChks.forEach(tChk => {
                    selectedTables.push(tChk.value);
                });
            }
            payload[dbName] = selectedTables;
        }
    });
    return payload;
}

// ==========================================
// Migration Core Logic
// ==========================================

async function startMigration(confirmOverwrite = false) {
    if (selectedDbs.length === 0) {
        showToast("Please select at least one database to migrate", "danger");
        return;
    }
    
    const profileName = selectProfile.value;
    if (!profileName) {
        showToast("Please select/save a profile to associate credentials", "danger");
        return;
    }
    
    const payload = {
        profile_name: profileName,
        databases: buildMigrationPayload(),
        dry_run: chkDryRun.checked,
        resume: chkResume.checked,
        verify_only: chkVerifyOnly.checked,
        fix_mismatches: chkFixMismatches.checked,
        exclude_directus: chkExcludeDirectus.checked,
        batch_size: parseInt(inputBatchSize.value) || 5000,
        confirm_overwrite: confirmOverwrite
    };
    
    btnStartMigration.disabled = true;
    btnCancelMigration.disabled = false;
    
    try {
        const response = await fetch(`${API_BASE}/api/migrate/start`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        
        const data = await response.json();
        
        if (data.warning === "database_exists") {
            overwriteMessage.textContent = data.message;
            modalOverwrite.classList.add("show");
            btnStartMigration.disabled = false;
            btnCancelMigration.disabled = true;
        } else if (response.ok) {
            showToast("Migration process spawned!", "success");
            logTerminal.innerHTML = "";
            connectLogStream();
            startPollingStatus();
        } else {
            showToast(data.error || "Failed to start migration", "danger");
            btnStartMigration.disabled = false;
            btnCancelMigration.disabled = true;
        }
    } catch (err) {
        showToast("Error starting migration", "danger");
        btnStartMigration.disabled = false;
        btnCancelMigration.disabled = true;
    }
}

async function cancelMigration() {
    if (!confirm("Are you sure you want to stop/abort the current migration?")) return;
    
    try {
        const response = await fetch(`${API_BASE}/api/migrate/cancel`, {
            method: "POST"
        });
        if (response.ok) {
            showToast("Cancellation command sent", "info");
        } else {
            const data = await response.json();
            showToast(data.error || "Failed to cancel migration", "danger");
        }
    } catch (err) {
        showToast("Error cancelling migration", "danger");
    }
}

// ==========================================
// Live SSE Logger Stream
// ==========================================

function connectLogStream() {
    if (logEventSource) {
        logEventSource.close();
    }
    
    logEventSource = new EventSource(`${API_BASE}/api/logs`);
    
    logEventSource.onmessage = function(e) {
        const msg = e.data;
        if (!msg || msg === ":heartbeat") return;
        
        if (msg.startsWith("LOGS_CONNECTED")) {
            appendLogLine("SYSTEM: Live log streamer connected.", "system");
            return;
        }
        
        // Parse type
        let type = "migration";
        let cleanMsg = msg;
        if (msg.startsWith("MIGRATION:")) {
            type = "migration";
            cleanMsg = msg.substring(10);
        } else if (msg.startsWith("ERROR:")) {
            type = "error";
            cleanMsg = msg.substring(6);
        } else if (msg.startsWith("VERIFICATION:")) {
            type = "verification";
            cleanMsg = msg.substring(13);
        } else if (msg.startsWith("PERFORMANCE:")) {
            type = "performance";
            cleanMsg = msg.substring(12);
        }
        
        appendLogLine(cleanMsg, type);
    };
    
    logEventSource.onerror = function() {
        appendLogLine("SYSTEM: Log connection interrupted. Reconnecting...", "system");
    };
}

function appendLogLine(text, className) {
    const line = document.createElement("div");
    line.className = `log-line ${className}`;
    line.textContent = text;
    
    logTerminal.appendChild(line);
    
    if (autoscroll) {
        logTerminal.scrollTop = logTerminal.scrollHeight;
    }
}

function toggleAutoscroll() {
    autoscroll = !autoscroll;
    btnAutoscroll.classList.toggle("active", autoscroll);
}

// ==========================================
// Migration Status Polling
// ==========================================

function startPollingStatus() {
    if (statusInterval) clearInterval(statusInterval);
    
    statusInterval = setInterval(pollStatus, 800);
}

async function pollStatus() {
    try {
        const response = await fetch(`${API_BASE}/api/migrate/status`);
        const status = await response.json();
        
        updateProgressUI(status);
        
        const state = status.status;
        if (state !== "RUNNING" && state !== "IDLE") {
            // Migration completed (SUCCESS, FAILED, CANCELLED, etc.)
            clearInterval(statusInterval);
            statusInterval = null;
            
            // Clean up controls
            btnStartMigration.disabled = false;
            btnCancelMigration.disabled = true;
            btnViewReport.disabled = false;
            
            if (state === "SUCCESS") {
                showToast("Database migration completed successfully!", "success");
            } else if (state === "FAILED") {
                showToast(`Migration failed: ${status.error_message}`, "danger");
            } else if (state === "CANCELLED") {
                showToast("Migration cancelled by user.", "info");
            }
            
            if (logEventSource) {
                logEventSource.close();
                logEventSource = null;
            }
        }
    } catch (err) {
        console.error("Error polling migration status", err);
    }
}

function updateProgressUI(status) {
    // 1. Status badge
    const state = status.status;
    globalStatusBadge.className = `status-badge ${state.toLowerCase()}`;
    globalStatusText.textContent = state.replace("_", " ");
    
    // 2. Main progress bar
    let pct = 0;
    if (status.tables_total > 0) {
        pct = (status.tables_copied / status.tables_total) * 100;
    }
    pct = Math.min(Math.max(pct, 0), 100);
    
    progressPercent.textContent = `${Math.round(pct)}%`;
    progressTables.textContent = `${status.tables_copied} / ${status.tables_total} Tables`;
    progressFill.style.width = `${pct}%`;
    
    // 3. Stats details
    statActiveDb.textContent = status.database || "-";
    statActiveTable.textContent = status.current_table || "-";
    statRowsCopied.textContent = `${status.current_table_rows_copied.toLocaleString()} / ${status.current_table_rows_total.toLocaleString()}`;
    statSpeed.textContent = `${Math.round(status.speed_rps).toLocaleString()} R/s`;
    
    // ETA formatting
    if (status.eta_seconds && status.eta_seconds !== Infinity) {
        const sec = Math.ceil(status.eta_seconds);
        const h = Math.floor(sec / 3600);
        const m = Math.floor((sec % 3600) / 60);
        const s = sec % 60;
        statEta.textContent = `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
    } else {
        statEta.textContent = "--:--:--";
    }
    
    // Verification
    if (status.status === "RUNNING") {
        statVerification.textContent = "Verifying...";
        statVerification.className = "stat-val warning";
    } else if (status.status === "SUCCESS") {
        statVerification.textContent = "PASSED";
        statVerification.className = "stat-val success";
    } else if (status.status === "FAILED") {
        statVerification.textContent = "FAILED";
        statVerification.className = "stat-val danger";
    } else {
        statVerification.textContent = "-";
        statVerification.className = "stat-val";
    }
}

async function checkActiveMigrationOnLoad() {
    try {
        const response = await fetch(`${API_BASE}/api/migrate/status`);
        const status = await response.json();
        
        if (status.status === "RUNNING") {
            btnStartMigration.disabled = true;
            btnCancelMigration.disabled = false;
            connectLogStream();
            startPollingStatus();
        } else {
            // Check if reports are available to enable view button
            const reportResp = await fetch(`${API_BASE}/api/reports`);
            if (reportResp.ok) {
                btnViewReport.disabled = false;
            }
        }
    } catch (err) {
        console.log("No active migration running on load.");
    }
}

// ==========================================
// Reports Visualizer Logic
// ==========================================

function showReportModal() {
    // Reload iframe src
    reportIframe.src = `${API_BASE}/api/reports/html?t=${Date.now()}`;
    modalReport.classList.add("show");
}
