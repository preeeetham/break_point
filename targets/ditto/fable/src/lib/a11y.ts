let region: HTMLElement | null = null;

/** One shared polite live region for status changes. */
export function announce(text: string) {
  if (typeof document === "undefined") return;
  if (!region) {
    region = document.createElement("div");
    region.setAttribute("role", "status");
    region.setAttribute("aria-live", "polite");
    region.setAttribute("aria-atomic", "true");
    region.className = "sr-only";
    document.body.appendChild(region);
  }
  region.textContent = "";
  window.setTimeout(() => {
    if (region) region.textContent = text;
  }, 60);
}
