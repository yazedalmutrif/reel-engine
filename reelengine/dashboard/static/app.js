/* Reel Engine dashboard: progressive enhancement for uploads and edits.
 * Without the local write API (read-only server, a file:// view, or a failed session call) nothing here
 * runs and the page stays the read-only static site. Same-origin only (CSP connect-src 'self'); the CSRF
 * token comes from GET /api/session and is sent as the X-CSRF-Token header on every POST. */
(function () {
  "use strict";
  var S = null;
  var $ = function (sel, el) { return (el || document).querySelector(sel); };
  var $$ = function (sel, el) { return Array.prototype.slice.call((el || document).querySelectorAll(sel)); };

  function post(path, data) {
    return fetch("/api/" + path, {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": S.csrf },
      body: JSON.stringify(data)
    }).then(function (r) {
      return r.json().catch(function () { return { error: "HTTP " + r.status }; }).then(function (j) {
        /* keep the field name so a form can put the message under the right input */
        if (!r.ok || j.error) { var e = new Error(j.error || ("HTTP " + r.status)); e.field = j.field; throw e; }
        return j;
      });
    });
  }
  function status(el, text, good) {
    if (!el) { return; }
    el.textContent = text;
    el.classList.toggle("form-status-good", !!good);
    el.classList.toggle("form-status-bad", good === false);
  }
  function reloadSoon() { setTimeout(function () { window.location.reload(); }, 900); }
  function mb(n) { return (n / 1048576).toFixed(n > 104857600 ? 0 : 1); }

  /* ---------------------------------------------------------------- copy */
  function initCopy() {
    $$("[data-copy]").forEach(function (b) {
      b.addEventListener("click", function () {
        var t = document.getElementById(b.getAttribute("data-copy"));
        if (!t || !navigator.clipboard) { return; }
        navigator.clipboard.writeText("https://" + t.textContent.trim()).then(function () {
          var old = b.textContent; b.textContent = "Copied";
          setTimeout(function () { b.textContent = old; }, 2000);
        });
      });
    });
  }

  /* ---------------------------------------------------------------- calendar */
  function initCalendar() {
    var form = $("[data-cal-form]");
    if (!form) { return; }
    var st = $("[data-cal-form-status]");
    var title = $("[data-cal-form-title]");
    var submit = $("[data-cal-submit]");
    function setRule(rule) {
      $$("[data-cal-rule]", form).forEach(function (r) { r.checked = r.value === rule; });
      $$("[data-cal-rule-only]", form).forEach(function (el) {
        var on = el.getAttribute("data-cal-rule-only").split(" ").indexOf(rule) >= 0;
        el.hidden = !on;
        $$("input,select,textarea", el).forEach(function (i) { i.disabled = !on; });
      });
    }
    function clearErrors() {
      $$(".field-error", form).forEach(function (e) { e.textContent = ""; });
      $$("[aria-invalid]", form).forEach(function (e) { e.removeAttribute("aria-invalid"); });
    }
    function reset() {
      form.reset(); clearErrors(); status(st, "");
      $("[data-cal-slug]", form).value = "";
      title.textContent = "Add occasion"; submit.textContent = "Add occasion";
      setRule("gregorian");
    }
    $$("[data-cal-rule]", form).forEach(function (r) { r.addEventListener("change", function () { setRule(r.value); }); });
    $$("[data-cal-new]").forEach(function (b) { b.addEventListener("click", function () { reset(); $("#cal-name-ar").focus(); }); });
    var cancel = $("[data-cal-form-cancel]");
    if (cancel) { cancel.addEventListener("click", reset); }
    $$("[data-cal-edit]").forEach(function (b) {
      b.addEventListener("click", function () {
        var row = b.closest("[data-cal-row]");
        var o = JSON.parse(row.getAttribute("data-cal-json"));
        reset();
        $("[data-cal-slug]", form).value = o.slug;
        ["name_ar", "name_en", "month", "day", "year", "duration_days", "category", "angle", "notes", "source_url", "checked_on", "keywords"].forEach(function (k) {
          var el = form.elements[k];
          if (el && o[k] !== null && o[k] !== undefined) { el.value = o[k]; }
        });
        form.elements.official.checked = !!o.official;
        setRule(o.rule);
        title.textContent = "Edit occasion · " + o.name_en; submit.textContent = "Save changes";
        document.getElementById("cal-form").scrollIntoView({ behavior: "smooth", block: "start" });
        $("#cal-name-ar").focus({ preventScroll: true });
      });
    });
    $$("[data-cal-delete]").forEach(function (b) {
      b.addEventListener("click", function () {
        var slug = b.getAttribute("data-cal-delete");
        var row = $('[data-cal-confirm-row="' + slug + '"]');
        var open = row.hidden;
        row.hidden = !open; b.setAttribute("aria-expanded", open ? "true" : "false");
        b.closest("tr").classList.toggle("cal-row-confirm", open);
        if (open) { $(".confirm-text", row).focus(); }
      });
    });
    $$("[data-cal-delete-cancel]").forEach(function (b) {
      b.addEventListener("click", function () {
        var slug = b.getAttribute("data-cal-delete-cancel");
        $('[data-cal-confirm-row="' + slug + '"]').hidden = true;
        var d = $('[data-cal-delete="' + slug + '"]'); d.setAttribute("aria-expanded", "false"); d.focus();
        d.closest("tr").classList.remove("cal-row-confirm");
      });
    });
    $$("[data-cal-delete-confirm]").forEach(function (b) {
      b.addEventListener("click", function () {
        var slug = b.getAttribute("data-cal-delete-confirm");
        b.disabled = true;
        post("calendar", { action: "delete", slug: slug }).then(function () {
          status(st, "Deleted " + slug + ".", true); reloadSoon();
        }).catch(function (e) { b.disabled = false; status(st, e.message, false); });
      });
    });
    form.addEventListener("submit", function (ev) {
      ev.preventDefault(); clearErrors();
      var f = form.elements, bad = null;
      function err(name, msg) {
        var el = f[name], e = document.getElementById(el.id + "-err");
        if (e) { e.textContent = msg; }
        el.setAttribute("aria-invalid", "true"); bad = bad || el;
      }
      if (!f.name_ar.value.trim()) { err("name_ar", "Arabic name is required."); }
      if (!f.name_en.value.trim()) { err("name_en", "English name is required."); }
      var day = parseInt(f.day.value, 10);
      if (!(day >= 1 && day <= 31)) { err("day", "Day must be 1-31."); }
      var src = f.source_url.value.trim();
      if (src && !/^https:\/\//i.test(src)) { src = "https://" + src.replace(/^http:\/\//i, ""); }
      if (src && !/^https:\/\/[^\s<>"']+\.[^\s<>"']+$/i.test(src)) { err("source_url", "This doesn't look like a web address."); }
      if (bad) { bad.focus(); return; }
      var rule = $("[data-cal-rule]:checked", form).value;
      var fields = {
        name_ar: f.name_ar.value.trim(), name_en: f.name_en.value.trim(), rule: rule,
        month: f.month.value, day: f.day.value, duration_days: f.duration_days.value || 1,
        category: f.category.value, official: f.official.checked, angle: f.angle.value, notes: f.notes.value,
        source_url: src, keywords: f.keywords.value
      };
      if (rule === "once") { fields.year = f.year.value; }
      if (f.checked_on.value) { fields.checked_on = f.checked_on.value; }
      var slug = $("[data-cal-slug]", form).value;
      submit.disabled = true; status(st, "Saving…");
      post("calendar", slug ? { action: "edit", slug: slug, fields: fields } : { action: "add", fields: fields }).then(function (j) {
        status(st, "Saved " + j.slug + ". Reloading…", true); reloadSoon();
      }).catch(function (e) { submit.disabled = false; status(st, e.message, false); });
    });
    $$("[data-cal-day]").forEach(function (el) {
      el.addEventListener("click", function () {
        var row = $('[data-cal-row="' + el.getAttribute("data-cal-day") + '"]');
        if (row) { row.scrollIntoView({ behavior: "smooth", block: "center" }); }
      });
    });
  }

  /* ---------------------------------------------------------------- accounts */
  function initAccounts() {
    var att = $("[data-acct-attach]");
    if (att) {
      att.addEventListener("submit", function (ev) {
        ev.preventDefault();
        var u = att.elements.username.value.trim().replace(/^@/, "");
        var e = $("[data-acct-user-error]"); e.textContent = "";
        if (!/^[A-Za-z0-9._]{1,30}$/.test(u)) { e.textContent = "Letters, numbers, dots and underscores only (1-30)."; att.elements.username.focus(); return; }
        var st = $("[data-acct-attach-status]"); status(st, "Attaching…");
        post("accounts", { action: "attach", username: u, role: att.elements.role.value }).then(function () {
          status(st, "Attached @" + u + ". Reloading…", true); reloadSoon();
        }).catch(function (x) { status(st, x.message, false); });
      });
    }
    $$("[data-acct-import-for]").forEach(function (b) {
      b.addEventListener("click", function () {
        var sel = $("[data-acct-import-select]"); if (sel) { sel.value = b.getAttribute("data-acct-import-for"); }
        var ta = $("#imp-paste"); if (ta) { ta.focus(); }
      });
    });
    var imp = $("[data-acct-import]");
    if (imp) {
      imp.addEventListener("submit", function (ev) {
        ev.preventDefault();
        var st = $("[data-acct-import-status]"), er = $("[data-acct-import-error]");
        er.textContent = "";
        var file = $("[data-acct-import-file]").files[0];
        var text = imp.elements.rows.value.trim();
        if (!file && !text) { er.textContent = "Paste some rows or choose a CSV file."; return; }
        function send(body, fmt) {
          status(st, "Importing…");
          post("accounts", { action: "import", username: imp.elements.username.value, text: body, format: fmt }).then(function (j) {
            var n = "Imported " + j.stored + " post" + (j.stored === 1 ? "" : "s");
            if (j.errors && j.errors.length) {
              er.textContent = j.errors.join(" · ");
              status(st, n + "; fix the rows below and import them again (reload to see the stored ones).", false);
            } else { status(st, n + ". Reloading…", true); reloadSoon(); }
          }).catch(function (x) { status(st, x.message, false); });
        }
        if (file) {
          if (file.size > 200000) { er.textContent = "CSV is too big (max 200 KB)."; return; }
          var rd = new FileReader();
          rd.onload = function () { send(String(rd.result), "csv"); };
          rd.readAsText(file);
        } else { send(text, "paste"); }
      });
    }
    $$("[data-acct-remove]").forEach(function (b) {
      b.addEventListener("click", function () {
        var c = $('[data-acct-confirm="' + b.getAttribute("data-acct-remove") + '"]');
        c.hidden = !c.hidden; b.setAttribute("aria-expanded", c.hidden ? "false" : "true");
        if (!c.hidden) { $(".confirm-text", c).focus(); }
      });
    });
    $$("[data-acct-remove-cancel]").forEach(function (b) {
      b.addEventListener("click", function () {
        var u = b.getAttribute("data-acct-remove-cancel");
        $('[data-acct-confirm="' + u + '"]').hidden = true;
        var r = $('[data-acct-remove="' + u + '"]'); r.setAttribute("aria-expanded", "false"); r.focus();
      });
    });
    $$("[data-acct-remove-confirm]").forEach(function (b) {
      b.addEventListener("click", function () {
        b.disabled = true;
        post("accounts", { action: "forget", username: b.getAttribute("data-acct-remove-confirm") }).then(reloadSoon)
          .catch(function (x) { b.disabled = false; alertText(b, x.message); });
      });
    });
  }
  function alertText(el, msg) {
    var p = document.createElement("p"); p.className = "field-error"; p.textContent = msg; el.parentNode.appendChild(p);
  }

  /* ---------------------------------------------------------------- upload */
  var TYPES = { ".mp4": "video/mp4", ".m4v": "video/x-m4v", ".mov": "video/quicktime" };
  function initUpload() {
    var input = $("[data-upload-input]");
    if (!input) { return; }
    var drop = $("[data-upload-drop]"), err = $("[data-upload-error]"), now = $("[data-upload-now]");
    var bar = $("[data-upload-progress]"), pct = $("[data-upload-pct]"), chip = $("[data-upload-state]");
    var chipText = $("[data-upload-state-text]"), name = $("[data-upload-name]"), cancel = $("[data-upload-cancel]");
    var xhr = null;
    if (!S.upload) {
      input.disabled = true; drop.classList.add("is-disabled");
      $(".upl-drop-title", drop).textContent = S.demo ? "Uploads are off in the demo" : "Uploads are off ([upload] enabled = false)";
      return;
    }
    function setState(key, text) {
      chip.className = "chip chip-sm upl-st-" + key; chipText.textContent = text;
    }
    function start(file) {
      err.textContent = "";
      var ext = (file.name.match(/\.[^.]+$/) || [""])[0].toLowerCase();
      if (S.extensions.indexOf(ext) < 0) { err.textContent = "Only " + S.extensions.join(", ") + " files."; return; }
      if (file.size > S.max_mb * 1048576) { err.textContent = "Too big: the limit is " + S.max_mb + " MB."; return; }
      now.hidden = false; name.textContent = file.name; bar.value = 0; setState("uploading", "Uploading");
      xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/upload");
      xhr.setRequestHeader("X-CSRF-Token", S.csrf);
      xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
      xhr.setRequestHeader("Content-Type", TYPES[ext]);
      xhr.upload.onprogress = function (e) {
        if (!e.lengthComputable) { return; }
        var p = Math.floor(100 * e.loaded / e.total);
        bar.value = p; pct.textContent = p + "% · " + mb(e.loaded) + " of " + mb(e.total) + " MB";
        if (e.loaded >= e.total) { bar.removeAttribute("value"); setState("checking", "Checking"); }
      };
      xhr.onload = function () {
        var j = {}; try { j = JSON.parse(xhr.responseText); } catch (x) { j = { error: "HTTP " + xhr.status }; }
        xhr = null; bar.value = 100;
        if (j.error || !j.status) { setState("error", "Error"); err.textContent = j.error || "Upload failed."; return; }
        if (j.status === "duplicate") {
          setState("duplicate", j.reel ? "Duplicate of reel #" + j.reel : "Duplicate of an earlier upload");
          pct.textContent = "Not processed again.";
        } else {
          setState("queued", "Queued for intake"); pct.textContent = "Saved as " + j.name + ". The next ticks take it in and run every QA gate.";
        }
        setTimeout(function () { window.location.reload(); }, 2500);
      };
      xhr.onerror = function () { xhr = null; setState("error", "Error"); err.textContent = "Connection lost; try again."; };
      xhr.onabort = function () { xhr = null; now.hidden = true; input.value = ""; input.focus(); };
      xhr.send(file);
    }
    input.addEventListener("change", function () { if (input.files[0]) { start(input.files[0]); } });
    cancel.addEventListener("click", function () { if (xhr) { xhr.abort(); } else { now.hidden = true; } });
    ["dragenter", "dragover"].forEach(function (t) { drop.addEventListener(t, function (e) { e.preventDefault(); drop.classList.add("is-over"); }); });
    ["dragleave", "drop"].forEach(function (t) { drop.addEventListener(t, function (e) { e.preventDefault(); drop.classList.remove("is-over"); }); });
    drop.addEventListener("drop", function (e) { if (e.dataTransfer.files[0]) { start(e.dataTransfer.files[0]); } });
  }

  /* ---------------------------------------------------------------- link account dialog
   * One <dialog> in base.html: attach + import + recommendations in one go, then poll recommend_status
   * every 2 s until the server (which rebuilds the site first) says done, and open accounts.html#rec-<user>.
   * Closing the dialog only stops the polling; the server job keeps running (resumable via sessionStorage).
   * Text goes into the DOM via textContent only. */
  var LINK_SAY = {
    attach: "Step 1 of 3: attaching the account.",
    recommend: "Step 3 of 3: writing recommendations. This can take up to 2 minutes.",
    done: "Done. Opening the plan…"
  };
  var LINK_SR = { todo: ", waiting", active: ", in progress", done: ", done", skipped: ", skipped", error: ", failed" };
  var LINK_KEY = "reel-link-job";

  function remember(job) {
    try {
      if (job) { sessionStorage.setItem(LINK_KEY, JSON.stringify({ username: job.username, payload: null, started: job.started })); }
      else { sessionStorage.removeItem(LINK_KEY); }
    } catch (x) { /* private window: no resume, nothing else changes */ }
  }
  function recalled() {
    try { return JSON.parse(sessionStorage.getItem(LINK_KEY) || "null"); } catch (x) { return null; }
  }

  /* The Accounts page is rebuilt by the server before it reports "done", so always load it fresh. */
  function linkGo(url, hash) {
    if (/accounts\.html$/.test(location.pathname)) {
      /* a reload restores the old scroll position, which would win over the jump to the plan */
      try { history.scrollRestoration = "manual"; } catch (x) { /* old browser: focus still moves */ }
      location.hash = hash; location.reload();
    }
    else { location.assign(url); }
  }

  function initLink() {
    var dlg = $("[data-link-dialog]");
    if (!dlg || typeof dlg.showModal !== "function") { return; }
    var form = $("[data-link-form]", dlg), fields = $("[data-link-fields]", dlg), run = $("[data-link-run]", dlg);
    var runH = $("[data-link-run-h]", dlg), runUser = $("[data-link-run-user]", dlg), elapsed = $("[data-link-elapsed]", dlg);
    var slow = $("[data-link-slow]", dlg), keepNote = $("[data-link-keep]", dlg), bar = $("[data-link-bar]", dlg), st = $("[data-link-status]", dlg);
    var errBox = $("[data-link-error]", dlg), errText = $("[data-link-error-text]", dlg);
    var submit = $("[data-link-submit]", dlg), cancel = $("[data-link-cancel]", dlg);
    var niche = form.elements.niche, count = $("[data-link-count]", dlg), more = $("[data-link-more]", dlg);
    var job = null, opener = null, tick = null;

    function setStep(name, state, note) {
      var li = $('[data-link-step="' + name + '"]', dlg);
      li.setAttribute("data-state", state);
      $("[data-link-step-sr]", li).textContent = LINK_SR[state];
      $("[data-link-step-note]", li).textContent = note || "";
    }
    function clearErrors() {
      $$(".field-error", form).forEach(function (e) { e.textContent = ""; });
      $$("[aria-invalid]", form).forEach(function (e) { e.removeAttribute("aria-invalid"); });
      errBox.hidden = true; errText.textContent = "";
    }
    function fieldErr(el, msg) {
      var e = document.getElementById(el.id + "-err");
      if (e) { e.textContent = msg; }
      el.setAttribute("aria-invalid", "true");
      return el;
    }
    function generalErr(msg) { errText.textContent = msg; errBox.hidden = false; errBox.focus(); }
    function updateCount() {
      var n = niche.value.length;
      count.textContent = n + " / 200";
      count.classList.toggle("link-count-near", n >= 180);
    }
    function showForm() {
      fields.hidden = false; fields.disabled = false; run.hidden = true;
      submit.hidden = false; submit.disabled = false; submit.textContent = "Link and analyse";
      cancel.textContent = "Cancel";
      stopTick();
    }
    function showRun(username) {
      fields.hidden = true; run.hidden = false; submit.hidden = true;
      cancel.textContent = "Close";
      runUser.textContent = "@" + username;
      bar.hidden = false; keepNote.hidden = false;
      runH.focus();
      startTick();
    }
    function startTick() {
      stopTick();
      var paint = function () {
        if (!job) { return; }
        var s = Math.floor((Date.now() - job.started) / 1000);
        elapsed.textContent = Math.floor(s / 60) + ":" + ("0" + (s % 60)).slice(-2);
        slow.hidden = s < 150;
      };
      paint(); tick = setInterval(paint, 1000);
    }
    function stopTick() { if (tick) { clearInterval(tick); tick = null; } }

    function open(ev) {
      opener = ev && ev.currentTarget || document.activeElement;
      dlg.showModal();
      var mem = !job && recalled();
      if (mem && mem.username) {               /* a job started on another page: resume watching it */
        job = { username: mem.username, payload: null, started: mem.started || Date.now(), phase: "running", fails: 0, t: null };
        showRun(job.username);
        setStep("attach", "done"); setStep("import", "done"); setStep("recommend", "active");
        status(st, LINK_SAY.recommend); poll();
      } else if (job && job.phase === "running") {
        showRun(job.username); poll();
      } else if (!job) {
        showForm(); updateCount();
        form.elements.username.focus();
      }
    }
    function done() {
      job.phase = "done"; remember(null); stopTick(); bar.hidden = true; keepNote.hidden = true; slow.hidden = true;
      setStep("recommend", "done");
      status(st, LINK_SAY.done, true);
      var root = document.body.getAttribute("data-root") || "";
      var hash = "#rec-" + job.username;
      setTimeout(function () { linkGo(root + "accounts.html" + hash, hash); }, 700);
    }
    function fail(msg) {
      job.phase = "error"; remember(null); stopTick(); bar.hidden = true; keepNote.hidden = true; slow.hidden = true;
      setStep("recommend", "error");
      status(st, "Writing recommendations failed.", false);
      if (job.payload) { submit.hidden = false; submit.disabled = false; submit.textContent = "Try again"; }
      generalErr(msg + " The account is attached; only the plan is missing.");
    }
    function poll() {
      if (!job || job.phase !== "running" || job.t || !dlg.open) { return; }
      job.t = setTimeout(function () {
        job.t = null;
        if (!dlg.open) { return; }
        post("accounts", { action: "recommend_status", username: job.username }).then(function (j) {
          job.fails = 0;
          if (j.state === "done") { done(); }
          else if (j.state === "error") { fail(j.error || "The analysis stopped."); }
          /* "idle" = the server has no job for this account (e.g. it restarted): stop instead of polling forever */
          else if (j.state === "idle") { fail("No linking job is running for this account (the dashboard may have restarted)."); }
          else { poll(); }
        }).catch(function (x) {
          job.fails += 1;
          if (job.fails >= 3) { fail("Lost contact with the dashboard server (" + x.message + "). The job may still finish: reload the Accounts tab in a minute."); }
          else { poll(); }
        });
      }, 2000);
    }
    function start(payload) {
      clearErrors();
      job = { username: payload.username, payload: payload, started: Date.now(), phase: "running", fails: 0, t: null };
      remember(job);
      showRun(payload.username);
      setStep("attach", "active"); setStep("import", "todo"); setStep("recommend", "todo");
      status(st, LINK_SAY.attach);
      post("accounts", payload).then(function (j) {
        setStep("attach", "done");
        if (payload.text) {
          var note = typeof j.stored === "number" ? j.stored + " reel" + (j.stored === 1 ? "" : "s") + " stored" : "";
          var bad = j.errors && j.errors.length;
          if (bad) { note += (note ? " · " : "") + bad + " row" + (bad === 1 ? "" : "s") + " skipped (" + j.errors[0] + ")"; }
          setStep("import", "done", note);
        }
        else { setStep("import", "skipped", "No reels added: the plan uses your description"); }
        setStep("recommend", "active");
        status(st, LINK_SAY.recommend);
        poll();
      }).catch(function (x) {
        job = null; remember(null);
        showForm();
        var el = x.field && form.elements[x.field];
        if (el && el.id && document.getElementById(el.id + "-err")) { if (more && more.contains(el)) { more.open = true; } fieldErr(el, x.message); el.focus(); }
        else { generalErr(x.message); }
        status(st, "Not linked.", false);
      });
    }

    $$("[data-link-open]").forEach(function (b) { b.addEventListener("click", open); });
    $$("[data-link-close]", dlg).forEach(function (b) { b.addEventListener("click", function () { dlg.close(); }); });
    dlg.addEventListener("close", function () {
      stopTick();
      if (job && job.t) { clearTimeout(job.t); job.t = null; }   /* stop asking; the server job keeps running */
      if (job && job.phase !== "running") { job = null; showForm(); clearErrors(); status(st, ""); }
      if (opener && opener.focus) { opener.focus(); }
    });
    niche.addEventListener("input", updateCount);

    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (job && job.phase === "error" && job.payload) { start(job.payload); return; }
      clearErrors();
      var f = form.elements, bad = null;
      var flag = function (el, msg) { fieldErr(el, msg); bad = bad || el; };   /* show every error, focus the first */
      var u = f.username.value.trim().replace(/^@/, "");
      var n = niche.value.trim();
      var text = f.text.value.trim(), file = f.file.files[0];
      if (!/^[A-Za-z0-9._]{1,30}$/.test(u)) { flag(f.username, "Letters, numbers, dots and underscores only (1-30)."); }
      if (n.length > 200) { flag(niche, "Keep it under 200 characters."); }
      if (text && file) { flag(f.text, "Choose one: paste rows or a CSV file, not both."); }
      if (file && !/\.csv$/i.test(file.name)) { flag(f.file, "Only .csv files."); }
      if (file && file.size > 200000) { flag(f.file, "CSV is too big (max 200 KB)."); }
      if (!n && !text && !file) { flag(niche, "Describe the account in a line, or add some reels below."); }
      if (bad) { if (more && more.contains(bad)) { more.open = true; } bad.focus(); return; }
      var payload = { action: "link", username: u, role: f.role.value, niche: n, text: text, format: text ? "paste" : "" };
      if (!file) { start(payload); return; }
      var rd = new FileReader();
      rd.onload = function () { payload.text = String(rd.result); payload.format = "csv"; start(payload); };
      rd.onerror = function () { fieldErr(f.file, "Couldn't read that file."); f.file.focus(); };
      rd.readAsText(file);
    });
  }

  /* After the redirect to accounts.html#rec-<username>: move keyboard/screen-reader focus to the card. */
  function focusRecTarget() {
    if (!/^#rec-[A-Za-z0-9._]{1,30}$/.test(location.hash)) { return; }
    var card = document.getElementById(location.hash.slice(1)), h = document.getElementById(location.hash.slice(1) + "-h");
    /* after location.reload() the browser restores the old scroll position, so jump to the card explicitly */
    if (card) { card.scrollIntoView({ block: "start" }); }
    if (h) { h.focus({ preventScroll: true }); }
    try { history.scrollRestoration = "auto"; } catch (x) { /* nothing to undo */ }
  }

  /* ---------------------------------------------------------------- boot */
  /* read-only views too: land on the plan after the link dialog's redirect. After "load" (plus a tick), so the
   * browser's own fragment scroll and scroll restoration have run and can't undo the focus or the jump. */
  window.addEventListener("load", function () { setTimeout(focusRecTarget, 0); });
  if (location.protocol !== "http:") { return; }
  fetch("/api/session", { credentials: "same-origin", cache: "no-store" }).then(function (r) {
    return r.ok ? r.json() : null;
  }).then(function (j) {
    if (!j || !j.writes || !j.csrf) { return; }
    S = j;
    document.body.setAttribute("data-writes", "on");
    initCopy(); initCalendar(); initAccounts(); initUpload(); initLink();
  }).catch(function () { /* read-only server: stay static */ });
})();
