import { useEffect } from "react";

// Wide tables never scroll sideways (21 Nov 2026). Every `table.data-table`
// inside `root` is measured: when its natural width does not fit its box, it
// is shown as "record rows" instead -- the first cell is the title and every
// other cell a labelled field, wrapping onto as many lines as it needs, so
// the row still carries ALL its information. It switches back to a normal
// table by itself when the window is wide enough.
//
// The labels come from the table's own header cells, so a page does not have
// to change. A cell with an empty header (the actions column) stays unlabelled.

function labelsOf(table) {
  return [...table.querySelectorAll(":scope > thead th")].map((th) => (th.textContent || "").trim());
}

function decorate(table) {
  const labels = labelsOf(table);
  // The row's title is the column whose header has `data-title`, else the first.
  const heads = [...table.querySelectorAll(":scope > thead th")];
  let titleAt = heads.findIndex((th) => th.hasAttribute("data-title"));
  if (titleAt < 0) titleAt = 0;
  table.querySelectorAll(":scope > tbody > tr").forEach((row) => {
    [...row.children].forEach((cell, index) => {
      if (cell.tagName !== "TD") return;
      const label = cell.colSpan > 1 ? "" : labels[index] || "";
      if (cell.getAttribute("data-label") !== label) cell.setAttribute("data-label", label);
      const short = (cell.textContent || "").trim().length <= 14;
      if (short !== cell.hasAttribute("data-short")) cell.toggleAttribute("data-short", short);
      if (index === titleAt) cell.setAttribute("data-title", "");
      else cell.removeAttribute("data-title");
    });
  });
}

// The width the table wants as a normal table, found with the record layout
// off. Kept per table so a window resize only has to compare two numbers.
function measure(table) {
  table.removeAttribute("data-records");
  // Natural width with every cell on one line (what the old table wanted).
  table.setAttribute("data-measure", "");
  const box = table.closest(".table-scroll") || table.parentElement;
  table.__wanted = table.scrollWidth;
  table.__box = box;
  table.removeAttribute("data-measure");
}

function apply(table) {
  const box = table.__box || table.parentElement;
  // A table stays a table only when it fits as it is (a few long cells may
  // wrap); otherwise it is shown as record rows.
  const fits = (table.__wanted || 0) <= (box ? box.clientWidth : 0) * 1.05;
  if (fits) table.removeAttribute("data-records");
  else table.setAttribute("data-records", "");
}

export function useResponsiveTables(rootRef, deps = []) {
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return undefined;
    let frame = 0;

    function all() {
      return [...root.querySelectorAll("table.data-table")];
    }
    function refresh() {
      frame = 0;
      for (const table of all()) {
        decorate(table);
        measure(table);
        apply(table);
      }
    }
    function resized() {
      for (const table of all()) {
        // A table whose box changed width: measure again (cheap) and decide.
        measure(table);
        apply(table);
      }
    }
    function schedule() {
      if (!frame) frame = requestAnimationFrame(refresh);
    }

    refresh();
    const mutations = new MutationObserver(schedule);
    mutations.observe(root, { childList: true, subtree: true, characterData: true });
    let timer = 0;
    const onResize = () => {
      clearTimeout(timer);
      timer = setTimeout(resized, 80);
    };
    window.addEventListener("resize", onResize);
    return () => {
      mutations.disconnect();
      window.removeEventListener("resize", onResize);
      clearTimeout(timer);
      if (frame) cancelAnimationFrame(frame);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}
