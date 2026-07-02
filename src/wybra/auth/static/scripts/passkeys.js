const PASSKEY_VERIFICATION_FAILED = "Passkey verification failed.";

function base64urlToBuffer(value) {
  const padding = "=".repeat((4 - (value.length % 4)) % 4);
  const base64 = (value + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = window.atob(base64);
  const bytes = new Uint8Array(raw.length);
  for (let index = 0; index < raw.length; index += 1) {
    bytes[index] = raw.charCodeAt(index);
  }
  return bytes.buffer;
}

function bufferToBase64url(buffer) {
  const bytes = new Uint8Array(buffer);
  let raw = "";
  for (const byte of bytes) {
    raw += String.fromCharCode(byte);
  }
  return window.btoa(raw).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function prepareCreationOptions(publicKey) {
  const options = { ...publicKey };
  options.challenge = base64urlToBuffer(options.challenge);
  options.user = { ...options.user, id: base64urlToBuffer(options.user.id) };
  if (options.excludeCredentials) {
    options.excludeCredentials = options.excludeCredentials.map((credential) => ({
      ...credential,
      id: base64urlToBuffer(credential.id),
    }));
  }
  return options;
}

function prepareRequestOptions(publicKey) {
  const options = { ...publicKey };
  options.challenge = base64urlToBuffer(options.challenge);
  if (options.allowCredentials) {
    options.allowCredentials = options.allowCredentials.map((credential) => ({
      ...credential,
      id: base64urlToBuffer(credential.id),
    }));
  }
  return options;
}

function credentialToJSON(credential) {
  const response = {};
  for (const key of [
    "attestationObject",
    "authenticatorData",
    "clientDataJSON",
    "signature",
    "userHandle",
  ]) {
    if (credential.response[key]) {
      response[key] = bufferToBase64url(credential.response[key]);
    }
  }
  if (typeof credential.response.getTransports === "function") {
    response.transports = credential.response.getTransports();
  }
  return {
    id: credential.id,
    rawId: bufferToBase64url(credential.rawId),
    response,
    type: credential.type,
    authenticatorAttachment: credential.authenticatorAttachment || null,
    clientExtensionResults: credential.getClientExtensionResults(),
  };
}

async function postJSON(path, csrfHeader, csrfToken, body) {
  const response = await window.fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "content-type": "application/json",
      [csrfHeader]: csrfToken,
    },
    body: JSON.stringify(body),
  });
  let payload;
  try {
    payload = await response.json();
  } catch (error) {
    if (response.ok) {
      throw error;
    }
    payload = { error: PASSKEY_VERIFICATION_FAILED };
  }
  if (!response.ok) {
    throw new Error(payload.error || PASSKEY_VERIFICATION_FAILED);
  }
  return payload;
}

function csrfTokenFrom(form) {
  const input = form.querySelector('input[name="csrf_token"]');
  return input ? input.value : "";
}

function setMessage(container, message) {
  if (container) {
    container.textContent = message;
  }
}

async function registerPasskey(form) {
  const message = form.querySelector("[data-passkey-message]");
  const button = form.querySelector("[data-passkey-register-button]");
  if (!window.PublicKeyCredential) {
    setMessage(message, "Passkeys are not supported by this browser.");
    return;
  }
  button.disabled = true;
  setMessage(message, "");
  try {
    const csrfToken = csrfTokenFrom(form);
    const csrfHeader = form.dataset.csrfHeader;
    const options = await postJSON(form.dataset.optionsPath, csrfHeader, csrfToken, {});
    const credential = await navigator.credentials.create({
      publicKey: prepareCreationOptions(options.publicKey),
    });
    if (!credential) {
      throw new Error("Passkey registration was cancelled.");
    }
    const labelInput = form.querySelector('input[name="label"]');
    const complete = await postJSON(
      form.dataset.completePath,
      csrfHeader,
      csrfToken,
      {
        challenge_id: options.challenge_id,
        credential: credentialToJSON(credential),
        label: labelInput ? labelInput.value : "",
      },
    );
    window.location.assign(complete.redirect_to || window.location.pathname);
  } catch (error) {
    setMessage(message, error.message || "Passkey registration failed.");
  } finally {
    button.disabled = false;
  }
}

async function loginWithPasskey(button) {
  const form = document.querySelector('form[action*="/login"]');
  const section = button.closest("section");
  const message = section ? section.querySelector("[data-passkey-message]") : null;
  if (!window.PublicKeyCredential) {
    setMessage(message, "Passkeys are not supported by this browser.");
    return;
  }
  const emailInput = form ? form.querySelector('input[name="email"]') : null;
  const email = emailInput ? emailInput.value.trim() : "";
  if (!email) {
    if (emailInput) {
      emailInput.focus();
    }
    setMessage(message, "Enter your email before using a passkey.");
    return;
  }
  button.disabled = true;
  setMessage(message, "");
  try {
    const csrfToken = form ? csrfTokenFrom(form) : button.dataset.csrfToken;
    const csrfHeader = button.dataset.csrfHeader;
    const returnTo = button.dataset.returnTo || "/";
    const options = await postJSON(button.dataset.optionsPath, csrfHeader, csrfToken, {
      email,
      return_to: returnTo,
    });
    const credential = await navigator.credentials.get({
      publicKey: prepareRequestOptions(options.publicKey),
    });
    if (!credential) {
      throw new Error("Passkey sign-in was cancelled.");
    }
    const complete = await postJSON(button.dataset.completePath, csrfHeader, csrfToken, {
      challenge_id: options.challenge_id,
      credential: credentialToJSON(credential),
      return_to: returnTo,
    });
    window.location.assign(complete.redirect_to || returnTo);
  } catch (error) {
    setMessage(message, error.message || "Passkey sign-in failed.");
  } finally {
    button.disabled = false;
  }
}

for (const form of document.querySelectorAll("[data-passkey-register]")) {
  const button = form.querySelector("[data-passkey-register-button]");
  if (button) {
    button.addEventListener("click", () => {
      void registerPasskey(form);
    });
  }
}

for (const button of document.querySelectorAll("[data-passkey-login]")) {
  button.addEventListener("click", () => {
    void loginWithPasskey(button);
  });
}
