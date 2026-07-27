/**
 * Page title + one-line description. Every route opens with one, so the heading
 * level and spacing are decided here rather than per page.
 */
export function PageHeader({ title, description }: { title: string; description?: string }) {
  return (
    <div className="mb-6">
      <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
      {description && <p className="text-muted-foreground mt-1 text-sm">{description}</p>}
    </div>
  );
}
