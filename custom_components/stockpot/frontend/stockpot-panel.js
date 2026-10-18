/*
 * <stockpot-panel>: the Stockpot panel in the Home Assistant sidebar (integration 0.12.0,
 * Stockpot M18, ADR 0021). No build step, no dependency.
 *
 * It holds an iframe of `<Stockpot URL>/embed`. The embedded Stockpot asks for a single-use code
 * (`stockpot:ready`, then `stockpot:expired` when its token must be renewed); the panel gets one
 * with the websocket command `stockpot/panel/code` (the integration calls Stockpot with its OAuth
 * access) and posts it to the iframe for Stockpot's origin only. Messages are accepted only from
 * that iframe and that origin. `stockpot:toggle-menu` opens Home Assistant's sidebar on narrow
 * layouts; `narrow` is passed on to Stockpot.
 */

const MESSAGES = {
  ready: "stockpot:ready",
  expired: "stockpot:expired",
  toggleMenu: "stockpot:toggle-menu",
  code: "stockpot:code",
  error: "stockpot:error",
  narrow: "stockpot:narrow",
};

// Errors after which a new code will not come: the panel shows why instead of Stockpot.
const DEFINITIVE = ["panel_disabled", "panel_requires_oauth", "reauth_required", "unauthorized", "two_factor_required", "email_unverified"];

const TEXTS = {
  fr: {
    disabledTitle: "Panneau Stockpot désactivé",
    disabledText:
      "Le panneau est désactivé pour ce Home Assistant. Active « Afficher Stockpot dans Home Assistant » dans Stockpot › Réglages › Home Assistant.",
    oauthTitle: "Connexion Stockpot à refaire",
    oauthText:
      "Le panneau demande une connexion par « Se connecter avec Stockpot ». Reconfigure l’intégration Stockpot dans Home Assistant.",
    errorTitle: "Stockpot indisponible",
    errorText: "Impossible d’ouvrir la session Stockpot pour le moment. Vérifie l’intégration Stockpot, puis recharge la page.",
    settings: "Ouvrir les réglages de Stockpot",
    frame: "Stockpot",
  },
  en: {
    disabledTitle: "Stockpot panel turned off",
    disabledText:
      "The panel is turned off for this Home Assistant. Turn on « Show Stockpot in Home Assistant » in Stockpot › Settings › Home Assistant.",
    oauthTitle: "Sign in with Stockpot again",
    oauthText: "The panel needs the « Sign in with Stockpot » connection. Reconfigure the Stockpot integration in Home Assistant.",
    errorTitle: "Stockpot unavailable",
    errorText: "The Stockpot session cannot be opened right now. Check the Stockpot integration, then reload the page.",
    settings: "Open Stockpot settings",
    frame: "Stockpot",
  },
};

class StockpotPanel extends HTMLElement {
  constructor() {
    super();
    this._hass = null;
    this._narrow = false;
    this._panel = null;
    this._iframe = null;
    this._requesting = false;
    this._onMessage = this._onMessage.bind(this);
    this.attachShadow({ mode: "open" });
  }

  set hass(hass) {
    this._hass = hass;
  }

  get hass() {
    return this._hass;
  }

  set narrow(narrow) {
    this._narrow = Boolean(narrow);
    this._post({ type: MESSAGES.narrow, narrow: this._narrow });
  }

  get narrow() {
    return this._narrow;
  }

  set panel(panel) {
    this._panel = panel;
    this._render();
  }

  get panel() {
    return this._panel;
  }

  // Set by the frontend, unused: the embedded Stockpot has its own router.
  set route(_route) {}

  connectedCallback() {
    window.addEventListener("message", this._onMessage);
    this._render();
  }

  disconnectedCallback() {
    window.removeEventListener("message", this._onMessage);
  }

  get _config() {
    return (this._panel && this._panel.config) || {};
  }

  get _origin() {
    try {
      return new URL(this._config.url).origin;
    } catch (_err) {
      return null;
    }
  }

  get _texts() {
    const language = (this._hass && this._hass.language) || "en";
    return language.startsWith("fr") ? TEXTS.fr : TEXTS.en;
  }

  _render() {
    const origin = this._origin;
    if (!this.isConnected || !origin || this._iframe) return;
    const style = document.createElement("style");
    style.textContent = `
      :host { display: block; height: 100%; background: var(--primary-background-color); }
      iframe { display: block; width: 100%; height: 100%; border: 0; }
      .notice { box-sizing: border-box; max-width: 560px; margin: 48px auto; padding: 24px; font-family: var(--paper-font-body1_-_font-family, sans-serif); color: var(--primary-text-color); }
      .notice h1 { font-size: 1.4rem; margin: 0 0 12px; }
      .notice p { line-height: 1.5; margin: 0 0 16px; }
      .notice a { display: inline-flex; align-items: center; min-height: 48px; color: var(--primary-color); }
    `;
    const iframe = document.createElement("iframe");
    iframe.src = `${origin}${new URL(this._config.url).pathname.replace(/\/$/, "")}/embed`;
    iframe.setAttribute("allow", "camera; fullscreen");
    iframe.setAttribute("title", this._texts.frame);
    this._iframe = iframe;
    this.shadowRoot.replaceChildren(style, iframe);
  }

  /** Posts to the embedded Stockpot, for its origin only. */
  _post(message) {
    const origin = this._origin;
    if (this._iframe && this._iframe.contentWindow && origin) {
      this._iframe.contentWindow.postMessage(message, origin);
    }
  }

  _onMessage(event) {
    // Only the embedded Stockpot, from Stockpot's origin.
    if (!this._iframe || event.source !== this._iframe.contentWindow || event.origin !== this._origin) return;
    const data = event.data;
    if (!data || typeof data !== "object") return;
    if (data.type === MESSAGES.ready || data.type === MESSAGES.expired) {
      if (data.type === MESSAGES.ready) this._post({ type: MESSAGES.narrow, narrow: this._narrow });
      this._sendCode(data.type === MESSAGES.ready);
    } else if (data.type === MESSAGES.toggleMenu) {
      this.dispatchEvent(new Event("hass-toggle-menu", { bubbles: true, composed: true }));
    }
  }

  /** A fresh code for the iframe. A failure on opening, or a definitive one, replaces it with a message. */
  async _sendCode(opening) {
    if (this._requesting || !this._hass) return;
    this._requesting = true;
    try {
      const result = await this._hass.callWS({ type: "stockpot/panel/code", entry_id: this._config.entry_id });
      this._post({ type: MESSAGES.code, code: result.code });
    } catch (err) {
      const code = (err && err.code) || "unknown";
      this._post({ type: MESSAGES.error, error: /^[a-z_]{1,64}$/.test(code) ? code : "unknown" });
      if (opening || DEFINITIVE.includes(code)) this._showNotice(code);
    } finally {
      this._requesting = false;
    }
  }

  _showNotice(code) {
    const texts = this._texts;
    const [title, text] =
      code === "panel_disabled"
        ? [texts.disabledTitle, texts.disabledText]
        : code === "panel_requires_oauth" || code === "reauth_required"
          ? [texts.oauthTitle, texts.oauthText]
          : [texts.errorTitle, texts.errorText];
    const notice = document.createElement("div");
    notice.className = "notice";
    notice.setAttribute("role", "alert");
    const heading = document.createElement("h1");
    heading.textContent = title;
    const paragraph = document.createElement("p");
    paragraph.textContent = text;
    notice.append(heading, paragraph);
    if (code === "panel_disabled" && this._config.url) {
      const link = document.createElement("a");
      link.href = `${this._config.url}/settings/home-assistant`;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = texts.settings;
      notice.append(link);
    }
    const style = this.shadowRoot.querySelector("style");
    this._iframe = null;
    this.shadowRoot.replaceChildren(...(style ? [style] : []), notice);
  }
}

if (!customElements.get("stockpot-panel")) {
  customElements.define("stockpot-panel", StockpotPanel);
}
