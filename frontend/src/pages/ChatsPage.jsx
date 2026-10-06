import { useEffect, useRef, useState } from "react";
import { Layout } from "../components/Layout";
import { useToast } from "../context/ToastContext";
import { extractErrorMessage } from "../api/client";
import { downloadChatFile, getChat, listChats } from "../api/chats";

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
    <div className={"chat-msg " + (outgoing ? "chat-msg--out" : "chat-msg--in")}>
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
          {outgoing ? "bot" : "them"} · {formatShort(message.created_at)}
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

          <div className="chats__thread">
            {!thread ? (
              <p className="chats__empty">Pick a chat on the left.</p>
            ) : (
              <>
                <div className="chats__thread-head">
                  <span className="chats__chip">
                    {thread.number}
                    {thread.name ? ` · ${thread.name}` : ""}
                    {thread.tags.length ? ` · ${thread.tags.join(", ")}` : ""}
                  </span>
                </div>
                {thread.messages.map((message) => (
                  <Message key={message.id} message={message} />
                ))}
                <div ref={threadEndRef} />
              </>
            )}
          </div>
        </div>
      </section>
    </Layout>
  );
}
