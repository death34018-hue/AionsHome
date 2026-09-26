(() => {
  const form = document.getElementById("unlock-form");
  form.addEventListener("submit", async event => {
    event.preventDefault();
    form.querySelector(".form-error").textContent = "";
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      const response = await fetch("/api/lounge-board/unlock", {
        method: "POST", credentials: "same-origin",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({code: form.elements.code.value}),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "解锁失败");
      form.elements.code.value = "";
      location.reload();
    } catch (error) {
      form.querySelector(".form-error").textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });
})();
