/**
 * Funscript Upload — UI plugin
 *
 * Injects an "Upload funscript" button into the scene detail toolbar.
 * When clicked:
 *   1. Opens a file picker restricted to .funscript files.
 *   2. Shows a dialog: if the scene is already interactive it asks to
 *      confirm-replace; either way it shows a "Generate heatmap after upload"
 *      checkbox so the user can opt into immediate heatmap generation.
 *   3. Reads the file, base64-encodes it, and calls runPluginTask with the
 *      payload, scene_id, overwrite flag, and generate_after flag.
 *   4. Polls the job queue and shows a status toast when the task completes.
 *
 * Requires: CommunityScriptsUILibrary (provides csLib.PathElementListener
 * and csLib.callGQL).
 */
(function () {
  "use strict";

  const PLUGIN_ID = "funscript_upload";
  const TASK_NAME = "Upload Funscript";
  const BUTTON_ID = "funscript-upload-btn";
  const MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024; // 20 MB client-side guard

  // FA 6 solid "file-arrow-up" icon — inline SVG so no external font dependency.
  // viewBox 0 0 384 512, path from Font Awesome Free 6.7.2 (CC BY 4.0).
  const ICON_SVG =
    '<svg aria-hidden="true" focusable="false" data-prefix="fas" ' +
    'data-icon="file-arrow-up" class="svg-inline--fa fa-file-arrow-up" ' +
    'role="img" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 384 512">' +
    '<path fill="currentColor" d="M64 0C28.7 0 0 28.7 0 64L0 448c0 35.3 28.7 64 ' +
    "64 64l256 0c35.3 0 64-28.7 64-64l0-288-128 0c-17.7 0-32-14.3-32-32L224 0 " +
    "64 0zM256 0l0 128 128 0L256 0zM216 408c0 13.3-10.7 24-24 24s-24-10.7-24-24" +
    "l0-102.1-31 31c-9.4 9.4-24.6 9.4-33.9 0s-9.4-24.6 0-33.9l72-72c9.4-9.4 " +
    "24.6-9.4 33.9 0l72 72c9.4 9.4 9.4 24.6 0 33.9s-24.6 9.4-33.9 0l-31-31L216 " +
    '408z"/></svg>';

  // -------------------------------------------------------------------------
  // GraphQL helpers
  // -------------------------------------------------------------------------

  async function gql(query, variables) {
    return csLib.callGQL({ query, variables: variables || {} });
  }

  async function getSceneInteractive(sceneId) {
    const data = await gql(
      `query FindScene($id: ID!) { findScene(id: $id) { interactive } }`,
      { id: sceneId }
    );
    return !!(data && data.findScene && data.findScene.interactive);
  }

  async function runPluginTask(sceneId, payloadB64, overwrite) {
    const data = await gql(
      `mutation RunPluginTask(
          $plugin_id: ID!
          $task_name: String!
          $args_map: Map
        ) {
          runPluginTask(
            plugin_id: $plugin_id
            task_name: $task_name
            args_map: $args_map
          )
        }`,
      {
        plugin_id: PLUGIN_ID,
        task_name: TASK_NAME,
        args_map: {
          mode: "upload",
          scene_id: String(sceneId),
          payload_b64: payloadB64,
          overwrite: !!overwrite,
        },
      }
    );
    return data && data.runPluginTask;
  }

  async function pollJob(jobId, timeoutMs) {
    const deadline = Date.now() + (timeoutMs || 120000);
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 800));
      const data = await gql(
        `query Jobs { jobQueue { id status description error } }`
      );
      const queue = (data && data.jobQueue) || [];
      const job = queue.find((j) => j.id === jobId);
      if (!job) {
        // Job has cleared the queue — treat as finished.
        return { status: "FINISHED", error: null };
      }
      if (["FINISHED", "CANCELLED", "FAILED"].includes(job.status)) {
        return job;
      }
    }
    return { status: "TIMEOUT", error: "job did not complete within timeout" };
  }

  // -------------------------------------------------------------------------
  // Toast / status display
  // -------------------------------------------------------------------------

  function showToast(message, type) {
    // type: "success" | "error" | "info"
    const toast = document.createElement("div");
    toast.style.cssText = [
      "position:fixed",
      "bottom:24px",
      "right:24px",
      "z-index:9999",
      "padding:12px 18px",
      "border-radius:6px",
      "font-size:14px",
      "color:#fff",
      "max-width:400px",
      "word-break:break-word",
      "box-shadow:0 4px 12px rgba(0,0,0,0.4)",
      type === "error"
        ? "background:#c0392b"
        : type === "success"
        ? "background:#27ae60"
        : "background:#2980b9",
    ].join(";");
    toast.textContent = message;
    document.body.appendChild(toast);
    setTimeout(() => {
      toast.style.transition = "opacity 0.4s";
      toast.style.opacity = "0";
      setTimeout(() => toast.remove(), 450);
    }, 4000);
  }

  // -------------------------------------------------------------------------
  // Confirm-replace dialog
  //
  // Only shown when the scene is already interactive (an existing funscript
  // would be overwritten). For brand-new uploads the upload proceeds without
  // a confirmation step. The "generate heatmap after upload" behavior is
  // controlled by the plugin setting (Settings → Plugins → Funscript Upload),
  // not a per-upload toggle.
  //
  // Resolves to true if the user confirmed replacement, false if cancelled.
  // -------------------------------------------------------------------------

  function confirmReplace() {
    return new Promise((resolve) => {
      const overlay = document.createElement("div");
      overlay.style.cssText =
        "position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,0.6);" +
        "display:flex;align-items:center;justify-content:center";

      const box = document.createElement("div");
      box.style.cssText =
        "background:#2c2f33;color:#fff;padding:24px 28px;border-radius:8px;" +
        "max-width:440px;width:90%;font-size:14px;line-height:1.5";

      const msg = document.createElement("p");
      msg.textContent =
        "This scene already has a funscript (it is marked interactive). " +
        "Do you want to replace the existing funscript?";
      msg.style.marginBottom = "20px";
      box.appendChild(msg);

      const btnRow = document.createElement("div");
      btnRow.style.cssText = "display:flex;gap:10px;justify-content:flex-end";

      const cancelBtn = document.createElement("button");
      cancelBtn.textContent = "Cancel";
      cancelBtn.className = "btn btn-secondary";
      cancelBtn.onclick = () => {
        overlay.remove();
        resolve(false);
      };
      btnRow.appendChild(cancelBtn);

      const replaceBtn = document.createElement("button");
      replaceBtn.textContent = "Replace";
      replaceBtn.className = "btn btn-danger";
      replaceBtn.onclick = () => {
        overlay.remove();
        resolve(true);
      };
      btnRow.appendChild(replaceBtn);

      box.appendChild(btnRow);
      overlay.appendChild(box);
      document.body.appendChild(overlay);
    });
  }

  // -------------------------------------------------------------------------
  // Read file as base64
  // -------------------------------------------------------------------------

  function readFileAsBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        // result is "data:...;base64,<b64>" — strip the prefix.
        const b64 = reader.result.split(",")[1];
        resolve(b64);
      };
      reader.onerror = () => reject(new Error("FileReader error"));
      reader.readAsDataURL(file);
    });
  }

  // -------------------------------------------------------------------------
  // Main upload flow
  // -------------------------------------------------------------------------

  async function handleFileSelected(file, sceneId) {
    if (!file) return;

    if (!file.name.endsWith(".funscript")) {
      showToast("Please select a .funscript file.", "error");
      return;
    }

    if (file.size > MAX_FILE_SIZE_BYTES) {
      showToast(
        `File is too large (${(file.size / 1024 / 1024).toFixed(1)} MB). Maximum is 20 MB.`,
        "error"
      );
      return;
    }

    // Check if scene is already interactive — if so, confirm replacement first.
    let isInteractive = false;
    try {
      isInteractive = await getSceneInteractive(sceneId);
    } catch (e) {
      // Non-fatal — proceed as if not interactive.
      console.warn("[funscript_upload] could not check interactive state:", e);
    }

    if (isInteractive) {
      const ok = await confirmReplace();
      if (!ok) return;
    }

    // Encode the file.
    let payloadB64;
    try {
      payloadB64 = await readFileAsBase64(file);
    } catch (e) {
      showToast("Failed to read file: " + e.message, "error");
      return;
    }

    showToast("Uploading funscript…", "info");

    // Launch the backend task.
    let jobId;
    try {
      jobId = await runPluginTask(sceneId, payloadB64, isInteractive);
    } catch (e) {
      showToast("Failed to start upload task: " + e.message, "error");
      return;
    }

    if (!jobId) {
      showToast("Upload task did not return a job ID.", "error");
      return;
    }

    // Poll until the job completes.
    let job;
    try {
      job = await pollJob(jobId, 120000);
    } catch (e) {
      showToast("Error while waiting for task: " + e.message, "error");
      return;
    }

    if (job.status === "FINISHED" && !job.error) {
      showToast("Funscript uploaded successfully.", "success");
    } else if (job.status === "TIMEOUT") {
      showToast(
        "The upload task is taking longer than expected. Check the Tasks log.",
        "info"
      );
    } else {
      showToast(
        "Upload failed: " + (job.error || job.status),
        "error"
      );
    }
  }

  // -------------------------------------------------------------------------
  // Button injection
  // -------------------------------------------------------------------------

  function getSceneIdFromUrl() {
    // URL pattern: /scenes/<id>[/...]
    const match = window.location.pathname.match(/\/scenes\/(\d+)/);
    return match ? match[1] : null;
  }

  function injectButton() {
    // Avoid duplicate injection.
    if (document.getElementById(BUTTON_ID)) return;

    const sceneId = getSceneIdFromUrl();
    if (!sceneId) return;

    // Find the right-hand toolbar group (contains rating, O-counter, etc.)
    const toolbarGroups = document.querySelectorAll(".scene-toolbar-group");
    if (!toolbarGroups.length) return;
    // Append to the last toolbar group, before the operations dropdown.
    const group = toolbarGroups[toolbarGroups.length - 1];

    // Hidden file input.
    const fileInput = document.createElement("input");
    fileInput.type = "file";
    fileInput.accept = ".funscript";
    fileInput.style.display = "none";
    fileInput.id = BUTTON_ID + "-input";
    fileInput.addEventListener("change", () => {
      const file = fileInput.files && fileInput.files[0];
      if (file) handleFileSelected(file, sceneId);
      // Reset so the same file can be re-selected.
      fileInput.value = "";
    });
    document.body.appendChild(fileInput);

    // Icon button — matches the "minimal" icon-only style used by
    // OrganizedButton / OCounterButton and other scene toolbar siblings.
    // Uses an inline FA SVG so it blends with Stash's bundled FontAwesome icons.
    const wrapper = document.createElement("span");
    wrapper.id = BUTTON_ID;

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "minimal btn btn-secondary";
    btn.title = "Upload funscript";
    btn.innerHTML = ICON_SVG;
    btn.addEventListener("click", () => fileInput.click());

    wrapper.appendChild(btn);

    // Insert before the last child of the group (the operations "..." dropdown
    // wrapper), so the button sits alongside the other action icons.
    const lastChild = group.lastElementChild;
    if (lastChild) {
      group.insertBefore(wrapper, lastChild);
    } else {
      group.appendChild(wrapper);
    }
  }

  // -------------------------------------------------------------------------
  // Registration — fire on every navigation to a scene page
  // -------------------------------------------------------------------------

  csLib.PathElementListener("/scenes/", ".scene-toolbar", injectButton);
})();
