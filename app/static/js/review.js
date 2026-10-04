/**
 * Страница задачи: опрос статуса, отображение Diff View, утверждение.
 */
(function () {
  "use strict";

  const root = document.getElementById("review-root");
  const jobId = root.dataset.jobId;
  const $ = (id) => document.getElementById(id);
  const STATES = ["processing", "failed", "review"];
  const tr = DiffView.translate; // переводы из window.I18N (см. diff.js)
  let working = null; // редактируемая копия черновика
  let job = null;

  function showState(name) {
    for (const state of STATES) $(`state-${state}`).classList.toggle("hidden", state !== name);
  }

  function setStatusBadge(status) {
    const badge = $("job-status");
    badge.textContent = tr(`status.${status}`);
    badge.className = `status-badge status-${status}`;
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    if (response.status === 401) {
      window.location.href = `/login?next=${encodeURIComponent(window.location.pathname)}`;
      throw new Error("unauthorized");
    }
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(formatError(body.detail) || tr("js.error_status", { status: response.status }));
    return body;
  }

  function formatError(detail) {
    if (!detail) return "";
    if (typeof detail === "string") return detail;
    // Ошибки валидации FastAPI: [{loc: [...], msg: "..."}]
    return detail.map((d) => `${(d.loc || []).slice(2).join(".")}: ${d.msg}`).join("; ");
  }

  function renderIssues(container, issues) {
    container.replaceChildren();
    for (const issue of issues) {
      const div = document.createElement("div");
      div.className = `diff-issue diff-issue-${issue.severity}`;
      const where = [issue.item_no != null ? tr("js.item_no", { n: issue.item_no }) : "", issue.field || ""]
        .filter(Boolean);
      div.textContent = (where.length ? `${where.join(", ")}: ` : "") + issue.message;
      container.append(div);
    }
  }

  function renderReview() {
    showState("review");
    // Если данные уже утверждались — показываем утверждённую версию.
    working = structuredClone(job.approved || job.proposed);
    DiffView.renderDiffView($("diff-container"), {
      reference: job.reference,
      proposed: working,
      issues: job.issues,
      summaryContainer: $("diff-summary"),
      editable: true,
      onValidityChange: (hasInvalid) => {
        $("approve-btn").disabled = hasInvalid;
        $("approve-hint").textContent = hasInvalid
          ? tr("js.invalid_numbers")
          : tr("ui.rev.approve_hint");
      },
    });
    if (job.status === "approved" || job.status === "exported") $("state-approved").classList.remove("hidden");
  }

  async function poll() {
    try {
      job = await api(`/api/jobs/${jobId}`);
    } catch (error) {
      setTimeout(poll, 5000);
      return;
    }
    setStatusBadge(job.status);
    if (job.status === "processing") {
      showState("processing");
      setTimeout(poll, 2000);
    } else if (job.status === "failed") {
      showState("failed");
      $("error-message").textContent = job.error_message || tr("js.unknown_error");
    } else {
      renderReview();
    }
  }

  $("approve-btn").addEventListener("click", async () => {
    const errors = (job.issues || []).filter((i) => i.severity === "error").length;
    const question = tr("js.confirm_errors", { n: errors });
    if (errors && !confirm(question)) {
      return;
    }
    const button = $("approve-btn");
    button.disabled = true;
    try {
      const result = await api(`/api/jobs/${jobId}/approve`, { method: "POST", body: JSON.stringify({ data: working }) });
      job.status = result.status;
      job.approved = structuredClone(working);
      setStatusBadge(result.status);
      renderIssues($("approve-issues"), result.issues);
      $("state-approved").classList.remove("hidden");
      $("state-approved").scrollIntoView({ behavior: "smooth" });
    } catch (error) {
      alert(tr("js.save_failed", { error: error.message }));
    } finally {
      button.disabled = false;
    }
  });

  $("retry-btn").addEventListener("click", async () => {
    try {
      await api(`/api/jobs/${jobId}/retry`, { method: "POST" });
      poll();
    } catch (error) {
      alert(error.message);
    }
  });

  $("only-changes").addEventListener("change", (event) => {
    $("diff-container").classList.toggle("only-changes", event.target.checked);
  });

  poll();
})();
