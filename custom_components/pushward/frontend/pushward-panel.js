// Placeholder for the PushWard sidebar panel; the panel itself replaces this file.
class PushWardPanel extends HTMLElement {}

if (!customElements.get("pushward-panel")) {
  customElements.define("pushward-panel", PushWardPanel);
}
