import type { TooltipContentProps } from "recharts";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { AnalyticsBreakdownResponse } from "@open-inspect/shared/types/analytics";
import { formatAnalyticsCount } from "@/lib/analytics";
import { formatSessionCost } from "@/lib/session-cost";
import { useHasMounted } from "@/hooks/use-has-mounted";

const Y_AXIS_WIDTH = 140;
// Rough average glyph width at fontSize 12 in the UI font; good enough to
// decide a character budget without measuring text in the DOM.
const CHAR_PX = 6.5;
const MAX_LABEL_CHARS = Math.max(4, Math.floor((Y_AXIS_WIDTH - 12) / CHAR_PX));

function truncateRepoLabel(label: string): string {
  // Prefer dropping the owner segment first ("owner/repo" -> "repo") since
  // the owner is redundant in a single-tenant deployment and this alone
  // fits most real repo names without any ellipsis.
  const shortName = label.includes("/") ? label.slice(label.indexOf("/") + 1) : label;
  if (shortName.length <= MAX_LABEL_CHARS) return shortName;
  return `${shortName.slice(0, MAX_LABEL_CHARS - 1)}…`;
}

function RepoAxisTick({ x, y, payload }: { x?: number; y?: number; payload?: { value: string } }) {
  const full = payload?.value ?? "";
  return (
    <text x={x} y={y} dy={4} textAnchor="end" fontSize={12} fill="var(--foreground)">
      <title>{full}</title>
      {truncateRepoLabel(full)}
    </text>
  );
}

interface RepoBarChartProps {
  entries?: AnalyticsBreakdownResponse["entries"];
  loading: boolean;
}

interface RepoChartRow {
  repo: string;
  sessions: number;
  cost: number;
  prs: number;
  messageCount: number;
}

function RepoChartTooltip({ active, payload }: TooltipContentProps) {
  const row = payload?.[0]?.payload as RepoChartRow | undefined;

  if (!active || !row) {
    return null;
  }

  return (
    // Fixed, deliberately narrow width rather than a viewport-relative calc:
    // Recharts clamps tooltip position to the chart's own container box, not
    // the browser viewport, so a tooltip sized off 100vw can still exceed a
    // narrow chart card (e.g. a phone-width single-column layout) and
    // overflow anyway. 11rem comfortably fits the four short stat rows and
    // stays well inside even a small chart container.
    <div className="w-[11rem] rounded-md border border-border bg-popover px-3 py-2 text-xs text-popover-foreground shadow-md">
      <div className="truncate font-medium text-foreground" title={row.repo}>
        {row.repo}
      </div>
      <div className="mt-2 grid gap-1.5">
        <div className="flex items-center justify-between gap-4">
          <span className="text-muted-foreground">Sessions</span>
          <span className="font-medium text-foreground">{formatAnalyticsCount(row.sessions)}</span>
        </div>
        <div className="flex items-center justify-between gap-4">
          <span className="text-muted-foreground">Cost</span>
          <span className="font-medium text-foreground">{formatSessionCost(row.cost)}</span>
        </div>
        <div className="flex items-center justify-between gap-4">
          <span className="text-muted-foreground">PRs</span>
          <span className="font-medium text-foreground">{formatAnalyticsCount(row.prs)}</span>
        </div>
        <div className="flex items-center justify-between gap-4">
          <span className="text-muted-foreground">Messages</span>
          <span className="font-medium text-foreground">
            {formatAnalyticsCount(row.messageCount)}
          </span>
        </div>
      </div>
    </div>
  );
}

export function AnalyticsRepoBarChart({ entries, loading }: RepoBarChartProps) {
  const hasMounted = useHasMounted();

  if (!hasMounted || (loading && !entries)) {
    return (
      <div className="rounded-md border border-border-muted bg-card p-5 animate-pulse">
        <div className="h-4 w-44 rounded bg-muted" />
        <div className="mt-2 h-4 w-64 rounded bg-muted" />
        <div className="mt-6 h-[320px] rounded bg-muted" />
      </div>
    );
  }

  if (!entries?.length) {
    return (
      <div className="rounded-md border border-border-muted bg-card p-5">
        <div className="text-lg font-semibold text-foreground">Sessions by Repository</div>
        <p className="mt-1 text-sm text-muted-foreground">
          No repository data found for this range.
        </p>
      </div>
    );
  }

  const chartHeight = Math.max(260, entries.length * 44);
  const leadRepo = entries.reduce(
    (top, entry) => (entry.sessions > top.sessions ? entry : top),
    entries[0]
  );
  const chartData = entries.map((entry) => ({
    repo: entry.key,
    sessions: entry.sessions,
    cost: entry.cost,
    prs: entry.prs,
    messageCount: entry.messageCount,
  }));

  return (
    <div className="rounded-md border border-border-muted bg-card p-5">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h2 className="text-lg font-semibold text-foreground">Sessions by Repository</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Horizontal distribution of session volume across repositories.
          </p>
        </div>
        <div className="grid gap-2 sm:min-w-[15rem] sm:grid-cols-2">
          <div className="rounded-md border border-border-muted bg-background px-3 py-3">
            <div className="text-xs uppercase tracking-wider text-secondary-foreground">
              Tracked repos
            </div>
            <div className="mt-2 text-lg font-semibold text-foreground">
              {formatAnalyticsCount(entries.length)}
            </div>
          </div>
          <div className="rounded-md border border-border-muted bg-background px-3 py-3">
            <div className="text-xs uppercase tracking-wider text-secondary-foreground">
              Top repo
            </div>
            <div className="mt-2 truncate text-sm font-semibold text-foreground">
              {leadRepo.key}
            </div>
            <div className="mt-1 text-xs text-muted-foreground">
              {formatAnalyticsCount(leadRepo.sessions)} sessions
            </div>
          </div>
        </div>
      </div>

      <div className="mt-6 max-h-[420px] overflow-y-auto rounded-lg border border-border-muted bg-background p-3 pr-2 sm:p-4">
        <div style={{ height: chartHeight }}>
          <ResponsiveContainer width="100%" height="100%" debounce={200}>
            <BarChart
              data={chartData}
              layout="vertical"
              margin={{ top: 8, right: 12, left: 12, bottom: 0 }}
            >
              <CartesianGrid stroke="var(--border)" horizontal={false} />
              <XAxis
                type="number"
                allowDecimals={false}
                axisLine={false}
                tickLine={false}
                tick={{ fill: "var(--muted-foreground)", fontSize: 12 }}
              />
              <YAxis
                type="category"
                dataKey="repo"
                width={Y_AXIS_WIDTH}
                axisLine={false}
                tickLine={false}
                tick={<RepoAxisTick />}
              />
              <Tooltip
                cursor={{ fill: "var(--accent-muted)" }}
                content={(props) => <RepoChartTooltip {...props} />}
              />
              <Bar dataKey="sessions" fill="var(--accent)" radius={[0, 4, 4, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
      <div className="mt-3 text-xs text-muted-foreground">
        The bars reflect session volume, and hover details include cost, PR totals, and messages.
      </div>
    </div>
  );
}
