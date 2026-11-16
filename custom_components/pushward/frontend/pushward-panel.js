// The PushWard sidebar panel (admin only): every tracked activity, widget and
// to-do list with a live preview, an editor that drives Home Assistant's own
// subentry flows, test sends to the phone, and the pushward-web playground.
// Plain ES module without a build step. Text from entities, flows and the API
// reaches the page only through text nodes, never as HTML.

const DOMAIN = "pushward";
const TYPES = ["tracked_entity", "tracked_widget", "tracked_todo"];
const KINDS = { tracked_entity: "activity", tracked_widget: "widget", tracked_todo: "notification" };
const TABS = { tracked: "tab_tracked", playground: "tab_playground" };
const INTEGRATION_PAGE = "/config/integrations/integration/pushward";
const FLOW_API = "config/config_entries/subentries/flow";
const FLOW_HEADERS = { "HA-Frontend-Base": `${location.protocol}//${location.host}` };
const REFRESH_MS = 1000;
const DRAFT_MS = 300;
const FORM_WAIT_MS = 5000;
const LAST_SENT_KEY = "pushward-panel:last-sent";

// No version query: the embed's chunks import the entry by its bare URL, and a second URL would load it twice.
const EMBED_URL = new URL("./embed/pushward-embed.js", import.meta.url).href;
const NOT_ALL_REQUIRED = "ui.panel.config.integrations.config_flow.not_all_required_fields";

// Home Assistant has no translation category for panels. The panel's own strings
// live under selector.panel.options: hassfest accepts that shape, and
// loadBackendTranslation("selector") loads it with the flows' option labels.
const panelKey = (name) => `component.${DOMAIN}.selector.panel.options.${name}`;
const subentryKey = (type, path) => `component.${DOMAIN}.config_subentries.${type}.${path}`;

const isEmpty = (value) => value === undefined || value === null || value === false;

/** Replace el's children; strings become text nodes, empty values are skipped. */
function fill(el, ...children) {
  el.replaceChildren(...children.flat().filter((child) => !isEmpty(child)));
  return el;
}

/** A new element: "@name" adds a listener, ".name" sets a property, anything else is an attribute. */
function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (isEmpty(value)) continue;
    if (key[0] === "@") el.addEventListener(key.slice(1), value);
    else if (key[0] === ".") el[key.slice(1)] = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  return fill(el, ...children);
}

/** fn, run once `ms` after the last call of a burst. */
function debounce(fn, ms) {
  let timer;
  const run = () => {
    clearTimeout(timer);
    timer = setTimeout(fn, ms);
  };
  run.cancel = () => clearTimeout(timer);
  return run;
}

/** The test items still on a phone, "entry_id:kind" -> subentry_id, as stored across visits; unreadable counts as none. */
function loadLastSent() {
  try {
    const stored = JSON.parse(localStorage.getItem(LAST_SENT_KEY));
    if (stored && typeof stored === "object" && !Array.isArray(stored)) {
      return Object.fromEntries(Object.entries(stored).filter(([, id]) => typeof id === "string"));
    }
  } catch (_err) {
    // Corrupt or blocked storage: start empty.
  }
  return {};
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const whenDefined = (tag, ms) =>
  Promise.race([customElements.whenDefined(tag).then(() => true), sleep(ms).then(() => false)]);

const errorText = (err) =>
  err?.body?.message || err?.message || err?.error || (typeof err === "string" ? err : "") || String(err?.code ?? "");

// What a required field holds before the user touches it, as HA's
// computeInitialHaFormData has it for the selectors these flows use.
const SELECTOR_INITIAL = {
  attribute: () => "",
  boolean: () => false,
  color_rgb: () => [0, 0, 0],
  entity: (cfg) => (cfg?.multiple ? [] : ""),
  icon: () => "",
  number: (cfg) => cfg?.min ?? 0,
  object: (cfg) => (cfg?.multiple ? [] : ""),
  select: (cfg) => {
    const first = cfg?.options?.[0];
    if (first === undefined) return undefined;
    const value = typeof first === "string" ? first : first.value;
    return cfg.multiple ? [value] : value;
  },
  text: (cfg) => (cfg?.multiple ? [] : ""),
  time: () => "00:00:00",
};
const TYPE_INITIAL = { boolean: false, string: "", float: 0 };

/** A form step's starting data: suggested values, defaults, then empty values of required fields. */
function initialData(schema) {
  const data = {};
  for (const field of schema) {
    const suggested = field.description?.suggested_value;
    if (suggested !== undefined && suggested !== null) data[field.name] = suggested;
    else if ("default" in field) data[field.name] = field.default;
    else if (field.type === "expandable") {
      const inner = initialData(field.schema);
      if (field.required || Object.keys(inner).length) data[field.name] = inner;
    } else if (!field.required) continue;
    else if (field.type === "integer") data[field.name] = field.valueMin ?? 0;
    else if (field.type in TYPE_INITIAL) data[field.name] = TYPE_INITIAL[field.type];
    else if (field.selector) {
      const [type, cfg] = Object.entries(field.selector)[0] ?? [];
      const value = SELECTOR_INITIAL[type]?.(cfg);
      if (value !== undefined) data[field.name] = value;
    }
  }
  return data;
}

/** What HA's form submits: top-level empty values and read-only fields are left out. */
function submitData(schema, data) {
  const out = {};
  for (const [key, value] of Object.entries(data)) {
    if (value === undefined || value === "") continue;
    const selector = schema.find((field) => field.name === key)?.selector;
    if (selector && Object.values(selector)[0]?.read_only) continue;
    out[key] = value;
  }
  return out;
}

/** Whether every required field holds a value, checked the way HA's form does before it submits. */
function requiredFilled(schema, data = {}) {
  return schema.every(
    (field) =>
      (!field.required || !["", undefined].includes(data[field.name])) &&
      (field.type !== "expandable" ||
        (!field.required && data[field.name] === undefined) ||
        requiredFilled(field.schema, data[field.name]))
  );
}

/** ha-form exists only once a Home Assistant page needed it; the config panel's automation editor loads it. */
async function loadHaForm() {
  if (customElements.get("ha-form")) return true;
  try {
    if (await whenDefined("partial-panel-resolver", 1000)) {
      const resolver = document.createElement("partial-panel-resolver");
      resolver.hass = { panels: [{ url_path: "tmp", component_name: "config" }] };
      resolver._updateRoutes();
      await resolver.routerOptions.routes.tmp.load();
    }
  } catch (_err) {
    // The wait below decides.
  }
  try {
    if (await whenDefined("ha-panel-config", 1000)) {
      await document.createElement("ha-panel-config").routerOptions.routes.automation.load();
    }
  } catch (_err) {
    // The wait below decides.
  }
  return whenDefined("ha-form", FORM_WAIT_MS);
}

/** In-app navigation the way HA's navigate() does it; modified clicks keep the browser's behaviour. */
function navigate(ev) {
  if (ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
  ev.preventDefault();
  history.pushState(null, "", ev.currentTarget.getAttribute("href"));
  window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
}

const STYLE = `
:host {
  display: block;
  min-height: 100vh;
  background: var(--primary-background-color);
  color: var(--primary-text-color);
  font-family: var(--ha-font-family-body, Roboto, sans-serif);
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  height: var(--header-height, 56px);
  padding: 0 16px 0 4px;
  background: var(--app-header-background-color, var(--primary-color));
  color: var(--app-header-text-color, var(--text-primary-color, #fff));
  border-bottom: var(--app-header-border-bottom, none);
}
.toolbar .title { font-size: 20px; margin-left: 12px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tabs { display: flex; gap: 4px; margin-left: auto; flex-shrink: 0; }
.tab {
  color: inherit;
  border: none;
  border-radius: 0;
  border-bottom: 2px solid transparent;
  padding: 16px 12px 14px;
}
.tab[aria-selected="true"] { border-bottom-color: currentColor; }
main { padding: 16px; max-width: 1400px; margin: 0 auto; box-sizing: border-box; }
h2 { font-size: 20px; font-weight: 500; margin: 0; }
h3 { font-size: 16px; font-weight: 500; margin: 20px 0 12px; color: var(--secondary-text-color); }
h4 { font-size: 16px; font-weight: 500; margin: 0; overflow-wrap: anywhere; }
.account { margin-bottom: 32px; }
.account-head { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; justify-content: space-between; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(300px, 100%), 1fr)); gap: 16px; }
.card {
  background: var(--card-background-color, var(--ha-card-background));
  border: 1px solid var(--divider-color);
  border-radius: var(--ha-card-border-radius, 12px);
  box-shadow: var(--ha-card-box-shadow, none);
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
}
.actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
button {
  font: inherit;
  font-size: 14px;
  background: none;
  color: var(--primary-color);
  border: 1px solid var(--divider-color);
  border-radius: 18px;
  padding: 6px 14px;
  cursor: pointer;
}
button.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: transparent; }
button.danger { color: var(--error-color); }
button:disabled { opacity: 0.5; cursor: default; }
button:focus-visible, a:focus-visible, select:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
a { color: var(--primary-color); }
p { margin: 0; }
.note { color: var(--secondary-text-color); }
.error { color: var(--error-color); }
.notice { padding: 12px 16px; margin-bottom: 16px; border-radius: 8px; background: var(--secondary-background-color); }
.empty { display: flex; flex-direction: column; gap: 8px; padding: 16px 0; }
.hint { color: var(--secondary-text-color); font-size: 14px; }
.hint ul { margin: 4px 0 0; padding-left: 20px; }
pushward-preview, pushward-playground { display: block; width: 100%; }
.editor { display: grid; grid-template-columns: minmax(0, 1fr) minmax(280px, 420px); gap: 24px; align-items: start; }
.preview-pane { position: sticky; top: 16px; display: flex; flex-direction: column; gap: 12px; }
.preview-pane h3 { margin: 0; }
.picker { display: flex; align-items: center; gap: 8px; margin-bottom: 16px; }
select {
  font: inherit;
  padding: 6px 8px;
  color: var(--primary-text-color);
  background: var(--card-background-color);
  border: 1px solid var(--divider-color);
  border-radius: 6px;
}
@media (max-width: 870px) {
  main { padding: 12px; }
  .toolbar .title { margin-left: 4px; }
  .tab { padding-left: 8px; padding-right: 8px; }
  .editor { grid-template-columns: minmax(0, 1fr); }
  .preview-pane { position: static; order: -1; }
}
`;

class PushWardPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._tab = "tracked";
    this._entries = []; // the loaded accounts
    this._unloaded = [];
    this._subentries = {}; // entry_id -> its subentries
    this._cards = new Map(); // subentry_id -> card state
    this._lastSent = loadLastSent(); // "entry_id:kind" -> the card whose test item is on that account's phone
    this._localizers = [];
    this._loadSeq = 0;
    this._scheduleLoad = debounce(() => this._load(), 500);
    // Page-wide in the embed; reads the account chosen when a request goes out.
    this._transport = (req) => this._api(req);
  }

  set hass(hass) {
    const old = this._hass;
    this._hass = hass;
    if (this._root) this._hassChanged(old, hass);
    else if (this.isConnected) this._start();
  }

  get hass() {
    return this._hass;
  }

  set narrow(narrow) {
    this._narrow = narrow;
    if (this._menu) this._menu.narrow = narrow;
  }

  get narrow() {
    return this._narrow;
  }

  connectedCallback() {
    if (!this._hass) return;
    if (!this._root) {
      this._start();
      return;
    }
    this._subscribe();
    this._load();
  }

  disconnectedCallback() {
    this._unsubscribe?.then((unsub) => unsub?.()).catch(() => {});
    this._unsubscribe = null;
    // Nothing fires from a panel that is gone: the list reload, the draft preview (closing cancels it), card refreshes.
    this._scheduleLoad.cancel();
    this._closeEditor();
    for (const card of this._cards.values()) {
      clearTimeout(card.timer);
      card.timer = null;
    }
  }

  _start() {
    const hass = this._hass;
    this._menu = customElements.get("ha-menu-button")
      ? h("ha-menu-button", { ".hass": hass, ".narrow": this._narrow })
      : null;
    this._tabs = h("div", { class: "tabs", role: "tablist" });
    this._main = h("main");
    this._root = h("div", {}, h("div", { class: "toolbar" }, this._menu, h("div", { class: "title" }, "PushWard"), this._tabs), this._main);
    this.shadowRoot.append(h("style", {}, STYLE), this._root);

    // Revalidated first: the entry keeps its name across builds, so a cached copy could predate an upgrade.
    fetch(EMBED_URL, { cache: "no-cache" })
      .catch(() => {})
      .then(() => import(EMBED_URL))
      .then(
        () => true,
        (err) => {
          console.warn("PushWard: the preview components did not load", err);
          return false;
        }
      )
      .then((ok) => {
        this._embedOk = ok;
        this._render();
      });
    this._loadTranslations();
    this._subscribe();
    this._render();
    this._load();
  }

  _loadTranslations() {
    const hass = this._hass;
    // The previous language's localizers answer until the new ones are in, so no raw key shows meanwhile.
    const loads = [
      hass.loadBackendTranslation("config_subentries", DOMAIN),
      hass.loadBackendTranslation("selector", DOMAIN),
      // Abort reasons shared across integrations.
      hass.loadBackendTranslation("config", "homeassistant"),
      // The config panel's strings, for the required-fields message.
      hass.loadFragmentTranslation?.("config"),
    ];
    this._translations = Promise.all(loads.map((load) => Promise.resolve(load).catch(() => undefined))).then((fns) => {
      if (hass.language !== this._hass.language) return;
      // hass.localize catches up a moment later; until then the loaders' own functions answer.
      this._localizers = fns.filter((fn) => typeof fn === "function");
      this._ready = true;
      this._render();
      // When the list arrived first, its cards are drawn only now and still need their previews.
      if (this._loaded && this._tab === "tracked" && !this._editor && this.isConnected) this._refreshAll();
    });
  }

  _hassChanged(old, hass) {
    if (this._menu) this._menu.hass = hass;
    if (this._editor?.form) this._editor.form.hass = hass;
    if (old.language !== hass.language) this._loadTranslations();
    if (old.localize !== hass.localize) this._render();
    if (old.language !== hass.language || old.themes?.darkMode !== hass.themes?.darkMode) this._applyLook();
    if (!this.isConnected || this._tab !== "tracked" || this._editor || old.states === hass.states) return;
    for (const card of this._cards.values()) {
      if (card.timer || !card.entityIds.some((id) => old.states[id] !== hass.states[id])) continue;
      // Changes within a second share one preview call, also for an entity that changes every moment.
      card.timer = setTimeout(() => this._refreshCard(card), REFRESH_MS);
    }
  }

  _subscribe() {
    if (this._unsubscribe) return;
    // Subentry edits, deletes and the reload each one triggers come in as config entry updates.
    this._unsubscribe = this._hass.connection
      .subscribeMessage(
        (changes) => {
          if (changes.some((change) => change.type && change.entry?.domain === DOMAIN)) this._scheduleLoad();
        },
        { type: "config_entries/subscribe" }
      )
      .catch(() => null);
  }

  _t(key, placeholders) {
    for (const localize of [this._hass.localize, ...this._localizers]) {
      const text = localize?.(key, placeholders);
      if (text) return text;
    }
    return "";
  }

  _p(name, placeholders) {
    return this._t(panelKey(name), placeholders) || name;
  }

  _look(el) {
    el.locale = this._hass.language;
    el.theme = this._hass.themes?.darkMode ? "dark" : "light";
    el.transport = this._transport;
    return el;
  }

  _applyLook() {
    for (const card of this._cards.values()) if (card.previewEl) this._look(card.previewEl);
    if (this._editor?.previewEl) this._look(this._editor.previewEl);
    if (this._playground) this._look(this._playground);
  }

  _previewEl(holder, kind, payload) {
    holder.previewEl ??= document.createElement("pushward-preview");
    this._look(holder.previewEl);
    holder.previewEl.kind = kind;
    holder.previewEl.payload = payload;
    return holder.previewEl;
  }

  async _api(req) {
    const msg = { type: "pushward/api", entry_id: this._pgEntry, method: req.method, path: req.path };
    if (req.body !== undefined && req.body !== null) msg.body = req.body;
    if (req.contentType) msg.content_type = req.contentType;
    try {
      const { status, body } = await this._hass.callWS(msg);
      return { status, body };
    } catch (err) {
      throw { message: errorText(err) };
    }
  }

  async _load() {
    const seq = ++this._loadSeq;
    const hass = this._hass;
    try {
      const entries = await hass.callWS({ type: "config_entries/get", domain: DOMAIN });
      const loaded = entries.filter((entry) => entry.state === "loaded");
      const lists = await Promise.all(
        loaded.map((entry) => hass.callWS({ type: "config_entries/subentries/list", entry_id: entry.entry_id }))
      );
      // A reply that comes back after the panel left is dropped; coming back loads again.
      if (seq !== this._loadSeq || !this.isConnected) return;
      this._entries = loaded;
      this._unloaded = entries.filter((entry) => entry.state !== "loaded");
      this._subentries = Object.fromEntries(loaded.map((entry, i) => [entry.entry_id, lists[i]]));
      this._loadError = null;
      // A removed entry took its test items with it.
      const ids = new Set(entries.map((entry) => entry.entry_id));
      for (const key of Object.keys(this._lastSent)) if (!ids.has(key.split(":")[0])) this._setLastSent(key, null);
    } catch (err) {
      if (seq !== this._loadSeq || !this.isConnected) return;
      this._loadError = err;
    }
    this._loaded = true;
    const present = new Set(Object.values(this._subentries).flatMap((subs) => subs.map((sub) => sub.subentry_id)));
    for (const [id, card] of this._cards) {
      if (present.has(id)) continue;
      clearTimeout(card.timer);
      this._cards.delete(id);
    }
    if (!this._entries.some((entry) => entry.entry_id === this._pgEntry)) {
      this._pgEntry = this._entries[0]?.entry_id ?? null;
    }
    this._render();
    if (this._tab === "tracked" && !this._editor) this._refreshAll();
  }

  _setTab(tab) {
    if (this._tab === tab) return;
    this._tab = tab;
    this._notice = null;
    this._render();
    if (tab === "tracked" && !this._editor) this._refreshAll();
  }

  _render() {
    if (!this._root) return;
    // The tabs wait for the panel's strings, so a cold load never shows their keys.
    fill(
      this._tabs,
      this._ready &&
        Object.entries(TABS).map(([tab, label]) =>
          h(
            "button",
            { class: "tab", role: "tab", "aria-selected": String(this._tab === tab), "@click": () => this._setTab(tab) },
            this._p(label)
          )
        )
    );
    if (!this._ready || !this._loaded) {
      fill(this._main, h("p", { class: "note" }, this._t("ui.common.loading")));
      return;
    }
    let view;
    if (this._tab === "playground") view = this._renderPlayground();
    else if (this._editor) view = this._renderEditor();
    else view = this._renderTracked();
    fill(this._main, view);
    // Off its tab the playground stays on the page, hidden: removing it ends its component and the edits made in it.
    if (this._pgBox) {
      this._pgBox.hidden = !this._main.contains(this._pgBox);
      this._main.append(this._pgBox);
    }
  }

  _link() {
    return h("a", { href: INTEGRATION_PAGE, "@click": navigate }, this._p("open_integration"));
  }

  _empty(text) {
    return h("div", { class: "empty" }, h("p", { class: "note" }, text), this._link());
  }

  // --- Tracked tab ---

  _renderTracked() {
    const nodes = [];
    if (this._notice) nodes.push(h("div", { class: "notice", role: "status" }, this._notice));
    if (this._loadError) nodes.push(h("p", { class: "error" }, errorText(this._loadError)));
    if (!this._entries.length && !this._unloaded.length) nodes.push(this._empty(this._p("no_entries")));
    for (const entry of this._unloaded) nodes.push(h("p", { class: "note" }, this._p("not_loaded", { name: entry.title })));
    for (const entry of this._entries) nodes.push(this._renderAccount(entry));
    return nodes;
  }

  _renderAccount(entry) {
    const subs = this._subentries[entry.entry_id] || [];
    const adds = TYPES.map((type) =>
      h(
        "button",
        { "@click": () => this._openEditor(entry.entry_id, type) },
        this._t(subentryKey(type, "initiate_flow.user")) || type
      )
    );
    const section = h(
      "section",
      { class: "account" },
      h("div", { class: "account-head" }, h("h2", {}, entry.title), h("div", { class: "actions" }, adds))
    );
    if (!subs.length) section.append(this._empty(this._p("no_subentries")));
    for (const type of TYPES) {
      const items = subs.filter((sub) => sub.subentry_type === type);
      if (!items.length) continue;
      section.append(
        h("h3", {}, this._t(subentryKey(type, "entry_type")) || type),
        h("div", { class: "grid" }, items.map((sub) => this._renderCard(entry, sub)))
      );
    }
    return section;
  }

  _renderCard(entry, sub) {
    let card = this._cards.get(sub.subentry_id);
    if (!card) {
      card = { id: sub.subentry_id, seq: 0, timer: null, entityIds: [], result: null, error: null, status: null };
      this._cards.set(card.id, card);
    }
    card.entryId = entry.entry_id;
    card.sub = sub;
    const kind = KINDS[sub.subentry_type];
    // Built once per render: a refresh repaints only the preview, the status and the buttons' state, so focus stays.
    card.previewBox = h("div");
    card.statusEl = h("p", { role: "status" });
    card.buttons = {
      send: h("button", { "@click": () => this._send(card, "send") }, this._p("send")),
      end: h("button", { "@click": () => this._send(card, "end") }, this._p("end")),
      playground: h(
        "button",
        { "@click": () => this._openPlayground(card.entryId, kind, card.result.payload) },
        this._p("open_playground")
      ),
    };
    card.actions = h("div", { class: "actions" });
    card.el = h("article", { class: "card" }, h("h4", {}, sub.title), card.previewBox, card.statusEl, card.actions);
    this._paintActions(card);
    this._paintCard(card);
    return card.el;
  }

  _paintCard(card) {
    const { sub, result, error, status, buttons } = card;
    const kind = KINDS[sub.subentry_type];
    const payload = result?.available ? result.payload : null;
    let preview;
    if (error) preview = h("p", { class: "error" }, this._p("preview_failed", { error: errorText(error) }));
    else if (!result) preview = h("p", { class: "note" }, this._t("ui.common.loading"));
    else if (!payload) preview = h("p", { class: "note" }, this._p("unavailable"));
    else if (this._embedOk === false) preview = h("p", { class: "note" }, this._p("embed_unavailable"));
    else preview = this._previewEl(card, kind, payload);
    if (card.previewBox.firstChild !== preview) fill(card.previewBox, preview);

    card.statusEl.hidden = !status;
    if (status) {
      card.statusEl.className = status.ok ? "note" : "error";
      // Unchanged text stays as it is, or the live region would announce it again.
      if (card.statusEl.textContent !== status.text) card.statusEl.textContent = status.text;
    }
    buttons.send.disabled = !payload || Boolean(card.busy);
    buttons.end.disabled = Boolean(card.busy);
    buttons.end.hidden = this._lastSent[`${card.entryId}:${kind}`] !== card.id;
    buttons.playground.disabled = !payload;
  }

  /** The action row: the card's buttons, or the delete confirmation in their place. */
  _paintActions(card) {
    const { sub, buttons } = card;
    fill(
      card.actions,
      card.confirming
        ? [
            h("span", {}, this._p("delete_confirm", { name: sub.title })),
            h("button", { class: "danger", "@click": () => this._delete(card) }, this._t("ui.common.delete")),
            h("button", { "@click": () => this._paintConfirm(card, false) }, this._t("ui.common.cancel")),
          ]
        : [
            h(
              "button",
              { "@click": () => this._openEditor(card.entryId, sub.subentry_type, sub.subentry_id) },
              this._t("ui.common.edit")
            ),
            h("button", { class: "danger", "@click": () => this._paintConfirm(card, true) }, this._t("ui.common.delete")),
            buttons.send,
            buttons.end,
            buttons.playground,
          ]
    );
  }

  _paintConfirm(card, confirming) {
    card.confirming = confirming;
    this._paintActions(card);
  }

  _refreshAll() {
    for (const card of this._cards.values()) this._refreshCard(card);
  }

  async _refreshCard(card) {
    clearTimeout(card.timer);
    card.timer = null;
    const ticket = ++card.seq;
    let result = null;
    let error = null;
    try {
      result = await this._hass.callWS({ type: "pushward/preview", entry_id: card.entryId, subentry_id: card.id });
    } catch (err) {
      error = err;
    }
    // A slower, older reply never paints over a newer one, and one for a panel that left is dropped.
    if (ticket !== card.seq || !this.isConnected) return;
    card.result = result;
    card.error = error;
    if (result) card.entityIds = result.entity_ids || [];
    if (card.el) this._paintCard(card);
    // The manager has not pushed the newest frame yet (throttled or in flight): ask again shortly.
    if (result?.pending && !card.timer && this._tab === "tracked" && !this._editor && this.isConnected) {
      card.timer = setTimeout(() => this._refreshCard(card), REFRESH_MS);
    }
  }

  async _send(card, action) {
    const kind = KINDS[card.sub.subentry_type];
    card.busy = true;
    card.status = null;
    this._paintCard(card);
    try {
      const msg = { type: "pushward/send_test", entry_id: card.entryId, kind, action };
      if (action === "send") msg.payload = card.result.payload;
      await this._hass.callWS(msg);
      if (kind !== "notification") this._setLastSent(`${card.entryId}:${kind}`, action === "send" ? card.id : null);
      card.status = { ok: true, text: action === "send" ? this._p("sent") : this._p("ended") };
    } catch (err) {
      card.status = { ok: false, text: errorText(err) };
    }
    card.busy = false;
    // The End button moves to the card that sent last.
    for (const other of this._cards.values()) if (other.el) this._paintCard(other);
  }

  /** Remember (cardId) or forget (null) the test item on a phone, across visits and reloads. */
  _setLastSent(key, cardId) {
    if (cardId) this._lastSent[key] = cardId;
    else delete this._lastSent[key];
    try {
      localStorage.setItem(LAST_SENT_KEY, JSON.stringify(this._lastSent));
    } catch (_err) {
      // Full or blocked storage: End is then offered for this visit only.
    }
  }

  async _delete(card) {
    try {
      await this._hass.callWS({ type: "config_entries/subentries/delete", entry_id: card.entryId, subentry_id: card.id });
    } catch (err) {
      card.status = { ok: false, text: errorText(err) };
      this._paintConfirm(card, false);
      this._paintCard(card);
      return;
    }
    clearTimeout(card.timer);
    this._cards.delete(card.id);
    this._subentries[card.entryId] = (this._subentries[card.entryId] || []).filter(
      (sub) => sub.subentry_id !== card.id
    );
    this._render();
  }

  _openPlayground(entryId, kind, payload) {
    this._pgEntry = entryId;
    this._initial = { kind, payload };
    this._setTab("playground");
  }

  // --- Playground tab ---

  _renderPlayground() {
    if (this._embedOk === false) return [h("p", { class: "note" }, this._p("embed_unavailable"))];
    if (!this._entries.length) return [this._empty(this._p("no_entries"))];
    this._playground ??= document.createElement("pushward-playground");
    this._pgBox ??= h("div");
    this._look(this._playground);
    this._playground.initial = this._initial ?? null;
    const picker =
      this._entries.length > 1 &&
      h(
        "label",
        { class: "picker" },
        this._p("account"),
        h(
          "select",
          { "@change": (ev) => (this._pgEntry = ev.target.value) },
          this._entries.map((entry) =>
            h("option", { value: entry.entry_id, selected: entry.entry_id === this._pgEntry }, entry.title)
          )
        )
      );
    return fill(this._pgBox, picker, this._playground);
  }

  // --- Editor ---

  async _openEditor(entryId, type, subentryId) {
    this._notice = null;
    const ed = { entryId, type, subentryId, step: null, step1: {}, data: {}, seq: 0, busy: true };
    ed.schedule = debounce(() => this._runDraft(ed), DRAFT_MS);
    this._editor = ed;
    this._render();
    if (!(await loadHaForm())) {
      if (this._editor !== ed) return;
      ed.fallback = true;
      this._render();
      return;
    }
    await this._translations;
    try {
      const step = await this._hass.callApi(
        "POST",
        FLOW_API,
        {
          handler: [entryId, type],
          show_advanced_options: Boolean(this._hass.userData?.showAdvanced),
          ...(subentryId ? { subentry_id: subentryId } : {}),
        },
        FLOW_HEADERS
      );
      ed.flowId = step.flow_id;
      if (this._editor !== ed) {
        // Cancelled while the flow started.
        if (step.type === "form") this._hass.callApi("DELETE", `${FLOW_API}/${step.flow_id}`).catch(() => {});
        return;
      }
      this._showStep(ed, step);
    } catch (err) {
      if (this._editor !== ed) return;
      ed.busy = false;
      ed.message = errorText(err) || this._t("ui.common.unknown_error");
      this._render();
    }
  }

  _closeEditor() {
    const ed = this._editor;
    if (!ed) return;
    this._editor = null;
    ed.schedule.cancel();
    if (ed.flowId && !ed.finished) this._hass.callApi("DELETE", `${FLOW_API}/${ed.flowId}`).catch(() => {});
    if (!this.isConnected) return;
    this._render();
    this._refreshAll();
  }

  _showStep(ed, step) {
    ed.busy = false;
    if (step.type === "form") {
      // A fresh form per step, as in HA's dialog.
      ed.step = step;
      ed.form = this._createForm(ed);
      ed.data = initialData(step.data_schema);
      ed.submitErrors = null;
      ed.message = null;
      ed.draft = null;
      this._render();
      this._runDraft(ed);
      return;
    }
    ed.finished = step.type === "create_entry" || step.type === "abort";
    if (step.type === "create_entry" || step.reason === "reconfigure_successful") {
      this._notice = step.type === "abort" ? this._abortText(ed, step) : this._t("ui.common.successfully_saved");
      ed.schedule.cancel();
      this._editor = null;
      this._render();
      // The account reloads with the change; its config entry updates reload the list again once it is back.
      this._scheduleLoad();
      return;
    }
    // Other aborts; menu, external and progress steps are not used by these flows.
    ed.step = null;
    ed.abort = step.type === "abort" ? this._abortText(ed, step) : step.type;
    this._render();
  }

  _abortText(ed, step) {
    const key = step.translation_domain
      ? `component.${step.translation_domain}.config.abort.${step.reason}`
      : subentryKey(ed.type, `abort.${step.reason}`);
    return this._t(key, step.description_placeholders) || step.reason;
  }

  async _submit(ed) {
    const step = ed.step;
    if (ed.busy || !step) return;
    ed.submitErrors = null;
    if (!requiredFilled(step.data_schema, ed.data)) {
      ed.message = this._t(NOT_ALL_REQUIRED) || this._t("ui.common.error_required");
      this._render();
      return;
    }
    ed.busy = true;
    ed.message = null;
    this._render();
    const data = submitData(step.data_schema, ed.data);
    try {
      const next = await this._hass.callApi("POST", `${FLOW_API}/${step.flow_id}`, data, FLOW_HEADERS);
      if (this._editor !== ed) return;
      // The details step builds on the first step's entity and template.
      if (next.type === "form" && next.step_id === "details" && step.step_id !== "details") ed.step1 = data;
      this._showStep(ed, next);
    } catch (err) {
      if (this._editor !== ed) return;
      ed.busy = false;
      ed.submitErrors = err?.body?.errors || null;
      ed.message = err?.body?.message || (ed.submitErrors ? null : errorText(err) || this._t("ui.common.unknown_error"));
      this._render();
    }
  }

  _createForm(ed) {
    const form = document.createElement("ha-form");
    const stepKey = () => subentryKey(ed.type, `step.${ed.step.step_id}`);
    const placeholders = () => ed.step.description_placeholders;
    const prefix = (options) => (options?.path?.[0] ? `sections.${options.path[0]}.` : "");
    form.computeLabel = (field, _data, options) =>
      field.type === "expandable"
        ? this._t(`${stepKey()}.sections.${field.name}.name`, placeholders()) || field.name
        : this._t(`${stepKey()}.${prefix(options)}data.${field.name}`, placeholders()) || field.name;
    // One node per field: a new one on every render would redraw the markdown on each keystroke.
    const helpers = new Map();
    form.computeHelper = (field, options) => {
      if (field.type === "expandable") return this._t(`${stepKey()}.sections.${field.name}.description`, placeholders());
      const key = `${stepKey()}.${prefix(options)}data_description.${field.name}`;
      const text = this._t(key, placeholders());
      // HA's dialog renders field helpers as markdown.
      if (!text || !customElements.get("ha-markdown")) return text;
      if (helpers.get(key)?.content !== text) helpers.set(key, h("ha-markdown", { ".content": text, ".breaks": true }));
      return helpers.get(key);
    };
    form.computeError = (error) => this._t(subentryKey(ed.type, `error.${error}`), placeholders()) || error;
    form.localizeValue = (key, values) => this._t(`component.${DOMAIN}.selector.${key}`, values);
    form.addEventListener("value-changed", (ev) => {
      ed.data = ev.detail.value;
      form.data = ed.data;
      ed.schedule();
    });
    return form;
  }

  _fieldLabel(ed, name) {
    for (const field of ed.step.data_schema) {
      if (field.name === name) return ed.form.computeLabel(field, ed.data, {});
      const inner = field.type === "expandable" && field.schema.find((f) => f.name === name);
      if (inner) return ed.form.computeLabel(inner, ed.data, { path: [field.name] });
    }
    return name;
  }

  _renderEditor() {
    const ed = this._editor;
    const cancel = h("button", { "@click": () => this._closeEditor() }, this._t("ui.common.cancel"));
    if (ed.fallback) {
      return h("div", { class: "card" }, h("p", {}, this._p("form_unavailable")), this._link(), h("div", { class: "actions" }, cancel));
    }
    if (ed.abort) {
      return h(
        "div",
        { class: "card" },
        h("p", {}, ed.abort),
        h("div", { class: "actions" }, h("button", { "@click": () => this._closeEditor() }, this._t("ui.common.back")))
      );
    }
    const step = ed.step;
    if (!step) {
      return h(
        "div",
        { class: "card" },
        ed.message ? h("p", { class: "error", role: "alert" }, ed.message) : h("p", { class: "note" }, this._t("ui.common.loading")),
        h("div", { class: "actions" }, cancel)
      );
    }
    const key = subentryKey(ed.type, `step.${step.step_id}`);
    const title = this._t(`${key}.title`, step.description_placeholders) || "PushWard";
    const description = this._t(`${key}.description`, step.description_placeholders);
    const errors = { ...step.errors, ...ed.submitErrors };
    ed.form.hass = this._hass;
    ed.form.schema = step.data_schema;
    ed.form.data = ed.data;
    ed.form.error = Object.keys(errors).length ? errors : undefined;
    ed.form.disabled = ed.busy;
    ed.form.context = { handler: step.handler, domain: DOMAIN };
    const submit =
      this._t(`${key}.submit`) || this._t(step.last_step === false ? "ui.common.next" : "ui.common.submit");
    ed.previewPane ??= h("div", { class: "preview-pane" });
    this._paintDraft(ed);
    return h(
      "div",
      { class: "editor" },
      h(
        "div",
        { class: "card" },
        h("h2", {}, title),
        description &&
          (customElements.get("ha-markdown")
            ? h("ha-markdown", { ".content": description, ".breaks": true })
            : h("p", {}, description)),
        ed.message && h("p", { class: "error", role: "alert" }, ed.message),
        ed.form,
        h(
          "div",
          { class: "actions" },
          cancel,
          h("button", { class: "primary", ".disabled": ed.busy, "@click": () => this._submit(ed) }, submit)
        )
      ),
      ed.previewPane
    );
  }

  /** The draft preview's request, or null on the first step of a new activity or widget (nothing to show yet). */
  _draftRequest(ed) {
    const step = ed.step;
    if (ed.type === "tracked_todo" || step.step_id === "details") {
      const data = { ...ed.step1, ...submitData(step.data_schema, ed.data) };
      if (!data.entity_id) return null;
      // On reconfigure the saved subentry supplies the defaults its form fills on submit.
      return ed.subentryId ? { subentry_type: ed.type, subentry_id: ed.subentryId, data } : { subentry_type: ed.type, data };
    }
    return ed.subentryId ? { subentry_id: ed.subentryId } : null;
  }

  async _runDraft(ed) {
    if (this._editor !== ed || !ed.step) return;
    const request = this._draftRequest(ed);
    const ticket = ++ed.seq;
    let draft = null;
    if (request) {
      try {
        draft = { result: await this._hass.callWS({ type: "pushward/preview", entry_id: ed.entryId, ...request }) };
      } catch (error) {
        draft = { error };
      }
    }
    // A slower, older reply never paints over a newer one.
    if (this._editor !== ed || ticket !== ed.seq) return;
    ed.draft = draft;
    this._paintDraft(ed);
  }

  _paintDraft(ed) {
    const pane = ed.previewPane;
    if (!pane) return;
    const { result, error } = ed.draft || {};
    if (!result && !error) {
      fill(pane);
      return;
    }
    const kind = KINDS[ed.type];
    let preview = null;
    if (this._embedOk === false) preview = h("p", { class: "note" }, this._p("embed_unavailable"));
    else if (result?.available) preview = this._previewEl(ed, kind, result.payload);
    // While a value is invalid, the last good preview stays up with the hint under it.
    else if (result?.errors && ed.previewEl) preview = ed.previewEl;
    let hint = null;
    if (error) hint = h("p", { class: "error" }, this._p("preview_failed", { error: errorText(error) }));
    else if (result.errors) {
      hint = h(
        "div",
        { class: "hint", role: "status" },
        h("p", {}, this._p("preview_invalid")),
        h(
          "ul",
          {},
          Object.entries(result.errors).map(([field, message]) =>
            h("li", {}, `${this._fieldLabel(ed, field)}: ${ed.form.computeError(message)}`)
          )
        )
      );
    } else if (!result.available) hint = h("p", { class: "note" }, this._p("unavailable"));
    fill(pane, h("h3", {}, this._p("preview")), preview, hint);
  }
}

if (!customElements.get("pushward-panel")) {
  customElements.define("pushward-panel", PushWardPanel);
}
