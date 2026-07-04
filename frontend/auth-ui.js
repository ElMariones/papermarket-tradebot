// auth-ui.js — shared auth state, topbar login widget and the spectator modal.
// Loaded by every page BEFORE its page script (app.js / markets.js).
//
// Everyone can SEE everything; only the right account can TOUCH things.
// window.AUTH holds {user: {id, username, role, portfolio} | null}.

window.AUTH = { user: null, ready: false };

// Must match the backend copy exactly — same message everywhere, on purpose.
const SPECTATOR_MSG =
  "You're viewing this as a spectator. Want your own portfolio? " +
  "Email mariolandaburuclares@gmail.com";

async function loadAuth() {
  try {
    const r = await fetch("/api/auth/me");
    const d = await r.json();
    AUTH.user = d.user || null;
  } catch (e) {
    AUTH.user = null;
  }
  AUTH.ready = true;
  renderAuthWidget();
  document.dispatchEvent(new CustomEvent("auth-ready"));
}

function renderAuthWidget() {
  const el = document.getElementById("authWidget");
  if (!el) return;
  if (AUTH.user) {
    el.innerHTML =
      `<span class="auth-name" title="Signed in as ${AUTH.user.username} (${AUTH.user.role})">` +
      `${AUTH.user.username}<span class="auth-role">${AUTH.user.role}</span></span>` +
      `<button id="btnLogout" class="btn btn-mini">Sign out</button>`;
    document.getElementById("btnLogout").onclick = async () => {
      await fetch("/api/auth/logout", { method: "POST" });
      location.reload();
    };
  } else {
    el.innerHTML = `<a href="/login" class="btn btn-mini btn-accent auth-link">Sign in</a>`;
  }
}

// One modal, injected once, reused by every locked control on every page.
function showSpectatorModal() {
  let overlay = document.getElementById("spectatorModal");
  if (!overlay) {
    overlay = document.createElement("div");
    overlay.id = "spectatorModal";
    overlay.className = "modal-overlay";
    overlay.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true">
        <div class="modal-eye">👁</div>
        <h3>Spectator mode</h3>
        <p>${SPECTATOR_MSG.replace(
          "mariolandaburuclares@gmail.com",
          '<a href="mailto:mariolandaburuclares@gmail.com">mariolandaburuclares@gmail.com</a>')}</p>
        <div class="modal-actions">
          ${window.AUTH && AUTH.user ? "" :
            '<a href="/login" class="btn btn-accent">Sign in</a>'}
          <button class="btn" id="spectatorClose">Keep watching</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    overlay.onclick = (e) => { if (e.target === overlay) overlay.classList.remove("open"); };
    overlay.querySelector("#spectatorClose").onclick = () =>
      overlay.classList.remove("open");
  }
  overlay.classList.add("open");
}

loadAuth();
