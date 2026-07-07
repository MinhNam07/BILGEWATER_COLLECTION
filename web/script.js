(function () {
    "use strict";

    const SYNC_CODE_KEY = "riftbound_sync_code";
    const DEBOUNCE_MS = 300;

    /** @type {Array<object>} */
    let allCards = [];
    /** @type {Map<string, number>} */
    let inventory = new Map();
    let activeFilter = "All";
    let searchQuery = "";
    let ownedOnly = false;
    let syncCode = "";
    let supabase = null;
    let realtimeChannel = null;
    let pendingSync = null;
    /** @type {Set<string>} */
    let pendingDeletes = new Set();
    let settingsModal = null;

    const els = {
        cardRow: document.getElementById("cardRow"),
        totalCardCount: document.getElementById("totalCardCount"),
        noResults: document.getElementById("noResults"),
        loadingMsg: document.getElementById("loadingMsg"),
        searchBar: document.querySelector(".searchBar"),
        ownedOnlyToggle: document.getElementById("ownedOnlyToggle"),
        syncStatus: document.getElementById("syncStatus"),
        settingsBtn: document.getElementById("settingsBtn"),
        syncCodeDisplay: document.getElementById("syncCodeDisplay"),
        syncCodeInput: document.getElementById("syncCodeInput"),
        copySyncCodeBtn: document.getElementById("copySyncCodeBtn"),
        applySyncCodeBtn: document.getElementById("applySyncCodeBtn"),
        settingsFeedback: document.getElementById("settingsFeedback"),
    };

    function getConfig() {
        return window.APP_CONFIG || {};
    }

    function generateSyncCode() {
        const part = () => Math.random().toString(36).slice(2, 6);
        return `${part()}-${part()}`;
    }

    function normalizeSyncCode(raw) {
        return (raw || "").trim().toLowerCase().replace(/\s+/g, "");
    }

    function getOrCreateSyncCode() {
        let code = localStorage.getItem(SYNC_CODE_KEY);
        if (!code) {
            code = generateSyncCode();
            localStorage.setItem(SYNC_CODE_KEY, code);
        }
        return code;
    }

    function setSyncStatus(text, className) {
        els.syncStatus.textContent = text;
        els.syncStatus.className = `sync-status small ${className || ""}`;
    }

    function initSupabase() {
        const cfg = getConfig();
        if (!cfg.supabaseUrl || !cfg.supabaseAnonKey) {
            setSyncStatus("Local only (no Supabase)", "local");
            return null;
        }
        if (!window.supabase) {
            setSyncStatus("Supabase SDK missing", "error");
            return null;
        }
        return window.supabase.createClient(cfg.supabaseUrl, cfg.supabaseAnonKey);
    }

    async function loadInventoryFromSupabase() {
        if (!supabase || !syncCode) return;

        setSyncStatus("Syncing…", "local");
        const { data, error } = await supabase
            .from("inventory")
            .select("card_id, quantity")
            .eq("sync_code", syncCode);

        if (error) {
            console.error("Load inventory error:", error);
            setSyncStatus("Sync error", "error");
            return;
        }

        inventory = new Map();
        for (const row of data || []) {
            if (row.quantity > 0) {
                inventory.set(row.card_id, row.quantity);
            }
        }
        setSyncStatus("Synced", "synced");
        render();
    }

    function scheduleSyncToSupabase() {
        if (!supabase || !syncCode) return;
        clearTimeout(pendingSync);
        pendingSync = setTimeout(flushInventoryToSupabase, DEBOUNCE_MS);
    }

    async function flushInventoryToSupabase() {
        if (!supabase || !syncCode) return;

        setSyncStatus("Saving…", "local");

        const rows = [];
        for (const [card_id, quantity] of inventory.entries()) {
            if (quantity > 0) {
                rows.push({ sync_code: syncCode, card_id, quantity });
            }
        }

        if (rows.length > 0) {
            const { error } = await supabase
                .from("inventory")
                .upsert(rows, { onConflict: "sync_code,card_id" });
            if (error) {
                console.error("Upsert error:", error);
                setSyncStatus("Save error", "error");
                return;
            }
        }

        for (const card_id of pendingDeletes) {
            await supabase
                .from("inventory")
                .delete()
                .eq("sync_code", syncCode)
                .eq("card_id", card_id);
        }
        pendingDeletes.clear();

        setSyncStatus("Synced", "synced");
    }

    function subscribeRealtime() {
        if (!supabase || !syncCode) return;

        if (realtimeChannel) {
            supabase.removeChannel(realtimeChannel);
        }

        realtimeChannel = supabase
            .channel(`inventory:${syncCode}`)
            .on(
                "postgres_changes",
                {
                    event: "*",
                    schema: "public",
                    table: "inventory",
                    filter: `sync_code=eq.${syncCode}`,
                },
                (payload) => {
                    const row = payload.new || payload.old;
                    if (!row) return;
                    if (payload.eventType === "DELETE") {
                        inventory.delete(row.card_id);
                    } else if (row.quantity > 0) {
                        inventory.set(row.card_id, row.quantity);
                    } else {
                        inventory.delete(row.card_id);
                    }
                    render();
                }
            )
            .subscribe();
    }

    function getQuantity(cardId) {
        return inventory.get(cardId) || 0;
    }

    function setQuantity(cardId, qty) {
        const n = Math.max(0, qty);
        const prev = getQuantity(cardId);
        if (n === 0) {
            inventory.delete(cardId);
            if (prev > 0) {
                pendingDeletes.add(cardId);
            }
        } else {
            inventory.set(cardId, n);
            pendingDeletes.delete(cardId);
        }
        updateTotal();
        scheduleSyncToSupabase();
    }

    function updateTotal() {
        let total = 0;
        for (const q of inventory.values()) {
            total += q;
        }
        els.totalCardCount.textContent = `Owned: ${total}`;
    }

    function getFilteredCards() {
        const q = searchQuery.toLowerCase().trim();
        return allCards.filter((card) => {
            if (activeFilter !== "All" && card.card_type !== activeFilter) {
                return false;
            }
            if (ownedOnly && getQuantity(card.id) === 0) {
                return false;
            }
            if (!q) return true;
            const hay = `${card.name} ${card.set_number} ${card.set_code}`.toLowerCase();
            return hay.includes(q);
        });
    }

    function foilBadge(status) {
        if (status === "foil") {
            return '<span class="badge badge-foil rounded-pill">Foil</span>';
        }
        if (status === "nonfoil") {
            return '<span class="badge badge-nonfoil rounded-pill">Non-foil</span>';
        }
        return '<span class="badge badge-nonfoil rounded-pill">Unknown</span>';
    }

    function escapeHtml(str) {
        return String(str)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }

    function renderCard(card) {
        const qty = getQuantity(card.id);
        const ownedClass = qty > 0 ? " owned" : "";
        const price = card.price ? `$${card.price}` : "—";
        const img = card.image_url
            ? `<img src="${escapeHtml(card.image_url)}" alt="" loading="lazy" decoding="async">`
            : `<div class="card-image-placeholder">No image</div>`;

        return `
            <div class="col">
                <article class="card-tile${ownedClass}" data-card-id="${escapeHtml(card.id)}">
                    <div class="card-image-wrap">${img}</div>
                    <div class="card-body-inner">
                        <h2 class="card-name">${escapeHtml(card.name)}</h2>
                        <div class="card-meta d-flex flex-wrap gap-1 align-items-center">
                            ${foilBadge(card.foil_status)}
                            <span>${escapeHtml(card.set_number || card.set_code || "")}</span>
                            <span>· ${escapeHtml(card.card_type)}</span>
                        </div>
                        <div class="card-price">${escapeHtml(price)}</div>
                        <a class="card-link" href="${escapeHtml(card.url)}" target="_blank" rel="noopener noreferrer">Bilgewater →</a>
                        <div class="qty-controls">
                            <button type="button" class="btn btn-outline-secondary qty-btn qty-minus" aria-label="Decrease quantity">−</button>
                            <span class="qty-display${qty > 0 ? " has-qty" : ""}">${qty}</span>
                            <button type="button" class="btn btn-outline-primary qty-btn qty-plus" aria-label="Increase quantity">+</button>
                        </div>
                    </div>
                </article>
            </div>`;
    }

    function render() {
        const filtered = getFilteredCards();
        els.cardRow.innerHTML = filtered.map(renderCard).join("");
        els.noResults.hidden = filtered.length > 0;
        updateTotal();
    }

    function onCardRowClick(e) {
        const tile = e.target.closest(".card-tile");
        if (!tile) return;
        const cardId = tile.dataset.cardId;
        if (!cardId) return;

        if (e.target.closest(".qty-minus")) {
            setQuantity(cardId, getQuantity(cardId) - 1);
            render();
        } else if (e.target.closest(".qty-plus")) {
            setQuantity(cardId, getQuantity(cardId) + 1);
            render();
        }
    }

    function bindFilters() {
        document.querySelectorAll(".filter-button").forEach((btn) => {
            btn.addEventListener("click", () => {
                document.querySelectorAll(".filter-button").forEach((b) => b.classList.remove("active"));
                btn.classList.add("active");
                activeFilter = btn.dataset.filter || "All";
                render();
            });
        });
    }

    function bindSearch() {
        let timer = null;
        els.searchBar.addEventListener("input", () => {
            clearTimeout(timer);
            timer = setTimeout(() => {
                searchQuery = els.searchBar.value;
                render();
            }, 150);
        });
    }

    function bindOwnedToggle() {
        els.ownedOnlyToggle.addEventListener("change", () => {
            ownedOnly = els.ownedOnlyToggle.checked;
            render();
        });
    }

    function showSettingsFeedback(msg, isError) {
        els.settingsFeedback.textContent = msg;
        els.settingsFeedback.className = `small mt-2 mb-0 ${isError ? "text-danger" : "text-success"}`;
    }

    async function applySyncCode(newCode) {
        const normalized = normalizeSyncCode(newCode);
        if (!normalized || normalized.length < 4) {
            showSettingsFeedback("Invalid sync code", true);
            return;
        }
        syncCode = normalized;
        localStorage.setItem(SYNC_CODE_KEY, syncCode);
        els.syncCodeDisplay.value = syncCode;
        els.syncCodeInput.value = "";
        showSettingsFeedback("Sync code applied. Loading collection…", false);
        await loadInventoryFromSupabase();
        subscribeRealtime();
        showSettingsFeedback("Collection loaded!", false);
    }

    function bindSettings() {
        const modalEl = document.getElementById("settingsModal");
        settingsModal = new bootstrap.Modal(modalEl);

        els.settingsBtn.addEventListener("click", () => {
            els.syncCodeDisplay.value = syncCode;
            els.settingsFeedback.textContent = "";
            settingsModal.show();
        });

        els.copySyncCodeBtn.addEventListener("click", async () => {
            try {
                await navigator.clipboard.writeText(syncCode);
                showSettingsFeedback("Copied to clipboard!", false);
            } catch {
                showSettingsFeedback("Copy failed — select and copy manually", true);
            }
        });

        els.applySyncCodeBtn.addEventListener("click", () => {
            applySyncCode(els.syncCodeInput.value);
        });
    }

    async function loadCards() {
        const resp = await fetch("./cards.json");
        if (!resp.ok) throw new Error(`Failed to load cards.json (${resp.status})`);
        const data = await resp.json();
        allCards = data.cards || data;
        if (!Array.isArray(allCards)) {
            throw new Error("Invalid cards.json format");
        }
    }

    async function init() {
        syncCode = getOrCreateSyncCode();
        supabase = initSupabase();

        bindFilters();
        bindSearch();
        bindOwnedToggle();
        bindSettings();
        els.cardRow.addEventListener("click", onCardRowClick);

        try {
            await loadCards();
            els.loadingMsg.hidden = true;
            await loadInventoryFromSupabase();
            subscribeRealtime();
            render();
        } catch (err) {
            console.error(err);
            els.loadingMsg.textContent = "Failed to load cards. Please refresh.";
            els.loadingMsg.classList.add("text-danger");
        }
    }

    document.addEventListener("DOMContentLoaded", init);
})();
