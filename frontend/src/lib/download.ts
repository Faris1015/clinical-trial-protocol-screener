/**
 * Handing a fetched response body to the browser's download machinery.
 *
 * Extracted when the cohort export (#102) landed beside the screening report
 * (#56): two controls, two endpoints, one set of browser quirks. Duplicating the
 * anchor dance would mean the next fix to it lands in one of them.
 *
 * Both downloads are fetched rather than linked. A plain `<a href download>` would
 * work — the routes are same-origin and the session is a cookie the browser
 * attaches itself — but it has no error channel: a 401 on an expired session, a
 * 409 for a run that never streamed, or a 404 would all navigate the user to a
 * JSON error body, and `apiFetch`'s session-expiry handler would never fire. Going
 * through fetch keeps both (the message renders inline, the expiry redirects) at
 * the cost of holding the document in memory for the length of one click.
 */

/**
 * The server's filename for this response, or `fallback`.
 *
 * Taken from `Content-Disposition` so the file a reviewer ends up with is named by
 * the same rule everywhere; the fallback covers a topology where the header isn't
 * readable (a cross-origin dev proxy that doesn't expose it), where a generic name
 * still beats a blob id.
 */
export function filenameFrom(disposition: string | null, fallback: string): string {
  const match = disposition?.match(/filename="([^"]+)"/);
  return match?.[1] ?? fallback;
}

/**
 * Save a fetched body to the user's downloads under `filename`.
 *
 * A synthetic click is the only way to hand a fetched body to the browser's
 * download machinery. Two details are not stylistic: the anchor has to be *in the
 * document* for a programmatic click to start a download in Firefox, and the
 * object URL must be revoked on a later task rather than on the line after
 * `click()` — revoking inside the same task cancels the download in some browsers,
 * while never revoking pins the whole document in memory for the lifetime of the
 * page.
 */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.hidden = true;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
