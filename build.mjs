import { build } from "esbuild";

await build({
  entryPoints: ["webui_chart_renderer_app.jsx"],
  bundle: true,
  format: "iife",
  globalName: "MaiBotStatsChart",
  platform: "browser",
  jsx: "automatic",
  minify: true,
  define: { "process.env.NODE_ENV": '"production"' },
  legalComments: "external",
  outfile: "assets/statistics_chart.bundle.js",
});
