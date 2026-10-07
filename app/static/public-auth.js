/** Session-aware chrome for public/marketing pages. */
(function () {
  function token() {
    try {
      return localStorage.getItem("sharesquad_token") || localStorage.getItem("billbuddy_token");
    } catch (_) {
      return null;
    }
  }

  function upgrade() {
    if (!token()) return;
    const actions = document.querySelector(".landing-account-actions, .public-header .flex.gap-2, .public-header");
    const loginLinks = document.querySelectorAll(
      'a[href="/login.html"], a[href="/login.html?mode=signup"], a.landing-login, #signupLink, #heroSignup'
    );
    loginLinks.forEach((link) => {
      link.href = "/app";
      if (link.id === "signupLink" || link.id === "heroSignup" || /sign up|create (a )?free account|create your account/i.test(link.textContent || "")) {
        if (link.querySelector(".signup-full")) {
          link.innerHTML = '<span class="signup-full">Open my dashboard</span><span class="signup-short">Open app</span>';
        } else {
          link.textContent = "Open my dashboard";
        }
      } else {
        link.textContent = "Open app";
      }
      link.classList.remove("landing-login");
      if (link.classList.contains("bb-btn-secondary") && !link.classList.contains("bb-btn-primary")) {
        // Keep secondary for "Open app" beside primary when both exist; landing script may collapse.
      }
    });

    // Prefer a single primary CTA where Log in + Sign up both became "Open…".
    document.querySelectorAll(".public-header, .landing-account-actions").forEach((header) => {
      const ctas = [...header.querySelectorAll("a.bb-btn-primary, a.bb-btn-secondary, a.landing-login")].filter((a) =>
        /open (app|my dashboard)/i.test(a.textContent || "") || a.querySelector(".signup-full")
      );
      if (ctas.length > 1) {
        ctas.slice(1).forEach((el) => el.classList.add("hidden"));
        ctas[0].classList.add("bb-btn-primary");
        ctas[0].classList.remove("bb-btn-secondary", "landing-login");
        if (!ctas[0].querySelector(".signup-full") && /open app/i.test(ctas[0].textContent || "")) {
          ctas[0].textContent = "Open app";
        }
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", upgrade);
  } else {
    upgrade();
  }
})();
