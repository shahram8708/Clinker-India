(() => {
  document.addEventListener("DOMContentLoaded", () => {
    const launcher = document.getElementById("aiChatLauncher");
    const panel = document.getElementById("aiChatPanel");
    const closeBtn = document.getElementById("aiChatClose");
    const messagesEl = document.getElementById("aiChatMessages");
    const inputEl = document.getElementById("aiChatInput");
    const sendBtn = document.getElementById("aiChatSend");
    const config = window.aiChatConfig || {};

    if (!launcher || !panel || !messagesEl || !inputEl || !sendBtn) {
      return;
    }

    let isOpen = false;
    let isSending = false;
    let typingEl = null;
    const history = [
      {
        role: "model",
        content:
          `Hi there! I'm the Clinker India AI copilot. Ask me anything about this page.`,
      },
    ];

    const scrollToBottom = () => {
      // Always keep the latest message in view, even if content overflows
      requestAnimationFrame(() => {
        messagesEl.scrollTo({ top: messagesEl.scrollHeight, behavior: "smooth" });
      });
    };

    const sanitizeAndRender = (text) => {
      if (typeof marked !== "undefined" && typeof DOMPurify !== "undefined") {
        return DOMPurify.sanitize(marked.parse(text));
      }
      return text;
    };

    const renderMessages = () => {
      messagesEl.innerHTML = "";
      history.forEach((msg) => {
        const row = document.createElement("div");
        row.className = `ai-chat-row ai-chat-row--${msg.role}`;

        const bubble = document.createElement("div");
        bubble.className = `ai-chat-bubble ai-chat-bubble--${msg.role}`;
        bubble.innerHTML = sanitizeAndRender(msg.content);

        row.appendChild(bubble);
        messagesEl.appendChild(row);
      });
      if (typingEl) {
        messagesEl.appendChild(typingEl);
      }
      scrollToBottom();
    };

    const setTyping = (state) => {
      if (state) {
        typingEl = document.createElement("div");
        typingEl.className = "ai-chat-row ai-chat-row--model ai-chat-typing";
        typingEl.innerHTML = `
          <div class="ai-chat-bubble ai-chat-bubble--model">
            <span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span>
          </div>`;
        messagesEl.appendChild(typingEl);
      } else if (typingEl) {
        typingEl.remove();
        typingEl = null;
      }
      scrollToBottom();
    };

    const openPanel = () => {
      isOpen = true;
      panel.classList.add("ai-chat-panel--open");
      panel.setAttribute("aria-hidden", "false");
      panel.setAttribute("aria-modal", "true");
      inputEl.focus();
      scrollToBottom();
    };

    const closePanel = () => {
      isOpen = false;
      panel.classList.remove("ai-chat-panel--open");
      panel.setAttribute("aria-hidden", "true");
      panel.setAttribute("aria-modal", "false");
    };

    const togglePanel = () => {
      if (isOpen) {
        closePanel();
      } else {
        openPanel();
      }
    };

    const pushMessage = (role, content) => {
      history.push({ role, content });
      renderMessages();
      launcher.classList.add("ai-chat-launcher--active");
    };

    const limitLength = (text, limit) => {
      if (!text) return "";
      return text.length > limit ? `${text.slice(0, limit)} ...` : text;
    };

    const getPageContext = () => {
      const title = document.title || "";
      const path = window.location.pathname;
      const heading = document.querySelector("#main-content h1, #main-content h2");
      const metaDescription = document.querySelector('meta[name="description"]');
      const details = [
        `Path: ${path}`,
        title ? `Title: ${title}` : "",
        heading && heading.textContent ? `Heading: ${heading.textContent.trim()}` : "",
        metaDescription && metaDescription.content ? `Description: ${metaDescription.content.trim()}` : "",
      ]
        .filter(Boolean)
        .join("\n");
      return limitLength(details, 900);
    };

    const compactHistory = (limit = 5, charLimit = 900) =>
      history.slice(-limit).map((msg) => ({
        role: msg.role === "model" ? "model" : "user",
        content: limitLength(msg.content, charLimit),
      }));

    const getCsrfToken = () => {
      const meta = document.querySelector('meta[name="csrf-token"]');
      return meta ? meta.getAttribute("content") : "";
    };

    const sendMessage = async () => {
      const text = (inputEl.value || "").trim();
      if (!text || isSending) return;

      if (!config.endpoint) {
        pushMessage("model", "Chat endpoint is not configured yet. Please try again in a bit.");
        return;
      }

      pushMessage("user", text);
      inputEl.value = "";
      isSending = true;
      sendBtn.disabled = true;
      setTyping(true);

      const combinedContext = [
        getPageContext(),
        typeof config.pageContext === "string" ? limitLength(config.pageContext, 2000) : "",
      ]
        .filter(Boolean)
        .join("\n\n");

      const payload = {
        messages: compactHistory(),
        pageContext: combinedContext,
      };

      try {
        const response = await fetch(config.endpoint, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": getCsrfToken(),
          },
          credentials: "same-origin",
          body: JSON.stringify(payload),
        });

        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(data.error || "Server error");
        }

        if (!data.reply) {
          throw new Error("No reply received");
        }

        pushMessage("model", data.reply);
      } catch (error) {
        pushMessage("model", `Oops, something went wrong: ${error.message}`);
      } finally {
        isSending = false;
        sendBtn.disabled = false;
        setTyping(false);
        inputEl.focus();
      }
    };

    launcher.addEventListener("click", togglePanel);
    launcher.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        togglePanel();
      }
    });

    if (closeBtn) {
      closeBtn.addEventListener("click", closePanel);
    }

    sendBtn.addEventListener("click", sendMessage);
    inputEl.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        sendMessage();
      }
    });

    renderMessages();
  });
})();
