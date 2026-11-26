"use strict";

function createAccountsView({api, element, sessionExpired, accessDenied, basePath}) {
  const path = basePath + "/accounts";
  const get = (id) => document.getElementById(id);
  const form = get("accountForm");
  const rows = get("accountsRows");
  const username = get("accountUsername");
  const password = get("accountPassword");
  const confirmation = get("accountConfirm");
  const title = get("accountFormTitle");
  const submit = get("accountSubmit");
  const cancel = get("accountCancel");
  const status = get("accountStatus");
  const usernameHint = get("accountUsernameHint");
  const passwordHint = get("accountPasswordHint");
  let generation = 0;
  let target = null;
  let busy = false;

  function edit(name = null) {
    target = name;
    form.reset();
    confirmation.setCustomValidity("");
    username.value = name ?? "";
    username.readOnly = name !== null;
    usernameHint.hidden = name !== null;
    title.textContent = name === null ? "Create account" : "Reset password";
    submit.textContent = title.textContent;
    passwordHint.textContent = name === null ? "Use 12–128 characters." :
      "Use 12–128 characters. Resetting the password signs this account out.";
    cancel.hidden = name === null;
  }

  function setBusy(value) {
    busy = value;
    submit.disabled = value;
    cancel.disabled = value;
    for (const button of rows.querySelectorAll("button")) button.disabled = value;
  }

  async function refresh(message = "") {
    if (busy) return;
    const request = ++generation;
    setBusy(true);
    status.textContent = message || "Loading accounts…";
    const result = await api(path);
    if (request !== generation) return;
    setBusy(false);
    if (result.status === 401) {
      sessionExpired();
      return;
    }
    if (result.status === 403) {
      accessDenied();
      return;
    }
    if (result.status !== 200 || !Array.isArray(result.payload?.accounts)) {
      status.textContent = "Could not load accounts. Try Refresh.";
      return;
    }
    rows.replaceChildren();
    for (const account of result.payload.accounts) {
      const row = element("tr");
      const cell = element("td");
      const actions = element("div", "account-actions");
      cell.append(actions);
      const name = element("th", null, account.username);
      name.scope = "row";
      if (account.can_reset_password) {
        actions.append(action("Reset password", account.username, () => {
          edit(account.username);
          password.focus();
        }));
      }
      if (account.can_disable) {
        actions.append(action("Disable", account.username, () => {
          if (!window.confirm(`Disable ${account.username}? This will end sign-in and Agent access.`)) return;
          void write(path + "/" + encodeURIComponent(account.username) + "/disable", undefined,
            `${account.username} was disabled.`);
        }));
      }
      row.append(name, element("td", null, account.disabled ? "Disabled" : "Active"), cell);
      rows.append(row);
    }
    status.textContent = message;
  }

  function action(label, name, run) {
    const button = element("button", null, label);
    button.type = "button";
    button.setAttribute("aria-label", label + " for " + name);
    button.addEventListener("click", () => { if (!busy) run(); });
    return button;
  }

  async function write(url, body, success) {
    if (busy) return;
    const request = ++generation;
    setBusy(true);
    status.textContent = "Saving…";
    const result = await api(url, {method: "POST", body});
    if (request !== generation) return;
    setBusy(false);
    if (result.status === 401) {
      sessionExpired();
      return;
    }
    if (result.status === 200 || result.status === 201) {
      edit();
      await refresh(success);
    } else if (result.status === 403) {
      edit();
      await refresh("This account change is no longer allowed.");
    } else {
      status.textContent = ({
        409: "The username already exists or the account is disabled.",
        422: "Check the username and password requirements.",
      })[result.status] || "Could not confirm the account change. Refresh to check its status.";
    }
  }

  confirmation.addEventListener("input", () => confirmation.setCustomValidity(""));
  password.addEventListener("input", () => confirmation.setCustomValidity(""));
  cancel.addEventListener("click", () => edit());
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (busy) return;
    if (password.value !== confirmation.value) {
      confirmation.setCustomValidity("The passwords must match.");
      confirmation.reportValidity();
      return;
    }
    const name = target ?? username.value;
    const body = target === null ? {username: name, password: password.value} : {password: password.value};
    const url = target === null ? path : path + "/" + encodeURIComponent(name) + "/password";
    const message = target === null ? `Created ${name}.` : `Password reset for ${name}.`;
    password.value = "";
    confirmation.value = "";
    void write(url, body, message);
  });

  return {
    refresh,
    close() {
      generation += 1;
      rows.replaceChildren();
      status.textContent = "";
      edit();
      setBusy(false);
    },
  };
}
