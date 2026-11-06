import { useEffect, useRef, useState } from "react";
import { Layout } from "../components/Layout";
import { useToast } from "../context/ToastContext";
import { extractErrorMessage } from "../api/client";
import { downloadChatFile, getChat, heldMessage, listChats, sendByHand, switchTakeover } from "../api/chats";

const LIVE_EVERY_MS = 5000;

const shortTime = new Intl.DateTimeFormat("en-IN", {
  timeZone: "Asia/Kolkata",
  day: "2-digit",
  month: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

function formatShort(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  // "06/10, 10:36" -> "10-06 10:36"
  const parts = Object.fromEntries(shortTime.formatToParts(date).map((p) => [p.type, p.value]));
  return `${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}

const STATUS_MARK = { sent: "✓", delivered: "✓✓", read: "✓✓", failed: "⚠" };

function ChatListItem({ chat, active, onOpen }) {
  return (
    <button type="button" className={"chat-item" + (active ? " chat-item--active" : "")} onClick={onOpen}>
      <div className="chat-item__top">
        <span className="chat-item__name">{chat.name || chat.number}</span>
        <span className="chat-item__time">{formatShort(chat.last_at)}</span>
      </div>
      <div className="chat-item__meta">
        {[...chat.tags, `${chat.message_count} msg`].join(" · ")}
        {chat.takeover?.on && <span className="takeover-badge">you have it</span>}
        {!chat.takeover?.on && chat.takeover?.pending > 0 && <span className="takeover-badge">{chat.takeover.pending} unanswered</span>}
      </div>
      <div className="chat-item__preview">{chat.preview}</div>
    </button>
  );
}

function Message({ message }) {
  const toast = useToast();
  const outgoing = message.direction === "out";
  const isFile = message.kind === "document" || message.kind === "image";
  return (
    <div className={"chat-msg " + (outgoing ? "chat-msg--out" : "chat-msg--in") + (message.sent_by ? " chat-msg--admin" : "")}>
      <div className="chat-msg__bubble">
        {message.kind === "template" && <div className="chat-msg__label">Template message</div>}
        {isFile && (
          <div className="chat-msg__file">
            <span>{message.kind === "image" ? "🖼️" : "📎"} {message.filename || "file"}</span>
            {message.has_file && (
              <button
                type="button"
                className="btn btn--ghost chat-msg__download"
                onClick={() =>
                  downloadChatFile(message.id, message.filename).catch((error) =>
                    toast.error(extractErrorMessage(error, "Could not download the file."))
                  )
                }
              >
                Download
              </button>
            )}
          </div>
        )}
        {message.text && <div className="chat-msg__text">{message.text}</div>}
        <div className="chat-msg__footer">
          {outgoing ? message.sent_by || "bot" : "them"} · {formatShort(message.created_at)}
          {outgoing && (
            <span className={"chat-msg__status chat-msg__status--" + message.status} title={message.error || message.status}>
              {" "}
              {STATUS_MARK[message.status] || ""} {message.status}
            </span>
          )}
        </div>
        {message.error && <div className="chat-msg__error">Not delivered: {message.error}</div>}
      </div>
    </div>
  );
}

const HANDOVER_SAYS = {
  "nothing pending": "Back with the bot — nothing was waiting for a reply.",
  acknowledgment: "Back with the bot — the last message was only a thanks, so no reply was sent.",
  answered: "Back with the bot — it answered their last message.",
  "still taken over": "Taken over again while the bot was replying — its reply was held.",
  failed: "Back with the bot — but it could not answer the last message; please check the chat.",
  "already with the bot": "Already with the bot.",
  "already running": "The bot is already answering.",
};

// HUMAN TAKEOVER (founder, 28 Oct, like the sales bot's console): the bot goes
// quiet on this chat, you reply by hand, and hand it back. The page shows a
// state only after the server confirmed it.
function TakeoverBar({ thread, onState }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const tk = thread.takeover || { on: false, version: 0, pending: [], held: [] };
  const pending = (tk.pending || []).length;
  async function flip() {
    if (tk.on && !window.confirm("Hand this chat back to the bot? It answers the last message if it still needs an answer.")) return;
    setBusy(true);
    try {
      const result = await switchTakeover(thread.number, !tk.on, tk.version);
      onState(result.state);
      toast.success(tk.on ? HANDOVER_SAYS[result.result] || "Back with the bot." : "You have this chat — the bot is paused.");
    } catch (error) {
      const detail = error?.response?.data?.detail;
      if (detail?.state) onState(detail.state);
      toast.error(detail?.error || extractErrorMessage(error, "Could not switch."));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className={"takeover-bar" + (tk.on ? " takeover-bar--on" : "")}>
      <span>
        {tk.on ? (
          <>
            <b>Human Takeover Active — Bot Paused</b> · {tk.by || "admin"} since {formatShort(tk.since)} — you reply to this chat
          </>
        ) : (
          <>
            <b>Bot active</b>
            {tk.handed_back_at ? ` · handed back by ${tk.by || "admin"} ${formatShort(tk.handed_back_at)}` : ""}
            {tk.resume?.status === "interrupted" ? " · the hand-back was cut off by a restart — check the chat" : ""}
          </>
        )}
        {pending > 0 && <span className="takeover-badge">{pending} unanswered</span>}
      </span>
      <button type="button" className={"btn " + (tk.on ? "btn--primary" : "btn--secondary")} onClick={flip} disabled={busy}>
        {busy ? "…" : tk.on ? "🤖 Hand back to bot" : "✋ Take over"}
      </button>
    </div>
  );
}

// What the bot wanted to say while you had the chat — kept, not lost.
function HeldBack({ thread, onState }) {
  const toast = useToast();
  const [busy, setBusy] = useState("");
  const held = thread.takeover?.held || [];
  if (!held.length) return null;
  async function act(item, action) {
    setBusy(item.id + action);
    try {
      const result = await heldMessage(thread.number, item.id, action);
      onState(result.state);
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not do that."));
    } finally {
      setBusy("");
    }
  }
  return (
    <div className="takeover-held">
      <div className="takeover-held__title">Held back from the bot ({held.length}) — it could not send these while you had the chat</div>
      {held.map((item) => (
        <div key={item.id} className="takeover-held__item">
          <span className="takeover-held__time">{formatShort(item.at)}</span>
          <span className="takeover-held__text">
            {item.kind !== "text" ? `(${item.kind}) ` : ""}
            {item.text || "—"}
          </span>
          {item.sendable && (
            <button type="button" className="btn btn--secondary btn--small" disabled={Boolean(busy)} onClick={() => act(item, "send")}>
              Send
            </button>
          )}
          <button type="button" className="btn btn--ghost btn--small" disabled={Boolean(busy)} onClick={() => act(item, "drop")}>
            Dismiss
          </button>
        </div>
      ))}
    </div>
  );
}

function Composer({ thread, onSent }) {
  const toast = useToast();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  if (!thread.takeover?.on) return null;
  async function send() {
    const body = text.trim();
    if (!body || busy) return;
    setBusy(true);
    try {
      const result = await sendByHand(thread.number, body);
      setText("");
      onSent(result.state);
    } catch (error) {
      toast.error(extractErrorMessage(error, "Not sent."));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="takeover-composer">
      <textarea
        rows={2}
        placeholder="Type your reply — it goes from the bot's WhatsApp number (Enter to send, Shift+Enter for a new line)"
        value={text}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            send();
          }
        }}
      />
      <button type="button" className="btn btn--primary" onClick={send} disabled={busy || !text.trim()}>
        {busy ? "Sending…" : "Send"}
      </button>
    </div>
  );
}

export function ChatsPage() {
  const toast = useToast();
  const [query, setQuery] = useState("");
  const [live, setLive] = useState(true);
  const [chats, setChats] = useState([]);
  const [isLoading, setIsLoading] = useState(true);
  const [openNumber, setOpenNumber] = useState(null);
  const [thread, setThread] = useState(null);
  const threadEndRef = useRef(null);
  const lastMessageId = useRef(null);

  async function loadChats(q = query) {
    try {
      setChats(await listChats(q));
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not load chats."));
    } finally {
      setIsLoading(false);
    }
  }

  function setTakeover(state) {
    if (state) setThread((current) => (current ? { ...current, takeover: state } : current));
  }

  async function loadThread(number) {
    if (!number) return;
    try {
      setThread(await getChat(number));
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not load this chat."));
    }
  }

  // Search (debounced).
  useEffect(() => {
    const timer = setTimeout(() => loadChats(query), 300);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query]);

  useEffect(() => {
    loadThread(openNumber);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openNumber]);

  // Live: refresh the list and the open chat every few seconds.
  useEffect(() => {
    if (!live) return undefined;
    const timer = setInterval(() => {
      loadChats();
      if (openNumber) loadThread(openNumber);
    }, LIVE_EVERY_MS);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live, openNumber, query]);

  // Jump to the newest message when a chat opens or a new message arrives.
  useEffect(() => {
    const messages = thread?.messages || [];
    const newest = messages.length ? messages[messages.length - 1].id : null;
    if (newest !== lastMessageId.current) {
      lastMessageId.current = newest;
      threadEndRef.current?.scrollIntoView({ block: "end" });
    }
  }, [thread]);

  return (
    <Layout title="Chats" subtitle="Who is writing to the WhatsApp line, and what the bot answered.">
      <section className="panel chats">
        <div className="chats__toolbar">
          <input
            className="chats__search"
            type="search"
            placeholder="search a number, a name, a word"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <label className="chats__live">
            <input type="checkbox" checked={live} onChange={(event) => setLive(event.target.checked)} /> live
          </label>
        </div>

        <div className="chats__body">
          <div className="chats__list">
            {isLoading ? (
              <div className="page-loading">Loading…</div>
            ) : chats.length === 0 ? (
              <p className="chats__empty">{query ? "Nothing matches that search." : "No messages yet."}</p>
            ) : (
              chats.map((chat) => (
                <ChatListItem
                  key={chat.number}
                  chat={chat}
                  active={chat.number === openNumber}
                  onOpen={() => setOpenNumber(chat.number)}
                />
              ))
            )}
          </div>

          <div className="chats__pane">
            {!thread ? (
              <p className="chats__empty">Pick a chat on the left.</p>
            ) : (
              <>
                <div className="chats__pane-head">
                  <span className="chats__chip">
                    {thread.number}
                    {thread.name ? ` · ${thread.name}` : ""}
                    {thread.tags.length ? ` · ${thread.tags.join(", ")}` : ""}
                  </span>
                  <TakeoverBar thread={thread} onState={setTakeover} />
                  <HeldBack thread={thread} onState={setTakeover} />
                </div>
                <div className="chats__thread">
                  {thread.messages.map((message) => (
                    <Message key={message.id} message={message} />
                  ))}
                  <div ref={threadEndRef} />
                </div>
                <Composer
                  thread={thread}
                  onSent={(state) => {
                    setTakeover(state);
                    loadThread(openNumber);
                  }}
                />
              </>
            )}
          </div>
        </div>
      </section>
    </Layout>
  );
}
