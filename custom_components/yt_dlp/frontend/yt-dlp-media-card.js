const CARD_TAG = "yt-dlp-media-card";

class YtDlpMediaCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._hass = null;
    this._selectedPlayer = "";
    this._selectedPlayers = [];
    this._managedTargets = [];
    this._managedTargetsLoaded = false;
    this._managedTargetsLoading = false;
    this._managedTargetsRetryAt = 0;
    this._managedTargetsLoadedAt = 0;
    this._view = "player";
    this._favoritesTab = "online";
    this._youtubeUrl = "";
    this._musicSearchQuery = "";
    this._musicSearchResults = [];
    this._musicSearchLoading = false;
    this._musicSearchPerformed = false;
    this._musicSearchResultQuery = "";
    this._musicSearchError = "";
    this._favoriteUrl = "";
    this._favorites = [];
    this._favoritesLoaded = false;
    this._favoritesLoading = false;
    this._favoritesPage = 1;
    this._onlineQuery = "";
    this._onlineSelected = new Set();
    this._library = [];
    this._libraryPath = "";
    this._libraryLoaded = false;
    this._libraryLoading = false;
    this._offlineQuery = "";
    this._libraryPage = 1;
    this._offlineSelected = new Set();
    this._pageSize = 20;
    this._nowPlaying = null;
    this._currentLibraryIndex = -1;
    this._currentFavoriteIndex = -1;
    this._repeatMode = "off";
    this._favoritePlaybackLoaded = false;
    this._favoritePlaybackLoading = false;
    this._favoritePlaybackActive = false;
    this._favoritePlaybackKind = null;
    this._favoritePlaybackQueue = [];
    this._favoritePlaybackCursor = 0;
    this._favoritePlaybackCurrent = null;
    this._favoritePlaybackSelectionBound = false;
    this._favoritePlaybackRevision = -1;
    this._favoritePlaybackRetryAt = 0;
    this._favoriteSettingsDirty = false;
    this._favoriteSettingsGeneration = 0;
    this._favoriteSettingsTimer = null;
    this._queue = null;
    this._queueAdvancing = false;
    this._queueStopRequested = false;
    this._queueStartedAt = 0;
    this._activePlayback = null;
    this._interruptedPlayback = null;
    this._interruptionCandidate = null;
    this._interruptionCandidateTimer = null;
    this._resumeIdleTimer = null;
    this._resumeInFlight = false;
    this._speakerSelectionRestored = false;
    this._busy = false;
    this._tickTimer = null;
    this._speakerPickerOpen = false;
    this._speakerMenuScrollTop = 0;
    this._speakerCloseTimer = null;
    this._favoritesViewportRestoreGeneration = 0;

    this._downloadForm = {
      url: "",
      media_type: "video",
      video_quality: "best",
      video_format: "mp4",
      audio_format: "mp3",
      audio_quality: "best",
      overwrite: false,
      wait_for_completion: false,
    };
    this._downloadJob = null;
    this._downloadSubmitting = false;
    this._downloadPollInFlight = false;
    this._downloadPollErrors = 0;
    this._lastDownloadPoll = 0;
    this._downloadNotice = null;
    try {
      const storedJobId = window.localStorage?.getItem("yt_dlp_media_card_active_job");
      if (/^[0-9a-f]{32}$/.test(storedJobId || "")) {
        this._downloadJob = { job_id: storedJobId, status: "queued" };
      }
    } catch (_error) {
      // Browser storage is an optional convenience only.
    }
    try {
      const storedPlayers = JSON.parse(window.localStorage?.getItem("yt_dlp_media_card_players") || "[]");
      if (Array.isArray(storedPlayers)) {
        this._selectedPlayers = storedPlayers.filter((entityId) => typeof entityId === "string" && entityId.startsWith("media_player."));
        this._selectedPlayer = this._selectedPlayers[0] || "";
        this._speakerSelectionRestored = this._selectedPlayers.length > 0;
      }
      const storedRepeat = window.localStorage?.getItem("yt_dlp_media_card_repeat");
      if (["off", "one", "all"].includes(storedRepeat)) this._repeatMode = storedRepeat;
    } catch (_error) {
      // Invalid browser storage is ignored; Home Assistant state remains authoritative.
    }
  }

  static getStubConfig() {
    return {
      title: "Media Center",
      subtitle: "YOUTUBE-DLP",
      color_preset: "cyan",
      background_opacity: 100,
    };
  }

  static getConfigForm() {
    const presets = [
      ["cyan", "Cyan – Xanh ngọc"], ["blue", "Blue – Xanh dương"], ["indigo", "Indigo – Chàm"], ["violet", "Violet – Tím"],
      ["magenta", "Magenta – Tím hồng"], ["rose", "Rose – Hồng"], ["orange", "Orange – Cam"], ["amber", "Amber – Hổ phách"],
      ["green", "Green – Xanh lá"], ["teal", "Teal – Xanh lục lam"],
    ];
    return {
      schema: [
        { name: "subtitle", selector: { text: {} } },
        { name: "title", selector: { text: {} } },
        {
          name: "color_preset",
          selector: {
            select: {
              mode: "dropdown",
              options: presets.map(([value, label]) => ({ value, label })),
            },
          },
        },
        {
          name: "background_opacity",
          selector: {
            number: {
              min: 0,
              max: 100,
              step: 5,
              unit_of_measurement: "%",
              mode: "slider",
            },
          },
        },
      ],
      computeLabel: (schema) => ({
        subtitle: "Dòng tiêu đề trên",
        title: "Tiêu đề thẻ",
        color_preset: "Mẫu màu",
        background_opacity: "Độ trong suốt nền thẻ",
      })[schema.name],
      computeHelper: (schema) => ({
        background_opacity: "Kéo thanh để chỉnh độ đậm của nền thẻ: 0% là trong suốt, 100% là nền đầy đủ.",
      })[schema.name],
    };
  }

  setConfig(config) {
    this._config = {
      title: "Media Center",
      subtitle: "YOUTUBE-DLP",
      color_preset: "cyan",
      background_opacity: 100,
      ...(config || {}),
    };
    // Playback targets are managed in the integration options, not per card.
    this._render();
  }

  set hass(hass) {
    const previous = this._hass;
    const previousSelected = this._selectedPlayer;
    const previousSelection = this._selectedPlayers.join("|");
    const previousState = previous?.states?.[previousSelected];
    const previousPlayers = this._playerSignature(previous);
    this._hass = hass;
    const favoritePlaybackChanged = this._syncFavoritePlaybackFromEntity(hass.states["yt_dlp.favorites_playback"]);
    const players = this._players();

    if (this._managedTargetsLoaded) {
      const allowed = new Set(this._configuredPlayers());
      this._selectedPlayers = this._selectedPlayers.filter((entityId) => allowed.has(entityId));
      if (!this._selectedPlayers.length) {
        const candidates = players.map((player) => player.entity_id);
        const active = candidates.find((entityId) => ["playing", "paused", "buffering"].includes(hass.states[entityId]?.state));
        const available = candidates.find((entityId) => this._targetAvailable(entityId));
        const preferred = active || available || candidates[0] || "";
        this._selectedPlayers = preferred ? [preferred] : [];
      }
      this._selectedPlayer = this._selectedPlayers[0] || "";
      if (this._selectedPlayers.length && (!this._speakerSelectionRestored || previousSelection !== this._selectedPlayers.join("|"))) {
        this._persistSpeakerSelection();
        this._speakerSelectionRestored = true;
      }
    }

    const currentState = hass.states[this._selectedPlayer];
    // Do not compare states from two different speakers when the persisted
    // Favorites session restores its own target player during this hass update.
    // Backend-owned Favorites playback also advances its queue server-side.
    if (!this._favoritePlaybackActive && previousSelected === this._selectedPlayer) {
      this._handlePlayerTransition(previousState, currentState);
    }

    const playerChanged = previousState !== currentState;
    const selectionChanged = previousSelected !== this._selectedPlayer || previousSelection !== this._selectedPlayers.join("|");
    const playersChanged = previousPlayers !== this._playerSignature(hass);
    const shouldRender = !previous || selectionChanged || playersChanged || favoritePlaybackChanged || (this._view !== "download" && playerChanged);
    if (shouldRender) this._render();
    if (
      !this._favoritePlaybackLoaded
      && !this._favoritePlaybackLoading
      && Date.now() >= this._favoritePlaybackRetryAt
    ) {
      queueMicrotask(() => this._loadFavoritePlaybackState());
    }
    if (
      !this._managedTargetsLoading
      && Date.now() >= this._managedTargetsRetryAt
      && (!this._managedTargetsLoaded || Date.now() - this._managedTargetsLoadedAt > 30000)
    ) {
      queueMicrotask(() => this._loadManagedTargets());
    }
  }

  connectedCallback() {
    if (!this._tickTimer) {
      this._tickTimer = window.setInterval(() => this._tick(), 1000);
    }
  }

  disconnectedCallback() {
    if (this._tickTimer) window.clearInterval(this._tickTimer);
    if (this._speakerCloseTimer) window.clearTimeout(this._speakerCloseTimer);
    if (this._interruptionCandidateTimer) window.clearTimeout(this._interruptionCandidateTimer);
    if (this._favoriteSettingsTimer) window.clearTimeout(this._favoriteSettingsTimer);
    this._tickTimer = null;
    this._speakerCloseTimer = null;
    this._interruptionCandidateTimer = null;
    this._favoriteSettingsTimer = null;
  }

  getCardSize() {
    return 8;
  }

  getGridOptions() {
    return {
      columns: 12,
      min_columns: 9,
    };
  }

  _managedTarget(entityId) {
    return this._managedTargets.find((target) => target.entity_id === entityId) || null;
  }

  _targetAvailable(entityId) {
    const state = this._hass?.states?.[entityId];
    return Boolean(state) && !["unavailable", "unknown"].includes(state.state);
  }

  _players() {
    if (!this._hass || !this._managedTargetsLoaded) return [];
    return this._managedTargets
      .map((target) => {
        const state = this._hass.states[target.entity_id];
        return state
          ? { ...state, _ytDlpTarget: target }
          : { entity_id: target.entity_id, state: "unavailable", attributes: {}, _ytDlpTarget: target };
      })
      .sort((a, b) => this._friendlyName(a).localeCompare(this._friendlyName(b)));
  }

  _configuredPlayers() {
    return this._managedTargets
      .map((target) => target?.entity_id)
      .filter((entityId) => typeof entityId === "string" && entityId.startsWith("media_player."));
  }

  _targetPlayers() {
    if (!this._hass || !this._managedTargetsLoaded) return [];
    const allowed = new Set(this._configuredPlayers());
    return [...new Set(this._selectedPlayers)]
      .filter((entityId) => allowed.has(entityId));
  }

  _setSelectedPlayers(entityIds) {
    const previousPrimary = this._selectedPlayer;
    let valid = [...new Set(entityIds)]
      .filter((entityId) => typeof entityId === "string" && this._configuredPlayers().includes(entityId));
    if (!valid.length && this._selectedPlayer && this._configuredPlayers().includes(this._selectedPlayer)) {
      valid = [this._selectedPlayer];
    }
    if (this._selectedPlayer && valid.includes(this._selectedPlayer)) {
      valid = [this._selectedPlayer, ...valid.filter((entityId) => entityId !== this._selectedPlayer)];
    }
    this._selectedPlayers = valid;
    this._selectedPlayer = valid[0] || "";
    this._persistSpeakerSelection();
    if (this._selectedPlayer !== previousPrimary) {
      this._nowPlaying = null;
      this._currentLibraryIndex = -1;
      this._currentFavoriteIndex = -1;
      this._queue = null;
      this._clearPlaybackInterruption();
      this._activePlayback = null;
    }
  }

  _persistSpeakerSelection() {
    try {
      window.localStorage?.setItem("yt_dlp_media_card_players", JSON.stringify(this._selectedPlayers));
    } catch (_error) {
      // Browser persistence is optional; live HA state remains authoritative.
    }
  }

  _persistRepeatMode() {
    try {
      window.localStorage?.setItem("yt_dlp_media_card_repeat", this._repeatMode);
    } catch (_error) {
      // Optional browser convenience only.
    }
  }

  _orderedOnlineSelection() {
    const known = this._favorites.filter((item) => this._onlineSelected.has(item.url)).map((item) => item.url);
    const knownSet = new Set(known);
    return [...known, ...[...this._onlineSelected].filter((url) => !knownSet.has(url))];
  }

  _orderedOfflineSelection() {
    const known = this._library.filter((item) => this._offlineSelected.has(item.id)).map((item) => item.id);
    const knownSet = new Set(known);
    return [...known, ...[...this._offlineSelected].filter((id) => !knownSet.has(id))];
  }

  _applyFavoritePlaybackState(data, forceSettings = false) {
    if (!data || typeof data !== "object") return false;
    const revision = Number(data.revision);
    if (Number.isFinite(revision) && this._favoritePlaybackLoaded && revision < this._favoritePlaybackRevision) return false;

    const before = [
      this._favoritePlaybackActive,
      this._favoritePlaybackKind || "",
      this._favoritePlaybackCursor,
      this._favoritePlaybackQueue.join("\u241f"),
      this._repeatMode,
      [...this._onlineSelected].sort().join("\u241f"),
      [...this._offlineSelected].sort().join("\u241f"),
      this._favoritePlaybackCurrent?.key || "",
    ].join("|");

    if (!this._favoriteSettingsDirty || forceSettings) {
      this._onlineSelected = new Set(Array.isArray(data.online_selected) ? data.online_selected.filter((value) => typeof value === "string") : []);
      this._offlineSelected = new Set(Array.isArray(data.offline_selected) ? data.offline_selected.filter((value) => typeof value === "string") : []);
      if (["off", "one", "all"].includes(data.repeat_mode)) this._repeatMode = data.repeat_mode;
      this._persistRepeatMode();
    }

    this._favoritePlaybackActive = Boolean(data.active);
    this._favoritePlaybackKind = ["online", "offline"].includes(data.kind) ? data.kind : null;
    this._favoritePlaybackQueue = Array.isArray(data.queue) ? data.queue.filter((value) => typeof value === "string") : [];
    this._favoritePlaybackCursor = Math.max(0, Number(data.cursor) || 0);
    this._favoritePlaybackCurrent = data.current && typeof data.current === "object" ? { ...data.current } : null;
    this._favoritePlaybackSelectionBound = Boolean(data.selection_bound);
    if (Number.isFinite(revision)) this._favoritePlaybackRevision = Math.max(this._favoritePlaybackRevision, revision);
    this._favoritePlaybackLoaded = true;

    const sessionPlayers = Array.isArray(data.media_players)
      ? data.media_players.filter((entityId) => typeof entityId === "string" && this._hass?.states?.[entityId])
      : [];
    if (this._favoritePlaybackActive && sessionPlayers.length) {
      this._selectedPlayers = [...new Set(sessionPlayers)];
      this._selectedPlayer = this._selectedPlayers[0] || this._selectedPlayer;
      this._speakerSelectionRestored = true;
      this._persistSpeakerSelection();
    }

    const currentKey = typeof data.current_key === "string" ? data.current_key : this._favoritePlaybackCurrent?.key;
    if (this._favoritePlaybackActive) {
      if (this._favoritePlaybackKind === "online") {
        this._currentFavoriteIndex = this._favorites.findIndex((item) => item.url === currentKey);
        this._currentLibraryIndex = -1;
      } else if (this._favoritePlaybackKind === "offline") {
        this._currentLibraryIndex = this._library.findIndex((item) => item.id === currentKey);
        this._currentFavoriteIndex = -1;
      }
      if (this._favoritePlaybackCurrent) this._nowPlaying = { ...this._favoritePlaybackCurrent };
      this._queue = null;
      this._activePlayback = null;
      this._clearPlaybackInterruption();
    } else {
      this._currentFavoriteIndex = -1;
      this._currentLibraryIndex = -1;
    }

    const after = [
      this._favoritePlaybackActive,
      this._favoritePlaybackKind || "",
      this._favoritePlaybackCursor,
      this._favoritePlaybackQueue.join("\u241f"),
      this._repeatMode,
      [...this._onlineSelected].sort().join("\u241f"),
      [...this._offlineSelected].sort().join("\u241f"),
      this._favoritePlaybackCurrent?.key || "",
    ].join("|");
    return before !== after;
  }

  _syncFavoritePlaybackFromEntity(state) {
    if (!state?.attributes) return false;
    const revision = Number(state.attributes.revision);
    if (Number.isFinite(revision) && this._favoritePlaybackLoaded && revision === this._favoritePlaybackRevision) return false;
    return this._applyFavoritePlaybackState(state.attributes, false);
  }

  async _loadFavoritePlaybackState() {
    if (!this._hass || this._favoritePlaybackLoading || this._favoritePlaybackLoaded) return;
    this._favoritePlaybackLoading = true;
    try {
      const response = await this._callServiceResponse("yt_dlp", "favorites_playback_get");
      this._favoritePlaybackRetryAt = 0;
      const changed = this._applyFavoritePlaybackState(response, false);
      if (changed) this._render();
    } catch (_error) {
      // Avoid hammering the websocket while the integration/config entry is
      // still loading. The HA state entity can synchronize immediately later.
      this._favoritePlaybackRetryAt = Date.now() + 5000;
    } finally {
      this._favoritePlaybackLoading = false;
    }
  }

  _schedulePersistFavoriteSettings() {
    this._favoriteSettingsDirty = true;
    const generation = ++this._favoriteSettingsGeneration;
    this._persistRepeatMode();
    this._persistFavoriteSettings(generation);
  }

  async _persistFavoriteSettings(generation) {
    if (!this._hass) return;
    try {
      const response = await this._callServiceResponse("yt_dlp", "favorites_playback_set", {
        online_selected: this._orderedOnlineSelection(),
        offline_selected: this._orderedOfflineSelection(),
        repeat_mode: this._repeatMode,
      });
      if (generation === this._favoriteSettingsGeneration) {
        this._favoriteSettingsDirty = false;
        this._applyFavoritePlaybackState(response, true);
        this._render();
      }
    } catch (error) {
      if (generation === this._favoriteSettingsGeneration) {
        this._favoriteSettingsDirty = false;
        this._notify(`Không thể lưu lựa chọn Yêu thích: ${error?.message || error}`);
      }
    }
  }

  async _favoritePlaybackCommand(direction) {
    if (!this._favoritePlaybackActive || !this._hass) return;
    this._busy = true;
    this._render();
    try {
      const response = await this._callServiceResponse("yt_dlp", "favorites_playback_skip", { direction });
      this._applyFavoritePlaybackState(response, true);
    } catch (error) {
      this._notify(`Không thể chuyển bài Yêu thích: ${error?.message || error}`);
    } finally {
      this._busy = false;
      this._render();
    }
  }

  _speakerSummary() {
    const selected = this._targetPlayers();
    if (!selected.length) return this._managedTargetsLoaded ? "Chọn thiết bị" : "Đang tải thiết bị…";
    if (selected.length === 1) return this._friendlyNameForEntity(selected[0]);
    return `${this._friendlyNameForEntity(selected[0])} + ${selected.length - 1} thiết bị`;
  }

  _playerSignature(hass) {
    if (!hass?.states || !this._managedTargetsLoaded) return "";
    return this._managedTargets
      .map((target) => `${target.entity_id}:${target.name}:${target.type}:${hass.states[target.entity_id]?.state || "missing"}`)
      .sort()
      .join("|");
  }

  _friendlyName(state) {
    return this._friendlyNameForEntity(state?.entity_id, state);
  }

  _friendlyNameForEntity(entityId, state = undefined) {
    const target = this._managedTarget(entityId);
    const current = state || this._hass?.states?.[entityId];
    return target?.name || current?.attributes?.friendly_name || entityId || "Media player";
  }

  _targetTypeLabel(entityId) {
    const type = this._managedTarget(entityId)?.type;
    return ({ speaker: "Loa", dlna: "DLNA", tv: "TV" })[type] || "Thiết bị";
  }

  _state() {
    if (!this._managedTargetsLoaded || !this._configuredPlayers().includes(this._selectedPlayer)) return null;
    return this._hass?.states?.[this._selectedPlayer] || null;
  }

  _icon(name) {
    return `<ha-icon icon="mdi:${name}"></ha-icon>`;
  }

  _escape(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  _artUrl(state) {
    const picture = state?.attributes?.entity_picture;
    if (picture) {
      if (picture.startsWith("/")) return this._hass?.hassUrl(picture) || picture;
      return picture;
    }
    if (this._nowPlaying?.thumbnail) return this._nowPlaying.thumbnail;
    return "";
  }

  _position(state) {
    const attrs = state?.attributes || {};
    let position = Number(attrs.media_position || 0);
    const duration = Number(attrs.media_duration || 0);
    const updatedAt = attrs.media_position_updated_at;
    if (state?.state === "playing" && updatedAt) {
      const stamp = Date.parse(updatedAt);
      if (Number.isFinite(stamp)) position += Math.max(0, (Date.now() - stamp) / 1000);
    }
    if (duration > 0) position = Math.min(position, duration);
    return { position: Math.max(0, position), duration: Math.max(0, duration) };
  }

  _formatTime(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
    const total = Math.floor(seconds);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
  }

  _durationLabel(seconds) {
    const value = Number(seconds);
    return Number.isFinite(value) && value > 0 ? this._formatTime(value) : "--:--";
  }

  _formatSize(bytes) {
    const value = Number(bytes || 0);
    if (!value) return "";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let n = value;
    let index = 0;
    while (n >= 1024 && index < units.length - 1) {
      n /= 1024;
      index += 1;
    }
    return `${n >= 10 || index === 0 ? n.toFixed(0) : n.toFixed(1)} ${units[index]}`;
  }

  _fileName(path) {
    const value = String(path || "");
    return value.split(/[\\/]/).filter(Boolean).pop() || value;
  }

  _pageData(items, requestedPage) {
    const totalPages = Math.max(1, Math.ceil(items.length / this._pageSize));
    const page = Math.max(1, Math.min(totalPages, Number(requestedPage) || 1));
    const start = (page - 1) * this._pageSize;
    return { page, totalPages, items: items.slice(start, start + this._pageSize), start };
  }

  _renderPagination(kind, page, totalPages) {
    if (totalPages <= 1) return "";
    const pages = [];
    const add = (value) => { if (!pages.includes(value)) pages.push(value); };
    add(1);
    for (let value = Math.max(2, page - 2); value <= Math.min(totalPages - 1, page + 2); value += 1) add(value);
    add(totalPages);
    pages.sort((a, b) => a - b);
    let previous = 0;
    const buttons = pages.map((value) => {
      const gap = previous && value - previous > 1 ? `<span class="page-gap">…</span>` : "";
      previous = value;
      return `${gap}<button data-page-kind="${kind}" data-page="${value}" class="${value === page ? "active" : ""}">${value}</button>`;
    }).join("");
    return `<div class="pagination" aria-label="Phân trang">${buttons}</div>`;
  }

  _formatMediaType(item) {
    const direct = String(item?.file_format || "").trim().replace(/^\./, "");
    if (direct) return direct.toUpperCase();
    const mime = String(item?.mime_type || "").split(";", 1)[0].toLowerCase();
    return ({
      "audio/mp4": "M4A",
      "audio/webm": "WEBM",
      "audio/ogg": "OGG",
      "audio/mpeg": "MP3",
      "audio/flac": "FLAC",
      "audio/wav": "WAV",
      "audio/aac": "AAC",
      "video/mp4": "MP4",
      "video/webm": "WEBM",
    })[mime] || (mime.includes("/") ? mime.split("/").pop().toUpperCase() : "AUDIO");
  }

  _searchArtist(item) {
    return item?.artist || item?.channel || item?.uploader || "Không rõ ca sĩ";
  }

  _searchFormatLabel(item) {
    const direct = String(item?.file_format || "").trim().replace(/^\./, "");
    const mime = String(item?.mime_type || "").trim();
    if (direct || mime) return this._formatMediaType(item);
    // Flat YouTube search intentionally avoids resolving all 20 streams.
    // The exact container is selected lazily only when the item is played or
    // added to Favorites; playback currently prefers M4A/AAC when available.
    return "AUTO";
  }

  _renderMusicSearchResults() {
    if (this._musicSearchLoading) {
      return `<div class="music-search-state loading">${this._icon("loading")} <span>Đang tìm tối đa 20 bài nhạc...</span></div>`;
    }
    if (this._musicSearchError) {
      return `<div class="music-search-state error">${this._icon("alert-circle-outline")} <span>${this._escape(this._musicSearchError)}</span></div>`;
    }
    if (!this._musicSearchPerformed) return "";
    if (!this._musicSearchResults.length) {
      return `<div class="music-search-state">${this._icon("music-off")} <span>Không tìm thấy bài phù hợp.</span></div>`;
    }

    return `
      <div class="music-search-meta">
        <span>Kết quả cho “${this._escape(this._musicSearchResultQuery)}”</span>
        <b>${this._musicSearchResults.length} bài</b>
      </div>
      <div class="music-search-list">
        ${this._musicSearchResults.map((item, index) => {
          const favorited = this._favorites.some((favorite) => favorite.url === item.url);
          return `
            <div class="music-search-row">
              <button class="favorite-main search-main" data-search-play="${index}" title="Phát ${this._escape(item.title || "bài nhạc")}" ${this._busy ? "disabled" : ""}>
                <span class="favorite-thumb ${item.thumbnail ? "has-art" : ""}" style="${item.thumbnail ? `background-image:url('${this._escape(item.thumbnail)}')` : ""}">${item.thumbnail ? "" : this._icon("youtube")}</span>
                <span class="favorite-copy">
                  <strong>${this._escape(item.title || "YouTube audio")}</strong>
                  <small>${this._escape(this._searchArtist(item))} · ${this._durationLabel(item.duration)} · ${this._escape(this._searchFormatLabel(item))}</small>
                </span>
                <span class="row-play">${this._icon("play")}</span>
              </button>
              <button class="search-favorite-btn ${favorited ? "saved" : ""}" data-search-favorite="${index}" title="${favorited ? "Đã có trong Yêu thích" : "Thêm vào Yêu thích"}" ${this._favoritesLoading ? "disabled" : ""}>
                ${this._icon(favorited ? "heart" : "heart-plus-outline")}
              </button>
            </div>`;
        }).join("")}
      </div>`;
  }

  _renderSpeakerPicker(players) {
    return `
      <div class="speaker-row">
        <details class="speaker-picker" id="speakerPicker" ${this._speakerPickerOpen ? "open" : ""}>
          <summary class="field speaker-field" aria-label="Chọn một hoặc nhiều thiết bị phát">
            ${this._icon("speaker-multiple")}
            <span class="speaker-summary">${this._escape(this._speakerSummary())}</span>
            <span class="speaker-count">${this._targetPlayers().length}</span>
            ${this._icon("chevron-down")}
          </summary>
          <div class="speaker-menu">
            <div class="speaker-menu-head">
              <strong>Thiết bị phát đã cấu hình</strong>
              <div>
                <button type="button" id="selectAllSpeakers">Tất cả</button>
                <button type="button" id="clearSpeakers">Chỉ thiết bị chính</button>
                <button type="button" id="closeSpeakerPicker" class="speaker-close" title="Thu gọn danh sách thiết bị" aria-label="Thu gọn danh sách thiết bị">${this._icon("chevron-up")}<span>Thu gọn</span></button>
              </div>
            </div>
            <div class="speaker-options">
              ${players.length ? players.map((player) => `
                <label class="speaker-option">
                  <input type="checkbox" data-speaker-id="${this._escape(player.entity_id)}" ${this._selectedPlayers.includes(player.entity_id) ? "checked" : ""}>
                  <span class="speaker-check">${this._icon("check")}</span>
                  <span class="speaker-name">${this._escape(this._friendlyName(player))} <small>${this._escape(this._targetTypeLabel(player.entity_id))}</small></span>
                </label>`).join("") : `<div class="speaker-empty">${this._managedTargetsLoaded ? "Chưa có thiết bị. Hãy thêm loa/tivi trong cấu hình tích hợp YouTube-DLP." : "Đang tải danh sách thiết bị…"}</div>`}
            </div>
          </div>
        </details>
      </div>`;
  }

  _repeatLabel() {
    return ({ off: "Lặp: Tắt", one: "Lặp 1 bài", all: "Lặp danh sách" })[this._repeatMode] || "Lặp: Tắt";
  }

  _repeatIcon() {
    return this._repeatMode === "one" ? "repeat-once" : "repeat";
  }

  _renderSelectionToolbar(kind, selectedCount, totalCount) {
    const suffix = kind === "online" ? "Online" : "Offline";
    return `
      <div class="selection-toolbar">
        <div class="selection-left">
          <button id="selectAll${suffix}" class="soft-btn small">${this._icon("checkbox-multiple-marked-outline")} Tất cả</button>
          <button id="clear${suffix}" class="soft-btn small" ${selectedCount ? "" : "disabled"}>${this._icon("checkbox-multiple-blank-outline")} Bỏ chọn</button>
          <span>${selectedCount} / ${totalCount} đã chọn</span>
        </div>
        <div class="selection-actions">
          <button data-repeat-mode class="soft-btn small repeat-${this._repeatMode}" title="Đổi chế độ lặp">${this._icon(this._repeatIcon())} ${this._repeatLabel()}</button>
          <button data-stop-favorites class="soft-btn small danger" title="Dừng loa đã chọn">${this._icon("stop")} Dừng</button>
          <button id="playSelected${suffix}" class="accent-btn compact" ${selectedCount && this._selectedPlayer && !this._busy ? "" : "disabled"}>${this._icon("playlist-play")} Phát đã chọn</button>
        </div>
      </div>`;
  }

  _renderFavoritesPlayer(isPlaying) {
    if (!this._favoritePlaybackActive) return "";
    const state = this._state();
    let { position, duration } = this._position(state);
    const savedDuration = Number(this._favoritePlaybackCurrent?.duration || 0);
    if (!(duration > 0) && savedDuration > 0) duration = savedDuration;
    const progress = duration > 0 ? Math.min(100, (position / duration) * 100) : 0;
    const volume = Math.round(Number(state?.attributes?.volume_level ?? 0) * 100);
    const muted = Boolean(state?.attributes?.is_volume_muted);
    const title = state?.attributes?.media_title || this._favoritePlaybackCurrent?.title || "Đang phát từ Yêu thích";
    const artist = state?.attributes?.media_artist || this._favoritePlaybackCurrent?.artist || this._friendlyName(state);
    const sourceLabel = this._favoritePlaybackKind === "offline" ? "Yêu thích · Offline" : "Yêu thích · Online";
    return `
      <section class="favorites-player">
        <div class="favorites-player-head">
          <div class="favorites-player-icon">${this._icon(isPlaying ? "equalizer" : "music-note")}</div>
          <div class="favorites-player-copy">
            <small>${this._escape(sourceLabel)}</small>
            <strong title="${this._escape(title)}">${this._escape(title)}</strong>
            <span>${this._escape(artist || "")}</span>
          </div>
        </div>
        <div class="transport favorites-transport">
          <div class="progress-head"><span id="elapsed">${this._formatTime(position)}</span><span id="duration">${this._formatTime(duration)}</span></div>
          <input id="progress" class="range progress" type="range" min="0" max="${duration || 1}" step="1" value="${Math.min(position, duration || 1)}" style="--value:${progress}%" ${duration ? "" : "disabled"}>
          <div class="controls">
            <button class="icon-btn" id="previous" title="Bài trước">${this._icon("skip-previous")}</button>
            <button class="icon-btn" id="stop" title="Dừng và xóa lựa chọn Yêu thích">${this._icon("stop")}</button>
            <button class="play-btn ${this._busy ? "busy" : ""}" id="playPause" title="Phát / Tạm dừng">
              ${this._busy ? this._icon("loading") : this._icon(isPlaying ? "pause" : "play")}
            </button>
            <button class="icon-btn" id="next" title="Bài tiếp">${this._icon("skip-next")}</button>
            <button class="icon-btn" id="mute" title="Tắt tiếng">${this._icon(muted ? "volume-off" : "volume-high")}</button>
          </div>
          <div class="volume-row">
            ${this._icon("volume-low")}
            <input id="volume" class="range" type="range" min="0" max="100" value="${volume}" style="--value:${volume}%">
            <span>${volume}%</span>
          </div>
        </div>
      </section>`;
  }

  _renderFavoritesView(players, isPlaying) {
    let panel = "";
    if (this._favoritesTab === "online") {
      const query = this._onlineQuery.trim().toLowerCase();
      const filtered = this._favorites
        .map((item, index) => ({ item, index }))
        .filter(({ item }) => !query || `${item.title || ""} ${item.artist || ""} ${item.url || ""}`.toLowerCase().includes(query));
      const page = this._pageData(filtered, this._favoritesPage);
      this._favoritesPage = page.page;
      panel = `
        <section class="panel active" id="favoriteOnlinePanel">
          <div class="favorite-add">
            ${this._icon("link-variant")}
            <input id="favoriteUrl" type="url" value="${this._escape(this._favoriteUrl)}" placeholder="Dán link YouTube để thêm vào Yêu thích..." autocomplete="off">
            <button id="addFavorite" class="accent-btn" ${this._favoritesLoading ? "disabled" : ""}>${this._icon(this._favoritesLoading ? "loading" : "heart-plus-outline")} Thêm</button>
          </div>
          <div class="library-tools favorite-tools">
            <div class="search-box">${this._icon("magnify")}<input id="onlineSearch" value="${this._escape(this._onlineQuery)}" placeholder="Tìm trong Online..."></div>
            <button id="refreshFavorites" class="soft-btn ${this._favoritesLoading ? "spinning" : ""}" title="Tải lại danh sách">${this._icon("refresh")}</button>
          </div>
          ${this._renderSelectionToolbar("online", this._onlineSelected.size, this._favorites.length)}
          <div class="favorite-meta"><span>Danh sách được lưu trong Home Assistant</span><span>${filtered.length} / ${this._favorites.length} bài</span></div>
          <div class="favorite-list">
            ${this._favoritesLoading && !this._favoritesLoaded ? `<div class="empty">${this._icon("loading")} Đang tải danh sách yêu thích...</div>` : ""}
            ${!this._favoritesLoading && this._favoritesLoaded && !filtered.length ? `<div class="empty">${this._icon("heart-outline")} Chưa có bài Online yêu thích.</div>` : ""}
            ${page.items.map(({ item, index }) => {
              const selected = this._onlineSelected.has(item.url);
              const current = index === this._currentFavoriteIndex;
              return `
                <div class="favorite-row ${current ? "selected" : ""}">
                  <button class="check-btn ${selected ? "checked" : ""}" data-online-select="${this._escape(item.url)}" title="Chọn bài">${this._icon(selected ? "checkbox-marked" : "checkbox-blank-outline")}</button>
                  <button class="favorite-main" data-online-index="${index}" title="Phát ${this._escape(item.title || "bài nhạc")}">
                    <span class="favorite-thumb ${item.thumbnail ? "has-art" : ""}" style="${item.thumbnail ? `background-image:url('${this._escape(item.thumbnail)}')` : ""}">${item.thumbnail ? "" : this._icon("youtube")}</span>
                    <span class="favorite-copy">
                      <strong>${this._escape(item.title || "YouTube audio")}</strong>
                      <small>${this._escape(item.artist || "Không rõ ca sĩ")} · ${this._durationLabel(item.duration)} · ${this._escape(this._formatMediaType(item))}</small>
                    </span>
                    <span class="row-play">${this._icon(current && isPlaying ? "equalizer" : "play")}</span>
                  </button>
                  <button class="delete-btn" data-remove-favorite="${this._escape(item.url)}" title="Xóa khỏi Yêu thích">${this._icon("delete-outline")}</button>
                </div>`;
            }).join("")}
          </div>
          ${this._renderPagination("favorites-online", page.page, page.totalPages)}
        </section>`;
    } else {
      const query = this._offlineQuery.trim().toLowerCase();
      const filtered = this._library
        .map((item, index) => ({ item, index }))
        .filter(({ item }) => !query || `${item.title || ""} ${item.artist || ""} ${item.filename || ""}`.toLowerCase().includes(query));
      const page = this._pageData(filtered, this._libraryPage);
      this._libraryPage = page.page;
      panel = `
        <section class="panel active" id="favoriteOfflinePanel">
          <div class="library-tools favorite-tools">
            <div class="search-box">${this._icon("magnify")}<input id="offlineSearch" value="${this._escape(this._offlineQuery)}" placeholder="Tìm trong Offline..."></div>
            <button id="refreshLibrary" class="soft-btn ${this._libraryLoading ? "spinning" : ""}" title="Quét lại thư mục">${this._icon("refresh")}</button>
          </div>
          ${this._renderSelectionToolbar("offline", this._offlineSelected.size, this._library.length)}
          <div class="favorite-meta"><span>${this._escape(this._libraryPath || "Thư mục thư viện")}</span><span>${filtered.length} / ${this._library.length} bài</span></div>
          <div class="favorite-list">
            ${this._libraryLoading && !this._libraryLoaded ? `<div class="empty">${this._icon("loading")} Đang quét thư viện...</div>` : ""}
            ${!this._libraryLoading && this._libraryLoaded && !filtered.length ? `<div class="empty">${this._icon("music-off")} Không tìm thấy file nhạc.</div>` : ""}
            ${page.items.map(({ item, index }) => {
              const selected = this._offlineSelected.has(item.id);
              const current = index === this._currentLibraryIndex;
              return `
                <div class="favorite-row ${current ? "selected" : ""}">
                  <button class="check-btn ${selected ? "checked" : ""}" data-offline-select="${this._escape(item.id)}" title="Chọn bài">${this._icon(selected ? "checkbox-marked" : "checkbox-blank-outline")}</button>
                  <button class="favorite-main" data-offline-index="${index}" title="Phát ${this._escape(item.title || item.filename)}">
                    <span class="favorite-thumb local">${this._icon(current && isPlaying ? "equalizer" : "music-note")}</span>
                    <span class="favorite-copy">
                      <strong>${this._escape(item.title || item.filename)}</strong>
                      <small>${this._escape(item.artist || "Không rõ ca sĩ")} · ${this._durationLabel(item.duration)} · ${this._escape(this._formatMediaType(item))}</small>
                    </span>
                    <span class="row-play">${this._icon(current && isPlaying ? "equalizer" : "play")}</span>
                  </button>
                </div>`;
            }).join("")}
          </div>
          ${this._renderPagination("favorites-offline", page.page, page.totalPages)}
        </section>`;
    }

    return `
      ${this._renderSpeakerPicker(players)}
      ${this._renderFavoritesPlayer(isPlaying)}
      <nav class="tabs secondary-tabs favorite-tabs">
        <button data-favorites-tab="online" class="${this._favoritesTab === "online" ? "active" : ""}">${this._icon("cloud-outline")} <span>Online</span> <b>${this._favoritesLoaded ? this._favorites.length : "…"}</b></button>
        <button data-favorites-tab="offline" class="${this._favoritesTab === "offline" ? "active" : ""}">${this._icon("harddisk")} <span>Offline</span> <b>${this._libraryLoaded ? this._library.length : "…"}</b></button>
      </nav>
      ${panel}`;
  }

  _backgroundOpacity() {
    const value = Number(this._config?.background_opacity);
    return Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 100;
  }

  _presetVars() {
    const presets = {
      cyan: ["#05a8d8", "#18d5e8"],
      blue: ["#2f6fed", "#58a6ff"],
      indigo: ["#4f5bd5", "#7b8cff"],
      violet: ["#7c4dff", "#b26cff"],
      magenta: ["#c13cff", "#ff63c7"],
      rose: ["#e94b7b", "#ff7a9e"],
      orange: ["#f06b2a", "#ff9a43"],
      amber: ["#d99000", "#ffc341"],
      green: ["#1f9d68", "#4fd29a"],
      teal: ["#008f8c", "#36c8bd"],
    };
    const [accent, accent2] = presets[this._config.color_preset] || presets.cyan;
    const opacity = this._backgroundOpacity();
    return `--accent:${accent};--accent2:${accent2};--card-opacity-pct:${opacity}%`;
  }

  _downloadStatusLabel(status) {
    return ({
      queued: "Đang chờ",
      extracting: "Đang lấy thông tin",
      downloading: "Đang tải",
      postprocessing: "Đang xử lý",
      completed: "Hoàn tất",
      error: "Lỗi",
      cancelled: "Đã hủy",
    })[status] || "Đang xử lý";
  }

  _downloadPercent(job) {
    if (!job) return 0;
    const direct = Number(job.progress);
    if (Number.isFinite(direct)) return Math.max(0, Math.min(100, direct));
    const downloaded = Number(job.downloaded_bytes || 0);
    const total = Number(job.total_bytes || 0);
    if (total > 0) return Math.max(0, Math.min(100, (downloaded / total) * 100));
    if (job.status === "completed") return 100;
    if (job.status === "postprocessing") return 99;
    return 0;
  }

  _isDownloadActive() {
    return this._downloadSubmitting || Boolean(this._downloadJob && !["completed", "error", "cancelled"].includes(this._downloadJob.status));
  }

  _renderDownloadNotice() {
    const notice = this._downloadNotice;
    if (!notice) return "";
    const icon = notice.type === "success" ? "check-circle" : notice.type === "error" ? "alert-circle" : "information";
    return `
      <div class="download-notice ${this._escape(notice.type || "info")}">
        <div class="notice-icon">${this._icon(icon)}</div>
        <div class="notice-copy">
          <strong>${this._escape(notice.title || "Thông báo")}</strong>
          <span>${this._escape(notice.message || "")}</span>
        </div>
        <button id="dismissDownloadNotice" class="notice-close" title="Đóng">${this._icon("close")}</button>
      </div>`;
  }

  _renderDownloadPanel() {
    const form = this._downloadForm;
    const job = this._downloadJob;
    const active = this._isDownloadActive();
    const percent = this._downloadPercent(job);
    const statusLabel = this._downloadSubmitting && !job ? "Đang khởi tạo" : this._downloadStatusLabel(job?.status);
    const downloaded = this._formatSize(job?.downloaded_bytes);
    const total = this._formatSize(job?.total_bytes);
    const speed = this._formatSize(job?.speed);
    const eta = Number(job?.eta);
    const resultFile = job?.final_files?.length ? this._fileName(job.final_files[0]) : this._fileName(job?.filename);

    return `
      <section class="panel ${this._view === "download" ? "active" : ""}" id="downloadPanel">
        <div class="download-hero">
          <div class="download-hero-icon">${this._icon("download-circle-outline")}</div>
          <div><strong>Tải YouTube</strong><span>Dùng trực tiếp action yt_dlp.download hiện có</span></div>
        </div>

        <label class="input-shell full">
          <span>${this._icon("link-variant")} Link</span>
          <input id="downloadUrl" type="url" value="${this._escape(form.url)}" placeholder="https://www.youtube.com/watch?v=..." autocomplete="off">
        </label>

        <div class="download-type" role="group" aria-label="Loại media">
          <button data-media-type="video" class="${form.media_type === "video" ? "active" : ""}">${this._icon("movie-open-play")} Video</button>
          <button data-media-type="audio" class="${form.media_type === "audio" ? "active" : ""}">${this._icon("music-note")} Audio</button>
        </div>

        <div class="option-grid">
          ${form.media_type === "video" ? `
            <label class="input-shell"><span>${this._icon("quality-high")} Chất lượng video</span>
              <select id="videoQuality">
                ${["best", "2160", "1440", "1080", "720", "480", "360"].map((value) => `<option value="${value}" ${form.video_quality === value ? "selected" : ""}>${value === "best" ? "Tốt nhất" : `${value}p`}</option>`).join("")}
              </select>
            </label>
            <label class="input-shell"><span>${this._icon("file-video")} Định dạng video</span>
              <select id="videoFormat">
                ${["mp4", "mkv", "webm"].map((value) => `<option value="${value}" ${form.video_format === value ? "selected" : ""}>${value.toUpperCase()}</option>`).join("")}
              </select>
            </label>` : `
            <label class="input-shell"><span>${this._icon("music-box-multiple")} Định dạng audio</span>
              <select id="audioFormat">
                ${["mp3", "m4a", "opus", "flac", "wav"].map((value) => `<option value="${value}" ${form.audio_format === value ? "selected" : ""}>${value.toUpperCase()}</option>`).join("")}
              </select>
            </label>
            <label class="input-shell"><span>${this._icon("waveform")} Chất lượng audio</span>
              <select id="audioQuality">
                ${["best", "320", "256", "192", "128", "96"].map((value) => `<option value="${value}" ${form.audio_quality === value ? "selected" : ""}>${value === "best" ? "Tốt nhất" : `${value} kbps`}</option>`).join("")}
              </select>
            </label>`}
        </div>

        <div class="toggle-list">
          <label class="toggle-row">
            <span><strong>Ghi đè file</strong><small>Cho phép thay thế file đã tồn tại</small></span>
            <input id="overwrite" type="checkbox" ${form.overwrite ? "checked" : ""}><i></i>
          </label>
          <label class="toggle-row">
            <span><strong>Chờ tải hoàn tất</strong><small>Giống trường wait_for_completion của action</small></span>
            <input id="waitForCompletion" type="checkbox" ${form.wait_for_completion ? "checked" : ""}><i></i>
          </label>
        </div>

        ${active || job ? `
          <div id="downloadJobCard" class="job-card ${job?.status === "completed" ? "done" : job?.status === "error" ? "failed" : ""}">
            <div class="job-top">
              <div id="downloadJobIcon" class="job-status-icon ${active ? "working" : ""}">${this._icon(job?.status === "completed" ? "check" : job?.status === "error" ? "alert" : "download")}</div>
              <div class="job-title"><strong id="downloadJobTitle">${this._escape(job?.title || resultFile || "Đang chuẩn bị tải...")}</strong><span id="downloadJobStatus">${this._escape(statusLabel)}</span></div>
              <b id="downloadJobPercent">${Math.round(percent)}%</b>
            </div>
            <div class="download-progress"><i id="downloadProgressBar" style="width:${percent}%"></i></div>
            <div class="job-metrics">
              <span id="downloadBytes">${this._icon("harddisk")} <span>${downloaded || "0 B"}${total ? ` / ${total}` : ""}</span></span>
              <span id="downloadSpeed" class="${job?.speed ? "" : "metric-hidden"}">${this._icon("speedometer")} <span>${job?.speed ? `${speed}/s` : ""}</span></span>
              <span id="downloadEta" class="${Number.isFinite(eta) && eta >= 0 && active ? "" : "metric-hidden"}">${this._icon("timer-outline")} <span>${Number.isFinite(eta) && eta >= 0 && active ? this._formatTime(eta) : ""}</span></span>
            </div>
            ${resultFile && !active ? `<div class="result-file">${this._icon("file-check-outline")}<span>${this._escape(resultFile)}</span></div>` : ""}
            ${job?.error ? `<div class="job-error">${this._icon("alert-circle-outline")} ${this._escape(job.error)}</div>` : ""}
          </div>` : ""}

        <button id="startDownload" class="download-btn ${active ? "busy" : ""}" ${active || !form.url.trim() ? "disabled" : ""}>
          ${active ? this._icon("loading") : this._icon("download")}
          ${active ? "Đang tải..." : "Bắt đầu tải"}
        </button>
      </section>`;
  }

  _composedParent(node) {
    if (!node) return null;
    if (node.parentElement) return node.parentElement;
    const root = node.getRootNode?.();
    return root?.host || null;
  }

  _captureFavoritesViewport() {
    if (this._view !== "favorites" || !this.shadowRoot) return null;
    const list = this.shadowRoot.querySelector(".favorite-list");
    if (!list) return null;

    const scrollers = [];
    const seen = new Set();
    let node = this;
    for (let depth = 0; node && depth < 40; depth += 1) {
      node = this._composedParent(node);
      if (!node || seen.has(node)) continue;
      seen.add(node);
      try {
        const top = Number(node.scrollTop) || 0;
        const left = Number(node.scrollLeft) || 0;
        const scrollHeight = Number(node.scrollHeight) || 0;
        const clientHeight = Number(node.clientHeight) || 0;
        if (top !== 0 || left !== 0 || scrollHeight > clientHeight + 1) {
          scrollers.push({ node, top, left });
        }
      } catch (_error) {
        // Some HA wrapper nodes do not expose writable scroll properties.
      }
    }

    const rootScroller = document.scrollingElement;
    let rootCaptured = scrollers.some((entry) => entry.node === rootScroller);
    if (rootScroller && !rootCaptured) {
      try {
        const top = Number(rootScroller.scrollTop) || 0;
        const left = Number(rootScroller.scrollLeft) || 0;
        if (top !== 0 || left !== 0 || Number(rootScroller.scrollHeight) > Number(rootScroller.clientHeight) + 1) {
          scrollers.push({ node: rootScroller, top, left });
          rootCaptured = true;
        }
      } catch (_error) {
        // Window fallback below covers browsers that hide scrollingElement.
      }
    }

    const rect = list.getBoundingClientRect();
    return {
      kind: this._favoritesTab,
      listTop: Number(rect.top) || 0,
      listScrollTop: Number(list.scrollTop) || 0,
      listScrollLeft: Number(list.scrollLeft) || 0,
      scrollers,
      rootCaptured,
      windowX: Number(window.scrollX) || 0,
      windowY: Number(window.scrollY) || 0,
    };
  }

  _restoreFavoritesViewport(snapshot) {
    if (!snapshot || this._view !== "favorites" || this._favoritesTab !== snapshot.kind) return;
    const generation = ++this._favoritesViewportRestoreGeneration;

    const apply = () => {
      if (generation !== this._favoritesViewportRestoreGeneration) return;
      if (this._view !== "favorites" || this._favoritesTab !== snapshot.kind) return;
      const list = this.shadowRoot?.querySelector(".favorite-list");
      if (!list) return;

      // First undo any focus/scroll-anchor jump caused by replacing the card DOM.
      for (const entry of snapshot.scrollers) {
        try {
          if (entry.node?.isConnected === false) continue;
          entry.node.scrollTop = entry.top;
          entry.node.scrollLeft = entry.left;
        } catch (_error) {
          // Ignore read-only or detached HA wrapper nodes.
        }
      }
      if (!snapshot.rootCaptured) {
        try {
          window.scrollTo(snapshot.windowX, snapshot.windowY);
        } catch (_error) {
          // Embedded WebViews may not expose window scrolling.
        }
      }

      // The Favorites player can appear above the list after pressing Play.
      // Restore the list's own scroll position, then compensate the outer HA
      // viewport for any height inserted above it so the touched row stays put.
      list.scrollTop = snapshot.listScrollTop;
      list.scrollLeft = snapshot.listScrollLeft;
      const newTop = Number(list.getBoundingClientRect().top) || 0;
      let remaining = newTop - snapshot.listTop;
      if (Math.abs(remaining) <= 0.5) return;

      for (const entry of snapshot.scrollers) {
        try {
          if (entry.node?.isConnected === false) continue;
          const before = Number(entry.node.scrollTop) || 0;
          entry.node.scrollTop = entry.top + remaining;
          const consumed = (Number(entry.node.scrollTop) || 0) - entry.top;
          remaining -= consumed;
          if (Math.abs(remaining) <= 0.5) break;
          if (Math.abs((Number(entry.node.scrollTop) || 0) - before) < 0.1 && consumed === 0) continue;
        } catch (_error) {
          // Try the next composed scroll container.
        }
      }

      if (Math.abs(remaining) > 0.5 && !snapshot.rootCaptured) {
        try {
          window.scrollTo(snapshot.windowX, snapshot.windowY + remaining);
        } catch (_error) {
          // No writable page viewport; inner list position is still restored.
        }
      }
    };

    apply();
    // Mobile HA/WebView can apply focus/scroll anchoring after the click handler.
    // Re-apply once on the next frame, but cancel stale restores after a new render.
    window.requestAnimationFrame?.(apply);
  }

  _render() {
    if (!this.shadowRoot) return;
    const favoritesViewport = this._captureFavoritesViewport();
    const state = this._state();
    const players = this._players();
    const isPlaying = state?.state === "playing";
    const isPaused = state?.state === "paused";
    const { position, duration } = this._position(state);
    const progress = duration > 0 ? Math.min(100, (position / duration) * 100) : 0;
    const volume = Math.round(Number(state?.attributes?.volume_level ?? 0) * 100);
    const muted = Boolean(state?.attributes?.is_volume_muted);
    const art = this._artUrl(state);
    const mediaTitle = state?.attributes?.media_title || this._nowPlaying?.title || "Chọn nội dung để phát";
    const mediaArtist = state?.attributes?.media_artist || this._nowPlaying?.artist || this._friendlyName(state);
    const activityStatus = this._isDownloadActive()
      ? { className: "downloading", icon: "download", label: "Đang tải" }
      : isPlaying
        ? { className: "playing", icon: "equalizer", label: "Đang phát" }
        : isPaused
          ? { className: "paused", icon: "pause", label: "Tạm dừng" }
          : null;
    const playerView = this._view === "player" ? `
      ${this._renderSpeakerPicker(players)}

      <section class="now-playing">
        <div class="art-wrap ${isPlaying ? "pulse" : ""}">
          <div class="art ${art ? "has-art" : ""}" style="${art ? `background-image:url('${this._escape(art)}')` : ""}">
            ${art ? "" : this._icon("music-note")}
          </div>
        </div>
        <div class="track-info">
          <div class="track-title" title="${this._escape(mediaTitle)}">${this._escape(mediaTitle)}</div>
          <div class="track-subtitle">${this._escape(mediaArtist || "")}</div>
        </div>
      </section>

      <section class="transport">
        <div class="progress-head"><span id="elapsed">${this._formatTime(position)}</span><span id="duration">${this._formatTime(duration)}</span></div>
        <input id="progress" class="range progress" type="range" min="0" max="${duration || 1}" step="1" value="${Math.min(position, duration || 1)}" style="--value:${progress}%" ${duration ? "" : "disabled"}>
        <div class="controls">
          <button class="icon-btn" id="previous" title="Bài trước">${this._icon("skip-previous")}</button>
          <button class="icon-btn" id="stop" title="Dừng loa đã chọn">${this._icon("stop")}</button>
          <button class="play-btn ${this._busy ? "busy" : ""}" id="playPause" title="Phát / Tạm dừng">
            ${this._busy ? this._icon("loading") : this._icon(isPlaying ? "pause" : "play")}
          </button>
          <button class="icon-btn" id="next" title="Bài tiếp">${this._icon("skip-next")}</button>
          <button class="icon-btn" id="mute" title="Tắt tiếng">${this._icon(muted ? "volume-off" : "volume-high")}</button>
        </div>
        <div class="volume-row">
          ${this._icon("volume-low")}
          <input id="volume" class="range" type="range" min="0" max="100" value="${volume}" style="--value:${volume}%">
          <span>${volume}%</span>
        </div>
      </section>

      <section class="panel active player-url-panel" id="youtubePanel">
        <div class="url-box">
          ${this._icon("link-variant")}
          <input id="youtubeUrl" type="url" value="${this._escape(this._youtubeUrl)}" placeholder="Dán link YouTube để phát trực tiếp..." autocomplete="off">
          <button id="saveCurrentFavorite" class="soft-btn heart-btn" ${!this._youtubeUrl.trim() || this._favoritesLoading ? "disabled" : ""} title="Thêm link này vào Yêu thích">${this._icon("heart-plus-outline")}</button>
          <button id="playUrl" class="accent-btn" ${!this._selectedPlayer || this._busy ? "disabled" : ""}>${this._icon("play")} Phát</button>
        </div>
        <div class="music-search-box">
          ${this._icon("magnify")}
          <input id="musicSearchQuery" type="search" value="${this._escape(this._musicSearchQuery)}" placeholder="Nhập tên bài hát hoặc ca sĩ..." autocomplete="off">
          <button id="searchMusic" class="accent-btn ${this._musicSearchLoading ? "busy" : ""}" ${this._musicSearchLoading || !this._musicSearchQuery.trim() ? "disabled" : ""}>${this._icon(this._musicSearchLoading ? "loading" : "magnify")} Tìm kiếm</button>
        </div>
        <div class="music-search-results">${this._renderMusicSearchResults()}</div>
      </section>` : "";

    const favoritesView = this._view === "favorites" ? this._renderFavoritesView(players, isPlaying) : "";

    this.shadowRoot.innerHTML = `
      <style>${this._styles()}</style>
      <ha-card class="card ${isPlaying ? "is-playing" : ""}" style="${this._presetVars()}">
        <div class="ambient" style="${art ? `background-image:url('${this._escape(art)}')` : ""}"></div>
        <div class="surface">
          <header>
            <div class="brand">
              <div class="brand-icon">${this._icon("music-circle")}</div>
              <div>
                <div class="eyebrow">${this._escape(this._config.subtitle || "YOUTUBE-DLP")}</div>
                <div class="brand-title">${this._escape(this._config.title || "Media Center")}</div>
              </div>
            </div>
            ${activityStatus ? `<div class="activity-badge ${activityStatus.className}">${this._icon(activityStatus.icon)}<span>${this._escape(activityStatus.label)}</span></div>` : ""}
          </header>

          <nav class="primary-tabs" aria-label="YouTube-DLP">
            <button data-view="player" class="${this._view === "player" ? "active" : ""}">
              ${this._icon("play-circle-outline")}<span>Media Player</span>
            </button>
            <button data-view="download" class="${this._view === "download" ? "active" : ""}">
              ${this._icon("download-circle-outline")}<span>Download</span>${this._isDownloadActive() ? `<i class="tab-pulse"></i>` : ""}
            </button>
            <button data-view="favorites" class="${this._view === "favorites" ? "active" : ""}">
              ${this._icon("heart-outline")}<span>Favourites</span>
            </button>
          </nav>

          ${this._renderDownloadNotice()}
          ${this._view === "player" ? playerView : this._view === "favorites" ? favoritesView : this._renderDownloadPanel()}
        </div>
      </ha-card>`;

    this._bindEvents();
    this._restoreFavoritesViewport(favoritesViewport);
  }

  _syncSpeakerPicker() {
    const selected = new Set(this._targetPlayers());
    const summary = this.shadowRoot?.querySelector(".speaker-summary");
    const count = this.shadowRoot?.querySelector(".speaker-count");
    if (summary) summary.textContent = this._speakerSummary();
    if (count) count.textContent = String(selected.size);
    this.shadowRoot?.querySelectorAll("[data-speaker-id]").forEach((checkbox) => {
      checkbox.checked = selected.has(checkbox.dataset.speakerId);
    });
  }

  _closeSpeakerPicker() {
    if (this._speakerCloseTimer) window.clearTimeout(this._speakerCloseTimer);
    this._speakerCloseTimer = null;
    this._speakerPickerOpen = false;
    this._speakerMenuScrollTop = 0;
    const picker = this.shadowRoot?.getElementById("speakerPicker");
    if (picker?.open) picker.open = false;
  }

  _bindEvents() {
    const $ = (id) => this.shadowRoot.getElementById(id);
    const speakerPicker = $("speakerPicker");
    const speakerOptions = this.shadowRoot.querySelector(".speaker-options");
    if (speakerOptions) speakerOptions.scrollTop = this._speakerMenuScrollTop;

    const cancelSpeakerClose = () => {
      if (this._speakerCloseTimer) window.clearTimeout(this._speakerCloseTimer);
      this._speakerCloseTimer = null;
    };
    const scheduleSpeakerClose = () => {
      cancelSpeakerClose();
      this._speakerCloseTimer = window.setTimeout(() => this._closeSpeakerPicker(), 180);
    };

    speakerPicker?.addEventListener("toggle", () => {
      this._speakerPickerOpen = speakerPicker.open;
      if (!speakerPicker.open) this._speakerMenuScrollTop = 0;
    });
    speakerPicker?.addEventListener("mouseenter", cancelSpeakerClose);
    speakerPicker?.addEventListener("mouseleave", scheduleSpeakerClose);
    speakerPicker?.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        this._closeSpeakerPicker();
        speakerPicker.querySelector("summary")?.focus();
      }
    });

    this.shadowRoot.querySelectorAll("[data-speaker-id]").forEach((checkbox) => {
      checkbox.addEventListener("change", () => {
        const previousPrimary = this._selectedPlayer;
        this._speakerPickerOpen = true;
        this._speakerMenuScrollTop = speakerOptions?.scrollTop || 0;
        const selected = [...this.shadowRoot.querySelectorAll("[data-speaker-id]:checked")]
          .map((item) => item.dataset.speakerId);
        this._setSelectedPlayers(selected);
        if (previousPrimary !== this._selectedPlayer) this._render();
        else this._syncSpeakerPicker();
      });
    });
    $("selectAllSpeakers")?.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const previousPrimary = this._selectedPlayer;
      this._speakerPickerOpen = true;
      this._speakerMenuScrollTop = speakerOptions?.scrollTop || 0;
      this._setSelectedPlayers(this._players().map((player) => player.entity_id));
      if (previousPrimary !== this._selectedPlayer) this._render();
      else this._syncSpeakerPicker();
    });
    $("clearSpeakers")?.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const previousPrimary = this._selectedPlayer;
      const primary = this._selectedPlayer || this._players()[0]?.entity_id || "";
      this._speakerPickerOpen = true;
      this._speakerMenuScrollTop = speakerOptions?.scrollTop || 0;
      this._setSelectedPlayers(primary ? [primary] : []);
      if (previousPrimary !== this._selectedPlayer) this._render();
      else this._syncSpeakerPicker();
    });
    $("closeSpeakerPicker")?.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      this._closeSpeakerPicker();
    });

    this.shadowRoot.querySelectorAll("[data-view]").forEach((button) => {
      button.addEventListener("click", () => {
        this._view = button.dataset.view;
        this._speakerPickerOpen = false;
        this._speakerMenuScrollTop = 0;
        if (this._view === "favorites") this._ensureFavoritesTabLoaded();
        this._render();
      });
    });

    this.shadowRoot.querySelectorAll("[data-favorites-tab]").forEach((button) => {
      button.addEventListener("click", () => {
        this._favoritesTab = button.dataset.favoritesTab;
        this._speakerPickerOpen = false;
        this._speakerMenuScrollTop = 0;
        this._ensureFavoritesTabLoaded();
        this._render();
      });
    });

    $("dismissDownloadNotice")?.addEventListener("click", () => {
      this._downloadNotice = null;
      this._render();
    });

    $("youtubeUrl")?.addEventListener("input", (event) => {
      this._youtubeUrl = event.target.value;
      const save = this.shadowRoot.getElementById("saveCurrentFavorite");
      if (save) save.disabled = !this._youtubeUrl.trim() || this._favoritesLoading;
    });
    $("playUrl")?.addEventListener("click", () => this._playUrl());
    $("saveCurrentFavorite")?.addEventListener("click", () => this._addFavorite(this._youtubeUrl));
    $("youtubeUrl")?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") this._playUrl();
    });
    $("musicSearchQuery")?.addEventListener("input", (event) => {
      this._musicSearchQuery = event.target.value;
      const button = this.shadowRoot.getElementById("searchMusic");
      if (button && !this._musicSearchLoading) button.disabled = !this._musicSearchQuery.trim();
    });
    $("musicSearchQuery")?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        this._searchMusic();
      }
    });
    $("searchMusic")?.addEventListener("click", () => this._searchMusic());
    this.shadowRoot.querySelectorAll("[data-search-play]").forEach((button) => {
      button.addEventListener("click", () => this._playSearchResult(Number(button.dataset.searchPlay)));
    });
    this.shadowRoot.querySelectorAll("[data-search-favorite]").forEach((button) => {
      button.addEventListener("click", () => this._addSearchResultFavorite(Number(button.dataset.searchFavorite)));
    });
    $("playPause")?.addEventListener("click", () => this._playPause());
    $("stop")?.addEventListener("click", () => this._stopPlayback());
    $("previous")?.addEventListener("click", () => this._previous());
    $("next")?.addEventListener("click", () => this._next());
    $("mute")?.addEventListener("click", () => this._toggleMute());
    $("volume")?.addEventListener("change", (event) => this._setVolume(Number(event.target.value)));
    $("progress")?.addEventListener("change", (event) => this._seek(Number(event.target.value)));

    $("favoriteUrl")?.addEventListener("input", (event) => { this._favoriteUrl = event.target.value; });
    $("favoriteUrl")?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") this._addFavorite(this._favoriteUrl);
    });
    $("addFavorite")?.addEventListener("click", () => this._addFavorite(this._favoriteUrl));
    $("refreshFavorites")?.addEventListener("click", () => this._loadFavorites());
    $("refreshLibrary")?.addEventListener("click", () => this._loadLibrary(true));

    const bindSearch = (id, field, pageField) => {
      $(id)?.addEventListener("input", (event) => {
        this[field] = event.target.value;
        this[pageField] = 1;
        const value = event.target.value;
        this._render();
        const search = this.shadowRoot.getElementById(id);
        if (search) {
          search.focus();
          search.setSelectionRange(value.length, value.length);
        }
      });
    };
    bindSearch("onlineSearch", "_onlineQuery", "_favoritesPage");
    bindSearch("offlineSearch", "_offlineQuery", "_libraryPage");

    this.shadowRoot.querySelectorAll("[data-online-index]").forEach((button) => {
      button.addEventListener("click", () => this._playFavorite(Number(button.dataset.onlineIndex), false));
    });
    this.shadowRoot.querySelectorAll("[data-offline-index]").forEach((button) => {
      button.addEventListener("click", () => this._playLibrary(Number(button.dataset.offlineIndex), false));
    });
    this.shadowRoot.querySelectorAll("[data-online-select]").forEach((button) => {
      button.addEventListener("click", () => this._toggleSelection(this._onlineSelected, button.dataset.onlineSelect, "online"));
    });
    this.shadowRoot.querySelectorAll("[data-offline-select]").forEach((button) => {
      button.addEventListener("click", () => this._toggleSelection(this._offlineSelected, button.dataset.offlineSelect, "offline"));
    });
    this.shadowRoot.querySelectorAll("[data-remove-favorite]").forEach((button) => {
      button.addEventListener("click", () => this._removeFavorite(button.dataset.removeFavorite));
    });

    $("selectAllOnline")?.addEventListener("click", () => this._selectAllFavorites("online"));
    $("clearOnline")?.addEventListener("click", () => { this._onlineSelected.clear(); this._schedulePersistFavoriteSettings(); this._render(); });
    $("playSelectedOnline")?.addEventListener("click", () => this._startQueue("online"));
    $("selectAllOffline")?.addEventListener("click", () => this._selectAllFavorites("offline"));
    $("clearOffline")?.addEventListener("click", () => { this._offlineSelected.clear(); this._schedulePersistFavoriteSettings(); this._render(); });
    $("playSelectedOffline")?.addEventListener("click", () => this._startQueue("offline"));
    this.shadowRoot.querySelectorAll("[data-repeat-mode]").forEach((button) => button.addEventListener("click", () => this._cycleRepeatMode()));
    this.shadowRoot.querySelectorAll("[data-stop-favorites]").forEach((button) => button.addEventListener("click", () => this._stopFavoritesPlayback()));

    this.shadowRoot.querySelectorAll("[data-page-kind]").forEach((button) => {
      button.addEventListener("click", () => {
        const page = Number(button.dataset.page) || 1;
        if (button.dataset.pageKind === "favorites-online") this._favoritesPage = page;
        if (button.dataset.pageKind === "favorites-offline") this._libraryPage = page;
        this._render();
      });
    });

    $("downloadUrl")?.addEventListener("input", (event) => {
      this._downloadForm.url = event.target.value;
      const button = this.shadowRoot.getElementById("startDownload");
      if (button && !this._isDownloadActive()) button.disabled = !this._downloadForm.url.trim();
    });
    this.shadowRoot.querySelectorAll("[data-media-type]").forEach((button) => {
      button.addEventListener("click", () => {
        this._downloadForm.media_type = button.dataset.mediaType;
        this._render();
      });
    });
    $("videoQuality")?.addEventListener("change", (event) => { this._downloadForm.video_quality = event.target.value; });
    $("videoFormat")?.addEventListener("change", (event) => { this._downloadForm.video_format = event.target.value; });
    $("audioFormat")?.addEventListener("change", (event) => { this._downloadForm.audio_format = event.target.value; });
    $("audioQuality")?.addEventListener("change", (event) => { this._downloadForm.audio_quality = event.target.value; });
    $("overwrite")?.addEventListener("change", (event) => { this._downloadForm.overwrite = event.target.checked; });
    $("waitForCompletion")?.addEventListener("change", (event) => { this._downloadForm.wait_for_completion = event.target.checked; });
    $("startDownload")?.addEventListener("click", () => this._startDownload());
    $("downloadUrl")?.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !this._isDownloadActive()) this._startDownload();
    });
  }

  async _loadManagedTargets() {
    if (!this._hass || this._managedTargetsLoading) return;
    this._managedTargetsLoading = true;
    try {
      const response = await this._callServiceResponse("yt_dlp", "get_media_targets");
      const targets = Array.isArray(response?.targets) ? response.targets : [];
      this._managedTargets = targets.filter((target) =>
        target && typeof target.entity_id === "string" && target.entity_id.startsWith("media_player.")
        && ["speaker", "dlna", "tv"].includes(target.type)
      );
      this._managedTargetsLoaded = true;
      this._managedTargetsLoadedAt = Date.now();
      this._managedTargetsRetryAt = 0;

      const allowed = new Set(this._configuredPlayers());
      this._selectedPlayers = this._selectedPlayers.filter((entityId) => allowed.has(entityId));
      if (!this._selectedPlayers.length) {
        const candidates = this._managedTargets.map((target) => target.entity_id);
        const active = candidates.find((entityId) => ["playing", "paused", "buffering"].includes(this._hass.states[entityId]?.state));
        const available = candidates.find((entityId) => this._targetAvailable(entityId));
        const preferred = active || available || candidates[0] || "";
        this._selectedPlayers = preferred ? [preferred] : [];
      }
      this._selectedPlayer = this._selectedPlayers[0] || "";
      this._persistSpeakerSelection();
      this._speakerSelectionRestored = true;
      this._render();
    } catch (_error) {
      this._managedTargetsRetryAt = Date.now() + 15000;
    } finally {
      this._managedTargetsLoading = false;
    }
  }

  async _callServiceResponse(domain, service, serviceData = {}, target = undefined) {
    if (!this._hass) throw new Error("Home Assistant chưa sẵn sàng");
    const message = {
      type: "call_service",
      domain,
      service,
      service_data: serviceData,
      return_response: true,
    };
    if (target) message.target = target;
    const result = await this._hass.callWS(message);
    return result?.response ?? result;
  }

  _targetType(entityId) {
    return this._managedTargets.find((target) => target.entity_id === entityId)?.type || "speaker";
  }

  async _playConfiguredUrl(url, selectedPlayers = this._targetPlayers()) {
    if (!selectedPlayers.length) throw new Error("Chưa chọn thiết bị phát");
    const hasManagedSpecialTarget = selectedPlayers.some((entityId) => {
      const type = this._targetType(entityId);
      return type === "dlna" || type === "tv";
    });

    // Keep ordinary speakers on the untouched v0.5.16 play/play_multi path.
    // DLNA and TV intentionally use the isolated dispatcher so the configured
    // target type actually selects the appropriate playback mechanism.
    if (!hasManagedSpecialTarget) {
      return selectedPlayers.length > 1
        ? this._callServiceResponse("yt_dlp", "play_multi", { url, media_players: selectedPlayers })
        : this._callServiceResponse("yt_dlp", "play", { url, media_player: selectedPlayers[0] });
    }

    return this._callServiceResponse("yt_dlp", "play_targets", {
      url,
      media_players: selectedPlayers,
    });
  }

  async _searchMusic() {
    const query = this._musicSearchQuery.trim();
    if (!query) return this._notify("Hãy nhập tên bài hát hoặc ca sĩ cần tìm.");
    if (!this._hass || this._musicSearchLoading) return;

    this._musicSearchLoading = true;
    this._musicSearchPerformed = true;
    this._musicSearchError = "";
    this._render();
    try {
      const response = await this._callServiceResponse("yt_dlp", "search", { query, limit: 20 });
      const results = Array.isArray(response?.results) ? response.results : [];
      this._musicSearchResults = results
        .filter((item) => item && typeof item.url === "string" && item.url.startsWith("http"))
        .slice(0, 20)
        .map((item) => ({
          ...item,
          artist: item.artist || item.channel || item.uploader || null,
        }));
      this._musicSearchResultQuery = query;
    } catch (error) {
      this._musicSearchResults = [];
      this._musicSearchResultQuery = query;
      this._musicSearchError = `Không thể tìm kiếm: ${error?.message || error}`;
    } finally {
      this._musicSearchLoading = false;
      this._render();
    }
  }

  async _playSearchResult(index) {
    const item = this._musicSearchResults[index];
    if (!item?.url || this._busy) return;
    if (!this._selectedPlayer) return this._notify("Hãy chọn thiết bị phát trước khi phát.");
    this._youtubeUrl = item.url;
    const resolved = await this._playUrl();
    if (resolved && this._musicSearchResults[index]?.url === item.url) {
      this._musicSearchResults[index] = { ...item, ...resolved };
      this._render();
    }
  }

  async _addSearchResultFavorite(index) {
    const item = this._musicSearchResults[index];
    if (!item?.url) return;
    if (this._favorites.some((favorite) => favorite.url === item.url)) {
      return this._notify(`Đã có trong Yêu thích: ${item.title || "YouTube audio"}`);
    }
    const saved = await this._addFavorite(item.url);
    if (saved && this._musicSearchResults[index]?.url === item.url) {
      this._musicSearchResults[index] = { ...item, ...saved };
      this._render();
    }
  }

  async _playUrl() {
    const url = this._youtubeUrl.trim();
    if (!url) return this._notify("Hãy nhập link YouTube.");
    if (!this._selectedPlayer) return this._notify("Hãy chọn thiết bị phát trước khi phát.");

    const selectedPlayers = this._targetPlayers();
    this._queue = null;
    this._busy = true;
    this._render();
    try {
      const response = await this._playConfiguredUrl(url, selectedPlayers);
      this._nowPlaying = response || { title: "YouTube", url };
      this._activePlayback = { kind: "url", url };
      this._currentLibraryIndex = -1;
      this._currentFavoriteIndex = -1;
      const targetText = selectedPlayers.length > 1 ? ` trên ${selectedPlayers.length} thiết bị` : "";
      this._notify(`Đang phát${targetText}: ${response?.title || "YouTube"}`);
      return response;
    } catch (error) {
      this._notify(`Không thể phát: ${error?.message || error}`);
    } finally {
      this._busy = false;
      this._render();
    }
  }

  async _startDownload() {
    if (!this._hass || this._isDownloadActive()) return;
    const form = this._downloadForm;
    const url = form.url.trim();
    if (!url) return this._notify("Hãy nhập link cần tải.");

    this._downloadSubmitting = true;
    this._downloadJob = null;
    this._downloadNotice = null;
    this._view = "download";
    this._render();

    try {
      const response = await this._callServiceResponse("yt_dlp", "download", {
        url,
        media_type: form.media_type,
        video_quality: form.video_quality,
        video_format: form.video_format,
        audio_format: form.audio_format,
        audio_quality: form.audio_quality,
        overwrite: Boolean(form.overwrite),
        wait_for_completion: Boolean(form.wait_for_completion),
      });

      this._downloadJob = response || null;
      this._downloadSubmitting = false;
      this._downloadPollErrors = 0;
      this._lastDownloadPoll = 0;

      if (!this._downloadJob?.job_id) {
        throw new Error("Action không trả về job_id");
      }
      this._storeActiveJob(this._downloadJob.job_id);

      if (["completed", "error", "cancelled"].includes(this._downloadJob.status)) {
        await this._finishDownload(this._downloadJob);
      } else {
        this._render();
        this._pollDownloadJob(true);
      }
    } catch (error) {
      this._downloadSubmitting = false;
      this._downloadJob = null;
      const message = error?.message || String(error);
      this._downloadNotice = { type: "error", title: "Không thể bắt đầu tải", message };
      this._notify(`Tải thất bại: ${message}`);
      this._render();
    }
  }

  async _pollDownloadJob(force = false) {
    const jobId = this._downloadJob?.job_id;
    if (!jobId || this._downloadPollInFlight || !this._hass) return;
    if (["completed", "error", "cancelled"].includes(this._downloadJob?.status)) return;

    const now = Date.now();
    if (!force && now - this._lastDownloadPoll < 1800) return;
    this._lastDownloadPoll = now;
    this._downloadPollInFlight = true;
    try {
      const response = await this._callServiceResponse("yt_dlp", "get_job", { job_id: jobId });
      if (!response) return;
      this._downloadPollErrors = 0;
      this._downloadJob = response;
      if (["completed", "error", "cancelled"].includes(response.status)) {
        await this._finishDownload(response);
      } else {
        this._render();
      }
    } catch (error) {
      // A transient frontend/websocket error must not affect the downloader.
      // Keep the job and retry; abandon only a stale browser-restored id after
      // repeated failures (for example after a Home Assistant restart).
      this._downloadPollErrors += 1;
      console.debug("YouTube-DLP card job poll failed", error);
      if (this._downloadPollErrors >= 10) {
        this._clearActiveJob();
        this._downloadJob = null;
        this._downloadPollErrors = 0;
        this._downloadNotice = {
          type: "error",
          title: "Không thể theo dõi tác vụ tải",
          message: "Job cũ không còn tồn tại hoặc Home Assistant đã được khởi động lại.",
        };
        this._render();
      }
    } finally {
      this._downloadPollInFlight = false;
    }
  }

  async _finishDownload(job) {
    this._downloadJob = job;
    this._downloadSubmitting = false;
    this._clearActiveJob();
    const file = job.final_files?.length ? this._fileName(job.final_files[0]) : this._fileName(job.filename);

    if (job.status === "completed") {
      const details = [
        file || job.title || "File đã tải",
        this._formatSize(job.file_size_bytes || job.total_bytes),
        job.format ? String(job.format).toUpperCase() : "",
        job.quality ? String(job.quality) : "",
      ].filter(Boolean).join(" • ");
      this._downloadNotice = {
        type: "success",
        title: "Tải xuống hoàn tất",
        message: details,
      };
      this._notify(`Tải xong: ${file || job.title || "file"}`);

      if (job.media_type === "audio") {
        if (this._libraryLoaded) this._loadLibrary(true);
        return;
      }
    } else {
      const message = job.error || (job.status === "cancelled" ? "Tác vụ đã bị hủy." : "Không rõ lỗi.");
      this._downloadNotice = {
        type: "error",
        title: job.status === "cancelled" ? "Tải đã hủy" : "Tải xuống thất bại",
        message,
      };
      this._notify(`${job.status === "cancelled" ? "Tải đã hủy" : "Tải lỗi"}: ${message}`);
    }
    this._render();
  }

  _storeActiveJob(jobId) {
    try {
      window.localStorage?.setItem("yt_dlp_media_card_active_job", jobId);
    } catch (_error) {
      // Optional browser persistence only.
    }
  }

  _clearActiveJob() {
    try {
      window.localStorage?.removeItem("yt_dlp_media_card_active_job");
    } catch (_error) {
      // Optional browser persistence only.
    }
  }

  _ensureFavoritesTabLoaded() {
    if (!this._favoritePlaybackLoaded && !this._favoritePlaybackLoading) this._loadFavoritePlaybackState();
    if (this._favoritesTab === "online" && !this._favoritesLoaded && !this._favoritesLoading) {
      this._loadFavorites();
    }
    if (this._favoritesTab === "offline" && !this._libraryLoaded && !this._libraryLoading) {
      this._loadLibrary(false);
    }
  }

  async _loadFavorites() {
    if (!this._hass || this._favoritesLoading) return;
    this._favoritesLoading = true;
    this._render();
    try {
      const response = await this._callServiceResponse("yt_dlp", "favorites_list");
      this._favorites = Array.isArray(response?.items) ? response.items : [];
      const valid = new Set(this._favorites.map((item) => item.url));
      const selectedBefore = this._onlineSelected.size;
      this._onlineSelected = new Set([...this._onlineSelected].filter((url) => valid.has(url)));
      this._favoritesLoaded = true;
      if (this._favoritePlaybackActive && this._favoritePlaybackKind === "online") {
        const currentKey = this._favoritePlaybackQueue[this._favoritePlaybackCursor];
        this._currentFavoriteIndex = this._favorites.findIndex((item) => item.url === currentKey);
      }
      if (this._favoritePlaybackLoaded && selectedBefore !== this._onlineSelected.size) this._schedulePersistFavoriteSettings();
    } catch (error) {
      this._notify(`Không thể tải Yêu thích Online: ${error?.message || error}`);
    } finally {
      this._favoritesLoading = false;
      this._render();
    }
  }

  async _addFavorite(rawUrl) {
    const url = String(rawUrl || "").trim();
    if (!url) return this._notify("Hãy nhập link YouTube cần thêm vào Yêu thích.");
    if (!this._hass || this._favoritesLoading) return;
    this._favoritesLoading = true;
    this._render();
    try {
      const response = await this._callServiceResponse("yt_dlp", "favorites_add", { url });
      const item = response?.item;
      if (!item?.url) throw new Error("Không nhận được metadata bài hát");
      this._favorites = [item, ...this._favorites.filter((entry) => entry.url !== item.url)];
      this._favoritesLoaded = true;
      this._favoritesPage = 1;
      if (this._favoriteUrl.trim() === url) this._favoriteUrl = "";
      this._notify(`Đã thêm Yêu thích: ${item.title || "YouTube audio"}`);
      return item;
    } catch (error) {
      this._notify(`Không thể thêm Yêu thích: ${error?.message || error}`);
    } finally {
      this._favoritesLoading = false;
      this._render();
    }
  }

  async _removeFavorite(url) {
    if (!url || !this._hass) return;
    try {
      await this._callServiceResponse("yt_dlp", "favorites_remove", { url });
      const removedIndex = this._favorites.findIndex((item) => item.url === url);
      this._favorites = this._favorites.filter((item) => item.url !== url);
      this._onlineSelected.delete(url);
      if (removedIndex === this._currentFavoriteIndex) this._currentFavoriteIndex = -1;
      else if (removedIndex >= 0 && removedIndex < this._currentFavoriteIndex) this._currentFavoriteIndex -= 1;
      this._render();
    } catch (error) {
      this._notify(`Không thể xóa Yêu thích: ${error?.message || error}`);
    }
  }

  _toggleSelection(selection, key, _kind) {
    if (!key) return;
    if (selection.has(key)) selection.delete(key);
    else selection.add(key);
    this._schedulePersistFavoriteSettings();
    this._render();
  }

  _selectAllFavorites(kind) {
    if (kind === "online") {
      this._onlineSelected = new Set(this._favorites.map((item) => item.url));
    } else {
      this._offlineSelected = new Set(this._library.map((item) => item.id));
    }
    this._schedulePersistFavoriteSettings();
    this._render();
  }

  _cycleRepeatMode() {
    const modes = ["off", "one", "all"];
    this._repeatMode = modes[(modes.indexOf(this._repeatMode) + 1) % modes.length];
    this._schedulePersistFavoriteSettings();
    this._render();
  }

  async _startQueue(kind) {
    if (!this._selectedPlayer) return this._notify("Hãy chọn thiết bị phát trước khi phát.");
    const queue = kind === "online"
      ? this._favorites.filter((item) => this._onlineSelected.has(item.url)).map((item) => item.url)
      : this._library.filter((item) => this._offlineSelected.has(item.id)).map((item) => item.id);
    if (!queue.length) return this._notify("Hãy chọn ít nhất một bài để phát liên tiếp.");
    await this._startFavoriteBackend(kind, queue, true, this._repeatMode);
  }

  async _startFavoriteBackend(kind, queue, replaceSelection, repeatMode) {
    const selectedPlayers = this._targetPlayers();
    if (!selectedPlayers.length || !queue.length || !this._hass) return;
    this._queue = null;
    this._queueStopRequested = false;
    this._clearPlaybackInterruption();
    this._activePlayback = null;
    this._busy = true;
    this._render();
    try {
      const response = await this._callServiceResponse("yt_dlp", "favorites_playback_start", {
        kind,
        queue,
        media_players: selectedPlayers,
        repeat_mode: repeatMode,
        replace_selection: Boolean(replaceSelection),
      });
      this._favoriteSettingsDirty = false;
      this._applyFavoritePlaybackState(response, true);
    } catch (error) {
      this._notify(`Không thể phát Yêu thích: ${error?.message || error}`);
    } finally {
      this._busy = false;
      this._render();
    }
  }

  async _playQueueCurrent() {
    // Kept only for stale in-memory queues created by an older card instance.
    // New Favorites playback is owned by Home Assistant via _startFavoriteBackend.
    const queue = this._queue;
    if (!queue?.indexes?.length) return;
    const index = queue.indexes[queue.cursor];
    queue.started = false;
    this._queueStartedAt = Date.now();
    if (queue.kind === "online") await this._playFavorite(index, true);
    else await this._playLibrary(index, true);
    if (this._queue === queue && this._state()?.state === "playing") queue.started = true;
  }

  _mediaIdentity(state) {
    const attrs = state?.attributes || {};
    return [
      attrs.media_content_id || "",
      attrs.media_title || "",
      attrs.media_artist || "",
      attrs.media_album_name || "",
    ].join("\u241f");
  }

  _clearInterruptionCandidate() {
    if (this._interruptionCandidateTimer) window.clearTimeout(this._interruptionCandidateTimer);
    this._interruptionCandidateTimer = null;
    this._interruptionCandidate = null;
  }

  _clearResumeIdleTimer() {
    if (this._resumeIdleTimer) window.clearTimeout(this._resumeIdleTimer);
    this._resumeIdleTimer = null;
  }

  _clearPlaybackInterruption() {
    this._clearInterruptionCandidate();
    this._clearResumeIdleTimer();
    this._interruptedPlayback = null;
    this._resumeInFlight = false;
  }

  _captureInterruptedPlayback(state) {
    if (!this._activePlayback) return null;
    const { position, duration } = this._position(state);
    const attrs = state?.attributes || {};
    return {
      source: { ...this._activePlayback },
      position,
      duration,
      identity: this._mediaIdentity(state),
      media: {
        contentId: attrs.media_content_id || "",
        title: attrs.media_title || "",
        artist: attrs.media_artist || "",
        album: attrs.media_album_name || "",
      },
    };
  }

  _matchesInterruptedMedia(state, interrupted) {
    if (!state || !interrupted) return false;
    if (this._mediaIdentity(state) === interrupted.identity) return true;
    const attrs = state.attributes || {};
    const saved = interrupted.media || {};
    if (saved.contentId && attrs.media_content_id && saved.contentId === attrs.media_content_id) return true;
    if (!saved.title || attrs.media_title !== saved.title) return false;
    if (saved.artist && attrs.media_artist && saved.artist !== attrs.media_artist) return false;
    return true;
  }

  _beginPlaybackInterruption(snapshot) {
    if (!snapshot || this._interruptedPlayback || this._resumeInFlight) return;
    this._clearInterruptionCandidate();
    this._clearResumeIdleTimer();
    this._interruptedPlayback = snapshot;
    if (this._queue) this._queue.started = false;
  }

  _deferPossibleInterruption(snapshot, delay = 7000) {
    this._clearInterruptionCandidate();
    this._interruptionCandidate = snapshot;
    this._interruptionCandidateTimer = window.setTimeout(() => {
      const candidate = this._interruptionCandidate;
      this._clearInterruptionCandidate();
      if (!candidate || this._interruptedPlayback || this._resumeInFlight) return;
      if (["idle", "off", "standby"].includes(this._state()?.state)) {
        this._activePlayback = null;
        if (this._queue && !this._queueStopRequested && !this._queueAdvancing) {
          this._queue.started = false;
          this._advanceQueueAfterEnd();
        }
      }
    }, delay);
  }

  async _waitForPlaybackStart() {
    for (let attempt = 0; attempt < 50; attempt += 1) {
      if (this._state()?.state === "playing") return true;
      await new Promise((resolve) => window.setTimeout(resolve, 200));
    }
    return ["playing", "buffering"].includes(this._state()?.state);
  }

  _scheduleInterruptedResume(delay = 900) {
    if (!this._interruptedPlayback || this._resumeInFlight || this._queueStopRequested) return;
    // Remote yt_dlp playback is restored by the integration backend so resume
    // continues to work even when this dashboard/card is not open. Avoid a
    // second frontend play_media call racing the backend. Local library files
    // still use the card-side fallback because they bypass yt_dlp.play.
    if (["online", "url"].includes(this._interruptedPlayback?.source?.kind)) return;
    this._clearResumeIdleTimer();
    this._resumeIdleTimer = window.setTimeout(() => {
      this._resumeIdleTimer = null;
      if (!this._interruptedPlayback || this._resumeInFlight || this._queueStopRequested) return;
      if (this._state()?.state !== "idle") return;
      this._resumeInterruptedPlayback();
    }, delay);
  }

  async _seekInterruptedPosition(interrupted) {
    if (!(await this._waitForPlaybackStart())) {
      throw new Error("Thiết bị chưa sẵn sàng để khôi phục vị trí phát");
    }
    const seekPosition = Math.max(0, Number(interrupted?.position) || 0);
    const selectedPlayers = this._targetPlayers();
    if (!selectedPlayers.length || !this._hass) throw new Error("Không tìm thấy thiết bị để khôi phục nhạc");
    let lastError = null;
    for (let attempt = 0; attempt < 4; attempt += 1) {
      try {
        await this._hass.callService(
          "media_player",
          "media_seek",
          { seek_position: seekPosition },
          { entity_id: selectedPlayers.length === 1 ? selectedPlayers[0] : selectedPlayers }
        );
        return true;
      } catch (error) {
        lastError = error;
        await new Promise((resolve) => window.setTimeout(resolve, 350 * (attempt + 1)));
      }
    }
    throw lastError || new Error("Không thể khôi phục vị trí phát");
  }

  async _resumeAutoRestoredPlayback() {
    const interrupted = this._interruptedPlayback;
    if (!interrupted || this._resumeInFlight || this._queueStopRequested) return;
    this._resumeInFlight = true;
    this._clearResumeIdleTimer();
    try {
      await this._seekInterruptedPosition(interrupted);
      if (this._interruptedPlayback === interrupted) this._interruptedPlayback = null;
      this._activePlayback = { ...interrupted.source };
      if (this._queue) {
        this._queue.started = true;
        this._queueStartedAt = Date.now();
      }
    } catch (error) {
      this._notify(`Không thể khôi phục đúng vị trí nhạc sau thông báo: ${error?.message || error}`);
    } finally {
      this._resumeInFlight = false;
    }
  }

  async _resumeInterruptedPlayback() {
    const interrupted = this._interruptedPlayback;
    if (!interrupted || this._resumeInFlight || this._queueStopRequested) return;
    if (this._state()?.state !== "idle") return;
    this._resumeInFlight = true;
    this._clearResumeIdleTimer();
    try {
      const source = interrupted.source;
      if (source.kind === "online") {
        const index = this._favorites.findIndex((item) => item.url === source.url);
        if (index >= 0) {
          await this._playFavorite(index, true);
        } else {
          const selectedPlayers = this._targetPlayers();
          if (!source.url || !selectedPlayers.length) return;
          const response = await this._playConfiguredUrl(source.url, selectedPlayers);
          this._nowPlaying = response || this._nowPlaying || { title: "YouTube audio", url: source.url };
          this._activePlayback = { kind: "online", url: source.url };
        }
      } else if (source.kind === "offline") {
        const index = this._library.findIndex((item) => item.id === source.id);
        if (index < 0) return;
        await this._playLibrary(index, true);
      } else if (source.kind === "url") {
        const selectedPlayers = this._targetPlayers();
        if (!source.url || !selectedPlayers.length) return;
        this._busy = true;
        this._render();
        try {
          const response = await this._playConfiguredUrl(source.url, selectedPlayers);
          this._nowPlaying = response || this._nowPlaying || { title: "YouTube audio", url: source.url };
          this._activePlayback = { kind: "url", url: source.url };
        } finally {
          this._busy = false;
          this._render();
        }
      }

      await this._seekInterruptedPosition(interrupted);
      if (this._interruptedPlayback === interrupted) this._interruptedPlayback = null;
      if (this._queue) {
        this._queue.started = true;
        this._queueStartedAt = Date.now();
      }
    } catch (error) {
      this._notify(`Không thể tiếp tục nhạc sau thông báo: ${error?.message || error}`);
    } finally {
      this._resumeInFlight = false;
    }
  }

  _handlePlayerTransition(previousState, currentState) {
    if (this._queueStopRequested || this._queueAdvancing || this._resumeInFlight) return;
    const current = currentState?.state;
    const previous = previousState?.state;
    const queue = this._queue;

    if (this._interruptedPlayback) {
      if (["playing", "buffering"].includes(current) && this._matchesInterruptedMedia(currentState, this._interruptedPlayback)) {
        queueMicrotask(() => this._resumeAutoRestoredPlayback());
        return;
      }
      if (current === "idle") {
        this._scheduleInterruptedResume();
      } else {
        this._clearResumeIdleTimer();
      }
      return;
    }

    if (this._interruptionCandidate) {
      if (["playing", "buffering"].includes(current)) {
        const candidate = this._interruptionCandidate;
        if (this._mediaIdentity(currentState) !== candidate.identity) {
          this._beginPlaybackInterruption(candidate);
          return;
        }
        this._clearInterruptionCandidate();
        if (queue) queue.started = true;
      }
      return;
    }

    if (this._activePlayback && previous === "playing" && current === "playing" && !this._busy) {
      const previousIdentity = this._mediaIdentity(previousState);
      if (previousIdentity && this._mediaIdentity(currentState) !== previousIdentity) {
        this._beginPlaybackInterruption(this._captureInterruptedPlayback(previousState));
        return;
      }
    }

    if (this._activePlayback && previous === "playing" && ["paused", "buffering"].includes(current) && !this._busy) {
      const previousIdentity = this._mediaIdentity(previousState);
      if (current === "paused" || this._mediaIdentity(currentState) !== previousIdentity) {
        if (queue) queue.started = false;
        this._deferPossibleInterruption(this._captureInterruptedPlayback(previousState), 5000);
        return;
      }
    }

    if (["playing", "buffering"].includes(current) && queue) queue.started = true;

    if (this._activePlayback && previous === "playing" && ["idle", "off", "standby"].includes(current)) {
      const snapshot = this._captureInterruptedPlayback(previousState);
      if (snapshot?.duration > 0 && snapshot.position < snapshot.duration - 1.5) {
        if (queue) queue.started = false;
        this._deferPossibleInterruption(snapshot);
        return;
      }
      this._activePlayback = null;
    }

    if (queue?.started && ["playing", "paused", "buffering"].includes(previous) && ["idle", "off", "standby"].includes(current)) {
      queue.started = false;
      queueMicrotask(() => this._advanceQueueAfterEnd());
    }
  }

  async _advanceQueueAfterEnd() {
    const queue = this._queue;
    if (!queue || this._queueAdvancing || this._queueStopRequested) return;
    this._queueAdvancing = true;
    try {
      if (this._repeatMode !== "one") {
        if (queue.cursor < queue.indexes.length - 1) queue.cursor += 1;
        else if (this._repeatMode === "all") queue.cursor = 0;
        else {
          this._queue = null;
          this._render();
          return;
        }
      }
      await this._playQueueCurrent();
    } finally {
      this._queueAdvancing = false;
    }
  }

  async _stopFavoritesPlayback() {
    if (!this._hass) return;
    const wasFavoritesActive = this._favoritePlaybackActive;
    this._busy = true;
    this._render();
    try {
      const response = await this._callServiceResponse("yt_dlp", "favorites_playback_stop");
      this._favoriteSettingsDirty = false;
      this._applyFavoritePlaybackState(response, true);
      // Keep the toolbar Stop behavior intuitive even when the currently
      // playing media was started from the Media Player tab.
      if (!wasFavoritesActive && this._selectedPlayer) await this._callMediaService("media_stop");
    } catch (error) {
      this._notify(`Không thể dừng Yêu thích: ${error?.message || error}`);
    } finally {
      this._busy = false;
      this._render();
    }
  }

  async _stopPlayback() {
    if (!this._hass) return;
    if (!this._selectedPlayer) return this._notify("Chưa có thiết bị phát được chọn.");
    this._queueStopRequested = true;
    this._queue = null;
    this._queueAdvancing = false;
    this._clearPlaybackInterruption();
    this._activePlayback = null;
    try {
      if (this._favoritePlaybackActive) {
        const response = await this._callServiceResponse("yt_dlp", "favorites_playback_stop");
        this._favoriteSettingsDirty = false;
        this._applyFavoritePlaybackState(response, true);
      } else {
        await this._callMediaService("media_stop");
      }
    } finally {
      this._queueStopRequested = false;
      this._render();
    }
  }

  async _playFavorite(index, _fromQueue = false) {
    const item = this._favorites[index];
    if (!item || !this._targetPlayers().length) return;
    this._currentFavoriteIndex = index;
    this._currentLibraryIndex = -1;
    this._nowPlaying = item;
    const repeatMode = this._repeatMode === "one" ? "one" : "off";
    return this._startFavoriteBackend("online", [item.url], false, repeatMode);
  }

  async _loadLibrary(force) {
    if (!this._hass || this._libraryLoading) return;
    this._libraryLoading = true;
    this._render();
    try {
      const response = await this._callServiceResponse("yt_dlp", "scan_library", { force: Boolean(force) });
      this._library = Array.isArray(response?.items) ? response.items : [];
      const valid = new Set(this._library.map((item) => item.id));
      const selectedBefore = this._offlineSelected.size;
      this._offlineSelected = new Set([...this._offlineSelected].filter((id) => valid.has(id)));
      this._libraryPath = response?.path || "";
      this._libraryLoaded = true;
      if (this._favoritePlaybackActive && this._favoritePlaybackKind === "offline") {
        const currentKey = this._favoritePlaybackQueue[this._favoritePlaybackCursor];
        this._currentLibraryIndex = this._library.findIndex((item) => item.id === currentKey);
      }
      if (this._favoritePlaybackLoaded && selectedBefore !== this._offlineSelected.size) this._schedulePersistFavoriteSettings();
    } catch (error) {
      this._notify(`Không thể quét thư viện: ${error?.message || error}`);
    } finally {
      this._libraryLoading = false;
      this._render();
    }
  }

  async _playLibrary(index, _fromQueue = false) {
    const item = this._library[index];
    if (!item || !this._targetPlayers().length || !this._hass) return;
    this._currentLibraryIndex = index;
    this._currentFavoriteIndex = -1;
    this._nowPlaying = { title: item.title || item.filename, artist: item.artist || "Không rõ ca sĩ", thumbnail: null, duration: item.duration };
    const repeatMode = this._repeatMode === "one" ? "one" : "off";
    return this._startFavoriteBackend("offline", [item.id], false, repeatMode);
  }

  async _playPause() {
    if (!this._selectedPlayer || this._busy) return;
    const playerState = this._state()?.state;
    if (playerState === "playing") return this._callMediaService("media_pause");
    if (playerState === "paused") return this._callMediaService("media_play");
    if (this._favoritePlaybackActive) return this._favoritePlaybackCommand("current");

    if (this._queue?.indexes?.length) return this._playQueueCurrent();
    if (this._youtubeUrl.trim()) return this._playUrl();
    return this._callMediaService("media_play");
  }

  async _callMediaService(service, data = {}) {
    if (!this._selectedPlayer || !this._hass) return;
    const selectedPlayers = this._targetPlayers();
    if (selectedPlayers.length > 1) {
      try {
        await this._hass.callService("media_player", service, data, { entity_id: selectedPlayers });
      } catch (error) {
        this._notify(`Lệnh loa thất bại: ${error?.message || error}`);
      }
      return;
    }
    try {
      await this._hass.callService("media_player", service, data, { entity_id: this._selectedPlayer });
    } catch (error) {
      this._notify(`Lệnh loa thất bại: ${error?.message || error}`);
    }
  }

  async _previous() {
    if (this._favoritePlaybackActive && !this._favoritePlaybackSelectionBound) {
      if (this._favoritePlaybackKind === "online" && this._currentFavoriteIndex >= 0 && this._favorites.length) {
        const index = (this._currentFavoriteIndex - 1 + this._favorites.length) % this._favorites.length;
        return this._playFavorite(index, false);
      }
      if (this._favoritePlaybackKind === "offline" && this._currentLibraryIndex >= 0 && this._library.length) {
        const index = (this._currentLibraryIndex - 1 + this._library.length) % this._library.length;
        return this._playLibrary(index, false);
      }
    }
    if (this._favoritePlaybackActive) return this._favoritePlaybackCommand("previous");
    if (this._queue?.indexes?.length) {
      this._queue.cursor = Math.max(0, this._queue.cursor - 1);
      return this._playQueueCurrent();
    }
    return this._callMediaService("media_previous_track");
  }

  async _next() {
    if (this._favoritePlaybackActive && !this._favoritePlaybackSelectionBound) {
      if (this._favoritePlaybackKind === "online" && this._currentFavoriteIndex >= 0 && this._favorites.length) {
        const index = (this._currentFavoriteIndex + 1) % this._favorites.length;
        return this._playFavorite(index, false);
      }
      if (this._favoritePlaybackKind === "offline" && this._currentLibraryIndex >= 0 && this._library.length) {
        const index = (this._currentLibraryIndex + 1) % this._library.length;
        return this._playLibrary(index, false);
      }
    }
    if (this._favoritePlaybackActive) return this._favoritePlaybackCommand("next");
    if (this._queue?.indexes?.length) {
      if (this._queue.cursor < this._queue.indexes.length - 1) this._queue.cursor += 1;
      else this._queue.cursor = this._repeatMode === "all" ? 0 : this._queue.cursor;
      return this._playQueueCurrent();
    }
    return this._callMediaService("media_next_track");
  }

  _toggleMute() {
    const muted = Boolean(this._state()?.attributes?.is_volume_muted);
    return this._callMediaService("volume_mute", { is_volume_muted: !muted });
  }

  _setVolume(value) {
    return this._callMediaService("volume_set", { volume_level: Math.max(0, Math.min(1, value / 100)) });
  }

  _seek(position) {
    return this._callMediaService("media_seek", { seek_position: Math.max(0, position) });
  }

  _tick() {
    this._updateProgressOnly();
    const state = this._state();
    if (this._queue?.started && !this._queueAdvancing && !this._queueStopRequested && state?.state === "playing") {
      const { position, duration } = this._position(state);
      if (duration > 0 && position >= Math.max(0, duration - 0.2) && Date.now() - this._queueStartedAt > 1500) {
        this._queue.started = false;
        this._advanceQueueAfterEnd();
      }
    }
    if (this._downloadJob && !["completed", "error", "cancelled"].includes(this._downloadJob.status)) {
      this._pollDownloadJob(false);
    }
  }

  _updateProgressOnly() {
    if (!this.shadowRoot || !this._hass) return;
    const state = this._state();
    let { position, duration } = this._position(state);
    if (this._favoritePlaybackActive && !(duration > 0)) {
      const savedDuration = Number(this._favoritePlaybackCurrent?.duration || 0);
      if (savedDuration > 0) duration = savedDuration;
    }
    const elapsed = this.shadowRoot.getElementById("elapsed");
    const durationEl = this.shadowRoot.getElementById("duration");
    const slider = this.shadowRoot.getElementById("progress");
    if (elapsed) elapsed.textContent = this._formatTime(position);
    if (durationEl) durationEl.textContent = this._formatTime(duration);
    if (slider && duration > 0 && this.shadowRoot.activeElement !== slider) {
      slider.max = duration;
      slider.value = Math.min(position, duration);
      slider.style.setProperty("--value", `${Math.min(100, (position / duration) * 100)}%`);
    }
  }

  _notify(message) {
    this.dispatchEvent(new CustomEvent("hass-notification", {
      bubbles: true,
      composed: true,
      detail: { message },
    }));
  }

  _styles() {
    return `
      :host{
        display:block;
        width:100%;
        min-width:0;
        container-type:inline-size;
        --text:var(--primary-text-color,#202124);
        --muted:var(--secondary-text-color,#6b7280);
        --card-bg:var(--ha-card-background,var(--card-background-color,#fff));
        --surface-soft:color-mix(in srgb,var(--card-bg) 96%,var(--text) 4%);
        --surface:color-mix(in srgb,var(--card-bg) 91%,var(--text) 9%);
        --surface-strong:color-mix(in srgb,var(--card-bg) 84%,var(--text) 16%);
        --border:color-mix(in srgb,var(--text) 13%,transparent);
        --border-soft:color-mix(in srgb,var(--text) 8%,transparent);
        --accent:var(--primary-color,#05a8d8);
        --accent2:#18d5e8;
        --card-opacity-pct:100%;
        color:var(--text);
        font-family:var(--paper-font-body1_-_font-family,inherit)
      }
      *{box-sizing:border-box}button,input,select{font:inherit}button{color:inherit}
      .card{position:relative;width:100%;min-width:0;overflow:hidden;border-radius:clamp(20px,4cqi,28px);color:var(--text);background:linear-gradient(145deg,color-mix(in srgb,var(--card-bg) var(--card-opacity-pct),transparent),color-mix(in srgb,color-mix(in srgb,var(--card-bg) 94%,var(--text) 6%) var(--card-opacity-pct),transparent));box-shadow:var(--ha-card-box-shadow,0 12px 34px rgba(0,0,0,.14));isolation:isolate}
      .ambient{position:absolute;z-index:0;inset:-40px;background-size:cover;background-position:center;filter:blur(48px) saturate(1.35);opacity:.07;transform:scale(1.12);transition:opacity .5s ease}.is-playing .ambient{opacity:.18;animation:floatbg 9s ease-in-out infinite alternate}
      .surface{position:relative;z-index:1;min-width:0;padding:clamp(14px,4cqi,24px);background:linear-gradient(180deg,color-mix(in srgb,color-mix(in srgb,var(--card-bg) 97%,var(--text) 3%) var(--card-opacity-pct),transparent),color-mix(in srgb,var(--card-bg) var(--card-opacity-pct),transparent));backdrop-filter:blur(18px)}
      header,.brand,.speaker-row,.field,.now-playing,.controls,.volume-row,.primary-tabs,.tabs,.url-box,.library-tools,.search-box,.library-meta,.track-row,.download-hero,.job-top,.job-metrics,.result-file,.download-notice,.pagination{display:flex;align-items:center}
      header{justify-content:space-between;gap:12px;min-width:0}.brand{gap:12px;min-width:0}.brand>div:last-child{min-width:0}.brand-icon{width:44px;height:44px;flex:0 0 44px;border-radius:14px;display:grid;place-items:center;background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;box-shadow:0 8px 24px color-mix(in srgb,var(--accent) 30%,transparent)}.brand-icon ha-icon{--mdc-icon-size:26px}.eyebrow{font-size:clamp(8px,2.2cqi,10px);font-weight:800;letter-spacing:.16em;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.brand-title{font-size:clamp(15px,4cqi,18px);font-weight:800;letter-spacing:-.02em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.activity-badge{flex:0 0 auto;display:inline-flex;align-items:center;justify-content:center;gap:5px;height:28px;padding:0 9px 0 8px;border:1px solid transparent;border-radius:9px;font-size:9.5px;font-weight:800;line-height:1;letter-spacing:.005em;white-space:nowrap}.activity-badge span{display:block}.activity-badge ha-icon{--mdc-icon-size:14px}.activity-badge.playing,.activity-badge.downloading{color:#20b879;border-color:color-mix(in srgb,#20b879 24%,transparent);background:color-mix(in srgb,#20b879 9%,var(--card-bg));box-shadow:0 4px 12px color-mix(in srgb,#20b879 8%,transparent)}.activity-badge.paused{color:color-mix(in srgb,var(--accent) 82%,var(--text));border-color:color-mix(in srgb,var(--accent) 22%,transparent);background:color-mix(in srgb,var(--accent) 8%,var(--card-bg))}.activity-badge.downloading ha-icon{animation:downloadFloat 1.25s ease-in-out infinite}
      .download-notice{gap:11px;margin-top:16px;padding:12px 13px;border-radius:16px;border:1px solid var(--border);animation:noticeIn .25s ease}.download-notice.success{background:color-mix(in srgb,#28b77c 12%,var(--card-bg));border-color:color-mix(in srgb,#28b77c 34%,transparent)}.download-notice.error{background:color-mix(in srgb,#e84c5b 11%,var(--card-bg));border-color:color-mix(in srgb,#e84c5b 34%,transparent)}.notice-icon{width:36px;height:36px;flex:0 0 36px;border-radius:12px;display:grid;place-items:center;background:var(--surface)}.success .notice-icon{color:#159b63}.error .notice-icon{color:#d63e50}.notice-icon ha-icon{--mdc-icon-size:21px}.notice-copy{min-width:0;flex:1;display:flex;flex-direction:column;gap:3px}.notice-copy strong{font-size:12px}.notice-copy span{font-size:10px;color:var(--muted);line-height:1.35;overflow-wrap:anywhere}.notice-close{width:30px;height:30px;border:0;border-radius:9px;background:transparent;color:var(--muted);cursor:pointer;display:grid;place-items:center}.notice-close:hover{background:var(--surface);color:var(--text)}.notice-close ha-icon{--mdc-icon-size:17px}
      .primary-tabs{gap:8px;margin-top:18px;padding:6px;border-radius:18px;background:var(--surface-soft);border:1px solid var(--border-soft)}.primary-tabs button{position:relative;flex:1;height:48px;border:0;border-radius:13px;background:transparent;color:var(--muted);cursor:pointer;display:flex;align-items:center;justify-content:center;gap:9px;font-size:12px;font-weight:850;letter-spacing:.01em;transition:background .18s ease,color .18s ease,transform .18s ease,box-shadow .18s ease}.primary-tabs button:hover{color:var(--text);background:var(--surface)}.primary-tabs button.active{color:#fff;background:linear-gradient(135deg,var(--accent),color-mix(in srgb,var(--accent2) 68%,var(--accent)));box-shadow:0 8px 22px color-mix(in srgb,var(--accent) 18%,transparent)}.primary-tabs ha-icon{--mdc-icon-size:21px}.primary-tabs b{font-size:8px;min-width:18px;height:18px;padding:0 5px;border-radius:999px;display:grid;place-items:center;background:color-mix(in srgb,currentColor 14%,transparent)}.primary-tabs .tab-pulse{position:absolute;right:12px;top:10px}
      .speaker-row{position:relative;margin-top:18px;z-index:12}.field{width:100%;height:46px;gap:10px;padding:0 14px;border-radius:15px;border:1px solid var(--border);background:var(--surface-soft)}.field>ha-icon{color:var(--muted);--mdc-icon-size:20px}.field select{appearance:none;flex:1;min-width:0;border:0;outline:0;background:transparent;color:var(--text);font-weight:650}.field select option,.input-shell select option{background:var(--card-bg);color:var(--text)}.speaker-picker{position:relative;width:100%}.speaker-picker>summary{box-sizing:border-box;display:flex;align-items:center;cursor:pointer;list-style:none;user-select:none}.speaker-picker>summary::-webkit-details-marker{display:none}.speaker-summary{min-width:0;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text);font-size:12px;font-weight:700}.speaker-count{min-width:22px;height:22px;padding:0 6px;border-radius:999px;display:grid;place-items:center;background:color-mix(in srgb,var(--accent) 14%,var(--card-bg));color:color-mix(in srgb,var(--accent) 78%,var(--text));font-size:9px;font-weight:850}.speaker-picker[open] .speaker-field{border-color:color-mix(in srgb,var(--accent) 42%,var(--border));box-shadow:0 0 0 2px color-mix(in srgb,var(--accent) 9%,transparent)}.speaker-picker[open] .speaker-field>ha-icon:last-child{transform:rotate(180deg)}.speaker-field>ha-icon:last-child{transition:transform .18s ease}.speaker-menu{position:absolute;left:0;right:0;top:calc(100% + 7px);z-index:30;padding:10px;border:1px solid var(--border);border-radius:15px;background:var(--card-bg);box-shadow:0 16px 38px rgba(0,0,0,.24)}.speaker-menu-head{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:2px 3px 8px}.speaker-menu-head strong{font-size:10px;white-space:nowrap}.speaker-menu-head>div{display:flex;gap:5px;min-width:0}.speaker-menu-head button{height:29px;padding:0 8px;border:1px solid var(--border-soft);border-radius:8px;background:var(--surface-soft);color:var(--muted);font-size:9px;font-weight:750;cursor:pointer;white-space:nowrap}.speaker-menu-head button:hover{color:var(--text);background:var(--surface)}.speaker-menu-head .speaker-close{display:flex;align-items:center;justify-content:center;gap:3px}.speaker-close ha-icon{--mdc-icon-size:14px}.speaker-options{max-height:230px;overflow:auto;overscroll-behavior:contain}.speaker-option{position:relative;display:flex;align-items:center;gap:9px;min-height:39px;padding:5px 7px;border-radius:10px;cursor:pointer}.speaker-option:hover{background:var(--surface-soft)}.speaker-option input{position:absolute;opacity:0;pointer-events:none}.speaker-check{width:23px;height:23px;flex:0 0 23px;border:1px solid var(--border);border-radius:7px;display:grid;place-items:center;background:var(--surface-soft);color:transparent}.speaker-check ha-icon{--mdc-icon-size:15px}.speaker-option input:checked+.speaker-check{border-color:color-mix(in srgb,var(--accent) 55%,transparent);background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff}.speaker-name{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px;font-weight:650;color:var(--text)}.speaker-name small{margin-left:6px;padding:2px 6px;border-radius:999px;background:var(--surface);color:var(--muted);font-size:8px;font-weight:800;letter-spacing:.04em}.speaker-empty{padding:12px 8px;color:var(--muted);font-size:10px;line-height:1.45}
      .now-playing{gap:20px;padding:24px 2px 18px}.art-wrap{position:relative;width:118px;height:118px;flex:0 0 118px;border-radius:28px;padding:4px;background:linear-gradient(135deg,color-mix(in srgb,var(--accent) 78%,#fff),var(--accent2));box-shadow:0 14px 34px rgba(0,0,0,.2)}.art-wrap.pulse:after{content:"";position:absolute;inset:-7px;border-radius:34px;border:1px solid color-mix(in srgb,var(--accent2) 55%,transparent);animation:ring 2.3s ease-out infinite}.art{width:100%;height:100%;display:grid;place-items:center;border-radius:24px;background:var(--surface-strong);background-size:cover;background-position:center;overflow:hidden}.art>ha-icon{--mdc-icon-size:48px;color:var(--muted)}.track-info{min-width:0;flex:1}.track-title{font-size:24px;font-weight:850;line-height:1.12;letter-spacing:-.035em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.track-subtitle{margin-top:8px;color:var(--muted);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
      .transport{padding:4px 2px 8px}.progress-head{display:flex;justify-content:space-between;font-size:10px;color:var(--muted);font-variant-numeric:tabular-nums;margin-bottom:5px}.range{appearance:none;width:100%;height:4px;border-radius:999px;outline:none;background:linear-gradient(90deg,var(--accent) 0 var(--value,0%),var(--surface-strong) var(--value,0%) 100%)}.range::-webkit-slider-thumb{appearance:none;width:14px;height:14px;border-radius:50%;background:var(--text);box-shadow:0 2px 8px rgba(0,0,0,.25);cursor:pointer}.range::-moz-range-thumb{width:14px;height:14px;border:0;border-radius:50%;background:var(--text);cursor:pointer}.progress{height:5px}.controls{justify-content:center;gap:13px;margin:18px 0 16px}.icon-btn,.play-btn,.soft-btn{border:0;cursor:pointer;display:grid;place-items:center;transition:transform .18s ease,background .18s ease,opacity .18s ease}.icon-btn{width:43px;height:43px;border-radius:50%;background:var(--surface)}.icon-btn:hover,.soft-btn:hover{background:var(--surface-strong);transform:translateY(-1px)}.play-btn{width:62px;height:62px;border-radius:50%;background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;box-shadow:0 10px 28px color-mix(in srgb,var(--accent) 34%,transparent)}.play-btn:hover{transform:scale(1.045)}.play-btn ha-icon{--mdc-icon-size:31px}.busy ha-icon,.spinning ha-icon{animation:spin 1s linear infinite}.volume-row{gap:10px;color:var(--muted)}.volume-row ha-icon{--mdc-icon-size:18px}.volume-row .range{flex:1}.volume-row span{width:36px;text-align:right;font-size:10px;font-variant-numeric:tabular-nums}
      .tabs{gap:7px;margin:16px 0 12px;padding:5px;border-radius:15px;background:var(--surface-soft);border:1px solid var(--border-soft)}.tabs button{position:relative;flex:1;height:38px;border:0;border-radius:11px;background:transparent;color:var(--muted);font-size:12px;font-weight:750;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:7px;min-width:0}.tabs button.active{background:var(--surface-strong);color:var(--text);box-shadow:0 4px 12px rgba(0,0,0,.07)}.tabs ha-icon{--mdc-icon-size:18px}.tabs b{font-size:9px;padding:2px 6px;border-radius:9px;background:var(--surface-strong)}.tab-pulse{width:6px;height:6px;border-radius:50%;background:#34c88a;box-shadow:0 0 10px rgba(52,200,138,.65);animation:pulseDot 1.3s ease-in-out infinite}
      .panel{display:none}.panel.active{display:block}.url-box{gap:9px;padding:8px 8px 8px 13px;border-radius:16px;background:var(--surface-soft);border:1px solid var(--border)}.url-box>ha-icon,.search-box>ha-icon{color:var(--muted);--mdc-icon-size:20px}.url-box input,.search-box input{min-width:0;flex:1;border:0;outline:0;color:var(--text);background:transparent}.url-box input::placeholder,.search-box input::placeholder{color:color-mix(in srgb,var(--muted) 75%,transparent)}.accent-btn{height:38px;border:0;border-radius:11px;padding:0 14px;display:flex;align-items:center;gap:6px;background:linear-gradient(135deg,var(--accent),color-mix(in srgb,var(--accent2) 72%,var(--accent)));color:#fff;font-size:12px;font-weight:800;cursor:pointer}.accent-btn:disabled{opacity:.45;cursor:default}.accent-btn ha-icon{--mdc-icon-size:17px}
      .library-list{overflow:auto;margin:0 -4px;padding:2px 4px 4px;scrollbar-width:thin}.library-list.scroll-five{max-height:260px}
      .library-tools{gap:8px}.search-box{flex:1;height:42px;gap:8px;padding:0 12px;border-radius:13px;background:var(--surface-soft);border:1px solid var(--border)}.soft-btn{width:42px;height:42px;border-radius:13px;background:var(--surface)}.library-meta{justify-content:space-between;gap:8px;padding:9px 2px 7px;color:var(--muted);font-size:9px}.library-meta span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.track-row{width:100%;min-height:52px;gap:10px;text-align:left;border:0;border-radius:13px;padding:9px 10px;background:transparent;cursor:pointer}.track-row:hover,.track-row.selected{background:var(--surface)}.track-icon{width:34px;height:34px;flex:0 0 34px;border-radius:10px;display:grid;place-items:center;background:var(--surface);color:var(--muted)}.track-row.selected .track-icon{background:color-mix(in srgb,var(--accent) 18%,var(--card-bg));color:color-mix(in srgb,var(--accent) 80%,var(--text))}.track-icon ha-icon{--mdc-icon-size:18px}.track-text{flex:1;min-width:0;display:flex;flex-direction:column;gap:3px}.track-text strong,.track-text small{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.track-text strong{font-size:12px;font-weight:750}.track-text small{font-size:9px;color:var(--muted)}.track-size{font-size:9px;color:var(--muted);white-space:nowrap}.row-play{opacity:.65;color:var(--text);transition:opacity .15s}.track-row:hover .row-play,.track-row.selected .row-play{opacity:1}.row-play ha-icon{--mdc-icon-size:18px}.empty{height:120px;display:flex;align-items:center;justify-content:center;gap:8px;color:var(--muted);font-size:12px}.empty.compact{height:86px}.empty ha-icon{--mdc-icon-size:20px}
      .player-url-panel{margin-top:12px}.heart-btn{width:38px;height:38px;flex:0 0 38px;border-radius:11px;color:color-mix(in srgb,var(--accent) 78%,var(--text))}.heart-btn:disabled{opacity:.4;cursor:default}
      .music-search-box{display:flex;align-items:center;gap:9px;margin-top:9px;padding:8px 8px 8px 13px;border-radius:16px;background:var(--surface-soft);border:1px solid var(--border)}.music-search-box>ha-icon{color:var(--muted);--mdc-icon-size:20px}.music-search-box input{min-width:0;flex:1;border:0;outline:0;color:var(--text);background:transparent}.music-search-box input::placeholder{color:color-mix(in srgb,var(--muted) 75%,transparent)}.music-search-box .accent-btn.busy ha-icon,.music-search-state.loading ha-icon{animation:spin 1s linear infinite}.music-search-results{margin-top:7px}.music-search-meta{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:5px 3px;color:var(--muted);font-size:9px}.music-search-meta span{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.music-search-meta b{white-space:nowrap}.music-search-list{max-height:520px;overflow:auto;overscroll-behavior:contain;padding-right:2px}.music-search-row{display:grid;grid-template-columns:minmax(0,1fr) 36px;align-items:center;gap:5px;min-height:64px;padding:5px;border-radius:14px}.music-search-row:hover{background:var(--surface-soft)}.search-main:disabled{opacity:.55;cursor:default}.search-favorite-btn{width:34px;height:34px;border:0;border-radius:10px;display:grid;place-items:center;background:transparent;color:var(--muted);cursor:pointer}.search-favorite-btn:hover{background:var(--surface);color:color-mix(in srgb,var(--accent) 80%,var(--text))}.search-favorite-btn.saved{color:color-mix(in srgb,var(--accent) 84%,var(--text))}.search-favorite-btn:disabled{opacity:.4;cursor:default}.search-favorite-btn ha-icon{--mdc-icon-size:18px}.music-search-state{min-height:78px;display:flex;align-items:center;justify-content:center;gap:8px;color:var(--muted);font-size:10px;text-align:center}.music-search-state.error{color:#d63e50}.music-search-state.error ha-icon{animation:none}
      .favorites-player{margin-top:12px;padding:14px;border:1px solid var(--border);border-radius:18px;background:var(--surface-soft);box-shadow:0 8px 24px rgba(0,0,0,.06)}.favorites-player-head{display:flex;align-items:center;gap:11px;min-width:0}.favorites-player-icon{width:42px;height:42px;flex:0 0 42px;border-radius:13px;display:grid;place-items:center;background:color-mix(in srgb,var(--accent) 13%,var(--card-bg));color:color-mix(in srgb,var(--accent) 82%,var(--text))}.favorites-player-icon ha-icon{--mdc-icon-size:22px}.favorites-player-copy{min-width:0;display:flex;flex:1;flex-direction:column;gap:2px}.favorites-player-copy small{font-size:8px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:color-mix(in srgb,var(--accent) 75%,var(--muted))}.favorites-player-copy strong,.favorites-player-copy span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.favorites-player-copy strong{font-size:12px}.favorites-player-copy span{font-size:9px;color:var(--muted)}.favorites-transport{padding-top:13px}.favorites-transport .controls{margin:15px 0 13px}.favorite-tabs{margin-top:14px}.favorite-add{display:flex;align-items:center;gap:9px;padding:8px 8px 8px 13px;margin-bottom:9px;border-radius:16px;background:var(--surface-soft);border:1px solid var(--border)}.favorite-add>ha-icon{color:var(--muted);--mdc-icon-size:20px}.favorite-add input{min-width:0;flex:1;border:0;outline:0;color:var(--text);background:transparent}.favorite-add input::placeholder{color:color-mix(in srgb,var(--muted) 75%,transparent)}.favorite-tools{margin-bottom:8px}.selection-toolbar{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin:8px 0}.selection-left,.selection-actions{display:flex;align-items:center;gap:6px;flex-wrap:wrap}.selection-left>span{font-size:9px;color:var(--muted);white-space:nowrap}.soft-btn.small{width:auto;height:32px;padding:0 9px;border-radius:10px;display:flex;align-items:center;justify-content:center;gap:5px;font-size:9px;font-weight:750}.soft-btn.small:disabled{opacity:.4;cursor:default;transform:none}.soft-btn.small ha-icon{--mdc-icon-size:15px}.soft-btn.danger{color:#d63e50}.repeat-one,.repeat-all{color:color-mix(in srgb,var(--accent) 82%,var(--text));background:color-mix(in srgb,var(--accent) 11%,var(--card-bg))}.accent-btn.compact{height:32px;padding:0 10px;border-radius:10px;font-size:9px}.favorite-meta{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:4px 2px 7px;color:var(--muted);font-size:9px}.favorite-meta span:first-child{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.favorite-list{max-height:520px;overflow:auto;overscroll-behavior:contain;overflow-anchor:none;padding-right:2px}.favorite-row{display:grid;grid-template-columns:32px minmax(0,1fr) 34px;align-items:center;gap:5px;min-height:64px;padding:5px;border-radius:14px}.favorite-row:hover,.favorite-row.selected{background:var(--surface-soft)}.check-btn,.delete-btn{width:32px;height:32px;border:0;border-radius:9px;display:grid;place-items:center;background:transparent;color:var(--muted);cursor:pointer}.check-btn:hover,.delete-btn:hover{background:var(--surface)}.check-btn.checked{color:color-mix(in srgb,var(--accent) 80%,var(--text))}.delete-btn:hover{color:#d63e50}.check-btn ha-icon,.delete-btn ha-icon{--mdc-icon-size:18px}.favorite-main{min-width:0;width:100%;border:0;background:transparent;display:flex;align-items:center;gap:10px;text-align:left;cursor:pointer;padding:3px}.favorite-thumb{width:52px;height:52px;flex:0 0 52px;border-radius:11px;display:grid;place-items:center;background:var(--surface);background-size:cover;background-position:center;color:var(--muted);overflow:hidden}.favorite-thumb.has-art{box-shadow:inset 0 0 0 1px var(--border-soft)}.favorite-thumb.local{background:color-mix(in srgb,var(--accent) 9%,var(--card-bg));color:color-mix(in srgb,var(--accent) 75%,var(--text))}.favorite-thumb ha-icon{--mdc-icon-size:23px}.favorite-copy{min-width:0;flex:1;display:flex;flex-direction:column;gap:5px}.favorite-copy strong,.favorite-copy small{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.favorite-copy strong{font-size:12px;font-weight:780}.favorite-copy small{font-size:9px;color:var(--muted)}.favorite-main .row-play{flex:0 0 auto;padding:0 3px}.favorite-row.selected .row-play{color:color-mix(in srgb,var(--accent) 80%,var(--text));opacity:1}
      .pagination{justify-content:center;gap:5px;margin-top:8px;min-height:30px}.pagination button{min-width:29px;height:29px;padding:0 8px;border:1px solid var(--border-soft);border-radius:9px;background:var(--surface-soft);color:var(--muted);cursor:pointer;font-size:10px;font-weight:750}.pagination button:hover{color:var(--text);background:var(--surface)}.pagination button.active{border-color:color-mix(in srgb,var(--accent) 42%,transparent);background:color-mix(in srgb,var(--accent) 14%,var(--card-bg));color:color-mix(in srgb,var(--accent) 76%,var(--text))}.page-gap{color:var(--muted);font-size:10px}
      .download-hero{gap:11px;margin-bottom:12px;padding:12px 13px;border-radius:16px;background:color-mix(in srgb,var(--accent) 8%,var(--card-bg));border:1px solid color-mix(in srgb,var(--accent) 18%,var(--border))}.download-hero-icon{width:39px;height:39px;display:grid;place-items:center;border-radius:12px;background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff}.download-hero-icon ha-icon{--mdc-icon-size:22px}.download-hero>div:last-child{display:flex;flex-direction:column;gap:3px}.download-hero strong{font-size:13px}.download-hero span{font-size:9px;color:var(--muted)}
      .input-shell{min-width:0;display:flex;flex-direction:column;gap:7px;padding:10px 12px;border-radius:14px;background:var(--surface-soft);border:1px solid var(--border)}.input-shell.full{margin-bottom:9px}.input-shell>span{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:9px;font-weight:700}.input-shell>span ha-icon{--mdc-icon-size:15px}.input-shell input,.input-shell select{width:100%;min-width:0;border:0;outline:0;background:transparent;color:var(--text);font-size:12px;font-weight:650}.input-shell input::placeholder{color:color-mix(in srgb,var(--muted) 70%,transparent)}.input-shell select{appearance:none}.option-grid{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-top:9px}.download-type{display:grid;grid-template-columns:1fr 1fr;gap:6px;padding:5px;border-radius:14px;background:var(--surface-soft);border:1px solid var(--border-soft)}.download-type button{height:38px;border:0;border-radius:10px;background:transparent;color:var(--muted);cursor:pointer;display:flex;align-items:center;justify-content:center;gap:7px;font-size:11px;font-weight:750}.download-type button.active{color:#fff;background:linear-gradient(135deg,var(--accent),color-mix(in srgb,var(--accent2) 68%,var(--accent)))}.download-type ha-icon{--mdc-icon-size:17px}
      .toggle-list{margin-top:9px;border-radius:15px;overflow:hidden;border:1px solid var(--border-soft);background:var(--surface-soft)}.toggle-row{position:relative;display:flex;align-items:center;justify-content:space-between;gap:14px;padding:11px 12px;cursor:pointer}.toggle-row+.toggle-row{border-top:1px solid var(--border-soft)}.toggle-row>span{display:flex;flex-direction:column;gap:2px}.toggle-row strong{font-size:10px}.toggle-row small{font-size:8px;color:var(--muted)}.toggle-row input{position:absolute;opacity:0;pointer-events:none}.toggle-row i{position:relative;width:36px;height:20px;flex:0 0 36px;border-radius:999px;background:var(--surface-strong);transition:.2s}.toggle-row i:after{content:"";position:absolute;top:3px;left:3px;width:14px;height:14px;border-radius:50%;background:var(--card-bg);box-shadow:0 2px 7px rgba(0,0,0,.22);transition:.2s}.toggle-row input:checked+i{background:linear-gradient(135deg,var(--accent),var(--accent2))}.toggle-row input:checked+i:after{transform:translateX(16px)}
      .job-card{margin-top:10px;padding:13px;border-radius:16px;background:var(--surface-soft);border:1px solid var(--border)}.job-card.done{border-color:rgba(40,183,124,.30);background:color-mix(in srgb,#28b77c 8%,var(--card-bg))}.job-card.failed{border-color:rgba(214,62,80,.30);background:color-mix(in srgb,#d63e50 8%,var(--card-bg))}.job-top{gap:10px}.job-status-icon{width:34px;height:34px;flex:0 0 34px;border-radius:11px;display:grid;place-items:center;background:var(--surface);color:var(--muted)}.job-status-icon.working{color:#159b63;background:color-mix(in srgb,#28b77c 10%,var(--card-bg))}.job-status-icon.working ha-icon{animation:downloadFloat 1.25s ease-in-out infinite}.job-title{min-width:0;flex:1;display:flex;flex-direction:column;gap:3px}.job-title strong{font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.job-title span{font-size:9px;color:var(--muted)}.job-top>b{font-size:11px;font-variant-numeric:tabular-nums}.download-progress{height:6px;margin-top:11px;overflow:hidden;border-radius:999px;background:var(--surface-strong)}.download-progress i{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,var(--accent),var(--accent2));box-shadow:0 0 14px color-mix(in srgb,var(--accent2) 35%,transparent);transition:width .3s ease}.job-metrics{gap:12px;flex-wrap:wrap;margin-top:9px;color:var(--muted);font-size:8px}.job-metrics>span{display:flex;align-items:center;gap:4px}.job-metrics ha-icon{--mdc-icon-size:13px}.job-metrics .metric-hidden{display:none}.result-file{gap:6px;margin-top:9px;padding-top:9px;border-top:1px solid var(--border-soft);font-size:9px;color:var(--muted);min-width:0}.result-file ha-icon{--mdc-icon-size:15px;color:#159b63}.result-file span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.job-error{display:flex;align-items:flex-start;gap:6px;margin-top:9px;padding:8px;border-radius:10px;background:color-mix(in srgb,#d63e50 9%,var(--card-bg));color:#d63e50;font-size:9px;line-height:1.35}.job-error ha-icon{--mdc-icon-size:15px;flex:0 0 auto}
      .download-btn{width:100%;height:45px;margin-top:11px;border:0;border-radius:14px;display:flex;align-items:center;justify-content:center;gap:8px;background:linear-gradient(135deg,var(--accent),color-mix(in srgb,var(--accent2) 72%,var(--accent)));color:#fff;font-size:12px;font-weight:850;cursor:pointer;box-shadow:0 9px 24px color-mix(in srgb,var(--accent) 22%,transparent);transition:transform .18s ease,opacity .18s ease}.download-btn:hover:not(:disabled){transform:translateY(-1px)}.download-btn:disabled{opacity:.45;cursor:default;box-shadow:none}.download-btn ha-icon{--mdc-icon-size:19px}
      @keyframes spin{to{transform:rotate(360deg)}}@keyframes ring{0%{opacity:.7;transform:scale(.97)}100%{opacity:0;transform:scale(1.1)}}@keyframes floatbg{from{transform:scale(1.1) translate3d(-1%,0,0)}to{transform:scale(1.18) translate3d(1%,1%,0)}}@keyframes noticeIn{from{opacity:0;transform:translateY(-5px)}to{opacity:1;transform:none}}@keyframes pulseDot{50%{opacity:.35;transform:scale(.75)}}@keyframes downloadFloat{50%{transform:translateY(2px)}}
      @container (max-width:520px){.surface{padding:16px}.selection-toolbar{align-items:stretch}.selection-left,.selection-actions{width:100%}.selection-actions{justify-content:flex-end}.favorite-list{max-height:430px}.primary-tabs button{height:45px;font-size:11px;gap:7px}.primary-tabs ha-icon{--mdc-icon-size:20px}.now-playing{gap:15px;padding-top:20px}.art-wrap{width:94px;height:94px;flex-basis:94px;border-radius:23px}.art{border-radius:19px}.track-title{font-size:clamp(17px,6cqi,20px)}.controls{gap:9px}.icon-btn{width:40px;height:40px}.play-btn{width:58px;height:58px}.library-list.scroll-five{max-height:250px}.track-size{display:none}.tabs button{font-size:11px;gap:5px}.tabs button span{max-width:62px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.option-grid{grid-template-columns:1fr}.download-hero span{display:none}.speaker-menu-head{align-items:flex-start;flex-wrap:wrap}.speaker-menu-head>div{width:100%;justify-content:flex-end;flex-wrap:wrap}}
      @container (max-width:380px){.favorite-row{grid-template-columns:28px minmax(0,1fr) 30px;padding:4px}.favorite-thumb{width:44px;height:44px;flex-basis:44px}.favorite-copy strong{font-size:11px}.selection-left>span{width:100%}.selection-actions .soft-btn.small{flex:1}.selection-actions .accent-btn.compact{flex:1;justify-content:center}.favorite-add{flex-wrap:wrap}.favorite-add>ha-icon{display:none}.favorite-add input{flex-basis:100%;height:30px}.favorite-add .accent-btn{width:100%;justify-content:center}.brand-icon{width:38px;height:38px;flex-basis:38px;border-radius:12px}.brand-icon ha-icon{--mdc-icon-size:23px}.activity-badge{height:26px;padding:0 7px;gap:4px;font-size:9px}.activity-badge ha-icon{--mdc-icon-size:13px}.primary-tabs button span{font-size:10px}.primary-tabs{padding:5px;gap:6px;margin-top:14px}.speaker-row{margin-top:14px}.speaker-menu{padding:8px}.speaker-menu-head>div{justify-content:flex-start}.speaker-menu-head button{flex:1;padding:0 5px}.speaker-menu-head .speaker-close span{display:none}.now-playing{gap:12px}.art-wrap{width:78px;height:78px;flex-basis:78px;border-radius:20px}.art{border-radius:16px}.art>ha-icon{--mdc-icon-size:38px}.track-title{font-size:17px}.track-subtitle{font-size:11px;margin-top:5px}.controls{gap:6px;margin:15px 0 14px}.icon-btn{width:36px;height:36px}.play-btn{width:52px;height:52px}.play-btn ha-icon{--mdc-icon-size:28px}.tabs b{display:none}.tabs button span{display:none}.tabs button{height:40px}.tabs ha-icon{--mdc-icon-size:20px}.url-box{flex-wrap:wrap;padding:9px}.url-box>ha-icon{display:none}.url-box input{flex-basis:100%;height:32px}.url-box .accent-btn{width:100%;justify-content:center}.music-search-box{flex-wrap:wrap;padding:9px}.music-search-box>ha-icon{display:none}.music-search-box input{flex-basis:100%;height:32px}.music-search-box .accent-btn{width:100%;justify-content:center}.music-search-row{grid-template-columns:minmax(0,1fr) 32px;padding:4px}.search-favorite-btn{width:30px;height:30px}.download-hero{padding:10px}.pagination{flex-wrap:wrap}}
      @container (max-width:300px){.surface{padding:12px}.eyebrow{display:none}.brand{gap:9px}.primary-tabs button span{display:none}.primary-tabs button{height:42px}.field{padding:0 10px}.speaker-summary{font-size:11px}.speaker-menu-head strong{width:100%}.speaker-menu-head>div{display:grid;grid-template-columns:1fr 1fr;width:100%}.speaker-menu-head .speaker-close{grid-column:1/-1}.now-playing{align-items:flex-start}.art-wrap{width:68px;height:68px;flex-basis:68px}.controls{gap:4px}.icon-btn{width:34px;height:34px}.play-btn{width:48px;height:48px}.volume-row{gap:7px}.library-meta span:first-child{max-width:65%}}
      @media(max-width:520px){:host{max-width:100%}}
      @media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
    `;
  }
}

if (!customElements.get(CARD_TAG)) customElements.define(CARD_TAG, YtDlpMediaCard);

window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === CARD_TAG)) {
  window.customCards.push({
    type: CARD_TAG,
    name: "YouTube-DLP Media Player",
    description: "Phát YouTube, Yêu thích Online/Offline, điều khiển loa và tải video/audio trực tiếp từ dashboard.",
    preview: true,
  });
}
