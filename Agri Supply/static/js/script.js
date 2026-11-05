// Mobile navigation and small shared frontend interactions.
document.addEventListener("DOMContentLoaded", () => {
  const toggle = document.querySelector(".menu-toggle");
  const nav = document.querySelector(".main-nav");

  if (toggle && nav) {
    toggle.addEventListener("click", () => {
      const isOpen = nav.classList.toggle("open");
      toggle.setAttribute("aria-expanded", String(isOpen));
      toggle.textContent = isOpen ? "✕" : "☰";
    });
  }

  document.querySelectorAll(".password-toggle").forEach((button) => {
    button.addEventListener("click", () => {
      const inputId = button.getAttribute("aria-controls");
      const password = inputId ? document.getElementById(inputId) : null;
      if (!password) return;

      const isVisible = password.type === "text";
      password.type = isVisible ? "password" : "text";
      button.textContent = isVisible ? "Show" : "Hide";
      button.setAttribute("aria-label", `${isVisible ? "Show" : "Hide"} password`);
      button.setAttribute("aria-pressed", String(!isVisible));
    });
  });

  // Automatically dismiss flash messages after a short delay.
  document.querySelectorAll(".flash").forEach((message) => {
    window.setTimeout(() => {
      message.style.transition = "opacity .4s ease";
      message.style.opacity = "0";
      window.setTimeout(() => message.remove(), 450);
    }, 5000);
  });
});
