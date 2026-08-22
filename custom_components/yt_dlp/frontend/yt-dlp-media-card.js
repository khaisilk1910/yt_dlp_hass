const CARD_TAG = "yt-dlp-media-card";

class YtDlpMediaCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._hass = null;
    this._selectedPlayer = "";
    this._selectedPlayers = [];
    this._speakerPickerOpen = false;
    this._view = "player";
    this._tab = "youtube";
    this._youtubeUrl = "";
    this._library = [];
    this._libraryPath = "";
    this._libraryLoaded = false;
    this._libraryLoading = false;
    this._query = "";
    this._libraryPage = 1;
    this._pageSize = 10;
    this._nowPlaying = null;
    this._currentLibraryIndex = -1;
    this._busy = false;
    this._tickTimer = null;

    this._downloadForm = {
      url: "",
      media_type: "video",
      video_quality: "1080",
      video_format: "mp4",
      audio_format: "mp3",
      audio_quality: "192",
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
        { name: "media_players", selector: { entity: { filter: { domain: "media_player" }, multiple: true, reorder: true } } },
        {
          name: "background_opacity",
          selector: {
            number: {
              min: 0,
              max: 100,
              step: 1,
              mode: "slider",
              unit_of_measurement: "%",
            },
          },
        },
        {
          name: "color_preset",
          selector: {
            select: {
              mode: "dropdown",
              options: presets.map(([value, label]) => ({ value, label })),
            },
          },
        },
      ],
      computeLabel: (schema) => ({
        subtitle: "Dòng tiêu đề trên",
        title: "Tiêu đề thẻ",
        media_players: "Loa mặc định",
        background_opacity: "Độ trong suốt nền thẻ",
        color_preset: "Mẫu màu",
      })[schema.name],
      computeHelper: (schema) => ({
        media_players: "Có thể chọn một hoặc nhiều loa để phát cùng lúc; loa đầu tiên là loa chính hiển thị trạng thái. Cấu hình media_player cũ vẫn được hỗ trợ.",
        background_opacity: "0% = nền thẻ trong suốt hoàn toàn; 100% = nền thẻ hiển thị đầy đủ. Giá trị phần trăm được hiển thị ngay trên thanh trượt.",
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
    const configuredOpacity = Number(this._config.background_opacity);
    this._config.background_opacity = Number.isFinite(configuredOpacity)
      ? Math.max(0, Math.min(100, configuredOpacity))
      : 100;
    const configuredPlayers = this._configuredPlayers();
    if (configuredPlayers.length) {
      this._selectedPlayers = configuredPlayers;
      this._selectedPlayer = configuredPlayers[0];
    }
    this._render();
  }

  set hass(hass) {
    const previous = this._hass;
    const previousSelected = this._selectedPlayer;
    const previousSelection = this._selectedPlayers.join("|");
    const previousState = previous?.states?.[previousSelected];
    const previousPlayers = this._playerSignature(previous);
    this._hass = hass;
    const players = this._players();

    this._selectedPlayers = this._selectedPlayers.filter((entityId) => Boolean(hass.states[entityId]));
    if (!this._selectedPlayers.length) {
      const configured = this._configuredPlayers().filter((entityId) => Boolean(hass.states[entityId]));
      this._selectedPlayers = configured.length
        ? configured
        : (players[0]?.entity_id ? [players[0].entity_id] : []);
    }
    this._selectedPlayer = this._selectedPlayers[0] || "";

    const playerChanged = previousState !== hass.states[this._selectedPlayer];
    const selectionChanged = previousSelected !== this._selectedPlayer || previousSelection !== this._selectedPlayers.join("|");
    const playersChanged = previousPlayers !== this._playerSignature(hass);
    const shouldRender = !previous || selectionChanged || playersChanged || (this._view === "player" && playerChanged);
    if (shouldRender) this._render();
    if (!previous && !this._libraryLoaded && !this._libraryLoading) {
      this._loadLibrary(false);
    }
  }

  connectedCallback() {
    if (!this._tickTimer) {
      this._tickTimer = window.setInterval(() => this._tick(), 1000);
    }
  }

  disconnectedCallback() {
    if (this._tickTimer) window.clearInterval(this._tickTimer);
    this._tickTimer = null;
  }

  getCardSize() {
    return 8;
  }

  _players() {
    if (!this._hass) return [];
    return Object.values(this._hass.states)
      .filter((state) => state.entity_id.startsWith("media_player."))
      .sort((a, b) => this._friendlyName(a).localeCompare(this._friendlyName(b)));
  }

  _configuredPlayers() {
    const multi = Array.isArray(this._config?.media_players)
      ? this._config.media_players
      : (typeof this._config?.media_players === "string" ? [this._config.media_players] : []);
    const legacy = Array.isArray(this._config?.media_player)
      ? this._config.media_player
      : (typeof this._config?.media_player === "string" ? [this._config.media_player] : []);
    return [...new Set([...multi, ...legacy].filter((entityId) => typeof entityId === "string" && entityId.startsWith("media_player.")))];
  }

  _targetPlayers() {
    if (!this._hass) return [];
    return [...new Set(this._selectedPlayers)]
      .filter((entityId) => Boolean(this._hass.states[entityId]));
  }

  _setSelectedPlayers(entityIds) {
    let valid = [...new Set(entityIds)]
      .filter((entityId) => typeof entityId === "string" && this._hass?.states?.[entityId]);
    if (!valid.length && this._selectedPlayer && this._hass?.states?.[this._selectedPlayer]) {
      valid = [this._selectedPlayer];
    }
    if (this._selectedPlayer && valid.includes(this._selectedPlayer)) {
      valid = [this._selectedPlayer, ...valid.filter((entityId) => entityId !== this._selectedPlayer)];
    }
    this._selectedPlayers = valid;
    this._selectedPlayer = valid[0] || "";
    this._nowPlaying = null;
    this._currentLibraryIndex = -1;
  }

  _speakerSummary() {
    const selected = this._targetPlayers();
    if (!selected.length) return "Chọn loa";
    if (selected.length === 1) return this._friendlyName(this._hass?.states?.[selected[0]]);
    return `${this._friendlyName(this._hass?.states?.[selected[0]])} + ${selected.length - 1} loa`;
  }

  _playerSignature(hass) {
    if (!hass?.states) return "";
    return Object.values(hass.states)
      .filter((state) => state.entity_id.startsWith("media_player."))
      .map((state) => `${state.entity_id}:${state.attributes?.friendly_name || ""}`)
      .sort()
      .join("|");
  }

  _friendlyName(state) {
    return state?.attributes?.friendly_name || state?.entity_id || "Media player";
  }

  _state() {
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
    if (this._nowPlaying?.thumbnail) return this._nowPlaying.thumbnail;
    const picture = state?.attributes?.entity_picture;
    if (picture) {
      if (picture.startsWith("/")) return this._hass?.hassUrl(picture) || picture;
      return picture;
    }
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
    const opacity = Number(this._config.background_opacity);
    const safeOpacity = Number.isFinite(opacity) ? Math.max(0, Math.min(100, opacity)) : 100;
    return `--accent:${accent};--accent2:${accent2};--card-opacity:${safeOpacity / 100}`;
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
    if (job.status === "postprocessing" || job.status === "completed") return 100;
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

  _render() {
    if (!this.shadowRoot) return;
    const state = this._state();
    const players = this._players();
    const isPlaying = state?.state === "playing";
    const isPaused = state?.state === "paused";
    const { position, duration } = this._position(state);
    const progress = duration > 0 ? Math.min(100, (position / duration) * 100) : 0;
    const volume = Math.round(Number(state?.attributes?.volume_level ?? 0) * 100);
    const muted = Boolean(state?.attributes?.is_volume_muted);
    const art = this._artUrl(state);
    const mediaTitle = this._nowPlaying?.title || state?.attributes?.media_title || "Sẵn sàng phát nhạc";
    const mediaArtist = this._nowPlaying?.artist || state?.attributes?.media_artist || this._friendlyName(state);
    const playerStatus = isPlaying ? "Đang phát" : isPaused ? "Tạm dừng" : state?.state === "off" ? "Đã tắt" : "Sẵn sàng";
    const headerStatus = this._isDownloadActive() ? "Đang tải" : playerStatus;
    const filteredLibrary = this._view === "player"
      ? this._library
        .map((item, index) => ({ item, index }))
        .filter(({ item }) => !this._query || `${item.title} ${item.filename}`.toLowerCase().includes(this._query.toLowerCase()))
      : [];
    const libraryPageData = this._pageData(filteredLibrary, this._libraryPage);
    this._libraryPage = libraryPageData.page;

    const playerView = this._view === "player" ? `
      <div class="speaker-row">
        <details class="speaker-picker" id="speakerPicker" ${this._speakerPickerOpen ? "open" : ""}>
          <summary class="field speaker-field" aria-label="Chọn một hoặc nhiều loa">
            ${this._icon("speaker-multiple")}
            <span class="speaker-summary">${this._escape(this._speakerSummary())}</span>
            <span class="speaker-count">${this._targetPlayers().length}</span>
            ${this._icon("chevron-down")}
          </summary>
          <div class="speaker-menu">
            <div class="speaker-menu-head">
              <strong>Phát trên loa</strong>
              <div>
                <button type="button" id="selectAllSpeakers">Tất cả</button>
                <button type="button" id="clearSpeakers">Chỉ loa chính</button>
              </div>
            </div>
            <div class="speaker-options">
              ${players.map((player) => `
                <label class="speaker-option">
                  <input type="checkbox" data-speaker-id="${this._escape(player.entity_id)}" ${this._selectedPlayers.includes(player.entity_id) ? "checked" : ""}>
                  <span class="speaker-check">${this._icon("check")}</span>
                  <span class="speaker-name">${this._escape(this._friendlyName(player))}</span>
                </label>`).join("")}
            </div>
          </div>
        </details>
      </div>

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
          <button class="icon-btn" id="stop" title="Dừng">${this._icon("stop")}</button>
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

      <nav class="tabs secondary-tabs">
        <button data-tab="youtube" class="${this._tab === "youtube" ? "active" : ""}">${this._icon("youtube")} <span>Phát</span></button>
        <button data-tab="library" class="${this._tab === "library" ? "active" : ""}">${this._icon("playlist-music")} <span>Thư viện</span> <b>${this._library.length}</b></button>
      </nav>

      <section class="panel ${this._tab === "youtube" ? "active" : ""}" id="youtubePanel">
        <div class="url-box">
          ${this._icon("link-variant")}
          <input id="youtubeUrl" type="url" value="${this._escape(this._youtubeUrl)}" placeholder="Dán link YouTube để phát trực tiếp..." autocomplete="off">
          <button id="playUrl" class="accent-btn" ${!this._selectedPlayer || this._busy ? "disabled" : ""}>${this._icon("play")} Phát</button>
        </div>
      </section>

      <section class="panel ${this._tab === "library" ? "active" : ""}" id="libraryPanel">
        <div class="library-tools">
          <div class="search-box">${this._icon("magnify")}<input id="librarySearch" value="${this._escape(this._query)}" placeholder="Tìm trong thư viện..."></div>
          <button id="refreshLibrary" class="soft-btn ${this._libraryLoading ? "spinning" : ""}" title="Quét lại thư mục">${this._icon("refresh")}</button>
        </div>
        <div class="library-meta"><span>${this._escape(this._libraryPath || "Thư mục thư viện")}</span><span>${filteredLibrary.length} / ${this._library.length} bài</span></div>
        <div class="library-list scroll-five">
          ${this._libraryLoading && !this._libraryLoaded ? `<div class="empty">${this._icon("loading")} Đang quét thư viện...</div>` : ""}
          ${!this._libraryLoading && this._libraryLoaded && !filteredLibrary.length ? `<div class="empty">${this._icon("music-off")} Không tìm thấy file nhạc.</div>` : ""}
          ${libraryPageData.items.map(({ item, index }) => `
            <button class="track-row ${index === this._currentLibraryIndex ? "selected" : ""}" data-library-index="${index}">
              <span class="track-icon">${this._icon(index === this._currentLibraryIndex && isPlaying ? "equalizer" : "music-note")}</span>
              <span class="track-text"><strong>${this._escape(item.title || item.filename)}</strong><small>${this._escape(item.filename)}</small></span>
              <span class="track-size">${this._formatSize(item.size)}</span>
              <span class="row-play">${this._icon("play")}</span>
            </button>`).join("")}
        </div>
        ${this._renderPagination("library", libraryPageData.page, libraryPageData.totalPages)}
      </section>` : "";

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
            <div class="status ${this._isDownloadActive() ? "downloading" : ""}"><span></span>${this._escape(headerStatus)}</div>
          </header>

          <nav class="primary-tabs" aria-label="YouTube-DLP">
            <button data-view="player" class="${this._view === "player" ? "active" : ""}">
              ${this._icon("play-circle-outline")}<span>Media Player</span>
            </button>
            <button data-view="download" class="${this._view === "download" ? "active" : ""}">
              ${this._icon("download-circle-outline")}<span>Download</span>${this._isDownloadActive() ? `<i class="tab-pulse"></i>` : ""}
            </button>
          </nav>

          ${this._renderDownloadNotice()}
          ${this._view === "player" ? playerView : this._renderDownloadPanel()}
        </div>
      </ha-card>`;

    this._bindEvents();
  }

  _bindEvents() {
    const $ = (id) => this.shadowRoot.getElementById(id);
    const speakerPicker = $("speakerPicker");
    speakerPicker?.addEventListener("toggle", () => {
      this._speakerPickerOpen = speakerPicker.open;
    });
    this.shadowRoot.querySelectorAll("[data-speaker-id]").forEach((checkbox) => {
      checkbox.addEventListener("change", () => {
        const selected = [...this.shadowRoot.querySelectorAll("[data-speaker-id]:checked")]
          .map((item) => item.dataset.speakerId);
        this._setSelectedPlayers(selected);
        this._speakerPickerOpen = true;
        this._render();
      });
    });
    $("selectAllSpeakers")?.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      this._setSelectedPlayers(this._players().map((player) => player.entity_id));
      this._speakerPickerOpen = true;
      this._render();
    });
    $("clearSpeakers")?.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const primary = this._selectedPlayer || this._players()[0]?.entity_id || "";
      this._setSelectedPlayers(primary ? [primary] : []);
      this._speakerPickerOpen = true;
      this._render();
    });

    this.shadowRoot.querySelectorAll("[data-view]").forEach((button) => {
      button.addEventListener("click", () => {
        this._view = button.dataset.view;
        this._render();
      });
    });

    this.shadowRoot.querySelectorAll("[data-tab]").forEach((button) => {
      button.addEventListener("click", () => {
        this._tab = button.dataset.tab;
        this._render();
      });
    });

    $("dismissDownloadNotice")?.addEventListener("click", () => {
      this._downloadNotice = null;
      this._render();
    });

    $("youtubeUrl")?.addEventListener("input", (event) => {
      this._youtubeUrl = event.target.value;
    });
    $("playUrl")?.addEventListener("click", () => this._playUrl());
    $("youtubeUrl")?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") this._playUrl();
    });
    $("playPause")?.addEventListener("click", () => this._playPause());
    $("stop")?.addEventListener("click", () => this._callMediaService("media_stop"));
    $("previous")?.addEventListener("click", () => this._previous());
    $("next")?.addEventListener("click", () => this._next());
    $("mute")?.addEventListener("click", () => this._toggleMute());
    $("volume")?.addEventListener("change", (event) => this._setVolume(Number(event.target.value)));
    $("progress")?.addEventListener("change", (event) => this._seek(Number(event.target.value)));
    $("refreshLibrary")?.addEventListener("click", () => this._loadLibrary(true));
    $("librarySearch")?.addEventListener("input", (event) => {
      this._query = event.target.value;
      this._libraryPage = 1;
      this._render();
      const search = this.shadowRoot.getElementById("librarySearch");
      if (search) {
        search.focus();
        search.setSelectionRange(this._query.length, this._query.length);
      }
    });
    this.shadowRoot.querySelectorAll("[data-library-index]").forEach((button) => {
      button.addEventListener("click", () => this._playLibrary(Number(button.dataset.libraryIndex)));
    });

    this.shadowRoot.querySelectorAll("[data-page-kind]").forEach((button) => {
      button.addEventListener("click", () => {
        const page = Number(button.dataset.page) || 1;
        if (button.dataset.pageKind === "library") this._libraryPage = page;
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

  async _playUrl() {
    const url = this._youtubeUrl.trim();
    if (!url) return this._notify("Hãy nhập link YouTube.");
    if (!this._selectedPlayer) return this._notify("Hãy chọn loa trước khi phát.");

    const selectedPlayers = this._targetPlayers();
    this._busy = true;
    this._render();
    try {
      const response = selectedPlayers.length > 1
        ? await this._callServiceResponse("yt_dlp", "play_multi", {
            url,
            media_players: selectedPlayers,
          })
        : await this._callServiceResponse("yt_dlp", "play", {
            url,
            media_player: this._selectedPlayer,
          });
      this._nowPlaying = response || { title: "YouTube audio", url };
      this._currentLibraryIndex = -1;
      const targetText = selectedPlayers.length > 1 ? ` trên ${selectedPlayers.length} loa` : "";
      this._notify(`Đang phát${targetText}: ${response?.title || "YouTube audio"}`);
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
        this._loadLibrary(true);
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

  async _loadLibrary(force) {
    if (!this._hass || this._libraryLoading) return;
    this._libraryLoading = true;
    this._render();
    try {
      const response = await this._callServiceResponse("yt_dlp", "scan_library", { force: Boolean(force) });
      this._library = Array.isArray(response?.items) ? response.items : [];
      this._libraryPath = response?.path || "";
      this._libraryLoaded = true;
    } catch (error) {
      this._notify(`Không thể quét thư viện: ${error?.message || error}`);
    } finally {
      this._libraryLoading = false;
      this._render();
    }
  }

  async _playLibrary(index) {
    const selectedPlayers = this._targetPlayers();
    if (selectedPlayers.length > 1) return this._playLibraryMulti(index, selectedPlayers);

    const item = this._library[index];
    if (!item || !this._selectedPlayer) return;
    this._busy = true;
    this._currentLibraryIndex = index;
    this._nowPlaying = { title: item.title || item.filename, artist: "Thư viện YouTube-DLP", thumbnail: null };
    this._render();
    try {
      await this._hass.callService(
        "media_player",
        "play_media",
        {
          media_content_id: item.media_content_id,
          media_content_type: item.mime_type || "music",
          extra: { metadata: { title: item.title || item.filename } },
        },
        { entity_id: this._selectedPlayer }
      );
    } catch (error) {
      this._notify(`Không thể phát file: ${error?.message || error}`);
    } finally {
      this._busy = false;
      this._render();
    }
  }

  async _playLibraryMulti(index, selectedPlayers) {
    const item = this._library[index];
    if (!item || !selectedPlayers.length || !this._hass) return;
    this._busy = true;
    this._currentLibraryIndex = index;
    this._nowPlaying = { title: item.title || item.filename, artist: "Thư viện YouTube-DLP", thumbnail: null };
    this._render();
    try {
      await this._hass.callService(
        "media_player",
        "play_media",
        {
          media_content_id: item.media_content_id,
          media_content_type: item.mime_type || "music",
          extra: { metadata: { title: item.title || item.filename } },
        },
        { entity_id: selectedPlayers }
      );
    } catch (error) {
      this._notify(`Không thể phát file: ${error?.message || error}`);
    } finally {
      this._busy = false;
      this._render();
    }
  }

  async _playPause() {
    if (!this._selectedPlayer || this._busy) return;
    const state = this._state();
    await this._callMediaService(state?.state === "playing" ? "media_pause" : "media_play");
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
    if (this._currentLibraryIndex >= 0 && this._library.length) {
      const index = (this._currentLibraryIndex - 1 + this._library.length) % this._library.length;
      return this._playLibrary(index);
    }
    return this._callMediaService("media_previous_track");
  }

  async _next() {
    if (this._currentLibraryIndex >= 0 && this._library.length) {
      const index = (this._currentLibraryIndex + 1) % this._library.length;
      return this._playLibrary(index);
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
    if (this._downloadJob && !["completed", "error", "cancelled"].includes(this._downloadJob.status)) {
      this._pollDownloadJob(false);
    }
  }

  _updateProgressOnly() {
    if (!this.shadowRoot || !this._hass) return;
    const state = this._state();
    const { position, duration } = this._position(state);
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
        min-width:0;
        max-width:100%;
        --text:var(--primary-text-color,#202124);
        --muted:var(--secondary-text-color,#6b7280);
        --card-bg:var(--ha-card-background,var(--card-background-color,#fff));
        --card-opacity:1;
        --surface-soft:color-mix(in srgb,var(--card-bg) 96%,var(--text) 4%);
        --surface:color-mix(in srgb,var(--card-bg) 91%,var(--text) 9%);
        --surface-strong:color-mix(in srgb,var(--card-bg) 84%,var(--text) 16%);
        --border:color-mix(in srgb,var(--text) 13%,transparent);
        --border-soft:color-mix(in srgb,var(--text) 8%,transparent);
        --accent:var(--primary-color,#05a8d8);
        --accent2:#18d5e8;
        color:var(--text);
        font-family:var(--paper-font-body1_-_font-family,inherit)
      }
      *{box-sizing:border-box}button,input,select{font:inherit}button{color:inherit}
      .card{position:relative;overflow:hidden;box-sizing:border-box;width:100%;min-width:0;border-radius:28px;color:var(--text);background:transparent;box-shadow:var(--ha-card-box-shadow,0 12px 34px rgba(0,0,0,.14));isolation:isolate}.card::before{content:"";position:absolute;inset:0;z-index:-1;pointer-events:none;background:linear-gradient(145deg,var(--card-bg),color-mix(in srgb,var(--card-bg) 94%,var(--text) 6%));opacity:var(--card-opacity);transition:opacity .18s ease}
      .ambient{position:absolute;inset:-40px;background-size:cover;background-position:center;filter:blur(48px) saturate(1.35);opacity:.07;transform:scale(1.12);transition:opacity .5s ease}.is-playing .ambient{opacity:.18;animation:floatbg 9s ease-in-out infinite alternate}
      .surface{position:relative;z-index:1;isolation:isolate;box-sizing:border-box;width:100%;min-width:0;padding:22px;background:transparent;backdrop-filter:blur(18px)}.surface::before{content:"";position:absolute;inset:0;z-index:-1;pointer-events:none;background:linear-gradient(180deg,color-mix(in srgb,var(--card-bg) 97%,var(--text) 3%),color-mix(in srgb,var(--card-bg) 99%,transparent));opacity:var(--card-opacity);transition:opacity .18s ease}
      header,.brand,.speaker-row,.field,.now-playing,.controls,.volume-row,.primary-tabs,.tabs,.url-box,.library-tools,.search-box,.library-meta,.track-row,.download-hero,.job-top,.job-metrics,.result-file,.download-notice,.pagination{display:flex;align-items:center}
      header{justify-content:space-between;gap:16px;min-width:0}.brand{gap:12px;min-width:0}.brand>div:last-child{min-width:0}.brand-title,.eyebrow{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.brand-icon{width:44px;height:44px;border-radius:14px;display:grid;place-items:center;background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;box-shadow:0 8px 24px color-mix(in srgb,var(--accent) 30%,transparent)}.brand-icon ha-icon{--mdc-icon-size:26px}.eyebrow{font-size:10px;font-weight:800;letter-spacing:.19em;color:var(--muted)}.brand-title{font-size:18px;font-weight:800;letter-spacing:-.02em}.status{gap:7px;padding:8px 11px;border:1px solid var(--border-soft);border-radius:999px;background:var(--surface-soft);font-size:11px;color:var(--muted)}.status span{width:7px;height:7px;border-radius:50%;background:var(--muted);box-shadow:0 0 0 4px color-mix(in srgb,var(--muted) 12%,transparent)}.is-playing .status span,.status.downloading span{background:#28b77c;box-shadow:0 0 0 4px rgba(40,183,124,.13),0 0 12px rgba(40,183,124,.52)}
      .download-notice{gap:11px;margin-top:16px;padding:12px 13px;border-radius:16px;border:1px solid var(--border);animation:noticeIn .25s ease}.download-notice.success{background:color-mix(in srgb,#28b77c 12%,var(--card-bg));border-color:color-mix(in srgb,#28b77c 34%,transparent)}.download-notice.error{background:color-mix(in srgb,#e84c5b 11%,var(--card-bg));border-color:color-mix(in srgb,#e84c5b 34%,transparent)}.notice-icon{width:36px;height:36px;flex:0 0 36px;border-radius:12px;display:grid;place-items:center;background:var(--surface)}.success .notice-icon{color:#159b63}.error .notice-icon{color:#d63e50}.notice-icon ha-icon{--mdc-icon-size:21px}.notice-copy{min-width:0;flex:1;display:flex;flex-direction:column;gap:3px}.notice-copy strong{font-size:12px}.notice-copy span{font-size:10px;color:var(--muted);line-height:1.35;overflow-wrap:anywhere}.notice-close{width:30px;height:30px;border:0;border-radius:9px;background:transparent;color:var(--muted);cursor:pointer;display:grid;place-items:center}.notice-close:hover{background:var(--surface);color:var(--text)}.notice-close ha-icon{--mdc-icon-size:17px}
      .primary-tabs{gap:8px;margin-top:18px;padding:6px;border-radius:18px;background:var(--surface-soft);border:1px solid var(--border-soft)}.primary-tabs button{position:relative;flex:1;height:48px;border:0;border-radius:13px;background:transparent;color:var(--muted);cursor:pointer;display:flex;align-items:center;justify-content:center;gap:9px;font-size:12px;font-weight:850;letter-spacing:.01em;transition:background .18s ease,color .18s ease,transform .18s ease,box-shadow .18s ease}.primary-tabs button:hover{color:var(--text);background:var(--surface)}.primary-tabs button.active{color:#fff;background:linear-gradient(135deg,var(--accent),color-mix(in srgb,var(--accent2) 68%,var(--accent)));box-shadow:0 8px 22px color-mix(in srgb,var(--accent) 18%,transparent)}.primary-tabs ha-icon{--mdc-icon-size:21px}.primary-tabs .tab-pulse{position:absolute;right:12px;top:10px}
      .speaker-row{position:relative;margin-top:18px;z-index:12}.field{width:100%;height:46px;gap:10px;padding:0 14px;border-radius:15px;border:1px solid var(--border);background:var(--surface-soft)}.field>ha-icon{color:var(--muted);--mdc-icon-size:20px}.field select{appearance:none;flex:1;min-width:0;border:0;outline:0;background:transparent;color:var(--text);font-weight:650}.field select option,.input-shell select option{background:var(--card-bg);color:var(--text)}.speaker-picker{position:relative;width:100%}.speaker-picker>summary{box-sizing:border-box;display:flex;align-items:center;cursor:pointer;list-style:none;user-select:none}.speaker-picker>summary::-webkit-details-marker{display:none}.speaker-summary{min-width:0;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text);font-size:12px;font-weight:700}.speaker-count{min-width:22px;height:22px;padding:0 6px;border-radius:999px;display:grid;place-items:center;background:color-mix(in srgb,var(--accent) 14%,var(--card-bg));color:color-mix(in srgb,var(--accent) 78%,var(--text));font-size:9px;font-weight:850}.speaker-picker[open] .speaker-field{border-color:color-mix(in srgb,var(--accent) 42%,var(--border));box-shadow:0 0 0 2px color-mix(in srgb,var(--accent) 9%,transparent)}.speaker-picker[open] .speaker-field>ha-icon:last-child{transform:rotate(180deg)}.speaker-field>ha-icon:last-child{transition:transform .18s ease}.speaker-menu{position:absolute;left:0;right:0;top:calc(100% + 7px);z-index:30;padding:10px;border:1px solid var(--border);border-radius:15px;background:var(--card-bg);box-shadow:0 16px 38px rgba(0,0,0,.24)}.speaker-menu-head{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:2px 3px 8px}.speaker-menu-head strong{font-size:10px}.speaker-menu-head>div{display:flex;gap:5px}.speaker-menu-head button{height:27px;padding:0 8px;border:1px solid var(--border-soft);border-radius:8px;background:var(--surface-soft);color:var(--muted);font-size:9px;font-weight:750;cursor:pointer}.speaker-menu-head button:hover{color:var(--text);background:var(--surface)}.speaker-options{max-height:230px;overflow:auto;overscroll-behavior:contain}.speaker-option{position:relative;display:flex;align-items:center;gap:9px;min-height:39px;padding:5px 7px;border-radius:10px;cursor:pointer}.speaker-option:hover{background:var(--surface-soft)}.speaker-option input{position:absolute;opacity:0;pointer-events:none}.speaker-check{width:23px;height:23px;flex:0 0 23px;border:1px solid var(--border);border-radius:7px;display:grid;place-items:center;background:var(--surface-soft);color:transparent}.speaker-check ha-icon{--mdc-icon-size:15px}.speaker-option input:checked+.speaker-check{border-color:color-mix(in srgb,var(--accent) 55%,transparent);background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff}.speaker-name{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px;font-weight:650;color:var(--text)}
      .now-playing{gap:20px;padding:24px 2px 18px}.art-wrap{position:relative;width:118px;height:118px;flex:0 0 118px;border-radius:28px;padding:4px;background:linear-gradient(135deg,color-mix(in srgb,var(--accent) 78%,#fff),var(--accent2));box-shadow:0 14px 34px rgba(0,0,0,.2)}.art-wrap.pulse:after{content:"";position:absolute;inset:-7px;border-radius:34px;border:1px solid color-mix(in srgb,var(--accent2) 55%,transparent);animation:ring 2.3s ease-out infinite}.art{width:100%;height:100%;display:grid;place-items:center;border-radius:24px;background:var(--surface-strong);background-size:cover;background-position:center;overflow:hidden}.art>ha-icon{--mdc-icon-size:48px;color:var(--muted)}.track-info{min-width:0;flex:1}.track-title{font-size:24px;font-weight:850;line-height:1.12;letter-spacing:-.035em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.track-subtitle{margin-top:8px;color:var(--muted);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
      .transport{padding:4px 2px 8px}.progress-head{display:flex;justify-content:space-between;font-size:10px;color:var(--muted);font-variant-numeric:tabular-nums;margin-bottom:5px}.range{appearance:none;width:100%;height:4px;border-radius:999px;outline:none;background:linear-gradient(90deg,var(--accent) 0 var(--value,0%),var(--surface-strong) var(--value,0%) 100%)}.range::-webkit-slider-thumb{appearance:none;width:14px;height:14px;border-radius:50%;background:var(--text);box-shadow:0 2px 8px rgba(0,0,0,.25);cursor:pointer}.range::-moz-range-thumb{width:14px;height:14px;border:0;border-radius:50%;background:var(--text);cursor:pointer}.progress{height:5px}.controls{justify-content:center;gap:13px;margin:18px 0 16px}.icon-btn,.play-btn,.soft-btn{border:0;cursor:pointer;display:grid;place-items:center;transition:transform .18s ease,background .18s ease,opacity .18s ease}.icon-btn{width:43px;height:43px;border-radius:50%;background:var(--surface)}.icon-btn:hover,.soft-btn:hover{background:var(--surface-strong);transform:translateY(-1px)}.play-btn{width:62px;height:62px;border-radius:50%;background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;box-shadow:0 10px 28px color-mix(in srgb,var(--accent) 34%,transparent)}.play-btn:hover{transform:scale(1.045)}.play-btn ha-icon{--mdc-icon-size:31px}.busy ha-icon,.spinning ha-icon{animation:spin 1s linear infinite}.volume-row{gap:10px;color:var(--muted)}.volume-row ha-icon{--mdc-icon-size:18px}.volume-row .range{flex:1}.volume-row span{width:36px;text-align:right;font-size:10px;font-variant-numeric:tabular-nums}
      .tabs{gap:7px;margin:16px 0 12px;padding:5px;border-radius:15px;background:var(--surface-soft);border:1px solid var(--border-soft)}.tabs button{position:relative;flex:1;height:38px;border:0;border-radius:11px;background:transparent;color:var(--muted);font-size:12px;font-weight:750;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:7px;min-width:0}.tabs button.active{background:var(--surface-strong);color:var(--text);box-shadow:0 4px 12px rgba(0,0,0,.07)}.tabs ha-icon{--mdc-icon-size:18px}.tabs b{font-size:9px;padding:2px 6px;border-radius:9px;background:var(--surface-strong)}.tab-pulse{width:6px;height:6px;border-radius:50%;background:#34c88a;box-shadow:0 0 10px rgba(52,200,138,.65);animation:pulseDot 1.3s ease-in-out infinite}
      .panel{display:none}.panel.active{display:block}.url-box{gap:9px;padding:8px 8px 8px 13px;border-radius:16px;background:var(--surface-soft);border:1px solid var(--border)}.url-box>ha-icon,.search-box>ha-icon{color:var(--muted);--mdc-icon-size:20px}.url-box input,.search-box input{min-width:0;flex:1;border:0;outline:0;color:var(--text);background:transparent}.url-box input::placeholder,.search-box input::placeholder{color:color-mix(in srgb,var(--muted) 75%,transparent)}.accent-btn{height:38px;border:0;border-radius:11px;padding:0 14px;display:flex;align-items:center;gap:6px;background:linear-gradient(135deg,var(--accent),color-mix(in srgb,var(--accent2) 72%,var(--accent)));color:#fff;font-size:12px;font-weight:800;cursor:pointer}.accent-btn:disabled{opacity:.45;cursor:default}.accent-btn ha-icon{--mdc-icon-size:17px}
      .library-list{overflow:auto;margin:0 -4px;padding:2px 4px 4px;scrollbar-width:thin}.library-list.scroll-five{max-height:260px}
      .library-tools{gap:8px}.search-box{flex:1;height:42px;gap:8px;padding:0 12px;border-radius:13px;background:var(--surface-soft);border:1px solid var(--border)}.soft-btn{width:42px;height:42px;border-radius:13px;background:var(--surface)}.library-meta{justify-content:space-between;gap:8px;padding:9px 2px 7px;color:var(--muted);font-size:9px}.library-meta span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.track-row{width:100%;min-height:52px;gap:10px;text-align:left;border:0;border-radius:13px;padding:9px 10px;background:transparent;cursor:pointer}.track-row:hover,.track-row.selected{background:var(--surface)}.track-icon{width:34px;height:34px;flex:0 0 34px;border-radius:10px;display:grid;place-items:center;background:var(--surface);color:var(--muted)}.track-row.selected .track-icon{background:color-mix(in srgb,var(--accent) 18%,var(--card-bg));color:color-mix(in srgb,var(--accent) 80%,var(--text))}.track-icon ha-icon{--mdc-icon-size:18px}.track-text{flex:1;min-width:0;display:flex;flex-direction:column;gap:3px}.track-text strong,.track-text small{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.track-text strong{font-size:12px;font-weight:750}.track-text small{font-size:9px;color:var(--muted)}.track-size{font-size:9px;color:var(--muted);white-space:nowrap}.row-play{opacity:.65;color:var(--text);transition:opacity .15s}.track-row:hover .row-play,.track-row.selected .row-play{opacity:1}.row-play ha-icon{--mdc-icon-size:18px}.empty{height:120px;display:flex;align-items:center;justify-content:center;gap:8px;color:var(--muted);font-size:12px}.empty.compact{height:86px}.empty ha-icon{--mdc-icon-size:20px}
      .pagination{justify-content:center;gap:5px;margin-top:8px;min-height:30px}.pagination button{min-width:29px;height:29px;padding:0 8px;border:1px solid var(--border-soft);border-radius:9px;background:var(--surface-soft);color:var(--muted);cursor:pointer;font-size:10px;font-weight:750}.pagination button:hover{color:var(--text);background:var(--surface)}.pagination button.active{border-color:color-mix(in srgb,var(--accent) 42%,transparent);background:color-mix(in srgb,var(--accent) 14%,var(--card-bg));color:color-mix(in srgb,var(--accent) 76%,var(--text))}.page-gap{color:var(--muted);font-size:10px}
      .download-hero{gap:11px;margin-bottom:12px;padding:12px 13px;border-radius:16px;background:color-mix(in srgb,var(--accent) 8%,var(--card-bg));border:1px solid color-mix(in srgb,var(--accent) 18%,var(--border))}.download-hero-icon{width:39px;height:39px;display:grid;place-items:center;border-radius:12px;background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff}.download-hero-icon ha-icon{--mdc-icon-size:22px}.download-hero>div:last-child{display:flex;flex-direction:column;gap:3px}.download-hero strong{font-size:13px}.download-hero span{font-size:9px;color:var(--muted)}
      .input-shell{min-width:0;display:flex;flex-direction:column;gap:7px;padding:10px 12px;border-radius:14px;background:var(--surface-soft);border:1px solid var(--border)}.input-shell.full{margin-bottom:9px}.input-shell>span{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:9px;font-weight:700}.input-shell>span ha-icon{--mdc-icon-size:15px}.input-shell input,.input-shell select{width:100%;min-width:0;border:0;outline:0;background:transparent;color:var(--text);font-size:12px;font-weight:650}.input-shell input::placeholder{color:color-mix(in srgb,var(--muted) 70%,transparent)}.input-shell select{appearance:none}.option-grid{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-top:9px}.download-type{display:grid;grid-template-columns:1fr 1fr;gap:6px;padding:5px;border-radius:14px;background:var(--surface-soft);border:1px solid var(--border-soft)}.download-type button{height:38px;border:0;border-radius:10px;background:transparent;color:var(--muted);cursor:pointer;display:flex;align-items:center;justify-content:center;gap:7px;font-size:11px;font-weight:750}.download-type button.active{color:#fff;background:linear-gradient(135deg,var(--accent),color-mix(in srgb,var(--accent2) 68%,var(--accent)))}.download-type ha-icon{--mdc-icon-size:17px}
      .toggle-list{margin-top:9px;border-radius:15px;overflow:hidden;border:1px solid var(--border-soft);background:var(--surface-soft)}.toggle-row{position:relative;display:flex;align-items:center;justify-content:space-between;gap:14px;padding:11px 12px;cursor:pointer}.toggle-row+.toggle-row{border-top:1px solid var(--border-soft)}.toggle-row>span{display:flex;flex-direction:column;gap:2px}.toggle-row strong{font-size:10px}.toggle-row small{font-size:8px;color:var(--muted)}.toggle-row input{position:absolute;opacity:0;pointer-events:none}.toggle-row i{position:relative;width:36px;height:20px;flex:0 0 36px;border-radius:999px;background:var(--surface-strong);transition:.2s}.toggle-row i:after{content:"";position:absolute;top:3px;left:3px;width:14px;height:14px;border-radius:50%;background:var(--card-bg);box-shadow:0 2px 7px rgba(0,0,0,.22);transition:.2s}.toggle-row input:checked+i{background:linear-gradient(135deg,var(--accent),var(--accent2))}.toggle-row input:checked+i:after{transform:translateX(16px)}
      .job-card{margin-top:10px;padding:13px;border-radius:16px;background:var(--surface-soft);border:1px solid var(--border)}.job-card.done{border-color:rgba(40,183,124,.30);background:color-mix(in srgb,#28b77c 8%,var(--card-bg))}.job-card.failed{border-color:rgba(214,62,80,.30);background:color-mix(in srgb,#d63e50 8%,var(--card-bg))}.job-top{gap:10px}.job-status-icon{width:34px;height:34px;flex:0 0 34px;border-radius:11px;display:grid;place-items:center;background:var(--surface);color:var(--muted)}.job-status-icon.working{color:#159b63;background:color-mix(in srgb,#28b77c 10%,var(--card-bg))}.job-status-icon.working ha-icon{animation:downloadFloat 1.25s ease-in-out infinite}.job-title{min-width:0;flex:1;display:flex;flex-direction:column;gap:3px}.job-title strong{font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.job-title span{font-size:9px;color:var(--muted)}.job-top>b{font-size:11px;font-variant-numeric:tabular-nums}.download-progress{height:6px;margin-top:11px;overflow:hidden;border-radius:999px;background:var(--surface-strong)}.download-progress i{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,var(--accent),var(--accent2));box-shadow:0 0 14px color-mix(in srgb,var(--accent2) 35%,transparent);transition:width .3s ease}.job-metrics{gap:12px;flex-wrap:wrap;margin-top:9px;color:var(--muted);font-size:8px}.job-metrics>span{display:flex;align-items:center;gap:4px}.job-metrics ha-icon{--mdc-icon-size:13px}.job-metrics .metric-hidden{display:none}.result-file{gap:6px;margin-top:9px;padding-top:9px;border-top:1px solid var(--border-soft);font-size:9px;color:var(--muted);min-width:0}.result-file ha-icon{--mdc-icon-size:15px;color:#159b63}.result-file span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.job-error{display:flex;align-items:flex-start;gap:6px;margin-top:9px;padding:8px;border-radius:10px;background:color-mix(in srgb,#d63e50 9%,var(--card-bg));color:#d63e50;font-size:9px;line-height:1.35}.job-error ha-icon{--mdc-icon-size:15px;flex:0 0 auto}
      .download-btn{width:100%;height:45px;margin-top:11px;border:0;border-radius:14px;display:flex;align-items:center;justify-content:center;gap:8px;background:linear-gradient(135deg,var(--accent),color-mix(in srgb,var(--accent2) 72%,var(--accent)));color:#fff;font-size:12px;font-weight:850;cursor:pointer;box-shadow:0 9px 24px color-mix(in srgb,var(--accent) 22%,transparent);transition:transform .18s ease,opacity .18s ease}.download-btn:hover:not(:disabled){transform:translateY(-1px)}.download-btn:disabled{opacity:.45;cursor:default;box-shadow:none}.download-btn ha-icon{--mdc-icon-size:19px}
      @keyframes spin{to{transform:rotate(360deg)}}@keyframes ring{0%{opacity:.7;transform:scale(.97)}100%{opacity:0;transform:scale(1.1)}}@keyframes floatbg{from{transform:scale(1.1) translate3d(-1%,0,0)}to{transform:scale(1.18) translate3d(1%,1%,0)}}@keyframes noticeIn{from{opacity:0;transform:translateY(-5px)}to{opacity:1;transform:none}}@keyframes pulseDot{50%{opacity:.35;transform:scale(.75)}}@keyframes downloadFloat{50%{transform:translateY(2px)}}
      @media(max-width:520px){.surface{padding:18px}.status{display:none}.primary-tabs button{height:45px;font-size:11px;gap:7px}.primary-tabs ha-icon{--mdc-icon-size:20px}.now-playing{gap:16px}.art-wrap{width:94px;height:94px;flex-basis:94px;border-radius:23px}.art{border-radius:19px}.track-title{font-size:20px}.controls{gap:9px}.icon-btn{width:40px;height:40px}.play-btn{width:58px;height:58px}.library-list.scroll-five{max-height:250px}.track-size{display:none}.tabs button{font-size:11px;gap:5px}.tabs button span{max-width:62px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.option-grid{grid-template-columns:1fr}.download-hero span{display:none}}
      @media(max-width:380px){.surface{padding:14px}.primary-tabs button span{font-size:10px}.primary-tabs{padding:5px;gap:6px}.tabs b{display:none}.tabs button span{display:none}.tabs button{height:40px}.tabs ha-icon{--mdc-icon-size:20px}.speaker-menu-head{align-items:flex-start;flex-direction:column}.speaker-menu-head>div{width:100%}.speaker-menu-head button{flex:1}.now-playing{gap:12px}.art-wrap{width:78px;height:78px;flex-basis:78px}.track-title{font-size:17px}.url-box{padding-left:10px}.accent-btn{padding:0 10px}}
      @media(max-width:320px){.surface{padding:12px}.brand-icon{width:38px;height:38px;border-radius:12px}.brand-title{font-size:16px}.primary-tabs{margin-top:14px}.primary-tabs button{height:42px}.speaker-row{margin-top:14px}.field{padding:0 10px}.speaker-count{display:none}.controls{gap:6px}.icon-btn{width:36px;height:36px}.play-btn{width:52px;height:52px}.url-box>ha-icon{display:none}}
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
    description: "Phát YouTube, điều khiển loa, duyệt thư viện và tải video/audio trực tiếp từ dashboard.",
    preview: true,
  });
}
