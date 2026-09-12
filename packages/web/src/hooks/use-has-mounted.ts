import { useEffect, useState } from "react";

/**
 * True only after the first client-side render has committed and the
 * browser has completed a layout pass. Recharts' ResponsiveContainer
 * measures its container synchronously on mount; inside a CSS Grid track
 * with a `minmax(0, ...)` minimum (see the analytics page's chart grid),
 * that measurement can land during the browser's very first layout
 * calculation and read 0x0 before the grid resolves its final track sizes,
 * which Recharts logs as an "invalid container dimension" warning even
 * though it recovers a moment later. Gating chart render on this hook skips
 * that first, premature measurement instead of trying to out-debounce it.
 */
export function useHasMounted(): boolean {
  const [hasMounted, setHasMounted] = useState(false);

  useEffect(() => {
    setHasMounted(true);
  }, []);

  return hasMounted;
}
