const fs = require("node:fs");
const vm = require("node:vm");

class FakeNode {
  constructor() {
    this.dataset = {};
    this.listeners = {};
    this.children = [];
    this.value = "";
    this.disabled = false;
    this.textContent = "";
    this.style = {};
    this.scrollHeight = 0;
  }

  addEventListener(kind, callback) {
    this.listeners[kind] = callback;
  }

  append(...children) {
    this.children.push(...children);
  }

  replaceChildren(...children) {
    this.children = [...children];
  }

  remove() {}

  get firstElementChild() {
    return this.children[0] || null;
  }
}

async function main() {
  const nodes = Object.fromEntries(
    [
      "form-error", "message-form", "message-text", "send-button",
      "character-count", "job-status", "messages", "quota-reset",
      "quota", "visitor-status", "logout",
    ].map((id) => [id, new FakeNode()]),
  );
  const document = {
    body: { dataset: { page: "chat" } },
    hidden: false,
    getElementById: (id) => nodes[id],
    createElement: () => new FakeNode(),
    addEventListener: () => undefined,
  };
  let fetchCount = 0;
  const window = {
    LOUNGE_STATE: {
      visitor_status: "active",
      visitor_name: "Visitor",
      host_name: "Host",
      quota: { remaining: 3, limit: 3, reset_at: null },
      job: null,
    },
    clearInterval: () => undefined,
    setInterval: () => 1,
  };
  const context = {
    document,
    window,
    fetch: async () => {
      fetchCount += 1;
      return { ok: true, json: async () => ({}) };
    },
    crypto: { randomUUID: () => "request-id" },
    EventSource: class {},
    location: { reload: () => undefined },
    console,
  };
  vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), context);

  nodes["message-text"].value = "🧬".repeat(150);
  nodes["message-text"].listeners.input();
  if (nodes["character-count"].textContent !== "150") {
    throw new Error(`150 emoji counted as ${nodes["character-count"].textContent}`);
  }
  if (nodes["send-button"].disabled) {
    throw new Error("150 emoji unexpectedly disabled send");
  }

  nodes["message-text"].value = `${"a".repeat(500)}🧬`;
  nodes["message-text"].listeners.input();
  if (nodes["character-count"].textContent !== "501") {
    throw new Error(`501 code points counted as ${nodes["character-count"].textContent}`);
  }
  if (!nodes["send-button"].disabled) {
    throw new Error("501 code points did not disable send");
  }
  if (!nodes["form-error"].textContent.includes("500")) {
    throw new Error("501 code points did not show the limit hint");
  }
  await nodes["message-form"].listeners.submit({ preventDefault() {} });
  if (fetchCount !== 0) {
    throw new Error("over-limit input reached the API");
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
