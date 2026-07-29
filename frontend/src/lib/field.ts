/**
 * Styling for the native form controls this app uses in place of shadcn's
 * input/select.
 *
 * Those would need a registry fetch to add, and both container images build the
 * frontend offline — so the login form, the runs filter and the criteria editor
 * (#53) style native `<input>`/`<select>` elements with the same tokens the
 * shadcn controls do (border-input, ring-ring, bg-background), which is what
 * keeps them consistent in either theme. It lives here because there are now
 * three call sites, and three drifting copies of a focus ring is how a form ends
 * up looking half-designed.
 *
 * Width is deliberately not set: the login form wants `w-full`, the runs filter a
 * fixed column, and the editor a mix. Callers append their own.
 */
export const FIELD =
  "h-9 rounded-lg border border-input bg-background px-3 text-sm outline-none " +
  "transition-all placeholder:text-muted-foreground " +
  "focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 " +
  "disabled:pointer-events-none disabled:opacity-50 dark:bg-input/30";
