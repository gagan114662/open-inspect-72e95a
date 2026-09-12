import path from "path";
import type { NextConfig } from "next";

const monorepoRoot = path.join(__dirname, "../..");

const nextConfig: NextConfig = {
  agentRules: false,
  // Standalone output is what OpenNext (web_platform = "cloudflare") builds from.
  // Vercel's builder does its own function bundling and traces the non-standalone
  // output; forcing standalone there leaves next-server.js.nft.json unwritten in
  // the location Vercel's build step expects, failing the build. Vercel sets
  // process.env.VERCEL during both remote and `vercel build` builds.
  output: process.env.VERCEL ? undefined : "standalone",
  // Both must match the monorepo root for Turbopack to resolve workspace packages
  outputFileTracingRoot: monorepoRoot,
  turbopack: {
    root: monorepoRoot,
  },
};

export default nextConfig;
