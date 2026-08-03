// Validated via the dataviz skill's palette validator (CVD-safe, normal-vision
// floor >= 15) -- see the reference palette. Status colors are reserved for
// status encodings only; series colors for magnitude/identity comparisons.
export const CHART_SERIES_PRIMARY = "#2a78d6"; // blue -- single-series bars/lines, "Ordered"
export const CHART_SERIES_SECONDARY = "#1baf7a"; // aqua -- "Delivered" in 2-series comparisons

export const STATUS_COLORS = {
  COMPLETE: "#0ca30c",
  PARTIAL: "#fab219",
  NOT_DELIVERED: "#d03b3b",
};

export const CHART_GRID = "#e1e0d9";
export const CHART_AXIS = "#898781";
