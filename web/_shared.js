/**
 * Cartograph shared editor library.
 *
 * Two modules:
 *   - Cartograph.Save:    handles dirty state + debounced PUT to /api/config/<name>
 *   - Cartograph.History: in-memory undo/redo stack with Cmd-Z / Cmd-Shift-Z bindings
 *
 * Pages opt in by calling Cartograph.Save.init(...) and Cartograph.History.init(...).
 * The two modules are independent — a page can use Save without History or
 * vice-versa, but typical use is both together.
 *
 * Save modes:
 *   - "auto":    markDirty() schedules a debounced PUT (narrative.html style)
 *   - "manual":  PUT only happens via forceSave() — markDirty() updates status
 *                pill but doesn't persist. Used by pages with explicit Commit
 *                buttons (place.html, paint.html, etc.).
 *
 * History contract:
 *   The page provides getCfg() (deep-cloneable cfg object) and applyCfg(cfg)
 *   (function that restores the page's in-memory state from the cloned cfg).
 *   pushSnapshot() captures the current cfg; undo() / redo() restore neighbors.
 *
 * Status pill integration:
 *   If a #saveStatus element exists with [data-state] attribute, Save updates
 *   it automatically. Pages without one are silently skipped. The build pill
 *   (#buildPill) is also updated when present.
 */
(function () {
  "use strict";

  // ============================================================
  //  Utilities
  // ============================================================

  function deepClone(obj) {
    // Reliable for plain JSON-friendly cfg objects; not suitable for
    // anything with cycles or non-serializable values, but our cfg
    // shape is YAML-derived so this is safe.
    return obj == null ? obj : JSON.parse(JSON.stringify(obj));
  }

  function deepEqual(a, b) {
    return JSON.stringify(a) === JSON.stringify(b);
  }

  // ============================================================
  //  Save module
  // ============================================================

  const Save = (function () {
    const state = {
      project: null,
      getCfg: null, // () => current cfg object
      mode: "manual", // "auto" | "manual"
      debounceMs: 700,
      pendingTimer: null,
      currentStatus: "idle", // idle | dirty | saving | saved | error
      detail: "",
      listeners: [], // [{cb, types}]
      // Hooks fired before/after each save; pages can use these to
      // synchronize related work (e.g. render trigger).
      onBeforeSave: null,
      onAfterSave: null,
    };

    function init(opts) {
      if (!opts || !opts.project || typeof opts.getCfg !== "function") {
        console.warn("Save.init: requires {project, getCfg}");
        return;
      }
      state.project = opts.project;
      state.getCfg = opts.getCfg;
      state.mode = opts.mode === "auto" ? "auto" : "manual";
      state.debounceMs = Number(opts.debounceMs) || 700;
      state.onBeforeSave = opts.onBeforeSave || null;
      state.onAfterSave = opts.onAfterSave || null;
      setStatus("idle");
    }

    function setStatus(status, detail) {
      state.currentStatus = status;
      state.detail = detail || "";
      // Update #saveStatus pill if present
      const ss = document.querySelector("#saveStatus");
      if (ss) {
        ss.setAttribute("data-state", status);
        const txt = document.querySelector("#saveStatusText");
        if (txt) {
          txt.textContent =
            {
              idle: "Idle",
              dirty: "Dirty",
              saving: "Saving…",
              saved: "Saved",
              error: "Error",
            }[status] || status;
        }
        const det = document.querySelector("#saveStatusDetail");
        if (det) det.textContent = detail || "";
      }
      // Notify listeners
      for (const l of state.listeners) {
        try {
          l(status, detail);
        } catch (e) {
          console.warn(e);
        }
      }
    }

    function markDirty() {
      if (state.currentStatus !== "saving") setStatus("dirty");
      if (state.mode === "auto") {
        if (state.pendingTimer) clearTimeout(state.pendingTimer);
        state.pendingTimer = setTimeout(commit, state.debounceMs);
      }
    }

    async function commit() {
      if (!state.project || !state.getCfg) return;
      // If a save was in flight, retry later
      if (state.currentStatus === "saving") {
        state.pendingTimer = setTimeout(commit, 200);
        return;
      }
      const cfg = state.getCfg();
      if (!cfg) return;
      setStatus("saving");
      try {
        if (state.onBeforeSave) {
          try {
            await state.onBeforeSave(cfg);
          } catch (_) {
            /* non-fatal */
          }
        }
        const res = await fetch("/api/config/" + state.project, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(cfg),
        });
        if (!res.ok) {
          setStatus("error", "PUT " + res.status);
          return;
        }
        setStatus("saved");
        // Clear back to idle after a short hold
        setTimeout(() => {
          if (state.currentStatus === "saved") setStatus("idle");
        }, 1200);
        if (state.onAfterSave) {
          try {
            await state.onAfterSave(cfg);
          } catch (_) {}
        }
      } catch (e) {
        setStatus("error", String(e.message || e));
      }
    }

    function forceSave() {
      // Cancel any pending debounced save and commit immediately
      if (state.pendingTimer) {
        clearTimeout(state.pendingTimer);
        state.pendingTimer = null;
      }
      return commit();
    }

    function onStatusChange(callback) {
      state.listeners.push(callback);
    }

    function getStatus() {
      return state.currentStatus;
    }

    return { init, markDirty, forceSave, setStatus, onStatusChange, getStatus };
  })();

  // ============================================================
  //  History module
  // ============================================================

  const History = (function () {
    const state = {
      getCfg: null,
      applyCfg: null,
      maxSnapshots: 50,
      stack: [], // [{label, cfg}]
      index: -1, // index of CURRENT state in stack (-1 = empty)
      listeners: [],
    };

    function init(opts) {
      if (
        !opts ||
        typeof opts.getCfg !== "function" ||
        typeof opts.applyCfg !== "function"
      ) {
        console.warn("History.init: requires {getCfg, applyCfg}");
        return;
      }
      state.getCfg = opts.getCfg;
      state.applyCfg = opts.applyCfg;
      state.maxSnapshots = Number(opts.maxSnapshots) || 50;
      state.stack = [];
      state.index = -1;
      // Push the initial state as the baseline
      pushSnapshot("initial");
    }

    function pushSnapshot(label) {
      if (!state.getCfg) return;
      const cfg = deepClone(state.getCfg());
      // Dedup: skip if identical to the current snapshot
      if (state.index >= 0) {
        const top = state.stack[state.index];
        if (top && deepEqual(top.cfg, cfg)) return;
      }
      // Trim future snapshots (we're branching off the current index)
      if (state.index < state.stack.length - 1) {
        state.stack = state.stack.slice(0, state.index + 1);
      }
      state.stack.push({ label: label || "", cfg });
      // Cap the stack size from the BOTTOM (drop oldest)
      while (state.stack.length > state.maxSnapshots) {
        state.stack.shift();
      }
      state.index = state.stack.length - 1;
      notify();
    }

    function undo() {
      if (!canUndo()) return false;
      state.index -= 1;
      const snap = state.stack[state.index];
      try {
        state.applyCfg(deepClone(snap.cfg));
      } catch (e) {
        console.warn("History.undo: applyCfg failed", e);
      }
      notify();
      return true;
    }

    function redo() {
      if (!canRedo()) return false;
      state.index += 1;
      const snap = state.stack[state.index];
      try {
        state.applyCfg(deepClone(snap.cfg));
      } catch (e) {
        console.warn("History.redo: applyCfg failed", e);
      }
      notify();
      return true;
    }

    function canUndo() {
      return state.index > 0;
    }
    function canRedo() {
      return state.index < state.stack.length - 1;
    }

    function depth() {
      return {
        undoLevels: Math.max(0, state.index),
        redoLevels: Math.max(0, state.stack.length - 1 - state.index),
      };
    }

    function onChange(callback) {
      state.listeners.push(callback);
    }

    function notify() {
      const d = depth();
      for (const l of state.listeners) {
        try {
          l(d);
        } catch (e) {
          console.warn(e);
        }
      }
    }

    return {
      init,
      pushSnapshot,
      undo,
      redo,
      canUndo,
      canRedo,
      depth,
      onChange,
    };
  })();

  // ============================================================
  //  Keyboard bindings
  //
  //  Cmd-Z / Ctrl-Z       → History.undo
  //  Cmd-Shift-Z / Ctrl-Y → History.redo
  //
  //  Skipped when focus is inside an editable text field — there the
  //  browser's native undo handles character-level edits, which is
  //  what users expect. The page's own input handlers will push a
  //  snapshot when the field commits (blur/change), and AT THAT POINT
  //  Cmd-Z works at the snapshot granularity.
  // ============================================================

  function isTextEditable(el) {
    if (!el) return false;
    const tag = el.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA") {
      // Number/range/checkbox/radio aren't text editable in the
      // browser-native-undo sense; let our undo handle them.
      const type = (el.type || "").toLowerCase();
      const textTypes = new Set([
        "text",
        "search",
        "email",
        "url",
        "tel",
        "password",
        "",
        undefined,
      ]);
      return textTypes.has(type);
    }
    if (el.isContentEditable) return true;
    return false;
  }

  function installKeybindings() {
    document.addEventListener("keydown", (e) => {
      const isMac = /Mac|iPad|iPhone/.test(navigator.platform);
      const mod = isMac ? e.metaKey : e.ctrlKey;
      if (!mod) return;
      const key = e.key.toLowerCase();
      // Cmd-Z / Ctrl-Z   → undo (unless shift held)
      // Cmd-Shift-Z      → redo
      // Ctrl-Y           → redo (Windows convention)
      if (
        (key === "z" && !e.shiftKey) ||
        (e.shiftKey && key === "z" && !isMac)
      ) {
        if (isTextEditable(e.target)) return;
        if (key === "z" && e.shiftKey) {
          if (History.redo()) e.preventDefault();
          return;
        }
        if (History.undo()) e.preventDefault();
      } else if (key === "z" && e.shiftKey) {
        if (isTextEditable(e.target)) return;
        if (History.redo()) e.preventDefault();
      } else if (key === "y" && !isMac) {
        if (isTextEditable(e.target)) return;
        if (History.redo()) e.preventDefault();
      }
    });
  }

  installKeybindings();

  // ============================================================
  //  Undo/redo UI affordance
  //
  //  A small floating pill in the bottom-left corner with two buttons
  //  (← undo, → redo). Self-mounts the first time History.init() is
  //  called. The buttons enable/disable themselves via History.onChange,
  //  so they always reflect the live stack state. Hidden entirely when
  //  no History is initialized (so pages that don't opt in see nothing).
  //
  //  Styling is inlined to avoid a per-page CSS edit; it picks colors
  //  from CSS custom properties when available, falling back to neutral
  //  greys so it works on every page's theme.
  // ============================================================

  function installUndoAffordance() {
    if (document.getElementById("cartoUndoPill")) return;

    const wrap = document.createElement("div");
    wrap.id = "cartoUndoPill";
    wrap.setAttribute("role", "group");
    wrap.setAttribute("aria-label", "Undo and redo");
    wrap.style.cssText = [
      "position:fixed",
      "left:14px",
      "bottom:14px",
      "z-index:9999",
      "display:none", // hidden until History.init runs
      "gap:0",
      "background:var(--surface, rgba(20,18,14,0.85))",
      "color:var(--ink, #d8cfb8)",
      "border:1px solid var(--rule, rgba(120,105,75,0.45))",
      "border-radius:999px",
      "padding:3px",
      "font:11px/1 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace",
      "letter-spacing:0.06em",
      "box-shadow:0 6px 18px rgba(0,0,0,0.35)",
      "backdrop-filter:blur(4px)",
      "user-select:none",
    ].join(";");

    function makeBtn(label, title, onClick) {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = label;
      b.title = title;
      b.style.cssText = [
        "appearance:none",
        "background:transparent",
        "color:inherit",
        "border:0",
        "padding:6px 10px",
        "border-radius:999px",
        "cursor:pointer",
        "font:inherit",
        "letter-spacing:inherit",
        "opacity:0.45",
        "transition:opacity 120ms ease, background 120ms ease",
      ].join(";");
      b.addEventListener("mouseenter", () => {
        if (!b.disabled) b.style.background = "rgba(255,255,255,0.06)";
      });
      b.addEventListener("mouseleave", () => {
        b.style.background = "transparent";
      });
      b.addEventListener("click", onClick);
      return b;
    }

    const isMac = /Mac|iPad|iPhone/.test(navigator.platform);
    const undoKey = isMac ? "⌘Z" : "Ctrl+Z";
    const redoKey = isMac ? "⇧⌘Z" : "Ctrl+Y";
    const undoBtn = makeBtn("↶ undo", `Undo (${undoKey})`, () => {
      History.undo();
    });
    const redoBtn = makeBtn("↷ redo", `Redo (${redoKey})`, () => {
      History.redo();
    });
    wrap.appendChild(undoBtn);
    wrap.appendChild(redoBtn);

    function refresh(d) {
      // d may be undefined on first call — pull live depth instead.
      const depth = d || History.depth();
      undoBtn.disabled = depth.undoLevels === 0;
      redoBtn.disabled = depth.redoLevels === 0;
      undoBtn.style.opacity = undoBtn.disabled ? "0.3" : "0.85";
      redoBtn.style.opacity = redoBtn.disabled ? "0.3" : "0.85";
      undoBtn.style.cursor = undoBtn.disabled ? "default" : "pointer";
      redoBtn.style.cursor = redoBtn.disabled ? "default" : "pointer";
    }

    document.body.appendChild(wrap);

    // History.onChange fires after every push/undo/redo. Subscribe so
    // the pill stays accurate without polling.
    History.onChange(refresh);
    refresh();

    // Reveal the pill the first time History.init runs. We watch by
    // monkey-patching init: subsequent calls are idempotent.
    return {
      reveal: () => {
        wrap.style.display = "inline-flex";
      },
    };
  }

  const __affordance = (function () {
    // Wrap History.init so we can lazily reveal the pill on first init.
    const orig = History.init;
    let pill = null;
    History.init = function (...args) {
      const result = orig.apply(this, args);
      if (!pill) pill = installUndoAffordance();
      if (pill && pill.reveal) pill.reveal();
      return result;
    };
    return null;
  })();

  // ============================================================
  //  Public API
  // ============================================================

  window.Cartograph = window.Cartograph || {};
  window.Cartograph.Save = Save;
  window.Cartograph.History = History;
  // Convenience for pages: glue Save and History together so
  // Save.markDirty() also pushes a History snapshot. Pages that don't
  // want this can skip the call.
  window.Cartograph.bindSaveToHistory = function (label) {
    let snapTimer = null;
    Save.onStatusChange((status) => {
      // Push a snapshot whenever a save COMMITS (or a manual-mode
      // markDirty signals an intent to checkpoint).
      if (status === "dirty" || status === "saved") {
        // Debounce snapshots so rapid edit bursts collapse into one
        if (snapTimer) clearTimeout(snapTimer);
        snapTimer = setTimeout(() => {
          History.pushSnapshot(label || "edit");
        }, 250);
      }
    });
  };
})();
